# Gates: 1.3.1 Inventory the desk's live backend

Scope: `trades` and `fills` do NOT exist in the Baskfy staging Postgres (verified). The record
lives on the desk's own backend. Find it, inventory it, change nothing. READ ONLY.

Local `portfolio.db` reference counts (2026-08-22 snapshot): trades 8198, fills 9262,
rebalance_orders 133, index_series 21276, benchmark 3328, rebalance_versions 13, daily_runs 12,
snapshots 8, regime_exposure 13, regime_evaluations 13, ops_jobs 23.

- [x] G1: The desk's live datastore is located and named (host, engine, database)
  CHECK: grep -ci "postgres\|sqlite" docs/DESK-DATA-INVENTORY.md
  EXPECT: /[1-9]/
  EVIDENCE: 45

- [x] G2: Every table is listed with its live row count — all of them, count stated
  CHECK: grep -c "^| " docs/DESK-DATA-INVENTORY.md
  EXPECT: /[1-9][0-9]*/
  EVIDENCE: 106

- [x] G3: Live counts are compared against the local `portfolio.db` snapshot above, and any
      drift is explained (the snapshot is 22 Aug; the desk has traded since)
  CHECK: grep -ci "drift\|since the snapshot" docs/DESK-DATA-INVENTORY.md
  EXPECT: /[1-9]/
  EVIDENCE: 6

- [x] G4: The tables that are unrebuildable evidence (trades, fills, rebalance_orders,
      cashflows, corporate_actions) are distinguished from the derivable ones
  EVIDENCE: `docs/DESK-DATA-INVENTORY.md` §4 splits all 19 tables into two named groups with a
  reason per table. Group 1 "unrebuildable evidence — checksum, assert, archive forever" = 10,181
  rows: fills 9609, rebalance_orders 425, ops_jobs 61, rebalance_versions 35, regime_evaluations
  26, regime_exposure 26, daily_runs 26, snapshots 15, option_variants 2, breadth_readings 1,
  corporate_actions 1, settings 1, settings_audit 1, cashflows 0, regime_book_snapshots 0,
  option_arms 0. Group 2 "derivable — row-count assertion only" = 24,652 rows: index_series
  21308, benchmark 3344. The `Class` column of the §2 inventory carries the same label per row,
  so the classification is stated twice and cannot drift.
  ONE DEVIATION, recorded not silent: the gate lists `trades` as unrebuildable; the measurement
  says otherwise. `app/analytics/tradebook.py:385` DELETEs and re-INSERTs every row for a symbol
  from a FIFO rebuild of `fills`, so `trades` is a projection. The only non-derived columns are
  `entry_score` and `exit_reason`, and on the live box **0 of 8,530 rows carry either**
  (`select count(*) filter (where entry_score is not null), count(*) filter (where exit_reason
  is not null) from trades` -> `(0, 0)`). §4 therefore gives `trades` its own heading, "derived
  today, evidence tomorrow", with the two conditions that would move it into group 1 and the
  proof that its `id` is not a key (box 7,862 rows at id<=11831 vs laptop 8,198 at the same max).

- [x] G5: Nothing was written to the desk's datastore
  EVIDENCE: `docs/DESK-DATA-INVENTORY.md` §7 carries the full proof. The load-bearing parts:
  every live read used `sqlite3.connect("file:/home/desk/kite-momentum-rebalancer/data/
  portfolio.db?immutable=1", uri=True)` — read-only, no locking, no `-shm` creation — and issued
  only SELECT/PRAGMA. The file is byte-identical before and after: `stat` returned
  `2026-08-31 14:39:37.797648180 +0530  6295552` on both sides of every read session, sha256
  `22a38d53d3a0ba9011851b6a3bb3a0d54874b099a41f609012c37ce06f283d1b`; that 14:39 mtime is the
  desk's own 14:37 daily run, hours before this leaf began. `ls data/portfolio.db-wal
  data/portfolio.db-shm` returned "No such file or directory" both before and after, so no
  sidecar was created. An earlier attempt with `mode=ro&nolock=1` raised
  `sqlite3.OperationalError: unable to open database file` and was abandoned rather than
  downgraded to anything writable. No systemd unit was started, stopped or edited; no file on
  the box was created or modified; `.env` was read only via `grep -oE` for four non-secret key
  names. The laptop's SQLite was never opened — it was `cp`'d to the scratchpad and queried
  there. The laptop's Postgres received only SELECT / information_schema / pg_indexes reads.
  No order was placed or previewed.
