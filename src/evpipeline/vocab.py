"""Controlled vocabularies from the handoff brief §4.

These are the locked picklists. Nothing in ingest or the UI may invent a value
outside them; the schema enforces it with foreign keys.
"""

from __future__ import annotations

# Stage funnel, ordered. stage_id == rank so that ORDER BY / MAX() work on it
# directly and "furthest stage" is just MAX(stage_id).
#
# seed.sql is authoritative for these ids and this module matches it. That is
# not a style preference: ranks 2 and 3 used to be assigned the other way round
# here, while the DDL's inline seed assigned them as below, and the two
# disagreed for the whole life of the SQLite database. `INSERT OR IGNORE` made
# the `stage` table take the DDL's ids, but ingest writes
# slide_observation.stage_id from STAGE_BY_NAME *directly* -- it consults
# neither the `stage` table nor slide_section_map -- so 709 observation rows
# resolved to the wrong stage name: 476 Hold / Nurture rows read as
# "NewCo / Fellows" and 233 NewCo rows read as "Hold / Nurture", changing the
# furthest-stage label of 57 companies. No metric moved (every threshold in
# v_entity_funnel is >= 4, and both ranks are below Preliminary Diligence),
# which is exactly why nobody noticed: the arithmetic was right and only the
# labels lied. Full account in MIGRATION.md.
#
# The old comment here said the 2/3 tie-break was arbitrary because "nothing
# downstream depends on it". That was true of the arithmetic and false of the
# labels. tests/test_vocab.py now asserts this list and the seeded `stage`
# table agree in both directions, so the next disagreement is a red test
# rather than a mislabelled UI.
STAGES: list[tuple[int, str]] = [
    (7, "Legal Diligence / Def Docs"),
    (6, "Negotiate / Offer"),
    (5, "Deep Diligence"),
    (4, "Preliminary Diligence"),
    (3, "Hold / Nurture"),
    (2, "NewCo / Fellows"),
    (1, "Meetings This Week"),
]

STAGE_BY_NAME: dict[str, int] = {name: rank for rank, name in STAGES}

THESIS_AREAS: list[tuple[str, str]] = [
    ("HH", "Human Health"),
    ("AS", "Autonomous Systems"),
    ("E&C", "Energy & Climate"),
    ("AIFS", "AI for Science"),
]

# Every raw slide-section string observed in extraction, mapped to a stage and
# (where the section is a thesis sub-section) a thesis area.
#
# Two extraction quirks are normalised here rather than in code:
#   * "C" appears where "E&C" was split by the ampersand glyph (§4).
#   * NewCo / Fellows is spelled three different ways across the deck.
SLIDE_SECTION_MAP: list[tuple[str, str, str | None, str | None]] = [
    # raw_section, stage name, thesis code, note
    ("Meetings this week", "Meetings This Week", None, None),
    ("Hold / Nurture", "Hold / Nurture", None, None),
    ("Deep Diligence", "Deep Diligence", None, None),
    ("Negotiate / Offer", "Negotiate / Offer", None, None),
    ("Legal diligence / Def Docs", "Legal Diligence / Def Docs", None, None),
    ("HH", "Preliminary Diligence", "HH", "thesis sub-section of prelim dil"),
    ("AS", "Preliminary Diligence", "AS", "thesis sub-section of prelim dil"),
    ("E&C", "Preliminary Diligence", "E&C", "thesis sub-section of prelim dil"),
    ("C", "Preliminary Diligence", "E&C", "ampersand glyph split; E&C"),
    ("AIFS", "Preliminary Diligence", "AIFS", "thesis sub-section of prelim dil"),
    ("Frontier Fellows / NewCo:", "NewCo / Fellows", None, "spelling variant"),
    ("FF / TF / EF NewCo:", "NewCo / Fellows", None, "spelling variant"),
    ("NewCo:", "NewCo / Fellows", None, "spelling variant"),
]

WORKING_GROUPS = ["Health", "Systems", "Climate", "Scale-Up", "AI for Science"]

ROUND_STAGES = [
    ("Preseed", 1),
    ("Seed", 2),
    ("Series A", 3),
    ("Series B", 4),
    ("Series C", 5),
]

# Affinity statuses. Note there is no Legal status — a known gap (§4), which is
# why 6 companies reached Legal on the slides and 0 in Affinity.
AFFINITY_STATUSES: list[tuple[str, int | None]] = [
    ("Sourcing - No Outreach", 1),
    ("Pre-Screen", 2),
    ("Initial Evaluation", 3),
    ("Preliminary Diligence", 4),
    ("Deep Diligence", 5),
    ("Wait", None),
    ("Potential Pathways", None),
    ("Protocompany", None),
    ("Pass", None),
    ("Loss", None),
    ("Invested", None),
]

ENRICHMENT_PRIORITIES: list[tuple[str, int]] = [
    ("P1 - advanced stage, incomplete", 1),
    ("P1 - discussed, incomplete", 1),
    ("P2 - prelim dil, mostly empty", 2),
    ("P3 - sparse record", 3),
    ("P4 - acceptable", 4),
]

# Source precedence (§2): slides define the population and all stage history,
# Affinity enriches only and may never add a company, public fills gaps on
# advanced-stage rows, Manual is a human write in the interface.
SOURCES: list[tuple[str, int]] = [
    ("Slides", 1),
    ("Affinity", 2),
    ("Public", 3),
    ("Manual", 4),
]

# §7 — fields that exist in no current source. Seeded so capture can start
# immediately; the brief notes every week of delay is permanent loss.
PASS_REASON_CATEGORIES: list[tuple[str, int]] = [
    ("Team", 1),
    ("Technology / technical risk", 2),
    ("Market size", 3),
    ("Timing - too early", 4),
    ("Timing - too late", 5),
    ("Valuation / terms", 6),
    ("Round dynamics / no allocation", 7),
    ("Thesis fit", 8),
    ("Competitive position", 9),
    ("Regulatory / reimbursement", 10),
    ("Capital intensity", 11),
    ("Founder unresponsive", 12),
    ("Company died / wound down", 13),
    ("Other", 99),
]

OUTCOME_TYPES: list[tuple[str, int]] = [
    ("Active", 0),
    ("Invested", 1),
    ("Passed", 1),
    ("Lost - competitive", 1),
    ("Lost - company died", 1),
    ("Dormant - no contact", 0),
    ("Tracking", 0),
]

SOURCE_CHANNELS = [
    "Warm intro",
    "Inbound",
    "Conference",
    "Portfolio referral",
    "Lab spinout",
    "Fellows programme",
    "Outbound",
    "Unknown",
]

# Region bucket used by the geography rollups. UNKNOWN is a gap, never a
# category to report as if it were a place (§8).
REGIONS = ["United States", "International", "UNKNOWN"]
