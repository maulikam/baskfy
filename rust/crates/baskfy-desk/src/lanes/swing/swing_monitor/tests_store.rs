//! `tests/test_swing_monitor.py`'s store and context tests: `TestTheSignalStore`,
//! `TestTheContextIsReReadPerTrigger`, `TestReservedSlotsAndHalfRisk`,
//! `TestTheGateIsReReadLive`, `TestTheKiteLevel` and `TestTelemetryNeverStopsAVerdict`'s store
//! half. Python answers the store's reads from a `FakeConn`; here the same rows sit in a real
//! SQLite database attached as `public` ([`Book`]), so the store's own SQL runs unchanged and
//! every assertion reads what was written. Where Python counts statements (`context_reads`,
//! `index_reads`), the Rust test observes the same fact another way, said at each test.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::{Arc, Mutex};

use baskfy_core::swing::market::{ExposureTier, MarketGate};
use baskfy_core::swing::opening_range::{TriggerState, TriggerVerdict};
use baskfy_core::swing::plan::SwingAccount;
use baskfy_core::swing::setups::{Setup, SwingConfig};
use chrono::{DateTime, FixedOffset, NaiveDate, TimeZone};
use rust_decimal::Decimal;

use super::runner::{GatedHeartbeat, PgSignalStore, load_config};
use crate::clock::ist;
use crate::ids::IdSource;
use crate::lanes::swing::breakout::{Signal, SignalStore, WatchedName};
use crate::lanes::swing::context::{SignalContext, load_context};
use crate::lanes::swing::live_gate::LiveLevel;
use crate::settings::Settings;
use crate::storage::{Conn, Db, DbValue, Param, Row};

pub(super) const SCHEMA_SQL: &[&str] = &[
    "CREATE TABLE public.instrument (id INTEGER PRIMARY KEY, symbol TEXT, kite_token INTEGER)",
    "CREATE TABLE public.sw_market_daily (user_id INTEGER, date TEXT, gate TEXT, \
     exposure_level INTEGER, max_open_positions INTEGER, max_exposure_pct REAL, \
     new_entries_allowed BOOLEAN, drawdown_locked BOOLEAN, constituent_count INTEGER, \
     pct_up_strong_1m REAL, pct_new_52w_high REAL, pct_above_ma_slow REAL)",
    "CREATE TABLE public.sw_config (user_id INTEGER, sleeve_capital_inr TEXT, \
     first_live_sessions_left INTEGER, adr_min_pct REAL, turnover_min_inr REAL, \
     price_min REAL, risk_per_trade_pct REAL, max_position_pct REAL, max_open_positions INTEGER)",
    "CREATE TABLE public.sw_position (id INTEGER PRIMARY KEY, user_id INTEGER, \
     instrument_id INTEGER, entry_avg TEXT, quantity_open INTEGER, quantity_entered INTEGER, \
     state TEXT)",
    "CREATE TABLE public.sw_plan (id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT, \
     user_id INTEGER, as_of TEXT, source TEXT, built_at TEXT, expires_at TEXT, plan_hash TEXT, \
     gate TEXT, exposure_level INTEGER, total_risk_inr TEXT, total_new_exposure_inr TEXT)",
    "CREATE TABLE public.sw_plan_line (id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, \
     user_id INTEGER, kind TEXT, instrument_id INTEGER, setup TEXT, quantity INTEGER, \
     trigger TEXT, stop TEXT, risk_inr TEXT, position_value TEXT, trail TEXT, note TEXT, \
     state TEXT, client_id TEXT, position_id INTEGER, updated_at TEXT)",
    "CREATE TABLE public.sw_plan_skip (id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, \
     user_id INTEGER, instrument_id INTEGER, symbol TEXT, reason TEXT, detail TEXT)",
    "CREATE TABLE public.sw_setup_daily (user_id INTEGER, instrument_id INTEGER, date TEXT, \
     adr_pct TEXT, turnover_avg INTEGER, score TEXT)",
    "CREATE TABLE public.sw_watch (id INTEGER PRIMARY KEY, user_id INTEGER, \
     instrument_id INTEGER, setup TEXT, trigger TEXT, focus BOOLEAN, adr_pct TEXT, score TEXT, \
     state TEXT)",
    "CREATE TABLE public.sw_signal (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, \
     watch_id INTEGER, instrument_id INTEGER, setup TEXT, session_date TEXT, raised_at TEXT, \
     state TEXT, or_window_minutes INTEGER, range_high TEXT, range_low TEXT, \
     low_of_day TEXT, last_price TEXT, entry TEXT, stop TEXT, plan_line_id INTEGER)",
    "CREATE TABLE public.sw_session (user_id INTEGER, session_date TEXT, mode TEXT, \
     monitor_ran BOOLEAN, signals INTEGER, notes TEXT, PRIMARY KEY (user_id, session_date))",
    "CREATE TABLE public.lv_heartbeat (user_id INTEGER, process TEXT, state TEXT, detail TEXT, \
     at TEXT, PRIMARY KEY (user_id, process))",
];

/// The index tables SW30's live gate reads; left out to prove a test never read them.
pub(super) const INDEX_SQL: &[&str] = &[
    "CREATE TABLE public.index_def (id INTEGER PRIMARY KEY, slug TEXT)",
    "CREATE TABLE public.index_snapshot_daily (index_id INTEGER, date TEXT, level REAL)",
];

pub(super) struct FixedIds;

impl IdSource for FixedIds {
    fn uuid4_bytes(&self) -> [u8; 16] {
        [0x42; 16]
    }
}

pub(super) fn day() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 8, 19).unwrap()
}

/// `IN_SESSION`: 10:30 on `DAY` (a Wednesday).
pub(super) fn in_session() -> DateTime<FixedOffset> {
    ist()
        .from_local_datetime(&day().and_hms_opt(10, 30, 0).unwrap())
        .single()
        .unwrap()
}

pub(super) fn d(s: &str) -> Decimal {
    s.parse().unwrap()
}

/// `_name(symbol, token, setup=FLAG, pivot="100")`.
pub(super) fn name(symbol: &str, token: i64) -> WatchedName {
    named(symbol, token, Setup::Flag, Some("100"))
}

pub(super) fn named(symbol: &str, token: i64, setup: Setup, pivot: Option<&str>) -> WatchedName {
    WatchedName {
        watch_id: token,
        instrument_id: token * 10,
        symbol: symbol.to_owned(),
        token,
        setup,
        pivot_high: pivot.map(d),
        upper_circuit: None,
        focus: false,
    }
}

/// `_signal(name, state, entry=…, stop=…)`.
pub(super) fn signal_of(
    watch: WatchedName,
    state: TriggerState,
    entry: Option<&str>,
    stop: Option<&str>,
) -> Signal {
    Signal {
        watch,
        at: day().and_hms_opt(9, 31, 0).unwrap(),
        verdict: TriggerVerdict {
            state,
            entry: entry.map(d),
            stop: stop.map(d),
            range_high: Some(d("99.5")),
            range_low: Some(d("98.0")),
        },
        last_price: entry.map_or_else(|| d("52.5"), d),
        low_of_day: d("97.8"),
        window_minutes: 5,
    }
}

pub(super) fn triggered(watch: WatchedName, entry: &str, stop: &str) -> Signal {
    signal_of(watch, TriggerState::Triggered, Some(entry), Some(stop))
}

/// A held position: `(id, symbol, token, entry_avg, quantity_open, quantity_entered)`.
pub(super) type Held = (i64, &'static str, i64, &'static str, i64, i64);
/// A line taken today: `(id, symbol, token, quantity, trigger, state, position_id)`.
pub(super) type Taken = (
    i64,
    &'static str,
    i64,
    i64,
    &'static str,
    &'static str,
    Option<i64>,
);

/// `FakeConn(gate=, rung=, capital=, stats=, first_live_left=)` and its mutable lists, as rows.
pub(super) struct Book {
    pub(super) gate: &'static str,
    pub(super) rung: i64,
    pub(super) capital: &'static str,
    pub(super) first_live_left: i64,
    /// `(symbol, token, adr_pct, turnover_avg, score)`; `AAA` (5.00, 100M, 72.00) is always there.
    pub(super) stats: Vec<(&'static str, i64, &'static str, i64, &'static str)>,
    pub(super) held: Vec<Held>,
    pub(super) taken: Vec<Taken>,
    /// `(symbol, token)` with a PROPOSED PENDING_RANGE line on today's plan.
    pub(super) reserved: Vec<(&'static str, i64)>,
    /// `(symbol, token, adr_pct, score)` WATCHING rows.
    pub(super) watch_stats: Vec<(&'static str, i64, &'static str, &'static str)>,
    pub(super) no_market: bool,
    pub(super) pct_up_strong_1m: f64,
    pub(super) drawdown_locked: bool,
    /// The benchmark's published levels before `DAY`, oldest first; `None` leaves the index
    /// tables out altogether.
    pub(super) index_levels: Option<Vec<f64>>,
    /// The person's sizing knobs on `sw_config` (`load_config`'s columns).
    pub(super) knobs: (f64, f64, f64, f64, f64, i64),
}

impl Default for Book {
    fn default() -> Self {
        Self {
            gate: "GREEN",
            rung: 3,
            capital: "1000000",
            first_live_left: 0,
            stats: Vec::new(),
            held: Vec::new(),
            taken: Vec::new(),
            reserved: Vec::new(),
            watch_stats: Vec::new(),
            no_market: false,
            pct_up_strong_1m: 10.09,
            drawdown_locked: false,
            index_levels: Some(vec![100.0; 249]),
            knobs: (4.0, 50_000_000.0, 20.0, 0.5, 20.0, 10),
        }
    }
}

async fn exec(conn: &mut Conn, sql: &str, params: &[Param]) {
    conn.execute(sql, params).await.unwrap();
}

async fn instrument(conn: &mut Conn, symbol: &str, token: i64) {
    exec(
        conn,
        "INSERT OR IGNORE INTO public.instrument VALUES (?, ?, ?)",
        &[
            Param::Int(token * 10),
            Param::Text(symbol.to_owned()),
            Param::Int(token),
        ],
    )
    .await;
}

impl Book {
    /// The database: a main file and `public` attached beside it.
    pub(super) async fn open(&self, dir: &std::path::Path) -> Conn {
        let mut conn = Db::connect_sqlite_path(&dir.join("main.db")).await.unwrap();
        let public = dir.join("public.db").to_string_lossy().into_owned();
        exec(
            &mut conn,
            "ATTACH DATABASE ? AS public",
            &[Param::Text(public)],
        )
        .await;
        for sql in SCHEMA_SQL {
            exec(&mut conn, sql, &[]).await;
        }
        let tiers = [(2, 25.0), (4, 50.0), (6, 75.0), (10, 100.0)];
        let (count, pct) = tiers[usize::try_from(self.rung).unwrap()];
        if !self.no_market {
            exec(
                &mut conn,
                "INSERT INTO public.sw_market_daily VALUES (1, '2026-08-18', ?, ?, ?, ?, ?, ?, \
                 500, ?, 3.0, 40.0)",
                &[
                    Param::Text(self.gate.to_owned()),
                    Param::Int(self.rung),
                    Param::Int(count),
                    Param::Float(pct),
                    Param::Int(i64::from(self.gate != "RED")),
                    Param::Int(i64::from(self.drawdown_locked)),
                    Param::Float(self.pct_up_strong_1m),
                ],
            )
            .await;
        }
        let k = self.knobs;
        exec(
            &mut conn,
            "INSERT INTO public.sw_config VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
            &[
                Param::Text(self.capital.to_owned()),
                Param::Int(self.first_live_left),
                Param::Float(k.0),
                Param::Float(k.1),
                Param::Float(k.2),
                Param::Float(k.3),
                Param::Float(k.4),
                Param::Int(k.5),
            ],
        )
        .await;
        let mut stats = vec![("AAA", 1_i64, "5.00", 100_000_000_i64, "72.00")];
        stats.extend(self.stats.iter().copied());
        for (symbol, token, adr, turnover, score) in stats {
            instrument(&mut conn, symbol, token).await;
            exec(
                &mut conn,
                "INSERT INTO public.sw_setup_daily VALUES (1, ?, '2026-08-18', ?, ?, ?)",
                &[
                    Param::Int(token * 10),
                    Param::Text(adr.to_owned()),
                    Param::Int(turnover),
                    Param::Text(score.to_owned()),
                ],
            )
            .await;
        }
        for (id, symbol, token, entry, open, entered) in &self.held {
            instrument(&mut conn, symbol, *token).await;
            exec(
                &mut conn,
                "INSERT INTO public.sw_position VALUES (?, 1, ?, ?, ?, ?, 'OPEN')",
                &[
                    Param::Int(*id),
                    Param::Int(token * 10),
                    Param::Text((*entry).to_owned()),
                    Param::Int(*open),
                    Param::Int(*entered),
                ],
            )
            .await;
        }
        exec(
            &mut conn,
            "INSERT INTO public.sw_plan (id, plan_id, user_id, as_of, source) \
             VALUES (1, 'morning', 1, '2026-08-19', 'MORNING')",
            &[],
        )
        .await;
        for (id, symbol, token, qty, trigger, state, position) in &self.taken {
            instrument(&mut conn, symbol, *token).await;
            exec(
                &mut conn,
                "INSERT INTO public.sw_plan_line (id, plan_id, user_id, kind, instrument_id, \
                 quantity, trigger, state, position_id, client_id) \
                 VALUES (?, 1, 1, 'BUY_ON_TRIGGER', ?, ?, ?, ?, ?, ?)",
                &[
                    Param::Int(*id + 1000),
                    Param::Int(token * 10),
                    Param::Int(*qty),
                    Param::Text((*trigger).to_owned()),
                    Param::Text((*state).to_owned()),
                    position.map_or(Param::Null, Param::Int),
                    Param::Text(format!("morning:{symbol}:BUY_ON_TRIGGER")),
                ],
            )
            .await;
        }
        for (symbol, token) in &self.reserved {
            instrument(&mut conn, symbol, *token).await;
            exec(
                &mut conn,
                "INSERT INTO public.sw_plan_line (plan_id, user_id, kind, instrument_id, \
                 quantity, state, note, client_id) \
                 VALUES (1, 1, 'PENDING_RANGE', ?, 0, 'PROPOSED', 'held for the range', ?)",
                &[
                    Param::Int(token * 10),
                    Param::Text(format!("morning:{symbol}:PENDING_RANGE")),
                ],
            )
            .await;
        }
        for (i, (symbol, token, adr, score)) in self.watch_stats.iter().enumerate() {
            instrument(&mut conn, symbol, *token).await;
            exec(
                &mut conn,
                "INSERT INTO public.sw_watch (id, user_id, instrument_id, setup, adr_pct, \
                 score, state, focus) VALUES (?, 1, ?, 'EP', ?, ?, 'WATCHING', 0)",
                &[
                    Param::Int(i64::try_from(i).unwrap() + 500),
                    Param::Int(token * 10),
                    Param::Text((*adr).to_owned()),
                    Param::Text((*score).to_owned()),
                ],
            )
            .await;
        }
        if let Some(levels) = &self.index_levels {
            for sql in INDEX_SQL {
                exec(&mut conn, sql, &[]).await;
            }
            exec(
                &mut conn,
                "INSERT INTO public.index_def VALUES (1, 'nifty-smallcap-250'), (2, 'nifty-500')",
                &[],
            )
            .await;
            let n = levels.len();
            for (i, level) in levels.iter().enumerate() {
                let back = i64::try_from(n - i).unwrap();
                let date = day() - chrono::Duration::days(back);
                for (index_id, value) in [(1, *level), (2, 1.0e6)] {
                    exec(
                        &mut conn,
                        "INSERT INTO public.index_snapshot_daily VALUES (?, ?, ?)",
                        &[
                            Param::Int(index_id),
                            Param::Text(date.format("%Y-%m-%d").to_string()),
                            Param::Float(value),
                        ],
                    )
                    .await;
                }
            }
        }
        conn
    }
}

/// A level that counts every ask (Python's `asked` list).
pub(super) struct Counting {
    pub(super) level: Mutex<Option<f64>>,
    pub(super) asked: Mutex<usize>,
}

impl Counting {
    pub(super) fn new(level: Option<f64>) -> Arc<Self> {
        Arc::new(Self {
            level: Mutex::new(level),
            asked: Mutex::new(0),
        })
    }
}

#[async_trait::async_trait]
impl LiveLevel for Counting {
    async fn level(&self) -> Option<f64> {
        *self.asked.lock().unwrap() += 1;
        *self.level.lock().unwrap()
    }
}

pub(super) fn flags(dry_run: bool, execution: bool) -> Settings {
    let mut s = Settings::defaults().unwrap();
    s.dry_run = dry_run;
    s.swing_execution_enabled = execution;
    s
}

/// `PgSignalStore(conn, user_id=1, day=DAY, config=DEFAULT_SWING_CONFIG)`.
pub(super) fn store_over(conn: Conn, settings: Settings) -> PgSignalStore {
    PgSignalStore {
        conn,
        user_id: 1,
        day: day(),
        config: SwingConfig::default(),
        live_level: None,
        context: None,
        lines_written: Vec::new(),
        pending_confirms: Vec::new(),
        notifier: None,
        notices: Vec::new(),
        settings,
        ids: Arc::new(FixedIds),
        now: Arc::new(|| {
            ist()
                .from_local_datetime(&day().and_hms_opt(9, 31, 0).unwrap())
                .single()
                .unwrap()
        }),
    }
}

pub(super) async fn rows(conn: &mut Conn, sql: &str) -> Vec<Row> {
    conn.execute(sql, &[]).await.unwrap().rows
}

pub(super) fn txt(r: &Row, key: &str) -> String {
    match r.get(key) {
        Some(DbValue::Text(t)) => t.clone(),
        Some(DbValue::Int(i)) => i.to_string(),
        Some(DbValue::Float(f)) => f.to_string(),
        Some(DbValue::Null) | None => "<null>".to_owned(),
        other => panic!("{key}: {other:?}"),
    }
}

pub(super) fn int_of(r: &Row, key: &str) -> i64 {
    match r.get(key) {
        Some(DbValue::Int(i)) => *i,
        other => panic!("{key}: {other:?}"),
    }
}

/// The lines the monitor wrote (ids beyond the morning's seeded ones).
pub(super) async fn new_lines(conn: &mut Conn) -> Vec<Row> {
    rows(
        conn,
        "SELECT * FROM public.sw_plan_line WHERE plan_id > 1 ORDER BY id",
    )
    .await
}

pub(super) async fn skips(conn: &mut Conn) -> Vec<(String, String)> {
    rows(
        conn,
        "SELECT reason, detail FROM public.sw_plan_skip ORDER BY id",
    )
    .await
    .iter()
    .map(|r| (txt(r, "reason"), txt(r, "detail")))
    .collect()
}

async fn context_of(book: &Book) -> (SignalContext, tempfile::TempDir) {
    let dir = tempfile::tempdir().unwrap();
    let mut conn = book.open(dir.path()).await;
    let ctx = load_context(
        &mut conn,
        1,
        day(),
        "public",
        None,
        None,
        "nifty-smallcap-250",
    )
    .await
    .unwrap();
    (ctx, dir)
}

// =============================================================================================
// TestTheSignalStore
// =============================================================================================

#[tokio::test]
async fn test_a_trigger_is_a_signal_row_and_a_one_line_signal_plan() {
    let dir = tempfile::tempdir().unwrap();
    let conn = Book::default().open(dir.path()).await;
    let mut s = store_over(conn, flags(true, false));
    s.raise_signal(&triggered(name("AAA", 1), "100.80", "97.80"))
        .await
        .unwrap();
    assert_eq!(
        rows(&mut s.conn, "SELECT * FROM public.sw_signal")
            .await
            .len(),
        1
    );
    let plans = rows(&mut s.conn, "SELECT * FROM public.sw_plan WHERE id > 1").await;
    assert_eq!(plans.len(), 1);
    let lines = new_lines(&mut s.conn).await;
    assert_eq!(lines.len(), 1);
    let line = &lines[0];
    // `04` §5: 0.5 % of ₹10 lakh over a ₹3 stop is 1,666 by risk; the 20 % cap at ₹100.80 is
    // 1,984, so risk wins.
    assert_eq!(txt(line, "kind"), "BUY_ON_TRIGGER");
    assert_eq!(int_of(line, "quantity"), 1666);
    assert_eq!(
        (txt(line, "trigger"), txt(line, "stop")),
        ("100.80".to_owned(), "97.80".to_owned())
    );
    assert!(
        txt(line, "client_id").ends_with(":AAA:BUY_ON_TRIGGER"),
        "client_id = plan_id:symbol:kind"
    );
    assert!(txt(line, "client_id").starts_with(&txt(&plans[0], "plan_id")));
    let built = crate::lanes::swing::values::parse_iso(&txt(&plans[0], "built_at")).unwrap();
    let expires = crate::lanes::swing::values::parse_iso(&txt(&plans[0], "expires_at")).unwrap();
    assert_eq!(
        built + chrono::Duration::minutes(30),
        expires,
        "30-minute expiry"
    );
    assert_eq!(s.lines_written, vec![int_of(line, "id")]);
    assert_eq!(s.pending_confirms.len(), 1, "a lined trigger is queued");
    let sig = &rows(&mut s.conn, "SELECT * FROM public.sw_signal").await[0];
    assert_eq!(int_of(sig, "plan_line_id"), int_of(line, "id"));
}

#[tokio::test]
async fn test_a_locked_name_is_a_signal_row_and_no_plan_line() {
    let dir = tempfile::tempdir().unwrap();
    let conn = Book::default().open(dir.path()).await;
    let mut s = store_over(conn, flags(true, false));
    let mut lck = name("LCK", 3);
    lck.upper_circuit = Some(d("52.5"));
    s.raise_signal(&signal_of(
        lck,
        TriggerState::LockedUpperCircuit,
        None,
        None,
    ))
    .await
    .unwrap();
    let sigs = rows(&mut s.conn, "SELECT * FROM public.sw_signal").await;
    assert_eq!(sigs.len(), 1);
    assert_eq!(txt(&sigs[0], "state"), "LOCKED_UPPER_CIRCUIT");
    assert!(
        rows(&mut s.conn, "SELECT * FROM public.sw_plan WHERE id > 1")
            .await
            .is_empty()
    );
    assert!(new_lines(&mut s.conn).await.is_empty());
    assert!(s.pending_confirms.is_empty());
}

#[tokio::test]
async fn test_under_a_red_gate_the_trigger_is_recorded_and_the_plan_says_why_not() {
    let dir = tempfile::tempdir().unwrap();
    let conn = Book {
        gate: "RED",
        ..Book::default()
    }
    .open(dir.path())
    .await;
    let mut s = store_over(conn, flags(true, false));
    s.raise_signal(&triggered(name("AAA", 1), "100.80", "97.80"))
        .await
        .unwrap();
    assert_eq!(
        rows(&mut s.conn, "SELECT * FROM public.sw_signal")
            .await
            .len(),
        1
    );
    assert_eq!(
        rows(&mut s.conn, "SELECT * FROM public.sw_plan WHERE id > 1")
            .await
            .len(),
        1
    );
    assert!(new_lines(&mut s.conn).await.is_empty());
    let sk = skips(&mut s.conn).await;
    assert_eq!(sk.len(), 1);
    assert_eq!(sk[0].0, "GATE_RED");
    assert!(s.pending_confirms.is_empty(), "a skip is never queued");
}

#[test]
fn test_the_store_never_names_a_placing_verb() {
    let code = super::tests::code_only(include_str!("runner.rs"));
    let start = code.find("impl PgSignalStore {").unwrap();
    let end = code.find("pub struct Confirmed").unwrap();
    let store = code[start..end].to_lowercase();
    for forbidden in [
        ".place(",
        "place_order",
        "place_gtt",
        "kc.",
        "gateway",
        "order_",
    ] {
        assert!(!store.contains(forbidden), "{forbidden}");
    }
}

// =============================================================================================
// TestTheContextIsReReadPerTrigger
// =============================================================================================

#[tokio::test]
async fn test_load_context_counts_positions_at_cost_and_todays_confirmed_lines() {
    let book = Book {
        rung: 0,
        held: vec![(1, "AAA", 1, "100.8", 1666, 1666)],
        taken: vec![
            // a live order accepted and not filled (SW7.1): held at the trigger, no position
            (7, "BBB", 2, 100, "210.0", "SENT", None),
            // a filled line whose position is the one above: counted once, at cost
            (8, "AAA", 1, 1666, "100.8", "FILLED", Some(1)),
        ],
        ..Book::default()
    };
    let (ctx, _dir) = context_of(&book).await;
    let symbols: Vec<&str> = ctx
        .account
        .open_symbols
        .iter()
        .map(String::as_str)
        .collect();
    assert_eq!(symbols, vec!["AAA", "BBB"]);
    assert_eq!(
        ctx.account.open_exposure_inr,
        d("100.8") * Decimal::from(1666) + Decimal::from(210) * Decimal::from(100)
    );
    assert_eq!(
        ctx.account.cash_available,
        Decimal::from(1_000_000) - ctx.account.open_exposure_inr
    );
    assert_eq!(ctx.entries_today, 2, "both lines were entries today");
    assert_eq!(
        ctx.pending
            .iter()
            .map(|p| p.symbol.as_str())
            .collect::<Vec<_>>(),
        vec!["BBB"]
    );
    assert_eq!(ctx.tier.level, 0);
    assert!((ctx.tier.max_exposure_pct - 25.0).abs() < f64::EPSILON);
}

#[tokio::test]
async fn test_load_context_with_no_market_row_is_red_and_rung_zero() {
    let (ctx, _dir) = context_of(&Book {
        no_market: true,
        ..Book::default()
    })
    .await;
    assert_eq!(ctx.gate, MarketGate::Red);
    assert!(!ctx.tier.new_entries_allowed);
    assert_eq!(ctx.tier.level, 0);
}

#[tokio::test]
async fn test_the_second_signal_plan_of_a_morning_is_sized_against_the_first_confirm() {
    let dir = tempfile::tempdir().unwrap();
    let conn = Book {
        rung: 0,
        stats: vec![("BBB", 2, "6.00", 200_000_000, "70.00")],
        ..Book::default()
    }
    .open(dir.path())
    .await;
    let mut s = store_over(conn, flags(true, false));
    s.raise_signal(&triggered(name("AAA", 1), "100.80", "97.80"))
        .await
        .unwrap();
    let first = new_lines(&mut s.conn).await;
    assert_eq!(int_of(&first[0], "quantity"), 1666);
    // The person confirms AAA on the desk: a position at cost, a FILLED line.
    let first_id = int_of(&first[0], "id");
    exec(
        &mut s.conn,
        "INSERT INTO public.sw_position VALUES (1, 1, 10, '100.8', 1666, 1666, 'OPEN')",
        &[],
    )
    .await;
    exec(
        &mut s.conn,
        "UPDATE public.sw_plan_line SET state = 'FILLED', position_id = 1 WHERE id = ?",
        &[Param::Int(first_id)],
    )
    .await;
    let mut bbb = name("BBB", 2);
    bbb.pivot_high = Some(d("205"));
    s.raise_signal(&triggered(bbb, "210.00", "204.00"))
        .await
        .unwrap();
    // The re-read is what sized it: a 09:15 reading would have given 833.
    let lines = new_lines(&mut s.conn).await;
    assert_eq!(lines.len(), 2);
    let second = &lines[1];
    assert_eq!(int_of(second, "quantity"), 390);
    assert_eq!(
        txt(second, "position_value"),
        "81900.00",
        "position_value = 390 × 210"
    );
    assert!(skips(&mut s.conn).await.is_empty());
    let book = d("100.80") * Decimal::from(1666) + d(&txt(second, "position_value"));
    assert_eq!(book, d("249832.80"));
    assert!(book <= Decimal::from(250_000));
    let ctx = s.context.clone().unwrap();
    assert_eq!(
        ctx.account.open_symbols.iter().cloned().collect::<Vec<_>>(),
        vec!["AAA".to_owned()]
    );
    assert_eq!(ctx.entries_today, 1);
}

#[tokio::test]
async fn test_a_trigger_that_fits_no_sliver_is_exposure_full_with_the_arithmetic() {
    let dir = tempfile::tempdir().unwrap();
    let conn = Book {
        rung: 0,
        stats: vec![("BBB", 2, "6.00", 200_000_000, "70.00")],
        held: vec![(1, "AAA", 1, "124.0", 2000, 2000)],
        ..Book::default()
    }
    .open(dir.path())
    .await;
    let mut s = store_over(conn, flags(true, false));
    let mut bbb = name("BBB", 2);
    bbb.pivot_high = Some(d("205"));
    s.raise_signal(&triggered(bbb, "210.00", "204.00"))
        .await
        .unwrap();
    assert!(new_lines(&mut s.conn).await.is_empty());
    let sk = skips(&mut s.conn).await;
    assert_eq!(sk.len(), 1);
    assert_eq!(sk[0].0, "EXPOSURE_FULL");
    assert!(
        sk[0].1.contains("leaves ₹2,000.00") && sk[0].1.contains("BELOW_MIN_TRADE_VALUE"),
        "{}",
        sk[0].1
    );
}

#[tokio::test]
async fn test_the_third_entry_of_a_session_is_the_cap_and_the_fourth_is_session_cap() {
    let dir = tempfile::tempdir().unwrap();
    let conn = Book {
        rung: 3,
        stats: vec![("DDD", 4, "5.00", 100_000_000, "60.00")],
        taken: vec![
            (0, "AAA", 1, 10, "100.0", "FILLED", None),
            (1, "BBB", 2, 10, "100.0", "SENT", None),
            (2, "CCC", 3, 10, "100.0", "CONFIRMED", None),
        ],
        ..Book::default()
    }
    .open(dir.path())
    .await;
    let mut s = store_over(conn, flags(true, false));
    s.raise_signal(&triggered(name("DDD", 4), "100.80", "97.80"))
        .await
        .unwrap();
    assert!(
        new_lines(&mut s.conn).await.is_empty(),
        "a fourth entry was lined"
    );
    let reasons: Vec<String> = skips(&mut s.conn).await.into_iter().map(|s| s.0).collect();
    assert_eq!(reasons, vec!["SESSION_CAP".to_owned()]);
    assert!(
        s.pending_confirms.is_empty(),
        "a skip was queued for auto-execute"
    );
}

#[tokio::test]
async fn test_the_store_still_accepts_a_starting_context_but_never_sizes_from_it() {
    let stale = SignalContext {
        gate: MarketGate::Green,
        tier: ExposureTier {
            level: 3,
            max_open_positions: 10,
            max_exposure_pct: 100.0,
            new_entries_allowed: true,
            drawdown_locked: false,
        },
        account: SwingAccount {
            equity: Decimal::from(1_000_000),
            cash_available: Decimal::from(1_000_000),
            open_symbols: std::collections::BTreeSet::new(),
            open_exposure_inr: Decimal::ZERO,
            open_entry_counts: std::collections::BTreeMap::new(),
        },
        detected: std::collections::BTreeMap::from([(
            "AAA".to_owned(),
            (d("5.00"), Some(Decimal::from(100_000_000)), d("72.00")),
        )]),
        entries_today: 0,
        pending: Vec::new(),
        reserved: Vec::new(),
        first_live_sessions_left: 0,
        live: None,
    };
    let dir = tempfile::tempdir().unwrap();
    let conn = Book {
        gate: "RED",
        ..Book::default()
    }
    .open(dir.path())
    .await;
    let mut s = store_over(conn, flags(true, false));
    s.context = Some(stale);
    s.raise_signal(&triggered(name("AAA", 1), "100.80", "97.80"))
        .await
        .unwrap();
    assert!(new_lines(&mut s.conn).await.is_empty());
    let reasons: Vec<String> = skips(&mut s.conn).await.into_iter().map(|s| s.0).collect();
    assert_eq!(reasons, vec!["GATE_RED".to_owned()]);
    assert_eq!(
        s.context.as_ref().unwrap().gate,
        MarketGate::Red,
        "the store's context is the last reading"
    );
}

#[tokio::test]
async fn test_load_config_hands_the_plan_the_persons_sizing_knobs() {
    let dir = tempfile::tempdir().unwrap();
    let mut conn = Book {
        knobs: (4.5, 60_000_000.0, 25.0, 0.25, 15.0, 4),
        ..Book::default()
    }
    .open(dir.path())
    .await;
    let config = load_config(&mut conn, 1, "public").await.unwrap();
    assert!((config.sizing.risk_per_trade_pct - 0.25).abs() < f64::EPSILON);
    assert!((config.sizing.max_position_pct - 15.0).abs() < f64::EPSILON);
    assert_eq!(config.sizing.max_open_positions, 4);
    assert!((config.liquidity.adr_min_pct - 4.5).abs() < f64::EPSILON);
    assert_eq!(
        config.stops,
        SwingConfig::default().stops,
        "only the person's knobs move"
    );
    // No row: the defaults.
    let none = load_config(&mut conn, 2, "public").await.unwrap();
    assert_eq!(none, SwingConfig::default());
}

// =============================================================================================
// TestReservedSlotsAndHalfRisk (SW10.5, A7 and A9)
// =============================================================================================

#[tokio::test]
async fn test_load_context_carries_the_reserved_slots_the_first_live_count_and_the_watch_adr() {
    let (ctx, _dir) = context_of(&Book {
        first_live_left: 3,
        reserved: vec![("GAPCO", 9)],
        watch_stats: vec![("GAPCO", 9, "6.19", "46.20")],
        ..Book::default()
    })
    .await;
    assert_eq!(ctx.reserved, vec!["GAPCO".to_owned()]);
    assert_eq!(ctx.first_live_sessions_left, 3);
    assert_eq!(ctx.detected["GAPCO"], (d("6.19"), None, d("46.20")));
    assert_eq!(
        ctx.detected["AAA"].0,
        d("5.00"),
        "a detection row still wins"
    );
}

fn two_filled() -> (Vec<Taken>, Vec<Held>) {
    (
        vec![
            (1, "AAA", 1, 100, "100.0", "FILLED", Some(1)),
            (2, "BBB", 2, 100, "100.0", "FILLED", Some(2)),
        ],
        vec![
            (1, "AAA", 1, "100.0", 100, 100),
            (2, "BBB", 2, "100.0", 100, 100),
        ],
    )
}

#[tokio::test]
async fn test_a_reserved_slot_counts_against_the_session_cap_for_other_names() {
    let (taken, held) = two_filled();
    let dir = tempfile::tempdir().unwrap();
    let conn = Book {
        rung: 3,
        stats: vec![("CCC", 3, "5.00", 100_000_000, "60.00")],
        taken,
        held,
        reserved: vec![("GAPCO", 9)],
        ..Book::default()
    }
    .open(dir.path())
    .await;
    let mut s = store_over(conn, flags(true, false));
    s.raise_signal(&triggered(name("CCC", 3), "100.80", "97.80"))
        .await
        .unwrap();
    assert!(new_lines(&mut s.conn).await.is_empty());
    let sk = skips(&mut s.conn).await;
    assert_eq!(sk.len(), 1);
    assert_eq!(sk[0].0, "SESSION_CAP");
}

#[tokio::test]
async fn test_the_reserved_name_itself_is_not_counted_against_its_own_slot() {
    let (taken, held) = two_filled();
    let dir = tempfile::tempdir().unwrap();
    let conn = Book {
        rung: 3,
        stats: vec![("GAPCO", 9, "6.19", 100_000_000, "46.20")],
        taken,
        held,
        reserved: vec![("GAPCO", 9)],
        ..Book::default()
    }
    .open(dir.path())
    .await;
    let mut s = store_over(conn, flags(true, false));
    s.raise_signal(&triggered(
        named("GAPCO", 9, Setup::Ep, None),
        "112.50",
        "107.50",
    ))
    .await
    .unwrap();
    let lines = new_lines(&mut s.conn).await;
    assert_eq!(lines.len(), 1);
    assert_eq!(txt(&lines[0], "kind"), "BUY_ON_TRIGGER");
    assert!(int_of(&lines[0], "quantity") > 0);
    // Released exactly once, before the plan was sized: the slot is SKIPPED with its note, and
    // a second release finds nothing.
    let pending = rows(
        &mut s.conn,
        "SELECT state, note FROM public.sw_plan_line WHERE kind = 'PENDING_RANGE'",
    )
    .await;
    assert_eq!(txt(&pending[0], "state"), "SKIPPED");
    assert_eq!(
        txt(&pending[0], "note"),
        "held for the range; slot released at 09:31 — the range broke, see the SIGNAL plan"
    );
    let raised = ist()
        .from_local_datetime(&day().and_hms_opt(9, 40, 0).unwrap())
        .single()
        .unwrap();
    assert_eq!(s.release_reservation(90, &raised).await.unwrap(), 0);
    let code = super::tests::code_only(include_str!("runner.rs"));
    let release = &code[code.find("pub async fn release_reservation(").unwrap()..];
    let release = &release[..release.find("RETURNING id").unwrap()];
    for part in [
        "user_id = ?",
        "kind = 'PENDING_RANGE'",
        "state = 'PROPOSED'",
    ] {
        assert!(release.contains(part), "{part}");
    }
}

#[tokio::test]
async fn test_a_wide_stop_on_a_live_gap_is_stop_too_wide_and_the_slot_is_released() {
    let dir = tempfile::tempdir().unwrap();
    let conn = Book {
        rung: 3,
        watch_stats: vec![("GAPCO", 9, "5.00", "46.20")],
        reserved: vec![("GAPCO", 9)],
        ..Book::default()
    }
    .open(dir.path())
    .await;
    let mut s = store_over(conn, flags(true, false));
    s.raise_signal(&triggered(
        named("GAPCO", 9, Setup::Ep, None),
        "112.50",
        "105.00",
    ))
    .await
    .unwrap();
    assert!(new_lines(&mut s.conn).await.is_empty());
    let sk = skips(&mut s.conn).await;
    assert_eq!(
        sk,
        vec![("SIZE_REFUSED".to_owned(), "STOP_TOO_WIDE".to_owned())]
    );
    let raised = ist()
        .from_local_datetime(&day().and_hms_opt(9, 40, 0).unwrap())
        .single()
        .unwrap();
    assert_eq!(s.release_reservation(90, &raised).await.unwrap(), 0);
}

#[tokio::test]
async fn test_a_live_gap_with_no_detection_row_is_sized_off_the_watch_rows_adr() {
    let dir = tempfile::tempdir().unwrap();
    let conn = Book {
        rung: 3,
        watch_stats: vec![("GAPCO", 9, "6.19", "46.20")],
        ..Book::default()
    }
    .open(dir.path())
    .await;
    let mut s = store_over(conn, flags(true, false));
    s.raise_signal(&triggered(
        named("GAPCO", 9, Setup::Ep, None),
        "112.50",
        "107.50",
    ))
    .await
    .unwrap();
    let lines = new_lines(&mut s.conn).await;
    assert_eq!(lines.len(), 1);
    assert_eq!(txt(&lines[0], "kind"), "BUY_ON_TRIGGER");
    assert_eq!(int_of(&lines[0], "quantity"), 1000);
}

#[tokio::test]
async fn test_the_signal_plan_is_half_risk_only_while_counting_down_and_live() {
    let mut sizes = Vec::new();
    for (left, live) in [(5, true), (5, false), (0, true)] {
        let dir = tempfile::tempdir().unwrap();
        let conn = Book {
            rung: 3,
            first_live_left: left,
            ..Book::default()
        }
        .open(dir.path())
        .await;
        let mut s = store_over(conn, flags(!live, live));
        s.raise_signal(&triggered(name("AAA", 1), "100.80", "97.80"))
            .await
            .unwrap();
        let lines = new_lines(&mut s.conn).await;
        assert_eq!(lines.len(), 1);
        sizes.push(int_of(&lines[0], "quantity"));
    }
    assert_eq!(sizes, vec![833, 1666, 1666]);
}

#[tokio::test]
async fn test_the_context_counts_a_partial_fills_open_remainder_as_exposure() {
    let (ctx, _dir) = context_of(&Book {
        rung: 3,
        held: vec![(1, "AAA", 1, "100.0", 40, 40)],
        taken: vec![(1, "AAA", 1, 100, "101.0", "SENT", Some(1))],
        ..Book::default()
    })
    .await;
    assert_eq!(ctx.account.open_exposure_inr, d("4000") + d("6060"));
    assert_eq!(ctx.entries_today, 1);
    assert!(ctx.pending.is_empty());
}

// =============================================================================================
// TestTelemetryNeverStopsAVerdict: the store half
// =============================================================================================

#[tokio::test]
async fn test_telemetry_never_raises_into_the_signal_store() {
    // The desk's telemetry is the "library absent" branch (R8.23): a count cannot fail. The row
    // and the line are written whatever the sink does, because the write is not behind it.
    let dir = tempfile::tempdir().unwrap();
    let conn = Book::default().open(dir.path()).await;
    let mut s = store_over(conn, flags(true, false));
    s.raise_signal(&triggered(name("AAA", 1), "103", "100"))
        .await
        .unwrap();
    assert_eq!(
        rows(&mut s.conn, "SELECT * FROM public.sw_signal")
            .await
            .len(),
        1
    );
    assert_eq!(new_lines(&mut s.conn).await.len(), 1);
}

// =============================================================================================
// TestTheGateIsReReadLive (SW30, SW31)
// =============================================================================================

async fn live_ctx(
    book: &Book,
    level: Option<f64>,
    now: DateTime<FixedOffset>,
) -> (SignalContext, usize) {
    let dir = tempfile::tempdir().unwrap();
    let mut conn = book.open(dir.path()).await;
    let live = Counting::new(level);
    let ctx = load_context(
        &mut conn,
        1,
        day(),
        "public",
        Some(live.as_ref()),
        Some(now),
        "nifty-smallcap-250",
    )
    .await
    .unwrap();
    let asked = *live.asked.lock().unwrap();
    (ctx, asked)
}

fn red0() -> Book {
    Book {
        gate: "RED",
        rung: 0,
        ..Book::default()
    }
}

#[tokio::test]
async fn test_a_cross_up_opens_a_red_close_at_the_bottom_rung() {
    let (ctx, _) = live_ctx(&red0(), Some(101.0), in_session()).await;
    assert_eq!(ctx.gate, MarketGate::Green);
    assert!(ctx.tier.new_entries_allowed && ctx.tier.level == 0);
    assert_eq!(ctx.tier.max_open_positions, 2);
    assert!((ctx.tier.max_exposure_pct - 25.0).abs() < f64::EPSILON);
    let live = ctx.live.unwrap();
    assert_eq!(live.index_slug, "nifty-smallcap-250");
    assert!(
        (live.reading.ma_fast - (100.0 + 2.0 / 11.0)).abs() < 1e-9,
        "EMA(10), SW31"
    );
    assert!(
        (live.reading.ma_slow - (100.0 + 2.0 / 21.0)).abs() < 1e-9,
        "EMA(20)"
    );
}

#[tokio::test]
async fn test_a_cross_down_closes_a_green_close() {
    let book = Book {
        gate: "GREEN",
        rung: 2,
        ..Book::default()
    };
    let (ctx, _) = live_ctx(&book, Some(99.0), in_session()).await;
    assert_eq!(ctx.gate, MarketGate::Red);
    assert!(!ctx.tier.new_entries_allowed);
}

#[tokio::test]
async fn test_level_averages_are_amber_not_green() {
    let (ctx, _) = live_ctx(&red0(), Some(100.0), in_session()).await;
    assert_eq!(ctx.gate, MarketGate::Amber);
    assert!(ctx.tier.new_entries_allowed);
}

#[tokio::test]
async fn test_the_green_close_keeps_its_rung_while_the_live_tape_agrees() {
    let book = Book {
        gate: "GREEN",
        rung: 2,
        ..Book::default()
    };
    let (ctx, _) = live_ctx(&book, Some(101.0), in_session()).await;
    assert_eq!(ctx.gate, MarketGate::Green);
    assert_eq!(ctx.tier.level, 2);
}

#[tokio::test]
async fn test_thin_breadth_at_the_close_is_still_red_whatever_the_index_does() {
    let book = Book {
        pct_up_strong_1m: 1.5,
        ..red0()
    };
    let (ctx, _) = live_ctx(&book, Some(101.0), in_session()).await;
    assert_eq!(ctx.gate, MarketGate::Red);
}

#[tokio::test]
async fn test_a_drawdown_lock_holds() {
    let book = Book {
        drawdown_locked: true,
        ..red0()
    };
    let (ctx, _) = live_ctx(&book, Some(101.0), in_session()).await;
    assert!(!ctx.tier.new_entries_allowed);
}

#[tokio::test]
async fn test_outside_the_session_the_close_stands_and_kite_is_not_asked() {
    for (h, m) in [(8, 59), (15, 31)] {
        let now = ist()
            .from_local_datetime(&day().and_hms_opt(h, m, 0).unwrap())
            .single()
            .unwrap();
        // No index tables: a read of them would fail the call (Python's `index_reads == 0`).
        let book = Book {
            index_levels: None,
            ..red0()
        };
        let (ctx, asked) = live_ctx(&book, Some(101.0), now).await;
        assert_eq!(ctx.gate, MarketGate::Red);
        assert!(ctx.live.is_none());
        assert_eq!(asked, 0, "Kite was asked outside the session");
    }
}

#[tokio::test]
async fn test_no_live_level_keeps_the_close() {
    let (ctx, asked) = live_ctx(&red0(), None, in_session()).await;
    assert_eq!(asked, 1);
    assert_eq!(ctx.gate, MarketGate::Red);
    assert!(ctx.live.is_none());
}

#[tokio::test]
async fn test_too_few_published_closes_keep_the_close() {
    let book = Book {
        index_levels: Some(vec![100.0; 10]),
        ..red0()
    };
    let (ctx, _) = live_ctx(&book, Some(101.0), in_session()).await;
    assert_eq!(ctx.gate, MarketGate::Red);
    assert!(ctx.live.is_none());
}

#[tokio::test]
async fn test_without_a_live_level_nothing_changes_from_before_sw30() {
    let (ctx, _dir) = context_of(&Book {
        index_levels: None,
        ..red0()
    })
    .await;
    assert_eq!(ctx.gate, MarketGate::Red);
    assert!(ctx.live.is_none());
}

#[tokio::test]
async fn test_the_index_read_asks_for_the_gate_slug_before_today() {
    // 249 closes at 100 before today for the gate's slug, then a 250th, far older, at 10⁶, and
    // another index entirely at 10⁶: the EMAs come out at 100 + 2/11 and 100 + 2/21 only if
    // exactly the gate slug's latest 249 closes before today were read (the EMAs' warm-up).
    let mut levels = vec![1.0e6];
    levels.extend([100.0; 249]);
    let book = Book {
        index_levels: Some(levels),
        ..red0()
    };
    let (ctx, _) = live_ctx(&book, Some(101.0), in_session()).await;
    let live = ctx.live.unwrap();
    assert!((live.reading.ma_fast - (100.0 + 2.0 / 11.0)).abs() < 1e-9);
    let code = super::tests::code_only(include_str!("../swing_exec/live_gate.rs"));
    assert!(
        code.contains("s.date < ?"),
        "the read is strictly before today"
    );
}

#[tokio::test]
async fn test_a_trigger_after_the_cross_is_lined_where_before_it_was_refused() {
    let dir = tempfile::tempdir().unwrap();
    let conn = red0().open(dir.path()).await;
    let level = Counting::new(Some(99.0));
    let mut s = store_over(conn, flags(true, false));
    s.live_level = Some(level.clone());
    s.now = Arc::new(in_session);
    assert!(!s.read_context().await.unwrap().tier.new_entries_allowed);
    *level.level.lock().unwrap() = Some(101.0);
    let ctx = s.read_context().await.unwrap();
    assert_eq!(ctx.gate, MarketGate::Green);
    assert!(ctx.tier.new_entries_allowed);
}

#[tokio::test]
async fn test_the_heartbeat_names_the_gate_in_force_and_rereads_it_once_a_minute() {
    let dir = tempfile::tempdir().unwrap();
    let conn = red0().open(dir.path()).await;
    let reads = Counting::new(None);
    let mut s = store_over(conn, flags(true, false));
    // Each context read asks the level once while in session (Python counts the market reads).
    s.live_level = Some(reads.clone());
    s.now = Arc::new(in_session);
    s.read_context().await.unwrap();
    let before = *reads.asked.lock().unwrap();
    let mut beat = GatedHeartbeat::new(60.0);
    let mut lines = Vec::new();
    for t in [0.0, 30.0, 61.0] {
        lines.push(beat.line(&mut s, "watching 3", t).await.unwrap());
    }
    assert_eq!(*reads.asked.lock().unwrap(), before + 2);
    assert_eq!(lines[0], "watching 3 · gate RED (last close)");
}

// =============================================================================================
// TestTheKiteLevel
// =============================================================================================

fn replay_with_quotes(
    quotes: serde_json::Value,
) -> (Arc<crate::fake_kite::ReplayKite>, tempfile::TempDir) {
    let dir = tempfile::tempdir().unwrap();
    let fixture = serde_json::json!({"session": {"authed": true}, "routes": {}, "quotes": quotes});
    std::fs::write(dir.path().join("kite.json"), fixture.to_string()).unwrap();
    let kite = crate::fake_kite::ReplayKite::load(dir.path(), None, "test-key").unwrap();
    (Arc::new(kite), dir)
}

#[tokio::test]
async fn test_the_quote_is_the_gate_index_kite_symbol() {
    let (kite, _dir) = replay_with_quotes(serde_json::json!({
        "NSE:NIFTY SMLCAP 250": {"instrument_token": 1, "last_price": 17747.25}
    }));
    let level = crate::lanes::swing::live_gate::KiteLevel::new(kite.clone(), "nifty-smallcap-250");
    assert_eq!(level.level().await, Some(17747.25));
    let calls = kite.calls();
    assert_eq!(calls.len(), 1);
    assert!(
        format!("{:?}", calls[0].params).contains("NIFTY SMLCAP 250"),
        "{:?}",
        calls[0]
    );
}

#[tokio::test]
async fn test_an_unmapped_index_or_a_failing_quote_is_no_level() {
    let (down, _dir) = replay_with_quotes(serde_json::json!({}));
    let failing =
        crate::lanes::swing::live_gate::KiteLevel::new(down.clone(), "nifty-smallcap-250");
    assert_eq!(failing.level().await, None);
    let unmapped = crate::lanes::swing::live_gate::KiteLevel::new(down.clone(), "nifty-unknown");
    assert_eq!(unmapped.level().await, None);
    assert_eq!(down.calls().len(), 1, "an unmapped index asks nothing");
}
