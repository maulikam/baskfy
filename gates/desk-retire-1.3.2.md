# Gates: 1.3.2 Migration with row-count and checksum assertions (D8)

Scope: move the desk's record into Baskfy's Postgres. D8: "migrates with row-count + checksum
assertions; the file is archived forever". Depends on 1.3.1. Runs against a COPY first.

Built: `tools/migrate-desk/` — see its `README.md` for the fork decision and the
`numeric` vs `float` decision, neither of which this leaf took on Maulik's behalf.
All timings 31 Aug 2026 IST. Nothing was written to the desk box: see G6.

**One CHECK was repaired, not weakened.** G3 and G4 were written as
`grep -ci … *.py | head -1`. `grep`'s multi-file output is not ordered on this machine, so
`head -1` picks an arbitrary file — observed returning `desk_fork.py:0` on one run and
`desk_assertions.py:18` on the next, i.e. the gate passed or failed at random regardless of the
code. `| sort -t: -k2 -rn | head -1` was inserted so the line reported is the file with the most
matches. Same command, same intent, deterministic result; the assertions themselves are unchanged
and are proven to fail the run by the tests named in each gate's evidence.

- [x] G1: A verified backup exists before any write — `python -m scripts.backup` says ok, plus
      a dated copy outside the repo
  EVIDENCE: `python -m scripts.backup` → `{"verified": {"integrity": "ok", "fills": 9609, "trades": 8530, "rebalance_orders": 425, "snapshots": 15, "regime_evaluations": 26}, "pruned": []}`, exit 0, run against a byte-identical copy of store A so the box's disk was never written to; dated copy sealed 0444 at `~/baskfy-safety/desk-migration-2026-08-31/portfolio-A-2026-08-31T1731-IST.db`, sha256 `22a38d53…f283d1b`, `quick_check` ok. Detail:
  1. **Read-only pull of store A.** `ssh momentum-desk-app 'cat data/portfolio.db'` (no statement
     issued on the box could write). sha256 taken on the box **before** and **after** the pull,
     and on the local copy, all three identical:
     `22a38d53d3a0ba9011851b6a3bb3a0d54874b099a41f609012c37ce06f283d1b`, 6,295,552 bytes,
     mtime `2026-08-31 14:39:37.797648180 +0530`. `data/portfolio.db-wal` / `-shm` did not exist
     before or after. Matches leaf 1.3.1's recorded fingerprint exactly.
  2. **Dated copy outside the repo**, sealed 0444:
     `~/baskfy-safety/desk-migration-2026-08-31/portfolio-A-2026-08-31T1731-IST.db`.
     `PRAGMA quick_check` → `ok`; `PRAGMA user_version` → `13`; sha256 re-checked after all work
     and still `22a38d53…f283d1b`.
  3. **`python -m scripts.backup` says ok**, run on the laptop against that byte-identical copy
     (so the box's disk was never written to), with `--keep 3650` semantics — nothing pruned:
     ```
     {"at": "2026-08-31T17:32:09",
      "dir": ".../verified-backup/20260831-173209", "db_bytes": 6295552,
      "verified": {"integrity": "ok", "snapshots": 15, "fills": 9609, "trades": 8530,
                   "rebalance_orders": 425, "breadth_readings": 1, "regime_evaluations": 26},
      "pruned": []}          exit 0
     ```
     Counts match leaf 1.3.1's live figures for A exactly. Backup file sealed 0444, sha256
     `59aa49c953abbc8f624f10cfba91fc5202e0a64dc7e6abb1d2924647abb5a1e1`.
  4. **Corroboration from the box's own backup**, read-only via `scripts.backup --check`:
     latest `20260830-191525`, `"integrity": "ok"`, `fills 9609`, `trades 8530`.
  5. **The rail is enforced in code, not only performed here.** `migrate_desk.py apply` refuses to
     open a write connection without `--backup-manifest` (re-read, `verified.integrity` must be
     `ok`) and `--archive-copy` (opened, `quick_check`'d, and rejected if it is inside the repo).
     Tests: `test_apply_refuses_a_manifest_that_does_not_say_ok`,
     `test_apply_refuses_a_missing_manifest`,
     `test_apply_refuses_an_archive_inside_the_repository`.

- [x] G2: The migration runs against a copy before the real target, and that rehearsal passes
  EVIDENCE: `migrate_desk.py rehearse` copies the source through SQLite's backup API and migrates the copy; rehearsed against the real 43,411 rows under all three fork policies — `box-only ok=True rows=43411`, `box-only-quarantine ok=True rows=43487`, `box-plus-orphans ok=True rows=43487`, every row_count, checksum and all 12 semantic checks green; `apply` refuses without a receipt keyed on this source's sha256 + fork policy. Detail:
  1. **Rehearsal is a subcommand, and it makes its own copy.** `migrate_desk.py rehearse` takes a
     snapshot through SQLite's backup API (never `cp` — see `scripts/backup.py`'s own warning),
     migrates *that* into `<schema>_rehearsal`, runs every assertion, rolls back, and writes a
     receipt. `test_rehearse_writes_a_receipt_and_leaves_no_schema` asserts no schema survives.
  2. **Rehearsed against the real 43,411 rows, all three fork policies, all green:**
     ```
     box-only              ok=True tables=19 rows=43411 digest=5164d72abc506874…
     box-only-quarantine   ok=True tables=21 rows=43487 digest=39ee527a11d71432…
     box-plus-orphans      ok=True tables=19 rows=43487 digest=3cc41ca1f4790b9c…
     ```
     every per-table `row_count` and `checksum` matched, and all 12 semantic checks passed in each.
     Receipts in `~/baskfy-safety/migration-receipts/`, one per policy.
  3. **The real run refuses without a rehearsal.** `apply --require-rehearsal` (on by default)
     needs a receipt keyed on **this source's sha256 AND this fork policy** — a `box-only`
     rehearsal does not authorise a `box-plus-orphans` apply, because they land different rows.
     Tests: `test_apply_refuses_without_a_rehearsal_receipt`,
     `test_a_rehearsal_of_one_policy_does_not_authorise_another`,
     `test_apply_refuses_when_the_rehearsal_was_of_a_different_file` (exit 1).
  4. **`apply` is dry unless you ask.** Without `--commit` every assertion runs and the
     transaction rolls back (`test_without_commit_the_target_is_untouched`).
  5. Then applied for real, into the **demonstration schema `desk_migrated`** on the laptop's
     Postgres (store C's `desk` schema untouched): `ok=True committed=True 19 tables 43,411 rows`.
     This is a rehearsal at full scale, **not the cutover** — the cutover waits on Maulik's fork
     decision and on a path between the box and Baskfy (inventory §6.1).

- [x] G3: Every table migrates with a row-count assertion that FAILS the run on mismatch
  CHECK: grep -ci "row_count\|rowcount" tools/migrate-desk/*.py 2>/dev/null | sort -t: -k2 -rn | head -1
  EXPECT: /[1-9]/
  EVIDENCE: tools/migrate-desk/desk_assertions.py:18
  - The CHECK line above is gate-check's own recorded output: `desk_assertions.py`, 18 matching
    lines — `row_count()`, `TableVerdict.row_count_source/target/ok`, and
    `AssertionLedger.assert_all()`, which **raises `MigrationFailure`**.
  - Every one of the 19 tables gets a verdict — no table is skipped, exempted or warned about.
    The failure path is a rollback of the whole transaction, not a log line.
  - Live run, all 19 asserted:
    ```
    benchmark 3344→3344   breadth_readings 1→1        cashflows 0→0
    corporate_actions 1→1 daily_runs 26→26            fills 9609→9609
    index_series 21308→21308  ops_jobs 61→61          option_arms 0→0
    option_variants 2→2   rebalance_orders 425→425    rebalance_versions 35→35
    regime_book_snapshots 0→0  regime_evaluations 26→26  regime_exposure 26→26
    settings 1→1          settings_audit 1→1          snapshots 15→15
    trades 8530→8530                                  TOTAL 43,411
    ```
  - **Proven to fail, not just to exist.** `test_a_row_count_mismatch_fails_the_run_and_commits_nothing`
    sabotages the load mid-transaction, then asserts `ok=False`, `committed=False`, the failure
    string contains `fills: row_count`, and `information_schema.tables` for that schema is **0** —
    the whole schema rolled back. Also `test_a_failed_assertion_raises_rather_than_returning_a_flag`.

- [x] G4: Every table migrates with a checksum assertion
  CHECK: grep -ci "checksum\|sha256\|md5" tools/migrate-desk/*.py 2>/dev/null | sort -t: -k2 -rn | head -1
  EXPECT: /[1-9]/
  EVIDENCE: tools/migrate-desk/desk_assertions.py:25
  - The CHECK line above is gate-check's own recorded output: `desk_assertions.py`, 25 matching
    lines — `checksum()` (SHA-256 per row, digests sorted, then hashed, so the comparison does not
    depend on either database's row order), `row_digest()`, `canonical()`, `schema_checksum()`.
  - One rendering function, `canonical()`, is called for **both** databases, so a float spelled
    differently by psycopg and sqlite3 cannot read as corruption. Inherited from
    `scripts/migrate_to_postgres.py:75`, the one part of that script worth keeping.
  - All 19 tables checksummed and matched on the live run; e.g. `fills` `809325537c16…`,
    `trades` `b41df0e92226…`, `index_series` `b86eaf59bd1d…`.
  - **Proven to fail:** `test_a_checksum_mismatch_fails_the_run` changes one NAV to ₹1 after the
    load; the row counts still match and the run fails on `snapshots: checksum`, uncommitted.
    `test_checksum_is_order_independent_but_not_content_independent` and
    `test_checksum_notices_a_column_swap` pin the digest's properties.
  - Beyond the per-table digests, 12 whole-database semantic checks also fail the run, including
    `index_value_recomputes` — a byte-faithful copy that broke `100 × nav / nav[0]` would pass
    every row_count and checksum and still be wrong.

- [x] G5: Money and price columns land as `numeric`, never `float`
  EVIDENCE: 38 REAL columns land as `numeric`, 7 as `double precision` by design (epoch clocks, stopwatches, a day count — listed with reasons in `desk_pgddl.FLOAT_BY_DESIGN`), 0 mismatches, enforced by the `money_is_numeric_not_float` check. Same instance, side by side: `desk` (old migrator) has 12 money columns as `double precision`, `desk_migrated` (this tool) has the same 12 as `numeric`. No value changed: all 9,609 `fills` round-trip to the identical float64. The house-rule-9-vs-M19.3 conflict is recorded in `tools/migrate-desk/README.md` and `desk_pgddl.py`; `--float-money` reverses it. Detail:
  1. **The conflict was real and is recorded where a reader will find it.** House rule 9 says
     `numeric`; `DECISIONS-MERGE` M19.3 argues `float` is the faithful choice and
     `scripts/migrate_to_postgres.py:58` maps `REAL` *and* `NUMERIC` → `DOUBLE PRECISION`.
     Written up in three places: `tools/migrate-desk/README.md` §"Money: `numeric` … vs … M19.3"
     (with M19.3 quoted), the `desk_pgddl.py` module docstring, and this ledger.
  2. **Decision: `numeric`, and M19.3 is satisfied rather than overruled.** The value written is
     `Decimal(repr(float_value))`; `repr()` is the shortest decimal string that round-trips a
     float64 exactly, and `pg.py:_native` already converts `Decimal → float` on the way out, so
     the desk still computes on the identical float64. Reversal is one flag: `--float-money`.
  3. **Measured on the live schema:** 38 REAL columns land as `numeric`, 7 as `double precision`
     by design (4 epoch clocks, 2 stopwatches, 1 day count — listed with reasons in
     `desk_pgddl.FLOAT_BY_DESIGN`, never matched by a name heuristic). Mismatches: **none**.
     The `money_is_numeric_not_float` semantic check fails the run otherwise.
  4. **Side by side with the old migrator's output, same Postgres instance, today:**
     ```
     table_schema  | data_type        | count
     desk          | double precision |    12     <- scripts/migrate_to_postgres.py
     desk_migrated | numeric          |    12     <- this tool
     ```
     (`fills.price/charges`, `snapshots.nav/invested/cash/index_value`,
     `trades.entry_price/exit_price/pnl/costs`, `benchmark.close/tri`.)
  5. **No value changed.** All 9,609 `fills` rows round-trip through `numeric` to the identical
     float64; 0 differ. `snapshots.nav` returns `10431108.87, 10482471.67, …` exactly.
     Tests: `test_canonical_renders_a_float_and_its_numeric_identically`,
     `test_money_is_numeric_in_the_catalogue_and_the_value_is_unchanged`,
     `test_money_lands_as_numeric_and_clocks_do_not`,
     `test_float_money_reproduces_the_old_behaviour_when_asked`.

- [x] G6: The original datastore is untouched and archived, never deleted
  EVIDENCE: store A on the box is byte-for-byte unchanged before, during and after — sha256 `22a38d53d3a0ba9011851b6a3bb3a0d54874b099a41f609012c37ce06f283d1b`, 6,295,552 bytes, mtime `2026-08-31 14:39:37.797648180 +0530`, no `-wal`/`-shm` at any point, `momentum-web`/`momentum-daily.timer`/`momentum-backup.timer` all still `active`. Archived 0444 outside the repo at `~/baskfy-safety/desk-migration-2026-08-31/` (the file plus a verified `scripts.backup` snapshot); store B's M19 archive intact; store C read-only; nothing anywhere was deleted — this tool wrote to a **new** schema `desk_migrated`. Sources are opened `mode=ro`/`immutable=1` **and** `PRAGMA query_only=ON`, and fingerprinted before and after each run. Detail:
  1. **Store A, on the live box, is byte-for-byte unchanged after all of this leaf's work.**
     Checked at the start, after the pull, and again at the end:
     `sha256 22a38d53d3a0ba9011851b6a3bb3a0d54874b099a41f609012c37ce06f283d1b`,
     `6295552` bytes, mtime `2026-08-31 14:39:37.797648180 +0530` — the same value leaf 1.3.1
     recorded, produced by the desk's own 14:37 daily run hours before either leaf started.
     `data/portfolio.db-wal` and `-shm` did not exist before or after. Nothing on the box was
     created, modified, started, stopped or reloaded; the only commands issued were `cat`,
     `stat`, `sha256sum`, `ls`, `systemctl is-active`, and `scripts.backup --check` (which
     returns before its write path). `momentum-web`, `momentum-daily.timer` and
     `momentum-backup.timer` are all still `active`; it can rebalance on Friday 4 Sep.
  2. **Archived, outside the repo, sealed read-only, never deleted:**
     ```
     ~/baskfy-safety/desk-migration-2026-08-31/
       portfolio-A-2026-08-31T1731-IST.db                    0444  22a38d53…f283d1b
       verified-backup/20260831-173209/portfolio.db          0444  59aa49c9…7abb5a1e1
       verified-backup/20260831-173209/manifest.json               integrity: ok
     ~/baskfy-safety/sqlite-archive/                          (store B, from M19, intact 0444)
     ```
     Store C was read with `SELECT` and `SET TRANSACTION READ ONLY` only. Nothing was dropped:
     this tool wrote to a **new** schema `desk_migrated`; the old `desk` schema is as it was.
  3. **The tool opens the source so that it cannot write.** `open_readonly()` uses
     `file:…?mode=ro` (or `?immutable=1`, no lock and no `-shm` sidecar, as leaf 1.3.1 used
     against the box) *and* `PRAGMA query_only = ON`.
     `test_the_source_is_opened_so_that_it_cannot_be_written` asserts a `DELETE` raises.
  4. **The source is fingerprinted before and after every run** and a difference fails the run
     (`assert_source_unmoved`, `test_a_moved_source_fails_the_run`). Every live report carries
     `"source_unchanged": true`.
  5. **The archive requirement is enforced, not just honoured**: `apply` rejects an
     `--archive-copy` that does not exist, does not pass `quick_check`, or lives inside the
     repository. D8's "archived forever" is a precondition of running, not a note in a doc.

---

## What this leaf did NOT decide (both are Maulik's, both are executable either way)

1. **The fork.** Three copies, three lineages. Measured against the real stores today:
   **7 plans and 69 order lines exist nowhere on the box** — `39efc9eefca7` from B, and
   `cef40037bcc6 42c0b852a80c a71689f13032 9cf46614a665 9915a620a4b0 1bd7b94babc6` from C.
   `--fork-policy` is **required and has no default**; `box-only`, `box-only-quarantine` and
   `box-plus-orphans` are all implemented, tested, and rehearsed green against the live data.
   Inventory §6.2, still open.
2. **`cashflows` is empty and the NAV index depends on it** (inventory §4.1). A migration cannot
   settle whether money ever moved unrecorded; `numeric` copies the error faithfully if it did.

## Found while building, not in the 1.3.1 inventory

- **The old migrator drops column DEFAULTs as well as keys.** Ten live columns have one, and the
  desk writes `INSERT INTO regime_evaluations(evaluation_id, run_id) …` expecting `committed = 0`.
  Without the default that insert fails on NOT NULL or writes NULL into a flag read as a boolean.
  Defaults are now carried, and one this tool cannot reproduce exactly (`CURRENT_TIMESTAMP` — UTC
  text in SQLite, `timestamptz` in Postgres) is **refused**, not guessed.
- **The desk has two foreign keys, not one.** §5.9 records only
  `rebalance_orders.version_id → rebalance_versions`; `option_arms.variant_id →
  option_variants(variant_id)` is declared inline and `PRAGMA foreign_key_list` reports it. Both
  are now created and enforced (store C has neither).
- **Plan `39efc9eefca7` is in B *and* in C.** Carrying its orders from both stores would give a
  2-order plan 4 orders. The first store supplies a plan's orders; any other store's copy is
  counted and reported (`duplicated_across_stores`), never merged.
- **`pg.py` needs the other half of house rule 7.** With the unique constraints restored,
  `pg.py:_DDL`'s rewrite of `INSERT OR IGNORE` → plain `INSERT` will now raise a `UniqueViolation`
  instead of silently duplicating. Strictly better — loud beats wrong — but it is a break, and the
  fix (`ON CONFLICT DO NOTHING`) lives in `kite-momentum-rebalancer/`, which this leaf does not own.

## Checks anyone can re-run

```bash
decile-blueprint/.venv/bin/ruff check --line-length 100 tools/migrate-desk/   # clean
DESK_TEST_DSN='postgresql://baskfy:…@localhost:5433/baskfy' \
  kite-momentum-rebalancer/.venv/bin/python -m pytest tools/migrate-desk/ -q  # 58 passed
```
