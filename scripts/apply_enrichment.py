#!/usr/bin/env python
"""Apply a proposals CSV through the validated write path, and log every write.

    python scripts/apply_enrichment.py data/enrichment_proposals_affinity.csv \
        --user you [--dry-run]

Every value goes through `evpipeline.validate.write_field` -- the same
function the CLI's --batch mode and any future browser path call. Nothing here
touches field_value directly, so the §8 rules, the append-only supersede and
the provenance columns all apply exactly as they would to a hand write.

The point of this script over a bare `--batch` is the audit trail: it reads
the current value *before* writing, so data/enrichment_log.csv records
old -> new per field rather than just the new value. A rejected write is
logged as a rejection with the validator's own message, so a value the rules
refused is visible rather than absent.

Proposals CSV columns: company, field, value, source, and optional
citation / note. That is the same shape `--batch` consumes, so a file can be
handed to either.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from evpipeline import connect
from evpipeline.validate import (
    ValidationError,
    _coerce,
    resolve_entity,
    validate_field_write,
    write_field,
)

DB = REPO_ROOT / "data" / "pipeline.db"
LOG = REPO_ROOT / "data" / "enrichment_log.csv"

LOG_COLUMNS = [
    "logged_at",
    "company_id",
    "entity_id",
    "company_name",
    "field",
    "old_value",
    "new_value",
    "source",
    "citation",
    "evidence",
    "outcome",
    "user",
]


def current_value(conn, entity_id: int, field: str) -> object | None:
    row = conn.execute(
        "SELECT value_text, value_num, is_zero FROM v_field_current "
        "WHERE entity_id = ? AND field = ?",
        (entity_id, field),
    ).fetchone()
    if row is None:
        return None
    if row["value_text"] is not None:
        return row["value_text"]
    if row["value_num"] is not None:
        return 0 if row["is_zero"] else row["value_num"]
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("proposals", help="CSV of company,field,value,source[,citation,note]")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--log", default=str(LOG))
    ap.add_argument("--user", required=True, help="recorded as created_by on every write")
    ap.add_argument("--dry-run", action="store_true", help="validate without writing")
    args = ap.parse_args()

    src = Path(args.proposals)
    if not src.exists():
        print(f"missing proposals: {src}", file=sys.stderr)
        return 2
    dbp = Path(args.db)
    if not dbp.exists():
        print(f"missing database: {dbp}; run scripts/build_db.py first", file=sys.stderr)
        return 2

    with open(src, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))

    conn = connect(dbp)
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    entries: list[dict[str, object]] = []
    written = rejected = unchanged = 0

    for i, r in enumerate(rows, start=2):
        company = (r.get("company") or "").strip()
        field = (r.get("field") or "").strip()
        value = (r.get("value") or "").strip()
        source = (r.get("source") or "").strip()
        citation = (r.get("citation") or "").strip() or None
        evidence = (r.get("note") or "").strip()

        entry: dict[str, object] = {
            "logged_at": now,
            "company_id": company,
            "entity_id": "",
            "company_name": "",
            "field": field,
            "old_value": "",
            "new_value": value,
            "source": source,
            "citation": citation or "",
            "evidence": evidence,
            "outcome": "",
            "user": args.user,
        }

        try:
            eid = resolve_entity(conn, company)
            entry["entity_id"] = eid
            entry["company_id"] = f"EV{eid:04d}"
            entry["company_name"] = conn.execute(
                "SELECT canonical_name FROM entity WHERE entity_id = ?", (eid,)
            ).fetchone()[0]

            old = current_value(conn, eid, field)
            entry["old_value"] = "" if old is None else old

            text, num, is_zero = _coerce(field, value)
            new_repr = text if text is not None else num

            # An identical value would still append a superseding row, which
            # is noise in the provenance history rather than a correction.
            if old is not None and str(old) == str(new_repr):
                entry["outcome"] = "unchanged"
                unchanged += 1
                entries.append(entry)
                continue

            if args.dry_run:
                validate_field_write(
                    conn, eid, field, text, source,
                    citation=citation, value_num=num, is_zero=is_zero,
                )
                entry["outcome"] = "would_write"
            else:
                write_field(
                    conn, eid, field, text, source, args.user,
                    citation=citation, value_num=num, is_zero=is_zero,
                )
                entry["outcome"] = "written"
            written += 1
        except ValidationError as exc:
            entry["outcome"] = f"rejected: {exc}"
            rejected += 1
            print(f"{src.name}:{i} REJECTED {company} {field}={value}: {exc}", file=sys.stderr)
        entries.append(entry)

    conn.close()

    # The log is append-only across runs: it is the audit record of what was
    # filled and from where, so a later run must not erase an earlier one.
    log = Path(args.log)
    log.parent.mkdir(parents=True, exist_ok=True)
    fresh = not log.exists()
    if not args.dry_run:
        with open(log, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=LOG_COLUMNS)
            if fresh:
                w.writeheader()
            w.writerows(entries)

    verb = "would write" if args.dry_run else "wrote"
    print(f"{verb} {written}, unchanged {unchanged}, rejected {rejected} (of {len(rows)} rows)")
    if not args.dry_run:
        print(f"logged {len(entries)} entries -> {log.relative_to(REPO_ROOT)}")
    return 1 if rejected else 0


if __name__ == "__main__":
    raise SystemExit(main())
