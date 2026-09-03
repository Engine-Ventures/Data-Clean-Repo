-- EV Deal Pipeline — controlled vocabularies (§4).
--
-- Split out of schema.sql so the DDL can be re-run against a shared Neon
-- branch without contending over rows. Idempotent: every insert is
-- ON CONFLICT DO NOTHING, so re-running seeds what is missing and leaves
-- existing rows — including any a human has since corrected — untouched.
--
-- Run order: schema.sql, then this file.
--
-- This file is the single source of truth for the picklists. evpipeline/vocab.py
-- holds the same values as Python constants for the ingest code that needs to
-- map a name to an id in memory; the two are asserted equal by the tests
-- rather than kept in sync by hand.
--
-- ===========================================================================
-- The stage_id assignment below is a CORRECTION, not a port.
--
-- The SQLite schema committed NewCo / Fellows = 2 and Hold / Nurture = 3.
-- vocab.py's STAGES list had them the other way round, and because
-- ingest.load_observations sets every observation's stage_id from
-- vocab.STAGE_BY_NAME[stage_on_slide], the two sub-Prelim stages were stored
-- swapped for the entire dataset: 476 observations whose slide section was
-- "Hold / Nurture" carry stage_id 2 ("NewCo / Fellows"), and 233 whose
-- section was a NewCo section carry stage_id 3 ("Hold / Nurture").
--
-- schema.sql's stage table is what names the ids, so it wins: NewCo = 2,
-- Hold = 3, and vocab.py is corrected to match. Nothing about the ranks
-- themselves changes.
-- ===========================================================================

-- Stage funnel, ordered. stage_id == rank.
INSERT INTO stage (stage_id, name, rank) VALUES
    (1, 'Meetings This Week',         1),
    (2, 'NewCo / Fellows',            2),
    (3, 'Hold / Nurture',             3),
    (4, 'Preliminary Diligence',      4),
    (5, 'Deep Diligence',             5),
    (6, 'Negotiate / Offer',          6),
    (7, 'Legal Diligence / Def Docs', 7)
ON CONFLICT DO NOTHING;

INSERT INTO thesis_area (code, name) VALUES
    ('HH',   'Human Health'),
    ('AS',   'Autonomous Systems'),
    ('E&C',  'Energy & Climate'),
    ('AIFS', 'AI for Science')
ON CONFLICT DO NOTHING;

-- Every raw slide-section string observed in extraction, mapped to a stage and
-- (where the section is a thesis sub-section) a thesis area. The 10 rows the
-- SQLite schema seeded are exactly the 10 distinct values in the current
-- workbook's Stage History!slide_section; the last three are extraction
-- variants carried over from vocab.py, seeded so a re-extraction that emits
-- them resolves without a code change.
--
-- Two extraction quirks are normalised here rather than in code:
--   * "C" appears where "E&C" was split by the ampersand glyph (§4).
--   * NewCo / Fellows is spelled several different ways across the deck.
--
-- The three NewCo spellings all map to stage 2. Under the old vocab.py
-- mapping, 'FF / TF / EF NewCo:' and 'NewCo:' were seeded as stage 3
-- ("Hold / Nurture") — wrong, and inconsistent with
-- 'Frontier Fellows / NewCo:' which the DDL had already seeded as 2.
INSERT INTO slide_section_map (raw_section, stage_id, thesis_code, note) VALUES
    ('Meetings this week',            1, NULL,   'Stage-only section, no thesis sub-header.'),
    ('Frontier Fellows / NewCo:',     2, NULL,   'Stage-only section.'),
    ('FF / TF / EF NewCo:',           2, NULL,   'Stage-only section; spelling variant.'),
    ('NewCo:',                        2, NULL,   'Stage-only section; spelling variant.'),
    ('Hold / Nurture',                3, NULL,   'Stage-only section.'),
    ('HH',                            4, 'HH',   'Preliminary Diligence, Human Health sub-header.'),
    ('AS',                            4, 'AS',   'Preliminary Diligence, Autonomous Systems sub-header.'),
    ('E&C',                           4, 'E&C',  'Preliminary Diligence, Energy & Climate sub-header.'),
    ('C',                             4, 'E&C',  'Ampersand glyph split; same as E&C.'),
    ('AIFS',                          4, 'AIFS', 'Preliminary Diligence, AI for Science sub-header.'),
    ('Deep Diligence',                5, NULL,   'Stage-only section — unlike Prelim Diligence, Deep Diligence has no thesis sub-header in this extraction.'),
    ('Negotiate / Offer',             6, NULL,   'Stage-only section.'),
    ('Legal diligence / Def Docs',    7, NULL,   'Stage-only section.')
ON CONFLICT DO NOTHING;

-- The 4 distinct atoms found in Companies!working_group_final (values there
-- are semicolon-joined when a company spans more than one, e.g. "Climate;
-- Systems"). Note: the workbook's own Data Dictionary text says "Health /
-- Systems / Climate / Scale-Up", but "Scale-Up" does not occur anywhere in
-- the actual working_group_final column — the fourth real value is
-- "AI for Science". Trust the data over the dictionary prose. Scale-Up is
-- seeded anyway because vocab.py listed it and the existing database has it;
-- it is an unused-but-valid picklist entry, not a data claim.
INSERT INTO working_group (name) VALUES
    ('Health'), ('Systems'), ('Climate'), ('AI for Science'), ('Scale-Up')
ON CONFLICT DO NOTHING;

-- The 4 distinct values found in Companies!stage, plus Series C from vocab.py.
INSERT INTO round_stage (name, rank) VALUES
    ('Preseed',   1),
    ('Seed',      2),
    ('Series A',  3),
    ('Series B',  4),
    ('Series C',  5)
ON CONFLICT DO NOTHING;

-- The 9 distinct values found in Companies!affinity_status, plus the two
-- vocab.py adds. Funnel-ordered statuses get a rank; terminal/non-funnel
-- outcomes (Pass, Loss, Wait, Potential Pathways, Protocompany) don't, since
-- they aren't comparable positions on a single track.
--
-- Invested carries rank 6 here. vocab.py listed it as NULL, but the DDL
-- seeded 6 first and so 6 is what the existing database holds; vocab.py is
-- corrected to match rather than the other way round.
--
-- Note there is no Legal status — a known gap (§4), which is why 6 companies
-- reached Legal on the slides and 0 in Affinity.
INSERT INTO affinity_status (name, rank) VALUES
    ('Sourcing - No Outreach',  1),
    ('Pre-Screen',              2),
    ('Initial Evaluation',      3),
    ('Preliminary Diligence',   4),
    ('Deep Diligence',          5),
    ('Invested',                6),
    ('Wait',                    NULL),
    ('Pass',                    NULL),
    ('Loss',                    NULL),
    ('Potential Pathways',      NULL),
    ('Protocompany',            NULL)
ON CONFLICT DO NOTHING;

-- The workbook's Data Dictionary defines 4 tiers (P1 advanced/discussed with
-- gaps; P2 prelim dil mostly empty; P3 sparse; P4 acceptable).
--
-- Both P3 spellings are seeded. The DDL had 'P3 - sparse' and vocab.py had
-- 'P3 - sparse record'; the existing database contains both, and
-- metrics.enrichment_priority() emits 'P3 - sparse record', so that is the
-- one anything FK-referencing this table will actually use. 'P3 - sparse' is
-- kept so an existing reference to it does not dangle. Collapsing the two is
-- a data decision, not a migration one — left as a known duplicate.
INSERT INTO enrichment_priority (name, tier) VALUES
    ('P1 - advanced stage, incomplete', 1),
    ('P1 - discussed, incomplete',      1),
    ('P2 - prelim dil, mostly empty',   2),
    -- 'P3 - sparse' was here too, at the same tier, and was unreachable:
    -- metrics.enrichment_priority can emit exactly five strings and that was
    -- not one of them. Removed rather than kept as a harmless spare, because
    -- a picklist member no producer can emit makes the picklist stop being a
    -- statement about the possible values. Caught by tests/test_vocab.py.
    ('P3 - sparse record',              3),
    ('P4 - acceptable',                 4)
ON CONFLICT DO NOTHING;

-- Source precedence (§2): slides define the population and all stage history,
-- Affinity enriches only and may never add a company, public fills gaps on
-- advanced-stage rows, Manual is a human write in the interface.
INSERT INTO source (name, precedence) VALUES
    ('Slides',   1),
    ('Affinity', 2),
    ('Public',   3),
    ('Manual',   4)
ON CONFLICT DO NOTHING;

-- §7 — fields that exist in no current source. Seeded so capture can start
-- immediately; the brief notes every week of delay is permanent loss.
INSERT INTO pass_reason_category (name, sort) VALUES
    ('Team',                            1),
    ('Technology / technical risk',     2),
    ('Market size',                     3),
    ('Timing - too early',              4),
    ('Timing - too late',               5),
    ('Valuation / terms',               6),
    ('Round dynamics / no allocation',  7),
    ('Thesis fit',                      8),
    ('Competitive position',            9),
    ('Regulatory / reimbursement',     10),
    ('Capital intensity',              11),
    ('Founder unresponsive',           12),
    ('Company died / wound down',      13),
    ('Other',                          99)
ON CONFLICT DO NOTHING;

INSERT INTO outcome_type (name, is_terminal) VALUES
    ('Active',              0),
    ('Invested',            1),
    ('Passed',              1),
    ('Lost - competitive',  1),
    ('Lost - company died', 1),
    ('Dormant - no contact', 0),
    ('Tracking',            0)
ON CONFLICT DO NOTHING;

INSERT INTO source_channel (name) VALUES
    ('Warm intro'),
    ('Inbound'),
    ('Conference'),
    ('Portfolio referral'),
    ('Lab spinout'),
    ('Fellows programme'),
    ('Outbound'),
    ('Unknown')
ON CONFLICT DO NOTHING;
