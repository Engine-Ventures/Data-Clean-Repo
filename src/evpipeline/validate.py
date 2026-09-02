"""Write-path validation rules from the handoff brief §8.

Every rule here is enforced on write rather than checked in a report, because
the point of the interface is to stop bad values entering in the first place.
Rules that cannot be expressed as a SQL constraint live here; the ones that
can (enum membership, is_zero, citation-required, currency pairing) are CHECK
constraints and foreign keys in schema.sql and are exercised by the tests.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Fields whose values must be an ISO 8601 date.
DATE_FIELDS = {
    "affinity_date_added",
    "first_meeting",
    "last_meeting",
    "outcome_date",
}

# Set on an entity whose relationship predates the slide extraction window.
PREDATES_FLAG = "relationship_predates_crm"


class ValidationError(ValueError):
    """Raised when a write would violate a §8 rule."""


@dataclass(frozen=True)
class Warning_:
    """A rule that warns rather than blocks."""

    rule: str
    entity_id: int | None
    detail: str


# ---------------------------------------------------------------------------
# Blocking rules
# ---------------------------------------------------------------------------

def check_iso_date(field: str, value: str | None) -> None:
    """Dates must be ISO 8601 (§8)."""
    if value is None or field not in DATE_FIELDS:
        return
    if not ISO_DATE.match(value):
        raise ValidationError(
            f"{field}={value!r} is not ISO 8601; Affinity exports MM/DD/YYYY "
            f"and must be converted before write"
        )


def check_enum(conn: sqlite3.Connection, table: str, value: str | None) -> None:
    """All enums come from the locked picklists, no free text (§8)."""
    if value is None:
        return
    hit = conn.execute(f"SELECT 1 FROM {table} WHERE name = ?", (value,)).fetchone()
    if hit is None:
        raise ValidationError(f"{value!r} is not in the {table} picklist")


def check_zero_vs_unknown(value_num: float | None, is_zero: bool) -> None:
    """Blank means unknown, 0 means genuinely zero; never interchangeable (§8)."""
    if is_zero and value_num not in (0, 0.0):
        raise ValidationError("is_zero set but value_num is not 0")
    if not is_zero and value_num == 0:
        raise ValidationError(
            "value_num is 0 without is_zero; a real zero must be marked as one "
            "so it cannot be read back as unknown"
        )


def check_money(amount_usd: float | None, amount_local: float | None, currency: str | None) -> None:
    """Currency always stored as _usd with amount_local + currency alongside (§8)."""
    if amount_usd is None:
        raise ValidationError("money values require amount_usd")
    if amount_local is not None and not currency:
        raise ValidationError("amount_local requires a currency")


def check_new_entity(domain: str | None) -> None:
    """New records require a domain (§8) — it is the primary match key."""
    if not domain or "." not in domain:
        raise ValidationError(
            "a new company requires a domain; name-only records cannot be "
            "matched reliably and create the fragmentation this layer exists to fix"
        )


def check_first_meeting_order(
    conn: sqlite3.Connection, entity_id: int, first_meeting: str | None
) -> None:
    """Reject first_meeting earlier than first_slide_date unless flagged (§8)."""
    if first_meeting is None:
        return
    row = conn.execute(
        "SELECT first_slide_date FROM v_entity_funnel WHERE entity_id = ?", (entity_id,)
    ).fetchone()
    if row is None or row[0] is None:
        return
    if first_meeting >= row[0]:
        return
    flagged = conn.execute(
        "SELECT 1 FROM v_field_current WHERE entity_id = ? AND field = ?",
        (entity_id, PREDATES_FLAG),
    ).fetchone()
    if flagged is None:
        raise ValidationError(
            f"first_meeting {first_meeting} precedes first_slide_date {row[0]}; "
            f"set {PREDATES_FLAG} if the relationship genuinely predates the "
            f"slide record"
        )


# ---------------------------------------------------------------------------
# Warning rules
# ---------------------------------------------------------------------------

def stage_regressions(conn: sqlite3.Connection) -> list[Warning_]:
    """Warn on stage regression without a flag (§8).

    A regression is legitimate — a deal cools and moves back to Hold — but it
    should be a stated decision rather than a silent diff, so each one is
    surfaced and lands in the review queue at ingest.
    """
    out: list[Warning_] = []
    for r in conn.execute(
        """SELECT t.entity_id, t.from_date, t.to_date, t.delta,
                  sf.name from_stage, st.name to_stage
           FROM v_stage_transition t
           JOIN stage sf ON sf.stage_id = t.from_stage_id
           JOIN stage st ON st.stage_id = t.to_stage_id
           WHERE t.delta < 0"""
    ):
        out.append(
            Warning_(
                "stage_regression",
                int(r["entity_id"]),
                f"{r['from_stage']} -> {r['to_stage']} between "
                f"{r['from_date']} and {r['to_date']} ({r['delta']:+d})",
            )
        )
    return out


def unknown_reported_as_category(conn: sqlite3.Connection) -> list[Warning_]:
    """UNKNOWN is a gap, not a category (§8).

    The staging workbook stored hq_region = 'UNKNOWN' for 342 rows, which makes
    it look like a third region in any group-by. Ingest drops those rather than
    storing them, so this should always come back empty.
    """
    n = conn.execute(
        "SELECT COUNT(*) FROM v_field_current WHERE value_text = 'UNKNOWN'"
    ).fetchone()[0]
    return (
        []
        if n == 0
        else [Warning_("unknown_as_category", None, f"{n} field values literally 'UNKNOWN'")]
    )


def run_all_warnings(conn: sqlite3.Connection) -> list[Warning_]:
    return stage_regressions(conn) + unknown_reported_as_category(conn)


# ---------------------------------------------------------------------------
# Validated write
# ---------------------------------------------------------------------------

def write_field(
    conn: sqlite3.Connection,
    entity_id: int,
    field: str,
    value: str | None,
    source: str,
    user: str,
    citation: str | None = None,
    value_num: float | None = None,
    is_zero: bool = False,
) -> int:
    """The single validated write path for an enriched field.

    Supersedes the previous current value rather than updating in place, so
    field_value stays append-only and the provenance history is complete.
    """
    check_iso_date(field, value)
    if value_num is not None or is_zero:
        check_zero_vs_unknown(value_num, is_zero)
    if field == "affinity_status":
        check_enum(conn, "affinity_status", value)
    elif field == "working_group":
        check_enum(conn, "working_group", value)
    elif field == "stage":
        check_enum(conn, "round_stage", value)
    if field == "first_meeting":
        check_first_meeting_order(conn, entity_id, value)
    if source == "Public" and not citation:
        raise ValidationError("public enrichment requires a citation")

    conn.execute(
        "UPDATE field_value SET superseded_at = datetime('now') "
        "WHERE entity_id = ? AND field = ? AND superseded_at IS NULL",
        (entity_id, field),
    )
    cur = conn.execute(
        "INSERT INTO field_value (entity_id, field, value_text, value_num, is_zero, "
        "source, citation, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (entity_id, field, value, value_num, 1 if is_zero else 0, source, citation, user),
    )
    conn.commit()
    return int(cur.lastrowid)


def resolve_gap(
    conn: sqlite3.Connection, entity_id: int, field: str, state: str, user: str, note: str = ""
) -> None:
    """Move a gap between the three states (§8 worklist).

    'confirmed_unavailable' is the one the current data cannot express at all:
    nothing records that someone looked for a value and found none to be had.
    """
    if state not in {"not_checked", "confirmed_unavailable", "filled"}:
        raise ValidationError(f"{state!r} is not a gap state")
    conn.execute(
        "INSERT INTO gap_status (entity_id, field, state, checked_by, checked_at, note) "
        "VALUES (?, ?, ?, ?, datetime('now'), ?) "
        "ON CONFLICT (entity_id, field) DO UPDATE SET "
        "state = excluded.state, checked_by = excluded.checked_by, "
        "checked_at = excluded.checked_at, note = excluded.note",
        (entity_id, field, state, user, note or None),
    )
    conn.commit()
