#!/usr/bin/env python
"""Trends across the advanced-stage diligence cohort.

    python scripts/trends_report.py [--in ui/index.html] [--out ui/trends_report.md]

Seven rollups -- funnel, stage-to-stage conversion, monthly intake, dwell,
round stage, geography and thesis area -- over the diligence cohort and nothing
else. Stages 1-3 (Meetings This Week, Hold / Nurture, NewCo / Fellows) are not
in any numerator and not in any denominator.

POPULATION. The cohort is not recomputed here. `screen_diligence.workbook_cohort`
reads it out of DiligenceCompanies_EVPipeline.xlsx and this script imports that
function, so the two can only ever disagree by the workbook changing underneath
them both. Worth being precise about what that function does, because it is not
a stage query: the cohort is *the 185 rows of the workbook's Companies sheet*,
joined to entities on `company_id == 'EV%04d' % entity_id`. It is not
`furthest_stage_id IN (4,5,6,7)` evaluated over the population. The two coincide
after screening -- every surviving company has furthest >= 4, because screening
drops sub-diligence observations and re-derives furthest from what is left --
but they are different selections over the unscreened 498, which is exactly what
screen_diligence reports as `dropped_despite_diligence_history`. This script
verifies the cohort and the payload agree and refuses to run if they do not.

SOURCE, and it bounds what is here. Everything reads the built interface, not
data/pipeline.db, because the database cannot currently be rebuilt -- two of the
three raw sources build_db.py needs are absent. That is fine for most of this:
the payload carries per-company observations, current field values and their gap
states, so the funnel, conversion, intake, geography and thesis sections are the
same numbers the database would give. Two consequences that are NOT cosmetic:

  * Section 4 re-derives v_stage_transition and v_dwell from the observation
    series rather than reading the views. The logic is theirs (a transition is a
    consecutive pair with a stage change; dwell is meetings at a stage), but it
    is a second implementation and will drift if the views change.
  * Section 5 CANNOT answer the question as asked. `funding_round.round_stage`
    is a database table with no representation in the payload. What is reported
    instead is the `stage` field_value -- Affinity's current round -- which is a
    different source with different coverage. It is labelled as such and must
    not be read as a funding_round rollup.

Deliberately absent, per the brief: sourcing/channel (entity_sourcing has zero
rows) and any round-size/valuation correlation (there is no valuation field in
the schema). Neither is a lookup away; see the notes at the end of the report.

Every count is printed against the denominator it was taken over, because a
cohort of 185 with 74 hq_country values makes "40 in North America" meaningless
on its own.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from itertools import pairwise
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from screen_diligence import WORKBOOK, workbook_cohort  # noqa: E402

PAYLOAD_RE = re.compile(r'(<script id="payload"[^>]*>)(.*?)(</script>)', re.DOTALL)

STAGE_NAME = {
    4: "Preliminary Diligence",
    5: "Deep Diligence",
    6: "Negotiate / Offer",
    7: "Legal Diligence / Def Docs",
}
STAGES = (4, 5, 6, 7)

# Intake is charted from October, the first meeting in the series.
INTAKE_FROM = "2025-10"

THESIS_ORDER = ["Human Health", "Autonomous Systems", "Energy & Climate", "AI for Science"]
ROUND_ORDER = ["Preseed", "Seed", "Series A", "Series B", "Series B+"]


def pct(n: int, d: int) -> str:
    return "n/a" if not d else f"{n / d:.0%}"


def of(n: int, d: int, what: str = "cohort companies") -> str:
    """The house format: never a bare count."""
    return f"{n} of {d} {what} ({pct(n, d)})"


def load_payload(path: Path) -> dict:
    m = PAYLOAD_RE.search(path.read_text())
    if not m:
        raise SystemExit(f"no inlined payload in {path}")
    return json.loads(m.group(2))


def field_map(company: dict) -> dict[str, object]:
    """Current field values for one company, name -> value."""
    return {row[0]: row[1] for row in company["fields"]}


class Cohort:
    """The cohort with the per-company series each section reads."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.dates = [m["date"] for m in payload["meetings"]]
        self.companies = payload["companies"]
        self.n = len(self.companies)

        # Week index -> date, and the per-week stage each company was shown at.
        # A company can appear twice in one week under two sub-sections; the
        # deeper stage wins, matching screen_diligence's own re-derivation.
        self.series: dict[int, list[tuple[str, int]]] = {}
        for c in self.companies:
            per: dict[int, int] = {}
            for wi, stage, *_ in c["obs"]:
                per[wi] = max(stage, per.get(wi, 0))
            self.series[c["id"]] = [(self.dates[w], s) for w, s in sorted(per.items())]

    def reached(self, stage: int) -> list[dict]:
        return [c for c in self.companies if (c["furthest"] or 0) >= stage]

    def latest_at(self, stage: int) -> list[dict]:
        return [c for c in self.companies if c["latest"] == stage]

    def first_at_or_above(self, cid: int, stage: int) -> str | None:
        for d, s in self.series[cid]:
            if s >= stage:
                return d
        return None


def check_population(cohort: dict[int, str], payload: dict) -> list[str]:
    """Every way the workbook cohort and the payload can disagree."""
    ids = {int(c["id"]) for c in payload["companies"]}
    problems = []
    if missing := sorted(set(cohort) - ids):
        problems.append(f"in the workbook, absent from the payload: {missing}")
    if extra := sorted(ids - set(cohort)):
        problems.append(f"in the payload, absent from the workbook: {extra}")
    off = sorted(c["id"] for c in payload["companies"] if (c["furthest"] or 0) < 4)
    if off:
        problems.append(f"payload companies below stage 4: {off}")
    return problems


# --- sections -------------------------------------------------------------
# Each returns a list of markdown lines. The printed table and the file are the
# same text, so the two can never say different things.


def s1_funnel(co: Cohort) -> list[str]:
    out = [
        "## 1. Funnel", "",
        ("Two different questions, kept apart per the README's `observed_at_*` vs "
        "`reached_*` distinction."),
        "",
        ("**Currently at each stage** — `latest_stage_id`, the stage of the most "
        "recent slide appearance. Sums to the cohort."),
        "",
        "| Stage | Currently at |",
        "| --- | --- |",
    ]
    for s in STAGES:
        out.append(f"| {s}. {STAGE_NAME[s]} | {of(len(co.latest_at(s)), co.n)} |")

    out += [
        "",
        ("**Ever reached each stage** — `furthest_stage_id >= stage`. Cumulative, so "
        "these do not sum; every company reached stage 4 by construction."),
        "",
        "| Stage | Ever reached |",
        "| --- | --- |",
    ]
    for s in STAGES:
        out.append(f"| {s}. {STAGE_NAME[s]} | {of(len(co.reached(s)), co.n)} |")
    return out


def s2_conversion(co: Cohort) -> list[str]:
    out = [
        "## 2. Stage-to-stage conversion", "",
        ("Of everyone who reached a stage, the share that also reached the next. "
        "`reached_*` semantics throughout; the denominator is the prior stage's "
        "reached count, not the cohort."),
        "",
        "| Transition | Converted |",
        "| --- | --- |",
    ]
    for a, b in pairwise(STAGES):
        num, den = len(co.reached(b)), len(co.reached(a))
        out.append(
            f"| {STAGE_NAME[a]} → {STAGE_NAME[b]} | "
            f"{num} of {den} who reached {STAGE_NAME[a]} ({pct(num, den)}) |"
        )
    end_to_end = len(co.reached(7))
    out += [
        "",
        (f"End to end, Preliminary Diligence → Legal Diligence: "
        f"{of(end_to_end, len(co.reached(4)), 'who reached Preliminary Diligence')}."),
    ]
    return out


def s3_intake(co: Cohort) -> list[str]:
    by_month: Counter = Counter()
    undated = 0
    for c in co.companies:
        d = co.first_at_or_above(c["id"], 4)
        if d is None:
            undated += 1
            continue
        by_month[d[:7]] += 1

    out = [
        "## 3. Monthly intake", "",
        ("Cohort companies by the month they **first reached** Preliminary Diligence "
        "(`reached_*` semantics — the earliest slide appearance at stage 4 or "
        "deeper). This is the intake trend to chart."),
        "",
        f"Dated: {of(co.n - undated, co.n)}."
        + (f" {undated} have no stage-4 appearance and are excluded." if undated else ""),
        "",
        "| Month | First reached Prelim Diligence |",
        "| --- | --- |",
    ]
    months = sorted(m for m in by_month if m >= INTAKE_FROM)
    for m in months:
        n = by_month[m]
        bar = "█" * n
        out.append(f"| {m} | {n} of {co.n} ({pct(n, co.n)}) {bar} |")
    if early := sorted(m for m in by_month if m < INTAKE_FROM):
        out += ["", f"Before {INTAKE_FROM}, excluded: {', '.join(early)}."]
    return out


def s4_dwell(co: Cohort, today: date) -> list[str]:
    # v_stage_transition: consecutive observations where the stage changed.
    gaps: list[int] = []
    movers = 0
    for c in co.companies:
        moved = False
        for (d0, s0), (d1, s1) in pairwise(co.series[c["id"]]):
            if s0 == s1:
                continue
            moved = True
            gaps.append((datetime.fromisoformat(d1) - datetime.fromisoformat(d0)).days)
        movers += moved

    avg = sum(gaps) / len(gaps) if gaps else 0

    out = [
        "## 4. Time in stage", "",
        ("_Re-derived from the observation series, not read from `v_stage_transition` "
        "and `v_dwell` — the database is not currently buildable. Same rule as the "
        "views: a transition is a consecutive pair of appearances whose stage "
        "differs; dwell is meetings at a stage._"),
        "",
        "**Between consecutive stage transitions**",
        "",
        (f"- Transitions observed: **{len(gaps)}**, across "
        f"{of(movers, co.n, 'cohort companies')} that moved stage at least once."),
        f"- Average gap: **{avg:.0f} days**"
        + (f" (median {sorted(gaps)[len(gaps) // 2]}, "
           f"range {min(gaps)} to {max(gaps)})" if gaps else ""),
        (f"- The other {of(co.n - movers, co.n)} were never seen at more than one stage, "
        "so they contribute no transition."),
        "",
        ("**Longest current dwell** — days since the last slide appearance, for "
        "companies whose latest stage has not changed since they entered it. "
        f"Today is {today.isoformat()}; the series ends {co.dates[-1]}."),
        "",
        "| Company | Stage | Entered | Last seen | Days since last seen | Meetings at stage |",
        "| --- | --- | --- | --- | --- | --- |",
    ]

    rows = []
    for c in co.companies:
        series = co.series[c["id"]]
        if not series or c["latest"] is None:
            continue
        # Start of the trailing run at the latest stage: the dwell only counts if
        # the stage has not changed since, which is what the run boundary means.
        entered = series[-1][0]
        for d, s in reversed(series):
            if s != c["latest"]:
                break
            entered = d
        last = series[-1][0]
        at_stage = sum(1 for _, s in series if s == c["latest"])
        rows.append((
            (today - date.fromisoformat(last)).days,
            c["name"], c["latest"], entered, last, at_stage,
        ))

    rows.sort(key=lambda r: (-r[0], r[1]))
    for days, name, stage, entered, last, at_stage in rows[:10]:
        out.append(
            f"| {name} | {stage}. {STAGE_NAME[stage]} | {entered} | {last} | "
            f"{days} | {at_stage} |"
        )
    out += ["", f"Ranked over {of(len(rows), co.n)} with at least one appearance."]
    return out


def s5_round(co: Cohort) -> list[str]:
    counts: Counter = Counter()
    for c in co.companies:
        v = field_map(c).get("stage")
        if v:
            counts[str(v)] += 1
    covered = sum(counts.values())

    out = [
        "## 5. Round stage", "",
        ("> **This is not `funding_round.round_stage`.** That table lives only in "
        "`data/pipeline.db`, which cannot currently be built, and it has no "
        "representation in the interface payload. What follows is the `stage` "
        "field_value — Affinity's *current* round for the organisation — which is "
        "a different source, a single value rather than a round history, and has "
        "its own coverage. Do not report it as a funding_round rollup. The "
        "funding_round split is unanswerable until the database is rebuilt."),
        "",
        (f"Coverage: {of(covered, co.n)} have a `stage` value; "
        f"{of(co.n - covered, co.n)} have none."),
        "",
        "| Round | Count | Share of cohort | Share of those with a value |",
        "| --- | --- | --- | --- |",
    ]
    order = [r for r in ROUND_ORDER if r in counts] + sorted(set(counts) - set(ROUND_ORDER))
    for r in order:
        n = counts[r]
        out.append(f"| {r} | {n} | {n} of {co.n} ({pct(n, co.n)}) | {pct(n, covered)} |")
    out.append(
        f"| _no value_ | {co.n - covered} | "
        f"{co.n - covered} of {co.n} ({pct(co.n - covered, co.n)}) | — |"
    )

    # The brief groups Series B and beyond; the data currently stops at Series B.
    b_plus = sum(n for r, n in counts.items() if r.startswith("Series B")
                 or r.startswith("Series C") or r.startswith("Series D"))
    seed = counts.get("Seed", 0)
    a = counts.get("Series A", 0)
    out += [
        "",
        ("Grouped as asked — Seed vs Series A vs Series B+ "
        "(Preseed shown separately, it is not Seed):"),
        "",
        f"- Seed: {of(seed, co.n)} — {pct(seed, covered)} of those with a value",
        f"- Series A: {of(a, co.n)} — {pct(a, covered)} of those with a value",
        f"- Series B+: {of(b_plus, co.n)} — {pct(b_plus, covered)} of those with a value",
        f"- Preseed: {of(counts.get('Preseed', 0), co.n)}",
    ]
    return out


def s6_geography(co: Cohort) -> list[str]:
    out = ["## 6. Geography", ""]

    for field, label in (("hq_region", "Region"), ("hq_country", "Country")):
        counts: Counter = Counter()
        for c in co.companies:
            v = field_map(c).get(field)
            if v:
                counts[str(v)] += 1
        covered = sum(counts.values())

        # Gap state distinguishes "nobody looked" from "looked, nothing to find".
        # Only the five GAP_FIELDS carry one, so hq_region has none at all and
        # its blanks cannot be split -- say so rather than reporting a zero.
        states: Counter = Counter()
        for c in co.companies:
            if field in field_map(c):
                continue
            states[c["gapState"].get(field, "untracked")] += 1
        tracked = set(states) != {"untracked"}

        out += [
            f"### {label} (`{field}`)", "",
            f"Coverage: {of(covered, co.n)} have a current value.",
            "",
            f"| {label} | Count | Share of cohort | Share of those with a value |",
            "| --- | --- | --- | --- |",
        ]
        for k, n in counts.most_common():
            out.append(f"| {k} | {n} | {n} of {co.n} ({pct(n, co.n)}) | {pct(n, covered)} |")
        blanks = co.n - covered
        out += ["", f"Of the {blanks} with no value: " + (
            ", ".join(f"{n} `{s}`" for s, n in states.most_common()) + "."
            if tracked else
            f"`{field}` is not one of the five tracked gap fields, so none carry a "
            "gap state — a blank here cannot be told apart from an unchecked one. "
            f"`hq_country` is tracked and has the same {covered} covered, so use "
            "its split as the proxy."), ""]
    return out


def s7_thesis(co: Cohort) -> list[str]:
    thesis: dict[int, str] = {}
    for c in co.companies:
        v = field_map(c).get("thesis_area")
        if v:
            thesis[c["id"]] = str(v)
    covered = len(thesis)

    counts = Counter(thesis.values())
    out = [
        "## 7. Thesis area", "",
        f"Coverage: {of(covered, co.n)} have a `thesis_area` value.",
        "",
        "| Thesis area | Count | Share of cohort | Share of those with a value |",
        "| --- | --- | --- | --- |",
    ]
    order = [t for t in THESIS_ORDER if t in counts] + sorted(set(counts) - set(THESIS_ORDER))
    for t in order:
        n = counts[t]
        out.append(f"| {t} | {n} | {n} of {co.n} ({pct(n, co.n)}) | {pct(n, covered)} |")

    # Cross-tab on furthest stage: does one thesis area run deeper than another?
    grid: dict[str, Counter] = defaultdict(Counter)
    for c in co.companies:
        t = thesis.get(c["id"])
        if t:
            grid[t][c["furthest"] or 0] += 1

    out += [
        "",
        "### Thesis area x furthest stage", "",
        ("Row denominator is that thesis area's own count, so the percentages answer "
        "\"how deep does this area get?\" rather than \"how big is it?\". Counts are "
        "`furthest_stage_id`, so a company appears in exactly one column."),
        "",
        "| Thesis area | " + " | ".join(f"{s}. {STAGE_NAME[s]}" for s in STAGES)
        + " | Reached Deep+ |",
        "| --- | " + " | ".join("---" for _ in STAGES) + " | --- |",
    ]
    for t in order:
        row = grid[t]
        tot = sum(row.values())
        cells = " | ".join(f"{row.get(s, 0)} ({pct(row.get(s, 0), tot)})" for s in STAGES)
        deep = sum(n for s, n in row.items() if s >= 5)
        out.append(f"| {t} | {cells} | {deep} of {tot} ({pct(deep, tot)}) |")

    all_deep = len(co.reached(5))
    out += [
        "",
        (f"Cohort baseline for comparison: {of(all_deep, co.n)} reached Deep Diligence "
        "or beyond. An area above that line is running deeper than the cohort average."),
        "",
        (f"The {co.n - covered} companies with no thesis area are excluded from the "
        "cross-tab, not bucketed as unknown."),
    ]
    return out


def notes() -> list[str]:
    return [
        "## Not in this report", "",
        ("- **Sourcing / channel.** `entity_sourcing` has zero rows; there is nothing "
        "to group by. Not added."),
        ("- **Round size / valuation correlation.** There is no valuation field "
        "anywhere in `schema.sql`. `round_size_usd` exists as a field_value, so a "
        "round-size distribution is possible, but a *correlation* against valuation "
        "is not. Not added — flagged as a question instead."),
        ("- **`funding_round.round_stage`.** Section 5 substitutes a different "
        "source; see the warning there."),
    ]


def build(co: Cohort, today: date, src: Path) -> str:
    p = co.payload
    head = [
        "# Diligence cohort — trends", "",
        (f"Cohort: **{co.n} companies** — {p['screen']['cohort']}, "
        f"per `{WORKBOOK.name}`."),
        "Stages 1-3 are excluded from every numerator and every denominator below.",
        "",
        f"- Source: `{src.relative_to(REPO_ROOT)}`, built {p['builtAt']}",
        f"- Slide series: {len(co.dates)} meetings, {co.dates[0]} → {co.dates[-1]}",
        f"- Report generated: {today.isoformat()}",
        "",
        "Every count carries the denominator it was taken over.",
        "",
    ]
    parts = [
        head,
        s1_funnel(co), s2_conversion(co), s3_intake(co), s4_dwell(co, today),
        s5_round(co), s6_geography(co), s7_thesis(co), notes(),
    ]
    return "\n".join("\n".join(part) + "\n" for part in parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="src", type=Path, default=REPO_ROOT / "ui" / "index.html")
    ap.add_argument("--out", dest="dst", type=Path,
                    default=REPO_ROOT / "ui" / "trends_report.md")
    ap.add_argument("--workbook", type=Path, default=WORKBOOK)
    ap.add_argument("--today", type=date.fromisoformat, default=date.today())
    args = ap.parse_args()

    if not args.src.exists():
        print(f"missing {args.src}; run scripts/build_ui.py first", file=sys.stderr)
        return 1
    if not args.workbook.exists():
        print(f"missing {args.workbook}", file=sys.stderr)
        return 1

    payload = load_payload(args.src)
    if "screen" not in payload:
        print(f"{args.src} is not screened; run scripts/screen_diligence.py first",
              file=sys.stderr)
        return 1

    cohort, _ = workbook_cohort(args.workbook)
    problems = check_population(cohort, payload)
    print(f"cohort: {len(cohort)} rows in {args.workbook.name}")
    print(f"payload: {len(payload['companies'])} companies")
    if problems:
        print("POPULATION MISMATCH — refusing to report:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    print("cohort and payload agree exactly\n")

    co = Cohort(payload)
    report = build(co, args.today, args.src)
    args.dst.write_text(report)

    print(report)
    print(f"wrote {args.dst.relative_to(REPO_ROOT)}  ({args.dst.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
