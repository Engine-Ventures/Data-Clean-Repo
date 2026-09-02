"""Tests for the merge/split review queue (§5, §6 Q2, §9).

The queue is the mechanism that lets 498 raw rows become a quotable company
count without any silent write, so these tests pin both what it proposes and
what it refuses to do on its own.
"""

from __future__ import annotations

import pytest

from evpipeline.ingest import canonical_score, norm_name

# ---------------------------------------------------------------------------
# Canonical-name scoring
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("fragment", "fuller"),
    [
        ("Machines", "Adiabatic Machines"),
        ("Attune", "Attune Neurosci"),
        ("Fluent", "Fluent BCI"),
        ("Harton", "Harton Tx"),
        ("Orpheus", "Orpheus Ocean"),
        ("Thermal", "PowerDrive Thermal"),
        ("Artisan Insight", "Artisan Insights"),
        ("One.Bio", "One Biosciences"),
    ],
)
def test_fuller_spelling_wins(fragment, fuller):
    assert canonical_score(fuller) > canonical_score(fragment)


@pytest.mark.parametrize(
    ("artifact", "clean"),
    [
        # Unbalanced bracket: a truncated slide cell, not a name.
        ("Tulip Biosciences (Raising", "Tulip Biosciences"),
        # Embedded separator: a grouped entry naming several companies.
        ("Flume / Gravity", "Gravity"),
        ("Water: Cetos Water / Kira", "Cetos Water"),
        # Trailing separator: a wrapped continuation.
        ("Cetos / Kira /Eden Tech/", "Cetos"),
    ],
)
def test_extraction_artifacts_lose_to_clean_names(artifact, clean):
    """A longer artifact must never outrank a shorter clean name."""
    assert canonical_score(clean) > canonical_score(artifact)


def test_score_is_a_total_order_on_the_cetos_family(conn):
    """The worst-fragmented family still produces a single winner."""
    names = [
        r["canonical_name"]
        for r in conn.execute(
            "SELECT canonical_name FROM entity WHERE canonical_name LIKE '%Cetos%'"
        )
    ]
    assert len(names) >= 7
    assert max(names, key=canonical_score) == "Cetos Water"


# ---------------------------------------------------------------------------
# What the queue proposes
# ---------------------------------------------------------------------------

def test_domain_merges_all_point_at_the_fuller_name(conn):
    """Every shared-domain proposal is oriented, not row-order arbitrary."""
    rows = conn.execute(
        """SELECT a.canonical_name src, b.canonical_name tgt
           FROM review_item ri
           JOIN entity a ON a.entity_id = ri.entity_id
           JOIN entity b ON b.entity_id = ri.target_id
           WHERE ri.kind = 'merge_proposal' AND ri.confidence = 0.95"""
    ).fetchall()
    assert len(rows) == 17
    for r in rows:
        assert canonical_score(r["tgt"]) > canonical_score(r["src"]), (
            f"{r['src']} -> {r['tgt']} points at the fragment"
        )


def test_every_domain_duplicate_family_is_queued(conn):
    """All 15 shared domains produce proposals covering every extra row."""
    families = conn.execute(
        """SELECT COUNT(*) FROM (
               SELECT domain FROM entity WHERE domain IS NOT NULL
               GROUP BY domain HAVING COUNT(*) > 1)"""
    ).fetchone()[0]
    assert families == 15
    extra_rows = conn.execute(
        """SELECT SUM(n - 1) FROM (
               SELECT COUNT(*) n FROM entity WHERE domain IS NOT NULL
               GROUP BY domain HAVING n > 1)"""
    ).fetchone()[0]
    queued = conn.execute(
        "SELECT COUNT(*) FROM review_item WHERE kind = 'merge_proposal' AND confidence = 0.95"
    ).fetchone()[0]
    assert queued == extra_rows


def test_grouped_entries_are_queued_not_deleted(conn):
    """Slash-joined entries stay as entities with an open split decision (§6 Q2).

    v2_DEDUPED resolved these by deleting the rows, which drops the company
    from the entity list while its slide observations remain. Here the row
    survives and the decision is recorded as work to do.
    """
    n = conn.execute(
        "SELECT COUNT(*) FROM review_item WHERE kind = 'group_split'"
    ).fetchone()[0]
    assert n == 14

    still_present = conn.execute(
        """SELECT COUNT(*) FROM entity
           WHERE canonical_name IN ('Cetos Water / Kira', 'Flume / Gravity',
                                    'Cetos / Kira /Eden Tech')"""
    ).fetchone()[0]
    assert still_present == 3


def test_v2_removals_are_proposals_not_applied(report, conn):
    """v2's 17 removals are recorded for review, and nothing is merged yet."""
    assert report["v2_removal_proposals"] == 17
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM review_item WHERE proposed_by = 'v2_DEDUPED'"
        ).fetchone()[0]
        == 17
    )


def test_nothing_is_merged_at_load_time(conn):
    """The population is still the raw 498; no proposal has been acted on."""
    assert conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0] == 498
    assert (
        conn.execute("SELECT COUNT(*) FROM entity WHERE merged_into IS NOT NULL").fetchone()[0]
        == 0
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM review_item WHERE state <> 'open'").fetchone()[0] == 0
    )


def test_phantom_rows_are_marked_not_deleted(conn):
    """A line-wrap continuation is flagged; the evidence row survives."""
    rows = conn.execute(
        "SELECT canonical_name, phantom_reason FROM entity WHERE is_phantom = 1"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["canonical_name"].startswith("/")
    assert "line-wrap" in rows[0]["phantom_reason"]


# ---------------------------------------------------------------------------
# Alias resolution
# ---------------------------------------------------------------------------

def test_all_slide_names_resolve_to_an_entity(report):
    """No slide observation is dropped for want of a name match."""
    assert report["unresolved_slide_names"] == 0
    assert report["resolved_exact"] + report["resolved_normalised"] == 2169


def test_alias_table_covers_the_casing_variants(conn):
    """The 18 slide spellings absent from company_name resolve via aliases."""
    for variant, canonical in [
        ("Corepower Magnetics", "CorePower Magnetics"),
        ("Neologic", "NeoLogic"),
        ("LiftOff", "Liftoff"),
        ("Nolux", "NoLux"),
    ]:
        row = conn.execute(
            """SELECT e.canonical_name FROM alias a
               JOIN entity e ON e.entity_id = a.entity_id
               WHERE a.alias_text = ?""",
            (variant,),
        ).fetchone()
        assert row is not None, f"{variant} has no alias"
        assert row["canonical_name"] == canonical


def test_normalisation_does_not_over_merge():
    """Casing and punctuation collapse; distinct companies stay distinct."""
    assert norm_name("CorePower Magnetics") == norm_name("Corepower  magnetics")
    assert norm_name("One.Bio") == norm_name("One Bio")
    # These are a merge decision, not a normalisation one.
    assert norm_name("Attune") != norm_name("Attune Tx")
    assert norm_name("Cetos") != norm_name("Cetos Water")
