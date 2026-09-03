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

**Stalled — needs attention before Monday.** These ten have gone longest without appearing on a slide, and have not moved stage since they entered the one they are in. Each is a live diligence deal that nobody has reported on; the action is to either advance it, pass on it, or say why it is still open. Today is 2026-09-03; the series ends 2026-08-31, so 3 days of the count below is simply the gap since the last meeting.

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

## A. Advancement splits

Every distribution below is cut two ways: **Deep+** — reached Deep Diligence or further, `furthest_stage_id >= 5`, 16 companies — against **Prelim-only** — `furthest_stage_id == 4`, 169 companies. Both are `reached_*` semantics. The two groups' coverage rates are computed and reported separately; neither is applied to the other.

### A1. Region (`hq_region`)

> **Read the Deep+ percentages with care.** That group is 16 companies against 169 at Preliminary Diligence only. At n=16, one company moves a share by 6%, so a gap between the two columns is not a signal on its own. The counts are exact; the percentages are fragile.

Coverage is reported per group, not pooled: **Deep+ 13 of 16 Deep+ companies (81%)**, **Prelim-only 61 of 169 Prelim-only companies (36%)**.

| Region | Deep+ (n=16) | share of Deep+ with a value | Prelim-only (n=169) | share of Prelim-only with a value |
| --- | --- | --- | --- | --- |
| International | 2 | 15% | 20 | 33% |
| United States | 11 | 85% | 41 | 67% |
| _no value_ | 3 | — | 108 | — |

### A2. Country (`hq_country`)

> **Read the Deep+ percentages with care.** That group is 16 companies against 169 at Preliminary Diligence only. At n=16, one company moves a share by 6%, so a gap between the two columns is not a signal on its own. The counts are exact; the percentages are fragile.

Coverage is reported per group, not pooled: **Deep+ 13 of 16 Deep+ companies (81%)**, **Prelim-only 61 of 169 Prelim-only companies (36%)**.

| Country | Deep+ (n=16) | share of Deep+ with a value | Prelim-only (n=169) | share of Prelim-only with a value |
| --- | --- | --- | --- | --- |
| Australia | 0 | 0% | 1 | 2% |
| Austria | 0 | 0% | 1 | 2% |
| Canada | 0 | 0% | 1 | 2% |
| France | 0 | 0% | 1 | 2% |
| Germany | 0 | 0% | 2 | 3% |
| Israel | 0 | 0% | 1 | 2% |
| Portugal | 0 | 0% | 1 | 2% |
| United Kingdom | 2 | 15% | 12 | 20% |
| United States | 11 | 85% | 41 | 67% |
| _no value_ | 3 | — | 108 | — |

### A3. Round stage (`stage`)

> **Still not `funding_round.round_stage`.** As in section 5, this is the `stage` field_value — Affinity's current round — because `funding_round` lives only in `data/pipeline.db`, which still cannot be built. Do not read this as a funding_round split.

> **Read the Deep+ percentages with care.** That group is 16 companies against 169 at Preliminary Diligence only. At n=16, one company moves a share by 6%, so a gap between the two columns is not a signal on its own. The counts are exact; the percentages are fragile.

Coverage is reported per group, not pooled: **Deep+ 13 of 16 Deep+ companies (81%)**, **Prelim-only 92 of 169 Prelim-only companies (54%)**.

| Round | Deep+ (n=16) | share of Deep+ with a value | Prelim-only (n=169) | share of Prelim-only with a value |
| --- | --- | --- | --- | --- |
| Preseed | 0 | 0% | 13 | 14% |
| Seed | 9 | 69% | 57 | 62% |
| Series A | 4 | 31% | 18 | 20% |
| Series B | 0 | 0% | 4 | 4% |
| _no value_ | 3 | — | 77 | — |


## B. Internal signal vs advancement

Do the team's own scores and Affinity's Interest flag track with getting deeper? Same Deep+ / Prelim-only split as section A. This is a two-group comparison to read in a room, **not** a regression: no correlation coefficient is computed and no significance is claimed. At these coverage levels none of it would survive one anyway.

### B1. Interest (`interest`)

> **Read the Deep+ percentages with care.** That group is 16 companies against 169 at Preliminary Diligence only. At n=16, one company moves a share by 6%, so a gap between the two columns is not a signal on its own. The counts are exact; the percentages are fragile.

Coverage is reported per group, not pooled: **Deep+ 9 of 16 Deep+ companies (56%)**, **Prelim-only 82 of 169 Prelim-only companies (49%)**.

| Interest | Deep+ (n=16) | share of Deep+ with a value | Prelim-only (n=169) | share of Prelim-only with a value |
| --- | --- | --- | --- | --- |
| 1. Very High | 9 | 100% | 3 | 4% |
| 2. High | 0 | 0% | 27 | 33% |
| 3. Moderate | 0 | 0% | 26 | 32% |
| 4. Low | 0 | 0% | 11 | 13% |
| 5. Propose Pass | 0 | 0% | 15 | 18% |
| _no value_ | 7 | — | 87 | — |

### B2. Team score (`score_team`)

> **Read the Deep+ percentages with care.** That group is 16 companies against 169 at Preliminary Diligence only. At n=16, one company moves a share by 6%, so a gap between the two columns is not a signal on its own. The counts are exact; the percentages are fragile.

> **Coverage is 5%** — `score_team` is recorded for only 9 of 185 here. The distribution below describes those 9, and cannot be extrapolated to the rest.

Coverage is reported per group, not pooled: **Deep+ 3 of 16 Deep+ companies (19%)**, **Prelim-only 6 of 169 Prelim-only companies (4%)**.

| Team score | Deep+ (n=16) | share of Deep+ with a value | Prelim-only (n=169) | share of Prelim-only with a value |
| --- | --- | --- | --- | --- |
| +++ | 2 | 67% | 1 | 17% |
| ++ | 1 | 33% | 4 | 67% |
| + | 0 | 0% | 1 | 17% |
| _no value_ | 13 | — | 163 | — |

### B3. Tech score (`score_tech`)

> **Read the Deep+ percentages with care.** That group is 16 companies against 169 at Preliminary Diligence only. At n=16, one company moves a share by 6%, so a gap between the two columns is not a signal on its own. The counts are exact; the percentages are fragile.

> **Coverage is 6%** — `score_tech` is recorded for only 11 of 185 here. The distribution below describes those 11, and cannot be extrapolated to the rest.

Coverage is reported per group, not pooled: **Deep+ 3 of 16 Deep+ companies (19%)**, **Prelim-only 8 of 169 Prelim-only companies (5%)**.

| Tech score | Deep+ (n=16) | share of Deep+ with a value | Prelim-only (n=169) | share of Prelim-only with a value |
| --- | --- | --- | --- | --- |
| +++ | 3 | 100% | 4 | 50% |
| ++ | 0 | 0% | 4 | 50% |
| _no value_ | 13 | — | 161 | — |

### B4. Opportunity score (`score_oppt`)

> **Read the Deep+ percentages with care.** That group is 16 companies against 169 at Preliminary Diligence only. At n=16, one company moves a share by 6%, so a gap between the two columns is not a signal on its own. The counts are exact; the percentages are fragile.

> **Coverage is 7%** — `score_oppt` is recorded for only 13 of 185 here. The distribution below describes those 13, and cannot be extrapolated to the rest.

Coverage is reported per group, not pooled: **Deep+ 3 of 16 Deep+ companies (19%)**, **Prelim-only 10 of 169 Prelim-only companies (6%)**.

| Opportunity score | Deep+ (n=16) | share of Deep+ with a value | Prelim-only (n=169) | share of Prelim-only with a value |
| --- | --- | --- | --- | --- |
| +++ | 2 | 67% | 5 | 50% |
| ++ | 1 | 33% | 5 | 50% |
| _no value_ | 13 | — | 159 | — |


## C. Affinity Status vs slide-derived stage

A process-health check, not a data question: Affinity's `Status` is maintained by hand, the slide stage is derived from what was presented. Where they contradict each other, one of the two is out of date. Stage columns are `furthest_stage_id` (`reached_*`).

Coverage: 123 of 185 cohort companies (66%) have an Affinity Status.

| Affinity Status | 4. Preliminary Diligence | 5. Deep Diligence | 6. Negotiate / Offer | 7. Legal Diligence / Def Docs | Total |
| --- | --- | --- | --- | --- | --- |
| Pass | 41 | 3 | 0 | 0 | 44 |
| Preliminary Diligence | 22 | 2 | 0 | 0 | 24 |
| Initial Evaluation | 24 | 0 | 0 | 0 | 24 |
| Wait | 13 | 1 | 0 | 0 | 14 |
| Pre-Screen | 9 | 0 | 0 | 0 | 9 |
| Invested | 0 | 0 | 0 | 3 | 3 |
| Loss | 0 | 1 | 0 | 1 | 2 |
| Sourcing - No Outreach | 2 | 0 | 0 | 0 | 2 |
| Deep Diligence | 0 | 1 | 0 | 0 | 1 |
| _no status_ | 58 | 2 | 0 | 2 | 62 |

### C1. Contradictions to resolve

46 cohort companies carry Loss or Pass in Affinity. Most of those are not contradictions — a company that was passed on and has not been presented since is a record working correctly, so listing all 46 as actions would bury the real ones.

**7 need a person.** Either they reached Deep Diligence or beyond *after* being closed, or they were still appearing on slides within 56 days of the last meeting (2026-08-31) — in both cases Affinity says the deal is dead and the slides say it is not.

| Company | Affinity Status | Furthest stage | Last seen | Why flagged |
| --- | --- | --- | --- | --- |
| Ovelle | Loss | 7. Legal Diligence / Def Docs | 2026-04-20 | **reached Deep+ after being closed** |
| Valor | Loss | 5. Deep Diligence | 2025-11-03 | **reached Deep+ after being closed** |
| DINYA | Pass | 5. Deep Diligence | 2025-11-10 | **reached Deep+ after being closed** |
| CloverLeaf | Pass | 5. Deep Diligence | 2026-01-12 | **reached Deep+ after being closed** |
| Attune Neurosci | Pass | 5. Deep Diligence | 2026-03-16 | **reached Deep+ after being closed** |
| Lobe Labs | Pass | 4. Preliminary Diligence | 2026-07-13 | **still on slides within 56 days of the last meeting** |
| Blisk Dynamics | Pass | 4. Preliminary Diligence | 2026-08-17 | **still on slides within 56 days of the last meeting** |
| Emergentia | Pass | 4. Preliminary Diligence | 2025-10-14 | consistent — closed, and quiet since |
| Flexnode | Pass | 4. Preliminary Diligence | 2025-10-27 | consistent — closed, and quiet since |
| OceanBit | Pass | 4. Preliminary Diligence | 2025-10-27 | consistent — closed, and quiet since |
| Nouxel | Pass | 4. Preliminary Diligence | 2025-11-03 | consistent — closed, and quiet since |
| SceniX | Pass | 4. Preliminary Diligence | 2025-11-03 | consistent — closed, and quiet since |
| JuliaHub | Pass | 4. Preliminary Diligence | 2025-11-10 | consistent — closed, and quiet since |
| Mursla Bio | Pass | 4. Preliminary Diligence | 2025-11-10 | consistent — closed, and quiet since |
| Artisan Insight | Pass | 4. Preliminary Diligence | 2025-12-01 | consistent — closed, and quiet since |
| Circadian OS | Pass | 4. Preliminary Diligence | 2025-12-08 | consistent — closed, and quiet since |
| Porpoise Power | Pass | 4. Preliminary Diligence | 2025-12-08 | consistent — closed, and quiet since |
| MetAI | Pass | 4. Preliminary Diligence | 2026-01-05 | consistent — closed, and quiet since |
| Merge4Energy | Pass | 4. Preliminary Diligence | 2026-01-26 | consistent — closed, and quiet since |
| Human Bio | Pass | 4. Preliminary Diligence | 2026-02-02 | consistent — closed, and quiet since |
| MintNeuro | Pass | 4. Preliminary Diligence | 2026-02-02 | consistent — closed, and quiet since |
| Exousia | Pass | 4. Preliminary Diligence | 2026-02-17 | consistent — closed, and quiet since |
| Graph Tx | Pass | 4. Preliminary Diligence | 2026-02-23 | consistent — closed, and quiet since |
| NeoLogic | Pass | 4. Preliminary Diligence | 2026-02-23 | consistent — closed, and quiet since |
| PseudolithIC | Pass | 4. Preliminary Diligence | 2026-03-02 | consistent — closed, and quiet since |
| Q.ANT | Pass | 4. Preliminary Diligence | 2026-03-02 | consistent — closed, and quiet since |
| Sensorium | Pass | 4. Preliminary Diligence | 2026-03-02 | consistent — closed, and quiet since |
| Sention | Pass | 4. Preliminary Diligence | 2026-03-02 | consistent — closed, and quiet since |
| Coformer.ai | Pass | 4. Preliminary Diligence | 2026-03-09 | consistent — closed, and quiet since |
| Braid Robotics | Pass | 4. Preliminary Diligence | 2026-03-16 | consistent — closed, and quiet since |
| Level 12 Bio | Pass | 4. Preliminary Diligence | 2026-03-16 | consistent — closed, and quiet since |
| Oak Robotics | Pass | 4. Preliminary Diligence | 2026-03-16 | consistent — closed, and quiet since |
| Neurofold | Pass | 4. Preliminary Diligence | 2026-03-23 | consistent — closed, and quiet since |
| Flume | Pass | 4. Preliminary Diligence | 2026-03-30 | consistent — closed, and quiet since |
| Fusion Dynamics | Pass | 4. Preliminary Diligence | 2026-03-30 | consistent — closed, and quiet since |
| Mueon | Pass | 4. Preliminary Diligence | 2026-03-30 | consistent — closed, and quiet since |
| Oxipital AI | Pass | 4. Preliminary Diligence | 2026-04-06 | consistent — closed, and quiet since |
| Gravity | Pass | 4. Preliminary Diligence | 2026-04-06 | consistent — closed, and quiet since |
| Horizon Labs | Pass | 4. Preliminary Diligence | 2026-05-18 | consistent — closed, and quiet since |
| Walden Robotics | Pass | 4. Preliminary Diligence | 2026-06-01 | consistent — closed, and quiet since |
| TriMind | Pass | 4. Preliminary Diligence | 2026-06-01 | consistent — closed, and quiet since |
| Kampto | Pass | 4. Preliminary Diligence | 2026-06-08 | consistent — closed, and quiet since |
| One.Bio | Pass | 4. Preliminary Diligence | 2026-06-08 | consistent — closed, and quiet since |
| CMatrics | Pass | 4. Preliminary Diligence | 2026-06-15 | consistent — closed, and quiet since |
| Coral Innovations | Pass | 4. Preliminary Diligence | 2026-06-22 | consistent — closed, and quiet since |
| Daedalus | Pass | 4. Preliminary Diligence | 2026-06-22 | consistent — closed, and quiet since |

**0 companies sit at an early Affinity status (Initial Evaluation, Pre-Screen, Sourcing - No Outreach) despite reaching Deep Diligence or beyond on the slides.**

_None._


## D. Working Group vs Thesis Area

Two independently sourced tags: Affinity's `working_group`, maintained by hand, against `thesis_area`, derived from the slide sub-section a company was presented under. Neither is treated as correct here — the point is to surface where they disagree so someone who knows can decide.

Coverage: `working_group` on 184 of 185 cohort companies (99%) — not reported anywhere else in this report. `thesis_area` on 182 of 185 cohort companies (98%). Both present on 182 of 185 cohort companies (98%), which is the comparison set below.

**They agree on 177 of 182 companies with both tags (97%).** 5 disagree.

| Working Group | Human Health | Autonomous Systems | Energy & Climate | AI for Science |
| --- | --- | --- | --- | --- |
| Systems | 1 | 74 | 0 | 2 |
| Climate | 0 | 1 | 63 | 0 |
| Health | 33 | 0 | 0 | 0 |
| Systems; Climate | 0 | 1 | 2 | 0 |
| Climate; Systems | 0 | 0 | 1 | 1 |
| AI for Science | 0 | 0 | 0 | 2 |
| Health; Systems | 1 | 0 | 0 | 0 |

### D1. Every disagreement

| Company | Working Group (Affinity) | Thesis area (slides) | Note |
| --- | --- | --- | --- |
| MatNex | Climate; Systems | AI for Science | Affinity lists several groups, none of them the slide area |
| Mattiq | Systems | AI for Science |  |
| Micrographia Bio | Systems | Human Health |  |
| Origins AI | Systems | AI for Science |  |
| Phasic | Climate | Autonomous Systems |  |

5 further companies carry several working groups, one of which matches the slide area; they are counted as agreeing: Blisk Dynamics (Systems; Climate), Bristol Superlight (Climate; Systems), Gravity (Systems; Climate), Marmot Energy (Systems; Climate), MintNeuro (Health; Systems).


## E. Discussion intensity

`times_discussed` counts the weeks a company was **bolded** on a slide, which is the record of it actually being talked through rather than merely listed. Every company here is in the diligence cohort, so a zero means a live diligence deal that has never had airtime.

| Times discussed | Companies |
| --- | --- |
| 0 times | 151 of 185 cohort companies (82%) |
| 1-2 times | 18 of 185 cohort companies (10%) |
| 3+ times | 16 of 185 cohort companies (9%) |

Coverage is total — `times_discussed` is derived for all 185 companies, so there is no missing-value case here.

### E1. Top 10 by times discussed

| Company | Times discussed | Furthest stage | Appearances | Last seen |
| --- | --- | --- | --- | --- |
| Bristol Superlight | 23 | 7. Legal Diligence / Def Docs | 27 | 2026-05-18 |
| CorePower Magnetics | 19 | 7. Legal Diligence / Def Docs | 30 | 2026-08-24 |
| DG Matrix | 17 | 7. Legal Diligence / Def Docs | 17 | 2026-02-17 |
| Alithia | 11 | 5. Deep Diligence | 16 | 2026-08-24 |
| Ovelle | 8 | 7. Legal Diligence / Def Docs | 14 | 2026-04-20 |
| Ayo Electronics | 6 | 7. Legal Diligence / Def Docs | 6 | 2026-04-06 |
| Numem | 6 | 5. Deep Diligence | 14 | 2026-08-31 |
| Coral Innovations | 5 | 4. Preliminary Diligence | 7 | 2026-06-22 |
| MatNex | 5 | 5. Deep Diligence | 8 | 2026-08-31 |
| Skouria | 5 | 5. Deep Diligence | 21 | 2026-04-06 |

### E2. Monthly discussion volume

Total bolded appearances across the cohort per month — same buckets as section 3, so intake and discussion can be charted on one axis. This counts appearances, not companies: one company discussed in three months contributes three.

Total: **150** bolded appearances across 185 cohort companies.

| Month | Discussed appearances | Intake (first reached Prelim) |
| --- | --- | --- |
| 2025-10 | 12 ████████████ | 36 |
| 2025-11 | 15 ███████████████ | 14 |
| 2025-12 | 13 █████████████ | 4 |
| 2026-01 | 12 ████████████ | 10 |
| 2026-02 | 12 ████████████ | 23 |
| 2026-03 | 19 ███████████████████ | 16 |
| 2026-04 | 11 ███████████ | 17 |
| 2026-05 | 16 ████████████████ | 15 |
| 2026-06 | 16 ████████████████ | 11 |
| 2026-07 | 12 ████████████ | 22 |
| 2026-08 | 12 ████████████ | 17 |


## F. Drive-folder coverage

Which cohort companies EV already holds a Drive folder for, from the join `match_drive_index.py` wrote into this build — read here, not recomputed, so the two cannot disagree. Restricted to the cohort, so these are not the 982-folder numbers from the index tab. Stage is `furthest_stage_id` (`reached_*`).

**83 of 185 cohort companies (45%) have a matched folder.**

| Furthest stage | Companies | With a folder | Without | Ambiguous |
| --- | --- | --- | --- | --- |
| 4. Preliminary Diligence | 169 | 69 of 169 (41%) | 100 | 0 |
| 5. Deep Diligence | 10 | 8 of 10 (80%) | 2 | 0 |
| 6. Negotiate / Offer | 0 | — | — | — |
| 7. Legal Diligence / Def Docs | 6 | 6 of 6 (100%) | 0 | 0 |

Deep+ 14 of 16 Deep+ companies (88%) against Prelim-only 69 of 169 Prelim-only companies (41%).

The expectation was that coverage improves with depth. It does hold here — but the Deep+ group is 16 companies, so this confirms nothing. Treat it as a count, not a trend.

### F1. Deep Diligence+ with no Drive folder

**2 of 16 Deep+ companies (12%).** A deal that got past Preliminary Diligence with nothing in storage under its name is the highest-priority item this report produces — either the folder exists under a spelling the join missed, or it was never created.

| Company | Furthest stage | Domain | Last seen | Index verdict |
| --- | --- | --- | --- | --- |
| Alithia | 5. Deep Diligence | alithiapower.com | 2026-08-24 | no match |
| CloverLeaf | 5. Deep Diligence | cloverleafbio.com | 2026-01-12 | no match |


## Not in this report

- **Sourcing / channel.** `entity_sourcing` has zero rows; there is nothing to group by. Not added.
- **Round size / valuation correlation.** There is no valuation field anywhere in `schema.sql`. `round_size_usd` exists as a field_value, so a round-size distribution is possible, but a *correlation* against valuation is not. Not added — flagged as a question instead.
- **`funding_round.round_stage`.** Section 5 substitutes a different source; see the warning there.
