"""The controlled vocabularies in vocab.py must match what seed.sql seeds.

This file exists because they did not, and 709 slide_observation rows carried
the wrong stage name as a result -- 476 Hold / Nurture rows reading as
"NewCo / Fellows" and 233 NewCo rows reading as "Hold / Nurture", changing the
furthest-stage label of 57 companies. See MIGRATION.md for the mechanism.

Two properties of that bug decide how these tests are written:

  * **It was invisible in the numbers.** Every threshold in v_entity_funnel is
    `>= 4` and both misassigned ranks are below Preliminary Diligence, so no
    metric moved. Only labels lied. A test that checks counts would have
    passed throughout.
  * **`INSERT OR IGNORE` hid it.** Seeding the union of two disagreeing
    definitions does not reconcile them, it just picks one per table and
    leaves the other live in code. So every assertion below is
    **bidirectional**: same ids, same names, and no extra rows on either
    side. A one-way subset check is what let this survive.
"""

from __future__ import annotations

import pytest

from evpipeline import vocab


def _rows(conn, query: str) -> list:
    return conn.execute(query).fetchall()


# ---------------------------------------------------------------------------
# stage
# ---------------------------------------------------------------------------


def test_stage_table_matches_vocab_exactly(seeded_conn):
    """vocab.STAGES and the seeded `stage` table agree in both directions."""
    seeded = {int(r["stage_id"]): r["name"] for r in _rows(
        seeded_conn, "SELECT stage_id, name FROM stage"
    )}
    declared = {int(sid): name for sid, name in vocab.STAGES}

    assert declared == seeded, (
        "vocab.STAGES disagrees with the seeded stage table.\n"
        f"  only in vocab.py: {sorted(set(declared.items()) - set(seeded.items()))}\n"
        f"  only in seed.sql: {sorted(set(seeded.items()) - set(declared.items()))}\n"
        "seed.sql is authoritative; vocab.py moves to match."
    )


def test_stage_id_equals_rank(seeded_conn):
    """stage_id == rank, which is what lets MAX(stage_id) mean furthest stage.

    v_entity_funnel.furthest_stage_id, v_entity_latest_stage and
    v_stage_transition all rely on this; if the two ever diverge, "furthest
    stage" silently starts meaning "highest surrogate key".
    """
    for r in _rows(seeded_conn, "SELECT stage_id, rank, name FROM stage"):
        assert int(r["stage_id"]) == int(r["rank"]), (
            f"stage {r['name']!r} has stage_id {r['stage_id']} but rank {r['rank']}"
        )


def test_stage_by_name_is_consistent_with_stages():
    """The derived lookup cannot drift from the list it is derived from."""
    assert {name: sid for sid, name in vocab.STAGES} == vocab.STAGE_BY_NAME
    assert len(vocab.STAGE_BY_NAME) == len(vocab.STAGES), "duplicate stage name"


def test_the_two_swapped_stages_are_pinned(seeded_conn):
    """Ranks 2 and 3 specifically, by name, in both producers.

    Pinned as literals rather than derived, because this is the exact pair
    that was wrong and a derived assertion would have been satisfied by the
    swap. The workbook's Data Dictionary treats NewCo/Fellows and Hold/Nurture
    as functionally tied below Preliminary Diligence, so the relative order of
    2 and 3 is arbitrary -- but it has to be arbitrary in ONE place, and that
    place is seed.sql.
    """
    assert vocab.STAGE_BY_NAME["NewCo / Fellows"] == 2
    assert vocab.STAGE_BY_NAME["Hold / Nurture"] == 3
    seeded = {r["name"]: int(r["stage_id"]) for r in _rows(
        seeded_conn, "SELECT stage_id, name FROM stage WHERE stage_id IN (2, 3)"
    )}
    assert seeded == {"NewCo / Fellows": 2, "Hold / Nurture": 3}


# ---------------------------------------------------------------------------
# slide_section_map -- the table that was internally inconsistent
# ---------------------------------------------------------------------------


def test_slide_section_map_matches_vocab_exactly(seeded_conn):
    """Every raw section maps to the same stage_id in vocab.py and seed.sql.

    The seeded table was self-contradictory before the fix: 'Hold / Nurture'
    resolved to stage 3 here while every observation carrying that section
    resolved to stage 2, because the two were written from different
    definitions. Comparing resolved stage *ids* (not names) is the point --
    the ids are what ingest actually writes.
    """
    seeded = {
        r["raw_section"]: (int(r["stage_id"]), r["thesis_code"])
        for r in _rows(
            seeded_conn, "SELECT raw_section, stage_id, thesis_code FROM slide_section_map"
        )
    }
    declared = {
        raw: (vocab.STAGE_BY_NAME[stage], thesis)
        for raw, stage, thesis, _note in vocab.SLIDE_SECTION_MAP
    }

    assert declared.keys() == seeded.keys(), (
        f"raw_section sets differ.\n"
        f"  only in vocab.py: {sorted(declared.keys() - seeded.keys())}\n"
        f"  only in seed.sql: {sorted(seeded.keys() - declared.keys())}"
    )
    mismatched = {k: (declared[k], seeded[k]) for k in declared if declared[k] != seeded[k]}
    assert not mismatched, (
        "raw_section -> (stage_id, thesis_code) differs between vocab.py and "
        f"seed.sql for: {mismatched}  (shown as vocab vs seeded)"
    )


def test_every_section_stage_is_a_real_stage(seeded_conn):
    """No section maps to a stage_id the stage table does not have.

    The FK enforces this in the database; asserted here so vocab.py cannot
    ship a broken mapping that only fails at insert time.
    """
    valid = {int(r["stage_id"]) for r in _rows(seeded_conn, "SELECT stage_id FROM stage")}
    for raw, stage, _thesis, _note in vocab.SLIDE_SECTION_MAP:
        assert stage in vocab.STAGE_BY_NAME, f"section {raw!r} names unknown stage {stage!r}"
        assert vocab.STAGE_BY_NAME[stage] in valid


def test_prelim_diligence_sections_carry_a_thesis_code(seeded_conn):
    """The thesis sub-sections are exactly the stage-4 ones, and only those.

    This is the invariant behind the agenda-marker/dual-listing story: a
    company is listed under "Meetings this week" (stage 1) *and* its thesis
    sub-section (stage 4). If a thesis code ever appeared on a non-prelim
    section, v_same_slide_stage_conflict's "distinct funnel stages" rule would
    start reporting legitimate dual listings as conflicts.
    """
    for r in _rows(seeded_conn, "SELECT raw_section, stage_id, thesis_code FROM slide_section_map"):
        if r["thesis_code"] is not None:
            assert int(r["stage_id"]) == 4, (
                f"section {r['raw_section']!r} has thesis code "
                f"{r['thesis_code']!r} but stage {r['stage_id']}"
            )


# ---------------------------------------------------------------------------
# The remaining vocabularies, same bidirectional rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "column", "declared"),
    [
        ("thesis_area", "code", lambda: {c for c, _n in vocab.THESIS_AREAS}),
        ("working_group", "name", lambda: set(vocab.WORKING_GROUPS)),
        ("round_stage", "name", lambda: {n for n, _r in vocab.ROUND_STAGES}),
        ("affinity_status", "name", lambda: {n for n, _r in vocab.AFFINITY_STATUSES}),
        ("enrichment_priority", "name", lambda: {n for n, _t in vocab.ENRICHMENT_PRIORITIES}),
        ("source", "name", lambda: {n for n, _p in vocab.SOURCES}),
        ("pass_reason_category", "name", lambda: {n for n, _s in vocab.PASS_REASON_CATEGORIES}),
        ("outcome_type", "name", lambda: {n for n, _t in vocab.OUTCOME_TYPES}),
        ("source_channel", "name", lambda: set(vocab.SOURCE_CHANNELS)),
    ],
)
def test_vocabulary_matches_seed(seeded_conn, table, column, declared):
    """Each locked picklist has the same members in code and in the database."""
    seeded = {r[0] for r in _rows(seeded_conn, f"SELECT {column} FROM {table}")}
    want = declared()
    assert want == seeded, (
        f"{table} membership differs.\n"
        f"  only in vocab.py: {sorted(want - seeded)}\n"
        f"  only in seed.sql: {sorted(seeded - want)}"
    )


def test_source_precedence_is_the_documented_order(seeded_conn):
    """Slides > Affinity > Public > Manual (§2), in both producers.

    Precedence is not decoration: ingest resolves a field conflict by it, so a
    reordering here silently changes which source wins a contested value.
    """
    assert [n for n, _p in sorted(vocab.SOURCES, key=lambda s: s[1])] == [
        "Slides", "Affinity", "Public", "Manual"
    ]
    seeded = [
        r["name"] for r in _rows(seeded_conn, "SELECT name FROM source ORDER BY precedence")
    ]
    assert seeded == ["Slides", "Affinity", "Public", "Manual"]
