"""Regression tests for the validation anchors in the handoff brief §10.

Any rebuild of the extraction must reproduce these. Where a number here
differs from the brief, the docstring says why and the difference is asserted
deliberately rather than tolerated.
"""

from __future__ import annotations

from collections import Counter

from evpipeline.metrics import (
    coverage_report,
    discussion_counts,
    dwell_medians,
    enrichment_priority,
    funnel_counts,
    furthest_stage_distribution,
    reconciliation,
)

# The five Fund III portfolio companies named on deck page 1 (§10).
FUND_III = {
    "Bristol Superlight",
    "CorePower Magnetics",
    "DG Matrix",
    "Axiomatic AI",
    "Ayo Electronics",
}


# ---------------------------------------------------------------------------
# Population and grain
# ---------------------------------------------------------------------------

def test_meeting_span_and_count(conn):
    """43 meetings, 2025-10-14 -> 2026-08-31."""
    row = conn.execute(
        "SELECT COUNT(*) n, MIN(meeting_date) lo, MAX(meeting_date) hi "
        "FROM meeting WHERE status = 'held'"
    ).fetchone()
    assert (row["n"], row["lo"], row["hi"]) == (43, "2025-10-14", "2026-08-31")


def test_missing_weeks_recorded_explicitly(conn):
    """The 3 extraction gaps span 4 absent Mondays, all recorded as meetings.

    The brief says "3 meeting weeks absent" and lists three date ranges, but
    the 2025-12-15 -> 2026-01-05 gap is 21 days and therefore hides two
    skipped weeks, not one. A skipped week must be a row (§9) so that it
    cannot be read as company attrition.
    """
    missing = [
        r["meeting_date"]
        for r in conn.execute(
            "SELECT meeting_date FROM meeting WHERE status <> 'held' ORDER BY meeting_date"
        )
    ]
    assert missing == ["2025-12-22", "2025-12-29", "2026-04-27", "2026-06-29"]


def test_raw_company_row_count(report):
    """498 raw company rows, loaded verbatim with no dedup applied."""
    assert report["entities"] == 498


def test_stage_history_row_count(report, conn):
    """2,169 stage-history rows, of which exactly one is an exact duplicate.

    `Mach Electric` appears twice at 2026-07-27 with identical stage, section,
    bold flag and slide page (page 36) — a double-extraction, not a
    dual-listing. The unique grain collapses it, so the log holds 2,168 rows
    and 2,168 + 1 reconciles to the brief's 2,169.
    """
    assert report["observations"] == 2168
    assert report["resolved_exact"] + report["resolved_normalised"] == 2169
    assert report["unresolved_slide_names"] == 0


def test_dual_listings_are_preserved(conn):
    """A company listed twice at one meeting keeps both rows.

    "Meetings This Week" is an agenda marker, not a funnel position: a company
    routinely appears there and in its thesis prelim-diligence sub-section at
    the same meeting. 74 such dual-listings exist at entity grain and all must
    survive, since collapsing them would erase the stage evidence.

    69 use one spelling — the genuine agenda + stage pairing. The other 5 list
    the same company twice under two spellings (`NeoLogic`/`Neologic`,
    `NoLux`/`Nolux`, `Stac12`/`StaC12`, `WAVR`/`WAVR Technologies`) and are
    queued as duplicate_listing for a human to check against the slide.
    """
    rows = conn.execute(
        """SELECT COUNT(DISTINCT name_on_slide) nd FROM slide_observation
           GROUP BY meeting_date, entity_id HAVING COUNT(*) > 1"""
    ).fetchall()
    assert len(rows) == 74
    assert sum(1 for r in rows if r["nd"] > 1) == 5


# ---------------------------------------------------------------------------
# Funnel
# ---------------------------------------------------------------------------

def test_furthest_stage_distribution(conn):
    """Legal 6, Negotiate 0, Deep 10, Prelim 175, NewCo 21, Hold 36, MTW 250."""
    assert furthest_stage_distribution(conn) == {
        "Legal Diligence / Def Docs": 6,
        "Deep Diligence": 10,
        "Preliminary Diligence": 175,
        "NewCo / Fellows": 21,
        "Hold / Nurture": 36,
        "Meetings This Week": 250,
    }


def test_furthest_stage_sums_to_population(conn):
    """The distribution must sum to 498 — the §8 reconciliation rule."""
    assert sum(furthest_stage_distribution(conn).values()) == 498


def test_deep_diligence_and_legal_counts(conn):
    """15 observed at Deep Diligence, 6 at Legal.

    The brief's "15 reached_deep_diligence" is an *observed at* count, not a
    cumulative one: Axiomatic AI went Preliminary Diligence -> Legal in a
    single week (2025-12-15) and was never seen in the Deep Diligence column.
    So 16 entities reached that depth while 15 were observed there, which is
    why the brief's own Legal 6 + Deep 10 rows sum to 16.
    """
    f = funnel_counts(conn)
    assert f["obs_deep"] == 15
    assert f["obs_legal"] == 6
    assert f["reached_deep"] == 16
    assert f["reached_legal"] == 6


def test_negotiate_offer_is_never_a_furthest_stage(conn):
    """Negotiate / Offer is observed but is nobody's furthest stage."""
    f = funnel_counts(conn)
    assert f["obs_negotiate"] == 5
    assert "Negotiate / Offer" not in furthest_stage_distribution(conn)


def test_fund_iii_companies_all_reached_legal(conn):
    """All five Fund III companies on deck page 1 reached Legal."""
    legal = {
        r["canonical_name"]
        for r in conn.execute(
            """SELECT e.canonical_name FROM entity e
               JOIN v_entity_funnel f USING (entity_id)
               WHERE f.observed_at_legal = 1"""
        )
    }
    assert legal >= FUND_III


def test_legal_is_not_the_same_as_invested(conn):
    """Reaching Legal does not imply investment — the §10 check is too strong.

    §10 states "every company reaching Legal appears on the Fund III
    investment list" as the original correctness check. It holds for 5 of the
    6: Ovelle reached Legal Diligence on 2026-03-30 and is listed in §12 among
    the deep-diligence companies that died. That is a real outcome, not an
    extraction error, and it is why entity_outcome exists as its own table.
    """
    legal = {
        r["canonical_name"]
        for r in conn.execute(
            """SELECT e.canonical_name FROM entity e
               JOIN v_entity_funnel f USING (entity_id)
               WHERE f.observed_at_legal = 1"""
        )
    }
    assert legal - FUND_III == {"Ovelle"}


# ---------------------------------------------------------------------------
# Discussion
# ---------------------------------------------------------------------------

def test_discussion_counts(conn):
    """34 companies discussed, 152 bold appearances total."""
    d = discussion_counts(conn)
    assert d["entities_discussed"] == 34
    assert d["bold_appearances"] == 152


def test_bold_colour_is_unpopulated_pending_reextraction(conn):
    """Bold colour is not in the staging workbook and must not be invented.

    §6 Q1 turns on the black/dark-red split (136 vs 16), which lives only in
    the PDF. The column exists so the model is ready for it; every row is NULL
    until re-extraction fills it.
    """
    n = conn.execute(
        "SELECT COUNT(*) FROM slide_observation WHERE bold_color IS NOT NULL"
    ).fetchone()[0]
    assert n == 0


# ---------------------------------------------------------------------------
# Dwell time and coverage
# ---------------------------------------------------------------------------

def test_dwell_medians(conn):
    """Prelim Dil 3 meetings, Deep Dil 3, Legal 7, Hold / Nurture 9."""
    m = dwell_medians(conn)
    assert m["Preliminary Diligence"] == 3
    assert m["Deep Diligence"] == 3
    assert m["Legal Diligence / Def Docs"] == 7
    assert m["Hold / Nurture"] == 9


def test_stage_coverage(conn):
    """Stage coverage 40% overall, 85% among discussed."""
    r = coverage_report(conn)
    assert round(r["stage"].pct, 3) == 0.400
    assert round(r["stage @ discussed cohort"].pct, 2) == 0.85


def test_geography_coverage_on_deep_diligence_cohort(conn):
    """80% — 12 of the 15 deep-diligence companies have a country."""
    c = coverage_report(conn)["hq_country @ deep-diligence cohort"]
    assert (c.present, c.total) == (12, 15)


def test_coverage_denominators_are_never_zero(conn):
    """Every count carries a real denominator, so none can be quoted bare."""
    for name, cov in coverage_report(conn).items():
        assert cov.total > 0, name
        assert cov.present <= cov.total, name


# ---------------------------------------------------------------------------
# Worklist
# ---------------------------------------------------------------------------

def test_enrichment_priority_distribution(conn):
    """Derived, not stored. One row better than the staging workbook.

    The workbook's distribution is 6 / 8 / 68 / 190 / 226. This layer produces
    6 / 8 / 67 / 190 / 227 because recovering Lithosquare's genuine 0.0 round
    size closes its last gap, moving it from "P2 - prelim dil, mostly empty"
    to "P4 - acceptable". That is the intended consequence of distinguishing
    zero from unknown: a field that was never missing stops being queued as
    work. The P1 queue is unaffected.
    """
    assert Counter(enrichment_priority(conn).values()) == {
        "P4 - acceptable": 227,
        "P3 - sparse record": 190,
        "P2 - prelim dil, mostly empty": 67,
        "P1 - discussed, incomplete": 8,
        "P1 - advanced stage, incomplete": 6,
    }


def test_p1_queue_is_fourteen_rows(conn):
    """The P1 queue the brief asks to work first is 14 rows."""
    p1 = [v for v in enrichment_priority(conn).values() if v.startswith("P1")]
    assert len(p1) == 14


def test_genuine_zero_round_sizes_recovered(conn):
    """Genuine 0.0 round sizes are stored as zero, not unknown (§5).

    Affinity holds 9 records with Round Size == 0.0, which the staging workbook
    flattened to unknown. Only 3 of those companies are in the slide-derived
    population at all — Lithosquare, Sequins and TriMind. The other 6 are
    Affinity-only records that never appeared on a slide, and Affinity may
    never add a company (§2 precedence), so they are correctly absent.
    """
    rows = {
        r["canonical_name"]
        for r in conn.execute(
            """SELECT e.canonical_name FROM v_field_current f
               JOIN entity e USING (entity_id)
               WHERE f.field = 'round_size_usd' AND f.is_zero = 1"""
        )
    }
    assert rows == {"Lithosquare", "Sequins", "TriMind"}


def test_zero_is_distinguishable_from_unknown(conn):
    """The three states must never collapse into each other (§8)."""
    zero = conn.execute(
        "SELECT COUNT(*) FROM v_field_current "
        "WHERE field = 'round_size_usd' AND is_zero = 1"
    ).fetchone()[0]
    known = conn.execute(
        "SELECT COUNT(*) FROM v_field_current "
        "WHERE field = 'round_size_usd' AND value_num IS NOT NULL AND is_zero = 0"
    ).fetchone()[0]
    unchecked = conn.execute(
        "SELECT COUNT(*) FROM gap_status "
        "WHERE field = 'round_size_usd' AND state = 'not_checked'"
    ).fetchone()[0]
    assert (zero, known, unchecked) == (3, 168, 330)


# ---------------------------------------------------------------------------
# Whole-database consistency
# ---------------------------------------------------------------------------

def test_reconciliation_is_clean(conn):
    """Block totals equal cohort totals; no orphans, no ghost observations."""
    assert reconciliation(conn) == []
