//! `tests/test_swing_desk.py`, the store and the view called directly: `TestStore`,
//! `TestView`, `TestTheSessionLock`, `TestTheSessionContext`, `TestResizeLine`, the store halves
//! of `TestPendingRangeOnTheDesk` and `TestTheLateFillReads`, `TestFirstLiveHeaderAndFocus`'s
//! view tests, `TestScanNowStatusBar.test_scan_status_is_pure_over_the_two_rows`,
//! `TestStatusBar`'s view and token tests and `TestThePollCostsNoKiteCall`. Each test names its
//! Python original; the asserts are Python's, in Rust's spelling.
//!
//! Where Python's `Spy` connection records every statement (`test_store_schema_prefix_...`), the
//! Rust test runs the store with `schema = "public"` against a connection whose `main` database
//! holds a **decoy** table of every name with a single unrelated column, and the real tables
//! attached as `public`: an unqualified reference resolves to the decoy first and fails.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::str::FromStr;
use std::sync::Arc;

use chrono::{Duration, NaiveDate};
use rust_decimal::Decimal;

use super::scenario::{
    LineSpec, MarketSpec, PositionSpec, Scenario, USER, after_close, int_of, iso, iso_d, ist_at,
    now, today,
};
use super::*;
use crate::lanes::swing::store::{Fields, PgSwingStore, StoreError, SwingStore};
use crate::lanes::swing::values::Val as SVal;
use crate::storage::{Db, Param};

fn d(text: &str) -> Decimal {
    Decimal::from_str(text).unwrap()
}

/// `token={"present": False, "label": "", "expired": True, "age_minutes": None}`.
fn no_token() -> Rec {
    Rec::of([
        ("present", Val::Bool(false)),
        ("label", Val::from("")),
        ("expired", Val::Bool(true)),
        ("age_minutes", Val::None),
    ])
}

/// `build_view(store, now=, token=...)` with the test environment's flags (DRY_RUN on, the swing
/// flags off).
fn paper() -> ViewFlags {
    ViewFlags {
        dry_run: true,
        execution_enabled: false,
        monitor_enabled: false,
        token: no_token(),
    }
}

fn live() -> ViewFlags {
    ViewFlags {
        dry_run: false,
        execution_enabled: true,
        ..paper()
    }
}

async fn view_json(
    store: &mut PgSwingStore,
    at: DateTime<FixedOffset>,
    flags: &ViewFlags,
) -> serde_json::Value {
    let v = build_view(store, at, flags).await.unwrap();
    let text = crate::wire::orjson_dumps(&PyValue::Map(jsonable_rec(&v))).unwrap();
    serde_json::from_str(&text).unwrap()
}

fn ids_of(rows: &serde_json::Value) -> Vec<i64> {
    rows.as_array()
        .unwrap()
        .iter()
        .map(|r| r["id"].as_i64().unwrap())
        .collect()
}

// =============================================================================================
// TestStore
// =============================================================================================

#[tokio::test]
async fn test_store_plan_and_line_round_trip_with_decimals_and_tz_aware_stamps() {
    let mut sc = Scenario::new().await;
    let ids = sc.morning().await;
    let mut store = sc.store().await;
    let plan = store.plan(&ids.signal_plan.1).await.unwrap().unwrap();
    assert_eq!(plan.id, ids.signal_plan.0);
    assert_eq!(plan.plan_id, ids.signal_plan.1);
    assert_eq!(plan.as_of, Some(today()));
    assert_eq!(
        (plan.source.as_str(), plan.gate.as_str()),
        ("SIGNAL", "GREEN")
    );
    assert_eq!(plan.built_at, Some(ist_at(2026, 9, 2, 9, 31)));
    assert_eq!(
        plan.expires_at,
        Some(plan.built_at.unwrap() + Duration::minutes(30))
    );
    assert_eq!(plan.exposure_level, 2);
    assert!(
        store
            .plan("6c3f8a52-0d2e-4c1a-9b7e-3f1d2a4b5c6d")
            .await
            .unwrap()
            .is_none()
    );
    // a uuid column on Postgres: no driver error
    assert!(store.plan("not-a-uuid").await.unwrap().is_none());

    let line = store.line(ids.trigger_line).await.unwrap().unwrap();
    assert_eq!(line.plan_pk, plan.id);
    assert_eq!(line.plan_id, plan.plan_id);
    assert_eq!(
        (line.kind.as_str(), line.symbol.as_str()),
        ("BUY_ON_TRIGGER", "ALPHAFLAG")
    );
    assert_eq!((line.instrument_id, line.quantity), (1, 1666));
    assert_eq!(line.trigger, Some(d("100.80")));
    assert_eq!(line.stop, Some(d("97.80")));
    assert_eq!(line.risk_inr, Some(d("300.00")));
    assert_eq!(line.position_value, Some(d("10080.00")));
    assert_eq!(
        (line.state.as_str(), line.trail.as_deref()),
        ("PROPOSED", Some("MA10"))
    );
    assert_eq!(
        line.client_id,
        format!("{}:ALPHAFLAG:BUY_ON_TRIGGER", plan.plan_id)
    );
    assert!(store.line(424_242).await.unwrap().is_none());
}

#[tokio::test]
async fn test_store_line_without_prices_returns_none_not_zero() {
    let mut sc = Scenario::new().await;
    let ids = sc.morning().await;
    let mut store = sc.store().await;
    let sell = store.line(ids.sell).await.unwrap().unwrap();
    assert_eq!(sell.trigger, None);
    assert_eq!(sell.stop, None);
    assert_eq!(sell.kind, "SELL_AT_OPEN");
}

#[tokio::test]
async fn test_store_set_line_keeps_what_it_was_not_told_to_change() {
    let mut sc = Scenario::new().await;
    let ids = sc.morning().await;
    let mut store = sc.store().await;
    store
        .set_line(ids.trigger_line, "SENT", Some("order-1"), None)
        .await
        .unwrap();
    let line = store.line(ids.trigger_line).await.unwrap().unwrap();
    assert_eq!(
        (
            line.state.as_str(),
            line.journal_ref.as_deref(),
            line.position_id
        ),
        ("SENT", Some("order-1"), None)
    );
    store
        .set_line(ids.trigger_line, "FILLED", None, Some(9))
        .await
        .unwrap();
    let line = store.line(ids.trigger_line).await.unwrap().unwrap();
    assert_eq!(
        (
            line.state.as_str(),
            line.journal_ref.as_deref(),
            line.position_id
        ),
        ("FILLED", Some("order-1"), Some(9))
    );
    // both at once, as `_record_line` does — the two must land in their own columns
    store
        .set_line(ids.sell, "FILLED", Some("DRY-abc:X:BUY"), Some(4))
        .await
        .unwrap();
    let line = store.line(ids.sell).await.unwrap().unwrap();
    assert_eq!(
        (line.journal_ref.as_deref(), line.position_id),
        (Some("DRY-abc:X:BUY"), Some(4))
    );
}

#[tokio::test]
async fn test_store_positions_round_trip_and_open_position_for() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    let mut store = sc.store().await;
    let fields: Fields = vec![
        ("instrument_id", SVal::Int(1)),
        ("setup", SVal::Text("FLAG".into())),
        ("entry_date", SVal::Date(today())),
        ("entry_avg", SVal::Dec(d("100.8"))),
        ("quantity_entered", SVal::Int(1666)),
        ("initial_stop", SVal::Dec(d("97.8"))),
        ("stop", SVal::Dec(d("97.8"))),
        ("trail", SVal::Text("MA10".into())),
        ("gtt_id", SVal::Text("G-1".into())),
        ("gtt_trigger", SVal::Dec(d("97.80"))),
        ("gtt_armed_at", SVal::Stamp(now())),
        ("simulated", SVal::Bool(true)),
    ];
    let pid = store.create_position(fields).await.unwrap();
    let pos = store.position(pid).await.unwrap().unwrap();
    assert_eq!(
        (pos.id, pos.symbol.as_str(), pos.state.as_str()),
        (pid, "ALPHAFLAG", "OPEN")
    );
    assert_eq!(pos.quantity_open, 1666, "defaulted to quantity_entered");
    assert_eq!(pos.entry_avg, Some(d("100.8000")));
    assert_eq!(pos.entry_avg.unwrap().scale(), 4);
    assert_eq!(pos.stop, Some(d("97.80")));
    assert_eq!(pos.initial_stop, Some(d("97.80")));
    assert_eq!(pos.gtt_armed_at, Some(now()));
    assert_eq!(pos.entry_date, Some(today()));
    assert!(pos.simulated && !pos.partial_done);
    assert_eq!((pos.closed_on, pos.r_multiple), (None, None));
    assert_eq!(store.open_position_for(1).await.unwrap().unwrap().id, pid);
    assert!(store.open_position_for(2).await.unwrap().is_none());
    assert!(store.position(999).await.unwrap().is_none());

    store
        .update_position(
            pid,
            vec![
                ("quantity_open", SVal::Int(1111)),
                ("partial_done", SVal::Bool(true)),
                ("partial_date", SVal::Date(today())),
                ("state", SVal::Text("PARTIAL".into())),
                ("stop", SVal::Dec(d("100.80"))),
                ("gtt_id", SVal::Null),
                ("gtt_trigger", SVal::Null),
            ],
        )
        .await
        .unwrap();
    let pos = store.position(pid).await.unwrap().unwrap();
    assert_eq!((pos.quantity_open, pos.state.as_str()), (1111, "PARTIAL"));
    assert!(pos.partial_done);
    assert_eq!(pos.partial_date, Some(today()));
    assert_eq!(pos.stop, Some(d("100.80")));
    assert_eq!(pos.gtt_id, None);
    // PARTIAL with shares still counts
    assert_eq!(store.open_position_for(1).await.unwrap().unwrap().id, pid);

    store
        .update_position(
            pid,
            vec![
                ("quantity_open", SVal::Int(0)),
                ("state", SVal::Text("CLOSED".into())),
                ("closed_on", SVal::Date(today())),
                ("exit_avg", SVal::Dec(d("104.25"))),
                ("close_reason", SVal::Text("MANUAL".into())),
                ("r_multiple", SVal::Dec(d("1.15"))),
                ("pnl_inr", SVal::Dec(d("5748.7"))),
            ],
        )
        .await
        .unwrap();
    let pos = store.position(pid).await.unwrap().unwrap();
    assert_eq!(pos.state, "CLOSED");
    assert_eq!(pos.exit_avg, Some(d("104.2500")));
    assert_eq!(pos.r_multiple, Some(d("1.15")));
    assert_eq!(pos.pnl_inr, Some(d("5748.70")));
    assert!(store.open_position_for(1).await.unwrap().is_none());
}

#[tokio::test]
async fn test_store_rounds_at_write_time() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    let mut store = sc.store().await;
    let pid = store
        .create_position(vec![
            ("instrument_id", SVal::Int(1)),
            ("setup", SVal::Text("FLAG".into())),
            ("entry_date", SVal::Date(today())),
            ("entry_avg", SVal::Dec(d("100.123456"))),
            ("quantity_entered", SVal::Int(10)),
            ("initial_stop", SVal::Dec(d("97.805"))),
            ("stop", SVal::Dec(d("97.805"))),
            ("trail", SVal::Text("MA20".into())),
        ])
        .await
        .unwrap();
    let rows = sc
        .exec(
            "SELECT entry_avg, stop FROM sw_position WHERE id = ?",
            &[Param::Int(pid)],
        )
        .await;
    let raw = |k: &str| crate::lanes::swing::values::raw_dec(rows[0].get(k).unwrap()).unwrap();
    assert_eq!(raw("entry_avg"), d("100.1235"));
    assert_eq!(raw("stop"), d("97.81"));
}

#[tokio::test]
async fn test_store_refuses_a_column_it_does_not_know() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    let mut store = sc.store().await;
    let e = store
        .create_position(vec![
            ("instrument_id", SVal::Int(1)),
            ("gtt", SVal::Text("no".into())),
        ])
        .await
        .unwrap_err();
    assert!(matches!(e, StoreError::Value(_)), "{e}");
    let e = store
        .update_position(1, vec![("gtt", SVal::Text("no".into()))])
        .await
        .unwrap_err();
    assert!(matches!(e, StoreError::Value(_)), "{e}");
}

#[tokio::test]
async fn test_store_create_position_accepts_the_symbol_execute_line_passes() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    let mut store = sc.store().await;
    let pid = store
        .create_position(vec![
            ("instrument_id", SVal::Int(2)),
            ("symbol", SVal::Text("BETAEP".into())),
            ("setup", SVal::Text("EP".into())),
            ("entry_date", SVal::Date(today())),
            ("entry_avg", SVal::Dec(d("210.50"))),
            ("quantity_entered", SVal::Int(50)),
            ("quantity_open", SVal::Int(50)),
            ("initial_stop", SVal::Dec(d("204.50"))),
            ("stop", SVal::Dec(d("204.50"))),
            ("trail", SVal::Text("MA20".into())),
            ("gtt_id", SVal::Null),
            ("gtt_trigger", SVal::Null),
            ("gtt_armed_at", SVal::Null),
            ("partial_done", SVal::Bool(false)),
            ("partial_date", SVal::Null),
            ("state", SVal::Text("OPEN".into())),
            ("closed_on", SVal::Null),
            ("exit_avg", SVal::Null),
            ("close_reason", SVal::Null),
            ("r_multiple", SVal::Null),
            ("pnl_inr", SVal::Null),
            ("simulated", SVal::Bool(true)),
        ])
        .await
        .unwrap();
    assert_eq!(store.position(pid).await.unwrap().unwrap().symbol, "BETAEP");
}

#[tokio::test]
async fn test_store_add_fill_round_trips() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    let pid = sc.position(PositionSpec::new(1)).await;
    let mut store = sc.store().await;
    let fid = store
        .add_fill(vec![
            ("position_id", SVal::Int(pid)),
            ("side", SVal::Text("BUY".into())),
            ("quantity", SVal::Int(300)),
            ("price", SVal::Dec(d("100.80"))),
            ("filled_at", SVal::Stamp(now())),
            ("journal_ref", SVal::Text("sim-1".into())),
            ("simulated", SVal::Bool(true)),
        ])
        .await
        .unwrap();
    let fills = store.fills_for(pid).await.unwrap();
    assert_eq!(fills.len(), 1);
    let fill = &fills[0];
    assert_eq!(
        (fill.id, fill.side.as_str(), fill.quantity),
        (fid, "BUY", 300)
    );
    assert_eq!(fill.price, Some(d("100.8000")));
    assert_eq!(fill.price.unwrap().scale(), 4);
    assert_eq!(fill.filled_at, Some(now()));
    assert!(fill.simulated);
    let e = store
        .add_fill(vec![
            ("position_id", SVal::Int(pid)),
            ("side", SVal::Text("BUY".into())),
            ("qty", SVal::Int(1)),
        ])
        .await
        .unwrap_err();
    assert!(matches!(e, StoreError::Value(_)));
}

#[tokio::test]
async fn test_store_bump_session_upserts_and_adds() {
    let mut sc = Scenario::new().await;
    let mut store = sc.store().await;
    assert!(store.session(today()).await.unwrap().is_none());
    store
        .bump_session(today(), "DRY_RUN", 1, 0, 0)
        .await
        .unwrap();
    let row = store.session(today()).await.unwrap().unwrap();
    assert_eq!(
        (row.mode.as_str(), row.confirms, row.fills),
        ("DRY_RUN", 1, 0)
    );
    assert!(!row.monitor_ran);
    assert_eq!(row.session_date, Some(today()));
    store
        .bump_session(today(), "DRY_RUN", 1, 1, 2)
        .await
        .unwrap();
    let row = store.session(today()).await.unwrap().unwrap();
    assert_eq!((row.confirms, row.fills, row.manage_actions), (2, 1, 2));
    let n = sc.exec("SELECT count(*) AS n FROM sw_session", &[]).await;
    assert_eq!(int_of(&n[0], "n"), 1);
}

#[tokio::test]
async fn test_store_bump_session_onto_the_monitors_row_keeps_its_mark() {
    let mut sc = Scenario::new().await;
    sc.session(true, 4, 0, 0).await;
    let mut store = sc.store().await;
    store
        .bump_session(today(), "DRY_RUN", 1, 1, 0)
        .await
        .unwrap();
    let row = store.session(today()).await.unwrap().unwrap();
    assert!(row.monitor_ran);
    assert_eq!((row.signals, row.confirms, row.fills), (4, 1, 1));
}

#[tokio::test]
async fn test_store_config_and_first_live_sessions() {
    let mut sc = Scenario::new().await;
    sc.config_with("250000.00", "0.750", 3, true, true).await;
    let mut store = sc.store().await;
    let cfg = store.config().await.unwrap();
    assert_eq!(cfg.sleeve_capital_inr, d("250000.00"));
    assert_eq!(cfg.risk_per_trade_pct, d("0.750"));
    assert_eq!((cfg.first_live_sessions_left, cfg.exposure_level), (3, 0));
    store.set_first_live_sessions_left(2).await.unwrap();
    assert_eq!(store.config().await.unwrap().first_live_sessions_left, 2);
    store.set_first_live_sessions_left(-1).await.unwrap();
    assert_eq!(
        store.config().await.unwrap().first_live_sessions_left,
        0,
        "never below the CHECK"
    );
}

#[tokio::test]
async fn test_store_config_without_a_row_is_the_schema_default() {
    let sc = Scenario::new().await;
    let mut store = sc.store().await;
    let cfg = store.config().await.unwrap();
    assert_eq!(cfg.sleeve_capital_inr, Decimal::ZERO);
    assert_eq!(cfg.first_live_sessions_left, 5);
    assert_eq!(cfg.risk_per_trade_pct, d("0.5"));
    assert!(!cfg.present);
}

#[tokio::test]
async fn test_store_is_scoped_to_its_user() {
    let mut sc = Scenario::new().await;
    let ids = sc.morning().await;
    let mut other = sc.store_for(2).await;
    assert!(other.plan(&ids.signal_plan.1).await.unwrap().is_none());
    assert!(other.line(ids.trigger_line).await.unwrap().is_none());
    assert!(other.position(ids.held).await.unwrap().is_none());
    assert!(other.open_positions().await.unwrap().is_empty());
    assert!(other.signals_for(today()).await.unwrap().is_empty());
}

/// The `main` database of the spy connection: every table the store may touch, as a decoy with
/// one unrelated column, so an unqualified statement resolves here and fails.
const DECOYS: [&str; 15] = [
    "instrument",
    "sw_config",
    "sw_market_daily",
    "sw_setup_daily",
    "sw_plan",
    "sw_plan_line",
    "sw_plan_skip",
    "sw_watch",
    "sw_signal",
    "sw_position",
    "sw_fill",
    "sw_session",
    "sw_catalyst",
    "sw_scan_run",
    "lv_protection_issue",
];

#[tokio::test]
async fn test_store_schema_prefix_qualifies_every_table() {
    let mut sc = Scenario::new().await;
    let ids = sc.morning().await;
    let spy_path = sc.dir.path().join("decoy.db");
    let mut conn = Db::connect_sqlite_path(&spy_path).await.unwrap();
    for t in DECOYS {
        conn.execute(&format!("CREATE TABLE {t} (decoy_only INTEGER)"), &[])
            .await
            .unwrap();
    }
    conn.execute(
        "ATTACH DATABASE ? AS public",
        &[Param::Text(sc.path.display().to_string())],
    )
    .await
    .unwrap();
    let mut s = PgSwingStore {
        conn,
        user_id: USER,
        schema: "public".to_owned(),
        broker_account_id: Some(1),
        live_level: None,
        settings: crate::settings::Settings::defaults().unwrap(),
        clock: Arc::new(crate::clock::FixedClock::new(now())),
    };
    let day = today();
    // Python's list, then every other statement the store issues.
    s.config().await.unwrap();
    s.plan(&ids.signal_plan.1).await.unwrap();
    s.line(1).await.unwrap();
    s.position(1).await.unwrap();
    s.open_position_for(1).await.unwrap();
    s.session(day).await.unwrap();
    s.signals_for(day).await.unwrap();
    s.latest_plan("MORNING").await.unwrap();
    s.open_positions().await.unwrap();
    s.recent_manage_actions(5).await.unwrap();
    s.skips_for(1).await.unwrap();
    s.lines_for(1).await.unwrap();
    s.signal_skip_for(1, day).await.unwrap();
    s.bump_session(day, "DRY_RUN", 0, 0, 0).await.unwrap();
    s.set_first_live_sessions_left(4).await.unwrap();
    s.set_line(1, "EXPIRED", None, None).await.unwrap();
    s.fills_for(1).await.unwrap();
    s.latest_scan_run().await.unwrap();
    s.latest_market_scan().await.unwrap();
    s.catalysts_for(&[1, 2]).await.unwrap();
    s.range_high_for(ids.trigger_line).await.unwrap();
    s.line_by_order("ORD-1").await.unwrap();
    s.sent_buy_lines(day).await.unwrap();
    s.note_line(ids.waiting, "x").await.unwrap();
    s.expire_pending(day).await.unwrap();
    s.resize_line(ids.waiting, 10, d("1"), d("1"), "n")
        .await
        .unwrap();
    s.note_session(day, "n").await.unwrap();
    s.open_protection_issues().await.unwrap();
    s.session_context(day).await.unwrap();
    let pid = s
        .create_position(vec![
            ("instrument_id", SVal::Int(5)),
            ("setup", SVal::Text("FLAG".into())),
            ("entry_date", SVal::Date(day)),
            ("entry_avg", SVal::Dec(d("10"))),
            ("quantity_entered", SVal::Int(1)),
            ("initial_stop", SVal::Dec(d("9"))),
            ("stop", SVal::Dec(d("9"))),
            ("trail", SVal::Text("MA10".into())),
        ])
        .await
        .unwrap();
    s.update_position(pid, vec![("quantity_open", SVal::Int(1))])
        .await
        .unwrap();
    s.add_fill(vec![
        ("position_id", SVal::Int(pid)),
        ("side", SVal::Text("BUY".into())),
        ("quantity", SVal::Int(1)),
        ("price", SVal::Dec(d("10"))),
        ("filled_at", SVal::Stamp(now())),
    ])
    .await
    .unwrap();
    s.lock_begin(day).await.unwrap();
    s.lock_commit().await.unwrap();
    s.request_scan(now(), Duration::seconds(60), Duration::seconds(600), "desk")
        .await
        .unwrap()
        .unwrap();
    // The writes landed in the attached (real) tables, never in a decoy.
    let n = s
        .conn
        .execute("SELECT count(*) AS n FROM main.sw_session", &[])
        .await
        .unwrap();
    assert_eq!(int_of(&n.rows[0], "n"), 0);
    // And the source agrees: every table a statement names is the `{}` the schema fills.
    let re = regex::Regex::new(r#"\b(?:FROM|JOIN|INTO|UPDATE)\s+(?:\\\s*)?([A-Za-z_][A-Za-z_.]*)"#)
        .unwrap();
    for (name, source) in [
        ("store.rs", include_str!("../store.rs")),
        ("context.rs", include_str!("../context.rs")),
    ] {
        for line in source
            .lines()
            .filter(|l| l.contains('"') && !l.trim_start().starts_with("//"))
        {
            for cap in re.captures_iter(line) {
                let word = &cap[1];
                assert!(
                    matches!(word, "SET" | "the" | "a" | "an" | "this" | "that" | "it")
                        || word.chars().all(|c| c.is_lowercase() && c != '_')
                            && !DECOYS.contains(&word),
                    "{name}: an unqualified table in {line:?}"
                );
            }
        }
        assert!(
            !source.contains("$1"),
            "{name}: `?` placeholders, the adapter's contract"
        );
    }
}

#[tokio::test]
async fn test_store_open_positions_lead_with_the_naked_one() {
    let mut sc = Scenario::new().await;
    let ids = sc.morning().await;
    let mut store = sc.store().await;
    let rows = store.open_positions().await.unwrap();
    assert_eq!(
        rows.iter().map(|r| r.id).collect::<Vec<_>>(),
        [ids.naked, ids.held]
    );
    assert_eq!(rows[0].gtt_id, None);
    assert_eq!(rows[1].gtt_id.as_deref(), Some("G-44"));
}

// =============================================================================================
// TestView
// =============================================================================================

#[tokio::test]
async fn test_a_waiting_buy_for_a_name_already_held_gets_no_button() {
    let mut sc = Scenario::new().await;
    let ids = sc.morning().await;
    let mut store = sc.store().await;
    let v = view_json(&mut store, now(), &paper()).await;
    let buys = v["plans"]["morning"]["buys"].as_array().unwrap().clone();
    let by = |id: i64| buys.iter().find(|b| b["id"] == id).unwrap().clone();
    assert_eq!(by(ids.waiting)["confirmable"], true);
    let held = by(ids.waiting_held);
    assert_eq!(held["confirmable"], false);
    assert!(
        held["why_not"]
            .as_str()
            .unwrap()
            .starts_with("already held")
    );
}

#[tokio::test]
async fn test_an_exit_for_a_name_the_book_does_not_hold_gets_no_button() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    let (pk, pid) = sc.plan("MORNING", now() - Duration::minutes(5)).await;
    let lid = sc
        .line(
            pk,
            &pid,
            LineSpec {
                quantity: 10,
                trigger: None,
                stop: None,
                ..LineSpec::new("SELL_AT_OPEN", 5, "EPSILON")
            },
        )
        .await;
    let mut store = sc.store().await;
    let v = view_json(&mut store, now(), &paper()).await;
    let exits = v["plans"]["morning"]["exits"].as_array().unwrap();
    assert_eq!(exits.len(), 1);
    assert_eq!(exits[0]["id"], lid);
    assert_eq!(exits[0]["confirmable"], false);
    assert_eq!(exits[0]["why_not"], "not a swing position");
}

#[tokio::test]
async fn test_a_sell_for_more_than_open_and_a_raise_below_the_stop_get_no_button() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    sc.position(PositionSpec {
        quantity: 300,
        quantity_open: Some(100),
        stop: "100.00",
        initial_stop: Some("97.80"),
        ..PositionSpec::new(4)
    })
    .await;
    let (pk, pid) = sc.plan("MORNING", now() - Duration::minutes(5)).await;
    let big = sc
        .line(
            pk,
            &pid,
            LineSpec {
                quantity: 150,
                trigger: None,
                stop: None,
                ..LineSpec::new("SELL_AT_OPEN", 4, "DELTAHELD")
            },
        )
        .await;
    let low = sc
        .line(
            pk,
            &pid,
            LineSpec {
                quantity: 0,
                trigger: None,
                stop: Some("99.00"),
                ..LineSpec::new("RAISE_GTT_STOP", 4, "DELTAHELD")
            },
        )
        .await;
    let mut store = sc.store().await;
    let v = view_json(&mut store, now(), &paper()).await;
    let exits = v["plans"]["morning"]["exits"].as_array().unwrap().clone();
    let by = |id: i64| exits.iter().find(|e| e["id"] == id).unwrap().clone();
    assert_eq!(by(big)["confirmable"], false);
    assert!(
        by(big)["why_not"]
            .as_str()
            .unwrap()
            .contains("more than the 100 open")
    );
    assert_eq!(by(low)["confirmable"], false);
    assert!(
        by(low)["why_not"]
            .as_str()
            .unwrap()
            .contains("resting stop")
    );
}

#[tokio::test]
async fn test_yesterdays_morning_plan_is_not_todays() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    sc.plan_full(
        "MORNING",
        ist_at(2026, 9, 1, 9, 10),
        NaiveDate::from_ymd_opt(2026, 9, 1).unwrap(),
        30,
    )
    .await;
    let mut store = sc.store().await;
    let v = view_json(&mut store, now(), &paper()).await;
    assert_eq!(v["plans"]["morning"], serde_json::Value::Null);
}

#[tokio::test]
async fn test_the_eod_preview_is_shown_expired_with_its_lines_and_no_buttons() {
    let mut sc = Scenario::new().await;
    sc.morning().await;
    let mut store = sc.store().await;
    let v = view_json(&mut store, now(), &paper()).await;
    let preview = &v["plans"]["preview"];
    assert_eq!(preview["source"], "EOD_PREVIEW");
    assert_eq!(preview["expired"], true);
    assert_eq!(preview["countdown"], "0:00");
    let exits = preview["exits"].as_array().unwrap();
    assert!(!exits.is_empty());
    assert!(exits.iter().all(|e| e["confirmable"] == false));
}

#[tokio::test]
async fn test_the_fingerprint_moves_when_a_line_moves() {
    let mut sc = Scenario::new().await;
    let ids = sc.morning().await;
    let mut store = sc.store().await;
    let before = view_json(&mut store, now(), &paper()).await["fingerprint"].clone();
    assert_eq!(
        before,
        view_json(&mut store, now(), &paper()).await["fingerprint"]
    );
    store
        .set_line(ids.trigger_line, "FILLED", None, None)
        .await
        .unwrap();
    assert_ne!(
        view_json(&mut store, now(), &paper()).await["fingerprint"],
        before
    );
}

/// `test_the_view_needs_a_tz_aware_clock`: Python raises `ValueError` for a naive `now`. The Rust
/// signature takes a `DateTime<FixedOffset>`, so a naive clock does not compile; what remains to
/// assert is the other half of Python's line — `now.astimezone(IST)` — that an aware instant in
/// any offset is read as the same IST moment.
#[tokio::test]
async fn test_the_view_needs_a_tz_aware_clock() {
    let mut sc = Scenario::new().await;
    sc.morning().await;
    let mut store = sc.store().await;
    let utc = now().with_timezone(&FixedOffset::east_opt(0).unwrap());
    assert_eq!(utc.to_rfc3339(), "2026-09-02T04:10:00+00:00");
    let in_ist = view_json(&mut store, now(), &paper()).await;
    let in_utc = view_json(&mut store, utc, &paper()).await;
    assert_eq!(in_ist, in_utc);
}

// =============================================================================================
// TestStatusBar (the view and the token, called directly)
// =============================================================================================

#[tokio::test]
async fn test_status_bar_shows_the_token_age_when_a_token_exists() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("tok.json");
    // Python's test saves with a fresh key and puts it in the environment; the Rust process
    // environment is shared by every test thread, so the token is saved under the key the store
    // itself resolves for this path with no configured key — the same `store_for` the bar reads.
    let issued = now() - Duration::minutes(125);
    crate::lanes::exec_core::token_store::store_for(&path, "")
        .unwrap()
        .save("secret", Some(issued))
        .unwrap();
    let info = token_age(now(), &path);
    assert_eq!(info.val("present"), Val::Bool(true));
    assert_eq!(info.val("expired"), Val::Bool(false));
    assert_eq!(info.val("age_minutes"), Val::Int(125));
    assert_eq!(info.val("label").py_str(), "2h 05m old");
    let stale = token_age(now() + Duration::days(1), &path);
    assert_eq!(stale.val("expired"), Val::Bool(true));
    assert!(stale.val("label").py_str().starts_with("expired"));
}

#[test]
fn test_status_bar_token_lookup_never_creates_a_key_file() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("absent.json");
    let _ = token_age(now(), &path);
    assert_eq!(std::fs::read_dir(dir.path()).unwrap().count(), 0);
}

#[test]
fn test_status_bar_monitor_state_is_derived_from_flag_clock_and_mark() {
    let at = |h: u32, m: u32| ist_at(2026, 9, 2, h, m);
    for (enabled, ran, when, state) in [
        (false, false, at(9, 40), "not enabled"),
        (true, false, at(8, 50), "idle"),
        (true, false, at(9, 40), "running"),
        (true, true, after_close(), "stopped"),
        (true, false, after_close(), "did not run"),
        (true, false, at(14, 0), "running"),
    ] {
        assert_eq!(
            monitor_state(enabled, ran, when).val("state").py_str(),
            state,
            "{enabled} {ran} {when}"
        );
    }
}

#[test]
fn test_status_bar_monitor_is_idle_on_a_weekend_not_missing() {
    let saturday = ist_at(2026, 9, 5, 11, 0);
    assert_eq!(
        monitor_state(true, false, saturday).val("state").py_str(),
        "idle"
    );
}

#[tokio::test]
async fn test_status_bar_refresh_is_five_seconds_only_inside_the_window() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    let mut s = sc.store().await;
    let inside = view_json(&mut s, now(), &paper()).await;
    assert_eq!(inside["poll_ms"], 5000);
    assert_eq!(inside["window_opens_in_ms"], serde_json::Value::Null);
    let before = view_json(&mut s, ist_at(2026, 9, 2, 8, 0), &paper()).await;
    assert_eq!(before["poll_ms"], 0);
    assert_eq!(before["window_opens_in_ms"], 75 * 60 * 1000);
    let during = view_json(&mut s, ist_at(2026, 9, 2, 14, 40), &paper()).await;
    assert_eq!(during["poll_ms"], 5000);
    assert_eq!(during["window_opens_in_ms"], serde_json::Value::Null);
    let after = view_json(&mut s, after_close(), &paper()).await;
    assert_eq!(after["poll_ms"], 0);
    assert_eq!(after["window_opens_in_ms"], serde_json::Value::Null);
}

// =============================================================================================
// TestTheSessionLock
// =============================================================================================

/// `store.conn.in_transaction is False`: a fresh `BEGIN` is accepted (SQLite refuses a nested
/// one), and is rolled straight back.
async fn not_in_transaction(store: &mut PgSwingStore) -> bool {
    let ok = store.conn.execute("BEGIN", &[]).await.is_ok();
    if ok {
        store.conn.execute("ROLLBACK", &[]).await.unwrap();
    }
    ok
}

#[tokio::test]
async fn test_lock_session_for_update_inserts_the_row_and_commits_what_was_written() {
    let mut sc = Scenario::new().await;
    let mut store = sc.store().await;
    assert!(store.session(today()).await.unwrap().is_none());
    store.lock_begin(today()).await.unwrap();
    assert!(
        store.session(today()).await.unwrap().is_some(),
        "the row is there before the body runs"
    );
    store
        .bump_session(today(), "DRY_RUN", 1, 0, 0)
        .await
        .unwrap();
    store.lock_commit().await.unwrap();
    let rows = sc
        .exec("SELECT confirms, mode, notes FROM sw_session", &[])
        .await;
    assert_eq!(int_of(&rows[0], "confirms"), 1);
    assert_eq!(
        crate::lanes::swing::values::text(rows[0].get("mode")),
        "DRY_RUN"
    );
    assert_eq!(
        crate::lanes::swing::values::text(rows[0].get("notes")),
        "swing-desk"
    );
    assert!(not_in_transaction(&mut store).await);
}

#[tokio::test]
async fn test_lock_keeps_an_existing_row_and_its_counters() {
    let mut sc = Scenario::new().await;
    sc.session(true, 4, 2, 0).await;
    let mut store = sc.store().await;
    store.lock_begin(today()).await.unwrap();
    store.lock_commit().await.unwrap();
    let row = store.session(today()).await.unwrap().unwrap();
    assert_eq!((row.monitor_ran, row.signals, row.confirms), (true, 4, 2));
}

#[tokio::test]
async fn test_lock_rolls_back_everything_written_inside_it_on_an_exception() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    let (pk, plan_id) = sc.plan("SIGNAL", now()).await;
    let line_id = sc
        .line(
            pk,
            &plan_id,
            LineSpec::new("BUY_ON_TRIGGER", 1, "ALPHAFLAG"),
        )
        .await;
    let mut store = sc.store().await;
    store.lock_begin(today()).await.unwrap();
    store
        .set_line(line_id, "CONFIRMED", None, None)
        .await
        .unwrap();
    store
        .bump_session(today(), "DRY_RUN", 1, 0, 0)
        .await
        .unwrap();
    store
        .create_position(vec![
            ("instrument_id", SVal::Int(1)),
            ("setup", SVal::Text("FLAG".into())),
            ("entry_date", SVal::Date(today())),
            ("entry_avg", SVal::Dec(d("100.80"))),
            ("quantity_entered", SVal::Int(10)),
            ("initial_stop", SVal::Dec(d("97.80"))),
            ("stop", SVal::Dec(d("97.80"))),
            ("trail", SVal::Text("MA10".into())),
        ])
        .await
        .unwrap();
    // "the broker call blew up": the caller's error path rolls the lock back.
    store.lock_rollback().await.unwrap();
    assert_eq!(
        store.line(line_id).await.unwrap().unwrap().state,
        "PROPOSED"
    );
    assert!(store.session(today()).await.unwrap().is_none());
    assert!(store.open_positions().await.unwrap().is_empty());
    assert!(not_in_transaction(&mut store).await);
    // and the connection is usable afterwards — a rollback is not a broken store
    store.lock_begin(today()).await.unwrap();
    store
        .bump_session(today(), "DRY_RUN", 1, 0, 0)
        .await
        .unwrap();
    store.lock_commit().await.unwrap();
    assert_eq!(store.session(today()).await.unwrap().unwrap().confirms, 1);
}

#[tokio::test]
async fn test_the_lock_is_real_a_second_connection_cannot_write_while_it_is_held() {
    let sc = Scenario::new().await;
    let mut store = sc.store().await;
    let mut other = sc.store().await;
    // Python's second connection has `timeout=0.2`.
    other
        .conn
        .execute("PRAGMA busy_timeout = 200", &[])
        .await
        .unwrap();
    store.lock_begin(today()).await.unwrap();
    let e = other.lock_begin(today()).await.unwrap_err();
    assert!(e.to_string().contains("locked"), "{e}");
    store.lock_commit().await.unwrap();
    // released on exit: the second store now gets its turn and sees the first's row
    other.lock_begin(today()).await.unwrap();
    other
        .bump_session(today(), "DRY_RUN", 1, 0, 0)
        .await
        .unwrap();
    other.lock_commit().await.unwrap();
    assert_eq!(store.session(today()).await.unwrap().unwrap().confirms, 1);
}

#[test]
fn test_the_lock_statement_is_for_update_on_postgres_and_begin_immediate_on_sqlite() {
    let source = include_str!("../store.rs");
    let start = source.rfind("async fn lock_begin").unwrap();
    let end = source.rfind("async fn session_context(&mut self").unwrap();
    let lock = &source[start..end];
    assert!(lock.contains("FOR UPDATE") && lock.contains("BEGIN IMMEDIATE"));
    assert!(lock.contains("ROLLBACK") && lock.contains("COMMIT"));
}

// =============================================================================================
// TestTheSessionContext
// =============================================================================================

#[tokio::test]
async fn test_session_context_reads_the_market_row_the_book_and_todays_lines() {
    let mut sc = Scenario::new().await;
    let ids = sc.morning().await;
    let (pk, plan_id) = ids.signal_plan.clone();
    sc.line(
        pk,
        &plan_id,
        LineSpec {
            quantity: 100,
            trigger: Some("300.50"),
            stop: Some("295.00"),
            state: "SENT",
            ..LineSpec::new("BUY_ON_TRIGGER", 5, "EPSILON")
        },
    )
    .await;
    let filled = sc
        .line(
            pk,
            &plan_id,
            LineSpec {
                quantity: 300,
                state: "FILLED",
                ..LineSpec::new("BUY_ON_TRIGGER", 4, "DELTAHELD")
            },
        )
        .await;
    sc.exec(
        "UPDATE sw_plan_line SET position_id = ? WHERE id = ?",
        &[Param::Int(ids.held), Param::Int(filled)],
    )
    .await;
    let mut store = sc.store().await;
    let ctx = store.session_context(today()).await.unwrap();
    assert_eq!(ctx.gate, baskfy_core::swing::market::MarketGate::Green);
    assert_eq!(ctx.tier.level, 2);
    assert_eq!(
        (ctx.tier.max_open_positions, ctx.tier.max_exposure_pct),
        (6, 75.0)
    );
    assert!(ctx.tier.new_entries_allowed && !ctx.tier.drawdown_locked);
    assert_eq!(ctx.account.equity, d("1000000.00"));
    let held = d("100.80") * Decimal::from(300) + d("210.50") * Decimal::from(300);
    let sent = d("300.50") * Decimal::from(100);
    assert_eq!(ctx.account.open_exposure_inr, held + sent);
    assert_eq!(ctx.account.cash_available, d("1000000.00") - held - sent);
    assert_eq!(
        ctx.account.open_symbols,
        ["DELTAHELD", "BETAEP", "EPSILON"]
            .iter()
            .map(|s| (*s).to_owned())
            .collect()
    );
    assert_eq!(
        ctx.entries_today, 2,
        "the SENT and the FILLED line; the PROPOSED ones are not entries"
    );
    assert_eq!(
        ctx.pending
            .iter()
            .map(|p| p.symbol.as_str())
            .collect::<Vec<_>>(),
        ["EPSILON"]
    );
    assert_eq!(
        ctx.detected["ALPHAFLAG"],
        (d("5.00"), Some(Decimal::from(100_000_000)), d("72.00"))
    );
    assert_eq!(
        ctx.detected.keys().map(String::as_str).collect::<Vec<_>>(),
        ["ALPHAFLAG", "BETAEP", "DELTAHELD", "EPSILON", "GAMMALOCK"]
    );
}

#[tokio::test]
async fn test_session_context_without_a_market_row_is_red_and_without_detections_is_blind() {
    let mut sc = Scenario::new().await;
    sc.config_with("1000000", "0.500", 5, false, false).await;
    let mut store = sc.store().await;
    let ctx = store.session_context(today()).await.unwrap();
    assert_eq!(ctx.gate, baskfy_core::swing::market::MarketGate::Red);
    assert!(!ctx.tier.new_entries_allowed);
    assert_eq!(ctx.tier.level, 0);
    assert!(ctx.detected.is_empty());
    assert_eq!(ctx.account.equity, d("1000000.00"));
    assert_eq!(ctx.entries_today, 0);
}

#[tokio::test]
async fn test_session_context_takes_the_latest_row_before_the_day_never_the_days_own() {
    let mut sc = Scenario::new().await;
    sc.config_with("1000000", "0.500", 5, false, true).await;
    for (on, rung, max_open, pct) in [
        (today() - Duration::days(7), 3, 10, "100.00"),
        (today() - Duration::days(1), 0, 2, "25.00"),
        (today(), 3, 10, "100.00"),
    ] {
        sc.market(MarketSpec {
            on,
            rung,
            max_open_positions: max_open,
            max_exposure_pct: pct,
            ..MarketSpec::default()
        })
        .await;
    }
    let mut store = sc.store().await;
    let ctx = store.session_context(today()).await.unwrap();
    assert_eq!(
        ctx.tier.level, 0,
        "last night's close, not a row dated today"
    );
}

#[tokio::test]
async fn test_session_context_carries_the_drawdown_lock() {
    let mut sc = Scenario::new().await;
    sc.config_with("1000000", "0.500", 5, false, true).await;
    sc.market(MarketSpec {
        drawdown_locked: true,
        new_entries_allowed: false,
        ..MarketSpec::default()
    })
    .await;
    let mut store = sc.store().await;
    assert!(
        store
            .session_context(today())
            .await
            .unwrap()
            .tier
            .drawdown_locked
    );
}

#[tokio::test]
async fn test_another_users_book_is_not_in_the_context() {
    let mut sc = Scenario::new().await;
    sc.morning().await;
    let mut other = sc.store_for(USER + 1).await;
    let ctx = other.session_context(today()).await.unwrap();
    assert!(ctx.account.open_symbols.is_empty());
    assert_eq!(ctx.entries_today, 0);
    assert_eq!(ctx.gate, baskfy_core::swing::market::MarketGate::Red);
    assert!(ctx.detected.is_empty());
}

// =============================================================================================
// TestResizeLine
// =============================================================================================

#[tokio::test]
async fn test_resize_line_rewrites_the_three_numbers_and_the_note_at_the_schemas_scale() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    let (pk, plan_id) = sc.plan("SIGNAL", now()).await;
    let line_id = sc
        .line(
            pk,
            &plan_id,
            LineSpec {
                quantity: 1666,
                ..LineSpec::new("BUY_ON_TRIGGER", 1, "ALPHAFLAG")
            },
        )
        .await;
    let mut store = sc.store().await;
    store
        .resize_line(
            line_id,
            390,
            d("2340"),
            d("81900.005"),
            "re-sized at confirm 1666 \u{2192} 390",
        )
        .await
        .unwrap();
    let line = store.line(line_id).await.unwrap().unwrap();
    assert_eq!(line.quantity, 390);
    assert_eq!(line.risk_inr, Some(d("2340.00")));
    assert_eq!(
        line.position_value,
        Some(d("81900.01")),
        "rounded at write time (house rule 8)"
    );
    assert_eq!(
        line.note.as_deref(),
        Some("re-sized at confirm 1666 \u{2192} 390")
    );
    assert_eq!(line.state, "PROPOSED");
    assert_eq!(line.trigger, Some(d("100.80")));
}

#[tokio::test]
async fn test_resize_line_is_scoped_to_the_user() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    let (pk, plan_id) = sc.plan("SIGNAL", now()).await;
    let line_id = sc
        .line(
            pk,
            &plan_id,
            LineSpec {
                quantity: 1666,
                ..LineSpec::new("BUY_ON_TRIGGER", 1, "ALPHAFLAG")
            },
        )
        .await;
    let mut other = sc.store_for(USER + 1).await;
    other
        .resize_line(line_id, 1, Decimal::ONE, Decimal::ONE, "x")
        .await
        .unwrap();
    let mut store = sc.store().await;
    assert_eq!(store.line(line_id).await.unwrap().unwrap().quantity, 1666);
}

// =============================================================================================
// TestPendingRangeOnTheDesk (the store and the view)
// =============================================================================================

/// `TestPendingRangeOnTheDesk._pending`.
pub(super) async fn pending(sc: &mut Scenario) -> (i64, String, i64) {
    sc.config().await;
    let (pk, plan_id) = sc.plan("MORNING", ist_at(2026, 9, 2, 9, 12)).await;
    let line_id = sc
        .line(
            pk,
            &plan_id,
            LineSpec {
                quantity: 0,
                trigger: Some("112.50"),
                stop: None,
                setup: Some("EP"),
                note: Some("EP score 46.20; live gap; slot reserved"),
                ..LineSpec::new("PENDING_RANGE", 5, "EPSILON")
            },
        )
        .await;
    (pk, plan_id, line_id)
}

#[tokio::test]
async fn test_the_view_lists_pending_lines_apart_from_the_buys_and_in_the_fingerprint() {
    let mut sc = Scenario::new().await;
    let (_, _, line_id) = pending(&mut sc).await;
    let mut store = sc.store().await;
    let view = view_json(&mut store, now(), &paper()).await;
    let plan = &view["plans"]["morning"];
    assert_eq!(ids_of(&plan["pending"]), [line_id]);
    assert_eq!(plan["buys"], serde_json::json!([]));
    assert_eq!(plan["pending"][0]["confirmable"], false);
    let before = view["fingerprint"].clone();
    store.expire_pending(today()).await.unwrap();
    let after = view_json(&mut store, now(), &paper()).await;
    assert_ne!(after["fingerprint"], before);
}

#[tokio::test]
async fn test_expire_pending_frees_only_todays_proposed_pending_lines() {
    let mut sc = Scenario::new().await;
    let (_, _, line_id) = pending(&mut sc).await;
    let (pk, plan_id) = sc
        .plan_full(
            "MORNING",
            ist_at(2026, 9, 1, 9, 12),
            today() - Duration::days(1),
            30,
        )
        .await;
    let yesterday = sc
        .line(
            pk,
            &plan_id,
            LineSpec {
                quantity: 0,
                trigger: Some("50.00"),
                stop: None,
                setup: Some("EP"),
                ..LineSpec::new("PENDING_RANGE", 3, "GAMMALOCK")
            },
        )
        .await;
    let mut store = sc.store().await;
    assert_eq!(store.expire_pending(today()).await.unwrap(), 1);
    let line = store.line(line_id).await.unwrap().unwrap();
    assert_eq!(line.state, "EXPIRED");
    assert!(line.note.unwrap().contains("slot freed at 10:45"));
    assert_eq!(
        store.line(yesterday).await.unwrap().unwrap().state,
        "PROPOSED"
    );
    assert_eq!(
        store.expire_pending(today()).await.unwrap(),
        0,
        "freed exactly once"
    );
}

// =============================================================================================
// TestTheLateFillReads
// =============================================================================================

#[tokio::test]
async fn test_range_high_for_reads_the_signal_that_became_the_line() {
    let mut sc = Scenario::new().await;
    let ids = sc.morning().await;
    let mut store = sc.store().await;
    assert_eq!(
        store.range_high_for(ids.trigger_line).await.unwrap(),
        Some(d("100.50"))
    );
    assert_eq!(
        store.range_high_for(ids.waiting).await.unwrap(),
        None,
        "an EOD entry has no range"
    );
}

#[tokio::test]
async fn test_line_by_order_and_sent_buy_lines_find_only_sent_buys() {
    let mut sc = Scenario::new().await;
    let ids = sc.morning().await;
    let mut store = sc.store().await;
    assert!(store.line_by_order("ORD-9").await.unwrap().is_none());
    assert!(store.sent_buy_lines(today()).await.unwrap().is_empty());
    store
        .set_line(ids.trigger_line, "SENT", Some("ORD-9"), None)
        .await
        .unwrap();
    let found = store.line_by_order("ORD-9").await.unwrap().unwrap();
    assert_eq!(found.id, ids.trigger_line);
    assert_eq!(
        store
            .sent_buy_lines(today())
            .await
            .unwrap()
            .iter()
            .map(|l| l.id)
            .collect::<Vec<_>>(),
        [ids.trigger_line]
    );
    store
        .set_line(ids.trigger_line, "FILLED", None, None)
        .await
        .unwrap();
    assert!(store.line_by_order("ORD-9").await.unwrap().is_none());
}

#[tokio::test]
async fn test_note_line_appends_and_a_position_carries_half_risk() {
    let mut sc = Scenario::new().await;
    let ids = sc.morning().await;
    let mut store = sc.store().await;
    store
        .note_line(ids.trigger_line, "10:45 cutoff: 40 filled")
        .await
        .unwrap();
    store.note_line(ids.trigger_line, "again").await.unwrap();
    assert!(
        store
            .line(ids.trigger_line)
            .await
            .unwrap()
            .unwrap()
            .note
            .unwrap()
            .ends_with("10:45 cutoff: 40 filled; again")
    );
    let pid = store
        .create_position(vec![
            ("instrument_id", SVal::Int(5)),
            ("symbol", SVal::Text("EPSILON".into())),
            ("setup", SVal::Text("FLAG".into())),
            ("entry_date", SVal::Date(today())),
            ("entry_avg", SVal::Dec(d("100.10"))),
            ("quantity_entered", SVal::Int(40)),
            ("quantity_open", SVal::Int(40)),
            ("initial_stop", SVal::Dec(d("96.00"))),
            ("stop", SVal::Dec(d("96.00"))),
            ("trail", SVal::Text("MA20".into())),
            ("state", SVal::Text("OPEN".into())),
            ("simulated", SVal::Bool(false)),
            ("half_risk", SVal::Bool(true)),
        ])
        .await
        .unwrap();
    assert!(store.position(pid).await.unwrap().unwrap().half_risk);
    assert!(!store.position(ids.held).await.unwrap().unwrap().half_risk);
}

// =============================================================================================
// TestFirstLiveHeaderAndFocus (the view)
// =============================================================================================

#[tokio::test]
async fn test_the_header_says_half_risk_only_when_a_confirm_would_be_real() {
    let mut sc = Scenario::new().await;
    sc.config_with("1000000", "0.500", 3, true, true).await;
    let mut store = sc.store().await;
    let p = view_json(&mut store, now(), &paper()).await;
    let l = view_json(&mut store, now(), &live()).await;
    assert_eq!(
        p["sleeve"]["first_live_header"],
        "first live sessions: 3 left \u{b7} risk 0.500%"
    );
    assert_eq!(p["sleeve"]["half_risk"], false);
    assert_eq!(
        l["sleeve"]["first_live_header"],
        "first live sessions: 3 left \u{b7} risk 0.250%"
    );
    assert_eq!(l["sleeve"]["half_risk"], true);
    assert_eq!(l["sleeve"]["risk_multiplier"], "0.5");
}

#[tokio::test]
async fn test_the_header_is_empty_once_the_countdown_is_done() {
    let mut sc = Scenario::new().await;
    sc.config_with("1000000", "0.500", 0, true, true).await;
    let mut store = sc.store().await;
    let v = view_json(&mut store, now(), &live()).await;
    assert_eq!(v["sleeve"]["first_live_header"], "");
    assert_eq!(v["sleeve"]["half_risk"], false);
}

#[tokio::test]
async fn test_focus_triggers_are_listed_first() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    sc.exec(
        "INSERT INTO sw_watch(id, user_id, instrument_id, setup, source, added_on, focus) \
         VALUES (1, ?, 3, 'FLAG', 'DETECTOR', ?, true), (2, ?, 5, 'FLAG', 'DETECTOR', ?, false)",
        &[
            Param::Int(USER),
            Param::Text(iso_d(today())),
            Param::Int(USER),
            Param::Text(iso_d(today())),
        ],
    )
    .await;
    sc.exec(
        "INSERT INTO sw_signal(user_id, watch_id, instrument_id, setup, session_date, \
         raised_at, state, last_price) VALUES (?, 1, 3, 'FLAG', ?, ?, 'BELOW_PIVOT', 50), \
         (?, 2, 5, 'FLAG', ?, ?, 'BELOW_PIVOT', 300)",
        &[
            Param::Int(USER),
            Param::Text(iso_d(today())),
            Param::Text(iso(ist_at(2026, 9, 2, 9, 20))),
            Param::Int(USER),
            Param::Text(iso_d(today())),
            Param::Text(iso(ist_at(2026, 9, 2, 9, 35))),
        ],
    )
    .await;
    let mut store = sc.store().await;
    let v = view_json(&mut store, now(), &paper()).await;
    let got: Vec<(String, bool)> = v["triggers"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| {
            (
                t["symbol"].as_str().unwrap().to_owned(),
                t["focus"].as_bool().unwrap(),
            )
        })
        .collect();
    assert_eq!(
        got,
        [
            ("GAMMALOCK".to_owned(), true),
            ("EPSILON".to_owned(), false)
        ]
    );
}

// =============================================================================================
// TestScanNowStatusBar.test_scan_status_is_pure_over_the_two_rows
// =============================================================================================

#[test]
fn test_scan_status_is_pure_over_the_two_rows() {
    let empty = scan_status(None, None);
    let j = |r: &Rec| -> serde_json::Value {
        serde_json::from_str(&crate::wire::orjson_dumps(&PyValue::Map(jsonable_rec(r))).unwrap())
            .unwrap()
    };
    assert_eq!(
        j(&empty),
        serde_json::json!({
            "date": null, "provisional": false, "scanned_at": null, "label": "",
            "run": null, "run_label": "", "in_flight": false,
        })
    );
    // Key order is Python's dict order too.
    assert_eq!(
        empty.0.iter().map(|(k, _)| k.as_str()).collect::<Vec<_>>(),
        [
            "date",
            "provisional",
            "scanned_at",
            "label",
            "run",
            "run_label",
            "in_flight"
        ]
    );
    let queued = ScanRun {
        id: 3,
        requested_at: Some(now()),
        started_at: None,
        finished_at: None,
        session_date: None,
        provisional: false,
        status: "QUEUED".to_owned(),
        funnel: None,
        detail: None,
        error: None,
        task_id: None,
    };
    let market = MarketScan {
        date: Some(today()),
        provisional: true,
        scanned_at: None,
    };
    let v = scan_status(Some(&market), Some(&queued));
    assert_eq!(
        v.val("label").py_str(),
        "provisional \u{2014} from live quotes"
    );
    assert_eq!(v.val("run_label").py_str(), "scan queued");
    assert_eq!(v.val("in_flight"), Val::Bool(true));
}

// =============================================================================================
// TestThePollCostsNoKiteCall
// =============================================================================================

/// SW20: twelve polls of the view touch no broker. `build_view` takes no Kite handle at all — the
/// store it reads is built with `live_level: None` here and the poll path never builds one on
/// SQLite — so the proof is twelve views over a store whose only I/O is the file, and a source
/// check that nothing `build_view` calls names the Kite client.
#[tokio::test]
async fn test_twelve_polls_touch_no_broker() {
    let mut sc = Scenario::new().await;
    sc.config().await;
    let mut store = sc.store().await;
    for _ in 0..12 {
        let v = view_json(&mut store, now(), &paper()).await;
        assert_eq!(v["poll_ms"], 5000);
    }
    let source = include_str!("../desk.rs");
    let start = source.find("pub async fn build_view(").unwrap();
    let end = source.find("pub fn unavailable_view(").unwrap();
    let body = &source[start..end];
    for word in ["kite", "ltp(", "quote(", "gateway"] {
        assert!(
            !body.to_lowercase().contains(word),
            "build_view names {word}"
        );
    }
}
