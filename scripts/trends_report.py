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
        ("**Stalled — needs attention before Monday.** These ten have gone longest "
        "without appearing on a slide, and have not moved stage since they entered "
        "the one they are in. Each is a live diligence deal that nobody has "
        "reported on; the action is to either advance it, pass on it, or say why "
        "it is still open. "
        f"Today is {today.isoformat()}; the series ends {co.dates[-1]}, so "
        f"{(today - date.fromisoformat(co.dates[-1])).days} days of the count below "
        "is simply the gap since the last meeting."),
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


# --- A-G: advancement splits, signal, process health -----------------------
# Everything below cuts the cohort the same way, so the split is defined once.
# Deep+ is furthest_stage_id >= 5; Prelim-only is furthest_stage_id == 4. Both
# are `reached_*` semantics -- where a company got to, not where it sits now.

SMALL_SAMPLE = 20


def split(co: Cohort) -> tuple[list[dict], list[dict]]:
    return co.reached(5), [c for c in co.companies if (c["furthest"] or 0) == 4]


def small_sample_note(deep: list[dict], prelim: list[dict]) -> list[str]:
    """Said before any percentage, not after it."""
    if len(deep) >= SMALL_SAMPLE:
        return []
    return [
        (f"> **Read the Deep+ percentages with care.** That group is {len(deep)} "
         f"companies against {len(prelim)} at Preliminary Diligence only. At "
         f"n={len(deep)}, one company moves a share by "
         f"{1 / len(deep):.0%}, so a gap between the two columns is not a signal "
         "on its own. The counts are exact; the percentages are fragile."),
        "",
    ]


def low_coverage_note(covered: int, total: int, what: str) -> list[str]:
    """Directly above the table it applies to, never in a footnote."""
    if not total or covered / total >= 0.30:
        return []
    return [
        (f"> **Coverage is {pct(covered, total)}** — {what} is recorded for only "
         f"{covered} of {total} here. The distribution below describes those "
         f"{covered}, and cannot be extrapolated to the rest."),
        "",
    ]


def split_table(co: Cohort, field: str, label: str, order: list[str] | None = None,
                value_of=None) -> list[str]:
    """One distribution, Deep+ beside Prelim-only, each with its own coverage.

    Every value seen on either side gets a row on both sides, so a category
    present in one group and absent from the other reads as an explicit 0
    rather than a missing line.
    """
    deep, prelim = split(co)
    pick = value_of or (lambda c: field_map(c).get(field))

    def counts(group):
        c: Counter = Counter()
        for e in group:
            v = pick(e)
            if v not in (None, ""):
                c[str(v)] += 1
        return c

    dc, pc = counts(deep), counts(prelim)
    d_cov, p_cov = sum(dc.values()), sum(pc.values())

    keys = set(dc) | set(pc)
    ordered = [k for k in (order or []) if k in keys] + sorted(keys - set(order or []))

    out = small_sample_note(deep, prelim)
    out += low_coverage_note(d_cov + p_cov, co.n, f"`{field}`")
    out += [
        (f"Coverage is reported per group, not pooled: "
         f"**Deep+ {of(d_cov, len(deep), 'Deep+ companies')}**, "
         f"**Prelim-only {of(p_cov, len(prelim), 'Prelim-only companies')}**."),
        "",
        (f"| {label} | Deep+ (n={len(deep)}) | share of Deep+ with a value | "
        f"Prelim-only (n={len(prelim)}) | share of Prelim-only with a value |"),
        "| --- | --- | --- | --- | --- |",
    ]
    for k in ordered:
        out.append(
            f"| {k} | {dc.get(k, 0)} | {pct(dc.get(k, 0), d_cov)} | "
            f"{pc.get(k, 0)} | {pct(pc.get(k, 0), p_cov)} |"
        )
    out.append(
        f"| _no value_ | {len(deep) - d_cov} | — | {len(prelim) - p_cov} | — |"
    )
    return [*out, ""]


def sa_advancement(co: Cohort) -> list[str]:
    deep, prelim = split(co)
    out = [
        "## A. Advancement splits", "",
        (f"Every distribution below is cut two ways: **Deep+** — reached Deep "
         f"Diligence or further, `furthest_stage_id >= 5`, {len(deep)} companies — "
         f"against **Prelim-only** — `furthest_stage_id == 4`, {len(prelim)} "
         "companies. Both are `reached_*` semantics. The two groups' coverage rates "
         "are computed and reported separately; neither is applied to the other."),
        "",
        "### A1. Region (`hq_region`)", "",
    ]
    out += split_table(co, "hq_region", "Region")
    out += ["### A2. Country (`hq_country`)", ""]
    out += split_table(co, "hq_country", "Country")
    out += [
        "### A3. Round stage (`stage`)", "",
        ("> **Still not `funding_round.round_stage`.** As in section 5, this is the "
         "`stage` field_value — Affinity's current round — because `funding_round` "
         "lives only in `data/pipeline.db`, which still cannot be built. Do not "
         "read this as a funding_round split."),
        "",
    ]
    out += split_table(co, "stage", "Round", ROUND_ORDER)
    return out


def sb_signal(co: Cohort) -> list[str]:
    out = [
        "## B. Internal signal vs advancement", "",
        ("Do the team's own scores and Affinity's Interest flag track with getting "
         "deeper? Same Deep+ / Prelim-only split as section A. This is a two-group "
         "comparison to read in a room, **not** a regression: no correlation "
         "coefficient is computed and no significance is claimed. At these coverage "
         "levels none of it would survive one anyway."),
        "",
    ]
    fields = (
        ("interest", "Interest", ["1. Very High", "2. High", "3. Moderate",
                                  "4. Low", "5. Propose Pass"]),
        ("score_team", "Team score", ["+++", "++", "+"]),
        ("score_tech", "Tech score", ["+++", "++", "+"]),
        ("score_oppt", "Opportunity score", ["+++", "++", "+"]),
    )
    for i, (field, label, order) in enumerate(fields, start=1):
        out += [f"### B{i}. {label} (`{field}`)", ""]
        out += split_table(co, field, label, order)
    return out


# Affinity statuses that assert the deal is over, and those that assert it never
# really started. Either is a contradiction when the slides still show diligence.
CLOSED_STATUS = {"Pass", "Loss"}
EARLY_STATUS = {"Sourcing - No Outreach", "Pre-Screen", "Initial Evaluation"}

# How close to the end of the slide series a company must have appeared for a
# closed Affinity status to read as a live contradiction rather than history.
RECENT_DAYS = 56


def sc_status(co: Cohort) -> list[str]:
    have = [(c, str(field_map(c).get("affinity_status")))
            for c in co.companies if field_map(c).get("affinity_status")]
    covered = len(have)

    grid: dict[str, Counter] = defaultdict(Counter)
    for c, s in have:
        grid[s][c["furthest"] or 0] += 1

    out = [
        "## C. Affinity Status vs slide-derived stage", "",
        ("A process-health check, not a data question: Affinity's `Status` is "
         "maintained by hand, the slide stage is derived from what was presented. "
         "Where they contradict each other, one of the two is out of date. Stage "
         "columns are `furthest_stage_id` (`reached_*`)."),
        "",
    ]
    out += low_coverage_note(covered, co.n, "`affinity_status`")
    out += [
        f"Coverage: {of(covered, co.n)} have an Affinity Status.",
        "",
        "| Affinity Status | " + " | ".join(f"{s}. {STAGE_NAME[s]}" for s in STAGES)
        + " | Total |",
        "| --- | " + " | ".join("---" for _ in STAGES) + " | --- |",
    ]
    for s in sorted(grid, key=lambda k: -sum(grid[k].values())):
        row = grid[s]
        tot = sum(row.values())
        cells = " | ".join(str(row.get(st, 0)) for st in STAGES)
        out.append(f"| {s} | {cells} | {tot} |")
    out += [
        "| _no status_ | " + " | ".join(
            str(sum(1 for c in co.companies
                    if not field_map(c).get("affinity_status")
                    and (c["furthest"] or 0) == st)) for st in STAGES)
        + f" | {co.n - covered} |",
        "",
    ]

    # --- the action list ---
    # "Pass" against a company last seen ten months ago is not a contradiction --
    # it is a pass that happened. What needs a person is a closed status on a deal
    # the slides still show moving: recently on a slide, or deep in the funnel.
    # Both tiers are listed, because the boundary is a judgement, but the count
    # that leads the section is the one worth acting on.
    end = date.fromisoformat(co.dates[-1])
    closed = [(c, s) for c, s in have if s in CLOSED_STATUS]
    early = [(c, s) for c, s in have if s in EARLY_STATUS and (c["furthest"] or 0) >= 5]

    def why(c) -> str:
        deep = (c["furthest"] or 0) >= 5
        recent = c["last"] and (end - date.fromisoformat(c["last"])).days <= RECENT_DAYS
        if deep and recent:
            return "**still active, and past Prelim**"
        if deep:
            return "**reached Deep+ after being closed**"
        if recent:
            return f"**still on slides within {RECENT_DAYS} days of the last meeting**"
        return "consistent — closed, and quiet since"

    live = [(c, s) for c, s in closed if not why(c).startswith("consistent")]

    out += [
        "### C1. Contradictions to resolve", "",
        (f"{len(closed)} cohort companies carry "
         f"{' or '.join(sorted(CLOSED_STATUS))} in Affinity. Most of those are not "
         "contradictions — a company that was passed on and has not been presented "
         "since is a record working correctly, so listing all 46 as actions would "
         "bury the real ones."),
        "",
        (f"**{len(live)} need a person.** Either they reached Deep Diligence or "
         f"beyond *after* being closed, or they were still appearing on slides "
         f"within {RECENT_DAYS} days of the last meeting ({co.dates[-1]}) — in both "
         "cases Affinity says the deal is dead and the slides say it is not."),
        "",
    ]
    if closed:
        out += ["| Company | Affinity Status | Furthest stage | Last seen | Why flagged |",
                "| --- | --- | --- | --- | --- |"]
        # Genuine contradictions first; within each, deepest and most recent first.
        for c, s in sorted(closed, key=lambda x: (
                why(x[0]).startswith("consistent"),
                -(x[0]["furthest"] or 0),
                x[0]["last"] or "",
        ), reverse=False):
            st = c["furthest"] or 0
            out.append(f"| {c['name']} | {s} | {st}. {STAGE_NAME.get(st, st)} | "
                       f"{c['last'] or '—'} | {why(c)} |")
        out.append("")

    out += [
        (f"**{len(early)} companies sit at an early Affinity status "
         f"({', '.join(sorted(EARLY_STATUS))}) despite reaching Deep Diligence or "
         "beyond on the slides.**"),
        "",
    ]
    if early:
        out += ["| Company | Affinity Status | Furthest stage | Last seen |",
                "| --- | --- | --- | --- |"]
        for c, s in sorted(early, key=lambda x: x[0]["name"]):
            st = c["furthest"] or 0
            out.append(f"| {c['name']} | {s} | {st}. {STAGE_NAME.get(st, st)} | "
                       f"{c['last'] or '—'} |")
    else:
        out.append("_None._")
    return [*out, ""]


# Affinity's Working Group vocabulary against the slide thesis vocabulary. The
# mapping is naming only -- it does not adjudicate which tag is correct.
WG_TO_THESIS = {
    "Systems": "Autonomous Systems",
    "Climate": "Energy & Climate",
    "Health": "Human Health",
    "AI for Science": "AI for Science",
}


def sd_working_group(co: Cohort) -> list[str]:
    rows = []
    for c in co.companies:
        f = field_map(c)
        wg, th = f.get("working_group"), f.get("thesis_area")
        if not wg or not th:
            continue
        groups = [g.strip() for g in str(wg).split(";") if g.strip()]
        mapped = [WG_TO_THESIS.get(g, g) for g in groups]
        rows.append((c, str(wg), str(th), str(th) in mapped, len(groups) > 1))

    wg_cov = sum(1 for c in co.companies if field_map(c).get("working_group"))
    both = len(rows)
    agree = sum(1 for *_, ok, _ in rows if ok)
    disagree = [r for r in rows if not r[3]]

    out = [
        "## D. Working Group vs Thesis Area", "",
        ("Two independently sourced tags: Affinity's `working_group`, maintained by "
         "hand, against `thesis_area`, derived from the slide sub-section a company "
         "was presented under. Neither is treated as correct here — the point is to "
         "surface where they disagree so someone who knows can decide."),
        "",
        (f"Coverage: `working_group` on {of(wg_cov, co.n)} — not reported anywhere "
         f"else in this report. `thesis_area` on "
         f"{of(sum(1 for c in co.companies if field_map(c).get('thesis_area')), co.n)}. "
         f"Both present on {of(both, co.n)}, which is the comparison set below."),
        "",
        (f"**They agree on {of(agree, both, 'companies with both tags')}.** "
        f"{len(disagree)} disagree."),
        "",
        "| Working Group | " + " | ".join(THESIS_ORDER) + " |",
        "| --- | " + " | ".join("---" for _ in THESIS_ORDER) + " |",
    ]
    grid: dict[str, Counter] = defaultdict(Counter)
    for _c, wg, th, _ok, _multi in rows:
        grid[wg][th] += 1
    for wg in sorted(grid, key=lambda k: -sum(grid[k].values())):
        cells = " | ".join(str(grid[wg].get(t, 0)) for t in THESIS_ORDER)
        out.append(f"| {wg} | {cells} |")

    out += ["", "### D1. Every disagreement", ""]
    if disagree:
        out += ["| Company | Working Group (Affinity) | Thesis area (slides) | Note |",
                "| --- | --- | --- | --- |"]
        for c, wg, th, _, multi in sorted(disagree, key=lambda r: r[0]["name"]):
            note = "Affinity lists several groups, none of them the slide area" if multi else ""
            out.append(f"| {c['name']} | {wg} | {th} | {note} |")
    else:
        out.append("_None._")

    multi_ok = [r for r in rows if r[3] and r[4]]
    if multi_ok:
        out += ["",
                (f"{len(multi_ok)} further companies carry several working groups, one "
                 "of which matches the slide area; they are counted as agreeing: "
                 + ", ".join(f"{c['name']} ({wg})" for c, wg, *_ in
                             sorted(multi_ok, key=lambda r: r[0]["name"])) + ".")]
    return [*out, ""]


def se_discussion(co: Cohort) -> list[str]:
    buckets = {"0 times": 0, "1-2 times": 0, "3+ times": 0}
    for c in co.companies:
        t = c["times"] or 0
        buckets["0 times" if t == 0 else "1-2 times" if t <= 2 else "3+ times"] += 1

    out = [
        "## E. Discussion intensity", "",
        ("`times_discussed` counts the weeks a company was **bolded** on a slide, "
         "which is the record of it actually being talked through rather than "
         "merely listed. Every company here is in the diligence cohort, so a zero "
         "means a live diligence deal that has never had airtime."),
        "",
        "| Times discussed | Companies |",
        "| --- | --- |",
    ]
    for k, n in buckets.items():
        out.append(f"| {k} | {of(n, co.n)} |")

    ranked = sorted(co.companies, key=lambda c: (-(c["times"] or 0), c["name"]))
    out += [
        "",
        (f"Coverage is total — `times_discussed` is derived for all "
         f"{co.n} companies, so there is no missing-value case here."),
        "",
        "### E1. Top 10 by times discussed", "",
        "| Company | Times discussed | Furthest stage | Appearances | Last seen |",
        "| --- | --- | --- | --- | --- |",
    ]
    for c in ranked[:10]:
        st = c["furthest"] or 0
        out.append(f"| {c['name']} | {c['times'] or 0} | {st}. {STAGE_NAME.get(st, st)} "
                   f"| {c['appearances']} | {c['last'] or '—'} |")

    # Monthly bold volume, on the same buckets as section 3 so the two chart together.
    by_month: Counter = Counter()
    for c in co.companies:
        weeks = {wi for wi, _stage, bold, *_ in c["obs"] if bold}
        for wi in weeks:
            by_month[co.dates[wi][:7]] += 1
    total_bold = sum(by_month.values())

    out += [
        "",
        "### E2. Monthly discussion volume", "",
        ("Total bolded appearances across the cohort per month — same buckets as "
         "section 3, so intake and discussion can be charted on one axis. This "
         "counts appearances, not companies: one company discussed in three months "
         "contributes three."),
        "",
        f"Total: **{total_bold}** bolded appearances across {co.n} cohort companies.",
        "",
        "| Month | Discussed appearances | Intake (first reached Prelim) |",
        "| --- | --- | --- |",
    ]
    intake: Counter = Counter()
    for c in co.companies:
        d = co.first_at_or_above(c["id"], 4)
        if d:
            intake[d[:7]] += 1
    for m in sorted(set(by_month) | set(intake)):
        if m < INTAKE_FROM:
            continue
        b = by_month.get(m, 0)
        out.append(f"| {m} | {b} {'█' * b} | {intake.get(m, 0)} |")
    return [*out, ""]


def sf_folders(co: Cohort) -> list[str]:
    """Drive-folder coverage from the payload's `idx`, restricted to the cohort.

    match_drive_index.py already wrote a per-company verdict into the payload, so
    this reads that rather than re-running the name ladder -- one join definition,
    as the rest of the pipeline does it.
    """
    def matched(c):
        return bool(c.get("idx")) and c["idx"].get("tier") != "ambiguous"

    def ambiguous(c):
        return bool(c.get("idx")) and c["idx"].get("tier") == "ambiguous"

    if not any("idx" in c for c in co.companies):
        return ["## F. Drive-folder coverage", "",
                ("_No index join in this build. Run "
                 "`python scripts/match_drive_index.py` first._"), ""]

    hits = [c for c in co.companies if matched(c)]
    out = [
        "## F. Drive-folder coverage", "",
        ("Which cohort companies EV already holds a Drive folder for, from the join "
         "`match_drive_index.py` wrote into this build — read here, not recomputed, "
         "so the two cannot disagree. Restricted to the cohort, so these are not the "
         "982-folder numbers from the index tab. Stage is `furthest_stage_id` "
         "(`reached_*`)."),
        "",
        f"**{of(len(hits), co.n)} have a matched folder.**",
        "",
        "| Furthest stage | Companies | With a folder | Without | Ambiguous |",
        "| --- | --- | --- | --- | --- |",
    ]
    for s in STAGES:
        grp = [c for c in co.companies if (c["furthest"] or 0) == s]
        if not grp:
            out.append(f"| {s}. {STAGE_NAME[s]} | 0 | — | — | — |")
            continue
        y = sum(1 for c in grp if matched(c))
        a = sum(1 for c in grp if ambiguous(c))
        out.append(f"| {s}. {STAGE_NAME[s]} | {len(grp)} | {y} of {len(grp)} "
                   f"({pct(y, len(grp))}) | {len(grp) - y - a} | {a} |")

    deep, prelim = split(co)
    d_hit = sum(1 for c in deep if matched(c))
    p_hit = sum(1 for c in prelim if matched(c))
    out += [
        "",
        (f"Deep+ {of(d_hit, len(deep), 'Deep+ companies')} against Prelim-only "
         f"{of(p_hit, len(prelim), 'Prelim-only companies')}."),
        "",
    ]
    # The expectation was that folder coverage improves with depth. Say which way
    # it actually went rather than leaving the reader to compare two percentages.
    if len(deep) < SMALL_SAMPLE:
        holds = (d_hit / max(len(deep), 1)) > (p_hit / max(len(prelim), 1))
        out += [(f"The expectation was that coverage improves with depth. It does "
                 f"{'hold' if holds else 'not hold'} "
                 f"here — but the Deep+ group is {len(deep)} companies, so this "
                 "confirms nothing. Treat it as a count, not a trend."), ""]

    no_folder = [c for c in deep if not matched(c)]
    out += [
        "### F1. Deep Diligence+ with no Drive folder", "",
        (f"**{of(len(no_folder), len(deep), 'Deep+ companies')}.** A deal that got "
         "past Preliminary Diligence with nothing in storage under its name is the "
         "highest-priority item this report produces — either the folder exists "
         "under a spelling the join missed, or it was never created."),
        "",
    ]
    if no_folder:
        out += ["| Company | Furthest stage | Domain | Last seen | Index verdict |",
                "| --- | --- | --- | --- | --- |"]
        for c in sorted(no_folder, key=lambda c: (-(c["furthest"] or 0), c["name"])):
            st = c["furthest"] or 0
            v = "ambiguous — matches several folders" if ambiguous(c) else "no match"
            out.append(f"| {c['name']} | {st}. {STAGE_NAME.get(st, st)} | "
                       f"{c['domain'] or '—'} | {c['last'] or '—'} | {v} |")
    else:
        out.append("_None — every Deep+ company has a folder._")
    return [*out, ""]


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
        s5_round(co), s6_geography(co), s7_thesis(co),
        sa_advancement(co), sb_signal(co), sc_status(co), sd_working_group(co),
        se_discussion(co), sf_folders(co),
        notes(),
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
