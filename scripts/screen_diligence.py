#!/usr/bin/env python
"""Screen a built interface down to the advanced-stage diligence cohort.

    python scripts/screen_diligence.py [--in ui/index.html] [--out ui/index.html]

The deal team rescoped the deliverable on 2026-09-02 to companies that reached
Preliminary Diligence or beyond; DiligenceCompanies_EVPipeline.xlsx is that
cohort. The interface was built before the rescope, so it still carries the
full 498-entity population and a category tab per stage. This screens the
inlined payload to match the workbook:

  * companies    -> exactly the rows in the workbook's Companies sheet, joined
                    on company_id == 'EV%04d' % entity_id (a deterministic
                    join; 184 of 185 canonical names agree exactly, the one
                    exception being an enrichment rename in the workbook).
  * observations -> stages 1-3 (Meetings This Week, Hold / Nurture,
                    NewCo / Fellows) are dropped outright, so no view can put a
                    screened company in a non-diligence category.
  * stages       -> only 4-7 survive, which empties those three category tabs
                    out of the catbar rather than showing them at zero.

Because slide appearances at a dropped stage are gone, slide_appearances,
first/last seen, latest position and the bold counts are RE-DERIVED from the
surviving observations. They therefore read lower than the same fields in the
workbook, which counts every appearance including pre-diligence weeks. The
divergence is deliberate and reported in the run summary.

Duplicate slide names that the workbook consolidated (its dedup log) are folded
into their surviving row using that row's name_variants_on_slides -- the
workbook's own record of which slide names it absorbed. Nothing is merged on
this script's own judgement; unapproved merge_proposals stay in the review
queue where the pipeline leaves them.

Idempotent: screening an already-screened file changes nothing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import defaultdict
from itertools import pairwise
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKBOOK = REPO_ROOT / "src" / "DiligenceCompanies_EVPipeline (1).xlsx"

# Stage ids from evpipeline.vocab.STAGES. Diligence is 4 and up; 1-3 are
# Meetings This Week, Hold / Nurture and NewCo / Fellows.
MIN_STAGE = 4

PAYLOAD_RE = re.compile(r'(<script id="payload"[^>]*>)(.*?)(</script>)', re.DOTALL)

GAP_FIELDS = ["website", "hq_country", "stage", "round_size_usd", "owner_name"]

# Rows a person has adjudicated, applied after the workbook's canonical name.
# Entity 35 is a real company: the extractor flagged "/Eden Tech" as a
# line-wrap continuation on the strength of its leading slash, the workbook
# kept that spelling, and the deal team confirmed on 2026-09-02 that it is
# Eden Tech. Clearing the flag also retires the line_wrap_candidate that
# raised it. The raw slide spelling survives as an alias.
ADJUDICATED: dict[int, dict[str, object]] = {
    35: {"name": "Eden Tech", "phantom": False, "phantomReason": None},
}


def norm(s: object) -> str:
    s = unicodedata.normalize("NFKD", str(s)).lower().strip()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def workbook_cohort(path: Path) -> tuple[dict[int, str], dict[str, int]]:
    """(entity_id -> workbook canonical name, normalised variant -> entity_id)."""
    co = pd.read_excel(path, sheet_name="Companies")
    cohort: dict[int, str] = {}
    variants: dict[str, int] = {}
    for _, r in co.iterrows():
        eid = int(str(r["company_id"])[2:])
        cohort[eid] = str(r["company_name"]).strip()
        raw = r.get("name_variants_on_slides")
        if isinstance(raw, str):
            for v in re.split(r"[;|]", raw):
                if v.strip():
                    variants[norm(v)] = eid
    return cohort, variants


def screen(payload: dict, cohort: dict[int, str], variants: dict[str, int]) -> tuple[dict, dict]:
    ents = {int(c["id"]): c for c in payload["companies"]}
    aliases = {int(k): list(v) for k, v in payload.get("aliases", {}).items()}

    missing = sorted(set(cohort) - set(ents))

    # Entities the workbook consolidated away: not in the cohort themselves, but
    # named in a cohort row's name_variants_on_slides.
    absorbed: dict[int, int] = {}
    for eid, c in ents.items():
        if eid in cohort:
            continue
        for name in [c["name"], *aliases.get(eid, [])]:
            tgt = variants.get(norm(name))
            if tgt is not None and tgt in cohort:
                absorbed[eid] = tgt
                break

    dropped_diligence = [
        {"id": eid, "name": c["name"], "furthest": c["furthest"], "obs": len(c["obs"])}
        for eid, c in sorted(ents.items())
        if eid not in cohort and eid not in absorbed and (c["furthest"] or 0) >= MIN_STAGE
    ]

    # --- companies -------------------------------------------------------
    kept: list[dict] = []
    renamed: list[dict] = []
    adjudicated: list[dict] = []
    recount: list[dict] = []
    obs_before = obs_after = 0

    extra_obs: dict[int, list[list]] = defaultdict(list)
    for src, tgt in absorbed.items():
        extra_obs[tgt].extend(ents[src]["obs"])

    for eid in sorted(cohort):
        c = dict(ents[eid])
        was_appearances = c.get("appearances") or 0
        obs = [list(o) for o in c["obs"]] + [list(o) for o in extra_obs.get(eid, [])]
        obs_before += len(obs)
        obs = sorted((o for o in obs if o[1] >= MIN_STAGE), key=lambda o: (o[0], o[1]))
        obs_after += len(obs)

        if c["name"] != cohort[eid]:
            renamed.append({"id": eid, "was": c["name"], "now": cohort[eid]})
            c["name"] = cohort[eid]

        fixed = ADJUDICATED.get(eid)
        if fixed:
            adjudicated.append({"id": eid, "was": c["name"], "now": fixed["name"]})
            c.update(fixed)

        # Re-derive everything the dropped observations fed.
        weeks = sorted({o[0] for o in obs})
        stage_by_week = {}
        for wi, stage, *_ in obs:
            stage_by_week[wi] = max(stage, stage_by_week.get(wi, 0))
        dates = [payload["meetings"][w]["date"] for w in weeks]
        bold_weeks = sorted({o[0] for o in obs if o[2]})

        c["obs"] = obs
        c["appearances"] = len(weeks)
        c["first"] = dates[0] if dates else None
        c["last"] = dates[-1] if dates else None
        c["latest"] = stage_by_week[weeks[-1]] if weeks else None
        c["furthest"] = max(stage_by_week.values()) if stage_by_week else None
        c["times"] = len(bold_weeks)
        c["discussed"] = bool(bold_weeks)
        c["observedDeep"] = any(s == 5 for s in stage_by_week.values())
        c["observedLegal"] = any(s == 7 for s in stage_by_week.values())
        c["reachedDeep"] = (c["furthest"] or 0) >= 5
        folded = {a for a in aliases.get(eid, [])}
        for src, tgt in absorbed.items():
            if tgt == eid:
                folded.update([ents[src]["name"], *aliases.get(src, [])])
        aliases[eid] = sorted(folded)

        if c["appearances"] != was_appearances:
            recount.append(
                {"id": eid, "name": c["name"], "appearances_was": was_appearances,
                 "appearances_now": c["appearances"]}
            )
        kept.append(c)

    out = dict(payload)
    out["companies"] = kept
    out["stages"] = [s for s in payload["stages"] if int(s["id"]) >= MIN_STAGE]
    out["aliases"] = {str(eid): aliases[eid] for eid in sorted(cohort) if aliases.get(eid)}
    # Stage jumps and regressions were flagged over the unscreened series, so
    # most of them assert a move through a stage this build no longer carries
    # ("Prelim -> Hold, -2 levels"). Re-derive them over the screened weekly
    # series with ingest's own rule (flag_stage_jumps: delta > 2 or delta < 0)
    # so the queue never points at a transition the trace cannot show.
    DERIVED = {"stage_jump", "stage_regression"}
    # A phantom flag cleared by hand takes its own review item with it.
    retired = [
        r for r in payload["review"]
        if r["kind"] == "line_wrap_candidate" and int(r["entity"]) in ADJUDICATED
    ]
    # A proposal whose other side is not in the file cannot be acted on here.
    # Two reasons it can happen, and they are reported separately: the workbook
    # already executed that merge (its dedup log), or the counterparty was
    # screened out on stage.
    dangling = [
        r for r in payload["review"]
        if int(r["entity"]) in cohort and r["kind"] not in DERIVED
        and r.get("target") is not None and int(r["target"]) not in cohort
    ]
    out["review"] = [
        r for r in payload["review"]
        if int(r["entity"]) in cohort and r["kind"] not in DERIVED
        and (r.get("target") is None or int(r["target"]) in cohort)
        and r not in retired
    ]
    next_id = max((int(r["id"]) for r in out["review"]), default=0) + 1
    dates = [m["date"] for m in payload["meetings"]]
    for c in kept:
        per: dict[int, int] = {}
        for wi, stage, *_ in c["obs"]:
            per[wi] = max(stage, per.get(wi, 0))
        series = sorted(per.items())
        for (w0, s0), (w1, s1) in pairwise(series):
            delta = s1 - s0
            if 0 <= delta <= 2:
                continue
            out["review"].append({
                "id": next_id,
                "kind": "stage_jump" if delta > 0 else "stage_regression",
                "entity": c["id"],
                "entityName": c["name"],
                "target": None,
                "targetName": None,
                "detail": f"stage moved {delta:+d} levels between {dates[w0]} and {dates[w1]}",
                "confidence": None,
                "by": "screen_diligence",
            })
            next_id += 1
    out["review"].sort(key=lambda r: int(r["id"]))

    # --- rollups, all re-derived over the screened cohort -----------------
    def reached(stage: int) -> int:
        return sum(1 for c in kept if (c["furthest"] or 0) >= stage)

    def observed(stage: int) -> int:
        return sum(1 for c in kept if any(o[1] == stage for o in c["obs"]))

    out["funnel"] = {
        "obs_prelim": observed(4), "obs_deep": observed(5),
        "obs_negotiate": observed(6), "obs_legal": observed(7),
        "reached_prelim": reached(4), "reached_deep": reached(5),
        "reached_negotiate": reached(6), "reached_legal": reached(7),
    }
    out["discussion"] = {
        "entities_discussed": sum(1 for c in kept if c["discussed"]),
        "bold_appearances": sum(1 for c in kept for o in c["obs"] if o[2]),
    }

    have = {
        c["id"]: {f for f, *_ in c["fields"]}
        for c in kept
    }
    n = len(kept)
    cov = {f: [sum(1 for c in kept if f in have[c["id"]]), n] for f in GAP_FIELDS}
    deep = [c for c in kept if (c["furthest"] or 0) >= 5]
    disc = [c for c in kept if c["discussed"]]
    cov["hq_country @ deep-diligence cohort"] = [
        sum(1 for c in deep if "hq_country" in have[c["id"]]), len(deep)
    ]
    cov["stage @ discussed cohort"] = [
        sum(1 for c in disc if "stage" in have[c["id"]]), len(disc)
    ]
    out["coverage"] = cov

    out["totals"] = dict(payload["totals"])
    out["totals"].update(
        entities=n,
        observations=sum(len(c["obs"]) for c in kept),
        reviewOpen=len(out["review"]),
        aliasCount=sum(len(v) for v in out["aliases"].values()),
    )
    out["screen"] = {
        "cohort": "advanced-stage diligence only (furthest stage >= Preliminary Diligence)",
        "source": WORKBOOK.name,
        "minStage": MIN_STAGE,
    }

    report = {
        "cohort_rows_in_workbook": len(cohort),
        "companies_kept": n,
        "companies_dropped": len(ents) - n,
        "workbook_rows_with_no_entity": missing,
        "consolidated_into_cohort_rows": [
            {"absorbed": ents[s]["name"], "into": cohort[t]} for s, t in sorted(absorbed.items())
        ],
        "renamed_to_workbook_canonical": renamed,
        "adjudicated_by_hand": adjudicated,
        "review_items_retired_by_adjudication": [r["id"] for r in retired],
        "observations_before": obs_before,
        "observations_after": obs_after,
        "observations_dropped_non_diligence": obs_before - obs_after,
        "appearance_counts_changed": len(recount),
        "review_open": len(out["review"]),
        "proposals_dropped_already_merged_by_workbook": [
            {"proposal": r["id"], "detail": f'{r["entityName"]} -> {r["targetName"]}'}
            for r in dangling if int(r["target"]) in absorbed
        ],
        "proposals_dropped_counterparty_screened_out": [
            {"proposal": r["id"], "detail": f'{r["entityName"]} -> {r["targetName"]}'}
            for r in dangling if int(r["target"]) not in absorbed
        ],
        "dropped_despite_diligence_history": dropped_diligence,
        "funnel": out["funnel"],
    }
    return out, report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", default=str(REPO_ROOT / "ui" / "index.html"))
    ap.add_argument("--out", dest="dst", default=None)
    ap.add_argument("--workbook", default=str(WORKBOOK))
    ap.add_argument(
        "--template",
        default=None,
        help="re-inline the screened payload into this template instead of --in's own markup",
    )
    ap.add_argument("--report", default=None, help="write the run summary here as JSON")
    args = ap.parse_args()

    src = Path(args.src)
    dst = Path(args.dst) if args.dst else src
    if not src.exists():
        print(f"missing interface: {src}", file=sys.stderr)
        return 1

    html = src.read_text()
    m = PAYLOAD_RE.search(html)
    if not m:
        print(f"no inlined payload in {src}", file=sys.stderr)
        return 1
    payload = json.loads(m.group(2))

    cohort, variants = workbook_cohort(Path(args.workbook))
    screened, report = screen(payload, cohort, variants)

    body = json.dumps(screened, separators=(",", ":"))
    if "</script" in body:
        print("payload would break out of its script tag", file=sys.stderr)
        return 1

    if args.template:
        shell = Path(args.template).read_text()
        tm = PAYLOAD_RE.search(shell)
        if not tm:
            print(f"no payload slot in {args.template}", file=sys.stderr)
            return 1
        dst.write_text(shell[: tm.start(2)] + body + shell[tm.end(2) :])
    else:
        dst.write_text(html[: m.start(2)] + body + html[m.end(2) :])

    print(json.dumps(report, indent=2))
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
