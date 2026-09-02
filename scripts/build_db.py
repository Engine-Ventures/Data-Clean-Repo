#!/usr/bin/env python
"""Build the pipeline database from the staging workbook.

    python scripts/build_db.py [--db data/pipeline.db] [--force]

Idempotent by construction: it rebuilds into a fresh file rather than mutating
an existing one, so a load can always be repeated and diffed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evpipeline import connect, create_schema
from evpipeline.ingest import build

RAW = Path(__file__).resolve().parents[1] / "data" / "raw"
DRAFT = RAW / "EV_Deal_Pipeline_Clean_Dataset_DRAFT.xlsx"
V2 = RAW / "EV_Deal_Pipeline_Clean_Dataset_v2_DEDUPED.xlsx"
AFFINITY = RAW / "affinity_export_2026-09-01.csv"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path("data") / "pipeline.db"))
    ap.add_argument("--force", action="store_true", help="overwrite an existing database")
    args = ap.parse_args()

    db_path = Path(args.db)
    if db_path.exists():
        if not args.force:
            print(f"{db_path} exists; pass --force to rebuild", file=sys.stderr)
            return 1
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    missing = [p for p in (DRAFT, V2, AFFINITY) if not p.exists()]
    if missing:
        for p in missing:
            print(f"missing source: {p}", file=sys.stderr)
        return 1

    conn = connect(db_path)
    create_schema(conn)
    report = build(conn, DRAFT, v2_path=V2, affinity_path=AFFINITY)
    conn.close()

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
