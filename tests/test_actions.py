"""Tests for the write verbs in src/evpipeline/actions.py.

These are the functions the review-queue buttons call, and until this file
existed none of them were pinned — every check below was run once by hand,
against the live database, while building the workbench this session.

Uses the `rw` fixture already established in test_validation.py: a per-test
copy of the built database, so writes never leak between tests and the
session-scoped `conn`/`report` fixtures stay read-only.
"""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from evpipeline import actions
from evpipeline.validate import ValidationError


@pytest.fixture()
def rw(report, tmp_path):
    dst = tmp_path / "rw.db"
    shutil.copyfile(report["_db_path"], dst)
    conn = sqlite3.connect(dst)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()


def _open_merge_proposal(conn) -> sqlite3.Row:
    """A domain-backed merge proposal, oriented src -> target, still open."""
    return conn.execute(
        "SELECT review_id, entity_id, target_id FROM review_item "
        "WHERE kind = 'merge_proposal' AND confidence = 0.95 AND state = 'open' "
        "LIMIT 1"
    ).fetchone()


def _some_live_entity(conn, exclude: set[int] = frozenset()) -> int:
    clause = f"AND entity_id NOT IN ({','.join('?' * len(exclude))})" if exclude else ""
    row = conn.execute(
        f"SELECT entity_id FROM entity WHERE merged_into IS NULL {clause} LIMIT 1",
        tuple(exclude),
    ).fetchone()
    return int(row[0])


# ---------------------------------------------------------------------------
# merge_entities
# ---------------------------------------------------------------------------


def test_accepting_a_proposal_collapses_the_funnel_by_one(rw):
    prop = _open_merge_proposal(rw)
    obs_before = rw.execute("SELECT COUNT(*) FROM slide_observation").fetchone()[0]
    funnel_before = rw.execute("SELECT COUNT(*) FROM v_entity_funnel").fetchone()[0]

    actions.accept_merge_proposal(rw, prop["review_id"], "test")

    obs_after = rw.execute("SELECT COUNT(*) FROM slide_observation").fetchone()[0]
    funnel_after = rw.execute("SELECT COUNT(*) FROM v_entity_funnel").fetchone()[0]
    assert obs_after == obs_before, "the evidence log must never change size"
    assert funnel_after == funnel_before - 1


def test_merge_moves_aliases_and_drops_a_collision(rw):
    prop = _open_merge_proposal(rw)
    src_id, dst_id = int(prop["entity_id"]), int(prop["target_id"])
    src_aliases_before = {
        r[0] for r in rw.execute("SELECT alias_text FROM alias WHERE entity_id = ?", (src_id,))
    }
    assert src_aliases_before, "fixture assumption: src has at least one alias"

    # Force a collision: one of src's aliases already exists on dst.
    clashing = next(iter(src_aliases_before))
    rw.execute(
        "INSERT INTO alias (entity_id, alias_text, alias_norm, source, match_method) "
        "VALUES (?, ?, ?, 'Manual', 'manual')",
        (dst_id, clashing + "__dst_copy", "zzz_collision_norm"),
    )
    rw.execute(
        "UPDATE alias SET alias_norm = 'zzz_collision_norm' WHERE entity_id = ? AND alias_text = ?",
        (src_id, clashing),
    )
    rw.commit()

    result = actions.merge_entities(rw, src_id, dst_id, "test")

    assert set(result["aliases_moved"]) == src_aliases_before
    left_on_src = rw.execute(
        "SELECT COUNT(*) FROM alias WHERE entity_id = ?", (src_id,)
    ).fetchone()[0]
    assert left_on_src == 0, "every alias leaves src, moved or dropped"


def test_cannot_merge_into_an_already_merged_entity(rw):
    a, b, c = (
        _some_live_entity(rw),
        _some_live_entity(rw, exclude={0}),
        None,
    )
    ids = rw.execute(
        "SELECT entity_id FROM entity WHERE merged_into IS NULL LIMIT 3"
    ).fetchall()
    a, b, c = (int(r[0]) for r in ids)

    actions.merge_entities(rw, a, b, "test")  # a -> b, one hop
    with pytest.raises(ValidationError, match=r"itself.*merged|chain"):
        actions.merge_entities(rw, c, a, "test")  # c -> a would be two hops


def test_cannot_merge_an_entity_into_itself(rw):
    eid = _some_live_entity(rw)
    with pytest.raises(ValidationError, match="itself"):
        actions.merge_entities(rw, eid, eid, "test")


def test_accepting_a_proposal_touches_exactly_one_review_row(rw):
    """Regression test: this used to leave two rows behind (293 -> 297 live)."""
    prop = _open_merge_proposal(rw)
    total_before = rw.execute("SELECT COUNT(*) FROM review_item").fetchone()[0]

    actions.accept_merge_proposal(rw, prop["review_id"], "test")

    total_after = rw.execute("SELECT COUNT(*) FROM review_item").fetchone()[0]
    assert total_after == total_before, (
        "accepting a queued proposal must update it in place, not insert a "
        "second row alongside it"
    )
    row = rw.execute(
        "SELECT state FROM review_item WHERE review_id = ?", (prop["review_id"],)
    ).fetchone()
    assert row["state"] == "accepted"


def test_a_merge_with_no_proposal_behind_it_still_gets_an_audit_row(rw):
    """The other branch: two companies a person noticed, that ingest never
    proposed. review_id=None (the default), so a fresh row is inserted."""
    ids = rw.execute(
        "SELECT entity_id FROM entity WHERE merged_into IS NULL LIMIT 2"
    ).fetchall()
    src_id, dst_id = int(ids[0][0]), int(ids[1][0])
    total_before = rw.execute("SELECT COUNT(*) FROM review_item").fetchone()[0]

    actions.merge_entities(rw, src_id, dst_id, "test")

    total_after = rw.execute("SELECT COUNT(*) FROM review_item").fetchone()[0]
    assert total_after == total_before + 1


# ---------------------------------------------------------------------------
# unmerge
# ---------------------------------------------------------------------------


def test_unmerge_restores_the_live_count(rw):
    prop = _open_merge_proposal(rw)
    src_id = int(prop["entity_id"])
    before = actions.live_count(rw)

    actions.accept_merge_proposal(rw, prop["review_id"], "test")
    assert actions.live_count(rw) == before - 1

    out = actions.unmerge(rw, src_id, "test")
    assert out["live_count"] == before
    assert rw.execute(
        "SELECT merged_into FROM entity WHERE entity_id = ?", (src_id,)
    ).fetchone()[0] is None


def test_unmerging_a_never_merged_entity_is_refused(rw):
    eid = _some_live_entity(rw)
    with pytest.raises(ValidationError, match="not merged"):
        actions.unmerge(rw, eid, "test")


# ---------------------------------------------------------------------------
# phantom
# ---------------------------------------------------------------------------


def test_phantom_needs_a_reason(rw):
    eid = _some_live_entity(rw)
    with pytest.raises(ValidationError, match="reason"):
        actions.mark_phantom(rw, eid, "   ", "test")


def test_marking_a_phantom_leaves_the_evidence_log_untouched(rw):
    eid = _some_live_entity(rw)
    obs_before = rw.execute("SELECT COUNT(*) FROM slide_observation").fetchone()[0]

    actions.mark_phantom(rw, eid, "line-wrap continuation, not a company", "test")

    row = rw.execute(
        "SELECT is_phantom, phantom_reason FROM entity WHERE entity_id = ?", (eid,)
    ).fetchone()
    assert row["is_phantom"] == 1
    assert "line-wrap" in row["phantom_reason"]
    obs_after = rw.execute("SELECT COUNT(*) FROM slide_observation").fetchone()[0]
    assert obs_after == obs_before


def test_unmark_phantom_restores_it(rw):
    eid = _some_live_entity(rw)
    actions.mark_phantom(rw, eid, "test reason", "test")
    actions.unmark_phantom(rw, eid, "test")
    row = rw.execute(
        "SELECT is_phantom, phantom_reason FROM entity WHERE entity_id = ?", (eid,)
    ).fetchone()
    assert row["is_phantom"] == 0
    assert row["phantom_reason"] is None


# ---------------------------------------------------------------------------
# resolve_review
# ---------------------------------------------------------------------------


def test_resolve_review_rejects_an_unknown_state(rw):
    prop = _open_merge_proposal(rw)
    with pytest.raises(ValidationError, match="review state"):
        actions.resolve_review(rw, prop["review_id"], "maybe-later", "test")


def test_resolve_review_on_a_missing_id_is_refused(rw):
    with pytest.raises(ValidationError, match="no review item"):
        actions.resolve_review(rw, 999_999, "rejected", "test")


def test_resolve_review_records_who_and_the_open_count(rw):
    prop = _open_merge_proposal(rw)
    open_before = rw.execute(
        "SELECT COUNT(*) FROM review_item WHERE state = 'open'"
    ).fetchone()[0]

    out = actions.resolve_review(rw, prop["review_id"], "deferred", "test", note="later")

    assert out["open_items"] == open_before - 1
    row = rw.execute(
        "SELECT state, resolved_by, resolution_note FROM review_item WHERE review_id = ?",
        (prop["review_id"],),
    ).fetchone()
    assert (row["state"], row["resolved_by"], row["resolution_note"]) == (
        "deferred", "test", "later",
    )


# ---------------------------------------------------------------------------
# add_entity
# ---------------------------------------------------------------------------


def test_add_entity_requires_a_domain(rw):
    with pytest.raises(ValidationError, match="domain"):
        actions.add_entity(rw, "New Co", "", "test")


def test_add_entity_rejects_a_domain_already_in_use(rw):
    row = rw.execute(
        "SELECT domain FROM entity WHERE domain IS NOT NULL LIMIT 1"
    ).fetchone()
    with pytest.raises(ValidationError, match="already belongs to"):
        actions.add_entity(rw, "Copycat Inc", row["domain"], "test")


def test_add_entity_rejects_public_field_with_no_citation(rw):
    with pytest.raises(ValidationError, match="citation"):
        actions.add_entity(
            rw, "Halide Thermal", "halidethermal.example", "test",
            fields=[{"field": "hq_country", "value": "Chile", "source": "Public"}],
        )


def test_add_entity_leaves_a_blank_field_unwritten(rw):
    """The three-state rule this whole layer is built on: blank is unknown,
    which is different from a stored empty string and different from zero."""
    out = actions.add_entity(
        rw, "Halide Thermal", "halidethermal.example", "test",
        fields=[
            {"field": "hq_country", "value": "United States", "source": "Affinity"},
            {"field": "owner_name", "value": "", "source": "Affinity"},
        ],
    )
    assert out["written"] == ["hq_country"]
    assert out["left_unknown"] == ["owner_name"]
    stored = rw.execute(
        "SELECT COUNT(*) FROM field_value WHERE entity_id = ? AND field = 'owner_name'",
        (out["entity_id"],),
    ).fetchone()[0]
    assert stored == 0, "a blank field must not be written as an empty string"


def test_add_entity_stores_a_genuine_zero_distinctly(rw):
    out = actions.add_entity(
        rw, "Halide Thermal", "halidethermal.example", "test",
        fields=[
            {"field": "round_size_usd", "value": "", "is_zero": True, "source": "Affinity"},
        ],
    )
    row = rw.execute(
        "SELECT value_num, is_zero FROM field_value "
        "WHERE entity_id = ? AND field = 'round_size_usd'",
        (out["entity_id"],),
    ).fetchone()
    assert (row["value_num"], row["is_zero"]) == (0.0, 1)
