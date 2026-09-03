#!/usr/bin/env python
"""Resolve the 185-company diligence cohort onto entity_ids.

    export DATABASE_URL='postgresql://localhost/evpipeline'
    python scripts/load_cohort.py

The cohort is a curated membership list delivered as its own workbook
(DiligenceCompanies_EVPipeline), not a rule over the observation log, so it
cannot be derived: `reached Preliminary Diligence` selects 191 entities in the
built database and is true of only 182 of the cohort's own 185 rows. It is
therefore loaded as a staging table and joined on alias_norm.

Name matching is the error band on every cohort number, so this script reports
it explicitly rather than leaving it implied: how many of the 185 resolved to
exactly one entity, how many resolved to more than one (ambiguous), and how
many resolved to none. A number quoted over the cohort is only as good as the
first of those three.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evpipeline import connect
from evpipeline.ingest import _clean, norm_name

COHORT = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "raw"
    / "DiligenceCompanies_EVPipeline (1).xlsx"
)

DDL = """
CREATE TABLE IF NOT EXISTS diligence_cohort (
    cohort_name text PRIMARY KEY,
    alias_norm  text NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cohort_norm ON diligence_cohort(alias_norm);
"""


def main() -> int:
    if not COHORT.exists():
        print(f"missing cohort workbook: {COHORT}", file=sys.stderr)
        return 1

    companies = pd.read_excel(COHORT, sheet_name="Companies").dropna(how="all")
    names = [n for n in (_clean(v) for v in companies.company_name) if n]

    conn = connect()
    conn.execute(DDL)
    conn.execute("TRUNCATE diligence_cohort")
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO diligence_cohort (cohort_name, alias_norm) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            [(n, norm_name(n)) for n in names],
        )
    conn.commit()

    loaded = int(conn.execute("SELECT COUNT(*) FROM diligence_cohort").fetchone()[0])

    # Match quality, per cohort row: 1 = clean, >1 = ambiguous, 0 = unmatched.
    rows = conn.execute(
        """SELECT dc.cohort_name, COUNT(DISTINCT a.entity_id) AS n
           FROM diligence_cohort dc
           LEFT JOIN alias a ON a.alias_norm = dc.alias_norm
           GROUP BY dc.cohort_name"""
    ).fetchall()
    exact = [r for r in rows if int(r["n"]) == 1]
    ambiguous = [r for r in rows if int(r["n"]) > 1]
    unmatched = [r for r in rows if int(r["n"]) == 0]

    entities = int(
        conn.execute(
            """SELECT COUNT(DISTINCT a.entity_id) FROM alias a
               JOIN diligence_cohort dc ON dc.alias_norm = a.alias_norm"""
        ).fetchone()[0]
    )

    print(f"cohort rows in workbook   : {len(names)}")
    print(f"cohort rows loaded        : {loaded}")
    print(f"  resolved to exactly one : {len(exact)}")
    print(f"  ambiguous (>1 entity)   : {len(ambiguous)}")
    print(f"  unmatched (0 entities)  : {len(unmatched)}")
    print(f"distinct entities selected : {entities}")
    if ambiguous:
        print("\nambiguous:")
        for r in ambiguous:
            print(f"  {r['cohort_name']} -> {r['n']} entities")
    if unmatched:
        print("\nunmatched:")
        for r in unmatched:
            print(f"  {r['cohort_name']}")

    conn.commit()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
