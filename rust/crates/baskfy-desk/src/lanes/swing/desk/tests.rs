//! `tests/test_swing_desk.py` and `tests/test_breadth_source.py`, the pure half: the status
//! bar's monitor state and refresh, the countdown, the SWING prefix, when a Confirm may appear
//! (`_line_view`), the trigger sentence, SW15's scan line, the fingerprint's hash, the exit
//! rule, and the breadth decision. The routes and the rendered page are graded against the
//! Python desk by `tools/parity/desk_cases/swing_desk.json`.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::BTreeSet;
use std::str::FromStr;

use async_trait::async_trait;
use chrono::{DateTime, FixedOffset, NaiveDate};
use rust_decimal::Decimal;

use super::*;
use crate::lanes::swing::breadth::{PipelineBreadth, PipelineError, breadth_for_plan};
use crate::lanes::swing::store::{LineRow, MarketScan, PositionRow, ScanRun, SignalRow};

fn at(text: &str) -> DateTime<FixedOffset> {
    DateTime::parse_from_rfc3339(text).unwrap()
}

fn d(text: &str) -> Decimal {
    Decimal::from_str(text).unwrap()
}

fn s(rec: &Rec, key: &str) -> String {
    rec.val(key).py_str()
}

// --- the status bar ------------------------------------------------------------------------

#[test]
fn monitor_state_is_derived_from_flag_clock_and_mark() {
    let wed = |t: &str| at(&format!("2026-09-02T{t}+05:30"));
    let off = monitor_state(false, false, wed("09:40:00"));
    assert_eq!(s(&off, "state"), "not enabled");
    assert_eq!(
        s(&off, "label"),
        "not enabled (BASKFY_SWING_MONITOR_ENABLED=false)"
    );
    assert_eq!(
        s(&monitor_state(true, true, wed("09:40:00")), "label"),
        "stopped at 15:30"
    );
    let before = monitor_state(true, false, wed("09:00:00"));
    assert_eq!(s(&before, "state"), "idle");
    assert_eq!(s(&before, "label"), "idle \u{2014} starts at 09:15");
    let running = monitor_state(true, false, wed("09:40:00"));
    assert_eq!(s(&running, "label"), "running since 09:15");
    // The window closes at monitor_close (SW26: 15:30), inclusive.
    assert_eq!(
        s(&monitor_state(true, false, wed("15:30:00")), "state"),
        "running"
    );
    let after = monitor_state(true, false, wed("15:31:00"));
    assert_eq!(s(&after, "state"), "did not run");
    assert_eq!(
        s(&after, "label"),
        "did not run \u{2014} no monitor_ran mark after 15:30"
    );
}

#[test]
fn monitor_is_idle_on_a_weekend_not_missing() {
    let sat = at("2026-09-05T11:00:00+05:30");
    let v = monitor_state(true, false, sat);
    assert_eq!(s(&v, "state"), "idle");
    assert_eq!(s(&v, "label"), "idle (not a trading day)");
    // A UTC clock is read in IST: 04:00Z on a Wednesday is 09:30 IST.
    assert_eq!(
        s(
            &monitor_state(true, false, at("2026-09-02T04:00:00Z")),
            "state"
        ),
        "running"
    );
}

#[test]
fn refresh_is_five_seconds_only_inside_the_window() {
    assert_eq!(refresh(at("2026-09-02T09:40:00+05:30")), (5_000, None));
    assert_eq!(refresh(at("2026-09-02T09:15:00+05:30")), (5_000, None));
    assert_eq!(refresh(at("2026-09-02T15:30:00+05:30")), (5_000, None));
    assert_eq!(refresh(at("2026-09-02T09:14:00+05:30")), (0, Some(60_000)));
    assert_eq!(refresh(at("2026-09-02T15:31:00+05:30")), (0, None));
}

#[test]
fn token_lookup_without_a_file_says_log_in_and_creates_nothing() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("kite_token.json");
    let v = token_age(at("2026-09-02T09:40:00+05:30"), &path);
    assert_eq!(v.val("present"), Val::Bool(false));
    assert_eq!(s(&v, "label"), "no token \u{2014} log in");
    assert_eq!(v.val("age_minutes"), Val::None);
    assert_eq!(v.val("expired"), Val::Bool(true));
    assert_eq!(std::fs::read_dir(dir.path()).unwrap().count(), 0);
}

// --- the plan's lines ----------------------------------------------------------------------

#[test]
fn countdown_is_m_ss_and_never_negative() {
    let now = at("2026-09-02T09:40:00+05:30");
    assert_eq!(countdown(None, now), "0:00");
    assert_eq!(
        countdown(Some(at("2026-09-02T09:39:00+05:30")), now),
        "0:00"
    );
    assert_eq!(
        countdown(Some(at("2026-09-02T09:41:30+05:30")), now),
        "1:30"
    );
    assert_eq!(
        countdown(Some(at("2026-09-02T10:10:00+05:30")), now),
        "30:00"
    );
    assert_eq!(
        countdown(Some(at("2026-09-02T09:40:05+05:30")), now),
        "0:05"
    );
}

#[test]
fn swing_prefix_on_every_line() {
    let kinds = [
        "BUY_ON_TRIGGER",
        "SELL_AT_OPEN",
        "RAISE_GTT_STOP",
        "PENDING_RANGE",
        "OTHER",
    ];
    for kind in kinds {
        let text = label(kind, "ABC", 10, Some(d("101.5")), Some(d("95")));
        assert!(text.starts_with("SWING "), "{text}");
    }
    assert_eq!(
        label("BUY_ON_TRIGGER", "ABC", 10, Some(d("101.5")), None),
        "SWING BUY ABC x10 @ 101.50"
    );
    assert_eq!(
        label("SELL_AT_OPEN", "ABC", 4, None, None),
        "SWING SELL ABC x4 at open"
    );
    assert_eq!(
        label("RAISE_GTT_STOP", "ABC", 4, None, Some(d("95"))),
        "SWING RAISE GTT ABC to 95.00"
    );
    assert_eq!(
        label("PENDING_RANGE", "ABC", 0, Some(d("100")), None),
        "SWING PENDING ABC \u{2014} range at 100.00, no stop yet"
    );
    assert_eq!(
        label("BUY_ON_TRIGGER", "ABC", 1, None, None),
        "SWING BUY ABC x1 @ \u{2014}"
    );
}

fn line(kind: &str, state: &str, quantity: i64, stop: Option<&str>) -> LineRow {
    LineRow {
        id: 7,
        plan_pk: 1,
        plan_id: "p".to_owned(),
        kind: kind.to_owned(),
        instrument_id: 42,
        symbol: "ABC".to_owned(),
        setup: Some("FLAG".to_owned()),
        quantity,
        trigger: Some(d("100")),
        stop: stop.map(d),
        risk_inr: None,
        position_value: None,
        trail: None,
        note: None,
        state: state.to_owned(),
        client_id: "c".to_owned(),
        journal_ref: None,
        position_id: None,
        updated_at: None,
        as_of: None,
        plan_source: "MORNING".to_owned(),
    }
}

fn position(open: i64, stop: Option<&str>) -> PositionRow {
    PositionRow {
        id: 1,
        instrument_id: 42,
        symbol: "ABC".to_owned(),
        setup: "FLAG".to_owned(),
        entry_date: None,
        entry_avg: Some(d("100")),
        quantity_entered: open,
        quantity_open: open,
        initial_stop: None,
        stop: stop.map(d),
        gtt_id: None,
        gtt_trigger: None,
        gtt_armed_at: None,
        trail: "MA10".to_owned(),
        partial_done: false,
        partial_date: None,
        state: "OPEN".to_owned(),
        closed_on: None,
        exit_avg: None,
        close_reason: None,
        r_multiple: None,
        pnl_inr: None,
        simulated: true,
        half_risk: false,
    }
}

#[test]
fn confirm_appears_only_on_a_proposed_line_of_a_live_plan() {
    let buy = line("BUY_ON_TRIGGER", "PROPOSED", 10, Some("95"));
    assert_eq!(why_not(&buy, false, None), "");
    assert_eq!(why_not(&buy, true, None), "plan expired");
    let sent = line("BUY_ON_TRIGGER", "SENT", 10, Some("95"));
    assert_eq!(why_not(&sent, false, None), "sent");
}

#[test]
fn a_waiting_buy_for_a_name_already_held_gets_no_button() {
    let buy = line("BUY_ON_TRIGGER", "PROPOSED", 10, Some("95"));
    assert_eq!(
        why_not(&buy, false, Some(&position(6, Some("94")))),
        "already held (6 open)"
    );
}

#[test]
fn an_exit_for_a_name_the_book_does_not_hold_gets_no_button() {
    for kind in ["SELL_AT_OPEN", "RAISE_GTT_STOP"] {
        let exit = line(kind, "PROPOSED", 5, Some("96"));
        assert_eq!(why_not(&exit, false, None), "not a swing position");
    }
}

#[test]
fn a_sell_for_more_than_open_and_a_raise_below_the_stop_get_no_button() {
    let held = position(6, Some("95.50"));
    let sell = line("SELL_AT_OPEN", "PROPOSED", 7, None);
    assert_eq!(why_not(&sell, false, Some(&held)), "more than the 6 open");
    assert_eq!(
        why_not(
            &line("SELL_AT_OPEN", "PROPOSED", 6, None),
            false,
            Some(&held)
        ),
        ""
    );
    let raise = line("RAISE_GTT_STOP", "PROPOSED", 6, Some("95.50"));
    assert_eq!(
        why_not(&raise, false, Some(&held)),
        "not above the resting stop 95.50"
    );
    let up = line("RAISE_GTT_STOP", "PROPOSED", 6, Some("97"));
    assert_eq!(why_not(&up, false, Some(&held)), "");
}

#[test]
fn a_pending_range_line_is_never_confirmable() {
    let pending = line("PENDING_RANGE", "PROPOSED", 0, None);
    assert_eq!(
        why_not(&pending, false, None),
        "pending the opening range \u{2014} not confirmable; the SIGNAL plan at window close is \
         the line"
    );
    let expired = line("PENDING_RANGE", "EXPIRED", 0, None);
    assert_eq!(why_not(&expired, false, None), "expired (pending range)");
    assert!(!EXECUTABLE_KINDS.contains(&PENDING_KIND));
}

#[test]
fn pct_of_sleeve_rounds_half_up_and_is_none_without_capital() {
    assert_eq!(
        pct_of_sleeve(Some(d("12500")), Some(d("100000"))),
        Some(d("12.50"))
    );
    assert_eq!(pct_of_sleeve(Some(d("1")), Some(d("0"))), None);
    assert_eq!(pct_of_sleeve(None, Some(d("1000"))), None);
    assert_eq!(pct_of_sleeve(Some(d("1")), Some(d("3"))), Some(d("33.33")));
    assert_eq!(pct_of_sleeve(Some(d("1")), Some(d("8"))), Some(d("12.50")));
    assert_eq!(
        pct_of_sleeve(Some(d("0.00125")), Some(d("1"))),
        Some(d("0.13"))
    );
}

fn signal(state: &str, window: Option<i64>) -> SignalRow {
    SignalRow {
        id: 1,
        instrument_id: 42,
        symbol: "ABC".to_owned(),
        setup: "FLAG".to_owned(),
        session_date: None,
        raised_at: None,
        state: state.to_owned(),
        or_window_minutes: window,
        range_high: Some(d("101.20")),
        range_low: Some(d("99.00")),
        low_of_day: None,
        last_price: Some(d("101.45")),
        entry: None,
        stop: None,
        plan_line_id: None,
        focus: false,
    }
}

#[test]
fn the_trigger_sentence_names_the_window_and_the_break() {
    assert_eq!(
        signal_text(&signal("TRIGGERED", Some(15))),
        "15-min ORH 101.20 broken at 101.45"
    );
    // No window recorded: the config's default (5 minutes).
    assert_eq!(
        signal_text(&signal("TRIGGERED", None)),
        "5-min ORH 101.20 broken at 101.45"
    );
    assert_eq!(
        signal_text(&signal("LOCKED_UPPER_CIRCUIT", Some(5))),
        "locked at the upper circuit (101.45) \u{2014} not enterable"
    );
    assert_eq!(
        signal_text(&signal("BELOW_PIVOT", Some(5))),
        "5-min ORH 101.20 broken at 101.45, below the pivot"
    );
    assert_eq!(
        signal_text(&signal("RANGE_INCOMPLETE", Some(5))),
        "range incomplete at 101.45"
    );
}

// --- SW15: the scan line -------------------------------------------------------------------

fn run(status: &str) -> ScanRun {
    ScanRun {
        id: 9,
        requested_at: Some(at("2026-09-02T13:40:00+05:30")),
        started_at: None,
        finished_at: Some(at("2026-09-02T13:42:10+05:30")),
        session_date: None,
        provisional: true,
        status: status.to_owned(),
        funnel: None,
        detail: None,
        error: None,
        task_id: None,
    }
}

#[test]
fn a_provisional_day_says_so_and_from_what() {
    let market = MarketScan {
        date: NaiveDate::from_ymd_opt(2026, 9, 2),
        provisional: true,
        scanned_at: Some(at("2026-09-02T08:12:00Z")),
    };
    let v = scan_status(Some(&market), None);
    assert_eq!(
        s(&v, "label"),
        "provisional \u{2014} scanned 13:42 IST from live quotes"
    );
    assert_eq!(v.val("provisional"), Val::Bool(true));
    let unstamped = MarketScan {
        scanned_at: None,
        ..market
    };
    assert_eq!(
        s(&scan_status(Some(&unstamped), None), "label"),
        "provisional \u{2014} from live quotes"
    );
}

#[test]
fn a_nightly_day_says_nothing_and_a_re_scan_says_re_scanned() {
    let nightly = MarketScan {
        date: NaiveDate::from_ymd_opt(2026, 9, 1),
        provisional: false,
        scanned_at: None,
    };
    assert_eq!(s(&scan_status(Some(&nightly), None), "label"), "");
    let rescan = MarketScan {
        scanned_at: Some(at("2026-09-01T18:02:00+05:30")),
        ..nightly
    };
    assert_eq!(
        s(&scan_status(Some(&rescan), None), "label"),
        "re-scanned 18:02 IST from published bars"
    );
    let none = scan_status(None, None);
    assert_eq!(none.val("date"), Val::None);
    assert_eq!(none.val("in_flight"), Val::Bool(false));
    assert_eq!(s(&none, "run_label"), "");
}

#[test]
fn the_last_run_is_beside_the_button_done_failed_or_in_flight() {
    let queued = scan_status(None, Some(&run("QUEUED")));
    assert_eq!(s(&queued, "run_label"), "scan queued");
    assert_eq!(queued.val("in_flight"), Val::Bool(true));
    let running = scan_status(None, Some(&run("RUNNING")));
    assert_eq!(s(&running, "run_label"), "scanning\u{2026}");
    let mut failed = run("FAILED");
    assert_eq!(
        s(&scan_status(None, Some(&failed)), "run_label"),
        "last scan failed"
    );
    failed.error = Some("no session".to_owned());
    let v = scan_status(None, Some(&failed));
    assert_eq!(s(&v, "run_label"), "last scan failed: no session");
    assert_eq!(v.val("in_flight"), Val::Bool(false));
    let mut done = run("DONE");
    assert_eq!(
        s(&scan_status(None, Some(&done)), "run_label"),
        "last scan 13:42 IST from live quotes"
    );
    let funnel =
        PyValue::from_json(br#"{"liquid": 412, "candidates": {"FLAG": 3, "EP": 2}}"#).unwrap();
    done.funnel = Some(funnel);
    done.provisional = false;
    assert_eq!(
        s(&scan_status(None, Some(&done)), "run_label"),
        "last scan 13:42 IST from published bars \u{b7} 412 liquid, 5 flagged"
    );
}

// --- the fingerprint, the exit rule --------------------------------------------------------

#[test]
fn sha1_is_fips_180_4() {
    assert_eq!(sha1_hex(b""), "da39a3ee5e6b4b0d3255bfef95601890afd80709");
    assert_eq!(sha1_hex(b"abc"), "a9993e364706816aba3e25717850c26c9cd0d89d");
    assert_eq!(
        sha1_hex(b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"),
        "84983e441c3bd26ebaae4aa1f95129e5e54670f1"
    );
    assert_eq!(
        sha1_hex(&[b'a'; 1000]),
        "291e9a6c66994949b57ba5e650361e98fc36b1ba"
    );
}

#[test]
fn the_exit_rule_is_the_swing_books_own() {
    assert_eq!(
        swing_exit_rule(),
        "stop under the range low, at most 10% below entry; 1/3 sold between bar 3 and 5, then \
         the stop to breakeven (or after 1R); the rest trails the 10-day MA (ADR at or above 6%) \
         or the 20-day MA; the GTT is the exit"
    );
}

#[test]
fn unavailable_view_is_a_banner_with_an_empty_book() {
    let flags = ViewFlags {
        dry_run: true,
        execution_enabled: false,
        monitor_enabled: false,
        token: Rec::new(),
    };
    let v = unavailable_view(
        "no such table: sw_config",
        at("2026-09-02T09:40:00+05:30"),
        &flags,
    );
    assert_eq!(v.val("available"), Val::Bool(false));
    assert_eq!(s(&v, "reason"), "no such table: sw_config");
    assert_eq!(v.val("sleeve"), Val::None);
    assert_eq!(v.val("poll_ms"), Val::Int(0));
    let keys: Vec<&str> = v.0.iter().map(|(k, _)| k.as_str()).collect();
    assert_eq!(
        keys,
        [
            "available",
            "reason",
            "now",
            "today",
            "status",
            "sleeve",
            "triggers",
            "plans",
            "exit_rule",
            "book",
            "poll_ms",
            "window_opens_in_ms",
            "fingerprint"
        ]
    );
    let json = crate::wire::orjson_dumps(&PyValue::Map(jsonable_rec(&v))).unwrap();
    assert!(
        json.contains(r#""now":"2026-09-02T09:40:00+05:30""#),
        "{json}"
    );
    assert!(json.contains(r#""simulated":true"#), "{json}");
}

#[test]
fn jsonable_writes_a_decimal_in_f_format_not_str() {
    assert_eq!(
        jsonable(&Val::Dec(d("0.50"))),
        PyValue::Str("0.50".to_owned())
    );
    assert_eq!(
        jsonable(&Val::Dec(d("12500.00"))),
        PyValue::Str("12500.00".to_owned())
    );
    assert_eq!(
        jsonable(&Val::Dec(d("0.0000001"))),
        PyValue::Str("0.0000001".to_owned())
    );
}

// --- the second law, restated for a page ---------------------------------------------------

fn code_only(source: &str) -> String {
    source
        .lines()
        .filter(|l| {
            let t = l.trim_start();
            !(t.starts_with("//") || t.starts_with("///") || t.starts_with("//!"))
        })
        .collect::<Vec<_>>()
        .join("\n")
}

#[test]
fn the_page_never_names_a_placing_verb_or_the_broker_writes() {
    let source = code_only(include_str!("../desk.rs"));
    for verb in [
        concat!("Kite", "Writes"),
        "place_order",
        "modify_order",
        "cancel_order",
        "place_gtt",
        "modify_gtt",
        "delete_gtt",
        ".place(",
        "auto_execute",
    ] {
        assert!(!source.contains(verb), "desk.rs names {verb}");
    }
}

#[test]
fn there_is_no_route_that_takes_more_than_one_line() {
    let source = code_only(include_str!("../desk.rs"));
    assert!(!source.to_lowercase().contains("confirm_all"));
    assert!(!source.contains("line_ids"));
}

// --- breadth_source -----------------------------------------------------------------------

struct Fixed(Result<(Option<f64>, BTreeSet<String>), PipelineError>);

#[async_trait]
impl PipelineBreadth for Fixed {
    async fn read(
        &self,
        _on: NaiveDate,
        _slug: &str,
    ) -> Result<(Option<f64>, BTreeSet<String>), PipelineError> {
        self.0.clone()
    }
}

fn set(items: &[&str]) -> BTreeSet<String> {
    items.iter().map(|s| (*s).to_owned()).collect()
}

fn get(m: &PyDict, k: &str) -> PyValue {
    m.get(k).cloned().unwrap_or(PyValue::None)
}

#[tokio::test]
async fn same_membership_uses_the_pipeline_and_says_so() {
    let on = NaiveDate::from_ymd_opt(2026, 8, 18).unwrap();
    let reader = Fixed(Ok((Some(68.6347), set(&["A", "B"]))));
    let out = breadth_for_plan(70.0, &set(&["A", "B"]), on, "nifty-total-market", &reader).await;
    let keys: Vec<String> = out.iter().map(|(k, _)| k.to_string()).collect();
    assert_eq!(
        keys,
        [
            "value",
            "source",
            "scan_value",
            "pipeline_value",
            "universe",
            "reason"
        ]
    );
    assert_eq!(get(&out, "value"), PyValue::Float(68.6347));
    assert_eq!(get(&out, "source"), PyValue::Str("pipeline".to_owned()));
    assert_eq!(get(&out, "scan_value"), PyValue::Float(70.0));
    assert_eq!(
        get(&out, "reason"),
        PyValue::Str("same 2 symbols as nifty-total-market on 2026-08-18".to_owned())
    );
}

#[tokio::test]
async fn a_different_universe_keeps_the_scans_figure_and_counts_both_sides() {
    let on = NaiveDate::from_ymd_opt(2026, 8, 18).unwrap();
    let reader = Fixed(Ok((Some(60.0), set(&["A", "B", "C"]))));
    let out = breadth_for_plan(70.0, &set(&["A", "D"]), on, "nifty-total-market", &reader).await;
    assert_eq!(get(&out, "value"), PyValue::Float(70.0));
    assert_eq!(get(&out, "source"), PyValue::Str("scan".to_owned()));
    assert_eq!(get(&out, "pipeline_value"), PyValue::Float(60.0));
    assert_eq!(
        get(&out, "reason"),
        PyValue::Str(
            "scan universe differs from nifty-total-market on 2026-08-18 (1 only in the scan, \
             2 only in the index); used the scan's own"
                .to_owned()
        )
    );
}

#[tokio::test]
async fn no_row_and_no_pipeline_both_fall_back_to_the_scan() {
    let on = NaiveDate::from_ymd_opt(2026, 8, 18).unwrap();
    let missing = Fixed(Ok((None, set(&["A"]))));
    let out = breadth_for_plan(55.5, &set(&["A"]), on, "u", &missing).await;
    assert_eq!(get(&out, "value"), PyValue::Float(55.5));
    assert_eq!(get(&out, "pipeline_value"), PyValue::None);
    assert_eq!(
        get(&out, "reason"),
        PyValue::Str("no market_health_daily row for 2026-08-18; used the scan's own".to_owned())
    );
    let down = Fixed(Err(PipelineError("OperationalError".to_owned())));
    let out = breadth_for_plan(55.5, &set(&["A"]), on, "u", &down).await;
    assert_eq!(get(&out, "source"), PyValue::Str("scan".to_owned()));
    assert_eq!(
        get(&out, "reason"),
        PyValue::Str("pipeline unreachable (OperationalError); used the scan's own".to_owned())
    );
}

// --- the view over a store: `Scenario.morning()` at 09:40 on Wednesday 2 Sep 2026 ----------

/// `tests/test_swing_desk.py`'s DDL (0028_swing.py in SQLite's spelling), dumped verbatim.
const SW_DDL: &str = include_str!("sw_ddl.sql");

async fn exec(conn: &mut crate::storage::Conn, sql: &str) {
    conn.execute(sql, &[]).await.unwrap();
}

async fn morning_store(dir: &std::path::Path) -> PgSwingStore {
    let mut conn = crate::storage::Db::connect_sqlite_path(&dir.join("swing.db"))
        .await
        .unwrap();
    for ddl in SW_DDL.split(";\n").filter(|s| !s.trim().is_empty()) {
        exec(&mut conn, ddl).await;
    }
    for sql in [
        "INSERT INTO instrument(id, symbol, name, kite_token) VALUES (1,'ALPHAFLAG','Alpha Flag Ltd',111),(2,'BETAEP','Beta EP Ltd',222),(3,'GAMMALOCK','Gamma Lock Ltd',333),(4,'DELTAHELD','Delta Held Ltd',444),(5,'EPSILON','Epsilon Ltd',555)",
        "INSERT INTO sw_config(user_id, sleeve_capital_inr, risk_per_trade_pct, first_live_sessions_left) VALUES (1, '1000000', '0.500', 5)",
        // held (armed) and naked positions
        "INSERT INTO sw_position(user_id, broker_account_id, instrument_id, setup, entry_date, entry_avg, quantity_entered, initial_stop, stop, gtt_id, gtt_trigger, gtt_armed_at, trail, quantity_open, state, simulated) VALUES (1, 1, 4, 'FLAG', '2026-08-30', '100.80', 300, '97.80', '97.80', 'G-44', '97.80', '2026-08-30T09:40:00+05:30', 'MA10', 300, 'OPEN', 1)",
        "INSERT INTO sw_position(user_id, broker_account_id, instrument_id, setup, entry_date, entry_avg, quantity_entered, initial_stop, stop, gtt_id, gtt_trigger, gtt_armed_at, trail, quantity_open, state, simulated) VALUES (1, 1, 2, 'FLAG', '2026-08-30', '210.50', 300, '204.50', '204.50', NULL, NULL, NULL, 'MA10', 300, 'OPEN', 1)",
        // last night's EOD preview (pk 1), the morning plan (pk 2), the 09:31 SIGNAL plan (pk 3), the skipped SIGNAL plan (pk 4)
        "INSERT INTO sw_plan(plan_id, user_id, as_of, source, built_at, expires_at, plan_hash, gate, exposure_level, total_risk_inr, total_new_exposure_inr) VALUES ('11111111-1111-4111-8111-111111111111', 1, '2026-09-01', 'EOD_PREVIEW', '2026-09-01T21:05:00+05:30', '2026-09-01T21:35:00+05:30', 'h', 'GREEN', 2, '5000.00', '150000.00')",
        "INSERT INTO sw_plan(plan_id, user_id, as_of, source, built_at, expires_at, plan_hash, gate, exposure_level, total_risk_inr, total_new_exposure_inr) VALUES ('22222222-2222-4222-8222-222222222222', 1, '2026-09-02', 'MORNING', '2026-09-02T09:12:00+05:30', '2026-09-02T09:42:00+05:30', 'h', 'GREEN', 2, '5000.00', '150000.00')",
        "INSERT INTO sw_plan(plan_id, user_id, as_of, source, built_at, expires_at, plan_hash, gate, exposure_level, total_risk_inr, total_new_exposure_inr) VALUES ('33333333-3333-4333-8333-333333333333', 1, '2026-09-02', 'SIGNAL', '2026-09-02T09:31:00+05:30', '2026-09-02T10:01:00+05:30', 'h', 'GREEN', 2, '5000.00', '150000.00')",
        "INSERT INTO sw_plan(plan_id, user_id, as_of, source, built_at, expires_at, plan_hash, gate, exposure_level, total_risk_inr, total_new_exposure_inr) VALUES ('44444444-4444-4444-8444-444444444444', 1, '2026-09-02', 'SIGNAL', '2026-09-02T09:35:00+05:30', '2026-09-02T10:05:00+05:30', 'h', 'GREEN', 2, '5000.00', '150000.00')",
        "INSERT INTO sw_plan_line(plan_id, user_id, kind, instrument_id, setup, quantity, trigger, stop, risk_inr, position_value, trail, note, state, client_id) VALUES (1, 1, 'SELL_AT_OPEN', 4, 'FLAG', 100, NULL, NULL, '300.00', '10080.00', 'MA10', 'partial into strength, day 3', 'PROPOSED', 'p1:DELTAHELD:SELL_AT_OPEN')",
        // morning lines: SELL (id 2), RAISE (id 3), waiting buy (id 4), waiting buy for a held name (id 5)
        "INSERT INTO sw_plan_line(plan_id, user_id, kind, instrument_id, setup, quantity, trigger, stop, risk_inr, position_value, trail, note, state, client_id) VALUES (2, 1, 'SELL_AT_OPEN', 4, 'FLAG', 100, NULL, NULL, '300.00', '10080.00', 'MA10', 'partial into strength, day 3', 'PROPOSED', 'p2:DELTAHELD:SELL_AT_OPEN')",
        "INSERT INTO sw_plan_line(plan_id, user_id, kind, instrument_id, setup, quantity, trigger, stop, risk_inr, position_value, trail, note, state, client_id) VALUES (2, 1, 'RAISE_GTT_STOP', 4, 'FLAG', 0, NULL, '100.80', '300.00', '10080.00', 'MA10', 'breakeven after partial', 'PROPOSED', 'p2:DELTAHELD:RAISE_GTT_STOP')",
        "INSERT INTO sw_plan_line(plan_id, user_id, kind, instrument_id, setup, quantity, trigger, stop, risk_inr, position_value, trail, note, state, client_id) VALUES (2, 1, 'BUY_ON_TRIGGER', 1, 'FLAG', 1666, '100.80', '97.80', '300.00', '10080.00', 'MA10', NULL, 'PROPOSED', 'p2:ALPHAFLAG:BUY_ON_TRIGGER')",
        "INSERT INTO sw_plan_line(plan_id, user_id, kind, instrument_id, setup, quantity, trigger, stop, risk_inr, position_value, trail, note, state, client_id) VALUES (2, 1, 'BUY_ON_TRIGGER', 2, 'EP', 200, '212.00', '204.50', '300.00', '10080.00', 'MA10', NULL, 'PROPOSED', 'p2:BETAEP:BUY_ON_TRIGGER')",
        "INSERT INTO sw_plan_skip(plan_id, user_id, instrument_id, symbol, reason, detail) VALUES (2, 1, 3, 'GAMMALOCK', 'LOCKED_UPPER_CIRCUIT', 'upper circuit at 52.50')",
        // the 09:31 trigger's line (id 6)
        "INSERT INTO sw_plan_line(plan_id, user_id, kind, instrument_id, setup, quantity, trigger, stop, risk_inr, position_value, trail, note, state, client_id) VALUES (3, 1, 'BUY_ON_TRIGGER', 1, 'FLAG', 1666, '100.80', '97.80', '300.00', '10080.00', 'MA10', NULL, 'PROPOSED', 'p3:ALPHAFLAG:BUY_ON_TRIGGER')",
        "INSERT INTO sw_signal(user_id, instrument_id, setup, session_date, raised_at, state, or_window_minutes, range_high, range_low, low_of_day, last_price, entry, stop, plan_line_id) VALUES (1, 1, 'FLAG', '2026-09-02', '2026-09-02T09:31:00+05:30', 'TRIGGERED', 5, '100.50', '98.00', '97.80', '100.80', '100.80', '97.80', 6)",
        "INSERT INTO sw_signal(user_id, instrument_id, setup, session_date, raised_at, state, or_window_minutes, range_high, range_low, low_of_day, last_price, entry, stop, plan_line_id) VALUES (1, 3, 'FLAG', '2026-09-02', '2026-09-02T09:20:00+05:30', 'LOCKED_UPPER_CIRCUIT', 5, '52.50', '98.00', '97.80', '52.50', NULL, NULL, NULL)",
        "INSERT INTO sw_plan_skip(plan_id, user_id, instrument_id, symbol, reason, detail) VALUES (4, 1, 5, 'EPSILON', 'TIER_FULL', '2 of 2 positions open')",
        "INSERT INTO sw_signal(user_id, instrument_id, setup, session_date, raised_at, state, or_window_minutes, range_high, range_low, low_of_day, last_price, entry, stop, plan_line_id) VALUES (1, 5, 'FLAG', '2026-09-02', '2026-09-02T09:35:00+05:30', 'TRIGGERED', 5, '300.00', '98.00', '97.80', '300.50', '300.50', '295.00', NULL)",
        "INSERT INTO sw_session(user_id, session_date, mode, monitor_ran, signals, confirms, fills, manage_actions) VALUES (1, '2026-09-02', 'DRY_RUN', 0, 3, 1, 1, 0)",
        // SW11B / SW15: a filing and an earnings date for ALPHAFLAG, the day's provisional market row, a finished scan
        "INSERT INTO sw_catalyst(user_id, instrument_id, headline, published_at, url, source, earnings_date) VALUES (1, 1, 'Board meeting outcome', '2026-09-01T18:30:00+05:30', 'https://nse.example/a.pdf', 'NSE_ANNOUNCEMENT', NULL)",
        "INSERT INTO sw_catalyst(user_id, instrument_id, headline, published_at, url, source, earnings_date) VALUES (1, 1, 'Results', NULL, 'https://nse.example/cal', 'NSE_EVENT_CALENDAR', '2026-09-10')",
        "INSERT INTO sw_market_daily(user_id, date, constituent_count, gate, exposure_level, max_open_positions, max_exposure_pct, new_entries_allowed, detail, provisional) VALUES (1, '2026-09-02', 40, 'GREEN', 2, 6, '75.00', 1, '{\"scan\": {\"scanned_at\": \"2026-09-02T09:38:00+05:30\"}}', 1)",
        "INSERT INTO sw_scan_run(user_id, requested_at, started_at, finished_at, session_date, provisional, status, detail) VALUES (1, '2026-09-02T09:36:00+05:30', '2026-09-02T09:36:05+05:30', '2026-09-02T09:38:00+05:30', '2026-09-02', 1, 'DONE', '{\"source\": \"desk\", \"funnel\": {\"liquid\": 412, \"candidates\": {\"FLAG\": 3, \"EP\": 1}}}')",
    ] {
        exec(&mut conn, sql).await;
    }
    let now = at("2026-09-02T09:40:00+05:30");
    PgSwingStore {
        conn,
        user_id: 1,
        schema: String::new(),
        broker_account_id: Some(1),
        live_level: None,
        settings: crate::settings::Settings::defaults().unwrap(),
        clock: std::sync::Arc::new(crate::clock::FixedClock::new(now)),
    }
}

fn flags() -> ViewFlags {
    ViewFlags {
        dry_run: true,
        execution_enabled: false,
        monitor_enabled: false,
        token: Rec::of([("present", Val::Bool(false))]),
    }
}

fn json_of(v: &Rec) -> serde_json::Value {
    let text = crate::wire::orjson_dumps(&PyValue::Map(jsonable_rec(v))).unwrap();
    serde_json::from_str(&text).unwrap()
}

#[tokio::test]
async fn the_morning_at_0940_renders_every_panel_as_the_spec_says() {
    let dir = tempfile::tempdir().unwrap();
    let mut store = morning_store(dir.path()).await;
    let v = build_view(&mut store, at("2026-09-02T09:40:00+05:30"), &flags())
        .await
        .unwrap();
    let j = json_of(&v);
    assert_eq!(j["available"], true);
    assert_eq!(j["poll_ms"], 5000);
    // Triggers newest first: EPSILON 09:35 (skipped), ALPHAFLAG 09:31 (a line), GAMMALOCK 09:20.
    let t = j["triggers"].as_array().unwrap();
    let symbols: Vec<&str> = t.iter().map(|x| x["symbol"].as_str().unwrap()).collect();
    assert_eq!(symbols, ["EPSILON", "ALPHAFLAG", "GAMMALOCK"]);
    assert_eq!(t[0]["skip"]["reason"], "TIER_FULL");
    assert_eq!(t[0]["confirmable"], false);
    assert_eq!(t[1]["confirmable"], true);
    assert_eq!(t[1]["line"]["countdown"], "21:00");
    assert_eq!(t[1]["line"]["label"], "SWING BUY ALPHAFLAG x1666 @ 100.80");
    assert_eq!(t[1]["text"], "5-min ORH 100.50 broken at 100.80");
    assert_eq!(t[2]["locked"], true);
    assert_eq!(t[2]["line"], serde_json::Value::Null);
    assert_eq!(t[2]["skip"], serde_json::Value::Null);
    // The plan: exits first (SELL then RAISE), then the buys; the held name's buy has no button.
    let m = &j["plans"]["morning"];
    assert_eq!(m["source"], "MORNING");
    assert_eq!(m["countdown"], "2:00");
    let exits: Vec<&str> = m["exits"]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| x["kind"].as_str().unwrap())
        .collect();
    assert_eq!(exits, ["SELL_AT_OPEN", "RAISE_GTT_STOP"]);
    assert_eq!(m["exits"][0]["confirmable"], true);
    assert_eq!(m["exits"][1]["confirmable"], true);
    assert_eq!(m["buys"][0]["trigger"], "100.80");
    assert_eq!(m["buys"][0]["confirmable"], true);
    assert_eq!(m["buys"][0]["pct_of_sleeve"], "1.01");
    assert_eq!(m["buys"][1]["why_not"], "already held (300 open)");
    assert_eq!(m["skips"][0]["detail"], "upper circuit at 52.50");
    assert_eq!(m["line_count"], 4);
    assert_eq!(m["exits"][0]["catalyst"], serde_json::Value::Null);
    // Last night's preview is shown, expired, with its line and no button.
    let p = &j["plans"]["preview"];
    assert_eq!(p["expired"], true);
    assert_eq!(p["exits"][0]["why_not"], "plan expired");
    // The book: the naked one first, counted; the armed one carries its GTT id.
    let book = j["book"]["positions"].as_array().unwrap();
    assert_eq!(book[0]["symbol"], "BETAEP");
    assert_eq!(book[0]["naked"], true);
    assert_eq!(book[0]["value_inr"], "63150.00");
    assert_eq!(book[1]["gtt_id"], "G-44");
    assert_eq!(j["book"]["naked_count"], 1);
    let manage = j["book"]["manage"].as_array().unwrap();
    assert_eq!(manage.len(), 3);
    assert!(
        manage
            .iter()
            .all(|x| x["label"].as_str().unwrap().starts_with("SWING "))
    );
    // The status bar: simulated, the monitor off, the session's counters.
    let st = &j["status"];
    assert_eq!(st["simulated"], true);
    assert_eq!(
        st["mode_label"],
        "SIMULATED \u{2014} every confirm journals simulated=true (DRY_RUN=true)"
    );
    assert_eq!(st["monitor"]["state"], "not enabled");
    assert_eq!(st["session"]["signals"], 3);
    assert_eq!(st["session"]["recorded"], true);
    // A9: a paper confirm is full size, so the header shows the countdown at full risk.
    assert_eq!(j["sleeve"]["risk_multiplier"], "1");
    assert_eq!(j["sleeve"]["half_risk"], false);
    assert_eq!(
        j["sleeve"]["first_live_header"],
        "first live sessions: 5 left \u{b7} risk 0.500%"
    );
    assert_eq!(j["fingerprint"].as_str().unwrap().len(), 16);
}

#[tokio::test]
async fn the_header_says_half_risk_only_when_a_confirm_would_be_real() {
    let dir = tempfile::tempdir().unwrap();
    let mut store = morning_store(dir.path()).await;
    let live = ViewFlags {
        dry_run: false,
        execution_enabled: true,
        ..flags()
    };
    let v = build_view(&mut store, at("2026-09-02T09:40:00+05:30"), &live)
        .await
        .unwrap();
    let j = json_of(&v);
    assert_eq!(j["status"]["simulated"], false);
    assert_eq!(
        j["status"]["mode_label"],
        "LIVE \u{2014} confirms place real orders"
    );
    assert_eq!(j["sleeve"]["risk_multiplier"], "0.5");
    assert_eq!(j["sleeve"]["half_risk"], true);
    assert_eq!(
        j["sleeve"]["first_live_header"],
        "first live sessions: 5 left \u{b7} risk 0.250%"
    );
}

#[tokio::test]
async fn the_fingerprint_moves_when_a_line_moves_and_the_plan_expires() {
    let dir = tempfile::tempdir().unwrap();
    let mut store = morning_store(dir.path()).await;
    let now = at("2026-09-02T09:40:00+05:30");
    let first = build_view(&mut store, now, &flags()).await.unwrap();
    let again = build_view(&mut store, now, &flags()).await.unwrap();
    assert_eq!(first.val("fingerprint"), again.val("fingerprint"));
    store.set_line(4, "CONFIRMED", None, None).await.unwrap();
    let moved = build_view(&mut store, now, &flags()).await.unwrap();
    assert_ne!(first.val("fingerprint"), moved.val("fingerprint"));
    // After the morning plan's 09:42 expiry its buys lose the button but stay on the page.
    let later = build_view(&mut store, at("2026-09-02T09:43:00+05:30"), &flags())
        .await
        .unwrap();
    let j = json_of(&later);
    assert_eq!(j["plans"]["morning"]["expired"], true);
    assert_eq!(j["plans"]["morning"]["countdown"], "0:00");
    assert_eq!(j["plans"]["morning"]["buys"][1]["why_not"], "plan expired");
}

#[tokio::test]
async fn yesterdays_morning_plan_is_not_todays() {
    let dir = tempfile::tempdir().unwrap();
    let mut store = morning_store(dir.path()).await;
    let v = build_view(&mut store, at("2026-09-03T09:40:00+05:30"), &flags())
        .await
        .unwrap();
    let j = json_of(&v);
    assert_eq!(j["plans"]["morning"], serde_json::Value::Null);
    assert_eq!(j["triggers"].as_array().unwrap().len(), 0);
    assert_eq!(j["status"]["session"]["recorded"], false);
}

#[tokio::test]
async fn a_store_without_the_sw_tables_is_an_error_the_page_turns_into_a_banner() {
    let dir = tempfile::tempdir().unwrap();
    let conn = crate::storage::Db::connect_sqlite_path(&dir.path().join("empty.db"))
        .await
        .unwrap();
    let mut store = PgSwingStore {
        conn,
        user_id: 1,
        schema: String::new(),
        broker_account_id: Some(1),
        live_level: None,
        settings: crate::settings::Settings::defaults().unwrap(),
        clock: std::sync::Arc::new(crate::clock::FixedClock::new(at(
            "2026-09-02T09:40:00+05:30",
        ))),
    };
    let err = build_view(&mut store, at("2026-09-02T09:40:00+05:30"), &flags())
        .await
        .unwrap_err();
    assert!(store_error_text(&err).contains("no such table: sw_config"));
}

/// Prints the morning view's `/swing/data` bytes for a side-by-side check against Python's
/// `build_view` over the same rows (`cargo test ... dump_morning_json -- --ignored`).
#[tokio::test]
#[ignore = "a diagnostic: prints the view for a manual diff against Python"]
#[allow(clippy::print_stdout)]
async fn dump_morning_json() {
    let dir = tempfile::tempdir().unwrap();
    let mut store = morning_store(dir.path()).await;
    for t in [
        "2026-09-02T09:40:00+05:30",
        "2026-09-02T09:43:00+05:30",
        "2026-09-02T09:10:30+05:30",
    ] {
        let v = build_view(&mut store, at(t), &flags()).await.unwrap();
        let text = crate::wire::orjson_dumps(&PyValue::Map(jsonable_rec(&v))).unwrap();
        println!("JSON {text}");
    }
}

/// Prints `swing.html` rendered over the morning view (asset_v "X") for a manual diff against
/// Jinja2 (`cargo test ... dump_morning_html -- --ignored`).
#[tokio::test]
#[ignore = "a diagnostic: prints the page for a manual diff against Python"]
#[allow(clippy::print_stdout)]
async fn dump_morning_html() {
    let dir = tempfile::tempdir().unwrap();
    let mut store = morning_store(dir.path()).await;
    let templates_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../kite-momentum-rebalancer/app/templates");
    let mut templates = crate::templates::Templates::new(&templates_dir, "X", false);
    crate::lanes::register_templates(&mut templates);
    for t in ["2026-09-02T09:40:00+05:30", "2026-09-02T09:10:30+05:30"] {
        let v = build_view(&mut store, at(t), &flags()).await.unwrap();
        let ctx = minijinja::Value::from_pairs(vec![
            (
                "request".to_owned(),
                minijinja::Value::from_pairs(vec![(
                    "url".to_owned(),
                    minijinja::Value::from_pairs(vec![(
                        "path".to_owned(),
                        minijinja::Value::from("/swing"),
                    )]),
                )]),
            ),
            ("v".to_owned(), template_value(&Val::Map(v))),
        ]);
        let html = templates.render("swing.html", ctx).unwrap();
        println!("HTML-BEGIN\n{html}\nHTML-END");
    }
}

/// Review: `held = {p["instrument_id"]: p for p in positions}` keeps the LAST open row for an
/// instrument (the schema has no unique index that forbids two), so the line's `position` and
/// its "already held (n open)" sentence name that one, not the first.
#[test]
fn two_open_rows_for_one_name_the_last_one_is_held_like_a_python_dict() {
    let plan = crate::lanes::swing::store::PlanRow {
        id: 1,
        plan_id: "p".to_owned(),
        as_of: None,
        source: "MORNING".to_owned(),
        built_at: None,
        expires_at: Some(at("2026-08-19T10:00:00+05:30")),
        gate: "GREEN".to_owned(),
        exposure_level: 3,
        total_risk_inr: None,
        total_new_exposure_inr: None,
    };
    let config = crate::lanes::swing::store::SleeveConfig {
        sleeve_capital_inr: d("100000"),
        risk_per_trade_pct: d("0.5"),
        max_position_pct: d("20"),
        max_open_positions: 5,
        first_live_sessions_left: 0,
        exposure_level: 3,
        present: true,
    };
    let mut first = position(6, Some("94"));
    first.id = 1;
    let mut second = position(9, Some("93"));
    second.id = 2;
    let buy = line("BUY_ON_TRIGGER", "PROPOSED", 10, Some("95"));
    let v = line_view(
        &buy,
        &plan,
        at("2026-08-19T09:40:00+05:30"),
        &config,
        &[first, second],
    );
    assert_eq!(s(&v, "why_not"), "already held (9 open)");
}
