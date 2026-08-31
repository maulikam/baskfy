# `tools/migrate-desk` — the desk's SQLite into Baskfy's Postgres

> **D8** — *"The desk's SQLite **migrates** with row-count + checksum assertions; the file is
> archived forever."* (root `CLAUDE.md`)

Built by leaf 1.3.2 of the desk-retirement tree, on the inventory leaf 1.3.1 wrote in
[`docs/DESK-DATA-INVENTORY.md`](../../docs/DESK-DATA-INVENTORY.md). **Read that first.** This
README assumes its findings and does not repeat them.

---

## Two decisions are not this tool's to make. Read these before running it.

### 1. The fork. `--fork-policy` is required and has no default.

There are **three copies** of the desk's database and they are three lineages, not three views
(inventory §1a):

| | Store | Rows | Last write |
|---|---|---|---|
| **A** | `desk@65.0.226.77:~/kite-momentum-rebalancer/data/portfolio.db` | **43,411** | 31 Aug 14:39 — **the record**, live, `DRY_RUN=false` |
| B | laptop `kite-momentum-rebalancer/data/portfolio.db` (archived 0444) | 42,285 | 22 Aug 08:16 |
| C | laptop Postgres `desk` schema, `baskfy-postgres` port 5433 | 42,359 | 22 Aug 08:52 |

Migrating A alone is right for 43,411 of 43,411 rows and loses exactly this, measured by this
tool against the real stores on 31 Aug 2026:

```
plans 7   orders 69   by_source {'B': {'plans': 1, 'orders': 2},
                                 'C': {'plans': 6, 'orders': 67}}
plan_ids  1bd7b94babc6  39efc9eefca7  42c0b852a80c
          9915a620a4b0  9cf46614a665  a71689f13032  cef40037bcc6
duplicated_across_stores  {'39efc9eefca7': {'C': 2}}
```

Seven plans and 69 order lines that exist nowhere on the box. None corresponds to a real order —
they are DRY_RUN-era planning. **That is an argument for discarding them and not a reason to do it
by accident**, so the tool refuses to run until the choice is typed:

| `--fork-policy` | What lands |
|---|---|
| `box-only` | A, and nothing else. B and C are left alone as a file and a schema. Leaf 1.3.1's recommendation, and the cheapest to reverse — the merge can be run later against the same sealed archive. |
| `box-only-quarantine` | A in `<schema>`, **plus** every orphan row in `<schema>_quarantine`, each tagged with the store it came from. The live schema is the box and only the box, so no report or NAV calculation can pick up a plan the desk never ran; nothing is destroyed. **The answer if the decision is not being made today.** |
| `box-plus-orphans` | A, with the orphans merged into the live tables. `version_id` is TEXT and globally unique so plans merge cleanly; `rebalance_orders.id` cannot — see below — so orphan orders are reissued ids above the box's high-water mark and every old→new pair is written into the run report. |

Two things the tool found while implementing this, neither of them in the inventory:

* **`39efc9eefca7` is in B *and* in C**, because C was migrated from B on 22 Aug. Carrying orders
  from both stores would give a 2-order plan 4 orders. The first store on the command line owns a
  plan and supplies its orders; any other store's copy is *counted and reported*
  (`duplicated_across_stores`) and never merged.
* **A tripwire on `fills`.** A fill that exists in a forked store and not on the box would be a
  finding to investigate, not a row to merge, so it aborts the run with the list. Today it is
  empty, as it should be.

`trades`, `daily_runs`, `ops_jobs` and `settings_audit` are **excluded** from orphan carry, with
reasons, in `desk_fork.py`. Short version: none has a key stable enough to distinguish "absent
from A" from "the same row wearing a different id" (§5.2, §5.3).

**Recorded for Maulik. Not decided here.** It is inventory §6 item 2, still open.

### 2. Money: `numeric` (house rule 9) vs `double precision` (M19.3). Both were right.

House rule 9 says *"Money and prices are `numeric`, never `float`."*
`scripts/migrate_to_postgres.py:58` maps `REAL` **and** `NUMERIC` to `DOUBLE PRECISION`, and store
C proves it: **12 money columns across `fills`, `snapshots`, `trades` and `benchmark` are
`double precision` there today.**

M19.3 argues the opposite deliberately, and the argument is good:

> *"Every number the desk has ever computed, displayed or traded on was already a float. Keeping
> `Decimal` here would make the Postgres backend arithmetically different from the record it was
> copied from, and a migration that changes the numbers is not a migration."*

**Decision taken: `numeric`, and M19.3 is satisfied rather than overruled.** The two positions
only conflict if the stored type dictates the arithmetic, and here it does not:

* The value written is `Decimal(repr(float_value))`. `repr()` is the shortest decimal string that
  round-trips a float64 exactly, so `float(Decimal(repr(x))) == x` bit for bit.
* `pg.py:_native` already converts every `Decimal` psycopg returns back to `float` before the
  desk's arithmetic sees it. The desk still computes on the identical float64.
* `desk_assertions.canonical()` renders a `Decimal` as `repr(float(v))` — identical to how it
  renders the SQLite float — so the checksum still proves the copy cell by cell.

Measured, not asserted: **all 9,609 `fills` rows round-trip to the identical float64** through
`numeric`, and `snapshots.nav` comes back exactly (`10431108.87`, `10482471.67`, …).

Reversal is one flag: `--float-money` restores M19.3's literal reading for the whole schema.

**What stays `double precision`, because house rule 9 governs money and prices and these are
neither** — listed explicitly in `desk_pgddl.FLOAT_BY_DESIGN`, never matched by a name heuristic:
`fills.when_ts`, `trades.entry_ts`, `trades.exit_ts`, `rebalance_versions.created_ts` (epoch
clocks); `daily_runs.duration_s`, `ops_jobs.duration_s` (stopwatches); `option_arms.dte_at_entry`
(a count of days). Against the live schema that is **38 columns `numeric`, 7 `double precision`.**

Three more type calls, all made the conservative way and all recorded in `desk_pgddl.py`:
booleans stay `bigint` 0/1 (the desk's SQL compares to `0`/`1`; `boolean` is a code change wearing
a schema change's clothes); `*_json` stays `text` (`jsonb` reorders keys and changes the bytes of
an evidence column); ISO-text dates stay `text` (`timestamptz` needs a stated per-column
assumption that the naive strings are IST and the epoch floats are UTC — §5.6, not this leaf's
call).

---

## Why not `scripts/migrate_to_postgres.py`

Its checksum machinery is genuinely good and is kept here almost verbatim. Its schema generation
is not: it builds `CREATE TABLE` from `PRAGMA table_info` alone and never reads `sqlite_master`'s
index DDL. Store C is that script's actual output. Side by side, measured today:

```
                         desk (old script)   desk_migrated (this tool)
primary keys                      0                    20
unique constraints                0                     3
foreign keys                      0                     2
indexes (pg_indexes)              0                    41
rebalance_orders id IS NULL      67                     0
money columns as float           12                     0   (12 numeric)
```

Each of the five defects is now a **named assertion that fails the run**:

| Inventory | Defect | Assertion here |
|---|---|---|
| §5.5.1 | 67 evidence rows with a NULL primary key | identity columns are `GENERATED BY DEFAULT AS IDENTITY` (implicitly NOT NULL); `no_null_identity_values` |
| §5.5.2 | uniques gone → `INSERT OR IGNORE` is unconditional → house rule 7 broken | `unique_constraints_present`, `primary_keys_present` |
| §5.5.3 | `ux_regime_canonical` not enforced | `ux_regime_canonical_present` — the partial predicate is read from `sqlite_master` and reproduced |
| §5.1 | money as float | `money_is_numeric_not_float` |
| §5.4 | `sqlite_sequence` filtered out; `max(id)+1` is not a substitute | `sequences_carried` — `setval()` from `sqlite_sequence`, so `regime_evaluations` starts at **33** against 26 rows, and `settings_audit` at **7** against 1 |

**Also found and fixed while building this, and not in the inventory:** the old migrator drops
**column DEFAULTs** too. Ten live columns have one (`fills.charges DEFAULT 0`,
`rebalance_orders.filled_qty DEFAULT 0` / `.status DEFAULT 'PENDING'`, `index_series.is_final
DEFAULT 1`, `daily_runs.failed_steps`, and the `regime_*` flags). The desk writes
`INSERT INTO regime_evaluations(evaluation_id, run_id) …` and expects `committed` to be `0`;
without the default that insert fails on NOT NULL or writes a NULL into a flag the reporting code
reads as a boolean. Defaults are carried, and a default expression the tool cannot reproduce
exactly (`CURRENT_TIMESTAMP` — UTC text in SQLite, `timestamptz` in Postgres) is **refused**, not
translated.

---

## The safety rails, enforced in code

`migrate_desk.py apply` will not open a write connection until all of these hold. A rail that
lives only in a checklist is a rail somebody steps over at 23:40 on a Sunday.

1. `--backup-manifest` — a `manifest.json` from `python -m scripts.backup` whose
   `verified.integrity` is `"ok"`. Re-read and re-checked, not trusted.
2. `--archive-copy` — a dated copy that **opens** and passes `PRAGMA quick_check`. Its sha256 goes
   into the report. An archive nobody has read is a hope.
3. That archive must be **outside the repository**. A backup in the working tree dies with the
   working tree.
4. The source's sha256 is taken before and after; a difference fails the run. The source is opened
   read-only, so a difference means something *else* wrote to it and the copy is of a moving
   target.
5. `--require-rehearsal` (on by default) needs a receipt keyed on **this source's sha256 and this
   fork policy**. A rehearsal of `box-only` does not authorise an apply of `box-plus-orphans`;
   they land different rows, which is the whole thing being rehearsed.

And two defaults that make the safe thing the lazy thing:

* **`apply` without `--commit` rolls back.** Every assertion runs, the report is printed, the
  target is untouched. You have to ask for the write.
* Everything happens in **one transaction**. A failed assertion rolls back the whole schema — a
  half-migrated evidence store is worse than none.

---

## Running it

```bash
PG='postgresql://baskfy:…@localhost:5433/baskfy'
ARCHIVE=~/baskfy-safety/desk-migration-2026-08-31/portfolio-A-2026-08-31T1731-IST.db

# 1. rehearse — takes its own copy through SQLite's backup API, migrates it into
#    <schema>_rehearsal, asserts everything, rolls back, writes a receipt.
python tools/migrate-desk/migrate_desk.py rehearse \
    --sqlite "$ARCHIVE" --postgres "$PG" --schema desk --immutable-source \
    --fork-policy box-only-quarantine \
    --secondary-sqlite  B=~/baskfy-safety/sqlite-archive/portfolio-2026-08-22T08-30-IST-FINAL-pre-postgres.db \
    --secondary-postgres C="$PG:desk"

# 2. apply — dry by default; add --commit when the report reads right.
python tools/migrate-desk/migrate_desk.py apply \
    --sqlite "$ARCHIVE" --postgres "$PG" --schema desk --immutable-source \
    --fork-policy box-only-quarantine \
    --backup-manifest ~/baskfy-safety/desk-migration-2026-08-31/verified-backup/20260831-173209/manifest.json \
    --archive-copy "$ARCHIVE" \
    --drop-existing --commit

# 3. verify — read-only, any time later.
python tools/migrate-desk/migrate_desk.py verify \
    --sqlite "$ARCHIVE" --postgres "$PG" --schema desk --immutable-source
```

Exit 0 only if every assertion passed. The report is JSON on stdout, and a copy of it is written
into `<schema>.migration_provenance` alongside the source path, sha256, byte count,
`user_version`, fork policy, money type, row total and schema digest — so the first question
anyone asks of a migrated evidence store nine months later ("which file is this a copy of?") has
an answer inside the database.

### Idempotency (house rule 7)

`apply` is reload-and-replace, not upsert: the SQLite file is the record and the schema is a
projection of it, so a re-run rebuilds the projection wholesale. Proven, not claimed — two applies
and a verify of the live 43,411 rows:

```
apply#1 digest : 5164d72abc506874b2b4c3b929af316d5f0e051f3b4f7a87386cc8041107be75
apply#2 digest : 5164d72abc506874b2b4c3b929af316d5f0e051f3b4f7a87386cc8041107be75
verify  digest : 5164d72abc506874b2b4c3b929af316d5f0e051f3b4f7a87386cc8041107be75
```

An upsert would be machinery for a situation that does not exist and would paper over the one that
does: a row deleted in SQLite would survive in Postgres forever.

---

## What it asserts

Per table, both, and **both fail the run** (`desk_assertions.AssertionLedger.assert_all` raises;
the caller rolls back):

* **`row_count`** — catches a truncated load, a failed `executemany`, a filtered read.
* **`checksum`** — SHA-256 over every cell of every row, order-independent (each row hashed, the
  hashes sorted, the sorted list hashed). Catches a reordered column, a re-rendered float, a NULL
  that became an empty string.

Both sides go through **one** rendering function, `canonical()`, so a difference in how psycopg and
sqlite3 spell a value cannot be mistaken for a difference in the data.

Then, whole-database, all of them run in both `apply` and `verify`:

| Check | What it would catch |
|---|---|
| `nav_series_identical` | the NAV curve changed |
| `index_value_recomputes` | a copy that is byte-faithful but broke the arithmetic (`100 × nav / nav[0]`) |
| `primary_keys_present` / `unique_constraints_present` / `foreign_keys_present` / `indexes_present` | the store-C regression, in four pieces |
| `ux_regime_canonical_present` | "committed exactly once" silently unenforced |
| `no_null_identity_values` | the 67-NULL-primary-key defect |
| `sequences_carried` | the first insert after cutover reissuing a historical id |
| `money_is_numeric_not_float` | house rule 9 |
| `no_dangling_rebalance_orders` | an order pointing at a plan that did not come across |
| `fills_anchor_intact` | the root record collapsing — 9,609 distinct broker `trade_id` in, 9,609 out |

---

## Files

| | |
|---|---|
| `desk_assertions.py` | `canonical()`, `checksum()`, `row_count()`, the verdict ledger. Nothing here warns. |
| `desk_introspect.py` | the whole SQLite schema — columns, PKs, uniques, partial indexes, FKs, `sqlite_sequence`. Refuses what it cannot read exactly. |
| `desk_pgddl.py` | SQLite → Postgres DDL. **The numeric decision lives here**, with M19.3's counter-argument in the module docstring. |
| `desk_fork.py` | **The fork decision lives here.** Three policies, orphan discovery, id remapping, the `fills` tripwire, and why four tables are excluded. |
| `desk_preflight.py` | the five safety rails and the rehearsal receipts. |
| `migrate_desk.py` | `rehearse` / `apply` / `verify`. One transaction, every assertion inside it. |
| `test_migrate_desk.py` | 58 tests. Postgres-backed ones skip without `DESK_TEST_DSN`. |

```bash
DESK_TEST_DSN='postgresql://baskfy:…@localhost:5433/baskfy' \
  kite-momentum-rebalancer/.venv/bin/python -m pytest tools/migrate-desk/ -q
```

---

## Still open — for Maulik, not for the tool

1. **The fork** (above). Inventory §6.2.
2. **`cashflows` is empty and the NAV index depends on it** (inventory §4.1). With zero cashflows
   every `snapshots.index_value` is chained on NAV alone. If external money ever moved and was not
   recorded, the index is wrong and this migration copies the error faithfully. `numeric` does not
   fix an unrecorded flow.
3. **A path between the box and Baskfy.** Shell access to `desk@65.0.226.77` exists from *this
   laptop* only; the Baskfy box's key is bound to a forced command. Until Maulik opens something,
   cutover is a laptop-mediated transfer: pull, archive, rehearse, apply.
4. **`app/analytics/pg.py` still needs the other half of house rule 7.** This tool restores the
   unique constraints; `pg.py:_DDL` still rewrites `INSERT OR IGNORE` / `INSERT OR REPLACE` to a
   plain `INSERT`, which will now raise a `UniqueViolation` instead of silently duplicating. That
   is strictly better — loud beats wrong — but it is a break, and the fix (`ON CONFLICT DO NOTHING`
   / `DO UPDATE`) is in `kite-momentum-rebalancer/`, which this leaf does not own.
5. **`trades` annotations.** 0 of 8,530 rows carry `entry_score` or `exit_reason` today, so
   `trades` is 100% derivable from `fills`. The moment one is set it stops being derivable, and the
   annotation-carrying key `(symbol, entry_ts, qty)` is not unique — 8,530 rows collapse to 5,815
   triples, so a rebuild would re-attach an annotation to the wrong lot. Latent, harmless at zero,
   worth fixing before it isn't.
