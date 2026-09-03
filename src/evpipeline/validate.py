"""Write-path validation rules from the handoff brief §8.

Every rule here is enforced on write rather than checked in a report, because
the point of the interface is to stop bad values entering in the first place.
Rules that cannot be expressed as a SQL constraint live here; the ones that
can (enum membership, is_zero, citation-required, currency pairing) are CHECK
constraints and foreign keys in schema.sql and are exercised by the tests.
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from .db import connect

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

def validate_field_write(
    conn: sqlite3.Connection,
    entity_id: int,
    field: str,
    value: str | None,
    source: str,
    citation: str | None = None,
    value_num: float | None = None,
    is_zero: bool = False,
) -> None:
    """Every §8 blocking rule that applies to one field write.

    Split out of write_field so a caller can check a write without performing
    it -- the CLI's --dry-run -- without a second copy of the rules that could
    drift from the one the write actually enforces.
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
    validate_field_write(
        conn, entity_id, field, value, source,
        citation=citation, value_num=value_num, is_zero=is_zero,
    )

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


# ---------------------------------------------------------------------------
# Headless entrypoint
# ---------------------------------------------------------------------------
#
# The §8 write surface was reachable only from Python, and the interface note
# in the README says editing "needs a server to be reachable from a browser".
# That makes scripted enrichment impossible, which is the whole of the Monday
# ask. This is that same write path with an argv front end: it calls
# write_field / resolve_gap and nothing else, so every rule above still bites.
# There is deliberately no --force and no direct-SQL escape hatch.

NUMERIC_SUFFIXES = ("_usd",)

# The four source names in the locked picklist (§2 precedence order).
SOURCES = ("Slides", "Affinity", "Public", "Manual")


def _is_numeric_field(field: str) -> bool:
    return field.endswith(NUMERIC_SUFFIXES)


def resolve_entity(conn: sqlite3.Connection, ref: str) -> int:
    """Resolve a company reference to an entity_id.

    Accepts a bare entity_id (``123``), the workbook's company_id
    (``EV0123``), a canonical name, or any recorded alias -- the alias table
    exists precisely so slide spellings resolve without touching the evidence
    log. A reference matching more than one live entity is an error rather
        than a guess, the same rule the merge proposals follow.
    """
    ref = ref.strip()
    if re.fullmatch(r"(?i)ev\d+", ref):
        ref = str(int(ref[2:]))
    if ref.isdigit():
        row = conn.execute(
            "SELECT entity_id FROM entity WHERE entity_id = ?", (int(ref),)
        ).fetchone()
        if row is None:
            raise ValidationError(f"no entity with id {ref}")
        return int(row[0])

    rows = conn.execute(
        """SELECT DISTINCT e.entity_id, e.canonical_name FROM entity e
           LEFT JOIN alias a ON a.entity_id = e.entity_id
           WHERE e.merged_into IS NULL
             AND (e.canonical_name = ? COLLATE NOCASE
                  OR a.alias_text = ? COLLATE NOCASE)""",
        (ref, ref),
    ).fetchall()
    if not rows:
        raise ValidationError(f"no live entity named {ref!r}")
    if len(rows) > 1:
        names = ", ".join(f"{r[1]} (id {r[0]})" for r in rows)
        raise ValidationError(
            f"{ref!r} matches {len(rows)} live entities: {names}; "
            f"use the entity id to disambiguate"
        )
    return int(rows[0][0])


def _parse_assignment(text: str) -> tuple[str, str]:
    if "=" not in text:
        raise ValidationError(f"expected field=value, got {text!r}")
    field, _, value = text.partition("=")
    field = field.strip()
    if not field:
        raise ValidationError(f"empty field name in {text!r}")
    return field, value.strip()


def _coerce(field: str, value: str) -> tuple[str | None, float | None, bool]:
    """(value_text, value_num, is_zero) for one field=value assignment.

    An empty value is rejected rather than written as NULL: blank means
    unknown, and the way to record "looked for it, none to be had" is
    --gap ... confirmed_unavailable, not a blank write.
    """
    if value == "":
        raise ValidationError(
            f"{field} was given an empty value; blank means unknown. To record "
            f"that a value was sought and does not exist, use "
            f"--gap <company> {field} confirmed_unavailable"
        )
    if _is_numeric_field(field):
        try:
            num = float(value.replace(",", "").replace("$", ""))
        except ValueError:
            raise ValidationError(f"{field} is numeric; {value!r} is not a number") from None
        return None, num, num == 0
    return value, None, False


def _read_batch(path: Path) -> list[dict[str, str]]:
    """Read a batch of writes from CSV.

    Columns: company, field, value, source, and optional citation / note.
    One row per field so the file is the same shape as the enrichment log.
    """
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    required = {"company", "field", "value", "source"}
    for i, r in enumerate(rows, start=2):
        missing = required - {k for k, v in r.items() if v not in (None, "")}
        if missing:
            raise ValidationError(f"{path}:{i} missing {', '.join(sorted(missing))}")
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m evpipeline.validate",
        description=(
            "The validated write path, headless. Applies the same §8 rules the "
            "browser path would, so enrichment can be scripted."
        ),
        epilog=(
            "examples:\n"
            "  python -m evpipeline.validate --set EV0123 hq_country='United States' "
            "--source Affinity --user you\n"
            "  python -m evpipeline.validate --set 'Eden Tech' stage=Seed "
            "--source Public --citation https://... --user you\n"
            "  python -m evpipeline.validate --gap EV0123 round_size_usd "
            "confirmed_unavailable --user you\n"
            "  python -m evpipeline.validate --batch enrich.csv --user you --dry-run\n"
            "  python -m evpipeline.validate --show EV0123\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # No default: there is no local file to fall back on any more. Passing
    # nothing makes db.connect() read DATABASE_URL, which is the normal path;
    # --db stays for pointing a one-off write at a different Neon branch.
    # (The rest of this module's port -- savepoints, the COLLATE NOCASE
    # rewrite, and pulling the commits out -- is phase 5, still pending.)
    ap.add_argument("--db", default=None, help="postgresql:// URL (default: $DATABASE_URL)")
    ap.add_argument("--user", help="who is making the write; recorded as created_by")
    ap.add_argument("--source", choices=SOURCES, help="provenance for --set")
    ap.add_argument("--citation", help="required when --source Public")
    ap.add_argument("--note", default="", help="note for --gap")
    ap.add_argument("--dry-run", action="store_true", help="validate without writing")

    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--set", nargs="+", metavar=("COMPANY", "FIELD=VALUE"),
        help="write one or more field=value pairs for one company",
    )
    mode.add_argument(
        "--gap", nargs=3, metavar=("COMPANY", "FIELD", "STATE"),
        help="set a gap state: not_checked | confirmed_unavailable | filled",
    )
    mode.add_argument("--batch", metavar="CSV", help="apply many writes from a CSV")
    mode.add_argument("--show", metavar="COMPANY", help="print current field values")
    mode.add_argument(
        "--warnings", action="store_true", help="run the non-blocking rules and print them",
    )

    args = ap.parse_args(argv)

    db = Path(args.db)
    if not db.exists():
        print(f"missing database: {db}; run scripts/build_db.py first", file=sys.stderr)
        return 2
    conn = connect(db)

    try:
        if args.show:
            eid = resolve_entity(conn, args.show)
            name = conn.execute(
                "SELECT canonical_name FROM entity WHERE entity_id = ?", (eid,)
            ).fetchone()[0]
            print(f"{name} (entity {eid}, EV{eid:04d})")
            for r in conn.execute(
                "SELECT field, value_text, value_num, is_zero, source, citation, created_by "
                "FROM v_field_current WHERE entity_id = ? ORDER BY field", (eid,)
            ):
                val = r["value_text"]
                if val is None and r["value_num"] is not None:
                    val = 0 if r["is_zero"] else r["value_num"]
                cite = f"  <{r['citation']}>" if r["citation"] else ""
                print(f"  {r['field']:26} {val!s:32} [{r['source']}/{r['created_by']}]{cite}")
            for r in conn.execute(
                "SELECT field, state, note FROM gap_status WHERE entity_id = ? "
                "AND state <> 'not_checked' ORDER BY field", (eid,)
            ):
                print(f"  gap {r['field']:22} {r['state']}  {r['note'] or ''}")
            return 0

        if args.warnings:
            warns = run_all_warnings(conn)
            for w in warns:
                who = f"entity {w.entity_id}" if w.entity_id is not None else "-"
                print(f"{w.rule:22} {who:12} {w.detail}")
            print(f"{len(warns)} warning(s)")
            return 0

        if not args.user:
            ap.error("--user is required for a write")

        # Each unit is (entity ref, field, raw value, source, citation, note).
        units: list[tuple[str, str, str, str, str | None, str]] = []
        if args.set:
            if len(args.set) < 2:
                ap.error("--set needs a company and at least one field=value")
            if not args.source:
                ap.error("--set requires --source")
            company = args.set[0]
            for assign in args.set[1:]:
                field, value = _parse_assignment(assign)
                units.append((company, field, value, args.source, args.citation, ""))
        elif args.gap:
            company, field, state = args.gap
            eid = resolve_entity(conn, company)
            if args.dry_run:
                if state not in {"not_checked", "confirmed_unavailable", "filled"}:
                    raise ValidationError(f"{state!r} is not a gap state")
                print(f"[dry-run] gap {field} -> {state} for entity {eid}")
                return 0
            resolve_gap(conn, eid, field, state, args.user, args.note)
            print(f"gap {field} -> {state} for entity {eid}")
            return 0
        else:
            for r in _read_batch(Path(args.batch)):
                units.append((
                    r["company"], r["field"].strip(), r["value"].strip(),
                    r["source"].strip(), (r.get("citation") or "").strip() or None,
                    (r.get("note") or "").strip(),
                ))

        written = failed = 0
        for company, field, value, source, citation, _note in units:
            try:
                eid = resolve_entity(conn, company)
                text, num, is_zero = _coerce(field, value)
                if args.dry_run:
                    validate_field_write(
                        conn, eid, field, text, source,
                        citation=citation, value_num=num, is_zero=is_zero,
                    )
                    print(f"[dry-run] ok   EV{eid:04d} {field}={value} ({source})")
                else:
                    write_field(
                        conn, eid, field, text, source, args.user,
                        citation=citation, value_num=num, is_zero=is_zero,
                    )
                    print(f"wrote EV{eid:04d} {field}={value} ({source})")
                written += 1
            except ValidationError as exc:
                print(f"REJECTED {company} {field}={value}: {exc}", file=sys.stderr)
                failed += 1

        verb = "would write" if args.dry_run else "wrote"
        print(f"{verb} {written}, rejected {failed}")
        return 1 if failed else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
