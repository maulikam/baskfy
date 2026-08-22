"""SQLite persistence for portfolio analytics (data/portfolio.db).

One connection helper, WAL journaling, versioned migrations via PRAGMA user_version.
Every write is idempotent: re-running a day's snapshot never duplicates a row and never
silently changes a stored value unless you explicitly pass force=True.

The portfolio index (base 100 at inception) is DERIVED, never accumulated: rechain_index()
recomputes the whole series from stored NAVs + cashflows in date order. That makes
backdated inserts self-healing and makes re-runs bit-for-bit reproducible.

CLI:
    python -m app.analytics.db --init          # create/migrate the schema
    python -m app.analytics.db --info          # schema version, row counts, date span
"""
from __future__ import annotations

import contextlib
import csv
import json
import os
import sqlite3
from typing import Any
from typing import Iterator, Sequence

from .. import config as C

SCHEMA_VERSION = 13

# --- schema ---------------------------------------------------------------------------
# Column sets are fixed by the analytics spec; extra *indexes* are fine, extra columns are
# not (phases 2-4 read these tables by name).
_MIGRATIONS: dict[int, Sequence[str]] = {
    1: (
        """CREATE TABLE IF NOT EXISTS snapshots(
               date          TEXT PRIMARY KEY,
               nav           REAL NOT NULL,
               invested      REAL NOT NULL,
               cash          REAL NOT NULL,
               holdings_json TEXT NOT NULL,
               index_value   REAL
           )""",
        """CREATE TABLE IF NOT EXISTS cashflows(
               id     INTEGER PRIMARY KEY AUTOINCREMENT,
               date   TEXT NOT NULL,
               amount REAL NOT NULL,        -- invest NEGATIVE, withdraw/terminal POSITIVE
               type   TEXT NOT NULL
           )""",
        # Makes cashflow imports re-runnable. Caveat: two genuinely identical flows on the
        # same day collapse to one — split them by type, or adjust by a paisa, if that ever
        # matters. Idempotent import is worth more here than that edge case.
        """CREATE UNIQUE INDEX IF NOT EXISTS ux_cashflows_natural
               ON cashflows(date, amount, type)""",
        """CREATE TABLE IF NOT EXISTS benchmark(
               index_name TEXT NOT NULL,
               date       TEXT NOT NULL,
               close      REAL,
               tri        REAL,
               PRIMARY KEY(index_name, date)
           )""",
        """CREATE TABLE IF NOT EXISTS rebalance_versions(
               version_id        TEXT PRIMARY KEY,
               created_ts        REAL NOT NULL,
               constituents_json TEXT NOT NULL,
               weights_json      TEXT NOT NULL,
               note              TEXT
           )""",
        """CREATE TABLE IF NOT EXISTS rebalance_orders(
               id                INTEGER PRIMARY KEY AUTOINCREMENT,
               version_id        TEXT NOT NULL REFERENCES rebalance_versions(version_id),
               symbol            TEXT NOT NULL,
               side              TEXT NOT NULL,
               planned_qty       INTEGER,
               planned_ref_price REAL,
               filled_qty        INTEGER DEFAULT 0,
               avg_fill_price    REAL,
               status            TEXT DEFAULT 'PENDING'
           )""",
        """CREATE INDEX IF NOT EXISTS ix_rebalance_orders_version
               ON rebalance_orders(version_id)""",
        """CREATE TABLE IF NOT EXISTS trades(
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
           )""",
        """CREATE INDEX IF NOT EXISTS ix_trades_symbol ON trades(symbol)""",
        """CREATE INDEX IF NOT EXISTS ix_trades_exit_ts ON trades(exit_ts)""",
    ),
    # --- regime overlay (checkpoint 2) -------------------------------------------------
    2: (
        # Daily index OHLC cache. is_final=0 marks a candle for a session that has not
        # closed yet; signal code must never consume one.
        """CREATE TABLE IF NOT EXISTS index_series(
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
           )""",
        """CREATE INDEX IF NOT EXISTS ix_index_series_date ON index_series(date)""",

        # Signal decisions. A deterministic evaluation_id may appear MANY times: once per
        # DRY_RUN/observe preview (each with its own run_id) plus at most one canonical
        # committed row. The partial unique index is what enforces "committed exactly once"
        # while leaving previews free to accumulate for audit.
        """CREATE TABLE IF NOT EXISTS regime_evaluations(
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
           )""",
        """CREATE UNIQUE INDEX IF NOT EXISTS ux_regime_canonical
               ON regime_evaluations(evaluation_id) WHERE run_id = 'canonical'""",
        """CREATE INDEX IF NOT EXISTS ix_regime_week
               ON regime_evaluations(scheduled_week_end)""",

        # Exposure reconciliation is a SEPARATE event stream from the signal decision:
        # one evaluation can accumulate many observations as fills arrive or fail.
        """CREATE TABLE IF NOT EXISTS regime_exposure(
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
           )""",
        """CREATE INDEX IF NOT EXISTS ix_regime_exposure_eval
               ON regime_exposure(evaluation_id)""",

        # Daily breadth, persisted so historical tier replay is possible at all. Without
        # this, any backtest is a price-only proxy and must say so.
        """CREATE TABLE IF NOT EXISTS breadth_readings(
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
           )""",

        # Frozen full-risk book composition per evaluation. The last committed R1 row is
        # the fallback weight source, which is why the tier is stored alongside.
        """CREATE TABLE IF NOT EXISTS regime_book_snapshots(
               evaluation_id      TEXT PRIMARY KEY,
               scheduled_week_end TEXT NOT NULL,
               policy_tier        TEXT NOT NULL,
               weights_json       TEXT NOT NULL,
               source             TEXT NOT NULL,
               coverage_pct       REAL NOT NULL,
               created_at         TEXT NOT NULL
           )""",
        """CREATE INDEX IF NOT EXISTS ix_book_snapshots_week
               ON regime_book_snapshots(scheduled_week_end)""",
    ),
    # --- runtime-editable settings ------------------------------------------------------
    3: (
        # Overrides that beat the environment at runtime. Resolution is
        # DB -> env -> code default, so .env stays the bootstrap and this is the live layer.
        """CREATE TABLE IF NOT EXISTS settings(
               key        TEXT PRIMARY KEY,
               value      TEXT NOT NULL,
               updated_at TEXT NOT NULL,
               note       TEXT
           )""",
        # Every change to a live trading parameter is recorded. Without this you cannot
        # answer "what was the stop multiple when that trade was placed?".
        """CREATE TABLE IF NOT EXISTS settings_audit(
               id         INTEGER PRIMARY KEY AUTOINCREMENT,
               key        TEXT NOT NULL,
               old_value  TEXT,
               new_value  TEXT NOT NULL,
               changed_at TEXT NOT NULL,
               note       TEXT
           )""",
        """CREATE INDEX IF NOT EXISTS ix_settings_audit_key
               ON settings_audit(key, changed_at)""",
    ),
    # --- operations run log --------------------------------------------------------------
    4: (
        # Every operation run from the interface, with its exact argv and full output.
        # An ops page without a record of what it did is just a pile of buttons.
        """CREATE TABLE IF NOT EXISTS ops_jobs(
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
           )""",
        """CREATE INDEX IF NOT EXISTS ix_ops_jobs_started ON ops_jobs(started_at DESC)""",
    ),
    5: (
        # Raw broker fills — the durable source of truth that `trades` is DERIVED from.
        #
        # Why this table has to exist: Kite Connect's /trades endpoint is same-day only
        # (it takes no date parameter and the book is flushed nightly), so history can
        # only ever be seeded once from a Console export. But FIFO cannot be computed
        # incrementally — a sell captured today has to consume a lot bought months ago.
        # Keeping every fill means both sources write here and lots are always rebuilt
        # from the complete picture, in one code path.
        #
        # trade_id is the broker's own identifier where there is one; a CSV row without
        # it gets a deterministic synthetic key, so re-importing a file is still a no-op.
        """CREATE TABLE IF NOT EXISTS fills(
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
           )""",
        """CREATE INDEX IF NOT EXISTS ix_fills_symbol ON fills(symbol, when_ts)""",
    ),
    6: (
        # Outcome of each daily collection, whoever launched it.
        #
        # Deliberately NOT ops_jobs: that table records "a process was started from the
        # page". This records "the collection produced this result", which is a different
        # fact — a scheduled run has no ops_jobs row at all, and the thing worth alerting
        # on is a streak of failures, not an exit code. Per-step detail is kept because
        # "snapshot failed, everything else landed" and "nothing ran" both exit non-zero
        # and need very different responses.
        """CREATE TABLE IF NOT EXISTS daily_runs(
               id           INTEGER PRIMARY KEY AUTOINCREMENT,
               ran_at       TEXT NOT NULL,
               session_date TEXT NOT NULL,
               trigger      TEXT NOT NULL,   -- schedule | cli | page
               outcome      TEXT NOT NULL,   -- ok | partial | failed | auth
               failed_steps INTEGER NOT NULL DEFAULT 0,
               steps_json   TEXT NOT NULL,
               duration_s   REAL
           )""",
        """CREATE INDEX IF NOT EXISTS ix_daily_runs_ran ON daily_runs(ran_at DESC)""",
    ),
    7: (
        # Links a stored plan back to the regime decision that motivated it.
        #
        # rebalance_versions and rebalance_orders were created in v1 and, until now, never
        # written by anything — while metrics.slippage() read them and the /regime page
        # needed a plan id it could not get. A regime tier on screen says nothing about
        # whether the book actually moved, and without this column there is no way to ask
        # "which plan implemented that decision, and what did it actually fill".
        #
        # Nullable on purpose: a plan built with the overlay off has no evaluation.
        """ALTER TABLE rebalance_versions ADD COLUMN evaluation_id TEXT""",
        """CREATE INDEX IF NOT EXISTS ix_rebalance_versions_eval
               ON rebalance_versions(evaluation_id)""",
        """CREATE INDEX IF NOT EXISTS ix_rebalance_versions_created
               ON rebalance_versions(created_ts DESC)""",
    ),
    8: (
        # Corporate actions that move quantity WITHOUT a trade, so FIFO lots can be
        # rebuilt correctly instead of merely reporting a mismatch.
        #
        # Bonus and split are stored separately because Indian tax treats them very
        # differently. A bonus share has NIL cost of acquisition and its holding period
        # starts at allotment, so selling one is short-term tax on the full proceeds. A
        # split just subdivides an existing lot: cost per share falls, and the holding
        # period is INHERITED from the original purchase. Modelling both as "extra
        # shares" would silently misprice one of them.
        #
        # ratio is stored as new:old, so a 1:1 bonus is (1,1) and a 1:2 split is (2,1).
        """CREATE TABLE IF NOT EXISTS corporate_actions(
               id         INTEGER PRIMARY KEY AUTOINCREMENT,
               symbol     TEXT NOT NULL,
               kind       TEXT NOT NULL,      -- bonus | split
               ex_date    TEXT NOT NULL,
               ratio_new  REAL NOT NULL,
               ratio_old  REAL NOT NULL,
               note       TEXT,
               created_at TEXT NOT NULL,
               UNIQUE(symbol, kind, ex_date)
           )""",
        """CREATE INDEX IF NOT EXISTS ix_corporate_actions_symbol
               ON corporate_actions(symbol, ex_date)""",
    ),
    9: (
        # Paper-only A/B research arms for the options work. Nothing here can place an
        # order; the table exists so an ENTRY observation and a later EXIT observation can
        # be paired across process runs, which a JSONL append log cannot do.
        #
        # Variants are PREREGISTERED with a hash and a registration timestamp. Expected
        # maximum Sharpe from N trials on a worthless strategy is ~1.6 at N=10
        # (Bailey/Borwein/Lopez de Prado/Zhu), so the count of variants tried is itself a
        # result and has to be recorded before the data arrives rather than reconstructed
        # from memory afterwards.
        """CREATE TABLE IF NOT EXISTS option_variants(
               variant_id   TEXT PRIMARY KEY,
               name         TEXT NOT NULL,
               arm          TEXT NOT NULL,      -- intraday (overnight dropped, v11)
               spec_json    TEXT NOT NULL,
               spec_hash    TEXT NOT NULL,
               registered_at TEXT NOT NULL,
               note         TEXT
           )""",
        # One row per opened paper position. exit_* stay NULL until it is closed, so an
        # arm abandoned mid-experiment is visible as an open row rather than vanishing.
        """CREATE TABLE IF NOT EXISTS option_arms(
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
           )""",
        """CREATE INDEX IF NOT EXISTS ix_option_arms_variant
               ON option_arms(variant_id, session_date)""",
        """CREATE INDEX IF NOT EXISTS ix_option_arms_open
               ON option_arms(exit_at) WHERE exit_at IS NULL""",
    ),
    10: (
        # Whether the hold ran into expiry morning, stored SEPARATELY from night_type.
        # The two are different axes: a night can be both a weekend and an expiry eve, and
        # collapsing them into one label would make that combination unrecoverable. The
        # label carries the headline; this column lets the axes be crossed.
        """ALTER TABLE option_arms ADD COLUMN expiry_eve INTEGER""",
        """ALTER TABLE option_arms ADD COLUMN elm_charged REAL""",
    ),
    11: (
        # The overnight arm was dropped on 16 Aug 2026: the system no longer holds any
        # option past the close (core/guards.py), so night_type, expiry_eve and
        # elm_charged describe a position that can never exist. gap_pct is renamed
        # spot_move_pct because with a single-session hold it measures an intraday move,
        # not an overnight gap, and a column whose name asserts the wrong thing is worse
        # than one that is merely unused.
        #
        # A rebuild rather than DROP COLUMN: no arm had been recorded yet, so nothing is
        # lost, and the table is left with no vestigial columns for a future reader to
        # wonder about. The overnight variants go too — they were preregistered but never
        # ran, so they consumed no look at the data and must not inflate the trial count
        # that governs the deflated Sharpe.
        """CREATE TABLE option_arms_v11(
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
           )""",
        """INSERT INTO option_arms_v11
               SELECT arm_id, variant_id, strategy, arm, session_date, expiry,
                      dte_at_entry, lots, lot_size, entry_at, entry_spot,
                      entry_fills_json, entry_credit, max_loss, margin, margin_source,
                      exit_at, exit_spot, exit_fills_json, exit_reason, breach_seen,
                      breach_at, mark_stop_seen, mark_stop_at, gap_pct, settled_json, note
               FROM option_arms WHERE arm <> 'overnight'""",
        """DROP TABLE option_arms""",
        """ALTER TABLE option_arms_v11 RENAME TO option_arms""",
        """CREATE INDEX IF NOT EXISTS ix_option_arms_variant
               ON option_arms(variant_id, session_date)""",
        """CREATE INDEX IF NOT EXISTS ix_option_arms_open
               ON option_arms(exit_at) WHERE exit_at IS NULL""",
        """DELETE FROM option_variants WHERE arm = 'overnight'""",
    ),
    12: (
        # The contracts an arm held, by instrument_token as well as by symbol.
        #
        # Kite's instruments dump lists only LIVE contracts: checked on 16 Aug 2026, zero
        # of the eighteen NIFTY expiries on file were in the past. Once a contract expires
        # its token cannot be looked up again, and historical_data takes a token, not a
        # symbol — so an option's price history becomes unreachable the moment it expires
        # unless the token was written down first. Recording it costs nothing now and is
        # the only thing that can make this data re-fetchable later.
        #
        # It also makes an open question answerable: whether a token recorded BEFORE expiry
        # still resolves AFTER it. That could not be tested, because no such token existed.
        # Two weeks of collection will settle it.
        """ALTER TABLE option_arms ADD COLUMN contracts_json TEXT""",
        # The underlying, for the same reason. Its token is stable and well known, but a
        # row that carries its own spot source needs no outside knowledge to be replayed.
        """ALTER TABLE option_arms ADD COLUMN underlying_token INTEGER""",
    ),
    13: (
        # The broker's order id, so a submitted order can be reconciled against the order
        # book later. Without it the only join is the symbol, which breaks the moment a
        # plan touches the same symbol twice.
        """ALTER TABLE rebalance_orders ADD COLUMN order_id TEXT""",
        """ALTER TABLE rebalance_orders ADD COLUMN reconciled_at TEXT""",
    ),
}


# --- connection -----------------------------------------------------------------------
#: M19 §3. "sqlite" (the default, and what the box runs) or "postgres" (the merged backend).
#: The desk's record of its own trades is the last thing that should move on a hunch, so this is
#: a deliberate opt-in and rolling back is the same one variable in reverse.
DB_BACKEND = os.getenv("DESK_DB_BACKEND", "sqlite")

#: Where M18's migration put the desk's tables. A schema rather than `public`, so the screener's
#: forty-two tables and the desk's nineteen cannot collide.
DB_SCHEMA = os.getenv("DESK_DB_SCHEMA", "desk")


@contextlib.contextmanager
def connect(path: str | None = None) -> Iterator[Any]:
    """The one and only connection helper. WAL + foreign keys + Row factory.

    isolation_level=None puts us in autocommit; use transaction() for multi-statement
    writes so a failure mid-way rolls back cleanly.

    On the Postgres backend (M19 §3) the same call returns `analytics.pg.Connection`, which
    presents the subset of sqlite3's interface the desk actually uses. An explicit `path` always
    means SQLite: a caller naming a file wants that file, and tests pass `tmp_path` constantly.
    """
    if path is None and DB_BACKEND == "postgres":
        from .pg import Connection                                            # noqa: PLC0415

        conn = Connection(f"{_pg_dsn()}?options=-csearch_path%3D{DB_SCHEMA}")
        try:
            yield conn
        finally:
            conn.close()
        return

    p = path or C.DB_PATH
    parent = os.path.dirname(p)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def _pg_dsn() -> str:
    return os.getenv("DESK_DATABASE_URL", "postgresql://baskfy:baskfy@localhost:5433/baskfy")


@contextlib.contextmanager
def transaction(conn: Any) -> Iterator[Any]:
    # BEGIN IMMEDIATE is SQLite's way of taking the write lock up front rather than discovering
    # a conflict half-way through. Postgres has no such spelling and does not need one.
    conn.execute("BEGIN IMMEDIATE" if not _is_postgres(conn) else "BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _is_postgres(conn: Any) -> bool:
    """Which backend a connection is, without importing psycopg to ask."""
    return conn.__class__.__module__.endswith("analytics.pg")


def schema_version(conn: Any) -> int:
    """The applied migration number.

    SQLite keeps it in the file header (`PRAGMA user_version`), which is elegant and has no
    Postgres equivalent — Postgres gets a one-row table in the desk schema instead. Same number,
    same meaning; the difference is confined to these six lines rather than to `migrate`.
    """
    if not _is_postgres(conn):
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version integer not null)")
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version (version) VALUES (0)")
        return 0
    return int(row[0])


def _set_schema_version(conn: Any, version: int) -> None:
    if not _is_postgres(conn):
        conn.execute(f"PRAGMA user_version={version}")
    else:
        conn.execute("UPDATE schema_version SET version = ?", (version,))


def migrate(conn: Any) -> int:
    """Apply pending migrations. Returns the resulting schema version."""
    have = schema_version(conn)
    for version in sorted(_MIGRATIONS):
        if version > have:
            with transaction(conn):
                for stmt in _MIGRATIONS[version]:
                    conn.execute(stmt)
                _set_schema_version(conn, version)
            have = version
    return have


def init_db(path: str | None = None) -> str:
    """Create/migrate the database. Safe to call on every process start."""
    p = path or C.DB_PATH
    with connect(p) as conn:
        migrate(conn)
    return p


# --- cashflows ------------------------------------------------------------------------
def record_cashflow(conn: sqlite3.Connection, date: str, amount: float, kind: str) -> bool:
    """Record an external cashflow. Sign convention: invest NEGATIVE, withdraw POSITIVE
    (the XIRR convention — money leaving your pocket is negative).

    Returns True if a new row was inserted, False if it already existed.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO cashflows(date, amount, type) VALUES(?,?,?)",
        (str(date), float(amount), str(kind)),
    )
    return cur.rowcount > 0


def import_cashflows_csv(conn: sqlite3.Connection, path: str) -> dict:
    """Load cashflows from a local CSV. Accepts either shape:

        date,amount,type          (amount signed: invest negative)
        date,debit,credit[,type]  (ledger style: debit = money in, credit = money out)

    Re-importing the same file is a no-op.
    """
    inserted = skipped = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            keys = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            date = keys.get("date") or keys.get("posting_date") or ""
            if not date:
                continue
            if "amount" in keys and keys["amount"]:
                amount = float(keys["amount"].replace(",", ""))
            else:
                debit = float((keys.get("debit") or "0").replace(",", "") or 0)
                credit = float((keys.get("credit") or "0").replace(",", "") or 0)
                # money INTO the account is an investment -> negative in XIRR terms
                amount = -(debit - credit)
            kind = keys.get("type") or ("invest" if amount < 0 else "withdraw")
            if record_cashflow(conn, date, amount, kind):
                inserted += 1
            else:
                skipped += 1
    return {"inserted": inserted, "skipped_existing": skipped}


def cashflows_by_date(conn: sqlite3.Connection) -> dict[str, float]:
    """date -> net flow INTO the portfolio (sign-flipped from the stored convention)."""
    rows = conn.execute("SELECT date, SUM(amount) AS amt FROM cashflows GROUP BY date")
    return {r["date"]: -float(r["amt"]) for r in rows}


# --- snapshots ------------------------------------------------------------------------
def upsert_snapshot(conn: sqlite3.Connection, snap: dict, *, force: bool = False) -> str:
    """Write one EOD snapshot. Returns 'inserted' | 'updated' | 'unchanged'.

    Default behaviour is insert-if-absent: re-running a day's snapshot leaves the stored
    values exactly as they were. Pass force=True to overwrite with freshly measured values
    (e.g. the first run happened before the close).
    """
    date = str(snap["date"])
    payload = (
        date,
        float(snap["nav"]),
        float(snap["invested"]),
        float(snap["cash"]),
        snap["holdings_json"] if isinstance(snap["holdings_json"], str)
        else json.dumps(snap["holdings_json"], separators=(",", ":"), default=str),
    )
    existing = conn.execute("SELECT date FROM snapshots WHERE date=?", (date,)).fetchone()
    if existing and not force:
        return "unchanged"
    if existing:
        conn.execute(
            "UPDATE snapshots SET nav=?, invested=?, cash=?, holdings_json=? WHERE date=?",
            (payload[1], payload[2], payload[3], payload[4], date),
        )
        return "updated"
    conn.execute(
        "INSERT INTO snapshots(date, nav, invested, cash, holdings_json) VALUES(?,?,?,?,?)",
        payload,
    )
    return "inserted"


def rechain_index(conn: sqlite3.Connection, base: float | None = None) -> int:
    """Recompute index_value for the whole snapshot series (base 100 at inception).

    Daily time-weighted return, external flows neutralised so contributions and
    withdrawals never register as performance:

        r_t   = (nav_t - flow_t) / nav_{t-1} - 1
        idx_t = idx_{t-1} * (1 + r_t)

    The inception row is assigned the base directly — no arithmetic — so it is exactly
    100.0. Days with no capital at risk yesterday (nav_{t-1} <= 0) carry the index forward
    unchanged rather than dividing by zero.

    Returns the number of rows updated.
    """
    b = C.INDEX_BASE if base is None else float(base)
    flows = cashflows_by_date(conn)
    rows = conn.execute("SELECT date, nav FROM snapshots ORDER BY date").fetchall()

    updates: list[tuple[float, str]] = []
    idx: float | None = None
    prev_nav: float | None = None
    for r in rows:
        date, nav = r["date"], float(r["nav"])
        if idx is None:
            idx = b                                   # inception: exactly the base
        elif prev_nav is not None and prev_nav > 0:
            flow = flows.get(date, 0.0)
            idx *= 1.0 + ((nav - flow) / prev_nav - 1.0)
        updates.append((idx, date))
        prev_nav = nav

    conn.executemany("UPDATE snapshots SET index_value=? WHERE date=?", updates)
    return len(updates)


def save_snapshot(conn: sqlite3.Connection, snap: dict, *, force: bool = False) -> str:
    """upsert + rechain in one transaction — the index series is never left stale."""
    with transaction(conn):
        outcome = upsert_snapshot(conn, snap, force=force)
        rechain_index(conn)
    return outcome


def get_snapshot(conn: sqlite3.Connection, date: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM snapshots WHERE date=?", (str(date),)).fetchone()


def snapshot_series(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT date, nav, invested, cash, index_value FROM snapshots ORDER BY date"
    ).fetchall()


def inception_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MIN(date) AS d FROM snapshots").fetchone()
    return row["d"] if row and row["d"] else None


def suggest_unrecorded_cashflows(conn: sqlite3.Connection, tol: float = 0.02) -> list[dict]:
    """Detection AID ONLY — never auto-inserts.

    kiteconnect exposes no ledger endpoint, so the cashflows table is authoritative and
    manual (or CSV-imported). This flags days where cash moved sharply while the invested
    leg barely moved, which usually means a deposit/withdrawal went unrecorded. Review the
    hits and record the real flows with record_cashflow().
    """
    rows = snapshot_series(conn)
    known = cashflows_by_date(conn)
    out = []
    for prev, cur in zip(rows, rows[1:]):
        d_cash = float(cur["cash"]) - float(prev["cash"])
        d_inv = float(cur["invested"]) - float(prev["invested"])
        base = float(prev["nav"]) or 1.0
        # cash jumped, but not because a trade moved money between cash and holdings
        unexplained = d_cash + d_inv
        if abs(unexplained) / base > tol and cur["date"] not in known:
            out.append({"date": cur["date"], "unexplained": round(unexplained, 2),
                        "cash_delta": round(d_cash, 2), "invested_delta": round(d_inv, 2)})
    return out


# --- CLI ------------------------------------------------------------------------------
def _info(path: str) -> None:
    with connect(path) as conn:
        migrate(conn)
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        print(f"db            : {path}")
        print(f"schema version: {ver}   journal_mode: {mode}")
        tables = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            " ORDER BY name")]
        for t in tables:
            n = conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
            print(f"  {t:<20} {n:>7} rows")
        rows = snapshot_series(conn)
        if rows:
            print(f"snapshots span: {rows[0]['date']} -> {rows[-1]['date']}")
            print(f"index          : {rows[0]['index_value']:.4f} -> {rows[-1]['index_value']:.4f}")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="portfolio analytics database")
    ap.add_argument("--db", default=C.DB_PATH)
    ap.add_argument("--init", action="store_true", help="create/migrate schema")
    ap.add_argument("--info", action="store_true", help="show schema version + row counts")
    a = ap.parse_args()
    if a.init or not a.info:
        print("initialised:", init_db(a.db))
    if a.info:
        _info(a.db)
