#!/usr/bin/env python
"""Generate the local review workbench from the database.

    python scripts/build_ui.py            # -> ui/index.html

Reads data/pipeline.db, inlines the data into ui/template.html and writes
ui/index.html. The data is inlined rather than fetched because the page is
opened over file:// where fetch() is blocked, and because a single portable
file is easier to hand to someone than a directory plus a server.

The output is deliberately not committed: it embeds confidential deal data.

This is a faithful build of the whole database, all 498 extracted rows and all
seven stages. The deliverable the deal team asked for on 2026-09-02 is narrower
-- the advanced-stage diligence cohort only -- so the screen is a second step
that runs over this output rather than a second code path in here:

    python scripts/build_ui.py
    python scripts/screen_diligence.py

Keeping it out of this script means the funnel, coverage and discussion rollups
have exactly one definition (metrics.py, over the full population) and exactly
one place that re-derives them for the cohort (screen_diligence.py).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evpipeline.metrics import (
    GAP_FIELDS,
    coverage_report,
    discussion_counts,
    enrichment_priority,
    funnel_counts,
    gap_counts,
    reconciliation,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DB = REPO_ROOT / "data" / "pipeline.db"
TEMPLATE = REPO_ROOT / "ui" / "template.html"
OUTPUT = REPO_ROOT / "ui" / "index.html"

PLACEHOLDER = '"__PIPELINE_DATA__"'


def collect(conn: sqlite3.Connection) -> dict:
    conn.row_factory = sqlite3.Row

    stages = [
        {"id": r["stage_id"], "name": r["name"]}
        for r in conn.execute("SELECT stage_id, name FROM stage ORDER BY stage_id")
    ]

    meetings = [
        {"date": r["meeting_date"], "status": r["status"], "note": r["note"]}
        for r in conn.execute(
            "SELECT meeting_date, status, note FROM meeting ORDER BY meeting_date"
        )
    ]
    idx_of = {m["date"]: i for i, m in enumerate(meetings)}

    priority = enrichment_priority(conn)
    gaps = gap_counts(conn)

    # Which of the five worklist fields each entity is missing, and the gap
    # state for each, so the worklist can show the three states distinctly.
    have: dict[int, set[str]] = {}
    for r in conn.execute(
        f"""SELECT entity_id, field FROM v_field_current
            WHERE field IN ({",".join("?" * len(GAP_FIELDS))})
              AND (value_text IS NOT NULL OR value_num IS NOT NULL)""",
        GAP_FIELDS,
    ):
        have.setdefault(int(r["entity_id"]), set()).add(r["field"])

    gap_state: dict[int, dict[str, str]] = {}
    for r in conn.execute("SELECT entity_id, field, state FROM gap_status"):
        gap_state.setdefault(int(r["entity_id"]), {})[r["field"]] = r["state"]

    funnel = {
        int(r["entity_id"]): dict(r)
        for r in conn.execute("SELECT * FROM v_entity_funnel")
    }
    discussion = {
        int(r["entity_id"]): dict(r)
        for r in conn.execute("SELECT * FROM v_entity_discussion")
    }
    # Latest observed stage, which the category tabs use for their "where is it
    # now" basis. Distinct from furthest_stage_id: a company can sit in Hold
    # after having reached Deep Diligence.
    latest = {
        int(r["entity_id"]): int(r["latest_stage_id"])
        for r in conn.execute("SELECT entity_id, latest_stage_id FROM v_entity_latest_stage")
    }

    # Observations, compact: [meeting index, stage id, bold] per entity.
    obs: dict[int, list[list[int]]] = {}
    for r in conn.execute(
        "SELECT entity_id, meeting_date, stage_id, is_bold, raw_section, slide_page "
        "FROM v_observation ORDER BY meeting_date"
    ):
        obs.setdefault(int(r["entity_id"]), []).append(
            [idx_of[r["meeting_date"]], int(r["stage_id"]), int(r["is_bold"]), r["slide_page"]]
        )

    # Current field values with provenance, for the detail drawer.
    fields: dict[int, list[list]] = {}
    for r in conn.execute(
        "SELECT entity_id, field, value_text, value_num, is_zero, source, citation, created_by "
        "FROM v_field_current ORDER BY field"
    ):
        val = r["value_text"]
        if val is None and r["value_num"] is not None:
            val = 0 if r["is_zero"] else r["value_num"]
        fields.setdefault(int(r["entity_id"]), []).append(
            [r["field"], val, r["source"], r["citation"], r["created_by"]]
        )

    companies = []
    for r in conn.execute(
        # Live population only: a company merged into another is not a separate
        # company, and a phantom was never one. Without this filter a merge
        # collapses the funnel views but leaves the merged-away row in the
        # list, so the count on screen never moves -- which is the one thing
        # working the review queue is supposed to show.
        #
        # The raw extraction count is still reported as totals.rawEntities, so
        # the page can say "498 extracted, 496 after merges" rather than
        # quietly redefining what 498 meant.
        "SELECT entity_id, canonical_name, domain, is_phantom, phantom_reason, merged_into "
        "FROM entity WHERE merged_into IS NULL AND is_phantom = 0 "
        "ORDER BY canonical_name COLLATE NOCASE"
    ):
        eid = int(r["entity_id"])
        f = funnel.get(eid, {})
        d = discussion.get(eid, {})
        missing = [g for g in GAP_FIELDS if g not in have.get(eid, set())]
        companies.append(
            {
                "id": eid,
                "name": r["canonical_name"],
                "domain": r["domain"],
                "phantom": bool(r["is_phantom"]),
                "phantomReason": r["phantom_reason"],
                "furthest": f.get("furthest_stage_id"),
                "latest": latest.get(eid),
                "appearances": f.get("slide_appearances") or 0,
                "first": f.get("first_slide_date"),
                "last": f.get("last_slide_date"),
                "observedDeep": bool(f.get("observed_at_deep_diligence")),
                "observedLegal": bool(f.get("observed_at_legal")),
                "reachedDeep": bool(f.get("reached_deep_diligence")),
                "discussed": bool(d.get("discussed")),
                "times": d.get("times_discussed") or 0,
                "priority": priority.get(eid),
                "gaps": gaps.get(eid, 0),
                "missing": missing,
                "gapState": gap_state.get(eid, {}),
                "obs": obs.get(eid, []),
                "fields": fields.get(eid, []),
            }
        )

    names = {int(r["entity_id"]): r["canonical_name"] for r in conn.execute(
        "SELECT entity_id, canonical_name FROM entity"
    )}
    review = [
        {
            "id": int(r["review_id"]),
            "kind": r["kind"],
            "entity": r["entity_id"],
            "entityName": names.get(r["entity_id"]),
            "target": r["target_id"],
            "targetName": names.get(r["target_id"]),
            "detail": r["detail"],
            "confidence": r["confidence"],
            "by": r["proposed_by"],
        }
        for r in conn.execute(
            "SELECT * FROM review_item WHERE state = 'open' "
            "ORDER BY confidence DESC NULLS LAST, review_id"
        )
    ]

    aliases: dict[int, list[str]] = {}
    for r in conn.execute(
        "SELECT entity_id, alias_text FROM alias ORDER BY alias_text"
    ):
        aliases.setdefault(int(r["entity_id"]), []).append(r["alias_text"])

    cov = {k: [v.present, v.total] for k, v in coverage_report(conn).items()}
    fc = funnel_counts(conn)
    dc = discussion_counts(conn)

    return {
        "builtAt": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "stages": stages,
        "meetings": meetings,
        "companies": companies,
        "review": review,
        "aliases": aliases,
        "coverage": cov,
        "funnel": fc,
        "discussion": dc,
        "reconciliation": reconciliation(conn),
        "totals": {
            "entities": len(companies),
            "rawEntities": int(
                conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0]
            ),
            "mergedAway": int(
                conn.execute(
                    "SELECT COUNT(*) FROM entity WHERE merged_into IS NOT NULL"
                ).fetchone()[0]
            ),
            "phantoms": int(
                conn.execute(
                    "SELECT COUNT(*) FROM entity WHERE is_phantom = 1"
                ).fetchone()[0]
            ),
            "observations": int(
                conn.execute("SELECT COUNT(*) FROM slide_observation").fetchone()[0]
            ),
            "meetingsHeld": sum(1 for m in meetings if m["status"] == "held"),
            "meetingsMissing": sum(1 for m in meetings if m["status"] != "held"),
            "reviewOpen": len(review),
            "aliasCount": int(conn.execute("SELECT COUNT(*) FROM alias").fetchone()[0]),
        },
    }


def main() -> int:
    if not DB.exists():
        print(f"missing {DB}; run scripts/build_db.py first", file=sys.stderr)
        return 1
    if not TEMPLATE.exists():
        print(f"missing {TEMPLATE}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(DB)
    data = collect(conn)
    conn.close()

    html = TEMPLATE.read_text()
    if PLACEHOLDER not in html:
        print(f"template has no {PLACEHOLDER} placeholder", file=sys.stderr)
        return 1
    payload = json.dumps(data, separators=(",", ":"))
    OUTPUT.write_text(html.replace(PLACEHOLDER, payload))

    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)}  ({OUTPUT.stat().st_size / 1024:.0f} KB)")
    print(f"  {data['totals']['entities']} companies, "
          f"{data['totals']['observations']} observations, "
          f"{data['totals']['reviewOpen']} open review items")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
