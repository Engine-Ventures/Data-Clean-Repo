# Diligence cohort — trends

Cohort: **185 companies** — advanced-stage diligence only (furthest stage >= Preliminary Diligence), per `DiligenceCompanies_EVPipeline (1).xlsx`.
Stages 1-3 are excluded from every numerator and every denominator below.

- Source: `ui/index.html`, built 2026-09-02 17:18 UTC
- Slide series: 47 meetings, 2025-10-14 → 2026-08-31
- Report generated: 2026-09-03

Every count carries the denominator it was taken over.


## 1. Funnel

Two different questions, kept apart per the README's `observed_at_*` vs `reached_*` distinction.

**Currently at each stage** — `latest_stage_id`, the stage of the most recent slide appearance. Sums to the cohort.

| Stage | Currently at |
| --- | --- |
| 4. Preliminary Diligence | 170 of 185 cohort companies (92%) |
| 5. Deep Diligence | 9 of 185 cohort companies (5%) |
| 6. Negotiate / Offer | 0 of 185 cohort companies (0%) |
| 7. Legal Diligence / Def Docs | 6 of 185 cohort companies (3%) |

**Ever reached each stage** — `furthest_stage_id >= stage`. Cumulative, so these do not sum; every company reached stage 4 by construction.

| Stage | Ever reached |
| --- | --- |
| 4. Preliminary Diligence | 185 of 185 cohort companies (100%) |
| 5. Deep Diligence | 16 of 185 cohort companies (9%) |
| 6. Negotiate / Offer | 6 of 185 cohort companies (3%) |
| 7. Legal Diligence / Def Docs | 6 of 185 cohort companies (3%) |

## 2. Stage-to-stage conversion

Of everyone who reached a stage, the share that also reached the next. `reached_*` semantics throughout; the denominator is the prior stage's reached count, not the cohort.

| Transition | Converted |
| --- | --- |
| Preliminary Diligence → Deep Diligence | 16 of 185 who reached Preliminary Diligence (9%) |
| Deep Diligence → Negotiate / Offer | 6 of 16 who reached Deep Diligence (38%) |
| Negotiate / Offer → Legal Diligence / Def Docs | 6 of 6 who reached Negotiate / Offer (100%) |

End to end, Preliminary Diligence → Legal Diligence: 6 of 185 who reached Preliminary Diligence (3%).

## 3. Monthly intake

Cohort companies by the month they **first reached** Preliminary Diligence (`reached_*` semantics — the earliest slide appearance at stage 4 or deeper). This is the intake trend to chart.

Dated: 185 of 185 cohort companies (100%).

| Month | First reached Prelim Diligence |
| --- | --- |
| 2025-10 | 36 of 185 (19%) ████████████████████████████████████ |
| 2025-11 | 14 of 185 (8%) ██████████████ |
| 2025-12 | 4 of 185 (2%) ████ |
| 2026-01 | 10 of 185 (5%) ██████████ |
| 2026-02 | 23 of 185 (12%) ███████████████████████ |
| 2026-03 | 16 of 185 (9%) ████████████████ |
| 2026-04 | 17 of 185 (9%) █████████████████ |
| 2026-05 | 15 of 185 (8%) ███████████████ |
| 2026-06 | 11 of 185 (6%) ███████████ |
| 2026-07 | 22 of 185 (12%) ██████████████████████ |
| 2026-08 | 17 of 185 (9%) █████████████████ |

## 4. Time in stage

_Re-derived from the observation series, not read from `v_stage_transition` and `v_dwell` — the database is not currently buildable. Same rule as the views: a transition is a consecutive pair of appearances whose stage differs; dwell is meetings at a stage._

**Between consecutive stage transitions**

- Transitions observed: **25**, across 15 of 185 cohort companies (8%) that moved stage at least once.
- Average gap: **17 days** (median 7, range 6 to 112)
- The other 170 of 185 cohort companies (92%) were never seen at more than one stage, so they contribute no transition.

**Longest current dwell** — days since the last slide appearance, for companies whose latest stage has not changed since they entered it. Today is 2026-09-03; the series ends 2026-08-31.

| Company | Stage | Entered | Last seen | Days since last seen | Meetings at stage |
| --- | --- | --- | --- | --- | --- |
| Emergentia | 4. Preliminary Diligence | 2025-10-14 | 2025-10-14 | 324 | 1 |
| Ropirio | 4. Preliminary Diligence | 2025-10-14 | 2025-10-14 | 324 | 1 |
| Telos | 4. Preliminary Diligence | 2025-10-14 | 2025-10-14 | 324 | 1 |
| Alkali Labs | 4. Preliminary Diligence | 2025-10-14 | 2025-10-20 | 318 | 2 |
| FAST Metals | 4. Preliminary Diligence | 2025-10-14 | 2025-10-20 | 318 | 2 |
| Flip Tx | 4. Preliminary Diligence | 2025-10-14 | 2025-10-20 | 318 | 2 |
| IceBox Energy | 4. Preliminary Diligence | 2025-10-14 | 2025-10-20 | 318 | 2 |
| Mana Battery | 4. Preliminary Diligence | 2025-10-20 | 2025-10-20 | 318 | 1 |
| Flexnode | 4. Preliminary Diligence | 2025-10-14 | 2025-10-27 | 311 | 3 |
| Heliux | 4. Preliminary Diligence | 2025-10-14 | 2025-10-27 | 311 | 3 |

Ranked over 185 of 185 cohort companies (100%) with at least one appearance.

## 5. Round stage

> **This is not `funding_round.round_stage`.** That table lives only in `data/pipeline.db`, which cannot currently be built, and it has no representation in the interface payload. What follows is the `stage` field_value — Affinity's *current* round for the organisation — which is a different source, a single value rather than a round history, and has its own coverage. Do not report it as a funding_round rollup. The funding_round split is unanswerable until the database is rebuilt.

Coverage: 105 of 185 cohort companies (57%) have a `stage` value; 80 of 185 cohort companies (43%) have none.

| Round | Count | Share of cohort | Share of those with a value |
| --- | --- | --- | --- |
| Preseed | 13 | 13 of 185 (7%) | 12% |
| Seed | 66 | 66 of 185 (36%) | 63% |
| Series A | 22 | 22 of 185 (12%) | 21% |
| Series B | 4 | 4 of 185 (2%) | 4% |
| _no value_ | 80 | 80 of 185 (43%) | — |

Grouped as asked — Seed vs Series A vs Series B+ (Preseed shown separately, it is not Seed):

- Seed: 66 of 185 cohort companies (36%) — 63% of those with a value
- Series A: 22 of 185 cohort companies (12%) — 21% of those with a value
- Series B+: 4 of 185 cohort companies (2%) — 4% of those with a value
- Preseed: 13 of 185 cohort companies (7%)

## 6. Geography

### Region (`hq_region`)

Coverage: 74 of 185 cohort companies (40%) have a current value.

| Region | Count | Share of cohort | Share of those with a value |
| --- | --- | --- | --- |
| United States | 52 | 52 of 185 (28%) | 70% |
| International | 22 | 22 of 185 (12%) | 30% |

Of the 111 with no value: `hq_region` is not one of the five tracked gap fields, so none carry a gap state — a blank here cannot be told apart from an unchecked one. `hq_country` is tracked and has the same 74 covered, so use its split as the proxy.

### Country (`hq_country`)

Coverage: 74 of 185 cohort companies (40%) have a current value.

| Country | Count | Share of cohort | Share of those with a value |
| --- | --- | --- | --- |
| United States | 52 | 52 of 185 (28%) | 70% |
| United Kingdom | 14 | 14 of 185 (8%) | 19% |
| Germany | 2 | 2 of 185 (1%) | 3% |
| Canada | 1 | 1 of 185 (1%) | 1% |
| Australia | 1 | 1 of 185 (1%) | 1% |
| Austria | 1 | 1 of 185 (1%) | 1% |
| Israel | 1 | 1 of 185 (1%) | 1% |
| France | 1 | 1 of 185 (1%) | 1% |
| Portugal | 1 | 1 of 185 (1%) | 1% |

Of the 111 with no value: 111 `not_checked`.


## 7. Thesis area

Coverage: 182 of 185 cohort companies (98%) have a `thesis_area` value.

| Thesis area | Count | Share of cohort | Share of those with a value |
| --- | --- | --- | --- |
| Human Health | 35 | 35 of 185 (19%) | 19% |
| Autonomous Systems | 76 | 76 of 185 (41%) | 42% |
| Energy & Climate | 66 | 66 of 185 (36%) | 36% |
| AI for Science | 5 | 5 of 185 (3%) | 3% |

### Thesis area x furthest stage

Row denominator is that thesis area's own count, so the percentages answer "how deep does this area get?" rather than "how big is it?". Counts are `furthest_stage_id`, so a company appears in exactly one column.

| Thesis area | 4. Preliminary Diligence | 5. Deep Diligence | 6. Negotiate / Offer | 7. Legal Diligence / Def Docs | Reached Deep+ |
| --- | --- | --- | --- | --- | --- |
| Human Health | 30 (86%) | 4 (11%) | 0 (0%) | 1 (3%) | 5 of 35 (14%) |
| Autonomous Systems | 73 (96%) | 2 (3%) | 0 (0%) | 1 (1%) | 3 of 76 (4%) |
| Energy & Climate | 62 (94%) | 2 (3%) | 0 (0%) | 2 (3%) | 4 of 66 (6%) |
| AI for Science | 4 (80%) | 1 (20%) | 0 (0%) | 0 (0%) | 1 of 5 (20%) |

Cohort baseline for comparison: 16 of 185 cohort companies (9%) reached Deep Diligence or beyond. An area above that line is running deeper than the cohort average.

The 3 companies with no thesis area are excluded from the cross-tab, not bucketed as unknown.

## Not in this report

- **Sourcing / channel.** `entity_sourcing` has zero rows; there is nothing to group by. Not added.
- **Round size / valuation correlation.** There is no valuation field anywhere in `schema.sql`. `round_size_usd` exists as a field_value, so a round-size distribution is possible, but a *correlation* against valuation is not. Not added — flagged as a question instead.
- **`funding_round.round_stage`.** Section 5 substitutes a different source; see the warning there.
