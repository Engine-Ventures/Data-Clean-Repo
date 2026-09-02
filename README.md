# EV Deal Pipeline — data layer

A versioned SQLite layer over the New Deal Meeting slide extraction, built to
replace manual editing of `EV_Deal_Pipeline_Clean_Dataset_DRAFT.xlsx`.

The staging workbook is a spreadsheet with 56 columns and no history. This is
the same information as an append-only evidence log plus a provenance layer,
so that corrections, merges and enrichment are recorded rather than overwritten.
No interface is built yet — that decision is deliberately still open.

## Quick start

```bash
source .venv/bin/activate          # see SETUP.md to build the venv
python scripts/build_db.py         # data/raw/*.xlsx -> data/pipeline.db
pytest                             # 69 regression tests
ruff check .
```

`build_db.py` always builds into a fresh file (`--force` to replace an
existing one), so a load can be repeated and two builds diffed.

## Source data

Not in git — it is confidential and it is not the system of record. Put these
in `data/raw/`:

| File | Role |
| --- | --- |
| `EV_Deal_Pipeline_Clean_Dataset_DRAFT.xlsx` | the raw extraction; 498 rows, 2,169 observations |
| `EV_Deal_Pipeline_Clean_Dataset_v2_DEDUPED.xlsx` | a sibling dedup attempt, imported as *proposals* |
| `affinity_export_2026-09-01.csv` | 581 Affinity records; enrichment only |

## What the layer does

Nine things the workbook cannot:

1. **Immutable entity IDs with an alias table.** `EV0001` is stable within one
   file only, so entities get a permanent internal id and every observed
   spelling maps to it. This is what lets the 18 slide names absent from
   `company_name` (`Corepower Magnetics`, `Neologic`, `LiftOff`, …) resolve
   without touching the evidence log.
2. **Merges are proposals, not edits.** 103 merge proposals sit in a review
   queue; 17 are backed by a shared domain, the most stable key available, and
   each is oriented at the fuller spelling rather than at whichever row came
   first. Nothing is merged at load time.
3. **Phantoms are marked, not deleted.** A PDF line-wrap continuation is
   flagged with a reason; its observations survive so the extraction stays
   auditable.
4. **Grouped entries stay whole.** `Cetos Water / Kira` and `Flume / Gravity`
   remain entities with an open `group_split` decision, rather than being
   deleted while their slide observations remain.
5. **Derived values are derived.** Everything reconstructible from the
   observation log — furthest stage, `reached_*`, `date_*`, appearance and
   discussion counts, gap counts, the P1–P4 worklist ranking — is a view, not
   a column. The 56 workbook columns reduce to ~20 stored inputs.
6. **Provenance on every enriched field.** Each write stores value, source
   (`Slides` | `Affinity` | `Public` | `Manual`), user and timestamp, and
   supersedes rather than overwrites, so a correction keeps its history. Public
   enrichment cannot be written without a citation.
7. **Three genuinely distinct states.** Blank (unknown), 0 (genuinely zero) and
   "checked, genuinely unavailable" are separate and enforced by constraints.
8. **Missing weeks are rows.** Four absent Mondays are recorded as meetings
   with `status = 'not_extracted'`, so a skipped meeting cannot read as company
   attrition.
9. **Corrections shadow the evidence.** `slide_observation` is never edited;
   `slide_observation_override` shadows it, and the views apply overrides.

## Layout

```
schema.sql                  tables, constraints and derived views
src/evpipeline/
  vocab.py                  the locked picklists (§4)
  db.py                     connection, schema creation, run log
  ingest.py                 workbook -> database, and the review-queue detectors
  metrics.py                derived metrics; every count carries its coverage
  validate.py               the §8 write-path rules
scripts/build_db.py         CLI
tests/                      69 tests: anchors, validation, merge queue
```

## Findings from the load

Things that came out of building this, beyond what the handoff brief records.

**The dedup in `v2_DEDUPED` is not safe to adopt as-is.** It resolves 17 rows
without breaking any funnel anchor, but it picks the fragment as canonical in
several families (`Artisan Insight` over `Artisan Insights`, `Augment Bio` over
`Augment Biologics`, `Dynamical Mind` over `Dynamical Minds`, `Hydron` over
`Hydron Desal`), leaves the worst family barely touched — seven `Cetos`
variants still stand — deletes `One Biosciences` with no merge target when
`One.Bio` shares its domain, and resolves the open slash-entry question by
deletion. All 17 are imported as proposals.

**Shared domains settle 15 merge families outright.** Domain is the key §9 says
to trust first, and it resolves families that name matching cannot, including
`One Biosciences` / `One.Bio` and `Flume / Gravity` / `Gravity`.

**"Meetings This Week" is not a funnel stage.** A company is routinely listed
twice at one meeting: once in the agenda column and once in its thesis
prelim-diligence sub-section. 74 such dual-listings exist. Treating the agenda
column as a mutually exclusive stage would drop real stage evidence, so the
observation grain includes stage and section.

**`reached_*` in the workbook means "observed at", not "got at least this
far".** Axiomatic AI moved Preliminary Diligence → Legal in one week
(2025-12-15, a genuine +3 jump, not a missing-week artifact) and was never seen
in the Deep Diligence column. So 15 companies were observed at Deep Diligence
while 16 reached that depth — which is why the brief's own furthest-stage rows
(Legal 6 + Deep 10) sum to 16 against a stated 15. Both semantics are exposed
separately; reporting has to say which it means.

**Legal is not the same as invested.** §10 offers "every company reaching Legal
appears on the Fund III investment list" as the original correctness check. It
holds for five of six. Ovelle reached Legal Diligence on 2026-03-30 and is
listed in §12 among the deep-diligence companies that died. The invariant is
too strong, not the data wrong.

**The three extraction gaps hide four missing weeks,** not three: the
2025-12-15 → 2026-01-05 gap is 21 days. Cadence is also not a clean 7-day grid
(39 Mondays, 4 Tuesdays), so absences are inferred from gaps rather than a grid.

**Affinity's dates are US-format `MM/DD/YYYY` strings,** normalised to ISO on
ingest. Once parsed, 105 entities have a `first_meeting` predating their first
slide appearance, some as early as 2018. That is expected — the slide window
opens 2025-10-14 — so they carry the `relationship_predates_crm` flag rather
than failing validation, which is §6 Q3's point that `first_slide_date` is the
anchor.

**Only 3 of Affinity's 9 zero round sizes belong to the slide population**
(Lithosquare, Sequins, TriMind). The other 6 are Affinity-only records that
never appeared on a slide, and Affinity may never add a company. Recovering
those three closes Lithosquare's last gap, which is why the derived worklist is
6/8/**67**/190/**227** against the workbook's 6/8/68/190/226.

**Bold colour is not in the workbook at all.** §6 Q1 — whether bold means
"discussed" or "on the agenda" — turns on the 136 black / 16 dark-red split,
which exists only in the PDF. The column is modelled and NULL everywhere; a
test asserts it stays NULL until re-extraction, so the distinction cannot be
quietly invented.

**Five companies are listed twice on a single slide under two spellings**
(`NeoLogic`/`Neologic`, `NoLux`/`Nolux`, `Stac12`/`StaC12`,
`WAVR`/`WAVR Technologies`). Whether the slide really listed them twice or the
extractor read one row twice needs a look at the page; all five are queued.

## Review queue

293 open items. This is the §9 "review queue, not silent writes" surface.

| Kind | Count | Meaning |
| --- | --- | --- |
| `merge_proposal` | 103 | 17 domain-backed, 69 name-shape, 17 from v2 |
| `stage_jump` | 102 | moved more than 2 levels between meetings |
| `stage_regression` | 68 | moved backwards; legitimate but should be stated |
| `group_split` | 14 | one row naming several companies |
| `duplicate_listing` | 5 | same company twice on one slide |
| `line_wrap_candidate` | 1 | leading-slash continuation |

## Regression tests

`tests/test_anchors.py` pins every anchor in §10. Where a number differs from
the brief the test asserts the difference deliberately and its docstring says
why — see the observed-at/reached split, the 2,168 + 1 duplicate, the four
missing weeks and the 67/227 worklist shift above.

`tests/test_validation.py` proves the §8 write rules actually bite: ISO dates,
locked picklists, zero-vs-unknown, citation-required public writes, currency
pairing, domain-required new records, the `first_meeting` ordering rule, and
the three gap states.

`tests/test_merge_proposals.py` covers the canonical-name scoring and asserts
that nothing is merged, deleted or resolved at load time.

## Not done

- No interface. The §8 browse/edit/worklist surface and the §9 slide generator
  both sit on this layer; the UI shape is an open decision.
- Extraction is not re-run. `bold_color` needs the PDF, and the deck holds 209
  meetings back to Aug 2021 against the 43 loaded here.
- §7 capture — pass reasons, outcomes, sourcer, valuations, founders,
  non-EV rounds, attendees — has tables and picklists but no rows. The brief is
  right that every week of delay is permanent loss.
- The 293 review items are unresolved by design; resolving them is a person's
  job, and `entity.merged_into` plus `slide_observation_override` are where the
  decisions land.
