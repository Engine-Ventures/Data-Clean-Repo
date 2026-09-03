-- EV Deal Pipeline — data layer (PostgreSQL / Neon)
--
-- Implements the architecture in the handoff brief §9:
--   * immutable entity IDs + alias table (never auto-match on name alone)
--   * append-only observations; transitions derived from diffs
--   * store inputs, derive metrics (no persisted computed columns)
--   * provenance on every enriched field
--   * review queue, not silent writes
--   * missing runs recorded explicitly
--
-- DDL ONLY. The controlled vocabularies that used to be seeded inline here
-- now live in seed.sql, so this file can be re-run against a shared Neon
-- branch without fighting over rows. Both files are idempotent: every object
-- is IF NOT EXISTS / OR REPLACE, and seed.sql inserts ON CONFLICT DO NOTHING.
--
-- Ported from SQLite. The porting decisions, so they are not re-litigated:
--   * surrogate keys are GENERATED ALWAYS AS IDENTITY. SQLite's bare
--     `INTEGER PRIMARY KEY` was an implicit rowid alias; in Postgres it is a
--     plain integer with no default, and every insert that omits the id would
--     fail. Tables whose PK is supplied explicitly (stage) or borrowed from a
--     parent (money_value, entity_outcome, entity_sourcing) stay plain integer.
--   * timestamps written by the database (created_at, started_at, ...) are
--     timestamptz DEFAULT now(). Date-shaped TEXT columns that are compared
--     against each other as text — meeting_date, field_value.value_text,
--     first_meeting — are deliberately NOT retyped; see the note on
--     v_entity_funnel.first_slide_date below.
--   * money is numeric, not real. Postgres `real` is float4 (~6 significant
--     digits), which visibly rounds an eight-figure round size; SQLite's REAL
--     was 8-byte and hid this.
--   * 0/1 integer flags (is_bold, is_zero, is_phantom, ev_participated,
--     is_terminal) stay integers rather than becoming boolean, because
--     v_entity_funnel and v_entity_discussion SUM() and MAX() over them and
--     Postgres rejects those aggregates on a boolean.
--
-- Grain notes (numbers verified against DiligenceCompanies_EVPipeline.xlsx,
-- the 2026-09-02 rescoped/deduplicated workbook — 185 companies, 43 weekly
-- meetings, 1,207-row Stage History sheet):
--   slide_observation  = company x meeting  (the 1,207-row evidence log; append-only)
--   field_value        = entity x field x write  (append-only; current value = latest)
--
-- Three-state nullability is explicit throughout. A value is:
--   present            -> field_value row with value_text/value_num set
--   genuinely zero     -> value_num = 0 with is_zero = 1
--   unknown, unchecked -> no field_value row, gap_status.state = 'not_checked'
--   unknown, confirmed -> gap_status.state = 'confirmed_unavailable'
-- Blank, zero and N/A are never interchangeable. (Confirmed in the workbook:
-- Companies!round_size_usd is blank, never 0, wherever Affinity's underlying
-- round size was 0 — per the workbook's own Data Dictionary and READ ME.)
--
-- No `PRAGMA foreign_keys = ON` equivalent is needed or possible: Postgres
-- always enforces foreign keys. Note that this makes the schema strictly
-- stricter than the SQLite original, where the PRAGMA was set on connections
-- opened through db.connect() but not on every path that touched the file.

-- ---------------------------------------------------------------------------
-- Controlled vocabularies (§4). Rows are seeded by seed.sql.
--
-- "All enums are FK-enforced; no free text" is what this section used to
-- claim, and it is true of 8 of the 11 tables below. It is NOT true of
-- working_group, affinity_status and enrichment_priority: those three are
-- consumed as field_value.value_text, which has no FK, so nothing stops a
-- value outside the picklist from being written. That is why a dead
-- enrichment_priority member ('P3 - sparse', which metrics.py could never
-- emit) survived unnoticed until tests/test_vocab.py went looking. Making
-- those three enforceable means either a per-field FK on field_value or a
-- CHECK against the vocabulary, and is a separate pass; until then
-- tests/test_vocab.py is the only thing holding them in line.
-- ---------------------------------------------------------------------------

-- Stage funnel, ordered. rank 7 = furthest.
-- The 7 stage names are exactly the 7 distinct values found in
-- Stage History!stage_on_slide (and Companies!furthest_stage_on_slides /
-- latest_stage_on_slides) in the current workbook — there is no 8th value
-- (e.g. no separate "Meetings This Week" vs "Sourcing" split).
--
-- stage_id is supplied explicitly by seed.sql and equals rank, so ORDER BY /
-- MAX() work on it directly. The workbook's Data Dictionary describes
-- furthest-stage priority as "Legal > Negotiate > Deep Dil > Prelim Dil >
-- NewCo/Hold > Meetings" — i.e. NewCo/Fellows and Hold/Nurture are treated as
-- functionally tied, both below Preliminary Diligence. ranks 2/3 are an
-- arbitrary tie-break (this table's UNIQUE(rank) requires one); nothing
-- downstream depends on their relative order, only on being < 4. What DOES
-- depend on it is that every producer of stage_id agrees which name owns
-- which id — see the note in seed.sql.
CREATE TABLE IF NOT EXISTS stage (
    stage_id    integer PRIMARY KEY,          -- == rank, so ORDER BY works directly
    name        text    NOT NULL UNIQUE,
    rank        integer NOT NULL UNIQUE CHECK (rank BETWEEN 1 AND 7)
);

CREATE TABLE IF NOT EXISTS thesis_area (
    code        text PRIMARY KEY,             -- HH, AS, E&C, AIFS
    name        text NOT NULL UNIQUE
);

-- Maps every raw slide-section string seen in extraction onto a stage + thesis
-- area. This is where slide-section normalisation happens, rather than in
-- ingest code. In this dataset each raw_section maps to exactly one
-- stage_on_slide (verified — no ambiguous section string was ever seen against
-- two different stages), so the one-to-one FK design below is sufficient for
-- this data as-is; a future extraction that reused a thesis code across two
-- stages would need this table keyed on (raw_section, stage_id) instead of
-- raw_section alone.
CREATE TABLE IF NOT EXISTS slide_section_map (
    raw_section text PRIMARY KEY,
    stage_id    integer REFERENCES stage(stage_id),
    thesis_code text    REFERENCES thesis_area(code),
    note        text
);

CREATE TABLE IF NOT EXISTS working_group (
    name        text PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS round_stage (
    name        text PRIMARY KEY,
    rank        integer NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS affinity_status (
    name        text PRIMARY KEY,
    rank        integer                       -- NULL where not funnel-ordered
);

CREATE TABLE IF NOT EXISTS enrichment_priority (
    name        text PRIMARY KEY,
    tier        integer NOT NULL CHECK (tier BETWEEN 1 AND 4)
);

-- Sources, in precedence order (§2): slides define population and stage
-- history, Affinity enriches only, public fills gaps, manual is a human write.
CREATE TABLE IF NOT EXISTS source (
    name        text PRIMARY KEY,
    precedence  integer NOT NULL UNIQUE
);

-- ---------------------------------------------------------------------------
-- Entities and aliases
-- ---------------------------------------------------------------------------

-- The canonical company. entity_id is permanent and internal; it is NOT the
-- spreadsheet's EV#### id, which the brief notes is stable within one file only.
CREATE TABLE IF NOT EXISTS entity (
    entity_id       integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    canonical_name  text    NOT NULL,
    domain          text,                     -- match on this first; most stable
    created_at      timestamptz NOT NULL DEFAULT now(),
    -- Set when this entity has been merged into another. Rows are never
    -- deleted; a merge redirects. NULL means live.
    merged_into     integer REFERENCES entity(entity_id),
    -- Phantom rows (PDF line-wrap artifacts) are marked, not deleted, so the
    -- extraction record stays auditable.
    is_phantom      integer NOT NULL DEFAULT 0 CHECK (is_phantom IN (0,1)),
    phantom_reason  text,
    CHECK (merged_into IS NULL OR merged_into <> entity_id)
);

CREATE INDEX IF NOT EXISTS idx_entity_live   ON entity(merged_into) WHERE merged_into IS NULL;
-- Deliberately NOT unique. Even after the workbook's own 2026-09-02 dedup
-- pass (which merged 15 name-variant groups, 498 -> 481 rows), the current
-- Companies sheet still has ONE domain shared by two live rows that were
-- never merged: "Attune Neurosci" and "Attune Neurosciences" both carry
-- domain attuneneuro.com, even though the READ ME dedup log claims
-- "Attune Neurosci+Attune" was merged (that log entry doesn't actually match
-- either surviving spelling). This is exactly the kind of gap this index
-- exists to surface, not hide: it becomes a merge_proposal review_item
-- rather than being silently rejected or silently merged at load.
CREATE INDEX IF NOT EXISTS idx_entity_domain ON entity(domain) WHERE domain IS NOT NULL;
-- Case-insensitive lookup for write.find_duplicates and validate.resolve_entity.
-- Replaces SQLite's `= ? COLLATE NOCASE`, which has no Postgres equivalent:
-- the queries now say lower(canonical_name) = lower(%s), and without these
-- they would seq-scan entity and alias on every single write.
CREATE INDEX IF NOT EXISTS idx_entity_name_lower ON entity(lower(canonical_name));

-- Every observed spelling -> entity. Includes slide names, Affinity names and
-- line-wrap variants. This is what makes name fragmentation fixable without
-- touching the evidence log.
CREATE TABLE IF NOT EXISTS alias (
    alias_id    integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity_id   integer NOT NULL REFERENCES entity(entity_id),
    alias_text  text    NOT NULL,
    alias_norm  text    NOT NULL,             -- casefolded/punct-stripped
    source      text    NOT NULL REFERENCES source(name),
    -- How the alias was established. 'name' alone is never auto-applied (§9).
    match_method text   NOT NULL CHECK (match_method IN
                    ('exact','domain','affinity_id','manual','line_wrap','fuzzy')),
    confidence  double precision,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (alias_text)
);

CREATE INDEX IF NOT EXISTS idx_alias_entity ON alias(entity_id);
CREATE INDEX IF NOT EXISTS idx_alias_norm   ON alias(alias_norm);
CREATE INDEX IF NOT EXISTS idx_alias_text_lower ON alias(lower(alias_text));

-- Grouped slide entries such as "Cetos / Kira /Eden Tech" — this literal
-- string (with several punctuation variants: trailing slash, missing space,
-- leading "Water:") recurs across multiple meetings in Stage History, and
-- "ScaleLight / TopoLight" and "Flume / Gravity" behave the same way.
-- Modelled as a real relation so the split decision is reversible either
-- way: the group row stays an entity, and its components are linked.
-- Deleting the row would lose the evidence — and the workbook's own READ ME
-- confirms this was a live problem: it flags that the ScaleLight/TopoLight
-- slide entry actually hides two distinct Affinity companies (org
-- 311517728 "Wait" and org 305936259 "Pass") that were never split apart.
CREATE TABLE IF NOT EXISTS entity_group_member (
    group_entity_id     integer NOT NULL REFERENCES entity(entity_id),
    component_entity_id integer NOT NULL REFERENCES entity(entity_id),
    PRIMARY KEY (group_entity_id, component_entity_id),
    CHECK (group_entity_id <> component_entity_id)
);

-- ---------------------------------------------------------------------------
-- Meetings, including the ones that did not happen
-- ---------------------------------------------------------------------------

-- A skipped meeting must not read as company attrition (§9), so absent weeks
-- are rows with status <> 'held'. The current workbook's Stage History sheet
-- contains exactly 43 distinct meeting_date values, running weekly from
-- 2025-10-14 through 2026-08-31 with no gaps observed in that span — so no
-- 'not_held' rows are currently evidenced, but the state exists for when one
-- occurs.
--
-- meeting_date stays text, not date. It is compared as text against
-- field_value.value_text (ingest.flag_predating_relationships) and against
-- v_entity_funnel.first_slide_date (validate.check_first_meeting_order).
-- SQLite would compare text to a date and return nonsense; Postgres refuses
-- outright. Retyping this column means finding and fixing every one of those
-- comparisons in the same commit, which is a separate pass.
CREATE TABLE IF NOT EXISTS meeting (
    meeting_date text PRIMARY KEY CHECK (meeting_date LIKE '____-__-__'),
    status       text NOT NULL CHECK (status IN
                     ('held','not_held','not_extracted','unknown')),
    slide_page   integer,
    note         text
);

-- ---------------------------------------------------------------------------
-- Slide observations — the append-only evidence log
-- ---------------------------------------------------------------------------

-- One row per company per meeting, straight from extraction. NEVER edited
-- (§3): corrections go to slide_observation_override, which shadows this.
CREATE TABLE IF NOT EXISTS slide_observation (
    observation_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    meeting_date   text    NOT NULL REFERENCES meeting(meeting_date),
    entity_id      integer NOT NULL REFERENCES entity(entity_id),
    name_on_slide  text    NOT NULL,          -- verbatim, pre-alias-resolution
    stage_id       integer NOT NULL REFERENCES stage(stage_id),
    raw_section    text    REFERENCES slide_section_map(raw_section),
    is_bold        integer NOT NULL CHECK (is_bold IN (0,1)),
    -- Whether bold means "discussed" vs "on the agenda" is unresolved, and
    -- the deck legend distinguishes black (Update) from dark red (Discussion)
    -- from blue (Fellow/Internal). Colour is NOT captured anywhere in the
    -- current workbook (Stage History!discussed_bold is a plain boolean with
    -- no colour dimension), so this stays nullable pending re-extraction from
    -- the source PDF; agenda_type would then be derivable from it.
    bold_color     text CHECK (bold_color IN ('black','dark_red','blue')),
    slide_page     integer,
    extracted_at   text
);

CREATE INDEX IF NOT EXISTS idx_obs_entity  ON slide_observation(entity_id, meeting_date);
CREATE INDEX IF NOT EXISTS idx_obs_meeting ON slide_observation(meeting_date);
-- Natural key includes stage and section because a company is routinely
-- listed twice at one meeting: once under "Meetings this week" (stage 1) and
-- once under its thesis prelim-diligence sub-section (stage 4). 66 such
-- dual-listings exist in the current workbook's 1,207-row Stage History
-- sheet (e.g. OceanBit and Plaid Semi both appear this way on 2025-10-20).
-- "Meetings This Week" is an agenda marker that coexists with a funnel
-- position, not a mutually exclusive stage — so collapsing on
-- (date, entity, name) would silently drop real evidence.
--
-- NULLS NOT DISTINCT is new here and is a fix, not a port. raw_section is
-- nullable, and under the SQLite index two rows with a NULL raw_section never
-- collided — so `INSERT OR IGNORE` silently failed to dedupe exactly the rows
-- most likely to be re-extracted. No row in the current data has a NULL
-- raw_section, so this changes nothing today; it closes the hole before the
-- next extraction opens it. (Requires PG >= 15; Neon is 16/17.)
CREATE UNIQUE INDEX IF NOT EXISTS idx_obs_grain
    ON slide_observation(meeting_date, entity_id, name_on_slide, stage_id, raw_section)
    NULLS NOT DISTINCT;

-- Corrections that shadow the evidence log without mutating it.
CREATE TABLE IF NOT EXISTS slide_observation_override (
    override_id    integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    observation_id integer NOT NULL REFERENCES slide_observation(observation_id),
    field          text    NOT NULL CHECK (field IN
                        ('stage_id','is_bold','bold_color','entity_id','raw_section')),
    new_value      text,
    reason         text    NOT NULL,
    created_by     text    NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (observation_id, field)
);

-- ---------------------------------------------------------------------------
-- Field-level provenance (§8: every write stores value, source, user, time)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS field_value (
    field_value_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity_id      integer NOT NULL REFERENCES entity(entity_id),
    field          text    NOT NULL,
    value_text     text,
    value_num      double precision,
    -- Distinguishes a real 0 from unknown. Required because Affinity's 0
    -- round sizes were read as unknown, not zero (per the workbook's own
    -- Data Dictionary note on round_size_usd, and confirmed in Companies:
    -- every row with a blank round_size_usd corresponds to an Affinity 0,
    -- never a literal 0 in the delivered file).
    is_zero        integer NOT NULL DEFAULT 0 CHECK (is_zero IN (0,1)),
    source         text    NOT NULL REFERENCES source(name),
    citation       text,                      -- required for source='Public'
    created_by     text    NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    superseded_at  timestamptz,               -- NULL = current
    CHECK (is_zero = 0 OR value_num = 0),
    CHECK (source <> 'Public' OR citation IS NOT NULL)
);

-- One current value per (entity, field). This is UNIQUE where the SQLite
-- index was not, and it is the point of the whole migration: write_field
-- supersedes-then-inserts, which is a read-modify-write race the moment two
-- people edit the same field through the API. Without this, concurrent writes
-- leave two rows with superseded_at IS NULL, v_field_current returns both, and
-- every join through it silently double-counts that entity. With it, the
-- second writer gets a unique violation the request handler can catch and
-- retry instead of corrupting the read model.
CREATE UNIQUE INDEX IF NOT EXISTS uq_fv_one_current ON field_value(entity_id, field)
    WHERE superseded_at IS NULL;

-- Note on score_team / score_tech / score_oppt: the workbook stores these as
-- text with a leading apostrophe ("'+", "'++", "'+++") — that's Excel's
-- force-text marker on the cell, not part of the value. Ingest strips the
-- leading apostrophe before writing value_text here (stores '+', '++',
-- '+++'), or the "'" would be mistaken for data on every read.

-- Currency is always stored as _usd plus the local amount alongside (§8).
-- numeric, not real: see the porting note at the top of this file.
CREATE TABLE IF NOT EXISTS money_value (
    field_value_id integer PRIMARY KEY REFERENCES field_value(field_value_id),
    amount_usd     numeric NOT NULL,
    amount_local   numeric,
    currency       text,
    fx_date        text,
    CHECK (amount_local IS NULL OR currency IS NOT NULL)
);

-- Three-state gap tracking (§8 Worklist): "not yet looked at" is distinct from
-- "checked, genuinely unavailable". The current workbook's Enrichment
-- Worklist sheet (129 rows) is exactly the projection of this state where it
-- is not yet 'filled' — i.e. needs_website/needs_geography/needs_stage/
-- needs_round_size/needs_owner = TRUE for at least one field.
CREATE TABLE IF NOT EXISTS gap_status (
    entity_id   integer NOT NULL REFERENCES entity(entity_id),
    field       text    NOT NULL,
    state       text    NOT NULL CHECK (state IN
                    ('not_checked','confirmed_unavailable','filled')),
    checked_by  text,
    checked_at  timestamptz,
    note        text,
    PRIMARY KEY (entity_id, field)
);

-- ---------------------------------------------------------------------------
-- Fields that exist in no source and must be captured going forward (§7)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS pass_reason_category (
    name text PRIMARY KEY,
    sort integer
);

CREATE TABLE IF NOT EXISTS outcome_type (
    name text PRIMARY KEY,
    is_terminal integer NOT NULL DEFAULT 0 CHECK (is_terminal IN (0,1))
);

CREATE TABLE IF NOT EXISTS source_channel (
    name text PRIMARY KEY
);

-- Disappearing from a slide is not a decision (§7): an outcome must be stated.
-- Nothing in the current workbook captures this explicitly today —
-- affinity_status has 'Pass' and 'Loss' values, but 63 of 185 companies in
-- Companies have no Affinity record at all (in_affinity = FALSE), so there is
-- no outcome field for them if they simply stop appearing on slides. This
-- table is forward-looking, populated going forward rather than backfilled
-- from the workbook.
CREATE TABLE IF NOT EXISTS entity_outcome (
    entity_id       integer PRIMARY KEY REFERENCES entity(entity_id),
    outcome         text    NOT NULL REFERENCES outcome_type(name),
    outcome_date    text,
    pass_category   text    REFERENCES pass_reason_category(name),
    pass_notes      text,
    recorded_by     text    NOT NULL,
    recorded_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS entity_sourcing (
    entity_id      integer PRIMARY KEY REFERENCES entity(entity_id),
    sourcer        text,                      -- distinct from owner
    channel        text REFERENCES source_channel(name),
    referrer_name  text,
    recorded_by    text,
    recorded_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS founder (
    founder_id  integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity_id   integer NOT NULL REFERENCES entity(entity_id),
    full_name   text    NOT NULL,
    role        text
);

-- Rounds the company raised, including ones EV did not join — needed for
-- graduation rates and the anti-portfolio (§7). Not populated from the
-- workbook: Companies!round_size_usd/stage capture only the current/latest
-- round, with no history, so this table starts empty and is filled going
-- forward or via further public-source enrichment.
CREATE TABLE IF NOT EXISTS funding_round (
    round_id        integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity_id       integer NOT NULL REFERENCES entity(entity_id),
    round_stage     text    REFERENCES round_stage(name),
    announced_date  text,
    amount_usd      numeric,
    pre_money_usd   numeric,
    post_money_usd  numeric,
    ev_participated integer CHECK (ev_participated IN (0,1)),
    citation        text
);

CREATE TABLE IF NOT EXISTS meeting_attendee (
    meeting_date text NOT NULL REFERENCES meeting(meeting_date),
    person       text NOT NULL,
    PRIMARY KEY (meeting_date, person)
);

-- ---------------------------------------------------------------------------
-- Review queue — fuzzy matches, first appearances, line-wrap candidates and
-- stage jumps >2 levels go to a human instead of being written silently (§9).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS review_item (
    review_id   integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind        text    NOT NULL CHECK (kind IN
                    ('fuzzy_match','first_appearance','line_wrap_candidate',
                     'stage_jump','merge_proposal','group_split','stage_regression',
                     'source_conflict','duplicate_listing')),
    entity_id   integer REFERENCES entity(entity_id),
    target_id   integer REFERENCES entity(entity_id),  -- merge/split counterpart
    detail      text    NOT NULL,
    confidence  double precision,
    proposed_by text    NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    state       text    NOT NULL DEFAULT 'open'
                    CHECK (state IN ('open','accepted','rejected','deferred')),
    resolved_by text,
    resolved_at timestamptz,
    resolution_note text
);

CREATE INDEX IF NOT EXISTS idx_review_open ON review_item(kind, state) WHERE state = 'open';

-- ---------------------------------------------------------------------------
-- Ingest run log — every load is recorded with a coverage report (§9 governance)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ingest_run (
    run_id       integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    source_file  text NOT NULL,
    source_sha256 text,
    schema_version text NOT NULL,
    row_counts   jsonb,
    note         text
);

-- ---------------------------------------------------------------------------
-- Derived views. Metrics are computed, never stored (§9).
--
-- CREATE OR REPLACE VIEW rather than IF NOT EXISTS, which Postgres does not
-- have for views. Note the consequence for re-runs: OR REPLACE will refuse a
-- definition that changes an existing view's column names, types or order, so
-- a future edit of that shape needs an explicit DROP VIEW ... CASCADE first.
-- Views are declared in dependency order; v_observation must exist before the
-- four views that read it.
-- ---------------------------------------------------------------------------

-- Effective observations: evidence log with overrides applied.
--
-- The override casts are guarded. SQLite's CAST('blue' AS INTEGER) quietly
-- returned 0; Postgres raises invalid input syntax for type integer and takes
-- down this view — and therefore every view and metric downstream of it. The
-- `field =` predicates mean only numeric-valued overrides should reach a cast
-- today, so the guard is defence against the first typo'd override row, not a
-- current bug. A non-numeric value in a numeric override field now reads as
-- "no override" instead of crashing the read path.
CREATE OR REPLACE VIEW v_observation AS
SELECT
    o.observation_id,
    o.meeting_date,
    COALESCE((SELECT CASE WHEN v.new_value ~ '^-?\d+$' THEN v.new_value::integer END
              FROM slide_observation_override v
              WHERE v.observation_id = o.observation_id AND v.field = 'entity_id'),
             o.entity_id)                                    AS entity_id,
    o.name_on_slide,
    COALESCE((SELECT CASE WHEN v.new_value ~ '^-?\d+$' THEN v.new_value::integer END
              FROM slide_observation_override v
              WHERE v.observation_id = o.observation_id AND v.field = 'stage_id'),
             o.stage_id)                                      AS stage_id,
    COALESCE((SELECT CASE WHEN v.new_value ~ '^-?\d+$' THEN v.new_value::integer END
              FROM slide_observation_override v
              WHERE v.observation_id = o.observation_id AND v.field = 'is_bold'),
             o.is_bold)                                       AS is_bold,
    COALESCE((SELECT v.new_value FROM slide_observation_override v
              WHERE v.observation_id = o.observation_id AND v.field = 'bold_color'),
             o.bold_color)                                    AS bold_color,
    o.raw_section,
    o.slide_page
FROM slide_observation o;

-- Resolve an entity through any merge chain (one hop is enough by construction:
-- merges always target a live entity).
CREATE OR REPLACE VIEW v_entity_resolved AS
SELECT e.entity_id,
       COALESCE(e.merged_into, e.entity_id) AS live_entity_id
FROM entity e;

-- Current value of every field, provenance attached.
CREATE OR REPLACE VIEW v_field_current AS
SELECT fv.entity_id, fv.field, fv.value_text, fv.value_num, fv.is_zero,
       fv.source, fv.citation, fv.created_by, fv.created_at
FROM field_value fv
WHERE fv.superseded_at IS NULL;

-- Stage transitions derived from consecutive observations (§9: derive from
-- diffs, do not persist).
--
-- The derived table is aliased (AS t). SQLite allowed an unaliased subquery in
-- FROM; Postgres treats it as a syntax error, which made this view and
-- v_entity_latest_stage the two hard failures of the port.
--
-- The window is collapsed to one stage per (entity, meeting_date) before the
-- LAG, and this is a correctness fix, not a tidy-up. Two things were wrong
-- with lagging over the raw observation grain:
--
--   1. NONDETERMINISM. ORDER BY meeting_date alone is not a total order over
--      that grain -- 74 (entity, date) pairs in the current data carry more
--      than one stage -- so which row LAG() saw was left to the executor, and
--      the derived delta changed between runs. That is not cosmetic:
--      ingest.flag_stage_jumps writes a review_item per transition with
--      delta > 2 or delta < 0, so the review queue itself was unreproducible.
--      Measured over data/pipeline.db, the flagged count moves between 172 and
--      192 purely on how ties happen to break -- against this project's own
--      claim that two builds can be diffed. GROUP BY makes (entity_id,
--      meeting_date) unique in the window's input, so ORDER BY meeting_date is
--      now a total order and no tiebreaker column is needed at all. That is
--      why the collapse is preferred over appending observation_id (or
--      stage_id, observation_id) to the ORDER BY: a tiebreaker picks a winner
--      among rows that should never have been separate window rows, where the
--      GROUP BY removes the tie.
--
--   2. SAME-DATE "TRANSITIONS" ARE NOT TRANSITIONS. A from_date = to_date row
--      asserts a stage move within one slide, which no slide can evidence.
--      70 of the 74 multi-stage dates are the legitimate dual listing this
--      file already documents on idx_obs_grain: "Meetings this week" (stage 1)
--      is an agenda marker that coexists with a funnel position, so every week
--      a prelim-diligence company was on the agenda manufactured a spurious
--      +3 "Meetings This Week -> Preliminary Diligence" jump. Those artifacts
--      were the majority of the queue: 175 flagged items over the raw grain
--      against 64 here.
--
-- MAX(stage_id) is the collapse rule because it is the one already in use
-- everywhere else -- v_entity_funnel.furthest_stage_id, and
-- scripts/screen_diligence.py, which re-derives jumps client-side with
-- `per[wi] = max(stage, ...)` precisely because this view's numbers were
-- unusable. The view now agrees with its only two consumers instead of being
-- worked around by them.
--
-- The remaining 4 multi-stage dates are all Ovelle and are a different animal
-- -- a stale slide row, not a dual listing; see the note below on
-- flag_duplicate_listings, which does not currently catch them.
--
-- Column names, types and order are unchanged, so CREATE OR REPLACE still
-- applies cleanly over the previous definition (see the note above on what
-- OR REPLACE will refuse).
CREATE OR REPLACE VIEW v_stage_transition AS
SELECT entity_id,
       prev_date  AS from_date,
       meeting_date AS to_date,
       prev_stage AS from_stage_id,
       stage_id   AS to_stage_id,
       stage_id - prev_stage AS delta
FROM (
    SELECT d.entity_id, d.meeting_date, d.stage_id,
           LAG(d.stage_id)     OVER w AS prev_stage,
           LAG(d.meeting_date) OVER w AS prev_date
    FROM (
        SELECT o.entity_id, o.meeting_date, MAX(o.stage_id) AS stage_id
        FROM v_observation o
        GROUP BY o.entity_id, o.meeting_date
    ) AS d
    WINDOW w AS (PARTITION BY d.entity_id ORDER BY d.meeting_date)
) AS t
WHERE prev_stage IS NOT NULL AND prev_stage <> stage_id;

-- Companies carrying two *funnel* stages on one slide. The agenda marker
-- (stage 1, "Meetings this week") is excluded from the count rather than used
-- as an escape hatch: it explains one funnel position coexisting with it, not
-- two. Counting distinct stage_id > 1 is what separates the 4 real conflicts
-- from the 70 legitimate dual listings -- filtering on "has no stage 1 row"
-- instead would miss the two Ovelle dates that carry the agenda marker AND
-- both funnel stages.
-- This is the residue the collapse in v_stage_transition hides by
-- construction, and hiding it silently would be exactly the "silent write"
-- §9 forbids -- so it is exposed as its own view for ingest to queue.
--
-- ingest.flag_duplicate_listings does NOT catch these: it requires
-- COUNT(DISTINCT name_on_slide) > 1, and these rows carry one spelling under
-- two sections. All 4 current rows are Ovelle (2026-02-23, 03-02, 03-09,
-- 03-16), which sat in Hold / Nurture from 2025-11-17, began appearing in the
-- HH prelim-diligence sub-section on 2026-02-23 and reached Deep Diligence on
-- 03-16 while the Hold / Nurture row was still on the slide -- i.e. the deck
-- was not updated for four meetings after the promotion. MAX() reads that
-- correctly (a stale row is always the lower stage), but a human should
-- confirm it rather than the pipeline assuming it.
CREATE OR REPLACE VIEW v_same_slide_stage_conflict AS
SELECT o.entity_id,
       o.meeting_date,
       MIN(o.stage_id) FILTER (WHERE o.stage_id > 1) AS lower_stage_id,
       MAX(o.stage_id) FILTER (WHERE o.stage_id > 1) AS upper_stage_id,
       COUNT(*)                                      AS observations
FROM v_observation o
GROUP BY o.entity_id, o.meeting_date
HAVING COUNT(DISTINCT o.stage_id) FILTER (WHERE o.stage_id > 1) > 1;

-- Per-entity funnel summary: furthest stage, first date at each level,
-- appearance counts, bold counts. Replaces the reached_*/date_* columns.
CREATE OR REPLACE VIEW v_entity_funnel AS
SELECT
    o.entity_id,
    COUNT(*)                                   AS slide_appearances,
    MIN(o.meeting_date)                        AS first_slide_date,
    MAX(o.meeting_date)                        AS last_slide_date,
    MAX(o.stage_id)                            AS furthest_stage_id,
    SUM(o.is_bold)                             AS bold_appearances,
    -- Two distinct semantics, deliberately both exposed.
    --
    -- observed_at_* : the company was actually seen in that stage's column.
    --   This is what the workbook's Trend Answers sheet counts (its
    --   "Reached Deep Diligence" = 15 and "Reached Legal" = 6 are both
    --   really observed_at_*, computed via COUNTIFS on reached_deep_diligence
    --   / reached_legal — which in the *workbook* actually store observed-at
    --   semantics despite the reached_* naming).
    -- reached_*     : the company got at least that far in the funnel.
    --
    -- They differ, and by more than one company. In the current workbook's
    -- Stage History, two companies were seen in Preliminary Diligence and
    -- then jumped straight to Legal Diligence without ever being observed at
    -- Deep Diligence or Negotiate/Offer: Axiomatic AI (2025-12-15, Prelim ->
    -- Legal in one week) and Corepower Magnetics. Reporting must state which
    -- is meant; a funnel conversion rate wants reached_*, a "who did we look
    -- at closely" count wants observed_at_*.
    --
    -- Every threshold here is >= 4 or = 4..7, which is why the stage 2/3
    -- naming swap fixed in seed.sql moves none of these numbers.
    MAX(CASE WHEN o.stage_id  = 4 THEN 1 ELSE 0 END) AS observed_at_prelim_diligence,
    MAX(CASE WHEN o.stage_id  = 5 THEN 1 ELSE 0 END) AS observed_at_deep_diligence,
    MAX(CASE WHEN o.stage_id  = 6 THEN 1 ELSE 0 END) AS observed_at_negotiate_offer,
    MAX(CASE WHEN o.stage_id  = 7 THEN 1 ELSE 0 END) AS observed_at_legal,
    MAX(CASE WHEN o.stage_id >= 4 THEN 1 ELSE 0 END) AS reached_prelim_diligence,
    MAX(CASE WHEN o.stage_id >= 5 THEN 1 ELSE 0 END) AS reached_deep_diligence,
    MAX(CASE WHEN o.stage_id >= 6 THEN 1 ELSE 0 END) AS reached_negotiate_offer,
    MAX(CASE WHEN o.stage_id >= 7 THEN 1 ELSE 0 END) AS reached_legal,
    MIN(CASE WHEN o.stage_id >= 4 THEN o.meeting_date END) AS date_prelim_diligence,
    MIN(CASE WHEN o.stage_id >= 5 THEN o.meeting_date END) AS date_deep_diligence,
    MIN(CASE WHEN o.stage_id >= 6 THEN o.meeting_date END) AS date_negotiate_offer,
    MIN(CASE WHEN o.stage_id >= 7 THEN o.meeting_date END) AS date_legal
FROM v_observation o
GROUP BY o.entity_id;

-- Latest observed stage per entity. Derived table aliased, as above.
--
-- Collapsed on (entity, meeting_date) for the same reason as
-- v_stage_transition, and it was the same bug: ORDER BY meeting_date DESC is
-- not a total order over the raw observation grain, so for any entity whose
-- LAST slide date carries more than one stage, ROW_NUMBER() = 1 picked an
-- arbitrary row and latest_stage_id changed between runs. 14 entities in the
-- current data are exposed, and the worst of them is not subtle: Axiomatic AI
-- was last seen on 2025-12-15 at both stage 7 (Legal) and stage 1 (the agenda
-- marker), so its "latest stage" could read as Meetings This Week. MAX() is
-- the same collapse rule used by v_entity_funnel.furthest_stage_id, and after
-- the GROUP BY the ordering key is unique, so rn = 1 is well defined.
CREATE OR REPLACE VIEW v_entity_latest_stage AS
SELECT entity_id, meeting_date AS last_seen, stage_id AS latest_stage_id
FROM (
    SELECT d.entity_id, d.meeting_date, d.stage_id,
           ROW_NUMBER() OVER (PARTITION BY d.entity_id
                              ORDER BY d.meeting_date DESC) rn
    FROM (
        SELECT o.entity_id, o.meeting_date, MAX(o.stage_id) AS stage_id
        FROM v_observation o
        GROUP BY o.entity_id, o.meeting_date
    ) AS d
) AS t
WHERE rn = 1;

-- Discussion summary. Kept separate from "on agenda" so that when the
-- bold-colour question above is resolved, agenda_type can be layered on
-- without reshaping anything. In the current workbook 34 of 185 companies
-- have discussed = TRUE (Companies!discussed_in_new_deal_meeting), matching
-- Trend Answers' "Discussed (bold on a slide)" population-A count of 34.
--
-- Collapsed to one row per (entity, meeting_date) before aggregating, the
-- same rule as v_stage_transition and v_entity_latest_stage. What this fixes
-- and what it does not:
--
--   * `discussed` is unaffected either way. MAX(is_bold) is idempotent under
--     the collapse, so the 34/185 binary is identical before and after. It is
--     not a number this change is allowed to move, and it does not.
--   * `times_discussed` was SUM(is_bold) over the RAW observation grain, which
--     counts one discussion once per row rather than once per meeting. 74
--     (entity, date) pairs in the current data carry more than one row (the
--     legitimate agenda-marker dual listing), and 8 of them carry a bold row.
--     Measured against this data the total does NOT move: 152 bold rows map to
--     152 distinct (entity, date) pairs, because in every one of those 8 the
--     bold mark sits on exactly one of the two rows. So this is a latent bug,
--     not a live wrong number -- it starts inflating the moment an extraction
--     marks both rows of a dual listing bold, which is a presentational
--     detail of the deck, not a fact about how often a company was discussed.
--
-- MAX(is_bold) is the collapse rule for the same reason MAX(stage_id) is
-- elsewhere: bold anywhere on the slide for that company at that meeting means
-- the company was discussed at that meeting, once.
CREATE OR REPLACE VIEW v_entity_discussion AS
SELECT entity_id,
       MAX(is_bold)                                          AS discussed,
       SUM(is_bold)                                          AS times_discussed,
       MIN(CASE WHEN is_bold = 1 THEN meeting_date END)       AS first_discussion_date,
       MAX(CASE WHEN is_bold = 1 THEN meeting_date END)       AS last_discussion_date
FROM (
    SELECT o.entity_id, o.meeting_date, MAX(o.is_bold) AS is_bold
    FROM v_observation o
    GROUP BY o.entity_id, o.meeting_date
) AS d
GROUP BY entity_id;

-- Dwell time: consecutive meetings an entity spent at each stage.
--
-- The previous definition was `COUNT(*) ... GROUP BY entity_id, stage_id` over
-- the raw observation grain, which did not compute what the comment claims,
-- in two separate ways:
--
--   1. NOT CONSECUTIVE. It was a total count of rows per stage, with no notion
--      of a run at all. A company that sat at Preliminary Diligence for three
--      meetings, moved to Deep Diligence, then regressed to Preliminary for
--      one more read as "4 meetings at Preliminary Diligence" -- one dwell
--      spell of 4 rather than two spells of 3 and 1. Every median computed
--      off it was a median of totals mislabelled as a median of dwells.
--   2. DOUBLE-COUNTED. Over the raw grain it counted rows, not meetings, so
--      the agenda-marker dual listing inflated it: summed over the current
--      data it totals 2,168 (the row count) against 2,092 actual
--      (entity, meeting) pairs -- 76 meeting-slots counted twice, once under
--      stage 1 and once under the funnel stage.
--
-- Both are fixed here, and the grain changes: one row per dwell SPELL
-- (entity, stage, island), not per (entity, stage). Callers that want the old
-- per-stage total should SUM(meetings_at_stage) GROUP BY entity_id, stage_id,
-- which is now the honest way to ask for it.
--
-- Islands are indexed over MEETINGS HELD, not calendar weeks. This is the
-- load-bearing choice. The cadence is weekly but ragged -- observed gaps of 6,
-- 7, 8, 14 and 21 days, and 4 weeks recorded as not extracted -- so a
-- calendar-week index would read every one of those gaps as an absence and
-- sever the run across it. Indexing over the ordinal position of held
-- meetings makes two consecutive held meetings adjacent no matter how far
-- apart they fall, so the 21-day gap costs nothing.
--
-- What DOES break a run, deliberately: the entity not appearing at a held
-- meeting between two appearances at the same stage. That is a real gap in the
-- evidence -- the company was off the slide that week -- and merging across it
-- would assert continuous presence the slides do not show. Such a company
-- yields two spells rather than one; `island_seq` numbers them in time order
-- so the pair is visible rather than silently summed.
--
-- MAX(stage_id) per (entity, meeting) is the same collapse rule used by
-- v_entity_funnel, v_stage_transition and v_entity_latest_stage, so the 76
-- dual-listed slots land on the funnel stage and not on the agenda marker.
--
-- DROP first: CREATE OR REPLACE refuses a definition that changes an existing
-- view's column list, and this adds three columns. Nothing reads v_dwell in
-- SQL (metrics.dwell_medians is its only consumer), so CASCADE takes nothing
-- with it.
DROP VIEW IF EXISTS v_dwell CASCADE;
CREATE VIEW v_dwell AS
WITH held AS (
    -- Ordinal position of each held meeting. The 4 not_extracted weeks are
    -- excluded, which is precisely what stops them severing a run.
    SELECT meeting_date,
           ROW_NUMBER() OVER (ORDER BY meeting_date) AS meeting_seq
    FROM meeting
    WHERE status = 'held'
),
per_meeting AS (
    SELECT o.entity_id,
           h.meeting_seq,
           h.meeting_date,
           MAX(o.stage_id) AS stage_id
    FROM v_observation o
    JOIN held h ON h.meeting_date = o.meeting_date
    GROUP BY o.entity_id, h.meeting_seq, h.meeting_date
),
islanded AS (
    -- Standard gaps-and-islands: consecutive meeting_seq values at the same
    -- stage share a constant (meeting_seq - row_number), which becomes the
    -- island key. A stage change or a skipped meeting shifts it.
    SELECT entity_id,
           stage_id,
           meeting_seq,
           meeting_date,
           meeting_seq - ROW_NUMBER() OVER (PARTITION BY entity_id, stage_id
                                            ORDER BY meeting_seq) AS island_key
    FROM per_meeting
)
SELECT entity_id,
       stage_id,
       ROW_NUMBER() OVER (PARTITION BY entity_id, stage_id
                          ORDER BY MIN(meeting_seq))  AS island_seq,
       COUNT(*)                                       AS meetings_at_stage,
       MIN(meeting_date)                              AS first_meeting_date,
       MAX(meeting_date)                              AS last_meeting_date
FROM islanded
GROUP BY entity_id, stage_id, island_key;
