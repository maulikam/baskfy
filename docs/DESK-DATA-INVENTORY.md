# The desk's data, inventoried — where the record is, what is in it, and what must survive

Leaf 1.3.1 of the desk-retirement tree. **Read-only throughout**: nothing in this document was
produced by a statement that could write. Gate ledger and the proof are in §7.

Measured **31 Aug 2026, 17:03–17:12 IST**, against the live box.

---

## 1. Where the desk's live record actually lives

**SQLite. On the trading box. One file.**

| Fact | Value | Evidence |
|---|---|---|
| Host | `65.0.226.77` (`desk.modelbasket.in`), AWS Lightsail Mumbai, hostname `ip-172-26-14-51` | `ssh momentum-desk-app 'hostname'` → `ip-172-26-14-51`; `~/.ssh/config` → `Host momentum-desk-app / HostName 65.0.226.77 / User desk` |
| Engine | **SQLite 3, WAL, schema `user_version = 13`** | `pragma user_version` → `13`; `app/analytics/db.py:487` `SCHEMA_VERSION = 13` |
| "Database name" | a file: `/home/desk/kite-momentum-rebalancer/data/portfolio.db`, 6,295,552 bytes, mtime `2026-08-31 14:39:37 +0530`, sha256 `22a38d53…f283d1b` | `stat -c "%y %s"`, `sha256sum` |
| Integrity | `pragma quick_check` → `ok` | run under `immutable=1` |
| Why not Postgres | The box has **no Postgres at all**: `command -v psql` → not found, `ss -ltn` shows no listener on 5432/5433, no Docker. | measured on the box |
| Backend selector | `app/analytics/db.py:488` `DB_BACKEND = os.getenv("DESK_DB_BACKEND", "sqlite")`. The box's `.env` **does not set it** — only `DRY_RUN=false` is in that region of the file. So the code default applies. | `grep -oE '^(DESK_DB_BACKEND\|DESK_DB_SCHEMA\|DB_PATH\|DRY_RUN)=' .env` on the box returned `DRY_RUN` only |
| Path selector | `app/config.py:252` `DB_PATH = os.getenv("DB_PATH", "data/portfolio.db")`, resolved relative to `WorkingDirectory=/home/desk/kite-momentum-rebalancer` | `deploy/systemd/momentum-web.service` |
| Live? | Yes, and trading live. `momentum-web`, `momentum-daily.timer`, `momentum-backup.timer` all `active`. Last daily run **31 Aug 2026 14:37 IST**, outcome `ok`. `DRY_RUN=false`. | `systemctl is-active`, `daily_runs` row 26 |

`app/analytics/pg.py` exists and works, but it is **not what the box runs**. It is the M19 cutover
adapter, and the cutover it performed was on the *developer's laptop*, not on the trading box —
`docs/00-merge-status.md:359`:

> **The desk reads and writes Postgres locally.** The code default is still `sqlite`, because the box
> has no Postgres — see M19.2.

`deploy/README.md` ("Why SQLite, and not Postgres") is the box's standing decision and it has not
been revisited: *"Postgres would add a service to run, patch, monitor and back up on the same box …
in exchange for nothing measurable today."*

### 1a. There are three copies of this database, and they have forked

This is the single most important finding in this document.

| # | Store | Engine | Rows | Last written | Status |
|---|---|---|---|---|---|
| A | `desk@65.0.226.77:~/kite-momentum-rebalancer/data/portfolio.db` | SQLite | **43,411** in 19 tables | 31 Aug 2026 14:39 IST | **THE RECORD.** Live, trading, `DRY_RUN=false` |
| B | laptop `kite-momentum-rebalancer/data/portfolio.db` | SQLite | 42,285 in 19 tables | 22 Aug 2026 08:16 IST | Frozen at the M19 cutover; archived 0444 as `~/baskfy-safety/sqlite-archive/portfolio-2026-08-22T08-30-IST-FINAL-pre-postgres.db` |
| C | laptop `baskfy-postgres` (Docker, host port 5433), db `baskfy`, schema `desk` | Postgres 16 / Timescale | 42,359 in 20 tables | 22 Aug 2026 08:52 IST | M19's cutover target. What the laptop's desk writes when `DESK_DB_BACKEND=postgres` |

The laptop's `.env` carries `DESK_DB_BACKEND=postgres` and `DESK_DB_SCHEMA=desk`; the DSN default is
`app/analytics/db.py:532` → `postgresql://baskfy:<redacted>@localhost:5433/baskfy`.

**They are not three views of one record. They are three lineages:**

- **A and B share history only up to 19 Aug 2026 ~13:07 IST.** The box holds 12 of the laptop's 13
  `rebalance_versions`; `39efc9eefca7` (created 19 Aug 18:17 IST, note `scan Investing_001 (1).csv`)
  exists **only on the laptop**. The box then went on to create 23 plans the laptop has never seen.
- **C holds 6 plans that exist nowhere else** — `cef40037bcc6`, `42c0b852a80c`, `a71689f13032`,
  `9cf46614a665`, `9915a620a4b0`, `1bd7b94babc6`, all created 22 Aug 08:28–08:52 IST, i.e. *after*
  the cutover flipped the laptop's backend and after the forever archive was taken at 08:30. Checked
  against the box: **none of the six is there.** They carry 67 `rebalance_orders` between them.
- **`trades.id` does not mean the same thing in A and B.** The box has 7,862 trades with `id ≤ 11831`
  where the laptop has 8,198 with that same maximum id. This is not corruption — see §5.2: `trades`
  is DELETEd and re-INSERTed wholesale per symbol on every tradebook rebuild, so AUTOINCREMENT
  re-issues ids differently on each machine.
- `rebalance_orders` **does** line up: the box holds exactly 133 rows with `id ≤ 187`, matching the
  laptop's 133 (`min 55, max 187`).

**Consequence for the migration: A is the source of truth and nothing else is.** B and C are to be
archived as historical artefacts, not merged. The one thing that would be lost by ignoring them is
plan `39efc9eefca7` and C's six cutover-morning plans — 7 plans and 67 orders of DRY_RUN-era
planning, none of which corresponds to a real order on the box. Decide that explicitly rather than
by omission.

---

## 2. Every table, with its live row count

**19 tables**, plus SQLite's internal `sqlite_sequence` (§5.4) = 20 objects in `sqlite_master`.
Total **43,411 rows**. Counts read 31 Aug 2026 17:03 IST.

| # | Table | Live rows (box) | Key | Class (§4) |
|---|---|---|---|---|
| 1 | `benchmark` | 3,344 | PK `(index_name, date)` | derivable |
| 2 | `breadth_readings` | 1 | PK `(as_of_date, universe_id)` | **unrebuildable** |
| 3 | `cashflows` | 0 | `id` + UNIQUE `(date, amount, type)` | **unrebuildable** (empty — §4.1) |
| 4 | `corporate_actions` | 1 | `id` + UNIQUE `(symbol, kind, ex_date)` | recoverable (§4.2) |
| 5 | `daily_runs` | 26 | `id` only | operational audit |
| 6 | `fills` | 9,609 | PK `trade_id` (broker's id) | **unrebuildable — the root record** |
| 7 | `index_series` | 21,308 | PK `(index_name, date)` | derivable |
| 8 | `ops_jobs` | 61 | `id` only | operational audit |
| 9 | `option_arms` | 0 | PK `arm_id` | frozen lab (D4) |
| 10 | `option_variants` | 2 | PK `variant_id` | frozen lab (D4) |
| 11 | `rebalance_orders` | 425 | `id` + FK `version_id` | **unrebuildable** |
| 12 | `rebalance_versions` | 35 | PK `version_id` | **unrebuildable** |
| 13 | `regime_book_snapshots` | 0 | PK `evaluation_id` | **unrebuildable** |
| 14 | `regime_evaluations` | 26 | UNIQUE `(evaluation_id, run_id)`; partial UNIQUE on `evaluation_id WHERE run_id='canonical'` | **unrebuildable** |
| 15 | `regime_exposure` | 26 | `id` + UNIQUE `(evaluation_id, observed_at)` | **unrebuildable** |
| 16 | `settings` | 1 | PK `key` | **unrebuildable** (trivially small) |
| 17 | `settings_audit` | 1 | `id` only | **unrebuildable** (trivially small) |
| 18 | `snapshots` | 15 | PK `date` | **unrebuildable** |
| 19 | `trades` | 8,530 | `id` — **unstable, see §5.2** | derived from `fills` |
| — | **TOTAL** | **43,411** | 19 tables | — |

Corroboration, independent of my read: the box's own `momentum-backup.timer` ran at 19:15 on 30 Aug
and its manifest records `integrity: ok, snapshots 15, fills 9609, trades 8530, rebalance_orders 422,
breadth_readings 1, regime_evaluations 25`. My 31 Aug figures differ only where the desk has since
written (`rebalance_orders` 422→425, `regime_evaluations` 25→26), which is what a live box should
look like.

### Spans and shape of the live data

| Table | Span | Detail |
|---|---|---|
| `trades` | entries 2023-08-31 → 2026-08-25, exits → 2026-08-25 | 441 open lots; **0 rows carry `entry_score` or `exit_reason`** |
| `fills` | 2023-08-16 → 2026-08-25 | 9,045 `console_csv` + 564 `kite_api` |
| `snapshots` | 2026-08-10 → 2026-08-28 | 15 sessions, no NULL `index_value` |
| `daily_runs` | run 1 (15 Aug) → run 26 (31 Aug 14:37, `ok`) | |
| `index_series` | 2005-01-03 → 2026-08-31 | 4 indices: NIFTY 50 (5,373), NIFTY 500 MOMENTUM 50 (5,312), NIFTY MIDCAP 150 (5,311), NIFTY SMLCAP 250 (5,312) |
| `benchmark` | 2020-01-01 → 2026-08-31 | 3 series: NIFTY 500 (1,655), NIFTY500MOMENTM50 (1,656), NIFTY200MOMENTM30 (33) |
| `rebalance_versions` | 2026-08-16 15:47 → 2026-08-31 09:09 (UTC) | 35 plans |
| `regime_evaluations` | — | **all 26 are previews; 0 rows have `run_id='canonical'`** |

---

## 3. Drift: live vs the 2026-08-22 snapshot

The reference snapshot is store **B** — the laptop's SQLite, frozen 22 Aug 08:16 IST, archived as the
M18/M19 "forever" copy. **+1,126 rows across 11 tables in nine days.**

| Table | Live (A, 31 Aug) | Snapshot (B, 22 Aug) | Drift | What produced it |
|---|---|---|---|---|
| `trades` | 8,530 | 8,198 | **+332** | not 332 new trades — the FIFO book was rebuilt from new fills; the whole table is re-derived (§5.2) |
| `fills` | 9,609 | 9,262 | **+347** | real broker fills captured 20–25 Aug by `momentum-daily` + live rebalances |
| `rebalance_orders` | 425 | 133 | **+292** | 22 plans' worth of order lines |
| `rebalance_versions` | 35 | 13 | **+22** | plans built on the box 20–31 Aug (weekly rebalance + previews) |
| `ops_jobs` | 61 | 23 | **+38** | ops-page and scheduled job records |
| `index_series` | 21,308 | 21,276 | **+32** | 4 indices × 8 sessions (20, 21, 24–28, 31 Aug) |
| `benchmark` | 3,344 | 3,328 | **+16** | 2 PRI series × 8 sessions |
| `daily_runs` | 26 | 12 | **+14** | one row per 18:30 collection + page-triggered runs |
| `regime_evaluations` | 26 | 13 | **+13** | one preview per daily run |
| `regime_exposure` | 26 | 13 | **+13** | one exposure observation per evaluation |
| `snapshots` | 15 | 8 | **+7** | EOD NAV for 20, 21, 24, 25, 26, 27, 28 Aug (7 sessions; 31 Aug's was correctly *skipped* at 14:37 as pre-close) |
| `breadth_readings` | 1 | 1 | 0 | needs `--scan`; never invented from a stale reading |
| `cashflows` | 0 | 0 | 0 | still empty (§4.1) |
| `corporate_actions` | 1 | 1 | 0 | the desk's own table, distinct from the screener's 289 |
| `option_arms` | 0 | 0 | 0 | frozen (D4) |
| `option_variants` | 2 | 2 | 0 | frozen (D4) |
| `regime_book_snapshots` | 0 | 0 | 0 | never written |
| `settings` | 1 | 1 | 0 | one row, `REGIME_ENABLED=true` |
| `settings_audit` | 1 | 1 | 0 | risk ceilings moved to `.env` at M4, so no new rows |
| **TOTAL** | **43,411** | **42,285** | **+1,126** | |

**The drift is coherent and fully explained**: eight NSE sessions (20, 21, 24, 25, 26, 27, 28, 31 Aug
— 22/23 and 29/30 are weekends) of automated collection plus the desk's ordinary weekly rebalancing.
Every increment lands in a table that the 18:30 daily job or the rebalance flow writes, and every
table that only a human action touches is unchanged. Note that `snapshots` gained 7 rather than 8:
31 Aug's run at 14:37 declined to store an intraday NAV as the day's final value — the desk behaving
correctly, not a gap.

**The drift also proves the migration cannot be a one-shot copy of an old file.** 42,285 is not the
number to migrate; it was never the box's number. Any migration must re-read A at cutover, and A
grows every trading evening at 18:30.

### Drift in store C (the laptop's Postgres) — a separate, smaller problem

| Table | C (Postgres) | B (SQLite archive) | Drift |
|---|---|---|---|
| `rebalance_versions` | 19 | 13 | +6, all on 22 Aug 08:28–08:52 IST, all post-cutover |
| `rebalance_orders` | 200 | 133 | +67 |
| `schema_version` | 1 | n/a (SQLite uses `PRAGMA user_version`) | +1 table |
| everything else | identical | identical | 0 |
| **TOTAL** | **42,359** | **42,285** | **+74** |

Those 67 rows expose a live defect — see §5.5.

---

## 4. Classification: what needs a checksum and a forever archive, and what does not

D8 requires row-count + checksum assertions and a permanent archive. Applying that to all 43,411
rows is cheap enough to just do, but the classification decides what a *failed* assertion means: for
group 1 it is a stop-the-migration event; for group 2 it is a note and a re-fetch.

### Group 1 — unrebuildable evidence. Checksum, assert, archive forever. 10,181 rows.

`deploy/README.md` states the desk's own version of this table, and I have kept its judgements.

| Table | Rows | Why it cannot be rebuilt |
|---|---|---|
| `fills` | 9,609 | **The root of the entire record.** Zerodha's `/trades` is flushed nightly and kiteconnect exposes no historical tradebook. 9,045 of these rows came from a one-time Zerodha Console CSV export that cannot be re-cut for periods now purged; the other 564 were captured live and are gone from the broker. Lose this and the strategy has no track record. |
| `snapshots` | 15 | `kc.margins()` has no history. A lost EOD row is a lost day of the NAV curve, permanently. |
| `rebalance_versions` | 35 | What was decided, and on what constituents and weights. |
| `rebalance_orders` | 425 | What was planned versus what filled, with the broker `order_id` for reconciliation. |
| `regime_evaluations` | 26 | The regime decision audit — inputs, reasons, config hash, algorithm version. |
| `regime_exposure` | 26 | Observed exposure against the cap at each evaluation. |
| `regime_book_snapshots` | 0 | Empty, but in this class if it ever fills. |
| `breadth_readings` | 1 | Needs a scan of a universe as it stood on a past date. Not re-derivable. |
| `cashflows` | 0 | External money in/out. Empty today — see §4.1. |
| `settings` | 1 | The one stored override (`REGIME_ENABLED=true`). |
| `settings_audit` | 1 | Who changed a setting, when, from what. |
| `corporate_actions` | 1 | In the gate's list; in practice recoverable — see §4.2. |
| `daily_runs` | 26 | Not evidence of a trade, but not reconstructible either: it is the only record of which collections ran and which failed. Cheap; keep it. |
| `ops_jobs` | 61 | Same. |
| `option_variants` | 2 | Frozen options lab (D4: frozen, not deleted). Keep verbatim, do not interpret. |
| `option_arms` | 0 | Same. |

### Group 2 — derivable. Row-count assertion is enough; no forever archive required. 24,652 rows.

| Table | Rows | How it is rebuilt | Caveat |
|---|---|---|---|
| `index_series` | 21,308 | Re-fetchable from Kite historical (`app/analytics/index_cache.py`) | Goes back to 2005-01-03. At Kite's ~3 req/s and needing the paid historical tier (NEEDS-MAULIK 6, unresolved), "re-fetchable" is true but is an evening's work, not a button. Copy it; just do not treat a mismatch as data loss. |
| `benchmark` | 3,344 | Re-fetchable from Kite / NSE (`app/analytics/benchmark.py`) | Currently PRI. The daily job's own step text says *"PRI — TRI is authoritative"*, so this table is already known to be the second-best series. |
| `trades` | 8,530 | **Derived from `fills`** by FIFO rebuild — see §5.2 | Not counted in the group-2 total above, because it is neither: see below. |

### `trades` is the interesting case — derived today, evidence tomorrow

`app/analytics/tradebook.py:_write_lots` DELETEs every row for a symbol and re-INSERTs the FIFO
rebuild. The table is a *projection of `fills`*, recomputed on every import and every live capture.
Two columns are not derived — `entry_score` and `exit_reason`, strategy annotations the broker
cannot know, which the rebuild carries across by `(symbol, entry_ts, qty)`.

**Measured on the live box: 0 of 8,530 rows carry either annotation.** So today `trades` is 100%
derivable from `fills`, and the honest migration is to carry `fills` with a checksum and let
`trades` be recomputed and then *compared*. That comparison is a far stronger correctness test than
copying the table would be.

Two things to record before this stops being true:
1. The moment anyone sets an `entry_score` or `exit_reason`, `trades` becomes partly unrebuildable.
2. The annotation-carrying key `(symbol, entry_ts, qty)` is **not unique**: 8,530 rows collapse to
   5,815 distinct triples. A rebuild would re-attach an annotation to the wrong lot. Latent, harmless
   while the count is zero, and worth fixing before it isn't.

### 4.1 `cashflows` is empty, and the NAV index depends on it

`db.py`'s header: *"The portfolio index (base 100 at inception) is DERIVED, never accumulated:
`rechain_index()` recomputes the whole series from stored NAVs + cashflows in date order."* With zero
cashflows, every `snapshots.index_value` is chained on NAV alone. All 15 rows have a non-NULL
`index_value`, so the series is complete on its own terms — but if any external money ever moved in
or out and was not recorded, the index is wrong and the migration would faithfully copy the error.
Flag for Maulik; not something a migration can settle.

### 4.2 `corporate_actions` — 1 row on the desk, 289 in the screener

The gate lists this as unrebuildable. On the desk it holds **one** row. The screener's own
`corporate_action` table went 4 → 289 at M28 (NEEDS-MAULIK item 4, closed). These are different
tables in different products; the desk's single row is trivially checksummable, and if it were ever
lost the screener's table is the better source. Treat as unrebuildable (it costs nothing) but do not
block on it.

---

## 5. Schema notes that will matter for the migration

Full DDL was read from the live box (`sqlite_master`). The important points:

### 5.1 Every money and price column is SQLite `REAL`, and the existing migrator lands them as `double precision`

`scripts/migrate_to_postgres.py:58` `_TYPE_MAP` maps `REAL → DOUBLE PRECISION` **and**
`NUMERIC → DOUBLE PRECISION`. Verified in store C: `trades.entry_price`, `trades.pnl`, `fills.price`,
`fills.charges`, `snapshots.nav/invested/cash/index_value`, `cashflows.amount`,
`rebalance_orders.planned_ref_price/avg_fill_price`, `benchmark.close/tri` are all `double precision`
in Postgres today.

That is **directly contrary to root house rule 9** — *"Money and prices are `numeric`, never
`float`"* — and it is not an oversight the migrator made carelessly. M19.3 argues the opposite case
deliberately: SQLite REAL has been the system of record since the desk existed, so *"every number the
desk has ever computed, displayed or traded on was already a float. Keeping `Decimal` here would make
the Postgres backend arithmetically different from the record it was copied from, and a migration
that changes the numbers is not a migration."*

Both are right, and the conflict is real, so it has to be decided rather than inherited:

- Landing as `numeric` satisfies the house rule and is what Baskfy's own tables do, but the
  checksum over the copied values will not match a checksum over the floats unless the canonical
  rendering is pinned (`migrate_to_postgres.py:85` uses `repr(float(v))`, which round-trips exactly).
- Landing as `double precision` reproduces the desk bit-for-bit and imports a rule violation into
  Baskfy's database.

Recommendation for the migration-design leaf: **`numeric` in the Baskfy tables, with the float
`repr()` retained in the assertion layer** — assert on `repr(float(numeric_value))` so the checksum
still proves the copy, while the stored type obeys the house rule. Full list of affected columns:
`benchmark.close`, `benchmark.tri`, `breadth_readings.pct_above_20dma`, `breadth_readings.coverage_pct`,
`cashflows.amount`, `corporate_actions.ratio_new`, `corporate_actions.ratio_old`,
`daily_runs.duration_s`, `fills.when_ts`, `fills.price`, `fills.charges`, `index_series.open/high/low/close`,
`ops_jobs.duration_s`, `rebalance_orders.planned_ref_price`, `rebalance_orders.avg_fill_price`,
`rebalance_versions.created_ts`, `regime_evaluations.breadth_pct`, `regime_evaluations.breadth_coverage_pct`,
`regime_exposure.actual_equity_pct/target_equity_cap_pct/exposure_gap_pct/pending_buy_pct/pending_sell_pct`,
`snapshots.nav/invested/cash/index_value`, `trades.entry_ts/exit_ts/entry_price/exit_price/entry_score/pnl/costs`,
plus the frozen `option_arms.*` numerics.

### 5.2 `trades.id` is not a key. Nothing may join on it.

`AUTOINCREMENT`, but the table is DELETE-and-reinsert per symbol on every rebuild
(`tradebook.py:385`). Proof: the box has 7,862 rows with `id ≤ 11831`; the laptop has 8,198 rows with
the same maximum. Same history, different ids. Anything downstream that stored a `trades.id` is
already holding a stale pointer.

The stable identifier in this database is **`fills.trade_id`** — the broker's own fill id, TEXT
PRIMARY KEY, 3–9 characters, 9,609 distinct. Anchor the migration on it.

### 5.3 Tables with no stable key at all

| Table | Situation |
|---|---|
| `trades` | surrogate id is unstable (§5.2); natural key `(symbol, entry_ts, qty)` is **not unique** — 8,530 rows → 5,815 triples |
| `daily_runs` | `id` only. Re-importing duplicates silently. |
| `ops_jobs` | `id` only. Same. |
| `settings_audit` | `id` only. Same. |
| `cashflows` | has `UNIQUE(date, amount, type)` — but `db.py` documents the trade-off: *"two genuinely identical flows on the same day collapse to one"* |

Every other table has a declared PK or UNIQUE that survives the move unchanged, including
`regime_evaluations`' partial unique index (`ux_regime_canonical … WHERE run_id = 'canonical'`),
which Postgres supports natively.

### 5.4 Implicit rowid dependency: `sqlite_sequence` is not migrated

Nine tables use `INTEGER PRIMARY KEY AUTOINCREMENT`, which is a rowid alias whose high-water mark
lives in SQLite's internal `sqlite_sequence`. Live values on the box:

| Table | `sqlite_sequence.seq` |
|---|---|
| `trades` | 12,546 |
| `rebalance_orders` | 479 |
| `regime_evaluations` | 32 |
| `regime_exposure` | 32 |
| `ops_jobs` | 61 |
| `daily_runs` | 26 |
| `settings_audit` | 6 |
| `corporate_actions` | 1 |

`migrate_to_postgres.py:110` `sqlite_tables()` filters `name NOT LIKE 'sqlite_%'`, so **none of these
counters is carried.** Note `regime_evaluations` seq 32 against 26 live rows and `settings_audit` seq 6
against 1 — rows have been deleted, so `max(id) + 1` is *not* a safe substitute. The migration must
carry `sqlite_sequence` explicitly and `setval()` each Postgres sequence, or the first insert after
cutover collides with a historical id.

### 5.5 The existing migrator drops every key and index — and it is already causing silent corruption

Measured against store C, the actual output of `scripts/migrate_to_postgres.py`:

```
select count(*) from pg_indexes where schemaname='desk';   ->  0
```

**Zero indexes. Zero primary keys. Zero unique constraints. Zero foreign keys.** The migrator builds
`CREATE TABLE` from `PRAGMA table_info` and copies rows; it never reads `sqlite_master`'s index DDL.
Consequences, all of them live in store C right now:

1. **`desk.rebalance_orders` has 67 rows with `id IS NULL`** — every row written since the M19
   cutover. `id` came across as a nullable `bigint` with no default, no identity, no PK, so the
   desk's `INSERT INTO rebalance_orders(version_id, symbol, …)` inserts NULL and Postgres accepts it.
   On SQLite those same 67 rows would have had ids 188–254. The primary key of an evidence table has
   been quietly discarded for nine days and nothing noticed.
2. **Idempotent ingestion is broken on the Postgres backend.** `pg.py:_DDL` translates
   `INSERT OR IGNORE INTO` and `INSERT OR REPLACE INTO` to plain `INSERT INTO`. With the unique
   indexes gone, both become unconditional inserts. House rule 7 — *"Re-running any day's job
   produces identical rows"* — does not hold there. It has not bitten yet only because store C has
   been idle since 22 Aug.
3. `ux_regime_canonical` — the partial unique index that enforces "committed exactly once" — is not
   enforced in Postgres.
4. `snapshots.date` PK is not enforced; a re-run could write two NAVs for one day.

**The desk-retirement migration must not reuse this migrator as-is.** Its checksum machinery
(`canonical()` / `checksum()` at lines 75–107, order-independent, one rendering called for both
sides) is genuinely good and worth keeping. Its schema generation is not.

### 5.6 Timestamps have no timezone, and there are two conventions in one database

- **Epoch floats**: `trades.entry_ts`, `trades.exit_ts`, `fills.when_ts`, `rebalance_versions.created_ts`
  — SQLite `REAL`, seconds since epoch, UTC-based.
- **ISO text, no offset**: `snapshots.date`, `benchmark.date`, `index_series.date`,
  `daily_runs.ran_at` / `session_date`, `ops_jobs.started_at` / `finished_at`, `settings.updated_at`,
  `regime_*.created_at` / `observed_at`, `breadth_readings.as_of_date`, `corporate_actions.ex_date`.

The box runs `TZ=Asia/Kolkata` (all three systemd units set it), so the naive ISO strings are IST and
the epoch floats are UTC. Moving to `timestamptz` requires stating that assumption once, in writing,
and applying it per column — it is not a type cast. `pg.py:_native` already has to convert
Postgres `date` back to an ISO string because *"the desk stores and compares ISO strings throughout"*,
so any change here is a code change, not a schema change.

### 5.7 Booleans are `INTEGER` 0/1

`regime_evaluations.committed`, `.dry_run`, `.transition_limited`, `.override_active`, `.data_stale`,
`.manual_action_required`, `index_series.is_final`, `option_arms.breach_seen`, `.mark_stop_seen`. The
desk's SQL compares them to `0`/`1`. Landing them as Postgres `boolean` changes every one of those
comparisons; landing them as `bigint` (what the current migrator does) is lossless but ugly. Pick one
and record it.

### 5.8 JSON is stored as TEXT, and should stay TEXT through the migration

`holdings_json`, `constituents_json`, `weights_json`, `steps_json`, `argv_json`, `spec_json`,
`book_weights_json`, `input_snapshot_json`, `reason_codes_json`, `reasons_json`, `entry_fills_json`,
`exit_fills_json`, `contracts_json`, `settled_json`. Converting to `jsonb` normalises whitespace and
reorders keys, which would break any checksum taken over the text and change the bytes of an evidence
column. Migrate as text; convert afterwards, if ever, as a separate reviewed change.

### 5.9 Miscellany

- `option_arms` appears in `sqlite_master` as `CREATE TABLE "option_arms"` (quoted) — the residue of
  an ALTER-driven rebuild. Cosmetic, but a naive DDL translator will carry the quotes.
- Two columns were added by ALTER without being folded into the base DDL: `rebalance_orders.order_id`
  and `.reconciled_at` (migration 13), `rebalance_versions.evaluation_id`, `option_arms.contracts_json`
  and `.underlying_token`. They sit after the closing paren in the stored DDL. Read columns from
  `PRAGMA table_info`, never by parsing the DDL text.
- `regime_evaluations` holds **26 rows and zero canonical rows** — every evaluation on the box is a
  preview. Worth knowing before anyone builds a report that assumes a committed regime history.
- One `FOREIGN KEY`: `rebalance_orders.version_id → rebalance_versions(version_id)`. SQLite enforces
  it (`PRAGMA foreign_keys=ON` in `db.connect`); store C does not have it at all.

---

## 6. What this leaf could not establish, and what the migration still needs

1. **A path for the box to reach Baskfy, or for Baskfy to reach the box.** Shell access to
   `desk@65.0.226.77` exists from *this laptop* (`~/.ssh/momentum_desk2`, `Host momentum-desk-app`).
   It does **not** exist from the Baskfy box: that key is bound to a forced command,
   `command="/home/desk/bin/emit-kite-token"`, and returns a token no matter what is asked
   (`tools/deploy/desk/README.md`; adversarially verified at GATES.md:102). So the migration is a
   laptop-mediated transfer unless Maulik opens something. Recorded, not assumed.
2. **Whether store B's plan `39efc9eefca7` and store C's six cutover plans are to be preserved or
   discarded.** They are the only rows that exist outside the box. My recommendation is to archive B
   and C as files and migrate only A, but the choice should be explicit.
3. **Whether `cashflows` being empty is correct** (§4.1). Only Maulik knows whether money ever moved
   in or out unrecorded.
4. **The `float` vs `numeric` decision** (§5.1) — house rule 9 against M19.3's faithfulness argument.

---

## 7. Gate ledger

**G5 — nothing was written to any desk datastore.** The proof, not the assertion:

- Every connection to the live box's database was opened read-only through a URI:
  `sqlite3.connect("file:/home/desk/kite-momentum-rebalancer/data/portfolio.db?immutable=1", uri=True)`.
  `immutable=1` opens without locking and without creating the `-shm` sidecar; a first attempt with
  `mode=ro&nolock=1` failed outright rather than falling back to anything that could write.
- Every statement issued was `SELECT` or `PRAGMA` (`user_version`, `quick_check`, `table_info`).
- The file was stat'd before and after every read session and never moved:
  `2026-08-31 14:39:37.797648180 +0530  6295552` both times, sha256
  `22a38d53d3a0ba9011851b6a3bb3a0d54874b099a41f609012c37ce06f283d1b`. Its mtime is 14:39, from the
  desk's own 14:37 daily run — hours before this leaf started.
- `data/portfolio.db-wal` and `data/portfolio.db-shm` did not exist before my reads and did not exist
  after: `ls: cannot access 'data/portfolio.db-wal': No such file or directory`, both times.
- No systemd unit was started, stopped, reloaded or edited; no file on the box was created or
  modified; `.env` was never printed, only `grep -oE` for four specific non-secret key names.
- The laptop's SQLite (store B) was never opened at all — it was `cp`'d to a scratchpad and every
  query ran against the copy.
- Store C (Postgres) received only `SELECT` and `information_schema` / `pg_indexes` reads.
- No order was placed, previewed or confirmed. `DRY_RUN=false` on that box was observed, not touched.
