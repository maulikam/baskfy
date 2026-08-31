# Gates: 1.3.3 Verify the migration, prove idempotent, archive forever

Scope: independent verification of 1.3.2. A migration that says it worked is not evidence.

This leaf wrote exactly one file besides this ledger — `gates/desk-retire-1.3.3-verify.sh` — and
**shares no code with `tools/migrate-desk/`**. It re-reads both databases itself, re-derives its
own digests with its own type-aware canonicalisation, and uses a different cell separator
(`\x1e` vs the migrator's `\x1f`) so no digest here can be the migrator's digest by accident.
`tools/migrate-desk/` was read and exercised, never edited (`git status` clean under it).
Store A's sha256 is unchanged: see G4. All timings 31 Aug 2026 IST.

- [x] G1: A standalone verification script exists and exits non-zero on any mismatch
  CHECK: test -x gates/desk-retire-1.3.3-verify.sh && echo executable
  EXPECT: executable
  EVIDENCE: `gates/desk-retire-1.3.3-verify.sh`, mode 0755, 9.9 KB, no import of the migrator.
  Proven to exit **1**, not merely to exist, against five deliberately-wrong targets — each one
  a clone of `desk_migrated` with a single defect introduced, so nothing but the defect differs:

  | # | what was broken | verifier said | exit |
  |---|---|---|---|
  | 0 | nothing (control) | `ALL TABLES MATCH` | **0** |
  | 1 | the whole old-migrator schema `desk` | 22 mismatches, 8/19 tables matched | **1** |
  | 2 | `fills.price` 1000.0 → 1000.0000000000001 (**one ULP**; row count unchanged) | `fills: digest fd80e7f1… -> 5fb44578…` | **1** |
  | 3 | `snapshots.nav` + 1e-15 (**sub-ULP**: same float64) | `snapshots: digest bdbab7be… -> c62860d3…` | **1** |
  | 4 | one `trades` row deleted | `trades: row_count 8530 -> 8529` **and** digest | **1** |
  | 5 | `trades.entry_price` ⇄ `exit_price` swapped | `trades: digest 03a32311… -> 5e2ad13d…` | **1** |
  | 6 | one `trades.exit_reason` NULL → `''` | `trades: digest 03a32311… -> bb8b57ab…` | **1** |

  Row 3 is the answer to "is the checksum computed over something insensitive": it is not, and
  it is **stricter than the migrator's own**. `desk_assertions.canonical()` renders a `Decimal`
  as `repr(float(v))`, so `Decimal('10431108.87')` and `Decimal('10431108.870000000000001')`
  hash identically there (verified by calling it directly); this script tags a numeric that
  carries precision the source float never had and fails. See FINDING 2.
  All sabotage was done in throwaway schemas, which were dropped; `desk` and `desk_migrated` are
  as they were, and the control run was repeated after cleanup and still says `ALL TABLES MATCH`.

- [x] G2: Row counts match source and target for every table — all tables, count stated
  CHECK: bash gates/desk-retire-1.3.3-verify.sh 2>&1 | tail -5
  EXPECT: /ALL TABLES MATCH/
  EVIDENCE: **19 of 19 tables**, every one asserted on BOTH row_count and an independent digest,
  **43,411 source rows → 43,411 target rows**, 0 mismatches, exit 0. No table is skipped or
  exempted; the script fails if a source table is absent from the target or is missing a column.
  ```
  benchmark        3344→3344   breadth_readings      1→1     cashflows              0→0
  corporate_actions   1→1      daily_runs           26→26    fills               9609→9609
  index_series    21308→21308  ops_jobs             61→61    option_arms            0→0
  option_variants     2→2      rebalance_orders    425→425   rebalance_versions    35→35
  regime_book_snapshots 0→0    regime_evaluations   26→26    regime_exposure       26→26
  settings            1→1      settings_audit        1→1     snapshots             15→15
  trades           8530→8530                                 TOTAL          43,411→43,411
  ```
  1.3.3's own schema digest, which is not the migrator's:
  `f3d12626acb0ff06c2cf04bbbee3810035e199740f5306b9eead47b2d3668e8a`.
  **Stated honestly:** `cashflows`, `option_arms` and `regime_book_snapshots` have 0 rows, so for
  those three both assertions pass vacuously (0 == 0, and the digest is sha256 of the empty
  string on both sides). The script prints `(empty: this assertion is vacuous)` on those lines
  rather than letting them read as evidence. What is *not* vacuous for them: `option_arms`'
  foreign key was probed with a bad `variant_id` and **rejected**, so the constraint is real even
  with no rows behind it (FINDING 5).
  Re-measured independently, the headline comparison 1.3.2 claims against the old migrator holds:
  primary keys `desk`=0 / `desk_migrated`=20 · uniques 0/3 · foreign keys 0/2 · `pg_indexes` 0/41 ·
  `rebalance_orders` with `id IS NULL` **67 → 0** · the 12 money columns `double precision` → `numeric`.

- [x] G3: Re-running the migration produces identical rows (house rule 7, idempotent ingestion)
  EVIDENCE: verified by re-running the migration myself, **not** by quoting 1.3.2's digest.
  Three real `apply --commit` runs against the sealed archive, `--fork-policy box-only`:
  into a fresh `desk_reverify_a`, into a fresh `desk_reverify_b`, then a second time over
  `desk_reverify_a` with `--drop-existing` (the true re-run). All three, plus the pre-existing
  `desk_migrated`, give the **same independent digest**
  `f3d12626acb0ff06c2cf04bbbee3810035e199740f5306b9eead47b2d3668e8a`, and the script said
  `ALL TABLES MATCH` (exit 0) each time.
  Digest equality alone would be a weak claim, so the two schemas were also diffed **in SQL**:
  a symmetric `EXCEPT ALL` over every column of all 20 tables returned `a-b=0  b-a=0` for every
  one — 0 tables differing, 0 rows either way. The catalogue matched too: **203 columns**
  (name, type, nullability, DEFAULT, ordinal position), **25 constraints** (`pg_get_constraintdef`
  text), **41 indexes** (`indexdef` text) — all IDENTICAL; and every identity sequence had the
  same `(last_value, is_called)`, 0 differences.
  Sequences re-derived from `sqlite_sequence` in the source rather than trusted: all 9 correct,
  including the two that prove `max(id)+1` is not a substitute —
  `regime_evaluations` next id **33** against 26 rows (`sqlite_sequence.seq` 32, `max(id)` 32) and
  `settings_audit` next **7** against 1 row. An INSERT that omits them was run and rolled back:
  it got `id=481`, `status='PENDING'`, `filled_qty=0` — identity generated, DEFAULTs carried.
  **The other half of idempotence, tested adversarially:** `apply --commit` *without*
  `--drop-existing` onto the already-populated schema does **not** duplicate rows — it exits 1
  with "schema already holds 21 tables … Nothing has been changed", and the schema afterwards
  still verified clean. `--fork-policy` really is required: omitting it is refused by argparse
  before any connection is opened.
  **The rollback is real, tested live and not by reading their test.** A copy of the source with
  one `snapshots.index_value` multiplied by 1.5 was fed to `apply --commit`: exit 1,
  `semantic check failed: index_value_recomputes`, and afterwards
  `information_schema.schemata` and `information_schema.tables` both return **0** for that
  schema name — the DDL rolled back with the data, no partial schema survived.
  All four scratch schemas were dropped afterwards; only `desk` and `desk_migrated` remain.

- [x] G4: The forever archive is written, its location recorded, and its checksum noted
  EVIDENCE: **Location** `~/baskfy-safety/desk-migration-2026-08-31/` (outside the repo, sealed 0444):
  ```
  portfolio-A-2026-08-31T1731-IST.db              6,295,552 B  0444
      sha256 22a38d53d3a0ba9011851b6a3bb3a0d54874b099a41f609012c37ce06f283d1b
      PRAGMA quick_check -> ok      PRAGMA user_version -> 13
  verified-backup/20260831-173209/portfolio.db    6,295,552 B  0444
      sha256 59aa49c953abbc8f624f10cfba91fc5202e0a64dc7e6abb1d2924647abb5a1e1
      manifest.json  "integrity": "ok", fills 9609, trades 8530, rebalance_orders 425
  ~/baskfy-safety/sqlite-archive/                 store B, from M19, intact 0444
  ```
  **And it matches the source it claims to archive**, which is the part worth checking: store A
  was re-read on the live box today and returns the **same** sha256
  `22a38d53d3a0ba9011851b6a3bb3a0d54874b099a41f609012c37ce06f283d1b`, 6,295,552 bytes,
  mtime `2026-08-31 14:39:37.797648180 +0530` — byte for byte the archive. The three receipts in
  `~/baskfy-safety/migration-receipts/` and `desk_migrated.migration_provenance` both record that
  same sha256 as their source, so the schema in Postgres names the file it came from.
  **The desk box is untouched and still works** (this leaf issued only `sha256sum`, `stat`, `ls`
  and `systemctl is-active` over ssh): no `-wal`/`-shm` on the box before or after, and
  `momentum-web`, `momentum-daily.timer`, `momentum-backup.timer` are all `active` — it can
  rebalance on Friday 4 Sep. Store C's `desk` schema is also untouched (`desk.fills` still 9,262).
  One blemish on the archive directory, not on the archive: FINDING 3.

- [x] G5: A spot-check reads five real trades out of Baskfy's Postgres and compares them field
      by field against the source
  EVIDENCE: five rows chosen to *expose* a type, rounding, NULL or boundary error — not the
  first five. **55 cells (5 trades × 11 columns), 0 mismatches.** Every `numeric` was required to
  satisfy both `float(pg) == sqlite_float` **and** `Decimal(repr(sqlite_float)) == pg` — value and
  stored digits, not just value.

  | trades.id | why this row | the cell that would have broken |
  |---|---|---|
  | 8987 | largest `pnl` in the book | `pnl` 234900.98 → `Decimal('234900.98')`, `exit_price` 8525.009766 |
  | 8580 | most negative `pnl` | `pnl` -69096.8, `entry_price` 4758.600098 |
  | 10480 | smallest **non-zero** \|pnl\| — worst case for rounding | `pnl` **-0.02**, `exit_price` 999.98999 |
  | 5588 | boundary: earliest `entry_ts` (1693475872.0) | epoch float kept as `double precision` |
  | 5585 | boundary: an **open** trade | `exit_ts`, `exit_price`, `pnl` all NULL on both sides — NULL, not 0 and not `''` |

  `entry_score` and `exit_reason` are NULL on all five, matching 1.3.1's finding that 0 of 8,530
  rows carry either. Extremes in `fills` — the broker record Zerodha cannot re-cut — were checked
  the same way: max price 49135.0, min price 4.5, longest repr 14231.049805, all exact.
  **Then the same test over the whole database rather than five rows:** all **154,884 cells in all
  38 `numeric` columns** round-trip to the identical float64, and **0** of them have stored decimal
  text differing from `Decimal(repr(source_float))`. 1.3.2 proved this for 9,609 `fills`; it holds
  for every money and price cell in the schema.

---

## What did NOT hold, or is weaker than it reads

1. **`migration_provenance.report_json` says `"committed": false` on every committed run.** The
   report is serialised before the transaction commits, so the copy stored *inside* the schema
   records the opposite of what happened. Reproduced on all three schemas I migrated and on
   1.3.2's own `desk_migrated`; the rows plainly exist, so the transaction committed. The only
   record of committed-ness in the provenance table is this field, and it is always false. Cosmetic
   today, misleading to the person who reads this table in nine months. **1.3.2's to fix, not mine.**
2. **The migrator's checksum is blind below one float64 ULP.** `canonical()` renders a `Decimal`
   as `repr(float(v))`; called directly, it returns `10431108.87` for both `Decimal('10431108.87')`
   and `Decimal('10431108.870000000000001')`. A `numeric` that gained digits the source float never
   had would pass every checksum. **Immaterial for this migration** — I proved all 154,884 numeric
   cells are exactly `Decimal(repr(float))`, so no such value exists — but the assertion is weaker
   than "a SHA-256 over every cell" sounds. This leaf's script closes the gap.
3. **The sealed archive directory has stale `-wal` (0 bytes) and `-shm` (32 KB) sidecars**, created
   17:42, sealed 0444 beside the file. SQLite made them because the archive was opened at least
   once *without* `immutable=1` — which is exactly the discipline 1.3.2's own G6 insists on for the
   box and the README's recipe uses. **No data risk**: the WAL is empty and the `.db` sha256 still
   equals store A's, re-checked after all of today's work. But a stale `-shm` travelling with a
   "forever" archive is a trap for whoever copies the directory, and it undercuts the wording
   "sealed 0444" — three files are sealed, only one was meant to be there.
4. **`indexes_present` uses `>=`, not `==`.** An extra index in the target would not fail the run.
   The count is right today (41), so this is a latent weakness, not a wrong result.
5. **Three of nineteen tables get no evidential value from the two assertions.** `cashflows`,
   `option_arms` and `regime_book_snapshots` are all zero-row: row_count 0 == 0 and both digests
   are sha256 of the empty string. Partly compensated — I probed the constraints rather than the
   rows, and **every restored key is genuinely enforced, not merely catalogued**: the
   `option_arms.variant_id` FK rejected a bad insert *on the empty table*, the
   `rebalance_orders.version_id` FK rejected one, and duplicate inserts were rejected by
   `PK fills(trade_id)`, `PK snapshots(date)`, `PK trades(id)`, `UNIQUE regime_evaluations`,
   `UNIQUE regime_exposure` and `UNIQUE corporate_actions`.
6. **`ux_regime_canonical` currently constrains zero rows.** It is reproduced correctly from
   `sqlite_master` — `UNIQUE (evaluation_id) WHERE run_id = 'canonical'` — but no row in the box
   has `run_id = 'canonical'`, so the semantic check is catalogue-only and the index has never
   bitten. Faithful to the source; just not evidence that it works.

## Claims of 1.3.2 that I tried to break and could not

* 43,411 rows, 19 tables, every row_count and checksum — **holds**, re-derived independently.
* Rollback on failure is whole-schema, not a warning — **holds**, tested live with a broken source.
* `--fork-policy` is required with no default — **holds**, refused before any connection opens.
* `sqlite_sequence` carried by `setval`, `regime_evaluations` next id 33 — **holds**, re-derived.
* Money as `numeric` round-trips bit-identically — **holds, and more widely than claimed**:
  154,884 cells in 38 columns, not 9,609 in one table.
* 7 plans / 69 order lines exist nowhere on the box — **holds**. Re-counted from store B's archive
  and store C's schema against store A: the same 7 plan_ids (`1bd7b94babc6 39efc9eefca7 42c0b852a80c
  9915a620a4b0 9cf46614a665 a71689f13032 cef40037bcc6`), 2 order lines from B + 67 from C = 69.
  (A naive count gives 71 because the superseded rehearsal file in `sqlite-archive/` carries the
  same `39efc9eefca7` a second time — which is precisely the double-count their
  `duplicated_across_stores` guard exists to prevent.)
* 58 tests — **holds**: `DESK_TEST_DSN` set to the live instance, `58 passed in 2.06s`, 0 skipped.

## Re-runnable

```bash
bash gates/desk-retire-1.3.3-verify.sh                       # exit 0, "ALL TABLES MATCH"
VERIFY_SCHEMA=desk bash gates/desk-retire-1.3.3-verify.sh    # exit 1, 22 mismatches
```
