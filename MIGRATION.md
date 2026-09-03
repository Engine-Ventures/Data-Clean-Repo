# SQLite → PostgreSQL (Neon) migration

Working notes for the port. This file exists because the reasoning behind
these decisions was reconstructed from scratch once already; every number
below was measured, and the command that measured it is given so it can be
re-measured rather than trusted.

Status legend: **done** / **in progress** / **not started**.

| Phase | File | Status |
|---|---|---|
| 1 | `schema.sql` + `seed.sql` (DDL) | **done, verified on PostgreSQL 17.11** |
| 2 | `src/evpipeline/db.py` | **done, verified on PostgreSQL 17.11** |
| 3 | `src/evpipeline/vocab.py` + `tests/test_vocab.py` + `tests/conftest.py` | **done**, 17/17 tests pass |
| 4 | `src/evpipeline/ingest.py` | not started |
| 5 | `src/evpipeline/validate.py` | not started (import-level fix only) |
| 6 | `src/evpipeline/write.py` | not started |
| 7 | `src/evpipeline/metrics.py` | not started |
| 8 | `README.md` figures | not started |
| — | `scripts/*.py` | **not in the audit order and all broken** — see *Scripts* |

Audit order is deliberate: `db.py` first because it is the thing that is
actually broken (see *Blockers*), `vocab.py` second because phase 4 onward
cannot be tested until the two stage-id producers agree.

---

## Blockers

Resolved in phase 2:

1. ~~**`db.py` feeds the ported `schema.sql` to SQLite**~~ — `db.py:26` was
   `conn.executescript(SCHEMA_PATH.read_text())` and the Postgres DDL did not
   parse there (`sqlite3.OperationalError: near "IDENTITY": syntax error`).
   Rewritten.
2. ~~**`psycopg` is not installed**~~ — `psycopg[binary]==3.2.10` and
   `psycopg-pool==3.2.6` installed into `.venv` and added to
   `requirements.txt` (which is a full `pip freeze`, so `psycopg-binary` and
   `typing_extensions` appear too).

3. ~~**There is no PostgreSQL anywhere in this environment**~~ — resolved.
   `postgresql@17` (17.11, Homebrew) installed and running as a service.

   ```bash
   export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"
   createdb evtest
   psql -v ON_ERROR_STOP=1 -d evtest -f schema.sql
   psql -v ON_ERROR_STOP=1 -d evtest -f seed.sql
   export TEST_DATABASE_URL=postgresql://localhost/evtest   # or a Neon branch
   PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
   ```

   Local Postgres exercises everything except Neon's pooler behaviour and its
   TLS/SNI routing; point `TEST_DATABASE_URL` at a scratch Neon branch to
   cover those.

Still open:

4. **`data/pipeline.db` is not reproducible from this tree** and must not be
   trusted as a baseline — it is a stratified artifact of two different stage
   mappings. See *The 709-row mislabel*. Every "current data" number in this
   file was measured against it, which is fine for the numbers that concern
   row counts and dates, and is exactly the point for the ones that concern
   stage labels.

---

## Porting decisions

Recorded so they are not re-litigated. The full rationale is inline in
`schema.sql`; this is the index.

- **Surrogate keys are `GENERATED ALWAYS AS IDENTITY`.** SQLite's bare
  `INTEGER PRIMARY KEY` was an implicit rowid alias; in Postgres it is a plain
  integer with no default, so every insert omitting the id would fail.
  **9 columns** (not 10 — an earlier count of 10 included the explanatory
  comment line at `schema.sql:17`, not a column):

  `entity.entity_id`, `alias.alias_id`, `slide_observation.observation_id`,
  `slide_observation_override.override_id`, `field_value.field_value_id`,
  `founder.founder_id`, `funding_round.round_id`, `review_item.review_id`,
  `ingest_run.run_id`.

  ```bash
  grep -c 'GENERATED ALWAYS AS IDENTITY' schema.sql   # 10 -- includes the comment
  grep -n 'GENERATED ALWAYS AS IDENTITY' schema.sql | grep -v '^[0-9]*:--' | wc -l   # 9
  ```

- **4 primary keys stay plain `integer`** — one supplied explicitly by
  `seed.sql`, three borrowed from a parent row: `stage.stage_id`,
  `money_value.field_value_id`, `entity_outcome.entity_id`,
  `entity_sourcing.entity_id`.

- **Money is `numeric`, not `real`.** Postgres `real` is float4 (~6
  significant digits) and visibly rounds an eight-figure round size; SQLite's
  `REAL` was 8-byte and hid it. **5 columns**: `money_value.amount_usd`,
  `money_value.amount_local`, `funding_round.amount_usd`,
  `funding_round.pre_money_usd`, `funding_round.post_money_usd`.

- **0/1 integer flags stay integers**, not boolean (`is_bold`, `is_zero`,
  `is_phantom`, `ev_participated`, `is_terminal`): `v_entity_funnel` and
  `v_entity_discussion` `SUM()`/`MAX()` over them and Postgres rejects those
  aggregates on a boolean.

- **Date-shaped TEXT columns are deliberately NOT retyped** (`meeting_date`,
  `field_value.value_text`, `first_meeting`). They are compared against each
  other as text in `ingest.flag_predating_relationships` and
  `validate.check_first_meeting_order`. SQLite compared text to a date and
  returned nonsense; Postgres refuses outright. Retyping means finding and
  fixing every one of those comparisons in the same commit — a separate pass,
  not this one.

- **Timestamps written by the database are `timestamptz DEFAULT now()`.**

- **DDL and seed data are separated.** The controlled vocabularies used to be
  seeded inline in `schema.sql`; they now live in `seed.sql` so the DDL can be
  re-run against a shared Neon branch without fighting over rows. Both files
  are idempotent (`IF NOT EXISTS` / `OR REPLACE`, `ON CONFLICT DO NOTHING`).
  **`create_schema` must therefore apply two files, in order.** This
  separation is also what fixes the mislabel below.

- **`CREATE OR REPLACE VIEW`**, since Postgres has no `IF NOT EXISTS` for
  views. Consequence for re-runs: `OR REPLACE` refuses a definition that
  changes an existing view's column names, types or order, so a future edit of
  that shape needs an explicit `DROP VIEW ... CASCADE` first. Both view
  rewrites in this port preserve column names, types and order.

- **`NULLS NOT DISTINCT` on `idx_obs_grain`** — a fix, not a port.
  `raw_section` is nullable, and under the SQLite index two rows with a NULL
  `raw_section` never collided, so `INSERT OR IGNORE` silently failed to
  dedupe exactly the rows most likely to be re-extracted. No row in the
  current data has a NULL `raw_section`, so this changes nothing today; it
  closes the hole before the next extraction opens it. Requires PG ≥ 15;
  Neon is 16/17.

- **`uq_fv_one_current` is UNIQUE where the SQLite index was not.** This is
  the point of the whole migration. `write_field` supersedes-then-inserts,
  which is a read-modify-write race the moment two people edit the same field
  through the API. Without the constraint, concurrent writes leave two rows
  with `superseded_at IS NULL`, `v_field_current` returns both, and every join
  through it silently double-counts that entity.

- **Override casts are guarded** (`WHEN v.new_value ~ '^-?\d+$'`). SQLite's
  `CAST('blue' AS INTEGER)` quietly returned 0; Postgres raises `invalid input
  syntax for type integer` and takes down `v_observation` and therefore every
  view downstream of it. 3 occurrences, one per numeric override field.

- **Two functional indexes replace `COLLATE NOCASE`**, which has no Postgres
  equivalent: `idx_entity_name_lower` and `idx_alias_text_lower`. The queries
  become `lower(col) = lower(%s)`; without the indexes they seq-scan `entity`
  and `alias` on every write. **Phase 5 owes the query-side rewrite** — the
  indexes are in place but the callers still say `COLLATE NOCASE`.

- **`executescript` → `execute`**, `lastrowid` → `RETURNING`.

- **No `PRAGMA foreign_keys = ON` equivalent is needed or possible.** Postgres
  always enforces foreign keys, which makes the schema strictly stricter than
  the SQLite original, where the PRAGMA was set on connections opened through
  `db.connect()` but not on every path that touched the file.

---

## The 709-row mislabel

**The two producers of `stage_id` disagreed, and 709 `slide_observation` rows
in `data/pipeline.db` currently resolve to the wrong stage name.** This is not
a hypothetical; it ships in `ui/index.html`.

`seed.sql` and the old inline seed say:

| stage_id | name |
|---|---|
| 2 | NewCo / Fellows |
| 3 | Hold / Nurture |

`vocab.py:11-21` said the opposite (`2 = Hold / Nurture`,
`3 = NewCo / Fellows`).

### Why `INSERT OR IGNORE` did not save it

`ingest.seed_vocab` docstring claimed that "seeding the union, with
schema.sql's committed stage_ids winning any conflict, is what keeps both
definitions valid". That is true for the `stage` table and false for the data:

- **`stage`** — `INSERT OR IGNORE` on the `stage_id` PK. The DDL's rows land
  first, so `2 = NewCo / Fellows` survives and vocab's conflicting row is
  discarded. Conflict resolution works.
- **`slide_observation.stage_id`** — written at `ingest.py:496` from
  `vocab.STAGE_BY_NAME[stage_name]` **directly**. It never consults the `stage`
  table or `slide_section_map`, so no conflict resolution applies. Hold /
  Nurture sections got `stage_id = 2`, which the `stage` table names
  *NewCo / Fellows*.
- **`slide_section_map`** — `INSERT OR IGNORE` on the `raw_section` PK, with
  `stage_id` also from `vocab.STAGE_BY_NAME`. Sections the DDL had already
  seeded kept the correct id; sections it had not seeded took vocab's wrong
  one. The result is a map that is **internally inconsistent**:

  | raw_section | stage_id | resolves to | correct? |
  |---|---|---|---|
  | `Hold / Nurture` | 3 | Hold / Nurture | yes |
  | `Frontier Fellows / NewCo:` | 2 | NewCo / Fellows | yes |
  | `FF / TF / EF NewCo:` | 3 | Hold / Nurture | **no** |
  | `NewCo:` | 3 | Hold / Nurture | **no** |

So `slide_section_map` and `slide_observation` disagree with each other about
the same raw section — `Hold / Nurture` maps to 3 in the map and to 2 in every
observation carrying it.

### Blast radius, measured

```
                                                     rows  entities
section 'Hold / Nurture'      -> stage_id 2 -> "NewCo / Fellows"    476   46
section 'FF / TF / EF NewCo:' -> stage_id 3 -> "Hold / Nurture"     102
section 'Frontier Fellows / NewCo:' -> stage_id 3 -> "Hold / Nurture" 131
                                                     ----
                                                      709
```

**57 companies** have a furthest stage of 2 or 3, i.e. their furthest-stage
label is one of the two swapped names and visibly changes when this is fixed
(36 at furthest = 2, 21 at furthest = 3). **63 companies** have a *latest*
stage of 2 or 3.

**No metric moves.** Every threshold in `v_entity_funnel` is `>= 4` or
`= 4..7`, and ranks 2 and 3 are both below Preliminary Diligence, so
conversion rates, reached/observed counts and the diligence cohort are
unaffected. What moves is **labels** — which is worse in one respect: a wrong
number invites checking, a wrong label reads as fact.

Reproduce:

```bash
.venv/bin/python - <<'EOF'
import sqlite3
c = sqlite3.connect('data/pipeline.db'); c.row_factory = sqlite3.Row
for r in c.execute("""SELECT o.raw_section, o.stage_id, s.name, COUNT(*) n
                      FROM slide_observation o JOIN stage s USING(stage_id)
                      WHERE o.stage_id IN (2,3) GROUP BY 1,2 ORDER BY 2"""):
    print(dict(r))
EOF
```

### The fix, and why a test rather than a comment

`seed.sql` is authoritative; `vocab.py` moves to match. The next build
rewrites stored `stage_id`s for these two names.

A comment saying "nothing downstream depends on ranks 2 and 3" is what allowed
this: it was true of the arithmetic and false of the labels, and nothing
enforced the agreement. Phase 3 therefore adds a test asserting
`vocab.STAGES` matches the seeded `stage` table **in both directions** (no
extra rows on either side, ids and names identical), plus `SLIDE_SECTION_MAP`
against `slide_section_map`'s seeded `stage_id`s. The failure mode has to be a
red test, not a mislabelled UI.

---

## `v_stage_transition` and `v_entity_latest_stage`

Two views lagged/ranked over the raw observation grain, where
`ORDER BY meeting_date` is **not a total order**: 74 `(entity_id,
meeting_date)` pairs in the current data carry more than one stage. Both are
now collapsed to one row per `(entity_id, meeting_date)` via `MAX(stage_id)`
before the window function, which makes the ordering key unique and needs no
tiebreaker column.

### Why collapse rather than add a tiebreaker

A tiebreaker picks a winner among rows that should never have been separate
window rows. Flagged review items (`ingest.flag_stage_jumps`:
`delta > 2 OR delta < 0`) measured against `data/pipeline.db`:

| window `ORDER BY` | transitions | review items |
|---|---|---|
| `meeting_date` (as ported — no tiebreaker) | 208 | 175 |
| `meeting_date, observation_id` | 208 | 175 |
| `meeting_date, observation_id DESC` | 220 | 184 |
| `meeting_date, stage_id, observation_id` | 209 | 172 |
| `meeting_date, stage_id DESC, observation_id` | 221 | 192 |
| **collapsed, `MAX(stage_id)` per entity-date** | **93** | **64** |

**The nondeterminism was observed, not just derived.** `data/pipeline.db`
stores **170** `stage_jump` + `stage_regression` rows. Re-executing the same
view definition over the same data — zero `slide_observation_override` rows,
so `v_observation` is a pass-through — yields **175**. Same data, same SQL, two
executions, two answers. That directly contradicts this project's claim that
two builds can be diffed.

Any tiebreaker also leaves the deeper problem intact: **70 of the 74
multi-stage dates are the legitimate agenda-marker dual listing** documented on
`idx_obs_grain`. "Meetings this week" (stage 1) is an agenda marker that
coexists with a funnel position, so every week a prelim-diligence company sat
on the agenda manufactured a spurious `+3` stage-1 → stage-4 jump. Those
artifacts were the majority of the queue: **175 → 64**.

`MAX(stage_id)` is not a new rule. It is already what
`v_entity_funnel.furthest_stage_id` uses and what
`scripts/screen_diligence.py:236` does client-side
(`per[wi] = max(stage, per.get(wi, 0))`), with a comment saying it re-derives
jumps itself *because* this view's numbers were unusable. The view now agrees
with both consumers instead of being worked around by them.

Same-date rows in the output: **0**. A `from_date = to_date` row asserts a
stage move within one slide, which no slide can evidence.

### `v_entity_latest_stage` had the identical bug

`ROW_NUMBER() OVER (PARTITION BY entity_id ORDER BY meeting_date DESC)` is not
a total order either. **14 entities** have a last slide date carrying more than
one stage, so `latest_stage_id` was a coin flip. The worst case is not subtle:
**Axiomatic AI** was last seen 2025-12-15 at both stage 7 (Legal) and stage 1
(agenda marker), so its "latest stage" could render as *Meetings This Week*.
After the collapse it resolves to 7, deterministically.

`v_dwell` was checked and left alone: no `(entity, stage)` pair has more
observations than distinct meetings, so `meetings_at_stage` does not
over-count.

### Verified / not verified

- **Verified**: both view bodies execute; `v_stage_transition` returns 93 rows
  / 64 flagged / 0 same-date rows; `v_entity_latest_stage` returns 498 rows;
  both produce byte-identical result sets across 10 consecutive executions.
- **Not verified**: stability across repeated *end-to-end builds*. Blocked on
  blockers 1 and 2. Owed once phase 2 lands.
- **Not verified**: Postgres-specific syntax against a real server. The view
  logic was validated in SQLite against `data/pipeline.db`; there is no
  Postgres or Neon connection in this environment. `FILTER` and the `WINDOW`
  clause happen to work in SQLite 3.53 too, which is why the bodies ran at
  all. `IDENTITY`, `timestamptz`, `jsonb`, `NULLS NOT DISTINCT`, `~` and
  `::integer` are unexercised.

---

## `v_same_slide_stage_conflict` (new)

The residue the collapse hides by construction. Silently collapsing a real
conflict is the kind of silent write §9 forbids, so it is exposed as its own
view for `ingest` to queue as a `source_conflict` review item (phase 4).

Returns exactly **4 rows, all Ovelle**: 2026-02-23, 03-02, 03-09, 03-16.

Ovelle sat in Hold / Nurture continuously from 2025-11-17, began reappearing
in the HH prelim-diligence sub-section (bold) on 2026-02-23, reached Deep
Diligence 03-16, Negotiate 03-23 and Legal 03-30 through 04-20. **The Hold /
Nurture row stayed on the slide for four meetings after the promotion** and
was then dropped. Deck-authoring lag, not evidence of two stages — and *not*
the agenda-column/thesis-subsection dual listing the README describes, since
03-09 and 03-16 carry no agenda marker at all.

`MAX()` reads the stale-row case correctly by construction (a stale row is
always the lower stage), but a human should confirm it rather than the
pipeline assuming it.

**`ingest.flag_duplicate_listings` structurally cannot catch these**:
`ingest.py:530` requires `COUNT(DISTINCT name_on_slide) > 1`, and Ovelle is
one spelling under two sections.

The `HAVING` counts distinct **funnel** stages (`stage_id > 1`) rather than
filtering on "has no stage-1 row". That distinction matters: 02-23 and 03-02
carry the agenda marker *and* both funnel stages, so the simpler filter finds
only 2 of the 4.

---

## Phase 2 — `db.py`

- **`Row(dict)`** restores `sqlite3.Row`'s contract: subscript by column name
  *or* position. psycopg3's default row factory is a tuple (position only) and
  `dict_row` is a dict (name only); this repo uses both styles freely — 47
  `fetchone()[0]` sites alongside named access throughout — so ~50 call sites
  keep working untouched. Subclassing `dict` is what makes `row.keys()`
  (`metrics.py:104,113`), `dict(r)` (`build_ui.py:96`) and
  `csv.DictWriter(fieldnames=rows[0].keys())` (`audit_coverage.py:159`) work
  for free; only `int` and `slice` subscripting is added.
  Inherited wart, shared with `sqlite3.Row`: selecting the same column name
  twice keeps one mapping entry (the last), though positional access resolves
  both. Alias duplicates in the query.

- **Pooling.** Module-level `psycopg_pool.ConnectionPool`, `min_size=1`,
  `max_size=8`, opened lazily by `pool()`. A fresh TCP+TLS connection to Neon
  is ~100ms of handshake and `scripts/serve.py` re-renders the whole page per
  GET. Small max on purpose: Neon's own connection limits are modest, and
  behind the `-pooler` endpoint there is already a PgBouncer, so a large
  client pool behind a server pool adds queueing rather than throughput.

- **The `-pooler` gotcha, which is the one worth knowing.** psycopg3 silently
  promotes a statement to a server-side prepared statement after
  `prepare_threshold` (default 5) executions. Behind PgBouncer in transaction
  pooling mode the sixth execution can land on a different server session
  where that statement does not exist, and fails with `prepared statement
  "_pg3_0" does not exist`. It therefore **passes every small test and breaks
  under load.** `_configure` sets `prepare_threshold = None` when the host
  contains `-pooler.`, detected via `urlsplit().hostname` rather than a
  substring match on the whole URL (a password could contain `-pooler.`).

- **`sslmode=require` is appended if absent.** Neon terminates TLS at the
  proxy and refuses plaintext, and it needs SNI to route to the right branch —
  libpq sends SNI whenever it negotiates TLS, so requiring sslmode is what
  makes branch routing work as well as what encrypts. Without it the failure
  message does not mention TLS.

- **`connect()` rejects a filesystem path loudly** rather than coercing it.
  Four scripts still pass one; a silent failure there would look like an empty
  database rather than an unported caller.

- **Commits.** Pulled out of `finish_run`. **Kept in `start_run`** — the one
  deliberate exception, flagged for overruling: the run log's purpose is to
  record that a load was *attempted*, including one that crashed, so holding
  the row in a transaction a failure rolls back would destroy the only
  evidence of the attempt. `create_schema` also commits, since a half-applied
  schema is not a useful thing to hand back.

- `row_counts` is wrapped in `psycopg.types.json.Jsonb` — psycopg3 will not
  infer `jsonb` from a bare dict. The old `json.dumps(..., sort_keys=True)` is
  dropped because jsonb normalises key order itself, so sorting no longer buys
  diff stability.

- `SCHEMA_VERSION` `0.1.0` → `0.2.0`. The storage engine changed and
  `ingest_run.schema_version` is the only record of which engine produced a
  given run's row counts.

## Phase 3 — `vocab.py`, and the test that should have existed

`vocab.STAGES` now matches `seed.sql`: `2 = NewCo / Fellows`,
`3 = Hold / Nurture`.

`tests/test_vocab.py` (17 tests) asserts agreement **bidirectionally** — same
ids, same names, no extra rows on either side — for `stage`,
`slide_section_map` (resolved `stage_id` *and* `thesis_code`), and the nine
other picklists, plus `stage_id == rank`, source precedence order, and a
literal pin on the 2/3 pair specifically. Bidirectionality is the whole point:
`INSERT OR IGNORE` seeding the union of two disagreeing definitions is what
hid this for the life of the SQLite database, and a one-way subset check would
have passed throughout. The 2/3 assertion is a literal rather than a derived
comparison because a derived assertion is satisfied by the swap.

Verified without a database by parsing `seed.sql` directly: `stage` matches,
`slide_section_map` keys match with zero mismatches, `stage_id == rank` holds,
thesis codes appear only on stage 4, and all nine other picklists match.

`tests/conftest.py` moved from "a throwaway SQLite file per session" to
per-session **schema** isolation: create `evtest_<random>`, `SET search_path`,
apply the DDL there, `DROP SCHEMA ... CASCADE` on teardown. Creating a
*database* would need privileges a Neon role may not have. New `seeded_conn`
fixture gives DDL + seed data with no workbook, so the schema and write-path
tests run when the raw sources are absent. `report`/`conn` now share one
connection, because the data lives in a schema that teardown drops — a second
connection opened later would not find it without the same `search_path`.

## What executing the DDL actually found

Standing up a real server before phases 4–7 was worth it immediately: it
confirmed the parts that were guesses and found two defects that no amount of
reading would have surfaced.

**Confirmed on PostgreSQL 17.11.** `schema.sql` and `seed.sql` both apply
cleanly and both are idempotent (re-applied, no error). Object counts from
`information_schema`: 27 tables, 9 views, 45 indexes, **9 identity columns**
(settling the 9-vs-10 question from the source of truth rather than from
`grep`), **5 numeric columns**, 7 `stage` rows, 13 `slide_section_map` rows.

The four indexes have exactly the intended definitions, read back from
`pg_indexes`:

```
idx_obs_grain      ... (meeting_date, entity_id, name_on_slide, stage_id, raw_section) NULLS NOT DISTINCT
uq_fv_one_current  ... (entity_id, field) WHERE (superseded_at IS NULL)
idx_entity_name_lower ... (lower(canonical_name))
idx_alias_text_lower  ... (lower(alias_text))
```

And they bite. Each of these is a rejection, not a hypothesis:

| Attempted write | Result |
|---|---|
| Second observation identical but for `raw_section IS NULL` | `duplicate key ... "idx_obs_grain"` — **the hole SQLite left open is closed** |
| Second `field_value` with `superseded_at IS NULL` for one (entity, field) | `duplicate key ... "uq_fv_one_current"` |
| `source = 'Public'` with no citation | `violates check constraint "field_value_check1"` |
| `is_zero = 1` with `value_num = 5` | `violates check constraint "field_value_check"` |

The guarded override cast behaves as designed: an override row setting
`stage_id = 'blue'` makes `v_observation` report the underlying stage (4)
rather than raising `invalid input syntax for type integer` and taking down
every view downstream.

Phase 2 verified end to end: `Row` from a real `cursor.description` (named,
positional, negative, slice, `keys()`, `dict()`), the `fetchone()[0]` pattern,
the pool, and `start_run`/`finish_run` round-tripping `RETURNING`, `Jsonb` and
`now()`.

### Defect 1 — `sslmode=require` forced on hosts that cannot do TLS

`database_url()` appended `sslmode=require` unconditionally. A Homebrew
Postgres has no certificate and rejects the attempt outright:

```
connection failed: server does not support SSL, but SSL was required
```

So the pool was unusable in exactly the environment the test suite runs in —
and this would have been invisible until someone tried to run it locally. Now
appended for **remote** hosts only (`urlsplit().hostname` not in
`{localhost, 127.0.0.1, ::1, ""}`), with an explicit `sslmode` in the URL
always honoured, so a local server that does have TLS can still ask for it.
Neon's guarantee is unchanged.

### Defect 2 — an unreachable `enrichment_priority` member

`tests/test_vocab.py` failed on its first real run, on a table nobody was
looking at:

```
enrichment_priority membership differs.
  only in vocab.py: []
  only in seed.sql: ['P3 - sparse']
```

`seed.sql` carried both `'P3 - sparse'` and `'P3 - sparse record'` at tier 3.
`metrics.enrichment_priority` (`metrics.py:170-184`) can emit exactly five
strings and `'P3 - sparse'` is not one of them — it was **unreachable**, and
`data/pipeline.db` carries the dead row too. Removed from `seed.sql` rather
than kept as a harmless spare: a picklist member no producer can emit makes
the picklist stop being a statement about the possible values.

This is the same failure mode as the 709-row mislabel — two definitions of one
controlled vocabulary drifting apart — with a benign effect instead of a
visible one. It is the reason the test was asked for.

**And it exposed a false claim in the schema.** `schema.sql`'s vocabulary
header said "All enums are FK-enforced; no free text". That is true of 8 of
the 11 tables. `working_group`, `affinity_status` and `enrichment_priority`
have **no FK pointing at them** — they are consumed as
`field_value.value_text`, which has no constraint — so nothing stops a value
outside the picklist from being written, which is precisely how a dead member
survived. The header now says so. Making those three enforceable (a per-field
FK on `field_value`, or a CHECK against the vocabulary) is a separate pass;
until then `tests/test_vocab.py` is the only thing holding them in line.

### Suite baseline after phase 3

`TEST_DATABASE_URL=postgresql://localhost/evtest`, 156 tests collected:

| File | Status |
|---|---|
| `test_vocab.py` | **17 pass** |
| `test_index_reach.py` | **17 pass** |
| `test_lookup.py` | **12 pass** |
| `test_merge_proposals.py` | 13 pass, 9 skip |
| `test_validation.py` | 3 pass, 22 skip |
| `test_anchors.py` | 22 skip (needs the raw workbooks) |
| `test_add_company.py` | **error** — own fixtures, still SQLite (phase 6) |
| `test_tags.py` | **error** — same (phase 6) |

The skips are workbook-dependent, and exactly **one** file is responsible:
`data/raw/` has `EV_Deal_Pipeline_Clean_Dataset_DRAFT.xlsx` and
`affinity_export_2026-09-01.csv` but **not
`EV_Deal_Pipeline_Clean_Dataset_v2_DEDUPED.xlsx`**, and `_require_sources`
skips on any one missing. So **phase 4 cannot be verified end to end until
that workbook is restored** — `ingest.build` is the thing under test and its
deduplicated input is absent. Its component functions can still be tested
against hand-built fixtures, and `flag_stage_jumps` / the new
`v_same_slide_stage_conflict` wiring can be tested against synthetic
observations, which is how the collapsed review-item counts will be pinned.

## Decision owed before phase 4 — placeholders

SQLite's `?` is not Postgres's `%s`, and there are **~58 parameterised query
sites** across `ingest.py`, `validate.py`, `write.py` and `metrics.py`.

Two options; **taking the first** unless overruled:

1. **Rewrite every query.** Explicit, greppable, no runtime surprises. It is
   the bulk of the mechanical work in phases 4–7.
2. **A translation shim in `db.py`** rewriting `?` → `%s` per query. Rejected:
   it must not touch a `?` inside a string literal, and it collides with
   psycopg's own `%` handling — a literal `%` in any query would then need
   doubling, silently, everywhere. `Row` was worth it because row access has
   no such ambiguity; SQL text does.

Other SQLite-isms phases 4–7 must convert, counted:

| Pattern | Sites | Becomes |
|---|---|---|
| `?` placeholders | ~58 | `%s` |
| `INSERT OR IGNORE` / `OR REPLACE` | 16 in `ingest.py`, 1 in `write.py` | `ON CONFLICT ... DO NOTHING` / `DO UPDATE` |
| `cur.lastrowid` | 6 (+1 in tests) | `RETURNING` |
| `GROUP_CONCAT` | 2 | `string_agg` (note: needs an explicit delimiter and `DISTINCT` placement differs) |
| `datetime('now')` / `strftime` | 5 | `now()` / `to_char` |
| `COLLATE NOCASE` | 2 in `validate.py`, 1 in `build_ui.py` | `lower() = lower()`, backed by the two functional indexes |
| `sqlite3.Connection` type hints | 6 modules | `psycopg.Connection` |
| `sqlite3.Row` / `row_factory` | 4 sites | `db.row_factory` |

`ingest.seed_vocab` is a phase-4 special case: now that `seed.sql` owns the
vocabularies and `create_schema` applies it, `seed_vocab` is redundant. Its
docstring is also now actively wrong — it claims "seeding the union, with
schema.sql's committed stage_ids winning any conflict, is what keeps both
definitions valid", which is the reasoning that produced the 709-row mislabel.
Delete the function, or reduce it to an assertion that the seed ran.

## Scripts — outside the audit order, and all currently broken

Not in the agreed phase list, but they are the actual entry points and none of
them will run:

| Script | Problem |
|---|---|
| `scripts/build_ui.py` | `sqlite3.connect(DB)`, `sqlite3.Row`, `COLLATE NOCASE` at line 149 |
| `scripts/serve.py` | `sqlite3.connect(DB)` in `render_page`, `connect(DB)` with a path, catches `sqlite3.Error` |
| `scripts/build_db.py` | drives `ingest.build`; also `--force` assumes a file it can delete |
| `scripts/audit_coverage.py` | `sqlite3.Row`, path-based connect |
| `scripts/match_drive_index.py` | `import sqlite3`, path-based connect |
| `scripts/screen_diligence.py` | reads the built payload, not the DB — probably unaffected |

`build_db.py --force` needs rethinking specifically: "always build into a
fresh file, so a load can be repeated and two builds diffed" was a
file-per-build guarantee. The Postgres equivalent is a schema per build (what
`conftest.py` now does) or a Neon branch per build; deleting a file is no
longer the primitive. This is worth a decision rather than a translation.

## Figures that go stale and must be updated (phase 8)

`README.md` is wrong in three places, one of which was already wrong before
this port:

| Location | Says | Actual |
|---|---|---|
| `README.md:211` | 293 open review items | **276** in `data/pipeline.db` today; **170** after the collapse |
| `README.md:274` | screened queue drops 293 → 21 | recompute; the 293 input never matched the DB |
| `README.md:413` | "The 293 review items are unresolved by design" | same |

Arithmetic for the post-collapse figure: 276 open − 170 derived
(`stage_jump` 102 + `stage_regression` 68) + 64 collapsed = **170**.

`scripts/screen_diligence.py` re-derives `stage_jump` / `stage_regression`
itself from the screened series, so the *served* queue figure moves by less
than the stored one — it must be recomputed from a real build, not adjusted on
paper.

The stage-swap fix additionally changes the furthest-stage **distribution
labels** for 57 companies, which the README's stage tables report.
