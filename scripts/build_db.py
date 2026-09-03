#!/usr/bin/env python
"""Build the pipeline database from the staging workbook.

    export DATABASE_URL='postgresql://localhost/evpipeline'
    python scripts/build_db.py [--force]

Ported from SQLite with db.py. ``--db`` is gone: the target is DATABASE_URL,
because there is no longer a file to name. That removes the old idempotency
story too -- "rebuild into a fresh file" has no Postgres equivalent -- so
``--force`` now means TRUNCATE the data tables and reload, and without it the
script refuses to run against a database that already holds entities. The
vocabulary tables are deliberately left alone: they are seeded by seed.sql and
re-seeded by ingest, and truncating them would break the FKs pointing at them
mid-load.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evpipeline import connect, create_schema
from evpipeline.ingest import build

# Every table holding loaded data, ordered so the list reads top-down; the
# actual delete order does not matter because one TRUNCATE ... CASCADE
# statement covering all of them defers FK checks to the end of the statement.
DATA_TABLES = [
    "slide_observation_override",
    "slide_observation",
    "meeting_attendee",
    "meeting",
    "money_value",
    "field_value",
    "gap_status",
    "entity_outcome",
    "entity_sourcing",
    "founder",
    "funding_round",
    "entity_group_member",
    "review_item",
    "alias",
    "entity",
    "ingest_run",
]

RAW = Path(__file__).resolve().parents[1] / "data" / "raw"
DRAFT = RAW / "EV_Deal_Pipeline_Clean_Dataset_DRAFT.xlsx"
V2 = RAW / "EV_Deal_Pipeline_Clean_Dataset_v2_DEDUPED.xlsx"
AFFINITY = RAW / "affinity_export_2026-09-01.csv"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--force", action="store_true", help="truncate loaded data and reload"
    )
    args = ap.parse_args()

    missing = [p for p in (DRAFT, AFFINITY) if not p.exists()]
    if missing:
        for p in missing:
            print(f"missing source: {p}", file=sys.stderr)
        return 1

    # V2_DEDUPED only contributes merge *proposals* to the review queue, so a
    # load without it is complete but for those 17 items. It is optional rather
    # than required so the database can be rebuilt from the DRAFT extraction
    # plus the Affinity export alone, which is what enrichment needs.
    v2 = V2 if V2.exists() else None
    if v2 is None:
        print(f"note: {V2.name} absent; v2 merge proposals not imported", file=sys.stderr)

    conn = connect()
    create_schema(conn)

    existing = int(conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0])
    if existing:
        if not args.force:
            print(
                f"database already holds {existing} entities; pass --force to "
                "truncate and reload",
                file=sys.stderr,
            )
            conn.close()
            return 1
        conn.execute(f"TRUNCATE {', '.join(DATA_TABLES)} RESTART IDENTITY CASCADE")
        conn.commit()

    report = build(conn, DRAFT, v2_path=v2, affinity_path=AFFINITY)
    conn.commit()
    conn.close()

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
