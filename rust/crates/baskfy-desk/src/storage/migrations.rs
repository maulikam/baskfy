//! Ports `app.analytics.db._MIGRATIONS` verbatim: version → the statements it runs, in order.
//! Generated from `fixtures/desk_migrations.json` (the live Python's dict, dumped) and held
//! to it by `tests/storage.rs`. SQLite spelling; [`super::translate`] adapts it for Postgres
//! exactly as `app.analytics.pg.translate` does.

/// `(version, statements)`, ascending.
#[rustfmt::skip]
pub const MIGRATIONS: &[(i64, &[&str])] = &[
    (1, &[
        r###"CREATE TABLE IF NOT EXISTS snapshots(
               date          TEXT PRIMARY KEY,
               nav           REAL NOT NULL,
               invested      REAL NOT NULL,
               cash          REAL NOT NULL,
               holdings_json TEXT NOT NULL,
               index_value   REAL
           )"###,
        r###"CREATE TABLE IF NOT EXISTS cashflows(
               id     INTEGER PRIMARY KEY AUTOINCREMENT,
               date   TEXT NOT NULL,
               amount REAL NOT NULL,        -- invest NEGATIVE, withdraw/terminal POSITIVE
               type   TEXT NOT NULL
           )"###,
        r###"CREATE UNIQUE INDEX IF NOT EXISTS ux_cashflows_natural
               ON cashflows(date, amount, type)"###,
        r###"CREATE TABLE IF NOT EXISTS benchmark(
               index_name TEXT NOT NULL,
               date       TEXT NOT NULL,
               close      REAL,
               tri        REAL,
               PRIMARY KEY(index_name, date)
           )"###,
        r###"CREATE TABLE IF NOT EXISTS rebalance_versions(
               version_id        TEXT PRIMARY KEY,
               created_ts        REAL NOT NULL,
               constituents_json TEXT NOT NULL,
               weights_json      TEXT NOT NULL,
               note              TEXT
           )"###,
        r###"CREATE TABLE IF NOT EXISTS rebalance_orders(
               id                INTEGER PRIMARY KEY AUTOINCREMENT,
               version_id        TEXT NOT NULL REFERENCES rebalance_versions(version_id),
               symbol            TEXT NOT NULL,
               side              TEXT NOT NULL,
               planned_qty       INTEGER,
               planned_ref_price REAL,
               filled_qty        INTEGER DEFAULT 0,
               avg_fill_price    REAL,
               status            TEXT DEFAULT 'PENDING'
           )"###,
        r###"CREATE INDEX IF NOT EXISTS ix_rebalance_orders_version
               ON rebalance_orders(version_id)"###,
        r###"CREATE TABLE IF NOT EXISTS trades(
               id           INTEGER PRIMARY KEY AUTOINCREMENT,
               symbol       TEXT NOT NULL,
               entry_ts     REAL,
               exit_ts      REAL,
               qty          INTEGER,
               entry_price  REAL,
               exit_price   REAL,
               entry_score  REAL,
               exit_reason  TEXT,
               pnl          REAL,
               costs        REAL
           )"###,
        r###"CREATE INDEX IF NOT EXISTS ix_trades_symbol ON trades(symbol)"###,
        r###"CREATE INDEX IF NOT EXISTS ix_trades_exit_ts ON trades(exit_ts)"###,
    ]),
    (2, &[
        r###"CREATE TABLE IF NOT EXISTS index_series(
               index_name       TEXT NOT NULL,
               date             TEXT NOT NULL,
               open             REAL,
               high             REAL,
               low              REAL,
               close            REAL NOT NULL,
               instrument_token INTEGER,
               is_final         INTEGER NOT NULL DEFAULT 1,
               updated_at       TEXT NOT NULL,
               PRIMARY KEY(index_name, date)
           )"###,
        r###"CREATE INDEX IF NOT EXISTS ix_index_series_date ON index_series(date)"###,
        r###"CREATE TABLE IF NOT EXISTS regime_evaluations(
               id                   INTEGER PRIMARY KEY AUTOINCREMENT,
               evaluation_id        TEXT NOT NULL,
               run_id               TEXT NOT NULL,
               committed            INTEGER NOT NULL DEFAULT 0,
               dry_run              INTEGER NOT NULL DEFAULT 1,
               mode                 TEXT NOT NULL,
               scheduled_week_end   TEXT NOT NULL,
               signal_session_date  TEXT NOT NULL,
               data_as_of           TEXT NOT NULL,
               raw_candidate_tier   TEXT NOT NULL,
               previous_policy_tier TEXT,
               policy_tier          TEXT NOT NULL,
               transition_limited   INTEGER NOT NULL,
               new_buys             TEXT NOT NULL,
               forced_action        TEXT NOT NULL,
               override_active      INTEGER NOT NULL DEFAULT 0,
               data_stale           INTEGER NOT NULL DEFAULT 0,
               manual_action_required INTEGER NOT NULL DEFAULT 0,
               breadth_pct          REAL,
               breadth_coverage_pct REAL,
               book_weights_json    TEXT NOT NULL,
               book_weight_source   TEXT NOT NULL,
               input_snapshot_json  TEXT NOT NULL,
               reason_codes_json    TEXT NOT NULL,
               reasons_json         TEXT NOT NULL,
               next_evaluation_date TEXT NOT NULL,
               last_transition_date TEXT,
               algorithm_version    TEXT NOT NULL,
               config_hash          TEXT NOT NULL,
               created_at           TEXT NOT NULL,
               UNIQUE(evaluation_id, run_id)
           )"###,
        r###"CREATE UNIQUE INDEX IF NOT EXISTS ux_regime_canonical
               ON regime_evaluations(evaluation_id) WHERE run_id = 'canonical'"###,
        r###"CREATE INDEX IF NOT EXISTS ix_regime_week
               ON regime_evaluations(scheduled_week_end)"###,
        r###"CREATE TABLE IF NOT EXISTS regime_exposure(
               id                    INTEGER PRIMARY KEY AUTOINCREMENT,
               evaluation_id         TEXT NOT NULL,
               observed_at           TEXT NOT NULL,
               actual_equity_pct     REAL NOT NULL,
               target_equity_cap_pct REAL NOT NULL,
               exposure_gap_pct      REAL NOT NULL,
               pending_buy_pct       REAL NOT NULL DEFAULT 0,
               pending_sell_pct      REAL NOT NULL DEFAULT 0,
               execution_status      TEXT NOT NULL,
               note                  TEXT,
               UNIQUE(evaluation_id, observed_at)
           )"###,
        r###"CREATE INDEX IF NOT EXISTS ix_regime_exposure_eval
               ON regime_exposure(evaluation_id)"###,
        r###"CREATE TABLE IF NOT EXISTS breadth_readings(
               as_of_date          TEXT NOT NULL,
               universe_id         TEXT NOT NULL,
               pct_above_20dma     REAL NOT NULL,
               eligible_count      INTEGER NOT NULL,
               observed_count      INTEGER NOT NULL,
               coverage_pct        REAL NOT NULL,
               universe_hash       TEXT NOT NULL,
               audit_run_id        TEXT NOT NULL,
               calculation_version TEXT NOT NULL,
               missing_policy      TEXT NOT NULL,
               created_at          TEXT NOT NULL,
               PRIMARY KEY(as_of_date, universe_id)
           )"###,
        r###"CREATE TABLE IF NOT EXISTS regime_book_snapshots(
               evaluation_id      TEXT PRIMARY KEY,
               scheduled_week_end TEXT NOT NULL,
               policy_tier        TEXT NOT NULL,
               weights_json       TEXT NOT NULL,
               source             TEXT NOT NULL,
               coverage_pct       REAL NOT NULL,
               created_at         TEXT NOT NULL
           )"###,
        r###"CREATE INDEX IF NOT EXISTS ix_book_snapshots_week
               ON regime_book_snapshots(scheduled_week_end)"###,
    ]),
    (3, &[
        r###"CREATE TABLE IF NOT EXISTS settings(
               key        TEXT PRIMARY KEY,
               value      TEXT NOT NULL,
               updated_at TEXT NOT NULL,
               note       TEXT
           )"###,
        r###"CREATE TABLE IF NOT EXISTS settings_audit(
               id         INTEGER PRIMARY KEY AUTOINCREMENT,
               key        TEXT NOT NULL,
               old_value  TEXT,
               new_value  TEXT NOT NULL,
               changed_at TEXT NOT NULL,
               note       TEXT
           )"###,
        r###"CREATE INDEX IF NOT EXISTS ix_settings_audit_key
               ON settings_audit(key, changed_at)"###,
    ]),
    (4, &[
        r###"CREATE TABLE IF NOT EXISTS ops_jobs(
               id          INTEGER PRIMARY KEY AUTOINCREMENT,
               name        TEXT NOT NULL,
               argv_json   TEXT NOT NULL,
               status      TEXT NOT NULL,      -- running | ok | failed | timeout
               exit_code   INTEGER,
               started_at  TEXT NOT NULL,
               finished_at TEXT,
               duration_s  REAL,
               output      TEXT,
               note        TEXT
           )"###,
        r###"CREATE INDEX IF NOT EXISTS ix_ops_jobs_started ON ops_jobs(started_at DESC)"###,
    ]),
    (5, &[
        r###"CREATE TABLE IF NOT EXISTS fills(
               trade_id    TEXT PRIMARY KEY,
               symbol      TEXT NOT NULL,
               when_ts     REAL NOT NULL,
               side        TEXT NOT NULL,      -- BUY | SELL
               quantity    INTEGER NOT NULL,
               price       REAL NOT NULL,
               exchange    TEXT,
               charges     REAL NOT NULL DEFAULT 0,
               source      TEXT NOT NULL,      -- console_csv | kite_api
               captured_at TEXT
           )"###,
        r###"CREATE INDEX IF NOT EXISTS ix_fills_symbol ON fills(symbol, when_ts)"###,
    ]),
    (6, &[
        r###"CREATE TABLE IF NOT EXISTS daily_runs(
               id           INTEGER PRIMARY KEY AUTOINCREMENT,
               ran_at       TEXT NOT NULL,
               session_date TEXT NOT NULL,
               trigger      TEXT NOT NULL,   -- schedule | cli | page
               outcome      TEXT NOT NULL,   -- ok | partial | failed | auth
               failed_steps INTEGER NOT NULL DEFAULT 0,
               steps_json   TEXT NOT NULL,
               duration_s   REAL
           )"###,
        r###"CREATE INDEX IF NOT EXISTS ix_daily_runs_ran ON daily_runs(ran_at DESC)"###,
    ]),
    (7, &[
        r###"ALTER TABLE rebalance_versions ADD COLUMN evaluation_id TEXT"###,
        r###"CREATE INDEX IF NOT EXISTS ix_rebalance_versions_eval
               ON rebalance_versions(evaluation_id)"###,
        r###"CREATE INDEX IF NOT EXISTS ix_rebalance_versions_created
               ON rebalance_versions(created_ts DESC)"###,
    ]),
    (8, &[
        r###"CREATE TABLE IF NOT EXISTS corporate_actions(
               id         INTEGER PRIMARY KEY AUTOINCREMENT,
               symbol     TEXT NOT NULL,
               kind       TEXT NOT NULL,      -- bonus | split
               ex_date    TEXT NOT NULL,
               ratio_new  REAL NOT NULL,
               ratio_old  REAL NOT NULL,
               note       TEXT,
               created_at TEXT NOT NULL,
               UNIQUE(symbol, kind, ex_date)
           )"###,
        r###"CREATE INDEX IF NOT EXISTS ix_corporate_actions_symbol
               ON corporate_actions(symbol, ex_date)"###,
    ]),
    (9, &[
        r###"CREATE TABLE IF NOT EXISTS option_variants(
               variant_id   TEXT PRIMARY KEY,
               name         TEXT NOT NULL,
               arm          TEXT NOT NULL,      -- intraday (overnight dropped, v11)
               spec_json    TEXT NOT NULL,
               spec_hash    TEXT NOT NULL,
               registered_at TEXT NOT NULL,
               note         TEXT
           )"###,
        r###"CREATE TABLE IF NOT EXISTS option_arms(
               arm_id        TEXT PRIMARY KEY,
               variant_id    TEXT NOT NULL REFERENCES option_variants(variant_id),
               strategy      TEXT NOT NULL,
               arm           TEXT NOT NULL,
               session_date  TEXT NOT NULL,
               expiry        TEXT NOT NULL,
               dte_at_entry  REAL,
               lots          INTEGER NOT NULL,
               lot_size      INTEGER NOT NULL,
               entry_at      TEXT NOT NULL,
               entry_spot    REAL,
               entry_fills_json TEXT NOT NULL,
               entry_credit  REAL,
               max_loss      REAL,
               margin        REAL,
               margin_source TEXT,
               exit_at       TEXT,
               exit_spot     REAL,
               exit_fills_json TEXT,
               exit_reason   TEXT,
               breach_seen   INTEGER,
               breach_at     TEXT,
               mark_stop_seen INTEGER,
               mark_stop_at  TEXT,
               night_type    TEXT,
               gap_pct       REAL,
               settled_json  TEXT,
               note          TEXT
           )"###,
        r###"CREATE INDEX IF NOT EXISTS ix_option_arms_variant
               ON option_arms(variant_id, session_date)"###,
        r###"CREATE INDEX IF NOT EXISTS ix_option_arms_open
               ON option_arms(exit_at) WHERE exit_at IS NULL"###,
    ]),
    (10, &[
        r###"ALTER TABLE option_arms ADD COLUMN expiry_eve INTEGER"###,
        r###"ALTER TABLE option_arms ADD COLUMN elm_charged REAL"###,
    ]),
    (11, &[
        r###"CREATE TABLE option_arms_v11(
               arm_id        TEXT PRIMARY KEY,
               variant_id    TEXT NOT NULL REFERENCES option_variants(variant_id),
               strategy      TEXT NOT NULL,
               arm           TEXT NOT NULL,
               session_date  TEXT NOT NULL,
               expiry        TEXT NOT NULL,
               dte_at_entry  REAL,
               lots          INTEGER NOT NULL,
               lot_size      INTEGER NOT NULL,
               entry_at      TEXT NOT NULL,
               entry_spot    REAL,
               entry_fills_json TEXT NOT NULL,
               entry_credit  REAL,
               max_loss      REAL,
               margin        REAL,
               margin_source TEXT,
               exit_at       TEXT,
               exit_spot     REAL,
               exit_fills_json TEXT,
               exit_reason   TEXT,
               breach_seen   INTEGER,
               breach_at     TEXT,
               mark_stop_seen INTEGER,
               mark_stop_at  TEXT,
               spot_move_pct REAL,
               settled_json  TEXT,
               note          TEXT
           )"###,
        r###"INSERT INTO option_arms_v11
               SELECT arm_id, variant_id, strategy, arm, session_date, expiry,
                      dte_at_entry, lots, lot_size, entry_at, entry_spot,
                      entry_fills_json, entry_credit, max_loss, margin, margin_source,
                      exit_at, exit_spot, exit_fills_json, exit_reason, breach_seen,
                      breach_at, mark_stop_seen, mark_stop_at, gap_pct, settled_json, note
               FROM option_arms WHERE arm <> 'overnight'"###,
        r###"DROP TABLE option_arms"###,
        r###"ALTER TABLE option_arms_v11 RENAME TO option_arms"###,
        r###"CREATE INDEX IF NOT EXISTS ix_option_arms_variant
               ON option_arms(variant_id, session_date)"###,
        r###"CREATE INDEX IF NOT EXISTS ix_option_arms_open
               ON option_arms(exit_at) WHERE exit_at IS NULL"###,
        r###"DELETE FROM option_variants WHERE arm = 'overnight'"###,
    ]),
    (12, &[
        r###"ALTER TABLE option_arms ADD COLUMN contracts_json TEXT"###,
        r###"ALTER TABLE option_arms ADD COLUMN underlying_token INTEGER"###,
    ]),
    (13, &[
        r###"ALTER TABLE rebalance_orders ADD COLUMN order_id TEXT"###,
        r###"ALTER TABLE rebalance_orders ADD COLUMN reconciled_at TEXT"###,
    ]),
];
