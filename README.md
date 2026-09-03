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
python scripts/build_ui.py         # data/pipeline.db -> ui/index.html
python scripts/screen_diligence.py # screen it to the diligence cohort
python scripts/match_drive_index.py # how much of the New Deals index is visible
python scripts/serve.py            # the same interface, with writes enabled
pytest                             # 139 regression tests
ruff check .
```

`build_ui.py` produces a portable read-only snapshot; `serve.py` renders the
same template from the live database on every request and adds the three
write surfaces described under **Writing from the browser** below.

`build_db.py` always builds into a fresh file (`--force` to replace an
existing one), so a load can be repeated and two builds diffed.

## Source data

Not in git — it is confidential and it is not the system of record. Put these
in `data/raw/`:

| File | Role |
| --- | --- |
| `EV_Deal_Pipeline_Clean_Dataset_DRAFT.xlsx` | the raw extraction; 498 rows, 2,169 observations |
| `EV_Deal_Pipeline_Clean_Dataset_v2_DEDUPED.xlsx` | a sibling dedup attempt, imported as *proposals* |
| `affinity_export_2026-09-01.csv` | the Affinity Deal Flow view; 2,869 rows, 2,815 organisations |
| `DiligenceCompanies_EVPipeline (1).xlsx` | the 185-company advanced-stage cohort |
| `Index, New Deals Companies, v2026-08-28-01.xlsx` | 982 Drive folders under *New Deals / 02. Companies* |
| `Copy of Monday - New Deal Meeting.pdf` | the whole deck: 1,018 pages, 153 dated meetings back to Aug 2021 |

Only 251 of the Affinity rows are linked to a slide company; the rest are
passes, pre-screens and sourcing records. Enrichment uses the linked ones,
and the index-reach join below uses all of them.

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
  write.py                  creating a company and setting tags, through §8
  tags.py                   tag canonical form and derived vocabulary
  lookup.py                 the add form's one public lookup (Wikidata)
scripts/build_db.py         CLI
scripts/match_drive_index.py how much of the Drive index the record covers
scripts/serve.py            local workbench with the write endpoints
tests/                      139 tests: anchors, validation, merge queue,
                            hand-add, tags, lookup, index reach
```

## Writing from the browser

`scripts/serve.py` binds 127.0.0.1 and has no authentication; it is a local
tool over confidential deal data and does not belong on a network interface.
Everything it writes goes through `validate.write_field`, so a browser write
obeys the same §8 rules as `python -m evpipeline.validate --batch`.

**Add a company.** Name, website, country, round stage, owner and tags. The
website is effectively required because a new record needs a domain (§8) and
that is the only place one can come from. A name that matches an existing
company warns rather than blocks: confirming creates the company anyway and
files each match as an open merge proposal. Nothing is auto-merged.

**Autofill.** Pressing *Look up* asks Wikidata once for the company's official
website (P856) and headquarters country (P159 → P17) and fills whichever of
those two fields are still empty. A suggestion is marked in the page as
suggested and is written with `source = Public` and the Wikidata entity URL as
its citation; edit the field and it becomes an ordinary `Manual` write under
your name. The distinction is visible before you save, because it is what the
save is about to record.

Two limits worth knowing before relying on it. Wikidata's coverage of
early-stage private companies is thin, so most companies here return nothing —
and the match must be exact on a normalised name, because the search API's top
hit for a startup name is routinely a large public company that shares a word.
A missing suggestion costs nothing; a wrong country on a deal record does.
It is also the only thing in this repo that reaches the internet: the company
name you typed goes to wikidata.org. `--no-lookup` turns it off.

**Tags.** Free text, comma-separated, one field per company, editable from the
company drawer — the only value the browser can change after creation. Tags
are stored in `field_value` like any other field (append-only, provenanced)
rather than in a table of their own; see the note at the top of `tags.py` for
why. They are *not* a worklist gap: an untagged company is untagged, not
incomplete, so no coverage denominator moves when you use them. Tags already
in use are suggested as you type, in the add form and the drawer, and the
Trends panel has a tag filter that re-derives every rollup on it — funnel,
coverage, dwell, weekly mix, intake and the trace — over just the tagged
companies, using the same rules `screen_diligence.py` uses for the build.

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

## Advanced-stage screen

The deal team rescoped the deliverable on 2026-09-02 to companies that reached
Preliminary Diligence or beyond. `DiligenceCompanies_EVPipeline (1).xlsx`
is that cohort — 185 companies — and `scripts/screen_diligence.py` screens the
built interface to match it:

```bash
python scripts/build_ui.py            # faithful full build: 498 rows, 7 stages
python scripts/screen_diligence.py    # -> the 185-company diligence cohort
```

The screen is a second pass, not a branch inside `build_ui.py`, so the funnel,
coverage and discussion rollups keep one definition over the full population
(`metrics.py`) and one re-derivation for the cohort (`screen_diligence.py`).
It is idempotent, and it does three things:

- **Companies** — exactly the workbook's rows, joined on
  `company_id == 'EV%04d' % entity_id`. A deterministic join: 184 of the 185
  canonical names agree exactly, the one exception being `Attune Tx` →
  `Attune Neurosciences`, an enrichment rename made in the workbook. Slide
  names the workbook's dedup consolidated are folded into the surviving row
  using that row's own `name_variants_on_slides`; nothing is merged on the
  script's judgement, and unapproved `merge_proposal`s stay in the queue.
- **Stages** — observations at Meetings This Week, Hold / Nurture and
  NewCo / Fellows are dropped, and stages 1–3 are removed from the payload and
  the template, so those three category tabs no longer exist on any basis.
  24 companies in the workbook have a *latest* position in one of them (16
  Meetings This Week, 8 Hold / Nurture); they read at their last diligence rung
  instead, which is why the latest-position tabs still sum to 185.
- **Re-derived counts** — `slide_appearances`, first/last seen, latest stage,
  furthest stage and the bold counts come from the surviving observations, so
  they read *lower* than the same fields in the workbook, which counts
  pre-diligence weeks too: 935 observations of 1,198, 75 companies with a lower
  week count, 150 bold appearances of 152. The funnel matches the workbook
  exactly (182 prelim, 15 deep, 5 negotiate, 6 legal). `stage_jump` and
  `stage_regression` items are re-derived on the screened series with ingest's
  own rule, which drops 293 open review items to 21 — most of them asserted a
  move into a stage the file no longer carries.

One row is adjudicated by hand, in `ADJUDICATED` at the top of the script:
entity 35 is **Eden Tech**, not the phantom `/Eden Tech`. The extractor flagged
it as a line-wrap continuation on the strength of its leading slash and the
workbook kept that spelling; the deal team confirmed it is a real company, so
the screen fixes the name, clears the phantom flag and retires the
`line_wrap_candidate` that raised it. The raw slide spelling survives as an
alias, and the workbook itself still reads `/Eden Tech`.

Two rows in the workbook have Preliminary Diligence slide history but no
`Companies` row, so the screen cannot place them: `Level 12 Bio NewCo` (7
appearances, likely the same company as `Level 12 Bio`/EV0250) and `Lila` (1,
likely `Lila Sciences`/EV0028). Both sit in the workbook's own `Stage History`
sheet. Related: `Attune Neurosci` (EV0017) and `Attune Neurosciences` (EV0080)
are both present — a dedup the workbook did not make.

## Index reach

`scripts/match_drive_index.py` answers the question the other way round from
the one the tab used to ask. The old measure took the 185 diligence companies
and asked which had a Drive folder. This one takes the **index** as the
denominator and asks how much of it the record can see at all:

```bash
python scripts/build_ui.py             # faithful full build
python scripts/screen_diligence.py     # -> the 185-company diligence cohort
python scripts/match_drive_index.py    # -> the Index reach tab
```

`Index, New Deals Companies, v2026-08-28-01.xlsx` lists 982 company folders
under *New Deals / 02. Companies*, each tagged with sector, vertical, a
one-line description and whether it is a portfolio company. Two of them
(`_Temp`, `_Academics`) are scaffolding rather than companies and are excluded,
leaving **980**. Each is looked for in the two records this repo is built from:

| Record | What it is |
| --- | --- |
| slides | the whole deck — 1,018 pages, 153 dated meetings, 2021-08-23 to 2026-08-31 |
| Affinity | the whole Deal Flow export — 2,815 organisations |

**705 of the 980 are visible in one or the other (72%); 275 are visible in
neither.** 425 are on a slide, 601 are in Affinity, 321 are in both. A folder
in neither column is material in storage for a company that never reached a
deal meeting slide and has no CRM record — either genuinely outside the deal
process, or a gap in the two records this pipeline treats as its evidence.
Six of the 275 are marked portfolio companies — `Axoft - Dr. Jia Liu`,
`Biobot.io`, `CFS`, `Sync Computing`, `Syzygy Plasmonics`, `WOHO` — and all
six carry the same folder-modified date, 2023-11-07, which is the bulk
back-fill the index was seeded from rather than anything about the deal. `CFS`
is the one with a `weak` deck hit: the acronym is on six pages, but at three
characters that is not a match this script will assert.

### Reading the slides side

The slides join reads the **deck**, not the database. `slide_observation`
holds 47 of the deck's 153 meetings (2025-10-14 onward), so joining the index
against the loaded population would score a 2021 folder as unseen purely
because the load window opens in 2025. Every row therefore carries the meeting
it was last seen on and whether that is inside the extracted window, so the
narrower reading is still available on the tab. Of the 425 on a slide, 113
resolve to an extracted entity the pipeline holds; 312 are found only in the
deck's text.

Both joins are on name — the index carries no domain and no entity id — so the
tier is carried into the payload and shown on every row:

| Tier | Rule |
| --- | --- |
| `exact` | normalised name == the slide entity or Affinity organisation |
| `alias` | the folder's other recorded name matches |
| `suffix` | equal after dropping corporate/descriptor tokens (`EnCharge` → `EnCharge AI`) |
| `prefix` | one name is a string prefix of the other, ≥8 characters on the shorter side |
| `text` | the name occurs in the deck's text as a whole token sequence |

Sector words are deliberately *not* in the suffix list, and `prefix` needs
eight characters, so `Cetos` reads N against Affinity's `Cetos Water` rather
than being asserted. A tier hitting more than one target is `ambiguous` and
shown as a question, not resolved on the script's judgement — one occurs,
`Plaid Semiconductors` against both `Plaid Semi` and `Plaid Semiconductor`.

Two rules do the real work on the text tier, and both exist because a bad Y is
worse than an N:

- **An occurrence has to be capitalised where it occurs.** Slides capitalise
  company names and prose does not. Without this rule `_Temp` matches 64 pages
  of the word "temp", and `Helix` matches "double helix". Of the 980, two are
  rejected as prose on this rule alone (`Meter` and `Everywhere`) — and
  `resonant link`, lower-cased in the index, still catches `Resonant Link` on
  the slide, which a case-sensitive rule would have missed along with
  `MiraTerra`, `LeadOptik`, `GreenBlu` and `LightLogiq`.
- **A name of three characters or fewer is a `weak` hit, counted apart.**
  `TPL` on one page is as likely to be an acronym in prose as the company.
  Five folders land here (`2Pi`, `6K`, `CFS`, `KdT`, `TPL`); three of them are
  in Affinity anyway.

21 folders qualify themselves in parentheses, so the head name and the
parenthetical are both searched and the row records which one hit:
`Alithia (Vertical GaN)` is on the deck's first pipeline slide as Alithia, and
`Trener Robotics (fka T-robotics)` is on the slides and the Fund III list as
T-Robotics. Ten of the 21 are visible only once the parenthetical is set aside
or searched on its own, so taking the folder name whole would have read them
as unseen.

**The deck stamps three January 2023 meetings as 2022.** Read oldest-first the
title dates must strictly increase; the meetings of 2023-01-03, 01-09 and
01-17 are all filed as 2022 and all sit above a correctly-stamped December
2022. They are corrected by advancing the year until the order holds, and the
corrections are printed rather than assumed. Taking the deck's dates literally
instead folds 34 meetings into one, which would move every "last seen" date
below the break. The deck's text is cached in `data/slide_text.json`, keyed on
the PDF's size and mtime, because extracting 1,018 pages takes ten seconds.

Like `screen_diligence.py` this runs over the *built* interface rather than
rewriting `build_ui.py`, and it is idempotent. `scripts/serve.py` computes the
same join once at startup, so the served page carries the tab too;
`--no-reach` skips it.

## Not done

- Read-only interface. `scripts/build_ui.py` inlines the database into
  `ui/template.html` and writes `ui/index.html`: a single portable file with
  five tabs — **Companies** (every company in each diligence category, on three
  bases: latest position, ever in category, furthest reached; screened to the
  four diligence stages, see above), **Index reach** (one row per New Deals
  folder, and whether the deck or Affinity can see it, see above), **Trends**
  (funnel, most-discussed, coverage, dwell, weekly stage mix, intake rate, and
  the per-company trace), **Review queue**, and **Enrichment**. Served by
  `scripts/serve.py` it also writes: add a company, and edit tags. Editing
  enrichment values, resolving gaps and executing merges are still
  command-line only — those change what the record asserts about a company,
  which needs a provenance and review conversation that tags do not. The §9
  slide generator is not built.
- Extraction is not re-run. `bold_color` needs the PDF, and the deck holds 209
  meetings back to Aug 2021 against the 43 loaded here.
- §7 capture — pass reasons, outcomes, sourcer, valuations, founders,
  non-EV rounds, attendees — has tables and picklists but no rows. The brief is
  right that every week of delay is permanent loss.
- The 293 review items are unresolved by design; resolving them is a person's
  job, and `entity.merged_into` plus `slide_observation_override` are where the
  decisions land.
