//! The service (`swing_monitor.main()` as `baskfy-desk service swing-monitor`) and the
//! container's schedule (`tests/test_swing_monitor_schedule.py`, whole):
//!
//! * `main` exits 0 with the flag off before touching anything, 2 without a user, raises
//!   Python's errors for a bad user id, no Kite key or a bad metrics port, and with no session by
//!   15:20 runs only the backstop clock;
//! * SW25 through the **real** confirm ([`DeskAutoConfirm`]): with any flag off nothing is ever
//!   confirmed (the line stays PROPOSED, no order or GTT reaches the broker); an execution that
//!   raises is ERROR and the next line is still tried; the line the store queued is exactly the
//!   line `execute_line` confirms (DRY_RUN: SIMULATED with a DRY GTT);
//! * the loop's desk hooks write `monitor_ran` and the gated heartbeat, and reload with the
//!   morning's circuit bands; the ticker's bus is read as Python's queues;
//! * the harness route is inert without `--harness`;
//! * `next_run`, executed against the cases Python executes the Dockerfile's heredoc against,
//!   and the heredoc itself still read `monitor_close` with its 15:30 fallback.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use baskfy_core::swing::opening_range::TriggerState;
use baskfy_core::swing::setups::SwingConfig;
use chrono::{DateTime, FixedOffset, NaiveTime, TimeZone};
use rust_decimal::Decimal;
use tower::ServiceExt as _;

use super::runner::{
    AutoConfirm, MonitorStore, PgSignalStore, TickBus as _, WatchHooks, drain_auto_execute,
};
use super::service::{
    BusQueues, DeskAutoConfirm, DeskHooks, KITE_KEY_MISSING, MonitorOrders, main_with, next_run,
    order_source,
};
use super::tests_clock::{Desk, day, ist_on};
use crate::clock::ist;
use crate::lanes::swing::breakout::SignalStore as _;
use crate::lanes::swing::execute::OrderSource as _;
use crate::storage::{Conn, Db, DbValue, Param};
use crate::wire::{PyDict, PyValue};

fn env_of(
    pairs: &'static [(&'static str, &'static str)],
) -> impl Fn(&str) -> Option<String> + Send + Sync {
    move |k: &str| {
        pairs
            .iter()
            .find(|(key, _)| *key == k)
            .map(|(_, v)| (*v).to_owned())
    }
}

// =============================================================================================
// main()
// =============================================================================================

#[tokio::test]
async fn main_exits_0_with_the_flag_off_before_touching_anything() {
    let desk = Desk::new(ist_on(9, 14, 0), |_| {}).await;
    assert_eq!(
        main_with(&desk.state, &env_of(&[("BASKFY_SOLE_USER_ID", "1")])).await,
        Ok(0)
    );
    assert!(
        desk.kite.calls().is_empty(),
        "the broker was reached with the flag off"
    );
}

#[tokio::test]
async fn main_without_a_user_is_exit_2_and_with_a_bad_one_is_pythons_value_error() {
    let desk = Desk::new(ist_on(9, 14, 0), |s| s.swing_monitor_enabled = true).await;
    assert_eq!(main_with(&desk.state, &env_of(&[])).await, Ok(2));
    assert_eq!(
        main_with(&desk.state, &env_of(&[("BASKFY_SOLE_USER_ID", "")])).await,
        Ok(2)
    );
    let err = main_with(&desk.state, &env_of(&[("BASKFY_SOLE_USER_ID", "one")]))
        .await
        .unwrap_err();
    assert_eq!(
        err,
        "ValueError: invalid literal for int() with base 10: 'one'"
    );
    assert!(desk.kite.calls().is_empty());
}

#[tokio::test]
async fn main_without_a_kite_key_refuses_as_kite_does() {
    let desk = Desk::new(ist_on(9, 14, 0), |s| {
        s.swing_monitor_enabled = true;
        s.kite_api_key = String::new();
    })
    .await;
    let err = main_with(&desk.state, &env_of(&[("BASKFY_SOLE_USER_ID", "1")]))
        .await
        .unwrap_err();
    assert_eq!(err, KITE_KEY_MISSING);
}

#[tokio::test]
async fn main_with_a_metrics_port_python_cannot_read_dies_before_the_watch() {
    let desk = Desk::new(ist_on(9, 14, 0), |s| s.swing_monitor_enabled = true).await;
    let err = main_with(
        &desk.state,
        &env_of(&[
            ("BASKFY_SOLE_USER_ID", "1"),
            ("DESK_MONITOR_METRICS_PORT", "ninety"),
        ]),
    )
    .await
    .unwrap_err();
    assert!(err.starts_with("ValueError"), "{err}");
    assert!(desk.kite.calls().is_empty());
}

#[tokio::test]
async fn a_login_that_never_comes_runs_only_the_backstop_and_exits_0() {
    // 15:25: past the late-login cut-off, so one session check and no wait.
    let desk = Desk::over(ist_on(15, 25, 0), serde_json::json!({}), false, |s| {
        s.swing_monitor_enabled = true
    })
    .await
    .with_tables()
    .await;
    let code = main_with(&desk.state, &env_of(&[("BASKFY_SOLE_USER_ID", "1")])).await;
    assert_eq!(code, Ok(0));
    let routes: Vec<String> = desk.kite.calls().iter().map(|c| c.route.clone()).collect();
    assert_eq!(
        routes,
        vec!["user.profile".to_owned()],
        "the watch ran without a session"
    );
    // The backstop ran both chores (both find nothing to do).
    let notes = desk.notes().await;
    assert_eq!(
        notes,
        vec![
            "cutoff 15:25: 0 reconciled, 0 cancelled, 0 refused, 0 slots freed",
            "gtt-sweep 15:25: 0 naked, 0 armed, 0 still naked",
        ]
    );
    desk.no_writes();
}

#[test]
fn the_service_is_wired_and_its_flag_defaults_false() {
    let swing = super::tests::code_only(include_str!("../../swing.rs"));
    assert!(
        swing.contains(
            "\"swing-monitor\" => Some(Box::pin(swing_monitor::service::run(state)).await)"
        )
    );
    let s = crate::settings::Settings::defaults().unwrap();
    assert!(!s.swing_monitor_enabled && !s.swing_auto_execute && !s.swing_execution_enabled);
    assert!(s.dry_run);
    // `run` keeps Python's exit code 2.
    let service = super::tests::code_only(include_str!("service.rs"));
    let run = &service[service.find("pub async fn run(").unwrap()..];
    assert!(run.contains("std::process::exit(code)"));
}

// =============================================================================================
// SW25 through the real confirm
// =============================================================================================

/// The monitor's connection: the desk's own file, attached again as `public` so the store's
/// `public.sw_*` SQL lands in the tables `execute_line` reads.
async fn monitor_conn(desk: &Desk) -> Conn {
    let mut conn = Db::connect_sqlite_path(&desk.db_path).await.unwrap();
    conn.execute(
        "ATTACH DATABASE ? AS public",
        &[Param::Text(desk.db_path.display().to_string())],
    )
    .await
    .unwrap();
    conn
}

/// A GREEN rung-3 book with AAA watched (FLAG, pivot 100, focus) and its detection row.
async fn seed(desk: &Desk) {
    for sql in [
        "INSERT INTO instrument (id, symbol, kite_token) VALUES (10, 'AAA', 1)",
        "INSERT INTO sw_config (user_id, sleeve_capital_inr, first_live_sessions_left) \
         VALUES (1, 1000000, 0)",
        "INSERT INTO sw_market_daily (user_id, date, constituent_count, pct_up_strong_1m, \
         pct_new_52w_high, pct_above_ma_slow, gate, exposure_level, max_open_positions, \
         max_exposure_pct, new_entries_allowed) VALUES (1, '2026-09-01', 500, 10.09, 3.0, 40.0, \
         'GREEN', 3, 10, 100.0, 1)",
        "INSERT INTO sw_setup_daily (user_id, date, instrument_id, setup, status, score, \
         adr_pct, turnover_avg) VALUES (1, '2026-09-01', 10, 'FLAG', 'SETTING_UP', 72.00, 5.00, \
         100000000)",
        "INSERT INTO sw_watch (user_id, instrument_id, setup, source, added_on, trigger, state, \
         focus) VALUES (1, 10, 'FLAG', 'EOD', '2026-09-01', 100, 'WATCHING', 1)",
    ] {
        desk.exec(sql, &[]).await;
    }
}

fn store_for(desk: &Desk, conn: Conn) -> PgSignalStore {
    let clock = desk.clock.clone();
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
        settings: desk.state.settings_snapshot(),
        ids: desk.state.ids.clone(),
        now: Arc::new(move || {
            use crate::clock::Clock as _;
            clock.now_ist()
        }),
    }
}

fn aaa_trigger(at: (u32, u32)) -> crate::lanes::swing::breakout::Signal {
    let mut s =
        super::tests_store::triggered(super::tests_store::name("AAA", 1), "100.80", "97.80");
    s.at = day().and_hms_opt(at.0, at.1, 0).unwrap();
    s
}

async fn line_state(desk: &Desk, id: i64) -> String {
    match desk
        .rows(&format!("SELECT state FROM sw_plan_line WHERE id = {id}"))
        .await
        .remove(0)
        .get("state")
    {
        Some(DbValue::Text(t)) => t.clone(),
        other => panic!("{other:?}"),
    }
}

#[tokio::test]
async fn with_any_flag_off_the_real_confirm_is_never_reached_and_the_line_stays_proposed() {
    let desk = Desk::new(ist_on(9, 40, 0), |_| {})
        .await
        .with_tables()
        .await;
    seed(&desk).await;
    let mut store = store_for(&desk, monitor_conn(&desk).await);
    store.raise_signal(&aaa_trigger((9, 38))).await.unwrap();
    let line = store.lines_written[0];
    let confirm = DeskAutoConfirm::new(desk.state.clone());
    for (dry_run, execution, auto) in [
        (true, true, true),
        (false, false, true),
        (false, true, false),
        (true, false, false),
    ] {
        let settings = super::tests::with_flags(dry_run, execution, auto);
        let mut queue = store.pending_confirms.clone();
        assert_eq!(queue.len(), 1);
        let done = drain_auto_execute(&mut queue, &settings, &confirm).await;
        assert!(done.is_empty() && queue.is_empty());
        assert_eq!(line_state(&desk, line).await, "PROPOSED");
    }
    desk.no_writes();
}

#[tokio::test]
async fn test_an_execution_that_raises_does_not_stop_the_monitor_through_the_real_confirm() {
    // No swing tables: the confirm fails at its first read (before any gateway call), as
    // Python's `open_store` raising does; both lines are ERROR and the second is still tried.
    let desk = Desk::new(ist_on(9, 40, 0), |_| {}).await;
    let confirm = DeskAutoConfirm::new(desk.state.clone());
    let mut queue = vec![
        (aaa_trigger((9, 31)), planned_for("plan-1", 1)),
        (aaa_trigger((9, 32)), planned_for("plan-2", 2)),
    ];
    let done = drain_auto_execute(
        &mut queue,
        &super::tests::with_flags(false, true, true),
        &confirm,
    )
    .await;
    assert_eq!(
        done,
        vec![
            ("AAA".to_owned(), "ERROR".to_owned()),
            ("AAA".to_owned(), "ERROR".to_owned())
        ]
    );
    assert!(queue.is_empty());
    desk.no_writes();
}

fn planned_for(plan_id: &str, line_id: i64) -> super::runner::Planned {
    use baskfy_core::swing::market::{ExposureTier, MarketGate};
    super::runner::Planned {
        line_id: Some(line_id),
        plan: crate::lanes::swing::rules::assemble(
            day(),
            MarketGate::Green,
            ExposureTier {
                level: 3,
                max_open_positions: 10,
                max_exposure_pct: 100.0,
                new_entries_allowed: true,
                drawdown_locked: false,
            },
            vec![],
            vec![],
            vec![],
        ),
        expires_at: ist_on(10, 1, 0),
        plan_id: Some(plan_id.to_owned()),
    }
}

#[tokio::test]
async fn the_queued_line_is_exactly_the_line_execute_line_confirms() {
    // DRY_RUN (the default): the confirm `drain_auto_execute` would make, made directly — the
    // plan the store just wrote, `confirm="true"`, the tick's price — goes through the swing
    // gateway and comes back SIMULATED with its DRY GTT, and nothing reaches the broker.
    let desk = Desk::new(ist_on(9, 40, 0), |_| {})
        .await
        .with_tables()
        .await;
    seed(&desk).await;
    let mut store = store_for(&desk, monitor_conn(&desk).await);
    store.raise_signal(&aaa_trigger((9, 38))).await.unwrap();
    let (signal, planned) = store.take_pending().remove(0);
    let line = planned.line_id.unwrap();
    let confirm = DeskAutoConfirm::new(desk.state.clone());
    let outcome = confirm
        .confirm(planned.plan_id.as_deref().unwrap(), line, signal.last_price)
        .await
        .unwrap();
    assert_eq!(outcome.status, "SIMULATED", "{outcome:?}");
    assert_eq!(line_state(&desk, line).await, "FILLED");
    let pos = desk
        .rows("SELECT gtt_id, quantity_open FROM sw_position")
        .await
        .remove(0);
    assert!(matches!(pos.get("gtt_id"), Some(DbValue::Text(g)) if g.starts_with("DRY-")));
    // A second confirm of the same line is the 409 a click gets.
    let again = confirm
        .confirm(planned.plan_id.as_deref().unwrap(), line, signal.last_price)
        .await
        .unwrap_err();
    assert!(again.starts_with("HTTPException: 409"), "{again}");
    desk.no_writes();
}

#[tokio::test]
async fn the_order_source_reads_the_last_history_row_and_refuses_without_a_key() {
    let desk = Desk::over(
        ist_on(9, 40, 0),
        serde_json::json!({"order.info": {"by_url_arg": "order_id", "answers": {
            "X1": [{"status": "OPEN"},
                   {"status": "COMPLETE", "filled_quantity": 10, "average_price": 101.5}]
        }}}),
        true,
        |_| {},
    )
    .await;
    let orders: MonitorOrders = order_source(&desk.state).unwrap();
    let seen = orders.order_status("X1").await;
    assert_eq!(
        (
            seen.status.as_str(),
            seen.filled_quantity,
            seen.average_price
        ),
        ("COMPLETE", 10, Decimal::new(1015, 1))
    );
    let unknown = orders.order_status("nope").await;
    assert_eq!(
        (unknown.status.as_str(), unknown.filled_quantity),
        ("OPEN", 0)
    );
    let keyless = Desk::new(ist_on(9, 40, 0), |s| s.kite_api_key = String::new()).await;
    assert!(order_source(&keyless.state).is_none());
}

// =============================================================================================
// The loop's desk hooks
// =============================================================================================

#[tokio::test]
async fn the_desk_hooks_record_the_start_beat_with_the_gate_and_reload_with_circuit_bands() {
    let desk = Desk::new(ist_on(9, 40, 0), |s| s.swing_monitor_enabled = true)
        .await
        .with_tables()
        .await;
    seed(&desk).await;
    desk.exec(
        "CREATE TABLE lv_heartbeat (user_id INTEGER, process TEXT, state TEXT, detail TEXT, \
         at TEXT, PRIMARY KEY (user_id, process))",
        &[],
    )
    .await;
    let mut hooks = DeskHooks::new(desk.state.clone(), monitor_conn(&desk).await, 1, day());
    hooks.on_first_tick().await.unwrap();
    let session = desk
        .rows("SELECT mode, monitor_ran, signals, notes FROM sw_session")
        .await
        .remove(0);
    assert_eq!(session.get("monitor_ran"), Some(&DbValue::Int(1)));
    assert_eq!(
        session.get("mode"),
        Some(&DbValue::Text("DRY_RUN".to_owned()))
    );
    assert_eq!(
        session.get("notes"),
        Some(&DbValue::Text("swing-monitor".to_owned()))
    );
    let mut store = store_for(&desk, monitor_conn(&desk).await);
    hooks
        .heartbeat(
            &mut store,
            "watching 1 name(s), 0 signal(s) raised, at 09:40:00",
        )
        .await
        .unwrap();
    let beat = desk
        .rows("SELECT state, detail FROM lv_heartbeat")
        .await
        .remove(0);
    assert_eq!(
        beat.get("detail"),
        Some(&DbValue::Text(
            "watching 1 name(s), 0 signal(s) raised, at 09:40:00 · gate GREEN (last close)"
                .to_owned()
        ))
    );
    assert_eq!(
        beat.get("state"),
        Some(&DbValue::Text("watching".to_owned()))
    );
    let list = hooks.reload().await.unwrap();
    assert_eq!(list.len(), 1);
    assert_eq!(list[0].symbol, "AAA");
    assert_eq!(list[0].upper_circuit, Some(Decimal::new(12095, 2)));
    assert!(list[0].focus);
    assert_eq!(list[0].pivot_high, Some(Decimal::from(100)));
    // The bands were one raw `quote` (Python's `kite.kc.quote`): a read, never a write.
    assert!(desk.kite.calls().iter().any(|c| c.route == "market.quote"));
    desk.no_writes();
}

#[test]
fn the_ticker_bus_is_read_as_pythons_queues() {
    let bus = Arc::new(crate::lanes::session::ticker::TickBus::new());
    bus.attach();
    let mut queues = BusQueues::new(bus.clone());
    queues.subscribe(1);
    let mut t = PyDict::default();
    t.insert("instrument_token", PyValue::Int(1));
    t.insert("last_price", PyValue::Float(101.0));
    bus.publish(&[t.clone(), t.clone()]).unwrap();
    assert_eq!(queues.drain(1), vec![t.clone(), t]);
    assert!(queues.drain(1).is_empty());
    assert!(
        queues.drain(2).is_empty(),
        "an unsubscribed token has no queue"
    );
}

#[tokio::test]
async fn the_harness_route_is_inert_without_the_harness() {
    let desk = Desk::new(ist_on(9, 40, 0), |_| {}).await;
    let app = crate::app::build_app(desk.state.clone());
    for method in ["POST", "GET"] {
        let req = Request::builder()
            .method(method)
            .uri("/__harness/swing-signal")
            .header("host", "127.0.0.1")
            .header("origin", "http://127.0.0.1")
            .body(Body::from("{}"))
            .unwrap();
        let resp = app.clone().oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND, "{method}");
        let body = axum::body::to_bytes(resp.into_body(), 1 << 16)
            .await
            .unwrap();
        assert_eq!(&body[..], br#"{"detail":"Not Found"}"#);
    }
}

#[test]
fn a_trigger_state_the_harness_names_is_one_python_knows() {
    // The route parses the verdict state by Python's enum values.
    let code = super::tests::code_only(include_str!("harness.rs"));
    for (word, state) in [
        ("\"TRIGGERED\"", TriggerState::Triggered),
        ("\"LOCKED_UPPER_CIRCUIT\"", TriggerState::LockedUpperCircuit),
        ("\"BELOW_PIVOT\"", TriggerState::BelowPivot),
    ] {
        assert!(code.contains(word));
        assert_eq!(
            format!(
                "\"{}\"",
                crate::lanes::swing::breakout::trigger_state_str(state)
            ),
            word
        );
    }
}

// =============================================================================================
// tests/test_swing_monitor_schedule.py
// =============================================================================================

fn ist_dt(y: i32, mo: u32, d: u32, h: u32, mi: u32) -> DateTime<FixedOffset> {
    ist().with_ymd_and_hms(y, mo, d, h, mi, 0).unwrap()
}

/// `CLOSE`: `monitor_close` from the config, as the heredoc reads it.
fn close() -> NaiveTime {
    super::tests::hm(SwingConfig::default().opening_range.monitor_close)
}

/// Friday 11 Sep 2026 13:09, the restart that sat out an afternoon.
fn friday() -> DateTime<FixedOffset> {
    ist_dt(2026, 9, 11, 13, 9)
}

#[test]
fn test_a_restart_after_the_open_starts_at_once() {
    assert_eq!(next_run(friday(), true, close()), friday());
}

#[test]
fn test_it_does_not_wait_for_monday() {
    assert_eq!(
        next_run(friday(), true, close()).date_naive(),
        friday().date_naive()
    );
}

#[test]
fn test_a_disabled_monitor_still_waits_for_the_next_open() {
    assert_eq!(
        next_run(friday(), false, close()),
        ist_dt(2026, 9, 14, 9, 14)
    );
}

#[test]
fn test_before_the_open_it_waits_for_the_open() {
    assert_eq!(
        next_run(ist_dt(2026, 9, 11, 6, 0), true, close()),
        ist_dt(2026, 9, 11, 9, 14)
    );
}

#[test]
fn test_after_the_close_it_waits_for_the_next_weekday() {
    assert_eq!(
        next_run(ist_dt(2026, 9, 10, 19, 0), true, close()),
        ist_dt(2026, 9, 11, 9, 14)
    );
}

#[test]
fn test_friday_evening_skips_the_weekend() {
    assert_eq!(
        next_run(ist_dt(2026, 9, 11, 19, 0), true, close()),
        ist_dt(2026, 9, 14, 9, 14)
    );
}

#[test]
fn test_a_weekend_afternoon_is_not_a_session_to_join() {
    assert_eq!(
        next_run(ist_dt(2026, 9, 12, 13, 0), true, close()),
        ist_dt(2026, 9, 14, 9, 14)
    );
}

#[test]
fn test_exactly_at_the_close_it_is_too_late_to_join() {
    assert_eq!(
        next_run(ist_dt(2026, 9, 11, 15, 30), true, close()),
        ist_dt(2026, 9, 14, 9, 14)
    );
}

fn loop_source() -> String {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../decile-blueprint/infra/docker/Dockerfile.desk");
    let text = std::fs::read_to_string(path).unwrap();
    let start = text
        .find("RUN cat > /usr/local/bin/swing-monitor-loop")
        .unwrap();
    let body = &text[text[start..].find('\n').unwrap() + start + 1..];
    body[..body.find("\nEOF").unwrap()].to_owned()
}

#[test]
fn test_the_loop_reads_monitor_close_rather_than_hardcoding_it() {
    let source = loop_source();
    assert!(source.contains("DEFAULT_SWING_CONFIG.opening_range.monitor_close"));
    assert!(
        source.contains("CLOSE = dt.time(15, 30)"),
        "the fallback is gone"
    );
    // The Rust `next_run` is the heredoc's: the same 09:14, the same weekday and close rule.
    assert!(source.contains("AT = dt.time(9, 14)"));
    assert!(source.contains("if enabled and now.weekday() <= 4 and now.time() < CLOSE:"));
}

#[test]
fn test_the_fallback_matches_todays_config() {
    assert_eq!(SwingConfig::default().opening_range.monitor_close, (15, 30));
}
