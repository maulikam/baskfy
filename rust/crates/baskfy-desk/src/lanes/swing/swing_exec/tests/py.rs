//! `tests/test_swing_execute.py`, every test under its Python name (lower-cased), each
//! asserting what Python asserts. Where Python uses the `gw` fixture the gateway here is the
//! REAL one ([`super::real::Rig`]: guards, risk, rate limits, idempotency and the journal file
//! over a broker that explodes); where it uses `live` (`RecordingGateway`) it is the parent
//! module's `FakeGateway` under live gates. [`every_python_test_has_a_rust_port`] reads the
//! Python file and fails on a test that is not listed in [`PORTED`].

use super::real::{Rig, RigOptions, exec, fill_val, float, int, rearm, run_line, st, text};
use std::sync::Arc;

use super::*;
use crate::lanes::exec_core::risk::RiskConfig;

/// `NOW.date()`.
fn today() -> NaiveDate {
    now().date_naive()
}

fn pos(store: &MemoryStore, id: i64) -> &PositionRow {
    &store.book.positions[&id]
}

fn sell_line(store: &mut MemoryStore, plan: &str, qty: i64, note: &str) -> i64 {
    store.add_line_with(plan, |l| {
        "SELL_AT_OPEN".clone_into(&mut l.kind);
        l.quantity = qty;
        l.trigger = None;
        l.stop = None;
        note.clone_into(l.note.get_or_insert_with(String::new));
    })
}

fn raise_line(store: &mut MemoryStore, plan: &str, stop: &str, note: &str) -> i64 {
    store.add_line_with(plan, |l| {
        "RAISE_GTT_STOP".clone_into(&mut l.kind);
        l.quantity = 0;
        l.trigger = None;
        l.stop = Some(d(stop));
        note.clone_into(l.note.get_or_insert_with(String::new));
    })
}

fn deletes(gw: &FakeGateway) -> Vec<DeleteGtt> {
    gw.calls()
        .into_iter()
        .filter_map(|c| match c {
            Call::Delete(x) => Some(x),
            _ => None,
        })
        .collect()
}

fn cancels(gw: &FakeGateway) -> Vec<CancelOrder> {
    gw.calls()
        .into_iter()
        .filter_map(|c| match c {
            Call::Cancel(x) => Some(x),
            _ => None,
        })
        .collect()
}

fn refused(r: Result<ExecOutcome, ExecError>) -> HttpRefusalView {
    match r {
        Err(ExecError::Http(h)) => HttpRefusalView {
            status: h.status,
            detail: h.detail,
        },
        other => panic!("expected an HTTPException, got {other:?}"),
    }
}

struct HttpRefusalView {
    status: u16,
    detail: String,
}

async fn exec_live(
    store: &mut MemoryStore,
    gw: &FakeGateway,
    plan: &str,
    line: i64,
    orders: Option<&dyn OrderSource>,
) -> (ExecOutcome, FakeClock) {
    let clock = FakeClock::default();
    let out = execute_live(store, gw, plan, line, orders, &clock).await;
    (out, clock)
}

async fn exec_fake(
    store: &mut MemoryStore,
    gw: &FakeGateway,
    plan: &str,
    line: i64,
    last_price: Option<Decimal>,
) -> ExecOutcome {
    let clock = FakeClock::default();
    execute_full(
        store,
        gw,
        plan,
        line,
        "true",
        now(),
        last_price,
        None,
        &clock,
    )
    .await
    .unwrap()
}

fn update(order_id: &str, status: &str, filled: i64, average: &str) -> OrderUpdate {
    OrderUpdate {
        order_id: order_id.to_owned(),
        status: status.to_owned(),
        filled_quantity: filled,
        average_price: d(average),
    }
}

fn at(h: u32, m: u32) -> DateTime<FixedOffset> {
    ist().with_ymd_and_hms(2026, 9, 2, h, m, 0).unwrap()
}

/// The module's code with its comments removed (`_code_only`): Rust's `//` and `///` lines.
fn code_only(source: &str) -> String {
    source
        .lines()
        .map(|l| l.split_once("//").map_or(l, |(code, _)| code))
        .collect::<Vec<_>>()
        .join("\n")
}

const EXECUTE_RS: &str = include_str!("../execute.rs");

/// One function's text in `execute.rs`, from its `fn name(` to the next top-level `}`.
fn function_source(name: &str) -> String {
    let start = EXECUTE_RS
        .find(&format!("fn {name}("))
        .unwrap_or_else(|| panic!("no fn {name}"));
    let rest = &EXECUTE_RS[start..];
    let end = rest.find("\n}\n").unwrap();
    rest[..end].to_owned()
}

// --- G1: the surface ---------------------------------------------------------------------

#[test]
fn test_contract_surface_by_name() {
    let _ = x::swing_gates;
    let _ = x::execute_line;
    let _ = x::rearm_gtt;
    let _ = x::on_order_update;
    let _ = x::cutoff_open_orders;
    let _ = x::eod_gtt_sweep;
    let _ = x::EXECUTABLE_KINDS;
    let outcome = ExecOutcome {
        status: "SIMULATED".to_owned(),
        reason: String::new(),
        order: None,
        gtt: None,
        position_id: None,
        simulated: true,
        filled_quantity: 0,
    };
    let keys: Vec<String> = outcome.to_py().iter().map(|(k, _)| k.to_string()).collect();
    assert_eq!(
        keys,
        [
            "status",
            "reason",
            "order",
            "gtt",
            "position_id",
            "simulated",
            "filled_quantity"
        ]
    );
}

#[test]
fn test_swing_gates_dry_run_unless_both_flags_allow() {
    let mut st = paper();
    st.dry_run = false;
    st.swing_execution_enabled = false;
    assert!(x::swing_gates(&st).dry_run);
    st.dry_run = true;
    st.swing_execution_enabled = true;
    assert!(x::swing_gates(&st).dry_run);
    st.dry_run = false;
    assert!(!x::swing_gates(&st).dry_run);
}

#[test]
fn test_swing_gates_never_allow_intraday_or_options() {
    let mut st = paper();
    st.intraday_enabled = true;
    st.options_enabled = true;
    let gates = x::swing_gates(&st);
    assert!(!gates.intraday_enabled && !gates.options_enabled);
}

#[tokio::test]
async fn test_build_swing_gateway_uses_swing_band_and_own_journal() {
    let settings = paper();
    let band = x::swing_stop_band(&settings).unwrap();
    assert_eq!(band.min_pct(), settings.swing_stop_band_min);
    assert_eq!(band.max_pct(), settings.swing_stop_band_max);
    let rig = Rig::new(&settings).await;
    assert_eq!(
        rig.core.journal_path().file_name().unwrap(),
        "swing_orders_journal.jsonl"
    );
    let desk = std::path::Path::new(crate::gateway::JOURNAL);
    let swing = x::swing_journal_path();
    assert_eq!(
        std::path::Path::new(&swing).parent(),
        desk.parent(),
        "the swing journal must sit beside the desk's"
    );
}

// --- G2: the four refusals, before anything is touched ------------------------------------

#[tokio::test]
async fn test_400_when_confirm_is_not_true() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    for confirm in ["", "false", "True", "yes"] {
        let r = run_line(
            &mut store,
            &rig,
            &rig.settings,
            &plan,
            line,
            confirm,
            now(),
            None,
        )
        .await;
        assert_eq!(refused(r).status, 400);
    }
    assert_eq!(store.line_state(line), "PROPOSED");
    assert!(store.book.sessions.is_empty());
}

#[tokio::test]
async fn test_404_unknown_plan() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    assert_eq!(
        refused(exec(&mut store, &rig, "no-such-plan", 1, None).await).status,
        404
    );
}

#[tokio::test]
async fn test_404_line_not_in_plan() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let p1 = store.add_plan();
    let p2 = store.add_plan();
    let line_in_p2 = store.add_line(&p2);
    assert_eq!(
        refused(exec(&mut store, &rig, &p1, line_in_p2, None).await).status,
        404
    );
    assert_eq!(
        refused(exec(&mut store, &rig, &p1, 999, None).await).status,
        404
    );
}

#[tokio::test]
async fn test_410_expired_plan_marks_the_line_expired() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan_built(now() - Duration::minutes(31));
    let line = store.add_line(&plan);
    assert_eq!(
        refused(exec(&mut store, &rig, &plan, line, None).await).status,
        410
    );
    assert_eq!(store.line_state(line), "EXPIRED");
    assert!(store.book.positions.is_empty() && store.book.fills.is_empty());
}

#[tokio::test]
async fn test_expired_boundary_is_thirty_minutes_exactly() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan_built(now() - Duration::minutes(30));
    let line = store.add_line(&plan);
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert_eq!(out.status, "SIMULATED");
}

#[tokio::test]
async fn test_409_when_line_is_not_proposed() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    for state in [
        "CONFIRMED",
        "SENT",
        "FILLED",
        "REJECTED",
        "SKIPPED",
        "EXPIRED",
    ] {
        let line = store.add_line_with(&plan, |l| state.clone_into(&mut l.state));
        assert_eq!(
            refused(exec(&mut store, &rig, &plan, line, None).await).status,
            409,
            "{state}"
        );
    }
}

#[tokio::test]
async fn test_409_a_re_posted_confirm_cannot_send_twice() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let first = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert_eq!(first.status, "SIMULATED");
    assert_eq!(
        refused(exec(&mut store, &rig, &plan, line, None).await).status,
        409
    );
    assert_eq!(store.book.fills.len(), 1);
    assert_eq!(store.session().1, 1);
}

// --- the BUY path -------------------------------------------------------------------------

#[tokio::test]
async fn test_buy_dry_run_fills_whole_line_and_arms_gtt_in_same_call() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert!(out.status == "SIMULATED" && out.simulated && out.reason.is_empty());
    let order = out.order.clone().unwrap();
    assert_eq!(st(&order), "DRY_RUN");
    assert_eq!(text(&order, "order_id"), format!("DRY-{plan}:ALPHA:BUY"));
    let gtt = out.gtt.clone().unwrap();
    assert_eq!(st(&gtt), gtt_status::DRY_RUN_GTT);
    assert_eq!(int(&gtt, "qty"), 100);
    assert_eq!(float(&gtt, "trigger"), 96.0);
    let pid = out.position_id.unwrap();
    let p = pos(&store, pid);
    assert_eq!((p.quantity_entered, p.quantity_open), (100, 100));
    assert_eq!(p.entry_avg, Some(d("100.00")));
    assert_eq!(
        (p.initial_stop, p.stop),
        (Some(d("96.00")), Some(d("96.00")))
    );
    assert!(p.gtt_id.as_deref().unwrap().starts_with("DRY-"));
    assert_eq!(p.gtt_trigger, Some(d("96.00")));
    assert_eq!(p.gtt_armed_at, Some(now()));
    assert!(p.state == "OPEN" && p.simulated);
    assert!(p.trail == "MA20" && p.setup == "FLAG");
    assert_eq!(store.book.fills.len(), 1);
    let fill = &store.book.fills[0];
    assert_eq!(fill_val(fill, "position_id"), &Val::Int(pid));
    assert_eq!(fill_val(fill, "side"), &Val::Text("BUY".to_owned()));
    assert_eq!(fill_val(fill, "quantity"), &Val::Int(100));
    assert_eq!(fill_val(fill, "price"), &Val::Dec(d("100.00")));
    assert_eq!(fill_val(fill, "filled_at"), &Val::Stamp(now()));
    assert_eq!(
        fill_val(fill, "journal_ref"),
        &Val::Text(text(&order, "order_id"))
    );
    assert_eq!(fill_val(fill, "simulated"), &Val::Bool(true));
    let l = &store.book.lines[&line];
    assert!(l.state == "FILLED" && l.position_id == Some(pid));
    assert_eq!(l.journal_ref, Some(text(&order, "order_id")));
    assert_eq!(store.session(), ("DRY_RUN".to_owned(), 1, 1, 0));
    assert_eq!(rig.write_calls(), 0);
}

#[tokio::test]
async fn test_buy_line_is_marked_confirmed_before_the_gateway_is_called() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert_eq!(store.line_history[0], (line, "CONFIRMED".to_owned()));
    assert_eq!(
        store.line_history.last().unwrap(),
        &(line, "FILLED".to_owned())
    );
}

#[tokio::test]
async fn test_buy_gtt_client_id_is_separate_from_order_client_id() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert!(rig.sent().await.contains_key(&format!("{plan}:ALPHA:BUY")));
    assert!(
        rig.gtt_sent()
            .await
            .contains_key(&format!("{plan}:ALPHA:GTT"))
    );
    assert_eq!(st(out.gtt.as_ref().unwrap()), gtt_status::DRY_RUN_GTT);
}

#[tokio::test]
async fn test_buy_journal_carries_order_and_gtt_lines() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert_eq!(rig.events(), ["dry_run", "gtt_dry_run"]);
}

#[tokio::test]
async fn test_buy_with_zero_quantity_is_blocked_before_the_gateway() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| l.quantity = 0);
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert!(
        out.status == "BLOCKED" && out.reason.contains("0 shares"),
        "{out:?}"
    );
    assert!(out.order.is_none() && out.gtt.is_none() && out.position_id.is_none());
    assert!(rig.sent().await.is_empty() && store.book.positions.is_empty());
    assert_eq!(store.line_state(line), "REJECTED");
    assert_eq!((store.session().1, store.session().2), (1, 0));
}

#[tokio::test]
async fn test_buy_with_stop_not_below_trigger_is_blocked_before_the_gateway() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| l.stop = Some(d("100.00")));
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert!(out.status == "BLOCKED" && out.reason.contains("not below"));
    assert!(rig.sent().await.is_empty() && store.book.positions.is_empty());
}

#[tokio::test]
async fn test_buy_for_a_name_at_the_per_name_cap_is_blocked_and_a_second_entry_is_not() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    store.add_position_with(|p| p.instrument_id = 11);
    store.add_position_with(|p| p.instrument_id = 11);
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| l.instrument_id = 11);
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert_eq!(out.status, "BLOCKED");
    assert!(out.reason.contains("ALREADY_HELD") && out.reason.contains("2 open entries"));
    assert!(rig.sent().await.is_empty());
}

#[tokio::test]
async fn test_buy_untouchable_instrument_raises_and_marks_line_rejected() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| "SGBAUG28".clone_into(&mut l.symbol));
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert_eq!(out.status, "BLOCKED");
    assert!(
        out.reason.to_lowercase().contains("protected") || out.reason.contains("SGB"),
        "{}",
        out.reason
    );
    assert_eq!(store.line_state(line), "REJECTED");
    assert!(store.book.positions.is_empty());
    assert!(rig.sent().await.is_empty());
}

#[tokio::test]
async fn test_buy_duplicate_from_gateway_is_blocked_with_no_second_position() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let first = store.add_line_with(&plan, |l| l.instrument_id = 11);
    let second = store.add_line_with(&plan, |l| l.instrument_id = 12);
    let out1 = exec(&mut store, &rig, &plan, first, None).await.unwrap();
    assert_eq!(out1.status, "SIMULATED");
    store.book.positions.clear();
    let out2 = exec(&mut store, &rig, &plan, second, None).await.unwrap();
    assert!(
        out2.status == "BLOCKED" && out2.reason.contains("DUPLICATE"),
        "{out2:?}"
    );
    assert_eq!(
        text(out2.order.as_ref().unwrap(), "order_id"),
        text(out1.order.as_ref().unwrap(), "order_id")
    );
    assert!(store.book.positions.is_empty() && store.book.fills.len() == 1);
}

#[tokio::test]
async fn test_buy_risk_blocked_is_blocked_with_the_gateway_error() {
    let rig = Rig::with(
        &paper(),
        RigOptions {
            risk: RiskConfig {
                max_position_value: 1_000.0,
                ..RiskConfig::default()
            },
            ..RigOptions::default()
        },
    )
    .await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert_eq!(out.status, "BLOCKED");
    assert!(
        out.reason.contains("RISK_BLOCKED") && out.reason.contains("> cap"),
        "{}",
        out.reason
    );
    assert!(store.book.positions.is_empty());
    assert_eq!(store.line_state(line), "REJECTED");
}

#[tokio::test]
async fn test_buy_gtt_refused_leaves_a_naked_position_and_says_so() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    // `gw._gtt_sent[f"{plan_id}:ALPHA:GTT"] = "DRY-earlier"`: the id used once already.
    let tenant = crate::gateway::TenantIds::new(1, 1).unwrap();
    rig.core
        .place_gtt_stop(&GttStop {
            symbol: "ALPHA".to_owned(),
            qty: 1,
            trigger: 90.0,
            last_price: 100.0,
            exchange: "NSE".to_owned(),
            series: None,
            client_id: Some(format!("{plan}:ALPHA:GTT")),
            tenant: Some(tenant),
            plan_tenant: Some(tenant),
            limit_fraction: None,
            fo_plan: None,
        })
        .await
        .unwrap();
    let line = store.add_line(&plan);
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert_eq!(out.status, "SIMULATED");
    assert_eq!(st(out.gtt.as_ref().unwrap()), "DUPLICATE");
    assert!(out.reason.contains("NAKED") && out.reason.contains("re-arm"));
    let p = pos(&store, out.position_id.unwrap());
    assert!(p.gtt_id.is_none() && p.quantity_open == 100);
}

// --- G5: the flag, and the broker that is never reached -----------------------------------

#[tokio::test]
async fn test_flag_off_dry_run_false_still_never_reaches_broker() {
    let mut settings = paper();
    settings.dry_run = false;
    settings.swing_execution_enabled = false;
    let rig = Rig::new(&settings).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let buy = store.add_line(&plan);
    let out = exec(&mut store, &rig, &plan, buy, None).await.unwrap();
    assert!(out.status == "SIMULATED" && out.simulated);
    assert_eq!(st(out.order.as_ref().unwrap()), "DRY_RUN");
    assert_eq!(st(out.gtt.as_ref().unwrap()), gtt_status::DRY_RUN_GTT);
    assert!(pos(&store, out.position_id.unwrap()).simulated);
    assert_eq!(
        fill_val(&store.book.fills[0], "simulated"),
        &Val::Bool(true)
    );
    assert_eq!(store.session().0, "DRY_RUN");
    assert_eq!(rig.write_calls(), 0);
}

#[tokio::test]
async fn test_flag_off_sell_and_raise_never_reach_broker() {
    let mut settings = paper();
    settings.dry_run = false;
    let rig = Rig::new(&settings).await;
    let mut store = MemoryStore::new();
    let pid = store.add_position_with(|p| p.gtt_id = Some("DRY-old".to_owned()));
    let plan = store.add_plan();
    let sell = sell_line(&mut store, &plan, 100, "PARTIAL_INTO_STRENGTH");
    let raise = raise_line(&mut store, &plan, "100.00", "BREAKEVEN_AFTER_PARTIAL");
    let s = exec(&mut store, &rig, &plan, sell, Some(d("110")))
        .await
        .unwrap();
    let r = exec(&mut store, &rig, &plan, raise, Some(d("110")))
        .await
        .unwrap();
    assert!(
        s.status == "SIMULATED" && r.status == "SIMULATED",
        "{s:?} {r:?}"
    );
    assert_eq!(pos(&store, pid).stop, Some(d("100.00")));
    assert!(
        pos(&store, pid)
            .gtt_id
            .as_deref()
            .unwrap()
            .starts_with("DRY-")
    );
    assert_eq!(rig.write_calls(), 0);
}

#[tokio::test]
async fn test_dry_run_rearm_never_reaches_broker() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let pid = store.add_position_with(|p| {
        p.gtt_id = None;
        p.gtt_trigger = None;
        p.gtt_armed_at = None;
    });
    let out = rearm(&mut store, &rig, &rig.settings, pid, "true", None)
        .await
        .unwrap();
    assert_eq!(out.status, "SIMULATED");
    assert_eq!(st(out.gtt.as_ref().unwrap()), gtt_status::DRY_RUN_GTT);
    assert!(
        pos(&store, pid)
            .gtt_id
            .as_deref()
            .unwrap()
            .starts_with("DRY-")
    );
    assert_eq!(rig.write_calls(), 0);
}

#[tokio::test]
async fn test_never_reaches_broker_across_the_whole_module_is_the_kc_contract() {
    // The order verbs are in scope by glob only: law 2's scan (`tests/non_negotiables.rs`)
    // allows their trait's name in the gateway's lane alone.
    use crate::kite::*;
    // The fake is the proof: every write it could be asked for raises.
    let rig = Rig::new(&paper()).await;
    assert!(rig.kite.place_order(&OrderParams::default()).await.is_err());
    assert!(rig.kite.delete_gtt(1).await.is_err());
    assert!(rig.kite.cancel_order("regular", "1", None).await.is_err());
    assert!(
        rig.kite
            .modify_order("regular", "1", &PyDict::new())
            .await
            .is_err()
    );
}

// --- G3 / G4: SELL_AT_OPEN ----------------------------------------------------------------

#[tokio::test]
async fn test_sell_for_more_than_quantity_open_is_blocked() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let pid = store.add_position_with(|p| {
        p.quantity_entered = 300;
        p.quantity_open = 200;
        "PARTIAL".clone_into(&mut p.state);
    });
    let plan = store.add_plan();
    let line = sell_line(&mut store, &plan, 201, "");
    let out = exec(&mut store, &rig, &plan, line, Some(d("105")))
        .await
        .unwrap();
    assert!(out.status == "BLOCKED" && out.reason.contains("exceeds the 200 open"));
    assert_eq!(out.position_id, Some(pid));
    assert!(rig.sent().await.is_empty() && store.book.fills.is_empty());
    assert_eq!(pos(&store, pid).quantity_open, 200);
}

#[tokio::test]
async fn test_sell_for_a_symbol_not_in_sw_position_is_blocked() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| {
        "SELL_AT_OPEN".clone_into(&mut l.kind);
        "WEEKLYNAME".clone_into(&mut l.symbol);
        l.instrument_id = 77;
        l.quantity = 10;
        l.trigger = None;
        l.stop = None;
    });
    let out = exec(&mut store, &rig, &plan, line, Some(d("50")))
        .await
        .unwrap();
    assert_eq!(out.status, "BLOCKED");
    assert_eq!(out.reason, "WEEKLYNAME: not a swing position");
    assert!(rig.sent().await.is_empty());
}

#[tokio::test]
async fn test_sell_when_quantity_open_already_zero_is_blocked() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    store.add_position_with(|p| {
        p.quantity_open = 0;
        "CLOSED".clone_into(&mut p.state);
    });
    let plan = store.add_plan();
    let line = sell_line(&mut store, &plan, 1, "");
    let out = exec(&mut store, &rig, &plan, line, Some(d("50")))
        .await
        .unwrap();
    assert!(out.status == "BLOCKED" && out.reason.contains("not a swing position"));
}

#[tokio::test]
async fn test_sell_with_zero_quantity_is_blocked() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    store.add_position_with(|_| {});
    let plan = store.add_plan();
    let line = sell_line(&mut store, &plan, 0, "");
    let out = exec(&mut store, &rig, &plan, line, Some(d("50")))
        .await
        .unwrap();
    assert!(out.status == "BLOCKED" && out.reason.contains("0 shares"));
    assert!(rig.sent().await.is_empty());
}

#[tokio::test]
async fn test_sell_without_last_price_is_blocked_not_guessed() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    store.add_position_with(|_| {});
    let plan = store.add_plan();
    let line = sell_line(&mut store, &plan, 100, "");
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert!(out.status == "BLOCKED" && out.reason.contains("last_price"));
    assert!(rig.sent().await.is_empty() && store.book.fills.is_empty());
}

#[tokio::test]
async fn test_sell_partial_resizes_gtt_for_the_remainder() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let pid = store.add_position_with(|p| {
        p.gtt_id = Some("DRY-buy".to_owned());
        p.quantity_entered = 300;
        p.quantity_open = 300;
    });
    let plan = store.add_plan();
    let line = sell_line(&mut store, &plan, 100, "PARTIAL_INTO_STRENGTH");
    let out = exec(&mut store, &rig, &plan, line, Some(d("110.00")))
        .await
        .unwrap();
    assert!(
        out.status == "SIMULATED" && out.reason.is_empty(),
        "{out:?}"
    );
    assert_eq!(st(out.order.as_ref().unwrap()), "DRY_RUN");
    let gtt = out.gtt.as_ref().unwrap();
    assert_eq!(st(gtt), gtt_status::DRY_RUN_GTT);
    assert_eq!(int(gtt, "qty"), 200);
    assert_eq!(float(gtt, "trigger"), 96.0);
    let p = pos(&store, pid);
    assert!(p.quantity_open == 200 && p.state == "PARTIAL");
    assert!(p.partial_done && p.partial_date == Some(today()));
    assert_eq!(p.exit_avg, Some(d("110.00")));
    assert_eq!(
        p.gtt_id.as_deref(),
        Some(format!("DRY-{plan}:ALPHA:SELL:GTT").as_str())
    );
    assert_eq!(p.gtt_trigger, Some(d("96.00")));
    assert!(p.closed_on.is_none() && p.r_multiple.is_none());
    let last = store.book.fills.last().unwrap();
    assert_eq!(fill_val(last, "side"), &Val::Text("SELL".to_owned()));
    assert_eq!(fill_val(last, "quantity"), &Val::Int(100));
    assert_eq!(fill_val(last, "price"), &Val::Dec(d("110.00")));
    assert_eq!(fill_val(last, "simulated"), &Val::Bool(true));
    assert_eq!(store.line_state(line), "FILLED");
    assert_eq!(store.session().2, 1);
}

#[tokio::test]
async fn test_sell_partial_gtt_resize_goes_through_gateway_delete_for_a_real_id() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let pid = store.add_position_with(|p| p.gtt_id = Some("123456".to_owned()));
    let plan = store.add_plan();
    let line = sell_line(&mut store, &plan, 100, "");
    let out = exec(&mut store, &rig, &plan, line, Some(d("105")))
        .await
        .unwrap();
    assert_eq!(out.status, "SIMULATED");
    assert_eq!(
        rig.events(),
        ["dry_run", "gtt_dry_run_delete", "gtt_dry_run"]
    );
    assert!(
        pos(&store, pid)
            .gtt_id
            .as_deref()
            .unwrap()
            .starts_with("DRY-")
    );
}

#[tokio::test]
async fn test_sell_and_raise_for_the_same_name_in_one_plan_both_arm() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let pid = store.add_position_with(|p| {
        p.gtt_id = Some("DRY-buy".to_owned());
        p.quantity_entered = 300;
        p.quantity_open = 300;
    });
    let plan = store.add_plan();
    let sell = sell_line(&mut store, &plan, 100, "PARTIAL_INTO_STRENGTH");
    let raise = raise_line(&mut store, &plan, "100.00", "BREAKEVEN_AFTER_PARTIAL");
    let s = exec(&mut store, &rig, &plan, sell, Some(d("104")))
        .await
        .unwrap();
    let r = exec(&mut store, &rig, &plan, raise, Some(d("104")))
        .await
        .unwrap();
    assert_eq!(s.status, "SIMULATED");
    assert_eq!(int(s.gtt.as_ref().unwrap(), "qty"), 200);
    assert_eq!(float(s.gtt.as_ref().unwrap(), "trigger"), 96.0);
    assert_eq!(r.status, "SIMULATED");
    assert_eq!(int(r.gtt.as_ref().unwrap(), "qty"), 200);
    assert_eq!(float(r.gtt.as_ref().unwrap(), "trigger"), 100.0);
    let p = pos(&store, pid);
    assert!(p.quantity_open == 200 && p.stop == Some(d("100.00")));
    assert_eq!(
        p.gtt_id.as_deref(),
        Some(format!("DRY-{plan}:ALPHA:RAISE:GTT").as_str())
    );
}

#[tokio::test]
async fn test_sell_close_out_writes_r_multiple_pnl_exit_avg_close_reason() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let pid = store.add_position_with(|p| {
        p.gtt_id = Some("DRY-buy".to_owned());
        p.quantity_entered = 300;
        p.quantity_open = 300;
        p.entry_avg = Some(d("100.00"));
        p.initial_stop = Some(d("96.00"));
        p.stop = Some(d("100.00"));
    });
    let plan = store.add_plan();
    let line = sell_line(&mut store, &plan, 300, "CLOSE_BELOW_TRAIL_MA");
    let out = exec(&mut store, &rig, &plan, line, Some(d("109.00")))
        .await
        .unwrap();
    assert!(out.status == "SIMULATED" && out.reason.is_empty());
    assert_eq!(
        st(out.gtt.as_ref().unwrap()),
        gtt_status::DRY_RUN_GTT_DELETE
    );
    let p = pos(&store, pid);
    assert!(p.state == "CLOSED" && p.quantity_open == 0);
    assert_eq!(p.closed_on, Some(today()));
    assert_eq!(p.exit_avg, Some(d("109.00")));
    assert_eq!(p.close_reason.as_deref(), Some("CLOSE_BELOW_TRAIL_MA"));
    assert_eq!(p.r_multiple, Some(d("2.25")));
    assert_eq!(p.pnl_inr, Some(d("2700.00")));
    assert!(p.gtt_id.is_none() && p.gtt_trigger.is_none() && p.gtt_armed_at.is_none());
    // `r_multiple(entry, stop, exit)` and `ClosedTrade(...).pnl_inr`, the book's own numbers.
    use crate::lanes::swing::rules::{closed_pnl, r_multiple};
    assert_eq!(p.r_multiple, r_multiple(d("100"), d("96"), d("109")).ok());
    assert_eq!(p.pnl_inr, Some(closed_pnl(d("100"), d("109"), 300)));
}

#[tokio::test]
async fn test_close_after_partial_exit_avg_is_share_weighted() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let pid = store.add_position_with(|p| {
        p.gtt_id = Some("DRY-buy".to_owned());
        p.quantity_entered = 300;
        p.quantity_open = 300;
    });
    let plan = store.add_plan();
    let partial = sell_line(&mut store, &plan, 100, "PARTIAL_INTO_STRENGTH");
    exec(&mut store, &rig, &plan, partial, Some(d("110.00")))
        .await
        .unwrap();
    let plan2 = store.add_plan();
    let rest = sell_line(&mut store, &plan2, 200, "CLOSE_BELOW_TRAIL_MA");
    let out = exec(&mut store, &rig, &plan2, rest, Some(d("104.00")))
        .await
        .unwrap();
    assert_eq!(out.status, "SIMULATED");
    let p = pos(&store, pid);
    assert_eq!(p.exit_avg, Some(d("106.00")));
    assert_eq!(p.r_multiple, Some(d("1.50")));
    assert_eq!(p.pnl_inr, Some(d("1800.00")));
    assert!(p.state == "CLOSED" && p.close_reason.as_deref() == Some("CLOSE_BELOW_TRAIL_MA"));
    let sides: Vec<&Val> = store
        .book
        .fills
        .iter()
        .map(|f| fill_val(f, "side"))
        .collect();
    assert_eq!(
        sides,
        [&Val::Text("SELL".to_owned()), &Val::Text("SELL".to_owned())]
    );
}

#[tokio::test]
async fn test_close_with_unknown_note_is_manual_and_a_loss_has_negative_r() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let pid = store.add_position_with(|p| {
        p.gtt_id = None;
        p.quantity_entered = 100;
        p.quantity_open = 100;
        p.entry_avg = Some(d("100.00"));
        p.initial_stop = Some(d("96.00"));
    });
    let plan = store.add_plan();
    let line = sell_line(&mut store, &plan, 100, "operator clicked sell");
    let out = exec(&mut store, &rig, &plan, line, Some(d("97.00")))
        .await
        .unwrap();
    assert!(out.status == "SIMULATED" && out.gtt.is_none());
    let p = pos(&store, pid);
    assert_eq!(p.close_reason.as_deref(), Some("MANUAL"));
    assert_eq!(
        (p.r_multiple, p.pnl_inr),
        (Some(d("-0.75")), Some(d("-300.00")))
    );
}

#[tokio::test]
async fn test_sell_partial_on_a_naked_position_arms_a_gtt_for_the_remainder() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let pid = store.add_position_with(|p| {
        p.gtt_id = None;
        p.gtt_trigger = None;
        p.gtt_armed_at = None;
    });
    let plan = store.add_plan();
    let line = sell_line(&mut store, &plan, 100, "");
    let out = exec(&mut store, &rig, &plan, line, Some(d("110")))
        .await
        .unwrap();
    assert_eq!(out.status, "SIMULATED");
    assert_eq!(st(out.gtt.as_ref().unwrap()), gtt_status::DRY_RUN_GTT);
    assert_eq!(int(out.gtt.as_ref().unwrap(), "qty"), 200);
    assert!(pos(&store, pid).gtt_id.is_some());
}

#[tokio::test]
async fn test_sell_partial_when_price_is_below_stop_leaves_naked_with_reason() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let pid = store.add_position_with(|p| {
        p.gtt_id = Some("DRY-buy".to_owned());
        p.stop = Some(d("96.00"));
    });
    let plan = store.add_plan();
    let line = sell_line(&mut store, &plan, 100, "");
    let out = exec(&mut store, &rig, &plan, line, Some(d("95.00")))
        .await
        .unwrap();
    assert!(
        out.status == "SIMULATED" && out.reason.contains("NAKED"),
        "{out:?}"
    );
    assert_eq!(st(out.gtt.as_ref().unwrap()), "BLOCKED");
    let p = pos(&store, pid);
    assert!(p.quantity_open == 200 && p.gtt_id.is_none());
}

// --- G3 / G4: RAISE_GTT_STOP --------------------------------------------------------------

#[tokio::test]
async fn test_raise_below_resting_stop_is_blocked() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let pid = store.add_position_with(|p| {
        p.stop = Some(d("98.00"));
        p.gtt_id = Some("DRY-buy".to_owned());
    });
    let plan = store.add_plan();
    let line = raise_line(&mut store, &plan, "97.00", "");
    let out = exec(&mut store, &rig, &plan, line, Some(d("110")))
        .await
        .unwrap();
    assert!(out.status == "BLOCKED" && out.reason.contains("never falls"));
    assert_eq!(pos(&store, pid).stop, Some(d("98.00")));
    assert_eq!(pos(&store, pid).gtt_id.as_deref(), Some("DRY-buy"));
    assert!(rig.gtt_sent().await.is_empty());
}

#[tokio::test]
async fn test_raise_equal_to_resting_stop_is_blocked() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    store.add_position_with(|p| p.stop = Some(d("98.00")));
    let plan = store.add_plan();
    let line = raise_line(&mut store, &plan, "98.00", "");
    let out = exec(&mut store, &rig, &plan, line, Some(d("110")))
        .await
        .unwrap();
    assert_eq!(out.status, "BLOCKED");
}

#[tokio::test]
async fn test_raise_for_symbol_not_held_is_blocked() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = raise_line(&mut store, &plan, "98.00", "");
    let out = exec(&mut store, &rig, &plan, line, Some(d("110")))
        .await
        .unwrap();
    assert!(out.status == "BLOCKED" && out.reason.contains("not a swing position"));
}

#[tokio::test]
async fn test_raise_without_last_price_is_blocked() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    store.add_position_with(|_| {});
    let plan = store.add_plan();
    let line = raise_line(&mut store, &plan, "100.00", "");
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert!(out.status == "BLOCKED" && out.reason.contains("last_price"));
}

#[tokio::test]
async fn test_raise_at_or_above_last_price_is_blocked_before_cancelling() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let pid = store.add_position_with(|p| p.gtt_id = Some("DRY-buy".to_owned()));
    let plan = store.add_plan();
    let line = raise_line(&mut store, &plan, "100.00", "");
    let out = exec(&mut store, &rig, &plan, line, Some(d("100.00")))
        .await
        .unwrap();
    assert!(out.status == "BLOCKED" && out.reason.contains("fire at once"));
    assert_eq!(pos(&store, pid).gtt_id.as_deref(), Some("DRY-buy"));
}

#[tokio::test]
async fn test_raise_gtt_stop_replaces_the_trigger_and_updates_the_position() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let pid = store.add_position_with(|p| {
        p.gtt_id = Some("123".to_owned());
        p.gtt_trigger = Some(d("96.00"));
        p.stop = Some(d("96.00"));
    });
    let plan = store.add_plan();
    let line = raise_line(&mut store, &plan, "100.00", "BREAKEVEN_AT_R");
    let out = exec(&mut store, &rig, &plan, line, Some(d("104.00")))
        .await
        .unwrap();
    assert!(
        out.status == "SIMULATED" && out.reason.is_empty(),
        "{out:?}"
    );
    assert!(out.order.is_none());
    assert_eq!(st(out.gtt.as_ref().unwrap()), gtt_status::DRY_RUN_GTT);
    assert_eq!(int(out.gtt.as_ref().unwrap(), "qty"), 300);
    let p = pos(&store, pid);
    assert!(p.stop == Some(d("100.00")) && p.gtt_trigger == Some(d("100.00")));
    assert_eq!(
        p.gtt_id.as_deref(),
        Some(format!("DRY-{plan}:ALPHA:RAISE:GTT").as_str())
    );
    assert_eq!(p.gtt_armed_at, Some(now()));
    assert_eq!(
        p.initial_stop,
        Some(d("96.00")),
        "the R denominator never moves"
    );
    assert_eq!(rig.events(), ["gtt_dry_run_delete", "gtt_dry_run"]);
    assert_eq!(store.line_state(line), "FILLED");
    assert_eq!(store.session(), ("DRY_RUN".to_owned(), 1, 0, 1));
}

#[tokio::test]
async fn test_raise_when_cancel_is_refused_places_no_second_gtt() {
    let rig = Rig::with(
        &paper(),
        RigOptions {
            risk: RiskConfig {
                max_daily_loss: 10.0,
                ..RiskConfig::default()
            },
            ..RigOptions::default()
        },
    )
    .await;
    rig.risk.on_pnl(-100.0).await.unwrap(); // trips the kill switch
    let mut store = MemoryStore::new();
    let pid = store.add_position_with(|p| {
        p.gtt_id = Some("123".to_owned());
        p.stop = Some(d("96.00"));
    });
    let plan = store.add_plan();
    let line = raise_line(&mut store, &plan, "100.00", "");
    let out = exec(&mut store, &rig, &plan, line, Some(d("104")))
        .await
        .unwrap();
    assert!(
        out.status == "BLOCKED" && out.reason.contains("KILL SWITCH"),
        "{out:?}"
    );
    assert!(rig.gtt_sent().await.is_empty());
    assert_eq!(pos(&store, pid).stop, Some(d("96.00")));
    assert_eq!(pos(&store, pid).gtt_id.as_deref(), Some("123"));
}

// --- rearm_gtt ---------------------------------------------------------------------------

#[tokio::test]
async fn test_rearm_gtt_400_without_confirm() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let pid = store.add_position_with(|p| p.gtt_id = None);
    let r = rearm(&mut store, &rig, &rig.settings, pid, "false", None).await;
    assert_eq!(refused(r).status, 400);
}

#[tokio::test]
async fn test_rearm_gtt_404_unknown_position() {
    let rig = Rig::new(&paper()).await;
    let r = rearm(
        &mut MemoryStore::new(),
        &rig,
        &rig.settings,
        9,
        "true",
        None,
    )
    .await;
    assert_eq!(refused(r).status, 404);
}

#[tokio::test]
async fn test_rearm_gtt_arms_naked_position_for_quantity_open() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let pid = store.add_position_with(|p| {
        p.gtt_id = None;
        p.gtt_trigger = None;
        p.gtt_armed_at = None;
        p.quantity_entered = 300;
        p.quantity_open = 200;
        "PARTIAL".clone_into(&mut p.state);
        p.stop = Some(d("97.00"));
    });
    let out = rearm(&mut store, &rig, &rig.settings, pid, "true", None)
        .await
        .unwrap();
    assert!(out.status == "SIMULATED" && out.position_id == Some(pid) && out.order.is_none());
    let gtt = out.gtt.as_ref().unwrap();
    assert_eq!(st(gtt), gtt_status::DRY_RUN_GTT);
    assert_eq!((int(gtt, "qty"), float(gtt, "trigger")), (200, 97.0));
    let p = pos(&store, pid);
    assert!(p.gtt_id.as_deref().unwrap().starts_with("DRY-"));
    assert_eq!(p.gtt_trigger, Some(d("97.00")));
    assert_eq!((p.gtt_armed_at, p.stop), (Some(now()), Some(d("97.00"))));
    assert_eq!(store.session().3, 1);
}

#[tokio::test]
async fn test_rearm_gtt_on_a_covered_position_is_blocked() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let pid = store.add_position_with(|p| p.gtt_id = Some("DRY-buy".to_owned()));
    let out = rearm(&mut store, &rig, &rig.settings, pid, "true", None)
        .await
        .unwrap();
    assert!(out.status == "BLOCKED" && out.reason.contains("already resting"));
    assert!(rig.gtt_sent().await.is_empty());
}

#[tokio::test]
async fn test_rearm_gtt_on_a_closed_position_is_blocked() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let pid = store.add_position_with(|p| {
        p.gtt_id = None;
        p.quantity_open = 0;
        "CLOSED".clone_into(&mut p.state);
    });
    let out = rearm(&mut store, &rig, &rig.settings, pid, "true", None)
        .await
        .unwrap();
    assert!(out.status == "BLOCKED" && out.reason.contains("nothing is open"));
}

#[tokio::test]
async fn test_rearm_gtt_breakeven_stop_needs_a_last_price() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let pid = store.add_position_with(|p| {
        p.gtt_id = None;
        p.stop = Some(d("100.00"));
        p.entry_avg = Some(d("100.00"));
    });
    let out = rearm(&mut store, &rig, &rig.settings, pid, "true", None)
        .await
        .unwrap();
    assert!(out.status == "BLOCKED" && out.reason.contains("last_price"));
    let out = rearm(&mut store, &rig, &rig.settings, pid, "true", Some(d("103")))
        .await
        .unwrap();
    assert_eq!(out.status, "SIMULATED");
}

// --- G6: the first live sessions (SW7.2, amended by SW10.5 / A9) ----------------------------

#[test]
fn test_risk_multiplier_for_is_half_only_for_a_real_order_with_sessions_left() {
    let mut cfg = MemoryStore::new().book.config.unwrap();
    cfg.first_live_sessions_left = 5;
    assert_eq!(x::risk_multiplier_for(&cfg, false), d("0.5"));
    assert_eq!(x::risk_multiplier_for(&cfg, true), Decimal::ONE);
    let mut zero = cfg.clone();
    zero.first_live_sessions_left = 0;
    assert_eq!(x::risk_multiplier_for(&zero, false), Decimal::ONE);
    assert_eq!(
        x::sizing_config(&cfg, d("0.5")).sizing.risk_per_trade_pct,
        0.25
    );
    assert_eq!(
        x::sizing_config(&cfg, Decimal::ONE)
            .sizing
            .risk_per_trade_pct,
        0.5
    );
}

fn half_risk_store() -> (MemoryStore, String, i64) {
    let mut store = MemoryStore::with(5, 3, MarketGate::Green, d("1000000"));
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| {
        l.quantity = 1666;
        l.trigger = Some(d("100.00"));
        l.stop = Some(d("97.00"));
    });
    (store, plan, line)
}

#[tokio::test]
async fn test_first_live_risk_multiplier_halves_the_risk_at_plan_time_not_the_quantity() {
    let live_gw = FakeGateway::new(&live());
    let (mut store, plan, line) = half_risk_store();
    let orders = ScriptedOrders::new(&[("COMPLETE", 833, "100.20")]);
    let (out, _) = exec_live(&mut store, &live_gw, &plan, line, Some(&orders)).await;
    assert!(
        out.status == "FILLED" && out.filled_quantity == 833,
        "{out:?}"
    );
    assert_eq!(live_gw.places()[0].qty, 833);
    let l = &store.book.lines[&line];
    assert!(l.quantity == 833 && l.note.as_deref().unwrap().contains("re-sized"));
    let p = pos(&store, out.position_id.unwrap());
    assert!(p.quantity_entered == 833 && p.half_risk);
    assert_eq!(live_gw.gtts()[0].qty, 833);
}

#[tokio::test]
async fn test_first_live_risk_multiplier_is_applied_once_not_twice() {
    let live_gw = FakeGateway::new(&live());
    let (mut store, plan, line) = half_risk_store();
    assert_eq!(
        store
            .session_context(today())
            .await
            .unwrap()
            .first_live_sessions_left,
        5
    );
    let orders = ScriptedOrders::new(&[("COMPLETE", 833, "100.20")]);
    exec_live(&mut store, &live_gw, &plan, line, Some(&orders)).await;
    assert_eq!(live_gw.places()[0].qty, 833);
    let code = code_only(&function_source("buy"));
    assert!(
        code.contains("sizing_config(&config, Decimal::ONE)"),
        "the multiplier is applied in entries_now, once"
    );
}

#[tokio::test]
async fn test_first_live_never_halves_a_simulated_confirm() {
    let rig = Rig::new(&paper()).await;
    let (mut store, plan, line) = half_risk_store();
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert!(out.simulated);
    let p = pos(&store, out.position_id.unwrap());
    assert!(p.quantity_entered == 1666 && !p.half_risk);
    assert_eq!(rig.sent_quantities(), [1666]);
}

#[tokio::test]
async fn test_first_live_at_zero_sends_full_size_and_tags_nothing() {
    let live_gw = FakeGateway::new(&live());
    let mut store = MemoryStore::with(0, 3, MarketGate::Green, d("1000000"));
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| {
        l.quantity = 1666;
        l.trigger = Some(d("100.00"));
        l.stop = Some(d("97.00"));
    });
    let orders = ScriptedOrders::new(&[("COMPLETE", 1666, "100.10")]);
    let (out, _) = exec_live(&mut store, &live_gw, &plan, line, Some(&orders)).await;
    assert_eq!(live_gw.places()[0].qty, 1666);
    assert!(!pos(&store, out.position_id.unwrap()).half_risk);
}

#[tokio::test]
async fn test_first_live_countdown_is_never_moved_by_a_request() {
    let live_gw = FakeGateway::new(&live());
    let mut store = MemoryStore::with(5, 3, MarketGate::Green, d("1000000"));
    let plan = store.add_plan();
    let a = store.add_line_with(&plan, |l| l.quantity = 100);
    let b = store.add_line_with(&plan, |l| {
        l.quantity = 100;
        "BETA".clone_into(&mut l.symbol);
        l.instrument_id = 12;
    });
    for line in [a, b] {
        let orders = ScriptedOrders::new(&[("COMPLETE", 50, "100")]);
        exec_live(&mut store, &live_gw, &plan, line, Some(&orders)).await;
    }
    assert_eq!(store.config().await.unwrap().first_live_sessions_left, 5);
    // The store protocol in Rust has no writer for the countdown at all; the module's code
    // names none either.
    let code = code_only(EXECUTE_RS);
    assert!(!code.contains("set_first_live_sessions_left"));
    assert!(!code.contains("first_live_quantity") && !code.contains("FIRST_LIVE_COUNTED"));
}

#[tokio::test]
async fn test_first_live_sell_and_raise_are_never_halved() {
    let live_gw = FakeGateway::new(&live());
    let mut store = MemoryStore::with(5, 3, MarketGate::Green, d("1000000"));
    let pid = store.add_position_with(|p| {
        p.gtt_id = Some("123".to_owned());
        p.stop = Some(d("96.00"));
    });
    let plan = store.add_plan();
    let sell = sell_line(&mut store, &plan, 100, "");
    let raise = raise_line(&mut store, &plan, "100.00", "");
    exec_fake(&mut store, &live_gw, &plan, sell, Some(d("104"))).await;
    exec_fake(&mut store, &live_gw, &plan, raise, Some(d("104"))).await;
    assert_eq!(live_gw.places()[0].qty, 100);
    assert_eq!(live_gw.gtts()[0].qty, 300);
    assert_eq!(pos(&store, pid).quantity_open, 300);
}

// --- SW10.5 / A8: the marketable limit, the poll, the late fill, the cutoff ------------------

#[tokio::test]
async fn test_the_entry_cap_is_still_a8s_and_is_sent_as_market_protection() {
    let live_gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| {
        l.trigger = Some(d("100.80"));
        l.stop = Some(d("97.80"));
    });
    store.range_highs.insert(line, d("100.00"));
    let orders = ScriptedOrders::new(&[("COMPLETE", 100, "100.9")]);
    let clock = FakeClock::default();
    execute_full(
        &mut store,
        &live_gw,
        &plan,
        line,
        "true",
        now(),
        Some(d("100.80")),
        Some(&orders),
        &clock,
    )
    .await
    .unwrap();
    let order = &live_gw.places()[0];
    assert_eq!(order.order_type, "MARKET");
    assert_eq!(order.price, None, "a MARKET order carries no price");
    assert_eq!(order.market_protection, Some(0.44));
}

#[tokio::test]
async fn test_a_price_already_past_the_cap_is_refused_not_chased() {
    let live_gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| {
        l.trigger = Some(d("100.80"));
        l.stop = Some(d("97.80"));
    });
    store.range_highs.insert(line, d("100.00"));
    let out = exec_fake(&mut store, &live_gw, &plan, line, Some(d("101.25"))).await;
    assert_eq!(out.status, "BLOCKED");
    assert!(out.reason.contains("run past the entry cap"));
    assert!(live_gw.places().is_empty(), "nothing reached the broker");
}

#[tokio::test]
async fn test_a_live_buy_without_a_price_is_blocked_never_guessed() {
    let live_gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let out = exec_fake(&mut store, &live_gw, &plan, line, None).await;
    assert!(out.status == "BLOCKED" && out.reason.contains("no live price"));
    assert!(live_gw.places().is_empty());
}

#[tokio::test]
async fn test_the_dry_run_journal_records_the_shape_that_would_have_been_sent() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    exec(&mut store, &rig, &plan, line, None).await.unwrap();
    let rows = rig.journal();
    assert_eq!(rows[0]["event"], "dry_run");
    assert_eq!(rows[0]["order_type"], "MARKET");
    assert!(rows[0]["price"].is_null());
    assert_eq!(
        rows[0]["reference_price"].as_f64(),
        Some(100.0),
        "the drill falls back to the trigger"
    );
}

#[tokio::test]
async fn test_the_request_polls_the_order_at_most_ten_seconds_at_two_a_second() {
    let live_gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let orders = ScriptedOrders::new(&[("OPEN", 0, "0")]);
    let (out, clock) = exec_live(&mut store, &live_gw, &plan, line, Some(&orders)).await;
    assert!(out.status == "SENT" && out.position_id.is_none() && out.filled_quantity == 0);
    assert!((19..=21).contains(&orders.polls()), "{}", orders.polls());
    assert!(clock.slept.lock().unwrap().iter().all(|s| *s == 0.5));
    assert!(clock.now() <= 10.0);
    assert_eq!(store.line_state(line), "SENT");
    assert!(live_gw.gtts().is_empty());
}

#[tokio::test]
async fn test_the_poll_stops_early_on_complete() {
    let live_gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| l.quantity = 100);
    let orders = ScriptedOrders::new(&[
        ("OPEN", 0, "0"),
        ("OPEN", 40, "100.10"),
        ("COMPLETE", 100, "100.15"),
    ]);
    let (out, clock) = exec_live(&mut store, &live_gw, &plan, line, Some(&orders)).await;
    assert!(out.status == "FILLED" && orders.polls() == 3 && clock.now() == 1.0);
}

#[tokio::test]
async fn test_complete_writes_the_position_the_fill_and_the_gtt_in_the_same_request() {
    let live_gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let orders = ScriptedOrders::new(&[("COMPLETE", 100, "100.15")]);
    let (out, _) = exec_live(&mut store, &live_gw, &plan, line, Some(&orders)).await;
    assert!(out.status == "FILLED" && !out.simulated && out.filled_quantity == 100);
    let pid = out.position_id.unwrap();
    let p = pos(&store, pid);
    assert_eq!((p.quantity_entered, p.quantity_open), (100, 100));
    assert!(p.entry_avg == Some(d("100.1500")) && !p.simulated);
    assert_eq!(p.gtt_id.as_deref(), Some("4242"));
    assert_eq!(p.gtt_trigger, Some(d("96.00")));
    let fills = &store.book.fills;
    assert_eq!(fills.len(), 1);
    assert_eq!(fill_val(&fills[0], "position_id"), &Val::Int(pid));
    assert_eq!(fill_val(&fills[0], "side"), &Val::Text("BUY".to_owned()));
    assert_eq!(fill_val(&fills[0], "quantity"), &Val::Int(100));
    assert_eq!(fill_val(&fills[0], "price"), &Val::Dec(d("100.15")));
    assert_eq!(fill_val(&fills[0], "filled_at"), &Val::Stamp(now()));
    assert_eq!(
        fill_val(&fills[0], "journal_ref"),
        &Val::Text("ORD1".to_owned())
    );
    assert_eq!(fill_val(&fills[0], "simulated"), &Val::Bool(false));
    let g = &live_gw.gtts()[0];
    assert!(g.qty == 100 && g.last_price == 100.15);
    assert_eq!(store.line_state(line), "FILLED");
    assert_eq!(store.book.lines[&line].position_id, Some(pid));
    assert_eq!(store.session().2, 1);
}

#[tokio::test]
async fn test_a_partial_fill_is_sent_with_the_filled_quantity_and_a_gtt_for_exactly_it() {
    let live_gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let orders = ScriptedOrders::new(&[("OPEN", 40, "100.10")]);
    let (out, _) = exec_live(&mut store, &live_gw, &plan, line, Some(&orders)).await;
    assert!(out.status == "SENT" && out.filled_quantity == 40 && out.position_id.is_some());
    let p = pos(&store, out.position_id.unwrap());
    assert_eq!((p.quantity_entered, p.quantity_open), (40, 40));
    assert_eq!(live_gw.gtts()[0].qty, 40);
    let l = &store.book.lines[&line];
    assert!(l.state == "SENT" && l.position_id == out.position_id);
    assert_eq!(l.journal_ref.as_deref(), Some("ORD1"));
    assert_eq!(store.session().2, 1);
}

#[tokio::test]
async fn test_partial_then_complete_ends_with_one_gtt_covering_exactly_the_filled_quantity() {
    let live_gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let orders = ScriptedOrders::new(&[("OPEN", 40, "100.00")]);
    let (out, _) = exec_live(&mut store, &live_gw, &plan, line, Some(&orders)).await;
    let later = x::on_order_update(
        &mut store,
        &live_gw,
        &live_gw.settings,
        &update("ORD1", "COMPLETE", 100, "100.30"),
        now() + Duration::minutes(20),
    )
    .await
    .unwrap()
    .unwrap();
    assert!(later.status == "FILLED" && later.filled_quantity == 100);
    let p = pos(&store, out.position_id.unwrap());
    assert_eq!((p.quantity_entered, p.quantity_open), (100, 100));
    assert_eq!(p.entry_avg, Some(d("100.3000")));
    let qtys: Vec<&Val> = store
        .book
        .fills
        .iter()
        .map(|f| fill_val(f, "quantity"))
        .collect();
    assert_eq!(qtys, [&Val::Int(40), &Val::Int(60)]);
    assert_eq!(
        fill_val(&store.book.fills[1], "price"),
        &Val::Dec(d("100.5000"))
    );
    assert_eq!(live_gw.gtts().len(), 1, "never a second GTT");
    let modifies = live_gw.modifies();
    assert_eq!(modifies.iter().map(|m| m.qty).collect::<Vec<_>>(), [100]);
    assert!(modifies[0].gtt_id == 4242 && modifies[0].trigger == 96.0);
    assert_eq!(store.line_state(line), "FILLED");
}

#[tokio::test]
async fn test_on_order_update_is_idempotent_on_a_repeated_postback() {
    let live_gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let orders = ScriptedOrders::new(&[("OPEN", 40, "100.00")]);
    exec_live(&mut store, &live_gw, &plan, line, Some(&orders)).await;
    let u = update("ORD1", "OPEN", 70, "100.20");
    let settings = live_gw.settings.clone();
    let first = x::on_order_update(&mut store, &live_gw, &settings, &u, now())
        .await
        .unwrap()
        .unwrap();
    let second = x::on_order_update(&mut store, &live_gw, &settings, &u, now())
        .await
        .unwrap()
        .unwrap();
    let stale = update("ORD1", "OPEN", 55, "100.20");
    let third = x::on_order_update(&mut store, &live_gw, &settings, &stale, now())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(first.filled_quantity, 70);
    assert!(second.filled_quantity == 70 && third.filled_quantity == 70);
    let qtys: Vec<&Val> = store
        .book
        .fills
        .iter()
        .map(|f| fill_val(f, "quantity"))
        .collect();
    assert_eq!(
        qtys,
        [&Val::Int(40), &Val::Int(30)],
        "the repeat and the stale wrote nothing"
    );
    assert_eq!(
        live_gw.modifies().iter().map(|m| m.qty).collect::<Vec<_>>(),
        [70]
    );
    let p = pos(&store, first.position_id.unwrap());
    assert_eq!((p.quantity_entered, p.quantity_open), (70, 70));
    assert_eq!(store.line_state(line), "SENT");
}

#[tokio::test]
async fn test_on_order_update_creates_the_position_when_the_request_saw_nothing_filled() {
    let live_gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let orders = ScriptedOrders::new(&[("OPEN", 0, "0")]);
    exec_live(&mut store, &live_gw, &plan, line, Some(&orders)).await;
    assert!(store.book.positions.is_empty());
    let out = x::on_order_update(
        &mut store,
        &live_gw,
        &live_gw.settings,
        &update("ORD1", "OPEN", 25, "100.05"),
        now(),
    )
    .await
    .unwrap()
    .unwrap();
    assert!(out.position_id.is_some() && out.filled_quantity == 25);
    assert!(live_gw.gtts()[0].qty == 25 && live_gw.modifies().is_empty());
    assert_eq!(store.book.lines[&line].position_id, out.position_id);
    assert_eq!(store.session().2, 1);
}

#[tokio::test]
async fn test_the_postback_modifies_the_gtt_never_places_a_second_one() {
    let live_gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let orders = ScriptedOrders::new(&[("OPEN", 10, "100.00")]);
    exec_live(&mut store, &live_gw, &plan, line, Some(&orders)).await;
    for filled in [20, 30, 100] {
        let status = if filled < 100 { "OPEN" } else { "COMPLETE" };
        x::on_order_update(
            &mut store,
            &live_gw,
            &live_gw.settings,
            &update("ORD1", status, filled, "100.00"),
            now(),
        )
        .await
        .unwrap();
    }
    assert_eq!(live_gw.gtts().len(), 1);
    let modifies = live_gw.modifies();
    assert_eq!(
        modifies.iter().map(|m| m.qty).collect::<Vec<_>>(),
        [20, 30, 100]
    );
    assert!(modifies.iter().all(|m| m.gtt_id == 4242));
    assert_eq!(pos(&store, 1).quantity_open, 100);
}

#[tokio::test]
async fn test_the_postback_never_modifies_the_gtt_above_the_filled_quantity() {
    let live_gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let orders = ScriptedOrders::new(&[("OPEN", 10, "100.00")]);
    exec_live(&mut store, &live_gw, &plan, line, Some(&orders)).await;
    for filled in [33, 57, 90] {
        x::on_order_update(
            &mut store,
            &live_gw,
            &live_gw.settings,
            &update("ORD1", "OPEN", filled, "100"),
            now(),
        )
        .await
        .unwrap();
        let last = live_gw.modifies().last().unwrap().qty;
        assert!(last <= filled);
        assert_eq!(last, pos(&store, 1).quantity_open);
    }
}

#[tokio::test]
async fn test_a_postback_for_an_unknown_or_weekly_order_is_ignored() {
    let live_gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let out = x::on_order_update(
        &mut store,
        &live_gw,
        &live_gw.settings,
        &update("WEEKLY-1", "COMPLETE", 10, "1"),
        now(),
    )
    .await
    .unwrap();
    assert!(out.is_none());
    assert!(store.book.positions.is_empty() && live_gw.modifies().is_empty());
    assert!(live_gw.gtts().is_empty());
}

#[tokio::test]
async fn test_a_postback_arms_a_naked_partial_for_the_whole() {
    // "The first arm was refused (naked)". Python nulls the stored id after a recorded arm,
    // which its `RecordingGateway` (no idempotency map) lets the postback re-arm under the same
    // client id; this fake keeps the gateway's map, so the first arm is refused for real.
    let mut live_gw = FakeGateway::new(&live());
    live_gw.refuse_gtt = true;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let orders = ScriptedOrders::new(&[("OPEN", 40, "100.00")]);
    exec_live(&mut store, &live_gw, &plan, line, Some(&orders)).await;
    live_gw.refuse_gtt = false;
    store
        .update_position(1, vec![("gtt_id", Val::Null), ("gtt_trigger", Val::Null)])
        .await
        .unwrap();
    x::on_order_update(
        &mut store,
        &live_gw,
        &live_gw.settings,
        &update("ORD1", "OPEN", 70, "100"),
        now(),
    )
    .await
    .unwrap();
    let gtts = live_gw.gtts();
    assert!(gtts.len() == 2 && gtts[1].qty == 70 && live_gw.modifies().is_empty());
    assert_eq!(pos(&store, 1).gtt_id.as_deref(), Some("4243"));
}

#[tokio::test]
async fn test_the_cutoff_cancels_the_open_remainder_and_leaves_the_gtt_untouched() {
    let live_gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let orders = ScriptedOrders::new(&[("OPEN", 40, "100.00")]);
    exec_live(&mut store, &live_gw, &plan, line, Some(&orders)).await;
    let later = ScriptedOrders::new(&[("OPEN", 40, "100.00")]);
    let report = x::cutoff_open_orders(
        &mut store,
        &live_gw,
        &live_gw.settings,
        Some(&later),
        at(10, 45),
    )
    .await
    .unwrap();
    assert_eq!(
        (report.reconciled, report.cancelled, report.cancel_failed),
        (1, 1, 0)
    );
    let c = cancels(&live_gw);
    assert_eq!(c.len(), 1);
    assert_eq!(
        (c[0].order_id.as_str(), c[0].symbol.as_str()),
        ("ORD1", "ALPHA")
    );
    assert_eq!(
        c[0].client_id.as_deref(),
        Some(format!("{plan}:ALPHA:CANCEL").as_str())
    );
    assert!(c[0].tenant.is_some() && c[0].plan_tenant.is_some());
    assert!(live_gw.modifies().is_empty() && live_gw.gtts().len() == 1);
    assert!(deletes(&live_gw).is_empty());
    assert_eq!(pos(&store, 1).quantity_open, 40);
    assert_eq!(pos(&store, 1).gtt_id.as_deref(), Some("4242"));
    assert_eq!(store.line_state(line), "FILLED");
    assert!(
        store.book.lines[&line]
            .note
            .as_deref()
            .unwrap()
            .contains("remaining 60 cancelled")
    );
}

#[tokio::test]
async fn test_the_cutoff_applies_a_fill_that_arrived_since_before_cancelling() {
    let live_gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let orders = ScriptedOrders::new(&[("OPEN", 40, "100.00")]);
    exec_live(&mut store, &live_gw, &plan, line, Some(&orders)).await;
    let later = ScriptedOrders::new(&[("OPEN", 75, "100.00")]);
    let report = x::cutoff_open_orders(
        &mut store,
        &live_gw,
        &live_gw.settings,
        Some(&later),
        at(10, 45),
    )
    .await
    .unwrap();
    assert_eq!(report.cancelled, 1);
    assert_eq!(pos(&store, 1).quantity_open, 75);
    assert_eq!(
        live_gw.modifies().iter().map(|m| m.qty).collect::<Vec<_>>(),
        [75]
    );
    assert!(
        store.book.lines[&line]
            .note
            .as_deref()
            .unwrap()
            .contains("remaining 25 cancelled")
    );
}

#[tokio::test]
async fn test_the_cutoff_expires_a_line_nothing_filled_and_reports_a_refused_cancel() {
    let live_gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let a = store.add_line(&plan);
    let b = store.add_line_with(&plan, |l| {
        "BETA".clone_into(&mut l.symbol);
        l.instrument_id = 12;
    });
    for line in [a, b] {
        let orders = ScriptedOrders::new(&[("OPEN", 0, "0")]);
        exec_live(&mut store, &live_gw, &plan, line, Some(&orders)).await;
    }
    let mut refusing = FakeGateway::new(&live());
    refusing.refuse_cancel = Some("BETA".to_owned());
    let later = ScriptedOrders::new(&[("OPEN", 0, "0")]);
    let report = x::cutoff_open_orders(
        &mut store,
        &refusing,
        &refusing.settings,
        Some(&later),
        at(10, 45),
    )
    .await
    .unwrap();
    assert_eq!((report.cancelled, report.cancel_failed), (1, 1));
    assert!(store.line_state(a) == "EXPIRED" && store.book.positions.is_empty());
    assert_eq!(store.line_state(b), "SENT");
    assert!(
        store.book.lines[&b]
            .note
            .as_deref()
            .unwrap()
            .contains("cancel refused")
    );
}

#[tokio::test]
async fn test_the_cutoff_leaves_a_completed_order_alone() {
    let live_gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let orders = ScriptedOrders::new(&[("OPEN", 40, "100.00")]);
    exec_live(&mut store, &live_gw, &plan, line, Some(&orders)).await;
    let later = ScriptedOrders::new(&[("COMPLETE", 100, "100.10")]);
    let report = x::cutoff_open_orders(
        &mut store,
        &live_gw,
        &live_gw.settings,
        Some(&later),
        at(10, 45),
    )
    .await
    .unwrap();
    assert!(report.cancelled == 0 && cancels(&live_gw).is_empty());
    assert_eq!(store.line_state(line), "FILLED");
    assert_eq!(pos(&store, 1).quantity_open, 100);
}

fn add_pending(store: &mut MemoryStore, plan: &str, edit: impl FnOnce(&mut LineRow)) -> i64 {
    store.add_line_with(plan, |l| {
        "PENDING_RANGE".clone_into(&mut l.kind);
        l.quantity = 0;
        l.stop = None;
        l.trigger = Some(d("112.50"));
        l.risk_inr = Some(Decimal::ZERO);
        l.position_value = Some(Decimal::ZERO);
        l.setup = Some("EP".to_owned());
        "GAPCO".clone_into(&mut l.symbol);
        l.instrument_id = 19;
        l.note = Some("EP score 46.20; live gap; slot reserved".to_owned());
        edit(l);
    })
}

#[tokio::test]
async fn test_the_cutoff_frees_the_unclaimed_pending_range_slots() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let pending = add_pending(&mut store, &plan, |_| {});
    let spent = add_pending(&mut store, &plan, |l| {
        "DONECO".clone_into(&mut l.symbol);
        l.instrument_id = 20;
        "SKIPPED".clone_into(&mut l.state);
    });
    let report = x::cutoff_open_orders(&mut store, &rig, &rig.settings, None, at(10, 45))
        .await
        .unwrap();
    assert_eq!(report.slots_freed, 1);
    assert!(store.line_state(pending) == "EXPIRED" && store.line_state(spent) == "SKIPPED");
    let again = x::cutoff_open_orders(&mut store, &rig, &rig.settings, None, at(10, 46))
        .await
        .unwrap();
    assert_eq!(again.slots_freed, 0);
}

#[tokio::test]
async fn test_a_dry_run_fill_follows_the_same_path_with_simulated_true() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let orders = ScriptedOrders::new(&[("OPEN", 0, "0")]);
    let clock = FakeClock::default();
    let cx = ExecContext {
        settings: &rig.settings,
        orders: Some(&orders),
        timer: &clock,
    };
    let out = x::execute_line(&mut store, &rig, &cx, &plan, line, "true", now(), None)
        .await
        .unwrap();
    assert!(out.status == "SIMULATED" && out.simulated && out.filled_quantity == 100);
    assert!(orders.polls() == 0 && clock.now() == 0.0);
    let p = pos(&store, out.position_id.unwrap());
    assert!(p.simulated);
    assert_eq!(
        fill_val(&store.book.fills[0], "simulated"),
        &Val::Bool(true)
    );
    assert!(p.entry_avg == Some(d("100.0000")) && p.quantity_open == 100);
    assert_eq!(rig.events(), ["dry_run", "gtt_dry_run"]);
}

#[tokio::test]
async fn test_a_rejected_order_with_nothing_filled_is_rejected_and_writes_no_book() {
    let live_gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let orders = ScriptedOrders::new(&[("REJECTED", 0, "0")]);
    let (out, _) = exec_live(&mut store, &live_gw, &plan, line, Some(&orders)).await;
    assert!(out.status == "REJECTED" && store.book.positions.is_empty());
    assert!(live_gw.gtts().is_empty());
    assert_eq!(store.line_state(line), "REJECTED");
}

#[tokio::test]
async fn test_the_1515_sweep_stub_re_arms_naked_positions_given_a_price() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let naked = store.add_position_with(|p| {
        p.gtt_id = None;
        p.gtt_trigger = None;
        p.stop = Some(d("96.00"));
    });
    let covered = store.add_position_with(|p| {
        "BETA".clone_into(&mut p.symbol);
        p.instrument_id = 12;
        p.gtt_id = Some("DRY-x".to_owned());
    });
    let prices = std::collections::BTreeMap::from([("ALPHA".to_owned(), d("104"))]);
    let outcomes = x::eod_gtt_sweep(&mut store, &rig, &rig.settings, at(15, 15), &prices)
        .await
        .unwrap();
    assert_eq!(
        outcomes.iter().map(|o| o.position_id).collect::<Vec<_>>(),
        [Some(naked)]
    );
    assert!(pos(&store, naked).gtt_id.is_some());
    assert_eq!(pos(&store, covered).gtt_id.as_deref(), Some("DRY-x"));
}

// --- SW10.5 / A7: a PENDING_RANGE line can never reach /swing/execute ------------------------

#[tokio::test]
async fn test_pending_range_is_refused_400_by_execute_line_before_the_lock() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = add_pending(&mut store, &plan, |_| {});
    let r = refused(exec(&mut store, &rig, &plan, line, None).await);
    assert!(r.status == 400 && r.detail.contains("PENDING_RANGE"));
    assert!(store.locks.is_empty() && store.line_history.is_empty());
    assert_eq!(store.line_state(line), "PROPOSED");
    assert!(rig.events().is_empty() && store.book.positions.is_empty());
}

#[tokio::test]
async fn test_pending_range_is_refused_under_live_gates_too() {
    let live_gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = add_pending(&mut store, &plan, |_| {});
    let clock = FakeClock::default();
    let r = execute_full(
        &mut store,
        &live_gw,
        &plan,
        line,
        "true",
        now(),
        None,
        None,
        &clock,
    )
    .await;
    assert_eq!(refused(r).status, 400);
    assert!(live_gw.places().is_empty() && live_gw.gtts().is_empty());
}

#[test]
fn test_pending_range_is_not_in_the_executable_kinds_at_source_level() {
    use baskfy_core::swing::plan::{EXECUTABLE_KINDS as CORE_KINDS, LineKind};
    assert!(!EXECUTABLE_KINDS.contains(&PENDING_RANGE));
    assert_eq!(
        EXECUTABLE_KINDS,
        ["BUY_ON_TRIGGER", "SELL_AT_OPEN", "RAISE_GTT_STOP"]
    );
    let core: Vec<String> = CORE_KINDS
        .iter()
        .map(|k| {
            serde_json::to_value(k)
                .unwrap()
                .as_str()
                .unwrap()
                .to_owned()
        })
        .collect();
    assert_eq!(core, EXECUTABLE_KINDS);
    assert!(!CORE_KINDS.contains(&LineKind::PendingRange));
    let code = code_only(EXECUTE_RS);
    // The refusal reads the set, and no branch of the module executes the kind.
    assert!(code.contains("!EXECUTABLE_KINDS.contains(&line.kind.as_str())"));
    assert!(!code.contains("== PENDING_RANGE") && !code.contains("PENDING_RANGE =>"));
}

// --- G8: the code names no broker method ---------------------------------------------------

#[test]
fn test_module_code_never_names_a_broker_method() {
    let code = code_only(EXECUTE_RS);
    for banned in [
        "kc.place_order",
        "kc.place_gtt",
        "kc.delete_gtt",
        "kiteconnect",
        "kite_client",
        "kc.instruments",
        concat!("Kite", "Writes"),
    ] {
        assert!(!code.contains(banned), "{banned}");
    }
    assert!(code.contains(".place(PlaceOrder"));
    assert!(code.contains(".place_gtt_stop(GttStop"));
    assert!(code.contains("gw.delete_gtt(DeleteGtt"));
}

#[tokio::test]
async fn test_prices_are_float_at_the_gateway_boundary() {
    let live_gw = FakeGateway::new(&live());
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| {
        l.trigger = Some(d("123.45"));
        l.stop = Some(d("119.90"));
    });
    exec_fake(&mut store, &live_gw, &plan, line, Some(d("123.45"))).await;
    let sent = &live_gw.places()[0];
    assert_eq!(sent.price, None);
    // `f64` by type: the protection and the risk layer's reference cross as floats.
    assert_eq!(sent.market_protection, Some(0.48));
    assert_eq!(sent.reference_price, Some(123.45));
}

#[tokio::test]
async fn test_prices_stay_decimal_in_the_store() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| {
        l.trigger = Some(d("123.45"));
        l.stop = Some(d("119.90"));
    });
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    let p = pos(&store, out.position_id.unwrap());
    // `Decimal` by type for entry_avg, initial_stop, stop and gtt_trigger.
    assert!(p.entry_avg.is_some() && p.initial_stop.is_some() && p.stop.is_some());
    assert_eq!(p.gtt_trigger, Some(d("119.90")));
    assert!(matches!(
        fill_val(&store.book.fills[0], "price"),
        Val::Dec(_)
    ));
}

#[tokio::test]
async fn test_live_gates_with_an_exploding_kc_are_rejected_never_a_position() {
    let rig = Rig::new(&live()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let out = exec(&mut store, &rig, &plan, line, Some(d("100.00")))
        .await
        .unwrap();
    assert_eq!(out.status, "REJECTED", "{out:?}");
    assert_eq!(st(out.order.as_ref().unwrap()), "ERROR");
    assert!(out.reason.contains("reached the broker"), "{}", out.reason);
    assert!(store.book.positions.is_empty() && store.book.fills.is_empty());
    assert_eq!(store.line_state(line), "REJECTED");
}

// --- edge cases hunted on the third pass ----------------------------------------------------

#[tokio::test]
async fn test_gtt_outside_the_swing_band_is_journalled_not_refused() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| l.stop = Some(d("99.70")));
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert_eq!(out.status, "SIMULATED");
    assert_eq!(st(out.gtt.as_ref().unwrap()), gtt_status::DRY_RUN_GTT);
    assert!(pos(&store, out.position_id.unwrap()).gtt_id.is_some());
    assert!(!rig.events().iter().any(|e| e == "gtt_band_warning"));
}

#[tokio::test]
async fn test_naive_timestamps_are_read_as_ist() {
    // Rust carries no naive timestamps: a store's stamps and `now` arrive with an offset. The
    // property Python pins — the plan, the fill and the session are read on IST's clock — is
    // asserted with every stamp handed over at UTC.
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let utc = FixedOffset::east_opt(0).unwrap();
    let plan = store.add_plan_built((now() - Duration::minutes(5)).with_timezone(&utc));
    let line = store.add_line(&plan);
    let at_utc = now().with_timezone(&utc);
    let out = run_line(
        &mut store,
        &rig,
        &rig.settings,
        &plan,
        line,
        "true",
        at_utc,
        None,
    )
    .await
    .unwrap();
    assert_eq!(out.status, "SIMULATED");
    assert_eq!(
        pos(&store, out.position_id.unwrap()).entry_date,
        Some(today())
    );
    assert_eq!(
        store.book.sessions.keys().copied().collect::<Vec<_>>(),
        [today()]
    );
}

#[tokio::test]
async fn test_session_day_is_the_ist_date() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let late = ist().with_ymd_and_hms(2026, 9, 3, 0, 30, 0).unwrap();
    let plan = store.add_plan_built(late - Duration::minutes(1));
    let line = store.add_line(&plan);
    let utc = FixedOffset::east_opt(0).unwrap();
    run_line(
        &mut store,
        &rig,
        &rig.settings,
        &plan,
        line,
        "true",
        late.with_timezone(&utc),
        None,
    )
    .await
    .unwrap();
    assert_eq!(
        store.book.sessions.keys().copied().collect::<Vec<_>>(),
        [NaiveDate::from_ymd_opt(2026, 9, 3).unwrap()]
    );
}

#[tokio::test]
async fn test_line_with_no_quantity_is_blocked() {
    // `quantity=None` reads as `int(None or 0)`; the Rust row carries the 0.
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| l.quantity = 0);
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert_eq!(out.status, "BLOCKED");
    assert!(rig.sent().await.is_empty());
}

#[tokio::test]
async fn test_unknown_kind_is_refused_400_before_anything_and_sends_nothing() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| "SHORT_ON_TRIGGER".clone_into(&mut l.kind));
    assert_eq!(
        refused(exec(&mut store, &rig, &plan, line, None).await).status,
        400
    );
    assert!(rig.sent().await.is_empty() && rig.gtt_sent().await.is_empty());
    assert!(store.line_state(line) == "PROPOSED" && store.line_history.is_empty());
}

#[tokio::test]
async fn test_rearm_twice_on_one_day_is_duplicate_blocked() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let pid = store.add_position_with(|p| p.gtt_id = None);
    let first = rearm(&mut store, &rig, &rig.settings, pid, "true", None)
        .await
        .unwrap();
    assert_eq!(first.status, "SIMULATED");
    store
        .update_position(pid, vec![("gtt_id", Val::Null)])
        .await
        .unwrap();
    let second = rearm(&mut store, &rig, &rig.settings, pid, "true", None)
        .await
        .unwrap();
    assert!(
        second.status == "BLOCKED" && second.reason.contains("DUPLICATE"),
        "{second:?}"
    );
}

#[tokio::test]
async fn test_two_partials_keep_a_running_share_weighted_exit_avg() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let pid = store.add_position_with(|p| {
        p.gtt_id = Some("DRY-buy".to_owned());
        p.quantity_entered = 300;
        p.quantity_open = 300;
    });
    for price in ["110.00", "120.00"] {
        let plan = store.add_plan();
        let line = sell_line(&mut store, &plan, 100, "PARTIAL_INTO_STRENGTH");
        let out = exec(&mut store, &rig, &plan, line, Some(d(price)))
            .await
            .unwrap();
        assert_eq!(out.status, "SIMULATED");
    }
    let p = pos(&store, pid);
    assert!(p.exit_avg == Some(d("115.00")) && p.quantity_open == 100);
    assert!(p.state == "PARTIAL" && p.r_multiple.is_none());
}

// --- SW9.5: the swing GTT rests 3 % under its trigger -------------------------------------

#[test]
fn test_the_swing_gtt_limit_fraction_is_a_config_constant_read_from_env() {
    let settings = Settings::defaults().unwrap();
    assert_eq!(settings.swing_gtt_limit_fraction, 0.97);
    assert!(
        0.0 < settings.swing_gtt_limit_fraction && settings.swing_gtt_limit_fraction < 0.995,
        "tighter than the weekly book's cushion"
    );
    let var = crate::settings::ENV_VARS
        .iter()
        .find(|v| v.key == "BASKFY_SWING_GTT_LIMIT_FRACTION")
        .unwrap();
    assert_eq!(var.default, "0.97");
}

#[tokio::test]
async fn test_every_live_swing_gtt_is_armed_with_the_swing_limit_fraction() {
    let live_gw = FakeGateway::new(&live());
    let mut store = MemoryStore::with(0, 3, MarketGate::Green, d("1000000"));
    let pid = store.add_position_with(|p| {
        p.gtt_id = Some("123".to_owned());
        p.stop = Some(d("96.00"));
    });
    let plan = store.add_plan();
    let raise = raise_line(&mut store, &plan, "100.00", "");
    let out = exec_fake(&mut store, &live_gw, &plan, raise, Some(d("104"))).await;
    assert_eq!(out.status, "FILLED");
    store
        .update_position(pid, vec![("gtt_id", Val::Null)])
        .await
        .unwrap();
    let again = rearm(
        &mut store,
        &live_gw,
        &live_gw.settings,
        pid,
        "true",
        Some(d("104")),
    )
    .await
    .unwrap();
    assert_eq!(again.status, "FILLED");
    let gtts = live_gw.gtts();
    assert_eq!(gtts.len(), 2, "the raise and the re-arm");
    assert_eq!(
        gtts.iter().map(|g| g.limit_fraction).collect::<Vec<_>>(),
        [Some(0.97), Some(0.97)]
    );
    assert!(
        gtts.iter()
            .all(|g| g.limit_fraction == Some(live_gw.settings.swing_gtt_limit_fraction))
    );
}

#[tokio::test]
async fn test_every_dry_run_swing_gtt_journals_the_swing_limit_fraction() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let buy = store.add_line_with(&plan, |l| l.quantity = 100);
    assert_eq!(
        exec(&mut store, &rig, &plan, buy, None)
            .await
            .unwrap()
            .status,
        "SIMULATED"
    );
    let pid = store.add_position_with(|p| {
        p.gtt_id = Some("DRY-old".to_owned());
        p.quantity_entered = 300;
        p.quantity_open = 300;
        "BETA".clone_into(&mut p.symbol);
        p.instrument_id = 12;
    });
    let sell = store.add_line_with(&plan, |l| {
        "SELL_AT_OPEN".clone_into(&mut l.kind);
        l.quantity = 100;
        l.trigger = None;
        l.stop = None;
        l.note = Some("PARTIAL_INTO_STRENGTH".to_owned());
        "BETA".clone_into(&mut l.symbol);
        l.instrument_id = 12;
    });
    assert_eq!(
        exec(&mut store, &rig, &plan, sell, Some(d("110")))
            .await
            .unwrap()
            .status,
        "SIMULATED"
    );
    let raise = store.add_line_with(&plan, |l| {
        "RAISE_GTT_STOP".clone_into(&mut l.kind);
        l.quantity = 0;
        l.trigger = None;
        l.stop = Some(d("100.00"));
        "BETA".clone_into(&mut l.symbol);
        l.instrument_id = 12;
    });
    assert_eq!(
        exec(&mut store, &rig, &plan, raise, Some(d("110")))
            .await
            .unwrap()
            .status,
        "SIMULATED"
    );
    store
        .update_position(pid, vec![("gtt_id", Val::Null)])
        .await
        .unwrap();
    assert_eq!(
        rearm(&mut store, &rig, &rig.settings, pid, "true", Some(d("110")))
            .await
            .unwrap()
            .status,
        "SIMULATED"
    );
    let armed: Vec<serde_json::Value> = rig
        .journal()
        .into_iter()
        .filter(|r| r["event"] == "gtt_dry_run")
        .collect();
    assert_eq!(armed.len(), 4, "buy, partial re-arm, raise, re-arm");
    assert!(
        armed
            .iter()
            .all(|r| r["limit_fraction"].as_f64() == Some(0.97))
    );
}

#[test]
fn test_the_weekly_books_gtt_path_does_not_name_the_swing_cushion() {
    // The weekly book's order path in Rust: the weekly lane and the desk's Kite wrapper.
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
    let mut files = vec![root.join("lanes/exec_core/kite_client.rs")];
    let mut stack = vec![root.join("lanes/weekly")];
    while let Some(dir) = stack.pop() {
        for entry in std::fs::read_dir(&dir).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                stack.push(path);
            } else if path.extension().is_some_and(|e| e == "rs") {
                files.push(path);
            }
        }
    }
    files.push(root.join("lanes/weekly.rs"));
    for path in files {
        let src = std::fs::read_to_string(&path).unwrap();
        assert!(
            !src.contains("swing_gtt_limit_fraction") && !src.contains("SWING_GTT_LIMIT_FRACTION"),
            "{}",
            path.display()
        );
    }
}

// --- SW10.4 (A5): the confirm-time gate ------------------------------------------------------

/// ALPHA held: 1,666 × 100.80 = ₹1,67,932.80, 16.79 % of the ₹10 lakh sleeve.
fn rung0_book(store: &mut MemoryStore) {
    store.add_position_with(|p| {
        p.entry_avg = Some(d("100.80"));
        p.quantity_entered = 1666;
        p.quantity_open = 1666;
        p.initial_stop = Some(d("97.80"));
        p.stop = Some(d("97.80"));
    });
}

/// BETA 833 × 210 = ₹1,74,930 — fits a 25 % rung alone, not beside ALPHA.
fn beta_line(store: &mut MemoryStore, plan: &str, quantity: i64) -> i64 {
    store.detected.insert(
        "BETA".to_owned(),
        (d("6.00"), Some(d("200000000")), d("70.00")),
    );
    store.add_line_with(plan, |l| {
        "BETA".clone_into(&mut l.symbol);
        l.instrument_id = 12;
        l.setup = Some("EP".to_owned());
        l.quantity = quantity;
        l.trigger = Some(d("210.00"));
        l.stop = Some(d("204.00"));
        l.risk_inr = Some(d("4998.00"));
        l.position_value = Some(d("174930.00"));
    })
}

fn book(store: &MemoryStore) -> Decimal {
    store
        .book
        .positions
        .values()
        .map(|p| p.entry_avg.unwrap() * Decimal::from(p.quantity_open))
        .sum()
}

fn rung(r: usize) -> MemoryStore {
    MemoryStore::with(0, r, MarketGate::Green, d("1000000"))
}

#[tokio::test]
async fn test_confirm_re_sizes_a_line_to_the_rungs_ceiling_and_rewrites_the_row() {
    let rig = Rig::new(&paper()).await;
    let mut store = rung(0);
    rung0_book(&mut store);
    let plan = store.add_plan();
    let line = beta_line(&mut store, &plan, 833);
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert_eq!(out.status, "SIMULATED", "{out:?}");
    assert_eq!(rig.sent_quantities(), [390]);
    assert_eq!(int(out.gtt.as_ref().unwrap(), "qty"), 390);
    let l = store.book.lines[&line].clone();
    assert!(l.quantity == 390 && l.state == "FILLED");
    assert_eq!(
        (l.risk_inr, l.position_value),
        (Some(d("2340.00")), Some(d("81900.00")))
    );
    let note = l.note.clone().unwrap();
    assert!(
        note.contains("re-sized at confirm 833 → 390") && note.contains("CASH"),
        "{note}"
    );
    assert_eq!(
        store.resize_log,
        [(line, 390, d("2340.00"), d("81900.00"), note)]
    );
    let p = pos(&store, out.position_id.unwrap());
    assert_eq!((p.quantity_entered, p.quantity_open), (390, 390));
    assert_eq!(
        fill_val(store.book.fills.last().unwrap(), "quantity"),
        &Val::Int(390)
    );
    assert_eq!(book(&store), d("249832.80"));
    assert!(
        book(&store) <= d("250000"),
        "24.98 % of the sleeve, not 34 %"
    );
    let events = rig.events();
    assert_eq!(events[events.len() - 2..], ["dry_run", "gtt_dry_run"]);
}

#[tokio::test]
async fn test_confirm_exposure_full_when_no_sliver_fits_is_blocked_and_sends_nothing() {
    let rig = Rig::new(&paper()).await;
    let mut store = rung(0);
    store.add_position_with(|p| {
        p.entry_avg = Some(d("124.00"));
        p.quantity_entered = 2000;
        p.quantity_open = 2000;
        p.initial_stop = Some(d("120.00"));
        p.stop = Some(d("120.00"));
    });
    let plan = store.add_plan();
    let line = beta_line(&mut store, &plan, 833);
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert!(
        out.status == "BLOCKED" && out.reason.starts_with("EXPOSURE_FULL: BETA"),
        "{out:?}"
    );
    assert!(
        out.reason.contains("leaves ₹2,000.00") && out.reason.contains("BELOW_MIN_TRADE_VALUE")
    );
    assert!(out.order.is_none() && out.gtt.is_none() && out.position_id.is_none());
    assert_eq!(store.line_state(line), "REJECTED");
    assert_eq!(
        store.book.lines[&line].quantity, 833,
        "a refused line keeps its planned size"
    );
    assert!(store.book.positions.len() == 1 && store.book.fills.is_empty());
    assert!(store.resize_log.is_empty() && rig.events().is_empty());
    assert_eq!(store.session(), ("DRY_RUN".to_owned(), 1, 0, 0));
}

#[tokio::test]
async fn test_confirm_tier_full_when_the_rung_allows_no_more_positions() {
    let rig = Rig::new(&paper()).await;
    let mut store = rung(0);
    store.add_position_with(|p| {
        p.entry_avg = Some(d("100.00"));
        p.quantity_entered = 100;
        p.quantity_open = 100;
    });
    store.add_position_with(|p| {
        "BETA".clone_into(&mut p.symbol);
        p.instrument_id = 12;
        p.entry_avg = Some(d("200.00"));
        p.quantity_entered = 100;
        p.quantity_open = 100;
        p.initial_stop = Some(d("190.00"));
        p.stop = Some(d("190.00"));
    });
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| {
        "GAMMA".clone_into(&mut l.symbol);
        l.instrument_id = 13;
        l.quantity = 50;
    });
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert!(
        out.status == "BLOCKED" && out.reason.starts_with("TIER_FULL: GAMMA"),
        "{out:?}"
    );
    assert!(out.reason.contains("rung 0 allows 2 positions"));
    assert!(store.book.positions.len() == 2 && rig.events().is_empty());
    assert_eq!(store.line_state(line), "REJECTED");
}

#[tokio::test]
async fn test_confirm_tier_full_takes_the_persons_cap_when_it_is_below_the_rung() {
    let rig = Rig::new(&paper()).await;
    let mut store = rung(3);
    store.book.config.as_mut().unwrap().max_open_positions = 1;
    store.add_position_with(|p| {
        p.quantity_entered = 100;
        p.quantity_open = 100;
    });
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| {
        "BETA".clone_into(&mut l.symbol);
        l.instrument_id = 12;
        l.quantity = 50;
    });
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert!(out.status == "BLOCKED" && out.reason.contains("TIER_FULL"));
    assert!(
        out.reason.contains("rung 3 allows 1 positions"),
        "{}",
        out.reason
    );
}

#[tokio::test]
async fn test_confirm_session_cap_after_three_entries_today() {
    let rig = Rig::new(&paper()).await;
    let mut store = rung(3);
    let plan = store.add_plan();
    let mut ids = Vec::new();
    for (sym, inst) in [("ALPHA", 11), ("BETA", 12), ("GAMMA", 13), ("DELTA", 14)] {
        ids.push(store.add_line_with(&plan, |l| {
            sym.clone_into(&mut l.symbol);
            l.instrument_id = inst;
            l.quantity = 10;
        }));
    }
    let mut outs = Vec::new();
    for id in &ids {
        outs.push(exec(&mut store, &rig, &plan, *id, None).await.unwrap());
    }
    assert_eq!(
        outs.iter().map(|o| o.status.as_str()).collect::<Vec<_>>(),
        ["SIMULATED", "SIMULATED", "SIMULATED", "BLOCKED"]
    );
    assert!(
        outs[3]
            .reason
            .starts_with("SESSION_CAP: DELTA — 3 new entries per session"),
        "{}",
        outs[3].reason
    );
    assert!(store.book.positions.len() == 3 && store.line_state(ids[3]) == "REJECTED");
    assert_eq!(rig.events().iter().filter(|e| *e == "dry_run").count(), 3);
}

#[tokio::test]
async fn test_session_cap_counts_a_sent_live_line_and_not_a_rejected_one() {
    let rig = Rig::new(&paper()).await;
    let mut store = rung(3);
    let plan = store.add_plan();
    for (sym, inst, state) in [
        ("A1", 21, "SENT"),
        ("A2", 22, "SENT"),
        ("A3", 23, "REJECTED"),
    ] {
        store.add_line_with(&plan, |l| {
            sym.clone_into(&mut l.symbol);
            l.instrument_id = inst;
            l.quantity = 10;
            state.clone_into(&mut l.state);
        });
    }
    let third = store.add_line_with(&plan, |l| {
        "A4".clone_into(&mut l.symbol);
        l.instrument_id = 24;
        l.quantity = 10;
    });
    assert_eq!(
        exec(&mut store, &rig, &plan, third, None)
            .await
            .unwrap()
            .status,
        "SIMULATED"
    );
    let fifth = store.add_line_with(&plan, |l| {
        "A5".clone_into(&mut l.symbol);
        l.instrument_id = 25;
        l.quantity = 10;
    });
    let out = exec(&mut store, &rig, &plan, fifth, None).await.unwrap();
    assert!(out.status == "BLOCKED" && out.reason.contains("SESSION_CAP"));
    let ctx = store.session_context(today()).await.unwrap();
    assert_eq!(ctx.entries_today, 3);
    assert_eq!(
        ctx.account.open_symbols,
        ["A1", "A2", "A4"].iter().map(|s| (*s).to_owned()).collect(),
        "SENT lines are held"
    );
}

#[tokio::test]
async fn test_a_sent_line_counts_as_exposure_at_its_trigger() {
    let rig = Rig::new(&paper()).await;
    let mut store = rung(0);
    let plan = store.add_plan();
    store.add_line_with(&plan, |l| {
        l.quantity = 1666;
        l.trigger = Some(d("100.80"));
        l.stop = Some(d("97.80"));
        "SENT".clone_into(&mut l.state);
    });
    let line = beta_line(&mut store, &plan, 833);
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert_eq!(out.status, "SIMULATED", "{out:?}");
    assert_eq!(rig.sent_quantities(), [390]);
    assert_eq!(store.book.lines[&line].quantity, 390);
}

#[tokio::test]
async fn test_confirm_never_sends_more_than_the_line_said() {
    let rig = Rig::new(&paper()).await;
    let mut store = rung(3);
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| l.quantity = 100);
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert_eq!(out.status, "SIMULATED");
    assert_eq!(rig.sent_quantities(), [100]);
    assert!(store.resize_log.is_empty() && store.book.lines[&line].quantity == 100);
    assert_eq!(
        store.book.lines[&line].note.as_deref(),
        Some(""),
        "an unchanged line keeps its note"
    );
}

#[tokio::test]
async fn test_the_line_being_confirmed_is_not_counted_against_itself() {
    let rig = Rig::new(&paper()).await;
    let mut store = rung(0);
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| {
        l.quantity = 1666;
        l.trigger = Some(d("100.80"));
        l.stop = Some(d("97.80"));
    });
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert_eq!(out.status, "SIMULATED", "{out:?}");
    assert_eq!(rig.sent_quantities(), [1666]);
    assert_eq!(store.context_reads, 1);
    let n = store.line_history.len();
    assert_eq!(
        store.line_history[n - 2..],
        [(line, "CONFIRMED".to_owned()), (line, "FILLED".to_owned())]
    );
}

#[tokio::test]
async fn test_confirm_re_sizes_with_the_persons_risk_knob() {
    let rig = Rig::new(&paper()).await;
    let mut store = rung(3);
    store.book.config.as_mut().unwrap().risk_per_trade_pct = d("0.250");
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| {
        l.quantity = 1666;
        l.trigger = Some(d("100.80"));
        l.stop = Some(d("97.80"));
    });
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert_eq!(out.status, "SIMULATED");
    assert_eq!(rig.sent_quantities(), [833]);
    assert!(
        store.book.lines[&line]
            .note
            .as_deref()
            .unwrap()
            .contains("re-sized at confirm 1666 → 833 (size by RISK)")
    );
}

async fn red_or_locked(red: bool, code: &str) {
    let rig = Rig::new(&paper()).await;
    let mut store = rung(1);
    if red {
        store.market_gate = MarketGate::Red;
        store.new_entries_allowed = false;
    } else {
        store.drawdown_locked = true;
    }
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert!(
        out.status == "BLOCKED" && out.reason.starts_with(&format!("{code}: ALPHA")),
        "{out:?}"
    );
    assert!(rig.events().is_empty() && store.book.positions.is_empty());
}

#[tokio::test]
async fn test_confirm_refuses_under_a_red_gate_or_a_drawdown_lock() {
    red_or_locked(true, "GATE_RED").await;
    red_or_locked(false, "DRAWDOWN_LOCKOUT").await;
}

#[tokio::test]
async fn test_confirm_refuses_a_name_with_no_adr_on_record() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    store.detected.remove("ALPHA");
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert!(out.status == "BLOCKED" && out.reason.starts_with("SIZE_REFUSED: ALPHA"));
    assert!(out.reason.contains("ADR is unknown") && rig.events().is_empty());
}

#[tokio::test]
async fn test_confirm_refuses_a_stop_wider_than_one_adr() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| {
        l.trigger = Some(d("100.00"));
        l.stop = Some(d("94.00"));
    });
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert_eq!(out.status, "BLOCKED");
    assert_eq!(out.reason, "SIZE_REFUSED: ALPHA — STOP_TOO_WIDE");
}

#[tokio::test]
async fn test_confirm_refuses_a_setup_this_book_does_not_trade() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| l.setup = Some("PARABOLIC_SHORT".to_owned()));
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert!(out.status == "BLOCKED" && out.reason.starts_with("NOT_TRADEABLE_SETUP"));
    let line = store.add_line_with(&plan, |l| l.setup = None);
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert!(out.status == "BLOCKED" && out.reason.starts_with("NOT_TRADEABLE_SETUP"));
}

#[tokio::test]
async fn test_a_partial_then_full_sequence_of_confirms_holds_the_ceiling_and_the_count() {
    let rig = Rig::new(&paper()).await;
    let mut store = rung(0);
    let plan = store.add_plan();
    let alpha = store.add_line_with(&plan, |l| {
        l.quantity = 1666;
        l.trigger = Some(d("100.80"));
        l.stop = Some(d("97.80"));
    });
    let beta = beta_line(&mut store, &plan, 833);
    store.detected.insert(
        "GAMMA".to_owned(),
        (d("5.00"), Some(d("100000000")), d("65.00")),
    );
    let gamma = store.add_line_with(&plan, |l| {
        "GAMMA".clone_into(&mut l.symbol);
        l.instrument_id = 13;
        l.quantity = 10;
    });
    let mut outs = Vec::new();
    for id in [alpha, beta, gamma] {
        outs.push(exec(&mut store, &rig, &plan, id, None).await.unwrap());
    }
    assert_eq!(
        outs.iter().map(|o| o.status.as_str()).collect::<Vec<_>>(),
        ["SIMULATED", "SIMULATED", "BLOCKED"]
    );
    assert_eq!(rig.sent_quantities(), [1666, 390]);
    assert!(outs[2].reason.starts_with("TIER_FULL: GAMMA"));
    assert_eq!(book(&store), d("249832.80"));
    assert_eq!(store.locks, [today(); 3]);
    assert_eq!((store.session().1, store.session().2), (3, 2));
}

#[tokio::test]
async fn test_the_lock_is_taken_for_every_kind_and_spans_the_gateway_call() {
    let order = Arc::new(Mutex::new(Vec::new()));
    let mut rig = Rig::new(&paper()).await;
    rig.log = Some(order.clone());
    let mut store = MemoryStore::new();
    store.lock_log = Some(order.clone());
    let pid = store.add_position_with(|p| {
        p.quantity_entered = 300;
        p.quantity_open = 300;
        p.gtt_id = Some("4242".to_owned());
    });
    let plan = store.add_plan();
    let sell = sell_line(&mut store, &plan, 100, "");
    let raise = raise_line(&mut store, &plan, "98.00", "");
    exec(&mut store, &rig, &plan, sell, Some(d("104.00")))
        .await
        .unwrap();
    exec(&mut store, &rig, &plan, raise, Some(d("104.00")))
        .await
        .unwrap();
    assert_eq!(
        *order.lock().unwrap(),
        [
            "lock",
            "place",
            "delete_gtt",
            "place_gtt_stop",
            "unlock",
            "lock",
            "place_gtt_stop",
            "unlock"
        ]
    );
    assert_eq!(pos(&store, pid).quantity_open, 200);
    assert_eq!(pos(&store, pid).stop, Some(d("98.00")));
}

#[tokio::test]
async fn test_a_gateway_exception_rolls_the_lock_back_and_records_the_refusal_once() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| "SGBAUG28".clone_into(&mut l.symbol));
    let out = exec(&mut store, &rig, &plan, line, None).await.unwrap();
    assert_eq!(out.status, "BLOCKED");
    assert_eq!(store.line_state(line), "REJECTED");
    assert_eq!(store.session().1, 1);
    assert!(store.book.positions.is_empty() && store.book.fills.is_empty());
}

#[tokio::test]
async fn test_a_line_confirmed_by_another_request_under_the_lock_is_409() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    store.lose_race = Some(line); // the other tab won the lock first
    let r = refused(exec(&mut store, &rig, &plan, line, None).await);
    assert!(
        r.status == 409 && r.detail.contains("FILLED"),
        "{}",
        r.detail
    );
    assert!(rig.events().is_empty() && store.book.positions.is_empty());
    assert!(
        store.book.sessions.is_empty(),
        "no confirm was counted for a line that was already gone"
    );
}

#[test]
fn test_sizing_config_carries_the_three_knobs_and_nothing_else() {
    use baskfy_core::swing::setups::SwingConfig;
    let mut cfg = MemoryStore::new().book.config.unwrap();
    cfg.risk_per_trade_pct = d("0.250");
    cfg.max_position_pct = d("15.00");
    cfg.max_open_positions = 4;
    cfg.sleeve_capital_inr = d("1");
    let config = x::sizing_config(&cfg, Decimal::ONE);
    assert_eq!(
        (
            config.sizing.risk_per_trade_pct,
            config.sizing.max_position_pct,
            config.sizing.max_open_positions
        ),
        (0.25, 15.0, 4)
    );
    assert_eq!(
        config.sizing.max_new_entries_per_session, 3,
        "the session cap is not a setting"
    );
    let default = SwingConfig::default();
    assert!(config.stops == default.stops && config.market == default.market);
    // `sizing_config({})`: the pack's own three numbers give the pack's sizing back.
    let mut pack = cfg.clone();
    pack.risk_per_trade_pct = d("0.5");
    pack.max_position_pct = d("20.0");
    pack.max_open_positions = default.sizing.max_open_positions;
    assert_eq!(x::sizing_config(&pack, Decimal::ONE).sizing, default.sizing);
}

// --- SW11: observability cannot reach the order path -------------------------------------------

/// Every telemetry helper replaced by a sink that raises through.
struct Boom;

impl x::ConfirmTelemetry for Boom {
    fn span(&self, _: &str, _: &[(&str, &str)]) -> Result<(), String> {
        Err("telemetry sink down".to_owned())
    }
    fn count(&self, _: &str, _: &[(&str, &str)]) -> Result<(), String> {
        Err("telemetry sink down".to_owned())
    }
    fn observe(&self, _: &str, _: f64) -> Result<(), String> {
        Err("telemetry sink down".to_owned())
    }
    fn capture(&self, _: &str, _: &[(&str, &str)]) -> Result<(), String> {
        Err("telemetry sink down".to_owned())
    }
}

#[tokio::test]
async fn test_telemetry_never_raises_into_execute_line() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    let clock = FakeClock::default();
    let cx = ExecContext {
        settings: &rig.settings,
        orders: None,
        timer: &clock,
    };
    let out = x::execute_line_observed(
        &mut store,
        &rig,
        &cx,
        &plan,
        line,
        "true",
        now(),
        None,
        &Boom,
    )
    .await
    .unwrap();
    assert_eq!(out.status, "SIMULATED");
    assert_eq!(st(out.gtt.as_ref().unwrap()), gtt_status::DRY_RUN_GTT);
    assert_eq!(store.line_state(line), "FILLED");
    let r = x::execute_line_observed(&mut store, &rig, &cx, &plan, line, "no", now(), None, &Boom)
        .await;
    assert_eq!(refused(r).status, 400);
    // An error path too: the capture and the metric raising change nothing about the refusal.
    let mut store = MemoryStore::new();
    store.lose_race = None;
    let plan = store.add_plan();
    let line = store.add_line(&plan);
    store.lose_race = Some(line);
    let r = x::execute_line_observed(
        &mut store,
        &rig,
        &cx,
        &plan,
        line,
        "true",
        now(),
        None,
        &Boom,
    )
    .await;
    assert_eq!(refused(r).status, 409);
}

#[test]
fn test_observe_raises_inside_a_real_span_keeps_the_bodys_exception() {
    use crate::lanes::session::telemetry::span;
    // A body that answers 409 comes out as that, not as the span's own failure.
    let caught: Result<(), u16> = span("swing.test", &[("kind", "BUY")], |_| Err(409));
    assert_eq!(caught, Err(409));
    // No tracer (the collector unreachable is the same branch here): the body runs, `current`
    // is None.
    let mut ran = false;
    let current = span("swing.test", &[], |current| {
        ran = true;
        current
    });
    assert!(ran && current.is_none());
}

#[tokio::test]
async fn test_sell_acts_on_the_position_the_line_names_when_a_name_holds_two_positions() {
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let first = store.add_position_with(|p| {
        p.quantity_entered = 300;
        p.quantity_open = 300;
    });
    let second = store.add_position_with(|p| {
        p.quantity_entered = 200;
        p.quantity_open = 200;
    });
    let plan = store.add_plan();
    let line = store.add_line_with(&plan, |l| {
        "SELL_AT_OPEN".clone_into(&mut l.kind);
        l.quantity = 100;
        l.trigger = None;
        l.stop = None;
        l.position_id = Some(first);
    });
    let out = exec(&mut store, &rig, &plan, line, Some(d("105")))
        .await
        .unwrap();
    assert_ne!(out.status, "BLOCKED", "{}", out.reason);
    assert_eq!(out.position_id, Some(first));
    assert_eq!(pos(&store, first).quantity_open, 200);
    assert_eq!(pos(&store, second).quantity_open, 200);
}

// --- R8.15.1: the defect fixed in this wave ----------------------------------------------------

#[tokio::test]
async fn a_plan_with_no_expiry_is_a_500_not_a_410_and_the_line_is_untouched() {
    // Python's `_aware(plan["expires_at"])` on None raises AttributeError: FastAPI's 500.
    let rig = Rig::new(&paper()).await;
    let mut store = MemoryStore::new();
    let plan = store.add_plan();
    store.book.plans.get_mut(&plan).unwrap().expires_at = None;
    let line = store.add_line(&plan);
    let r = exec(&mut store, &rig, &plan, line, None).await;
    assert!(matches!(r, Err(ExecError::Store(_))), "{r:?}");
    assert_eq!(store.line_state(line), "PROPOSED");
    assert!(store.locks.is_empty() && rig.events().is_empty());
}

// --- the coverage check -----------------------------------------------------------------------

/// Every `def test_*` in `tests/test_swing_execute.py`, ported above under its own name.
const PORTED: &[&str] = &[
    "test_contract_surface_by_name",
    "test_swing_gates_dry_run_unless_both_flags_allow",
    "test_swing_gates_never_allow_intraday_or_options",
    "test_build_swing_gateway_uses_swing_band_and_own_journal",
    "test_400_when_confirm_is_not_true",
    "test_404_unknown_plan",
    "test_404_line_not_in_plan",
    "test_410_expired_plan_marks_the_line_expired",
    "test_expired_boundary_is_thirty_minutes_exactly",
    "test_409_when_line_is_not_proposed",
    "test_409_a_re_posted_confirm_cannot_send_twice",
    "test_buy_dry_run_fills_whole_line_and_arms_gtt_in_same_call",
    "test_buy_line_is_marked_confirmed_before_the_gateway_is_called",
    "test_buy_gtt_client_id_is_separate_from_order_client_id",
    "test_buy_journal_carries_order_and_gtt_lines",
    "test_buy_with_zero_quantity_is_blocked_before_the_gateway",
    "test_buy_with_stop_not_below_trigger_is_blocked_before_the_gateway",
    "test_buy_for_a_name_at_the_per_name_cap_is_blocked_and_a_second_entry_is_not",
    "test_buy_untouchable_instrument_raises_and_marks_line_rejected",
    "test_buy_duplicate_from_gateway_is_blocked_with_no_second_position",
    "test_buy_risk_blocked_is_blocked_with_the_gateway_error",
    "test_buy_gtt_refused_leaves_a_naked_position_and_says_so",
    "test_flag_off_dry_run_false_still_never_reaches_broker",
    "test_flag_off_sell_and_raise_never_reach_broker",
    "test_dry_run_rearm_never_reaches_broker",
    "test_never_reaches_broker_across_the_whole_module_is_the_kc_contract",
    "test_sell_for_more_than_quantity_open_is_blocked",
    "test_sell_for_a_symbol_not_in_sw_position_is_blocked",
    "test_sell_when_quantity_open_already_zero_is_blocked",
    "test_sell_with_zero_quantity_is_blocked",
    "test_sell_without_last_price_is_blocked_not_guessed",
    "test_sell_partial_resizes_gtt_for_the_remainder",
    "test_sell_partial_gtt_resize_goes_through_gateway_delete_for_a_real_id",
    "test_sell_and_raise_for_the_same_name_in_one_plan_both_arm",
    "test_sell_close_out_writes_r_multiple_pnl_exit_avg_close_reason",
    "test_close_after_partial_exit_avg_is_share_weighted",
    "test_close_with_unknown_note_is_manual_and_a_loss_has_negative_r",
    "test_sell_partial_on_a_naked_position_arms_a_gtt_for_the_remainder",
    "test_sell_partial_when_price_is_below_stop_leaves_naked_with_reason",
    "test_raise_below_resting_stop_is_blocked",
    "test_raise_equal_to_resting_stop_is_blocked",
    "test_raise_for_symbol_not_held_is_blocked",
    "test_raise_without_last_price_is_blocked",
    "test_raise_at_or_above_last_price_is_blocked_before_cancelling",
    "test_raise_gtt_stop_replaces_the_trigger_and_updates_the_position",
    "test_raise_when_cancel_is_refused_places_no_second_gtt",
    "test_rearm_gtt_400_without_confirm",
    "test_rearm_gtt_404_unknown_position",
    "test_rearm_gtt_arms_naked_position_for_quantity_open",
    "test_rearm_gtt_on_a_covered_position_is_blocked",
    "test_rearm_gtt_on_a_closed_position_is_blocked",
    "test_rearm_gtt_breakeven_stop_needs_a_last_price",
    "test_risk_multiplier_for_is_half_only_for_a_real_order_with_sessions_left",
    "test_first_live_risk_multiplier_halves_the_risk_at_plan_time_not_the_quantity",
    "test_first_live_risk_multiplier_is_applied_once_not_twice",
    "test_first_live_never_halves_a_simulated_confirm",
    "test_first_live_at_zero_sends_full_size_and_tags_nothing",
    "test_first_live_countdown_is_never_moved_by_a_request",
    "test_first_live_sell_and_raise_are_never_halved",
    "test_the_entry_cap_is_still_a8s_and_is_sent_as_market_protection",
    "test_a_price_already_past_the_cap_is_refused_not_chased",
    "test_a_live_buy_without_a_price_is_blocked_never_guessed",
    "test_the_dry_run_journal_records_the_shape_that_would_have_been_sent",
    "test_the_request_polls_the_order_at_most_ten_seconds_at_two_a_second",
    "test_the_poll_stops_early_on_complete",
    "test_complete_writes_the_position_the_fill_and_the_gtt_in_the_same_request",
    "test_a_partial_fill_is_sent_with_the_filled_quantity_and_a_gtt_for_exactly_it",
    "test_partial_then_complete_ends_with_one_gtt_covering_exactly_the_filled_quantity",
    "test_on_order_update_is_idempotent_on_a_repeated_postback",
    "test_on_order_update_creates_the_position_when_the_request_saw_nothing_filled",
    "test_the_postback_modifies_the_gtt_never_places_a_second_one",
    "test_the_postback_never_modifies_the_gtt_above_the_filled_quantity",
    "test_a_postback_for_an_unknown_or_weekly_order_is_ignored",
    "test_a_postback_arms_a_naked_partial_for_the_whole",
    "test_the_cutoff_cancels_the_open_remainder_and_leaves_the_gtt_untouched",
    "test_the_cutoff_applies_a_fill_that_arrived_since_before_cancelling",
    "test_the_cutoff_expires_a_line_nothing_filled_and_reports_a_refused_cancel",
    "test_the_cutoff_leaves_a_completed_order_alone",
    "test_the_cutoff_frees_the_unclaimed_pending_range_slots",
    "test_a_dry_run_fill_follows_the_same_path_with_simulated_true",
    "test_a_rejected_order_with_nothing_filled_is_rejected_and_writes_no_book",
    "test_the_1515_sweep_stub_re_arms_naked_positions_given_a_price",
    "test_pending_range_is_refused_400_by_execute_line_before_the_lock",
    "test_pending_range_is_refused_under_live_gates_too",
    "test_pending_range_is_not_in_the_executable_kinds_at_source_level",
    "test_module_code_never_names_a_broker_method",
    "test_prices_are_float_at_the_gateway_boundary",
    "test_prices_stay_decimal_in_the_store",
    "test_live_gates_with_an_exploding_kc_are_rejected_never_a_position",
    "test_gtt_outside_the_swing_band_is_journalled_not_refused",
    "test_naive_timestamps_are_read_as_ist",
    "test_session_day_is_the_ist_date",
    "test_line_with_no_quantity_is_blocked",
    "test_unknown_kind_is_refused_400_before_anything_and_sends_nothing",
    "test_rearm_twice_on_one_day_is_duplicate_blocked",
    "test_two_partials_keep_a_running_share_weighted_exit_avg",
    "test_the_swing_gtt_limit_fraction_is_a_config_constant_read_from_env",
    "test_every_live_swing_gtt_is_armed_with_the_swing_limit_fraction",
    "test_every_dry_run_swing_gtt_journals_the_swing_limit_fraction",
    "test_the_weekly_books_gtt_path_does_not_name_the_swing_cushion",
    "test_confirm_re_sizes_a_line_to_the_rungs_ceiling_and_rewrites_the_row",
    "test_confirm_exposure_full_when_no_sliver_fits_is_blocked_and_sends_nothing",
    "test_confirm_tier_full_when_the_rung_allows_no_more_positions",
    "test_confirm_tier_full_takes_the_persons_cap_when_it_is_below_the_rung",
    "test_confirm_session_cap_after_three_entries_today",
    "test_session_cap_counts_a_sent_live_line_and_not_a_rejected_one",
    "test_a_sent_line_counts_as_exposure_at_its_trigger",
    "test_confirm_never_sends_more_than_the_line_said",
    "test_the_line_being_confirmed_is_not_counted_against_itself",
    "test_confirm_re_sizes_with_the_persons_risk_knob",
    "test_confirm_refuses_under_a_red_gate_or_a_drawdown_lock",
    "test_confirm_refuses_a_name_with_no_adr_on_record",
    "test_confirm_refuses_a_stop_wider_than_one_adr",
    "test_confirm_refuses_a_setup_this_book_does_not_trade",
    "test_a_partial_then_full_sequence_of_confirms_holds_the_ceiling_and_the_count",
    "test_the_lock_is_taken_for_every_kind_and_spans_the_gateway_call",
    "test_a_gateway_exception_rolls_the_lock_back_and_records_the_refusal_once",
    "test_a_line_confirmed_by_another_request_under_the_lock_is_409",
    "test_sizing_config_carries_the_three_knobs_and_nothing_else",
    "test_telemetry_never_raises_into_execute_line",
    "test_observe_raises_inside_a_real_span_keeps_the_bodys_exception",
    "test_sell_acts_on_the_position_the_line_names_when_a_name_holds_two_positions",
];

/// The Python test names in `path`, lower-cased (Rust test names are snake case).
pub(super) fn python_tests(path: &str) -> Vec<String> {
    let file = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../kite-momentum-rebalancer/tests")
        .join(path);
    let source = std::fs::read_to_string(&file).unwrap();
    source
        .lines()
        .filter_map(|l| {
            let t = l.trim_start();
            let rest = t
                .strip_prefix("def test_")
                .or_else(|| t.strip_prefix("async def test_"))?;
            let name = rest.split('(').next()?;
            Some(format!("test_{name}").to_lowercase())
        })
        .collect()
}

#[test]
fn every_python_test_has_a_rust_port() {
    let python = python_tests("test_swing_execute.py");
    assert_eq!(python.len(), PORTED.len(), "{python:?}");
    let missing: Vec<&String> = python
        .iter()
        .filter(|n| !PORTED.contains(&n.as_str()))
        .collect();
    assert!(missing.is_empty(), "not ported: {missing:?}");
}
