//! `tests/test_twt_auto.py` and the refusal half of `tests/test_twt_execute.py`, ported as the
//! spec (TW17–TW20; non-negotiable 1's second exception). No test here holds a live gateway: the
//! drain's collaborators are seams, `execute_line` is a spy where the three flags are live, and
//! where the real `execute_line` runs it is refused before any broker could be asked.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use chrono::{DateTime, Duration, FixedOffset, NaiveDate, TimeZone};
use rust_decimal::Decimal;

use super::auto::{self as a, Collaborators, RunReport};
use super::execute::{self as x, ExecError, ExecOutcome, ExecResult};
use crate::gateway::{Gateway, GatewayError};
use crate::lanes::twt::NoSleeveGateway;
use crate::lanes::twt::common::{Rec, Val};
use crate::lanes::twt::store::{
    LineUpdate, SessionBump, SleeveMoney, StoreError, StoreResult, TwtStore,
};
use crate::settings::Settings;

fn ist() -> FixedOffset {
    FixedOffset::east_opt(5 * 3600 + 1800).unwrap()
}

/// Monday 21 Sep 2026, ten seconds past the open — when the loop runs it.
fn open() -> DateTime<FixedOffset> {
    ist().with_ymd_and_hms(2026, 9, 21, 9, 15, 10).unwrap()
}

fn built() -> DateTime<FixedOffset> {
    ist().with_ymd_and_hms(2026, 9, 21, 9, 5, 0).unwrap()
}

fn session() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 9, 18).unwrap()
}

fn d(text: &str) -> Decimal {
    text.parse().unwrap()
}

fn flags(dry_run: bool, execution: bool, auto: bool) -> Settings {
    let mut s = Settings::defaults().unwrap();
    s.dry_run = dry_run;
    s.twt_execution_enabled = execution;
    s.twt_auto_execute = auto;
    s
}

fn live() -> Settings {
    flags(false, true, true)
}

// =============================================================================================
// A store holding only what the runner and `_validate` read
// =============================================================================================

#[derive(Default)]
struct State {
    plans: Vec<Rec>,
    lines: Vec<Rec>,
    closed: Vec<NaiveDate>,
    calendar_unreadable: bool,
}

#[derive(Clone, Default)]
struct FakeStore(Arc<Mutex<State>>);

const KIND_ORDER: [&str; 4] = ["SELL_AT_OPEN", "ARM_GTT", "RAISE_GTT_STOP", "BUY_AT_OPEN"];

fn not_here<T>(what: &str) -> StoreResult<T> {
    Err(StoreError::Bad(format!(
        "the runner's fake store has no {what}"
    )))
}

impl FakeStore {
    fn line_state(&self, id: i64) -> String {
        let st = self.0.lock().unwrap();
        st.lines
            .iter()
            .find(|l| l.int("id") == id)
            .unwrap()
            .s("state")
    }

    fn states(&self) -> Vec<String> {
        self.0
            .lock()
            .unwrap()
            .lines
            .iter()
            .map(|l| l.s("state"))
            .collect()
    }
}

#[async_trait]
impl TwtStore for FakeStore {
    async fn plan(&mut self, plan_id: &str) -> StoreResult<Option<Rec>> {
        let st = self.0.lock().unwrap();
        Ok(st.plans.iter().find(|p| p.s("plan_id") == plan_id).cloned())
    }
    async fn line(&mut self, line_id: i64) -> StoreResult<Option<Rec>> {
        let st = self.0.lock().unwrap();
        Ok(st.lines.iter().find(|l| l.int("id") == line_id).cloned())
    }
    async fn set_line(&mut self, line_id: i64, state: &str, extra: LineUpdate) -> StoreResult<()> {
        let mut st = self.0.lock().unwrap();
        if let Some(l) = st.lines.iter_mut().find(|l| l.int("id") == line_id) {
            l.set("state", Val::Str(state.to_owned()));
            if let Some(n) = extra.note {
                l.set("note", Val::Str(n));
            }
        }
        Ok(())
    }
    async fn open_position_for(&mut self, _instrument_id: i64) -> StoreResult<Option<Rec>> {
        not_here("open_position_for")
    }
    async fn position(&mut self, _position_id: i64) -> StoreResult<Option<Rec>> {
        not_here("position")
    }
    async fn open_positions(&mut self) -> StoreResult<Vec<Rec>> {
        not_here("open_positions")
    }
    async fn create_position(&mut self, _fields: Rec) -> StoreResult<i64> {
        not_here("create_position")
    }
    async fn update_position(&mut self, _position_id: i64, _fields: Rec) -> StoreResult<()> {
        not_here("update_position")
    }
    async fn add_fill(&mut self, _fields: Rec) -> StoreResult<i64> {
        not_here("add_fill")
    }
    async fn order(&mut self, _order_id: i64) -> StoreResult<Option<Rec>> {
        not_here("order")
    }
    async fn order_by_broker_id(&mut self, _id: &str) -> StoreResult<Option<Rec>> {
        not_here("order_by_broker_id")
    }
    async fn create_exit_order(&mut self, _fields: Rec) -> StoreResult<i64> {
        not_here("create_exit_order")
    }
    async fn exit_order_by_broker_id(&mut self, _id: &str) -> StoreResult<Option<Rec>> {
        not_here("exit_order_by_broker_id")
    }
    async fn update_exit_order(&mut self, _id: i64, _fields: Rec) -> StoreResult<()> {
        not_here("update_exit_order")
    }
    async fn open_orders(&mut self) -> StoreResult<Vec<Rec>> {
        not_here("open_orders")
    }
    async fn open_protection_issues(&mut self) -> StoreResult<Vec<Rec>> {
        not_here("open_protection_issues")
    }
    async fn line_by_client_id(&mut self, _client_id: &str) -> StoreResult<Option<Rec>> {
        not_here("line_by_client_id")
    }
    async fn create_order(&mut self, _fields: Rec) -> StoreResult<i64> {
        not_here("create_order")
    }
    async fn update_order(&mut self, _order_id: i64, _fields: Rec) -> StoreResult<()> {
        not_here("update_order")
    }
    async fn entries_taken(&mut self, _day: NaiveDate) -> StoreResult<i64> {
        not_here("entries_taken")
    }
    async fn bump_session(&mut self, _day: NaiveDate, _bump: SessionBump) -> StoreResult<()> {
        not_here("bump_session")
    }
    async fn set_naked_count(&mut self, _day: NaiveDate, _m: &str, _n: i64) -> StoreResult<()> {
        not_here("set_naked_count")
    }
    async fn lock_session_for_update(&mut self, _day: NaiveDate) -> StoreResult<()> {
        not_here("lock_session_for_update")
    }
    async fn sleeve_money(&mut self, _as_of: NaiveDate) -> StoreResult<SleeveMoney> {
        not_here("sleeve_money")
    }
    async fn adj_factor(&mut self, _id: i64, _as_of: NaiveDate) -> StoreResult<Decimal> {
        not_here("adj_factor")
    }
    async fn count_first_live_entry(
        &mut self,
        _position_id: i64,
        _session_date: NaiveDate,
        _now: DateTime<FixedOffset>,
        _live: bool,
    ) -> StoreResult<bool> {
        not_here("count_first_live_entry")
    }
    async fn expire_plans(&mut self, _now: DateTime<FixedOffset>) -> StoreResult<i64> {
        not_here("expire_plans")
    }
    async fn zero_capital(
        &mut self,
        _now: DateTime<FixedOffset>,
        _changed_by: &str,
    ) -> StoreResult<Option<Decimal>> {
        not_here("zero_capital")
    }
    async fn todays_plan(&mut self, on_or_before: NaiveDate) -> StoreResult<Option<Rec>> {
        let st = self.0.lock().unwrap();
        let mut plans: Vec<Rec> = st
            .plans
            .iter()
            .filter(|p| p.date("session_date").is_some_and(|d| d <= on_or_before))
            .cloned()
            .collect();
        plans.sort_by_key(|p| (p.date("session_date"), p.stamp("built_at")));
        Ok(plans.pop())
    }
    async fn lines_for(&mut self, plan_pk: i64) -> StoreResult<Vec<Rec>> {
        let st = self.0.lock().unwrap();
        let mut rows: Vec<Rec> = st
            .lines
            .iter()
            .filter(|l| l.int("plan_pk") == plan_pk)
            .cloned()
            .collect();
        rows.sort_by_key(|l| {
            let k = l.s("kind");
            (
                KIND_ORDER.iter().position(|o| *o == k).unwrap_or(99),
                l.s("symbol"),
            )
        });
        Ok(rows)
    }
    async fn is_trading_day(&mut self, day: NaiveDate) -> StoreResult<Option<bool>> {
        let st = self.0.lock().unwrap();
        if st.calendar_unreadable {
            return not_here("trading_day");
        }
        Ok(Some(!st.closed.contains(&day)))
    }
}

fn a_plan(
    store: &FakeStore,
    source: &str,
    built_at: DateTime<FixedOffset>,
    ttl_minutes: i64,
) -> (String, i64) {
    let mut st = store.0.lock().unwrap();
    let pk = i64::try_from(st.plans.len()).unwrap() + 1;
    let plan_id = format!("00000000-0000-4000-8000-{pk:012}");
    st.plans.push(Rec::of([
        ("id", Val::Int(pk)),
        ("plan_id", Val::Str(plan_id.clone())),
        ("session_date", Val::Date(session())),
        ("source", Val::from(source)),
        ("built_at", Val::Stamp(built_at)),
        (
            "expires_at",
            Val::Stamp(built_at + Duration::minutes(ttl_minutes)),
        ),
        ("gate", Val::from("OPEN")),
    ]));
    (plan_id, pk)
}

fn a_line(
    store: &FakeStore,
    plan: &(String, i64),
    line_id: i64,
    symbol: &str,
    kind: &str,
    state: &str,
) {
    let suffix = if kind == "BUY_AT_OPEN" {
        String::new()
    } else {
        format!(":{kind}")
    };
    store.0.lock().unwrap().lines.push(Rec::of([
        ("id", Val::Int(line_id)),
        ("plan_pk", Val::Int(plan.1)),
        ("plan_id", Val::Str(plan.0.clone())),
        ("kind", Val::from(kind)),
        ("instrument_id", Val::Int(1000 + line_id)),
        ("symbol", Val::from(symbol)),
        ("quantity", Val::Int(2_500)),
        ("stop_price", Val::Dec(d("80.00"))),
        ("state", Val::from(state)),
        ("note", Val::from("")),
        (
            "client_id",
            Val::Str(format!("{}:{symbol}{suffix}", plan.0)),
        ),
    ]));
}

/// `_morning(store)`: two buys, a raise, an arm and a rejected buy.
fn morning(store: &FakeStore) -> (String, i64) {
    let plan = a_plan(store, "MORNING", built(), 30);
    a_line(store, &plan, 1, "BBB", "BUY_AT_OPEN", "PROPOSED");
    a_line(store, &plan, 2, "AAA", "BUY_AT_OPEN", "PROPOSED");
    a_line(store, &plan, 3, "ZZZ", "RAISE_GTT_STOP", "PROPOSED");
    a_line(store, &plan, 4, "YYY", "ARM_GTT", "PROPOSED");
    a_line(store, &plan, 5, "CCC", "BUY_AT_OPEN", "REJECTED");
    plan
}

// =============================================================================================
// The collaborators: every seam `run_once` takes
// =============================================================================================

#[derive(Debug, Clone, PartialEq)]
struct Call {
    plan_id: String,
    line_id: i64,
    price: Option<Decimal>,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Exec {
    /// `SpyExecute`: records, marks the line SENT, sends nothing.
    Spy,
    /// The real `execute_line` (the trait's default).
    Real,
}

struct Seams {
    settings: Settings,
    now: DateTime<FixedOffset>,
    store: FakeStore,
    exec: Exec,
    raise_on: Option<String>,
    /// `_Refuse`: a collaborator that must never be reached.
    refuse: bool,
    reached: Mutex<Vec<String>>,
    authed: Mutex<VecDeque<bool>>,
    slept: Mutex<Vec<f64>>,
    calls: Mutex<Vec<Call>>,
}

impl Seams {
    fn new(settings: Settings, store: &FakeStore) -> Self {
        Self {
            settings,
            now: open(),
            store: store.clone(),
            exec: Exec::Spy,
            raise_on: None,
            refuse: false,
            reached: Mutex::new(Vec::new()),
            authed: Mutex::new(VecDeque::new()),
            slept: Mutex::new(Vec::new()),
            calls: Mutex::new(Vec::new()),
        }
    }

    fn at(mut self, now: DateTime<FixedOffset>) -> Self {
        self.now = now;
        self
    }

    fn reach(&self, what: &str) {
        self.reached.lock().unwrap().push(what.to_owned());
    }

    fn calls(&self) -> Vec<Call> {
        self.calls.lock().unwrap().clone()
    }

    async fn run(&self) -> RunReport {
        a::run_once(self, a::SESSION_WAIT_SECONDS, a::SESSION_POLL_SECONDS)
            .await
            .unwrap()
    }
}

#[async_trait]
impl Collaborators for Seams {
    fn settings(&self) -> Settings {
        self.settings.clone()
    }

    fn now(&self) -> DateTime<FixedOffset> {
        self.now
    }

    async fn open_store(&self) -> Result<Box<dyn TwtStore>, String> {
        if self.refuse {
            self.reach("the store");
        }
        Ok(Box::new(self.store.clone()))
    }

    async fn gateway(&self) -> Arc<dyn Gateway> {
        if self.refuse {
            self.reach("the gateway");
        }
        Arc::new(NoSleeveGateway("twt"))
    }

    async fn price_for(&self, _symbol: &str) -> Option<Decimal> {
        if self.refuse {
            self.reach("a price read");
        }
        Some(d("100.00"))
    }

    async fn authed(&self) -> bool {
        if self.refuse {
            self.reach("the Kite session");
        }
        self.authed.lock().unwrap().pop_front().unwrap_or(true)
    }

    async fn sleep(&self, seconds: f64) {
        self.slept.lock().unwrap().push(seconds);
    }

    async fn execute(
        &self,
        store: &mut dyn TwtStore,
        gateway: &dyn Gateway,
        settings: &Settings,
        plan_id: &str,
        line_id: i64,
        now: DateTime<FixedOffset>,
        last_price: Option<Decimal>,
    ) -> ExecResult<ExecOutcome> {
        if self.refuse {
            self.reach("execute_line");
        }
        if self.exec == Exec::Real {
            return x::execute_line(
                store, gateway, settings, plan_id, line_id, "true", now, last_price,
            )
            .await;
        }
        self.calls.lock().unwrap().push(Call {
            plan_id: plan_id.to_owned(),
            line_id,
            price: last_price,
        });
        let line = store.line(line_id).await?.unwrap();
        if self.raise_on.as_deref() == Some(line.s("symbol").as_str()) {
            return Err(ExecError::Gateway(GatewayError {
                python_type: "RuntimeError".to_owned(),
                message: "the broker fell over".to_owned(),
            }));
        }
        store
            .set_line(line_id, "SENT", LineUpdate::default())
            .await?;
        Ok(ExecOutcome {
            status: "SENT".to_owned(),
            reason: String::new(),
            order: None,
            gtt: None,
            order_row_id: None,
            position_id: None,
            simulated: false,
            naked: false,
        })
    }
}

// =============================================================================================
// The flag: the desk's, default false, and all three or nothing
// =============================================================================================

#[test]
fn it_defaults_false_and_so_do_the_sleeves_execution_flag_and_dry_run_defaults_true() {
    let s = Settings::defaults().unwrap();
    assert!(!s.twt_auto_execute);
    assert!(!s.twt_execution_enabled);
    assert!(s.dry_run);
    assert!(!a::auto_execute_enabled(&s));
    let source = include_str!("../../../settings.rs");
    assert!(source.contains(
        "EnvVar { key: \"BASKFY_TWT_AUTO_EXECUTE\", module: \"app.config\", attrs: \
         &[\"TWT_AUTO_EXECUTE\"], kind: Kind::BoolEqTrue, default: \"false\" }"
    ));
}

#[test]
fn it_needs_all_three_flags_and_any_one_off_is_off() {
    let mut cells = Vec::new();
    for dry_run in [true, false] {
        for execution in [false, true] {
            for auto in [false, true] {
                if a::auto_execute_enabled(&flags(dry_run, execution, auto)) {
                    cells.push((dry_run, execution, auto));
                }
            }
        }
    }
    assert_eq!(cells, vec![(false, true, true)], "more than one live cell");
}

#[test]
fn the_kinds_it_sends_are_exactly_the_kinds_a_click_may_send() {
    let mut auto = a::AUTO_KINDS.to_vec();
    let mut click = x::EXECUTABLE_KINDS.to_vec();
    auto.sort_unstable();
    click.sort_unstable();
    assert_eq!(auto, click);
    assert!(
        a::AUTO_KINDS.contains(&"SELL_AT_OPEN"),
        "TW20: sells are drained too"
    );
    assert_eq!(a::AUTO_SOURCES, ["MORNING", "LIVE"], "never EVENING");
}

// =============================================================================================
// Off means off
// =============================================================================================

#[tokio::test]
async fn any_flag_off_executes_nothing_and_opens_nothing() {
    for (dry_run, execution, auto) in [
        (true, false, false),
        (true, true, true),
        (false, false, true),
        (false, true, false),
        (true, false, true),
        (true, true, false),
        (false, false, false),
    ] {
        let store = FakeStore::default();
        morning(&store);
        let mut seams = Seams::new(flags(dry_run, execution, auto), &store);
        seams.refuse = true;
        let report = seams.run().await;
        assert!(!report.ran);
        assert!(report.reason.contains("off"), "{}", report.reason);
        assert!(
            seams.reached.lock().unwrap().is_empty(),
            "reached with auto-execute off: {:?}",
            seams.reached.lock().unwrap()
        );
        assert!(
            store
                .states()
                .iter()
                .all(|s| s == "PROPOSED" || s == "REJECTED")
        );
    }
}

#[tokio::test]
async fn the_drain_itself_refuses_with_the_flag_off() {
    let store = FakeStore::default();
    let plan = morning(&store);
    let plan_rec = store.clone().plan(&plan.0).await.unwrap().unwrap();
    let mut seams = Seams::new(flags(true, false, false), &store);
    seams.refuse = true;
    let mut handle = store.clone();
    let done = a::drain_plan(&seams, &mut handle, &NoSleeveGateway("twt"), &plan_rec)
        .await
        .unwrap();
    assert!(done.is_empty());
    assert!(seams.reached.lock().unwrap().is_empty());
}

// =============================================================================================
// On: every PROPOSED line through execute_line, once, in the plan's own order
// =============================================================================================

#[tokio::test]
async fn each_proposed_line_goes_through_execute_line_once_stops_first() {
    let store = FakeStore::default();
    let plan = morning(&store);
    let seams = Seams::new(live(), &store);
    let report = seams.run().await;
    assert!(report.ran);
    assert_eq!(report.plan_id.as_deref(), Some(plan.0.as_str()));
    let calls = seams.calls();
    assert_eq!(
        calls.iter().map(|c| c.line_id).collect::<Vec<_>>(),
        vec![4, 3, 2, 1],
        "ARM, RAISE, then buys A-Z"
    );
    assert!(calls.iter().all(|c| c.plan_id == plan.0));
    assert!(calls.iter().all(|c| c.price == Some(d("100.00"))));
    assert!(report.line().contains("4 line(s)"), "{}", report.line());
}

#[tokio::test]
async fn sells_go_first() {
    let store = FakeStore::default();
    let plan = morning(&store);
    a_line(&store, &plan, 6, "SSS", "SELL_AT_OPEN", "PROPOSED");
    let seams = Seams::new(live(), &store);
    seams.run().await;
    assert_eq!(
        seams.calls().iter().map(|c| c.line_id).collect::<Vec<_>>(),
        vec![6, 4, 3, 2, 1]
    );
}

#[tokio::test]
async fn one_line_that_raises_does_not_cost_the_rest() {
    let store = FakeStore::default();
    morning(&store);
    let mut seams = Seams::new(live(), &store);
    seams.raise_on = Some("AAA".to_owned());
    let report = seams.run().await;
    let statuses: Vec<(String, String)> = report
        .attempts
        .iter()
        .map(|(s, _, st)| (s.clone(), st.clone()))
        .collect();
    assert_eq!(
        statuses,
        vec![
            ("YYY".to_owned(), "SENT".to_owned()),
            ("ZZZ".to_owned(), "SENT".to_owned()),
            ("AAA".to_owned(), "ERROR".to_owned()),
            ("BBB".to_owned(), "SENT".to_owned()),
        ]
    );
    assert_eq!(
        store.line_state(2),
        "PROPOSED",
        "the failed line stays confirmable by hand"
    );
}

#[tokio::test]
async fn a_second_run_sends_nothing() {
    let store = FakeStore::default();
    morning(&store);
    let seams = Seams::new(live(), &store);
    seams.run().await;
    seams.run().await;
    assert_eq!(seams.calls().len(), 4);
}

#[tokio::test]
async fn the_real_execute_line_is_reached_with_confirm_true() {
    // A line already SENT: the real `execute_line` answers 409, which it reaches only past the
    // `confirm` check (400) — so the drain passed `confirm="true"`, the plan's own id and line.
    let store = FakeStore::default();
    let plan = a_plan(&store, "MORNING", built(), 30);
    a_line(&store, &plan, 1, "AAA", "BUY_AT_OPEN", "PROPOSED");
    store
        .clone()
        .set_line(1, "SENT", LineUpdate::default())
        .await
        .unwrap();
    // `executable_lines` skips a non-PROPOSED line, so drive the drain's own call directly.
    let mut seams = Seams::new(live(), &store);
    seams.exec = Exec::Real;
    let mut handle = store.clone();
    let err = seams
        .execute(
            &mut handle,
            &NoSleeveGateway("twt"),
            &live(),
            &plan.0,
            1,
            open(),
            Some(d("100")),
        )
        .await
        .unwrap_err();
    assert!(
        matches!(err, ExecError::Http(ref h) if h.status == 409),
        "{err}"
    );
}

#[tokio::test]
async fn a_refusal_of_the_real_path_is_reported_with_its_status_and_nothing_else_runs() {
    // The real `execute_line` over a store with no session row to lock: the first line errors,
    // the rest still go and each is reported; the gateway refuses everything (fail closed).
    let store = FakeStore::default();
    morning(&store);
    let mut seams = Seams::new(live(), &store);
    seams.exec = Exec::Real;
    let report = seams.run().await;
    assert!(report.ran);
    assert_eq!(report.attempts.len(), 4);
    assert!(report.attempts.iter().all(|(_, _, st)| st == "ERROR"));
    assert_eq!(
        store.states(),
        vec!["PROPOSED", "PROPOSED", "PROPOSED", "PROPOSED", "REJECTED"]
    );
}

// =============================================================================================
// No plan, no order — and never a plan of its own
// =============================================================================================

async fn refused(seams: &Seams) -> RunReport {
    let report = seams.run().await;
    assert!(!report.ran, "{report:?}");
    assert!(report.attempts.is_empty());
    assert!(seams.calls().is_empty());
    report
}

#[tokio::test]
async fn no_plan_sends_nothing() {
    let store = FakeStore::default();
    let r = refused(&Seams::new(live(), &store)).await;
    assert!(r.reason.starts_with("NO_PLAN"), "{}", r.reason);
}

#[tokio::test]
async fn an_expired_plan_sends_nothing() {
    let store = FakeStore::default();
    let plan = a_plan(&store, "MORNING", built(), 5); // expired 09:10
    a_line(&store, &plan, 1, "AAA", "BUY_AT_OPEN", "PROPOSED");
    let r = refused(&Seams::new(live(), &store)).await;
    assert!(r.reason.starts_with("EXPIRED"), "{}", r.reason);
    assert_eq!(store.line_state(1), "PROPOSED");
}

#[tokio::test]
async fn a_plan_at_the_instant_it_expires_is_expired() {
    let store = FakeStore::default();
    let plan = a_plan(&store, "MORNING", built(), 30);
    a_line(&store, &plan, 1, "AAA", "BUY_AT_OPEN", "PROPOSED");
    let seams = Seams::new(live(), &store).at(built() + Duration::minutes(30));
    let r = refused(&seams).await;
    assert!(r.reason.starts_with("EXPIRED"), "{}", r.reason);
}

#[tokio::test]
async fn a_live_plan_built_today_is_used() {
    let store = FakeStore::default();
    let plan = a_plan(&store, "LIVE", open() + Duration::hours(2), 30);
    a_line(&store, &plan, 1, "AAA", "BUY_AT_OPEN", "PROPOSED");
    let seams = Seams::new(live(), &store).at(open() + Duration::hours(2) + Duration::minutes(5));
    let report = seams.run().await;
    assert!(report.ran);
    assert_eq!(report.plan_id.as_deref(), Some(plan.0.as_str()));
    assert_eq!(
        seams.calls().iter().map(|c| c.line_id).collect::<Vec<_>>(),
        vec![1]
    );
}

#[tokio::test]
async fn an_expired_live_plan_sends_nothing() {
    let store = FakeStore::default();
    let plan = a_plan(&store, "LIVE", open() + Duration::hours(2), 30);
    a_line(&store, &plan, 1, "AAA", "BUY_AT_OPEN", "PROPOSED");
    let seams = Seams::new(live(), &store).at(open() + Duration::hours(2) + Duration::minutes(31));
    assert!(refused(&seams).await.reason.starts_with("EXPIRED"));
}

#[tokio::test]
async fn a_live_plan_is_drained_in_the_afternoon_too() {
    let store = FakeStore::default();
    let at = ist().with_ymd_and_hms(2026, 9, 21, 14, 0, 0).unwrap();
    let plan = a_plan(&store, "LIVE", at, 30);
    a_line(&store, &plan, 1, "AAA", "BUY_AT_OPEN", "PROPOSED");
    let seams = Seams::new(live(), &store).at(at + Duration::minutes(1));
    let report = seams.run().await;
    assert!(report.ran);
    assert_eq!(seams.calls().len(), 1);
}

#[tokio::test]
async fn an_evening_plan_is_never_used() {
    let store = FakeStore::default();
    let plan = a_plan(&store, "EVENING", built(), 24 * 60);
    a_line(&store, &plan, 1, "AAA", "BUY_AT_OPEN", "PROPOSED");
    let r = refused(&Seams::new(live(), &store)).await;
    assert!(r.reason.starts_with("NOT_MORNING"), "{}", r.reason);
}

#[tokio::test]
async fn yesterdays_morning_plan_is_never_used() {
    let store = FakeStore::default();
    let plan = a_plan(&store, "MORNING", built() - Duration::days(1), 48 * 60);
    a_line(&store, &plan, 1, "AAA", "BUY_AT_OPEN", "PROPOSED");
    let r = refused(&Seams::new(live(), &store)).await;
    assert!(r.reason.starts_with("STALE_PLAN"), "{}", r.reason);
}

#[tokio::test]
async fn nothing_before_the_open_at_the_close_or_on_a_weekend() {
    for at in [
        ist().with_ymd_and_hms(2026, 9, 21, 9, 14, 59).unwrap(),
        ist().with_ymd_and_hms(2026, 9, 21, 15, 30, 0).unwrap(),
        ist().with_ymd_and_hms(2026, 9, 19, 9, 20, 0).unwrap(), // Saturday
    ] {
        let store = FakeStore::default();
        morning(&store);
        refused(&Seams::new(live(), &store).at(at)).await;
    }
    let store = FakeStore::default();
    morning(&store);
    let sat = ist().with_ymd_and_hms(2026, 9, 19, 9, 20, 0).unwrap();
    let r = refused(&Seams::new(live(), &store).at(sat)).await;
    assert_eq!(r.reason, "Sat is not a weekday");
    let early = ist().with_ymd_and_hms(2026, 9, 21, 9, 14, 59).unwrap();
    let r = refused(&Seams::new(live(), &store).at(early)).await;
    assert_eq!(
        r.reason,
        "09:14 IST is outside the session; the entry is at market inside it (04 \u{a7}5.1, TW19)"
    );
}

#[tokio::test]
async fn an_exchange_holiday_sends_nothing() {
    let store = FakeStore::default();
    morning(&store);
    store.0.lock().unwrap().closed.push(open().date_naive());
    let r = refused(&Seams::new(live(), &store)).await;
    assert_eq!(r.reason, "2026-09-21 is not a trading session");
}

#[tokio::test]
async fn an_unreadable_calendar_is_treated_as_open() {
    let store = FakeStore::default();
    morning(&store);
    store.0.lock().unwrap().calendar_unreadable = true;
    let seams = Seams::new(live(), &store);
    assert!(seams.run().await.ran);
    assert_eq!(seams.calls().len(), 4);
}

#[tokio::test]
async fn it_waits_for_a_kite_session_then_gives_up_without_sending() {
    let store = FakeStore::default();
    morning(&store);
    let seams = Seams::new(live(), &store);
    seams
        .authed
        .lock()
        .unwrap()
        .extend([false, false, false, false]);
    let report = a::run_once(&seams, 90.0, 30.0).await.unwrap();
    assert!(!report.ran);
    assert_eq!(report.reason, "no Kite session after 90s");
    assert_eq!(*seams.slept.lock().unwrap(), vec![30.0, 30.0, 30.0]);
    assert!(seams.calls().is_empty());
}

#[tokio::test]
async fn a_session_that_arrives_late_is_used() {
    let store = FakeStore::default();
    morning(&store);
    let seams = Seams::new(live(), &store);
    seams.authed.lock().unwrap().extend([false, false, true]);
    let report = seams.run().await;
    assert!(report.ran);
    assert_eq!(seams.calls().len(), 4);
}

#[test]
fn the_report_line_says_what_happened() {
    let off = RunReport {
        reason: "NO_PLAN: x".to_owned(),
        ..RunReport::default()
    };
    assert_eq!(off.line(), "TWT AUTO: nothing sent \u{2014} NO_PLAN: x");
    let ran = RunReport {
        ran: true,
        reason: String::new(),
        plan_id: Some("p".to_owned()),
        attempts: vec![
            (
                "BBB".to_owned(),
                "BUY_AT_OPEN".to_owned(),
                "SENT".to_owned(),
            ),
            (
                "AAA".to_owned(),
                "BUY_AT_OPEN".to_owned(),
                "BLOCKED".to_owned(),
            ),
        ],
    };
    assert_eq!(
        ran.line(),
        "TWT AUTO: plan p: 2 line(s) \u{2014} BLOCKED 1, SENT 1 [BBB BUY_AT_OPEN SENT; AAA \
         BUY_AT_OPEN BLOCKED]"
    );
    let empty = RunReport {
        ran: true,
        plan_id: Some("p".to_owned()),
        ..RunReport::default()
    };
    assert_eq!(
        empty.line(),
        "TWT AUTO: plan p: 0 line(s) \u{2014} no executable lines"
    );
}

// =============================================================================================
// The runner never builds a plan and never reaches the broker except through execute_line
// =============================================================================================

#[test]
fn it_names_no_order_call_of_its_own() {
    let source = include_str!("../auto.rs");
    for forbidden in [
        "place_order",
        "place_gtt",
        ".place(",
        "kc.",
        "run_twt_evening",
        "store_plan",
        "INSERT",
        "UPDATE ",
    ] {
        assert!(!source.contains(forbidden), "auto.rs names {forbidden}");
    }
    assert!(source.contains("execute::execute_line"));
}

// =============================================================================================
// execute_line: nothing fires without the click (`TestNothingFiresWithoutTheClick`)
// =============================================================================================

async fn confirm(
    store: &FakeStore,
    plan_id: &str,
    line_id: i64,
    confirm: &str,
    now: DateTime<FixedOffset>,
) -> ExecError {
    let mut handle = store.clone();
    x::execute_line(
        &mut handle,
        &NoSleeveGateway("twt"),
        &flags(true, false, false),
        plan_id,
        line_id,
        confirm,
        now,
        Some(d("100")),
    )
    .await
    .unwrap_err()
}

fn status(e: &ExecError) -> (u16, String) {
    match e {
        ExecError::Http(h) => (h.status, h.detail.clone()),
        other => panic!("not an HTTP refusal: {other}"),
    }
}

#[tokio::test]
async fn a_missing_confirm_is_a_four_hundred_before_anything_is_read() {
    let store = FakeStore::default();
    let plan = morning(&store);
    for c in ["false", "", "yes", "1"] {
        let (code, detail) = status(&confirm(&store, &plan.0, 1, c, open()).await);
        assert_eq!(code, 400);
        assert_eq!(
            detail,
            "confirm=true is required \u{2014} nothing fires without it"
        );
    }
    // `str(confirm).lower()`: TRUE is a confirm.
    let (code, _) = status(&confirm(&store, "nope", 1, "TRUE", open()).await);
    assert_eq!(code, 404);
}

#[tokio::test]
async fn an_unknown_plan_is_a_four_oh_four() {
    let store = FakeStore::default();
    morning(&store);
    let (code, detail) = status(&confirm(&store, "no-such-plan", 1, "true", open()).await);
    assert_eq!((code, detail.as_str()), (404, "no plan no-such-plan"));
}

#[tokio::test]
async fn an_expired_plan_is_a_four_ten_and_nothing_is_sent() {
    let store = FakeStore::default();
    let plan = morning(&store);
    let expires = built() + Duration::minutes(30);
    for at in [expires, expires + Duration::seconds(1)] {
        let (code, detail) = status(&confirm(&store, &plan.0, 1, "true", at).await);
        assert_eq!(code, 410);
        assert!(
            detail.starts_with(&format!(
                "plan {} expired at 2026-09-21T09:35:00+05:30; rebuild it",
                plan.0
            )),
            "{detail}"
        );
    }
    assert_eq!(store.line_state(1), "PROPOSED");
}

#[tokio::test]
async fn a_line_of_another_plan_is_a_four_oh_four() {
    let store = FakeStore::default();
    let first = morning(&store);
    let other = a_plan(&store, "MORNING", built(), 30);
    a_line(&store, &other, 9, "OTH", "BUY_AT_OPEN", "PROPOSED");
    let (code, detail) = status(&confirm(&store, &first.0, 9, "true", open()).await);
    assert_eq!(code, 404);
    assert_eq!(detail, format!("no line 9 on plan {}", first.0));
    let (code, _) = status(&confirm(&store, &first.0, 999, "true", open()).await);
    assert_eq!(code, 404);
}

#[tokio::test]
async fn a_kind_this_sleeve_does_not_execute_is_a_four_hundred() {
    let store = FakeStore::default();
    let plan = morning(&store);
    a_line(&store, &plan, 7, "MIS", "BUY_INTRADAY", "PROPOSED");
    let (code, detail) = status(&confirm(&store, &plan.0, 7, "true", open()).await);
    assert_eq!(code, 400);
    assert_eq!(
        detail,
        "a BUY_INTRADAY line cannot be executed on this sleeve"
    );
}

#[tokio::test]
async fn a_second_confirm_of_the_same_line_is_refused() {
    let store = FakeStore::default();
    let plan = morning(&store);
    let (code, detail) = status(&confirm(&store, &plan.0, 5, "true", open()).await);
    assert_eq!(code, 409);
    assert_eq!(
        detail,
        format!(
            "line 5 is already REJECTED \u{2014} a line is confirmed once, and client_id {}:CCC \
             would be the gateway's second refusal",
            plan.0
        )
    );
}

// =============================================================================================
// No order reaches a broker in DRY_RUN (`TestNoOrderReachesABrokerInDryRun`)
// =============================================================================================

#[test]
fn the_gates_are_cnc_only_and_dry_run_unless_both_flags_say_live() {
    for dry_run in [true, false] {
        for execution in [false, true] {
            let g = x::twt_gates(&flags(dry_run, execution, false));
            assert!(!g.intraday_enabled, "MIS is never this sleeve's");
            assert!(!g.options_enabled, "NFO/BFO is never this sleeve's");
            assert!(!g.fno_carry_enabled);
            assert_eq!(g.dry_run, dry_run || !execution);
            assert_eq!(
                x::is_simulated(&flags(dry_run, execution, false)),
                g.dry_run
            );
        }
    }
    assert!(x::twt_gates(&Settings::defaults().unwrap()).dry_run);
}

#[test]
fn the_stop_band_and_limit_leg_are_the_engines() {
    let band = x::twt_stop_band().unwrap();
    assert_eq!(band, crate::gateway::StopBand::new(0.005, 0.30).unwrap());
    assert!((x::twt_limit_fraction() - 0.97).abs() < 1e-12);
}

#[tokio::test]
async fn drain_now_with_the_flag_off_does_nothing() {
    // The supervisor's entry point: the same three flags; with them off it opens nothing.
    let store = FakeStore::default();
    morning(&store);
    let mut seams = Seams::new(flags(true, false, false), &store);
    seams.refuse = true;
    let report = a::run_once(&seams, 0.0, 0.0).await.unwrap();
    assert!(!report.ran);
    assert!(report.reason.contains("auto-execute is off"));
    assert!(seams.reached.lock().unwrap().is_empty());
}
