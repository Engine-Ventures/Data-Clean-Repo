#!/usr/bin/env python
"""List exactly which companies are missing which enrichment fields.

    python scripts/audit_coverage.py [--out data/coverage_audit.csv]

The "Field Coverage" block on the trends page reports fractions; this reports
the *names behind them*, which is what a person needs in order to go and fill
them in.

Cohort membership and the derived flags (furthest stage, discussed) are read
from the built interface rather than recomputed here, because the page's
coverage denominators are themselves derived by screen_diligence.py over the
screened observation series. Reading its output is what guarantees this audit
and the page agree; recomputing would be a second definition free to drift.
Field *values* are cross-checked against the database, and any disagreement is
reported rather than smoothed over.

Cohorts, in the priority order the deal team asked for:

  1. hq_country over the companies that reached Deep Diligence or later
     -- 16 companies, the smallest and highest-value gap.
  2. stage over the "ever discussed" companies -- 34 companies.
  3. stage over the full diligence cohort -- 185 companies.
  4. hq_country over the full cohort.
  5. website / round_size_usd / owner_name over the full cohort.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

INTERFACE = REPO_ROOT / "ui" / "index.html"
DB = REPO_ROOT / "data" / "pipeline.db"
DEFAULT_OUT = REPO_ROOT / "data" / "coverage_audit.csv"

PAYLOAD_RE = re.compile(r'<script id="payload"[^>]*>(.*?)</script>', re.DOTALL)

# Deep Diligence is stage 5; 6 is Negotiate / Offer, 7 is Legal / Def Docs.
DEEP = 5

# (priority, cohort key, label, field)
COHORTS = [
    (1, "deep", "hq_country @ deep-diligence cohort", "hq_country"),
    (2, "discussed", "stage @ discussed cohort", "stage"),
    (3, "all", "stage", "stage"),
    (4, "all", "hq_country", "hq_country"),
    (5, "all", "website", "website"),
    (6, "all", "round_size_usd", "round_size_usd"),
    (7, "all", "owner_name", "owner_name"),
]


def load_payload(path: Path) -> dict:
    m = PAYLOAD_RE.search(path.read_text())
    if not m:
        raise SystemExit(f"no inlined payload in {path}")
    return json.loads(m.group(1))


def db_values(conn: sqlite3.Connection, fields: set[str]) -> dict[tuple[int, str], object]:
    conn.row_factory = sqlite3.Row
    out: dict[tuple[int, str], object] = {}
    q = ",".join("?" * len(fields))
    for r in conn.execute(
        f"""SELECT entity_id, field, value_text, value_num, is_zero
            FROM v_field_current WHERE field IN ({q})
              AND (value_text IS NOT NULL OR value_num IS NOT NULL)""",
        sorted(fields),
    ):
        val = r["value_text"]
        if val is None and r["value_num"] is not None:
            val = 0 if r["is_zero"] else r["value_num"]
        out[(int(r["entity_id"]), r["field"])] = val
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interface", default=str(INTERFACE))
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    payload = load_payload(Path(args.interface))
    companies = payload["companies"]

    cohorts = {
        "all": companies,
        "deep": [c for c in companies if (c["furthest"] or 0) >= DEEP],
        "discussed": [c for c in companies if c["discussed"]],
    }

    have: dict[int, dict[str, object]] = {
        int(c["id"]): {f[0]: f[1] for f in c["fields"]} for c in companies
    }
    gap_state: dict[int, dict[str, str]] = {
        int(c["id"]): c.get("gapState") or {} for c in companies
    }

    fields = {f for *_, f in COHORTS}
    disagreements: list[str] = []
    dbp = Path(args.db)
    if dbp.exists():
        conn = sqlite3.connect(dbp)
        dbv = db_values(conn, fields)
        conn.close()
        for c in companies:
            eid = int(c["id"])
            for f in sorted(fields):
                page, db = have[eid].get(f), dbv.get((eid, f))
                if (page is None) != (db is None):
                    where = (
                        "set in db, absent on page" if page is None
                        else "on page, absent in db"
                    )
                    disagreements.append(f"EV{eid:04d} {c['name']}: {f} is {where}")
    else:
        disagreements.append(f"database {dbp} absent; page values not cross-checked")

    rows: list[dict[str, object]] = []
    summary: list[tuple[int, str, int, int, int]] = []
    for prio, key, label, field in COHORTS:
        pool = cohorts[key]
        missing = [c for c in pool if have[int(c["id"])].get(field) is None]
        present = len(pool) - len(missing)
        summary.append((prio, label, present, len(pool), len(missing)))
        for c in sorted(missing, key=lambda c: (-(c["furthest"] or 0), c["name"].lower())):
            eid = int(c["id"])
            rows.append({
                "priority": prio,
                "cohort": label,
                "field": field,
                "company_id": f"EV{eid:04d}",
                "entity_id": eid,
                "company_name": c["name"],
                "domain": c["domain"] or "",
                "furthest_stage": c["furthest"] or "",
                "discussed": int(bool(c["discussed"])),
                "times_discussed": c["times"] or 0,
                "appearances": c["appearances"] or 0,
                "first_seen": c["first"] or "",
                "last_seen": c["last"] or "",
                "gap_state": gap_state[eid].get(field, "not_checked"),
                "enrichment_priority": c["priority"] or "",
            })

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["priority"])
        w.writeheader()
        w.writerows(rows)

    print(f"cohort: {payload.get('screen', {}).get('cohort', 'unscreened')}")
    print(f"built:  {payload['builtAt']}\n")
    print(f"{'#':>2}  {'field @ cohort':40} {'have':>9}  {'pct':>6}  {'missing':>7}")
    for prio, label, present, total, nmiss in summary:
        pct = 0.0 if not total else present / total * 100
        print(f"{prio:>2}  {label:40} {present:>4}/{total:<4} {pct:>5.1f}%  {nmiss:>7}")
    print(f"\n{len(rows)} gap rows -> {out.relative_to(REPO_ROOT)}")

    # The two small, high-value cohorts, named inline: they are the Monday ask.
    for prio, key, label, field in COHORTS[:2]:
        miss = [c for c in cohorts[key] if have[int(c["id"])].get(field) is None]
        print(f"\n{label} -- {len(miss)} missing:")
        for c in miss:
            print(f"  EV{int(c['id']):04d}  {c['name']:34} {c['domain'] or '(no domain)'}")

    if disagreements:
        print(f"\n{len(disagreements)} page/database disagreement(s):", file=sys.stderr)
        for d in disagreements[:20]:
            print(f"  {d}", file=sys.stderr)
        return 1

    print("\npage and database agree on every audited field.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
