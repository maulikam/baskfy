//! `tests/test_swing_monitor.py`'s store tests that guard non-negotiables at the monitor: the
//! per-session entry cap (`SESSION_CAP` on the fourth entry of a session) and A9's first-live
//! half risk (half only while counting down AND a real order would go out). Python answers
//! `load_context`'s reads from a `FakeConn`; here the same rows sit in a real SQLite database
//! attached as `public`, so the store's own SQL runs unchanged.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::Arc;

use baskfy_core::swing::opening_range::{TriggerState, TriggerVerdict};
use baskfy_core::swing::setups::{Setup, SwingConfig};
use chrono::{NaiveDate, TimeZone};
use rust_decimal::Decimal;

use super::runner::PgSignalStore;
use crate::clock::ist;
use crate::ids::IdSource;
use crate::lanes::swing::breakout::{Signal, SignalStore, WatchedName};
use crate::settings::Settings;
use crate::storage::{Conn, Db, DbValue, Param};

const SCHEMA_SQL: &[&str] = &[
    "CREATE TABLE public.instrument (id INTEGER PRIMARY KEY, symbol TEXT)",
    "CREATE TABLE public.sw_market_daily (user_id INTEGER, date TEXT, gate TEXT, \
     exposure_level INTEGER, max_open_positions INTEGER, max_exposure_pct REAL, \
     new_entries_allowed BOOLEAN, drawdown_locked BOOLEAN, constituent_count INTEGER, \
     pct_up_strong_1m REAL, pct_new_52w_high REAL, pct_above_ma_slow REAL)",
    "CREATE TABLE public.sw_config (user_id INTEGER, sleeve_capital_inr TEXT, \
     first_live_sessions_left INTEGER)",
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
     instrument_id INTEGER, adr_pct TEXT, score TEXT, state TEXT)",
    "CREATE TABLE public.sw_signal (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, \
     watch_id INTEGER, instrument_id INTEGER, setup TEXT, session_date TEXT, raised_at TEXT, \
     state TEXT, or_window_minutes INTEGER, range_high TEXT, range_low TEXT, \
     low_of_day TEXT, last_price TEXT, entry TEXT, stop TEXT, plan_line_id INTEGER)",
];

struct FixedIds;

impl IdSource for FixedIds {
    fn uuid4_bytes(&self) -> [u8; 16] {
        [0x42; 16]
    }
}

fn day() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 8, 19).unwrap()
}

fn name(symbol: &str, token: i64) -> WatchedName {
    WatchedName {
        watch_id: token,
        instrument_id: token * 10,
        symbol: symbol.to_owned(),
        token,
        setup: Setup::Flag,
        pivot_high: Some(Decimal::from(100)),
        upper_circuit: None,
        focus: false,
    }
}

fn signal(watch: WatchedName, entry: &str, stop: &str) -> Signal {
    let entry: Decimal = entry.parse().unwrap();
    Signal {
        watch,
        at: day().and_hms_opt(9, 31, 0).unwrap(),
        verdict: TriggerVerdict {
            state: TriggerState::Triggered,
            entry: Some(entry),
            stop: Some(stop.parse().unwrap()),
            range_high: Some(Decimal::new(995, 1)),
            range_low: Some(Decimal::from(98)),
        },
        last_price: entry,
        low_of_day: Decimal::new(978, 1),
        window_minutes: 5,
    }
}

/// `FakeConn(rung=3, capital="1000000", first_live_left=…, stats=…)` as rows.
async fn book(dir: &std::path::Path, first_live_left: i64, extra: &[(&str, i64)]) -> Conn {
    let mut conn = Db::connect_sqlite_path(&dir.join("main.db")).await.unwrap();
    let public = dir.join("public.db").to_string_lossy().into_owned();
    conn.execute("ATTACH DATABASE ? AS public", &[Param::Text(public)])
        .await
        .unwrap();
    for sql in SCHEMA_SQL {
        conn.execute(sql, &[]).await.unwrap();
    }
    let run = |sql: &'static str| sql;
    conn.execute(
        run(
            "INSERT INTO public.sw_market_daily VALUES (1, '2026-08-18', 'GREEN', 3, 10, 100.0, \
             1, 0, 500, 10.09, 3.0, 40.0)",
        ),
        &[],
    )
    .await
    .unwrap();
    conn.execute(
        "INSERT INTO public.sw_config VALUES (1, '1000000', ?)",
        &[Param::Int(first_live_left)],
    )
    .await
    .unwrap();
    let mut stats = vec![("AAA", 1_i64, "72.00")];
    stats.extend(extra.iter().map(|(s, t)| (*s, *t, "60.00")));
    for (symbol, token, score) in stats {
        conn.execute(
            "INSERT INTO public.instrument VALUES (?, ?)",
            &[Param::Int(token * 10), Param::Text(symbol.to_owned())],
        )
        .await
        .unwrap();
        conn.execute(
            "INSERT INTO public.sw_setup_daily VALUES (1, ?, '2026-08-18', '5.00', 100000000, ?)",
            &[Param::Int(token * 10), Param::Text(score.to_owned())],
        )
        .await
        .unwrap();
    }
    conn
}

fn store(conn: Conn, settings: Settings) -> PgSignalStore {
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

fn flags(dry_run: bool, execution: bool) -> Settings {
    let mut s = Settings::defaults().unwrap();
    s.dry_run = dry_run;
    s.swing_execution_enabled = execution;
    s
}

fn ints(rows: &[crate::storage::Row], key: &str) -> Vec<i64> {
    rows.iter()
        .map(|r| match r.get(key) {
            Some(DbValue::Int(i)) => *i,
            other => panic!("{key}: {other:?}"),
        })
        .collect()
}

#[tokio::test]
async fn the_third_entry_of_a_session_is_the_cap_and_the_fourth_is_session_cap() {
    let dir = tempfile::tempdir().unwrap();
    let mut conn = book(dir.path(), 0, &[("BBB", 2), ("CCC", 3), ("DDD", 4)]).await;
    conn.execute(
        "INSERT INTO public.sw_plan (id, plan_id, user_id, as_of, source) \
         VALUES (1, 'evening', 1, '2026-08-19', 'MORNING')",
        &[],
    )
    .await
    .unwrap();
    for (i, (token, state)) in [(1, "FILLED"), (2, "SENT"), (3, "CONFIRMED")]
        .into_iter()
        .enumerate()
    {
        conn.execute(
            "INSERT INTO public.sw_plan_line (id, plan_id, user_id, kind, instrument_id, \
             quantity, trigger, state) VALUES (?, 1, 1, 'BUY_ON_TRIGGER', ?, 10, '100.0', ?)",
            &[
                Param::Int(i64::try_from(i).unwrap() + 1),
                Param::Int(token * 10),
                Param::Text(state.to_owned()),
            ],
        )
        .await
        .unwrap();
    }
    let mut s = store(conn, flags(true, false));
    s.raise_signal(&signal(name("DDD", 4), "100.80", "97.80"))
        .await
        .unwrap();
    let lines = s
        .conn
        .execute("SELECT id FROM public.sw_plan_line WHERE id > 3", &[])
        .await
        .unwrap()
        .rows;
    assert!(lines.is_empty(), "a fourth entry was lined");
    let skips = s
        .conn
        .execute("SELECT reason FROM public.sw_plan_skip", &[])
        .await
        .unwrap()
        .rows;
    let reasons: Vec<String> = skips
        .iter()
        .map(|r| match r.get("reason") {
            Some(DbValue::Text(t)) => t.clone(),
            other => panic!("{other:?}"),
        })
        .collect();
    assert_eq!(reasons, vec!["SESSION_CAP".to_owned()]);
    assert!(
        s.pending_confirms.is_empty(),
        "a skip was queued for auto-execute"
    );
}

#[tokio::test]
async fn the_signal_plan_is_half_risk_only_while_counting_down_and_live() {
    let mut sizes = Vec::new();
    for (left, live) in [(5, true), (5, false), (0, true)] {
        let dir = tempfile::tempdir().unwrap();
        let conn = book(dir.path(), left, &[]).await;
        let mut s = store(conn, flags(!live, live));
        s.raise_signal(&signal(name("AAA", 1), "100.80", "97.80"))
            .await
            .unwrap();
        let rows = s
            .conn
            .execute("SELECT quantity FROM public.sw_plan_line", &[])
            .await
            .unwrap()
            .rows;
        let q = ints(&rows, "quantity");
        assert_eq!(q.len(), 1);
        sizes.push(q[0]);
        assert_eq!(s.pending_confirms.len(), 1, "a lined trigger is queued");
    }
    assert_eq!(sizes, vec![833, 1666, 1666]);
}
