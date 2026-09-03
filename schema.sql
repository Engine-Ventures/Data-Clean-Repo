-- EV Deal Pipeline — data layer
--
-- Implements the architecture in the handoff brief §9:
--   * immutable entity IDs + alias table (never auto-match on name alone)
--   * append-only observations; transitions derived from diffs
--   * store inputs, derive metrics (no persisted computed columns)
--   * provenance on every enriched field
--   * review queue, not silent writes
--   * missing runs recorded explicitly
--
-- Grain notes:
--   slide_observation  = company x meeting  (the 2,169-row evidence log; append-only)
--   field_value        = entity x field x write  (append-only; current value = latest)
--
-- Three-state nullability is explicit throughout. A value is:
--   present            -> field_value row with value_text/value_num set
--   genuinely zero     -> value_num = 0 with is_zero = 1
--   unknown, unchecked -> no field_value row, gap_status.state = 'not_checked'
--   unknown, confirmed -> gap_status.state = 'confirmed_unavailable'
-- Blank, zero and N/A are never interchangeable.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Controlled vocabularies (§4). All enums are FK-enforced; no free text.
-- ---------------------------------------------------------------------------

-- Stage funnel, ordered. rank 7 = furthest.
CREATE TABLE stage (
    stage_id    INTEGER PRIMARY KEY,          -- == rank, so ORDER BY works directly
    name        TEXT    NOT NULL UNIQUE,
    rank        INTEGER NOT NULL UNIQUE CHECK (rank BETWEEN 1 AND 7)
);

CREATE TABLE thesis_area (
    code        TEXT PRIMARY KEY,             -- HH, AS, E&C, AIFS
    name        TEXT NOT NULL UNIQUE
);

-- Maps every raw slide-section string seen in extraction onto a stage + thesis
-- area. This is where the E&C-splits-to-C glyph bug and the several NewCo
-- spellings get normalised, rather than in ingest code.
CREATE TABLE slide_section_map (
    raw_section TEXT PRIMARY KEY,
    stage_id    INTEGER REFERENCES stage(stage_id),
    thesis_code TEXT    REFERENCES thesis_area(code),
    note        TEXT
);

CREATE TABLE working_group (
    name        TEXT PRIMARY KEY
);

CREATE TABLE round_stage (
    name        TEXT PRIMARY KEY,
    rank        INTEGER NOT NULL UNIQUE
);

CREATE TABLE affinity_status (
    name        TEXT PRIMARY KEY,
    rank        INTEGER                       -- NULL where not funnel-ordered
);

CREATE TABLE enrichment_priority (
    name        TEXT PRIMARY KEY,
    tier        INTEGER NOT NULL CHECK (tier BETWEEN 1 AND 4)
);

-- Sources, in precedence order (§2): slides define population and stage
-- history, Affinity enriches only, public fills gaps, manual is a human write.
CREATE TABLE source (
    name        TEXT PRIMARY KEY,
    precedence  INTEGER NOT NULL UNIQUE
);

-- ---------------------------------------------------------------------------
-- Entities and aliases
-- ---------------------------------------------------------------------------

-- The canonical company. entity_id is permanent and internal; it is NOT the
-- spreadsheet's EV#### id, which the brief notes is stable within one file only.
CREATE TABLE entity (
    entity_id       INTEGER PRIMARY KEY,
    canonical_name  TEXT    NOT NULL,
    domain          TEXT,                     -- match on this first; most stable
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    -- Set when this entity has been merged into another. Rows are never
    -- deleted; a merge redirects. NULL means live.
    merged_into     INTEGER REFERENCES entity(entity_id),
    -- Phantom rows (PDF line-wrap artifacts) are marked, not deleted, so the
    -- extraction record stays auditable.
    is_phantom      INTEGER NOT NULL DEFAULT 0 CHECK (is_phantom IN (0,1)),
    phantom_reason  TEXT,
    CHECK (merged_into IS NULL OR merged_into <> entity_id)
);

CREATE INDEX idx_entity_live   ON entity(merged_into) WHERE merged_into IS NULL;
-- Deliberately NOT unique. The raw extraction contains 15 domains shared by
-- two or three rows -- that is the name-fragmentation of §5 showing up on the
-- stable key. Duplicates are allowed in and surfaced as merge_proposal review
-- items rather than rejected at load, so the evidence is never silently lost.
CREATE INDEX idx_entity_domain ON entity(domain) WHERE domain IS NOT NULL;

-- Every observed spelling -> entity. Includes slide names, Affinity names and
-- line-wrap variants. This is what makes the name fragmentation in §5 fixable
-- without touching the evidence log.
CREATE TABLE alias (
    alias_id    INTEGER PRIMARY KEY,
    entity_id   INTEGER NOT NULL REFERENCES entity(entity_id),
    alias_text  TEXT    NOT NULL,
    alias_norm  TEXT    NOT NULL,             -- casefolded/punct-stripped
    source      TEXT    NOT NULL REFERENCES source(name),
    -- How the alias was established. 'name' alone is never auto-applied (§9).
    match_method TEXT   NOT NULL CHECK (match_method IN
                    ('exact','domain','affinity_id','manual','line_wrap','fuzzy')),
    confidence  REAL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (alias_text)
);

CREATE INDEX idx_alias_entity ON alias(entity_id);
CREATE INDEX idx_alias_norm   ON alias(alias_norm);

-- Grouped slide entries such as "Cetos / Kira / Eden Tech" (§6 Q2). Modelled
-- as a real relation so the split decision is reversible either way: the
-- group row stays an entity, and its components are linked. Deleting the row
-- (what v2_DEDUPED did) would lose the evidence.
CREATE TABLE entity_group_member (
    group_entity_id     INTEGER NOT NULL REFERENCES entity(entity_id),
    component_entity_id INTEGER NOT NULL REFERENCES entity(entity_id),
    PRIMARY KEY (group_entity_id, component_entity_id),
    CHECK (group_entity_id <> component_entity_id)
);

-- ---------------------------------------------------------------------------
-- Meetings, including the ones that did not happen
-- ---------------------------------------------------------------------------

-- A skipped meeting must not read as company attrition (§9), so absent weeks
-- are rows with status <> 'held'.
CREATE TABLE meeting (
    meeting_date TEXT PRIMARY KEY CHECK (meeting_date LIKE '____-__-__'),
    status       TEXT NOT NULL CHECK (status IN
                     ('held','not_held','not_extracted','unknown')),
    slide_page   INTEGER,
    note         TEXT
);

-- ---------------------------------------------------------------------------
-- Slide observations — the append-only evidence log
-- ---------------------------------------------------------------------------

-- One row per company per meeting, straight from extraction. NEVER edited
-- (§3): corrections go to slide_observation_override, which shadows this.
CREATE TABLE slide_observation (
    observation_id INTEGER PRIMARY KEY,
    meeting_date   TEXT    NOT NULL REFERENCES meeting(meeting_date),
    entity_id      INTEGER NOT NULL REFERENCES entity(entity_id),
    name_on_slide  TEXT    NOT NULL,          -- verbatim, pre-alias-resolution
    stage_id       INTEGER NOT NULL REFERENCES stage(stage_id),
    raw_section    TEXT    REFERENCES slide_section_map(raw_section),
    is_bold        INTEGER NOT NULL CHECK (is_bold IN (0,1)),
    -- §6 Q1 is unresolved: bold may mean "discussed" or "on the agenda", and
    -- the deck legend distinguishes black (Update) from dark red (Discussion)
    -- from blue (Fellow/Internal). The brief says design for both. Colour is
    -- NOT in the current workbook, so this is nullable pending re-extraction
    -- from the PDF; agenda_type is then derivable from it.
    bold_color     TEXT CHECK (bold_color IN ('black','dark_red','blue')),
    slide_page     INTEGER,
    extracted_at   TEXT
);

CREATE INDEX idx_obs_entity  ON slide_observation(entity_id, meeting_date);
CREATE INDEX idx_obs_meeting ON slide_observation(meeting_date);
-- Natural key includes stage and section because a company is routinely
-- listed twice at one meeting: once under "Meetings this week" (stage 1) and
-- once under its thesis prelim-diligence sub-section (stage 4). 71 such
-- dual-listings exist. "Meetings This Week" is an agenda marker that coexists
-- with a funnel position, not a mutually exclusive stage -- so collapsing on
-- (date, entity, name) would silently drop real evidence.
CREATE UNIQUE INDEX idx_obs_grain
    ON slide_observation(meeting_date, entity_id, name_on_slide, stage_id, raw_section);

-- Corrections that shadow the evidence log without mutating it.
CREATE TABLE slide_observation_override (
    override_id    INTEGER PRIMARY KEY,
    observation_id INTEGER NOT NULL REFERENCES slide_observation(observation_id),
    field          TEXT    NOT NULL CHECK (field IN
                        ('stage_id','is_bold','bold_color','entity_id','raw_section')),
    new_value      TEXT,
    reason         TEXT    NOT NULL,
    created_by     TEXT    NOT NULL,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (observation_id, field)
);

-- ---------------------------------------------------------------------------
-- Field-level provenance (§8: every write stores value, source, user, time)
-- ---------------------------------------------------------------------------

CREATE TABLE field_value (
    field_value_id INTEGER PRIMARY KEY,
    entity_id      INTEGER NOT NULL REFERENCES entity(entity_id),
    field          TEXT    NOT NULL,
    value_text     TEXT,
    value_num      REAL,
    -- Distinguishes a real 0 from unknown. Required because Affinity's 0.0
    -- round sizes were read as unknown, not zero (§5).
    is_zero        INTEGER NOT NULL DEFAULT 0 CHECK (is_zero IN (0,1)),
    source         TEXT    NOT NULL REFERENCES source(name),
    citation       TEXT,                      -- required for source='Public'
    created_by     TEXT    NOT NULL,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    superseded_at  TEXT,                      -- NULL = current
    CHECK (is_zero = 0 OR value_num = 0),
    CHECK (source <> 'Public' OR citation IS NOT NULL)
);

CREATE INDEX idx_fv_current ON field_value(entity_id, field)
    WHERE superseded_at IS NULL;

-- Currency is always stored as _usd plus the local amount alongside (§8).
CREATE TABLE money_value (
    field_value_id INTEGER PRIMARY KEY REFERENCES field_value(field_value_id),
    amount_usd     REAL NOT NULL,
    amount_local   REAL,
    currency       TEXT,
    fx_date        TEXT,
    CHECK (amount_local IS NULL OR currency IS NOT NULL)
);

-- Three-state gap tracking (§8 Worklist): "not yet looked at" is distinct from
-- "checked, genuinely unavailable".
CREATE TABLE gap_status (
    entity_id   INTEGER NOT NULL REFERENCES entity(entity_id),
    field       TEXT    NOT NULL,
    state       TEXT    NOT NULL CHECK (state IN
                    ('not_checked','confirmed_unavailable','filled')),
    checked_by  TEXT,
    checked_at  TEXT,
    note        TEXT,
    PRIMARY KEY (entity_id, field)
);

-- ---------------------------------------------------------------------------
-- Fields that exist in no source and must be captured going forward (§7)
-- ---------------------------------------------------------------------------

CREATE TABLE pass_reason_category (
    name TEXT PRIMARY KEY,
    sort INTEGER
);

CREATE TABLE outcome_type (
    name TEXT PRIMARY KEY,
    is_terminal INTEGER NOT NULL DEFAULT 0 CHECK (is_terminal IN (0,1))
);

CREATE TABLE source_channel (
    name TEXT PRIMARY KEY
);

-- Disappearing from a slide is not a decision (§7): an outcome must be stated.
CREATE TABLE entity_outcome (
    entity_id       INTEGER PRIMARY KEY REFERENCES entity(entity_id),
    outcome         TEXT    NOT NULL REFERENCES outcome_type(name),
    outcome_date    TEXT,
    pass_category   TEXT    REFERENCES pass_reason_category(name),
    pass_notes      TEXT,
    recorded_by     TEXT    NOT NULL,
    recorded_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE entity_sourcing (
    entity_id      INTEGER PRIMARY KEY REFERENCES entity(entity_id),
    sourcer        TEXT,                      -- distinct from owner
    channel        TEXT REFERENCES source_channel(name),
    referrer_name  TEXT,
    recorded_by    TEXT,
    recorded_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE founder (
    founder_id  INTEGER PRIMARY KEY,
    entity_id   INTEGER NOT NULL REFERENCES entity(entity_id),
    full_name   TEXT    NOT NULL,
    role        TEXT
);

-- Rounds the company raised, including ones EV did not join — needed for
-- graduation rates and the anti-portfolio (§7).
CREATE TABLE funding_round (
    round_id        INTEGER PRIMARY KEY,
    entity_id       INTEGER NOT NULL REFERENCES entity(entity_id),
    round_stage     TEXT    REFERENCES round_stage(name),
    announced_date  TEXT,
    amount_usd      REAL,
    pre_money_usd   REAL,
    post_money_usd  REAL,
    ev_participated INTEGER CHECK (ev_participated IN (0,1)),
    citation        TEXT
);

CREATE TABLE meeting_attendee (
    meeting_date TEXT NOT NULL REFERENCES meeting(meeting_date),
    person       TEXT NOT NULL,
    PRIMARY KEY (meeting_date, person)
);

-- ---------------------------------------------------------------------------
-- Review queue — fuzzy matches, first appearances, line-wrap candidates and
-- stage jumps >2 levels go to a human instead of being written silently (§9).
-- ---------------------------------------------------------------------------

CREATE TABLE review_item (
    review_id   INTEGER PRIMARY KEY,
    kind        TEXT    NOT NULL CHECK (kind IN
                    ('fuzzy_match','first_appearance','line_wrap_candidate',
                     'stage_jump','merge_proposal','group_split','stage_regression',
                     'source_conflict','duplicate_listing')),
    entity_id   INTEGER REFERENCES entity(entity_id),
    target_id   INTEGER REFERENCES entity(entity_id),  -- merge/split counterpart
    detail      TEXT    NOT NULL,
    confidence  REAL,
    proposed_by TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    state       TEXT    NOT NULL DEFAULT 'open'
                    CHECK (state IN ('open','accepted','rejected','deferred')),
    resolved_by TEXT,
    resolved_at TEXT,
    resolution_note TEXT
);

CREATE INDEX idx_review_open ON review_item(kind, state) WHERE state = 'open';

-- ---------------------------------------------------------------------------
-- Ingest run log — every load is recorded with a coverage report (§9 governance)
-- ---------------------------------------------------------------------------

CREATE TABLE ingest_run (
    run_id       INTEGER PRIMARY KEY,
    started_at   TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at  TEXT,
    source_file  TEXT NOT NULL,
    source_sha256 TEXT,
    schema_version TEXT NOT NULL,
    row_counts   TEXT,                        -- JSON
    note         TEXT
);

-- ---------------------------------------------------------------------------
-- Derived views. Metrics are computed, never stored (§9).
-- ---------------------------------------------------------------------------

-- Effective observations: evidence log with overrides applied.
CREATE VIEW v_observation AS
SELECT
    o.observation_id,
    o.meeting_date,
    -- override, then merge redirect, then the raw extraction value
    COALESCE(
        me.merged_into,
        (SELECT CAST(new_value AS INTEGER) FROM slide_observation_override v
         WHERE v.observation_id = o.observation_id AND v.field = 'entity_id'),
        o.entity_id)                                          AS entity_id,
    o.entity_id                                               AS raw_entity_id,
    o.name_on_slide,
    COALESCE((SELECT CAST(new_value AS INTEGER) FROM slide_observation_override v
              WHERE v.observation_id = o.observation_id AND v.field = 'stage_id'),
             o.stage_id)                                      AS stage_id,
    COALESCE((SELECT CAST(new_value AS INTEGER) FROM slide_observation_override v
              WHERE v.observation_id = o.observation_id AND v.field = 'is_bold'),
             o.is_bold)                                       AS is_bold,
    COALESCE((SELECT new_value FROM slide_observation_override v
              WHERE v.observation_id = o.observation_id AND v.field = 'bold_color'),
             o.bold_color)                                    AS bold_color,
    o.raw_section,
    o.slide_page
FROM slide_observation o
-- Resolve the observation onto the LIVE entity, following any merge.
--
-- Without this join, entity.merged_into is decoration: setting it changes no
-- count, the merged-away company keeps its own funnel row, and the population
-- never falls. Everything downstream -- v_entity_funnel, v_entity_discussion,
-- v_dwell, v_stage_transition -- reads through this view, so joining here is
-- what makes a merge mean something, and it does so without ever editing
-- slide_observation, which the append-only rule forbids.
--
-- One hop is sufficient by construction: actions.merge_entities refuses to
-- merge into an entity that is itself merged, so chains cannot form.
LEFT JOIN entity me ON me.entity_id = COALESCE(
    (SELECT CAST(new_value AS INTEGER) FROM slide_observation_override v
     WHERE v.observation_id = o.observation_id AND v.field = 'entity_id'),
    o.entity_id);

-- Resolve an entity through any merge chain (one hop is enough by construction:
-- merges always target a live entity).
CREATE VIEW v_entity_resolved AS
SELECT e.entity_id,
       COALESCE(e.merged_into, e.entity_id) AS live_entity_id
FROM entity e;

-- Current value of every field, provenance attached.
CREATE VIEW v_field_current AS
SELECT fv.entity_id, fv.field, fv.value_text, fv.value_num, fv.is_zero,
       fv.source, fv.citation, fv.created_by, fv.created_at
FROM field_value fv
WHERE fv.superseded_at IS NULL;

-- Stage transitions derived from consecutive observations (§9: derive from
-- diffs, do not persist).
CREATE VIEW v_stage_transition AS
SELECT entity_id,
       prev_date  AS from_date,
       meeting_date AS to_date,
       prev_stage AS from_stage_id,
       stage_id   AS to_stage_id,
       stage_id - prev_stage AS delta
FROM (
    SELECT o.entity_id, o.meeting_date, o.stage_id,
           LAG(o.stage_id)     OVER w AS prev_stage,
           LAG(o.meeting_date) OVER w AS prev_date
    FROM v_observation o
    WINDOW w AS (PARTITION BY o.entity_id ORDER BY o.meeting_date)
)
WHERE prev_stage IS NOT NULL AND prev_stage <> stage_id;

-- Per-entity funnel summary: furthest stage, first date at each level,
-- appearance counts, bold counts. Replaces the reached_*/date_* columns.
CREATE VIEW v_entity_funnel AS
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
    --   This is what the staging workbook's reached_* columns meant, and what
    --   the brief's headline counts use (15 deep diligence, 6 legal).
    -- reached_*     : the company got at least that far in the funnel.
    --
    -- They differ. Axiomatic AI moved Preliminary Diligence -> Legal in a
    -- single week (2025-12-15, a genuine +3 jump, not a missing-week artifact)
    -- and was never observed in the Deep Diligence column. So
    -- observed_at_deep_diligence = 15 while reached_deep_diligence = 16, and
    -- the brief's own furthest-stage rows (Legal 6 + Deep 10) sum to 16.
    -- Reporting must state which is meant; a funnel conversion rate wants
    -- reached_*, a "who did we look at closely" count wants observed_at_*.
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

-- Latest observed stage per entity.
CREATE VIEW v_entity_latest_stage AS
SELECT entity_id, meeting_date AS last_seen, stage_id AS latest_stage_id
FROM (
    SELECT entity_id, meeting_date, stage_id,
           ROW_NUMBER() OVER (PARTITION BY entity_id ORDER BY meeting_date DESC) rn
    FROM v_observation
) WHERE rn = 1;

-- Discussion summary. Kept separate from "on agenda" so that when §6 Q1 is
-- answered, agenda_type can be layered on without reshaping anything.
CREATE VIEW v_entity_discussion AS
SELECT entity_id,
       MAX(is_bold)                                          AS discussed,
       SUM(is_bold)                                          AS times_discussed,
       MIN(CASE WHEN is_bold = 1 THEN meeting_date END)       AS first_discussion_date,
       MAX(CASE WHEN is_bold = 1 THEN meeting_date END)       AS last_discussion_date
FROM v_observation
GROUP BY entity_id;

-- Dwell time: consecutive meetings an entity spent at each stage.
CREATE VIEW v_dwell AS
SELECT entity_id, stage_id, COUNT(*) AS meetings_at_stage
FROM v_observation
GROUP BY entity_id, stage_id;
