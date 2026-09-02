"""Tests that the §8 write-path rules actually bite.

A validation rule that is documented but not enforced is worse than none, so
each rule here is exercised against a live database: the bad write must fail
and the good write must succeed.
"""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from evpipeline.validate import (
    PREDATES_FLAG,
    ValidationError,
    check_enum,
    check_first_meeting_order,
    check_iso_date,
    check_money,
    check_new_entity,
    check_zero_vs_unknown,
    resolve_gap,
    run_all_warnings,
    stage_regressions,
    unknown_reported_as_category,
    write_field,
)


@pytest.fixture()
def rw(report, tmp_path):
    """A writable database, copied per test.

    The validated write helpers commit, so a rollback cannot undo them. Each
    test therefore gets its own copy of the built database and the shared
    read-only `conn` fixture stays pristine.
    """
    dst = tmp_path / "rw.db"
    shutil.copyfile(report["_db_path"], dst)
    conn = sqlite3.connect(dst)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()


def _some_entity(conn) -> int:
    return int(
        conn.execute(
            "SELECT entity_id FROM entity WHERE merged_into IS NULL LIMIT 1"
        ).fetchone()[0]
    )


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def test_iso_date_required():
    check_iso_date("first_meeting", "2026-01-08")
    with pytest.raises(ValidationError, match="ISO 8601"):
        check_iso_date("first_meeting", "01/08/2026")


def test_non_date_fields_are_not_date_checked():
    check_iso_date("description", "whatever text")


def test_all_stored_dates_are_iso(conn):
    """No MM/DD/YYYY survived ingest."""
    bad = conn.execute(
        """SELECT COUNT(*) FROM v_field_current
           WHERE field IN ('first_meeting', 'last_meeting', 'affinity_date_added')
             AND value_text NOT LIKE '____-__-__'"""
    ).fetchone()[0]
    assert bad == 0


def test_meeting_dates_are_iso(conn):
    bad = conn.execute(
        "SELECT COUNT(*) FROM meeting WHERE meeting_date NOT LIKE '____-__-__'"
    ).fetchone()[0]
    assert bad == 0


# ---------------------------------------------------------------------------
# Zero vs unknown
# ---------------------------------------------------------------------------

def test_zero_must_be_marked():
    check_zero_vs_unknown(0.0, True)
    check_zero_vs_unknown(5_000_000.0, False)
    with pytest.raises(ValidationError, match="must be marked"):
        check_zero_vs_unknown(0.0, False)
    with pytest.raises(ValidationError, match="not 0"):
        check_zero_vs_unknown(1.0, True)


def test_schema_rejects_mismarked_zero(rw):
    """The CHECK constraint backs the Python rule."""
    eid = _some_entity(rw)
    with pytest.raises(sqlite3.IntegrityError):
        rw.execute(
            "INSERT INTO field_value (entity_id, field, value_num, is_zero, source, "
            "created_by) VALUES (?, 'round_size_usd', 1.0, 1, 'Manual', 'test')",
            (eid,),
        )


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

def test_enum_membership(conn):
    check_enum(conn, "affinity_status", "Deep Diligence")
    with pytest.raises(ValidationError, match="picklist"):
        check_enum(conn, "affinity_status", "Legal Diligence")


def test_affinity_has_no_legal_status(conn):
    """The known gap in §4: Affinity cannot express Legal, the slides can."""
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM affinity_status WHERE name LIKE '%Legal%'"
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM stage WHERE name LIKE '%Legal%'").fetchone()[0] == 1
    )


def test_schema_rejects_unknown_stage(rw):
    eid = _some_entity(rw)
    with pytest.raises(sqlite3.IntegrityError):
        rw.execute(
            "INSERT INTO slide_observation (meeting_date, entity_id, name_on_slide, "
            "stage_id, is_bold) VALUES ('2025-10-14', ?, 'X', 99, 0)",
            (eid,),
        )


def test_schema_rejects_unknown_source(rw):
    eid = _some_entity(rw)
    with pytest.raises(sqlite3.IntegrityError):
        rw.execute(
            "INSERT INTO field_value (entity_id, field, value_text, source, created_by) "
            "VALUES (?, 'website', 'x.com', 'Guesswork', 'test')",
            (eid,),
        )


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

def test_public_source_requires_citation(rw):
    eid = _some_entity(rw)
    with pytest.raises(sqlite3.IntegrityError):
        rw.execute(
            "INSERT INTO field_value (entity_id, field, value_text, source, created_by) "
            "VALUES (?, 'hq_country', 'USA', 'Public', 'test')",
            (eid,),
        )


def test_every_field_value_has_source_and_user(conn):
    bad = conn.execute(
        "SELECT COUNT(*) FROM field_value WHERE source IS NULL OR created_by IS NULL"
    ).fetchone()[0]
    assert bad == 0


def test_write_field_supersedes_rather_than_overwrites(rw):
    """field_value is append-only; history survives a correction."""
    eid = _some_entity(rw)
    write_field(rw, eid, "hq_city", "Cambridge", "Manual", "tester")
    write_field(rw, eid, "hq_city", "Boston", "Manual", "tester")

    current = rw.execute(
        "SELECT value_text FROM v_field_current WHERE entity_id = ? AND field = 'hq_city'",
        (eid,),
    ).fetchall()
    assert [r[0] for r in current] == ["Boston"]

    history = rw.execute(
        "SELECT COUNT(*) FROM field_value WHERE entity_id = ? AND field = 'hq_city'", (eid,)
    ).fetchone()[0]
    assert history >= 2


def test_write_field_rejects_public_without_citation(rw):
    eid = _some_entity(rw)
    with pytest.raises(ValidationError, match="citation"):
        write_field(rw, eid, "hq_country", "Switzerland", "Public", "tester")


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------

def test_money_pairing():
    check_money(5_000_000.0, None, None)
    check_money(5_000_000.0, 4_500_000.0, "CHF")
    with pytest.raises(ValidationError, match="currency"):
        check_money(5_000_000.0, 4_500_000.0, None)
    with pytest.raises(ValidationError, match="amount_usd"):
        check_money(None, None, "USD")


def test_schema_rejects_local_amount_without_currency(rw):
    eid = _some_entity(rw)
    cur = rw.execute(
        "INSERT INTO field_value (entity_id, field, value_num, source, created_by) "
        "VALUES (?, 'round_size_usd', 1.0, 'Manual', 'test')",
        (eid,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        rw.execute(
            "INSERT INTO money_value (field_value_id, amount_usd, amount_local) "
            "VALUES (?, 1.0, 0.9)",
            (cur.lastrowid,),
        )


# ---------------------------------------------------------------------------
# New records
# ---------------------------------------------------------------------------

def test_new_entity_requires_domain():
    check_new_entity("example.com")
    with pytest.raises(ValidationError, match="domain"):
        check_new_entity(None)
    with pytest.raises(ValidationError, match="domain"):
        check_new_entity("not-a-domain")


# ---------------------------------------------------------------------------
# Date ordering against the slide record
# ---------------------------------------------------------------------------

def test_first_meeting_before_slides_needs_flag(rw):
    """The rule blocks an unflagged early date and allows a flagged one."""
    row = rw.execute(
        """SELECT f.entity_id, f.first_slide_date FROM v_entity_funnel f
           WHERE f.entity_id NOT IN (
               SELECT entity_id FROM v_field_current WHERE field = ?)
           LIMIT 1""",
        (PREDATES_FLAG,),
    ).fetchone()
    eid, first_slide = int(row[0]), row[1]

    with pytest.raises(ValidationError, match="precedes"):
        check_first_meeting_order(rw, eid, "2019-01-01")

    rw.execute(
        "INSERT INTO field_value (entity_id, field, value_text, source, created_by) "
        "VALUES (?, ?, '1', 'Manual', 'test')",
        (eid, PREDATES_FLAG),
    )
    check_first_meeting_order(rw, eid, "2019-01-01")
    check_first_meeting_order(rw, eid, first_slide)


def test_predating_relationships_are_flagged_not_dropped(report, conn):
    """All 105 genuinely-older relationships carry the flag.

    Affinity first_meeting dates reach back to 2018 while the slide window
    opens 2025-10-14, so the ordering is expected rather than erroneous.
    """
    assert report["predating_relationships_flagged"] == 105
    unflagged = conn.execute(
        """SELECT COUNT(*) FROM v_entity_funnel f
           JOIN v_field_current fm
             ON fm.entity_id = f.entity_id AND fm.field = 'first_meeting'
           WHERE fm.value_text < f.first_slide_date
             AND f.entity_id NOT IN (
                 SELECT entity_id FROM v_field_current WHERE field = ?)""",
        (PREDATES_FLAG,),
    ).fetchone()[0]
    assert unflagged == 0


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------

def test_unknown_is_not_stored_as_a_category(conn):
    """hq_region 'UNKNOWN' must not be stored — it is a gap, not a region."""
    assert unknown_reported_as_category(conn) == []
    regions = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT value_text FROM v_field_current WHERE field = 'hq_region'"
        )
    }
    assert regions == {"United States", "International"}


def test_stage_regressions_are_surfaced(conn):
    """Regressions exist in the data and every one is reported."""
    regs = stage_regressions(conn)
    assert len(regs) > 0
    assert all(r.rule == "stage_regression" for r in regs)
    queued = conn.execute(
        "SELECT COUNT(*) FROM review_item WHERE kind = 'stage_regression'"
    ).fetchone()[0]
    assert queued == len(regs)


def test_warning_set_matches_queue(conn):
    assert run_all_warnings(conn) == stage_regressions(conn)


# ---------------------------------------------------------------------------
# Three-state gaps
# ---------------------------------------------------------------------------

def test_gap_states_are_three_distinct_things(rw):
    eid = _some_entity(rw)
    resolve_gap(rw, eid, "hq_country", "confirmed_unavailable", "tester", "no public HQ")
    row = rw.execute(
        "SELECT state, checked_by, note FROM gap_status WHERE entity_id = ? AND field = ?",
        (eid, "hq_country"),
    ).fetchone()
    assert row["state"] == "confirmed_unavailable"
    assert row["checked_by"] == "tester"
    assert row["note"] == "no public HQ"

    resolve_gap(rw, eid, "hq_country", "filled", "tester")
    assert (
        rw.execute(
            "SELECT state FROM gap_status WHERE entity_id = ? AND field = 'hq_country'", (eid,)
        ).fetchone()[0]
        == "filled"
    )


def test_gap_state_must_be_valid(rw):
    with pytest.raises(ValidationError, match="gap state"):
        resolve_gap(rw, _some_entity(rw), "website", "probably_fine", "tester")


def test_everything_starts_unchecked(conn):
    """Nothing in the source data records a confirmed-unavailable gap."""
    states = {
        r[0]: r[1]
        for r in conn.execute("SELECT state, COUNT(*) FROM gap_status GROUP BY state")
    }
    assert states == {"not_checked": 1513}
