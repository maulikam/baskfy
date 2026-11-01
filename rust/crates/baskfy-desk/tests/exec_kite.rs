//! Lane **exec_kite**: `app/kite_client.py` and `app/token_store.py`, ported test-first.
//!
//! The spec is the desk's own tests and the non-negotiables they guard:
//! * `tests/test_holdings_same_day.py` (all 15) — holdings = `quantity + t1_quantity +
//!   collateral_quantity` (non-negotiable 2), pledged surfaced as `pledged_qty`, information
//!   only (non-negotiable 3), today's CNC buys folded in net of overnight carry, a failed
//!   positions read refusing rather than under-reporting;
//! * `tests/test_seven_non_negotiables.py` 2, 3, 6 (the documented GTT exception: its own
//!   guard), 7 (an untouchable refused before any network call);
//! * `tests/test_kite_limits.py`'s client half — every request-carrying read takes its family's
//!   slot, the LTP cache keeps a polling page off Kite's 1 req/s endpoint;
//! * `kite_client.place_gtt_stop` / `delete_gtt`: DRY_RUN simulates and **never reaches the
//!   broker**; live (a fake transport, never Kite) snaps to the tick with the limit at 99.5%;
//! * `token_store.py`: encrypted at rest, the fallback key beside the token at 0600, a token
//!   written by another process picked up without a restart.
//!
//! Every test runs with no network: the broker is [`Recorder`], which records each call.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::{Arc, Mutex, RwLock};

use async_trait::async_trait;
use baskfy_desk::app::StateParts;
use baskfy_desk::clock::{Clock, FixedClock};
use baskfy_desk::ids::HarnessIds;
use baskfy_desk::kite::{
    GttParams, KiteError, KiteExceptionKind, KiteReads, KiteWrites, OrderParams,
};
use baskfy_desk::lanes::exec_core::exec_kite;
use baskfy_desk::lanes::exec_core::exec_kite::kite_client::{ClientError, DeskKite, NO_API_KEY};
use baskfy_desk::lanes::exec_core::exec_kite::token_store;
use baskfy_desk::lanes::exec_core::kite_limits::LimitClock;
use baskfy_desk::settings::{Settings, SharedSettings};
use baskfy_desk::storage::{Db, DbConfig};
use baskfy_desk::wire::{PyDict, PyValue};
use baskfy_desk::{AppState, DeskPaths, build_state};
use baskfy_providers::secret::Secret;

fn v(json: &str) -> PyValue {
    PyValue::from_json(json.as_bytes()).unwrap()
}

/// A broker that answers from fields and records every call (name, detail).
struct Recorder {
    calls: Mutex<Vec<String>>,
    tokens: Mutex<Vec<String>>,
    holdings: Mutex<PyValue>,
    positions: Mutex<Option<PyValue>>,
    ltp: Mutex<PyValue>,
    quote: Mutex<Option<PyValue>>,
    instruments: Mutex<PyValue>,
    margins: Mutex<PyValue>,
    profile_fails: Mutex<bool>,
    gtt_answer: Mutex<Option<Result<PyValue, String>>>,
    delete_fails: Mutex<bool>,
    session: Mutex<PyValue>,
    gtt_params: Mutex<Vec<PyDict>>,
}

impl Recorder {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            calls: Mutex::new(Vec::new()),
            tokens: Mutex::new(Vec::new()),
            holdings: Mutex::new(v("[]")),
            positions: Mutex::new(Some(v(r#"{"net": []}"#))),
            ltp: Mutex::new(v("{}")),
            quote: Mutex::new(None),
            instruments: Mutex::new(v("[]")),
            margins: Mutex::new(v("{}")),
            profile_fails: Mutex::new(false),
            gtt_answer: Mutex::new(None),
            delete_fails: Mutex::new(false),
            session: Mutex::new(v(r#"{"access_token": "tok-from-exchange"}"#)),
            gtt_params: Mutex::new(Vec::new()),
        })
    }

    fn log(&self, what: impl Into<String>) {
        self.calls.lock().unwrap().push(what.into());
    }

    fn calls(&self) -> Vec<String> {
        self.calls.lock().unwrap().clone()
    }

    fn writes(&self) -> Vec<String> {
        self.calls()
            .into_iter()
            .filter(|c| {
                [
                    "place_order",
                    "modify_order",
                    "cancel_order",
                    "place_gtt",
                    "modify_gtt",
                    "delete_gtt",
                ]
                .iter()
                .any(|w| c.starts_with(w))
            })
            .collect()
    }
}

fn net_err(msg: &str) -> KiteError {
    KiteError::kite(KiteExceptionKind::Network, msg)
}

#[async_trait]
impl KiteReads for Recorder {
    fn login_url(&self) -> String {
        self.log("login_url");
        "https://kite.zerodha.com/connect/login?api_key=test&v=3".to_owned()
    }
    fn set_access_token(&self, token: &Secret) {
        self.tokens.lock().unwrap().push(token.expose().to_owned());
    }
    async fn generate_session(&self, rt: &str, _s: &Secret) -> Result<PyValue, KiteError> {
        self.log(format!("generate_session {rt}"));
        Ok(self.session.lock().unwrap().clone())
    }
    async fn profile(&self) -> Result<PyValue, KiteError> {
        self.log("profile");
        if *self.profile_fails.lock().unwrap() {
            return Err(KiteError::kite(
                KiteExceptionKind::Token,
                "Incorrect api_key or access_token.",
            ));
        }
        Ok(v(r#"{"user_id": "T"}"#))
    }
    async fn holdings(&self) -> Result<PyValue, KiteError> {
        self.log("holdings");
        Ok(self.holdings.lock().unwrap().clone())
    }
    async fn positions(&self) -> Result<PyValue, KiteError> {
        self.log("positions");
        self.positions
            .lock()
            .unwrap()
            .clone()
            .ok_or_else(|| net_err("network"))
    }
    async fn margins(&self, segment: Option<&str>) -> Result<PyValue, KiteError> {
        self.log(format!("margins {segment:?}"));
        Ok(self.margins.lock().unwrap().clone())
    }
    async fn ltp(&self, instruments: &[String]) -> Result<PyValue, KiteError> {
        self.log(format!("ltp {}", instruments.join(",")));
        let all = self.ltp.lock().unwrap().clone();
        let PyValue::Map(all) = all else {
            return Ok(v("{}"));
        };
        let mut out = PyDict::new();
        for k in instruments {
            if let Some(x) = all.get(k) {
                out.insert(k.clone(), x.clone());
            }
        }
        Ok(PyValue::Map(out))
    }
    async fn ohlc(&self, _i: &[String]) -> Result<PyValue, KiteError> {
        self.log("ohlc");
        Ok(v("{}"))
    }
    async fn quote(&self, instruments: &[String]) -> Result<PyValue, KiteError> {
        self.log(format!("quote {}", instruments.len()));
        self.quote
            .lock()
            .unwrap()
            .clone()
            .ok_or_else(|| net_err("Too many requests"))
    }
    async fn instruments(&self, exchange: Option<&str>) -> Result<PyValue, KiteError> {
        self.log(format!("instruments {exchange:?}"));
        Ok(self.instruments.lock().unwrap().clone())
    }
    async fn historical_data(
        &self,
        token: i64,
        from: &str,
        to: &str,
        interval: &str,
        _c: bool,
        _oi: bool,
    ) -> Result<PyValue, KiteError> {
        self.log(format!("historical_data {token} {from} {to} {interval}"));
        Ok(v("[]"))
    }
    async fn orders(&self) -> Result<PyValue, KiteError> {
        self.log("orders");
        Ok(v("[]"))
    }
    async fn order_history(&self, id: &str) -> Result<PyValue, KiteError> {
        self.log(format!("order_history {id}"));
        Ok(v("[]"))
    }
    async fn trades(&self) -> Result<PyValue, KiteError> {
        self.log("trades");
        Ok(PyValue::None)
    }
    async fn get_gtts(&self) -> Result<PyValue, KiteError> {
        self.log("get_gtts");
        Ok(v("[]"))
    }
    async fn basket_order_margins(
        &self,
        _p: &[PyValue],
        _c: bool,
        _m: Option<&str>,
    ) -> Result<PyValue, KiteError> {
        self.log("basket_order_margins");
        Ok(v("[]"))
    }
}

#[async_trait]
impl KiteWrites for Recorder {
    async fn place_order(&self, order: &OrderParams) -> Result<String, KiteError> {
        self.log(format!("place_order {}", order.tradingsymbol));
        Ok("1".to_owned())
    }
    async fn modify_order(&self, _v: &str, id: &str, _p: &PyDict) -> Result<String, KiteError> {
        self.log(format!("modify_order {id}"));
        Ok(id.to_owned())
    }
    async fn cancel_order(
        &self,
        _v: &str,
        id: &str,
        _p: Option<&str>,
    ) -> Result<String, KiteError> {
        self.log(format!("cancel_order {id}"));
        Ok(id.to_owned())
    }
    async fn place_gtt(&self, gtt: &GttParams) -> Result<PyValue, KiteError> {
        self.log(format!("place_gtt {}", gtt.tradingsymbol));
        self.gtt_params.lock().unwrap().push(gtt.to_params()?);
        match self.gtt_answer.lock().unwrap().clone() {
            Some(Ok(x)) => Ok(x),
            Some(Err(m)) => Err(KiteError::kite(KiteExceptionKind::Input, m)),
            None => Ok(v(r#"{"trigger_id": 4242}"#)),
        }
    }
    async fn modify_gtt(&self, id: i64, _g: &GttParams) -> Result<PyValue, KiteError> {
        self.log(format!("modify_gtt {id}"));
        Ok(v("{}"))
    }
    async fn delete_gtt(&self, id: i64) -> Result<PyValue, KiteError> {
        self.log(format!("delete_gtt {id}"));
        if *self.delete_fails.lock().unwrap() {
            return Err(KiteError::kite(KiteExceptionKind::Input, "Invalid trigger"));
        }
        Ok(v(r#"{"trigger_id": 1}"#))
    }
}

/// `time.monotonic()` for the limiter and the LTP cache: settable, and a sleep only moves it.
#[derive(Default)]
struct Mono(Mutex<f64>);

impl Mono {
    fn advance(&self, seconds: f64) {
        *self.0.lock().unwrap() += seconds;
    }
}

#[async_trait]
impl LimitClock for Mono {
    fn now(&self) -> f64 {
        1_000.0 + *self.0.lock().unwrap()
    }
    async fn sleep(&self, seconds: f64) {
        self.advance(seconds.max(0.0));
    }
}

struct Desk {
    kite: DeskKite,
    broker: Arc<Recorder>,
    clock: Arc<FixedClock>,
    mono: Arc<Mono>,
    settings: SharedSettings,
    dir: tempfile::TempDir,
}

fn settings_for(dir: &std::path::Path, dry_run: bool) -> Settings {
    let mut s = Settings::defaults().unwrap();
    s.dry_run = dry_run;
    s.force_ipv4 = false;
    s.shared_read_limits = false;
    s.shared_read_limits_redis_url = String::new();
    s.kite_api_key = "test-key".to_owned();
    s.kite_api_secret = "test-secret".to_owned();
    s.token_file = dir.join("kite_token.json").display().to_string();
    s
}

fn desk_with(broker: Arc<Recorder>, dry_run: bool) -> Desk {
    let dir = tempfile::tempdir().unwrap();
    let settings: SharedSettings = Arc::new(RwLock::new(settings_for(dir.path(), dry_run)));
    let at = chrono::DateTime::parse_from_rfc3339("2026-10-30T11:00:00+05:30").unwrap();
    let clock = Arc::new(FixedClock::new(at));
    let mono = Arc::new(Mono::default());
    let kite = DeskKite::with_limit_clock(
        broker.clone(),
        settings.clone(),
        clock.clone() as Arc<dyn Clock>,
        mono.clone() as Arc<dyn LimitClock>,
    );
    Desk {
        kite,
        broker,
        clock,
        mono,
        settings,
        dir,
    }
}

fn desk(dry_run: bool) -> Desk {
    desk_with(Recorder::new(), dry_run)
}

fn s(x: &str) -> String {
    x.to_owned()
}

// =====================================================================================
// holdings — tests/test_holdings_same_day.py, one for one
// =====================================================================================

fn hold(sym: &str, qty: i64, t1: i64, collat: i64) -> String {
    format!(
        r#"{{"tradingsymbol": "{sym}", "exchange": "NSE", "quantity": {qty}, "t1_quantity": {t1},
            "collateral_quantity": {collat}, "average_price": 100.0, "last_price": 100.0}}"#
    )
}

fn pos(sym: &str, qty: i64, product: &str, overnight: Option<i64>) -> String {
    let on = overnight.map_or(String::new(), |o| format!(r#""overnight_quantity": {o},"#));
    format!(
        r#"{{"tradingsymbol": "{sym}", "exchange": "NSE", "quantity": {qty}, "product": "{product}",
            {on} "average_price": 100.0, "last_price": 100.0}}"#
    )
}

fn cnc(sym: &str, qty: i64) -> String {
    pos(sym, qty, "CNC", Some(0))
}

async fn held(holdings: &[String], positions: &[String]) -> Vec<(String, i64)> {
    let broker = Recorder::new();
    *broker.holdings.lock().unwrap() = v(&format!("[{}]", holdings.join(",")));
    *broker.positions.lock().unwrap() =
        Some(v(&format!(r#"{{"net": [{}]}}"#, positions.join(","))));
    let d = desk_with(broker, true);
    d.kite
        .holdings()
        .await
        .unwrap()
        .iter()
        .map(|r| {
            let PyValue::Str(sym) = r.get("symbol").unwrap() else {
                panic!()
            };
            let PyValue::Int(q) = r.get("quantity").unwrap() else {
                panic!("{r:?}")
            };
            (sym.clone(), *q)
        })
        .collect()
}

fn q(rows: &[(String, i64)], sym: &str) -> Option<i64> {
    rows.iter().find(|(k, _)| k == sym).map(|(_, q)| *q)
}

#[tokio::test]
async fn a_stock_bought_today_is_counted() {
    let r = held(&[], &[cnc("CUPID", 2536)]).await;
    assert_eq!(q(&r, "CUPID"), Some(2536));
}

#[tokio::test]
async fn a_sale_made_today_is_not_subtracted_twice() {
    let r = held(&[hold("SAILIFE", 286, 0, 149)], &[cnc("SAILIFE", -112)]).await;
    assert_eq!(q(&r, "SAILIFE"), Some(435));
}

#[tokio::test]
async fn a_full_exit_reads_as_zero_not_negative() {
    let r = held(&[hold("EMCURE", 0, 0, 0)], &[cnc("EMCURE", -97)]).await;
    assert!(q(&r, "EMCURE").is_none_or(|x| x == 0));
}

#[tokio::test]
async fn topping_up_an_existing_position_sums_both_sources() {
    let r = held(&[hold("LAURUSLABS", 108, 0, 0)], &[cnc("LAURUSLABS", 211)]).await;
    assert_eq!(q(&r, "LAURUSLABS"), Some(319));
}

#[tokio::test]
async fn pledged_shares_still_count() {
    let r = held(&[hold("ATHERENERG", 0, 0, 277)], &[]).await;
    assert_eq!(q(&r, "ATHERENERG"), Some(277));
}

#[tokio::test]
async fn t1_shares_still_count() {
    let r = held(&[hold("X", 10, 5, 2)], &[]).await;
    assert_eq!(q(&r, "X"), Some(17));
}

#[tokio::test]
async fn intraday_and_derivative_positions_are_ignored() {
    let r = held(
        &[],
        &[
            pos("NIFTY26AUG24000CE", 650, "MIS", Some(0)),
            pos("RELIANCE", 10, "MIS", Some(0)),
        ],
    )
    .await;
    assert!(r.is_empty(), "{r:?}");
}

#[tokio::test]
async fn a_zero_quantity_position_adds_nothing() {
    let r = held(&[hold("X", 50, 0, 0)], &[cnc("X", 0)]).await;
    assert_eq!(q(&r, "X"), Some(50));
}

#[tokio::test]
async fn a_failed_positions_call_raises_rather_than_omitting_todays_buys() {
    let broker = Recorder::new();
    *broker.holdings.lock().unwrap() = v(&format!("[{}]", hold("X", 50, 0, 0)));
    *broker.positions.lock().unwrap() = None;
    let d = desk_with(broker, true);
    let err = d.kite.holdings().await.unwrap_err();
    assert_eq!(err.python_type, "RuntimeError");
    assert!(err.message.contains("today's purchases"), "{}", err.message);
}

#[tokio::test]
async fn the_whole_18_aug_plan_reconciles() {
    let r = held(
        &[
            hold("SAILIFE", 286, 0, 149),
            hold("HFCL", 1308, 0, 0),
            hold("LAURUSLABS", 108, 0, 0),
            hold("WELCORP", 302, 0, 0),
            hold("EMCURE", 0, 0, 0),
        ],
        &[
            cnc("CUPID", 2536),
            cnc("HSCL", 864),
            cnc("SANSERA", 172),
            cnc("SAILIFE", -112),
            cnc("HFCL", -835),
            cnc("EMCURE", -97),
        ],
    )
    .await;
    assert_eq!(q(&r, "CUPID"), Some(2536));
    assert_eq!(q(&r, "HSCL"), Some(864));
    assert_eq!(q(&r, "SANSERA"), Some(172));
    assert_eq!(q(&r, "SAILIFE"), Some(435));
    assert_eq!(q(&r, "HFCL"), Some(1308));
    assert_eq!(q(&r, "WELCORP"), Some(302));
    assert_eq!(q(&r, "EMCURE").unwrap_or(0), 0);
}

#[tokio::test]
async fn a_position_carried_overnight_is_not_added_to_holdings_again() {
    let r = held(
        &[hold("CUPID", 0, 2509, 0)],
        &[pos("CUPID", 2509, "CNC", Some(2509))],
    )
    .await;
    assert_eq!(
        q(&r, "CUPID"),
        Some(2509),
        "the settled position was counted twice"
    );
}

#[tokio::test]
async fn a_same_day_buy_is_still_folded_in() {
    let r = held(&[], &[pos("SANSERA", 172, "CNC", Some(0))]).await;
    assert_eq!(q(&r, "SANSERA"), Some(172));
}

#[tokio::test]
async fn a_partly_carried_position_folds_only_the_part_bought_today() {
    let r = held(
        &[hold("ANANDRATHI", 0, 500, 0)],
        &[pos("ANANDRATHI", 679, "CNC", Some(500))],
    )
    .await;
    assert_eq!(q(&r, "ANANDRATHI"), Some(679));
}

#[tokio::test]
async fn a_missing_overnight_field_degrades_to_the_previous_behaviour() {
    let r = held(&[], &[pos("HSCL", 864, "CNC", None)]).await;
    assert_eq!(q(&r, "HSCL"), Some(864));
}

#[tokio::test]
async fn a_same_day_sell_is_still_never_subtracted_twice() {
    let r = held(
        &[hold("SAILIFE", 435, 0, 0)],
        &[pos("SAILIFE", -112, "CNC", Some(0))],
    )
    .await;
    assert_eq!(q(&r, "SAILIFE"), Some(435));
}

// =====================================================================================
// non-negotiables 2 and 3: the row's shape — pledged is surfaced, never a gate
// =====================================================================================

#[tokio::test]
async fn non_negotiable_2_and_3_the_row_counts_pledged_and_t1_and_surfaces_pledged_qty() {
    let broker = Recorder::new();
    *broker.holdings.lock().unwrap() = v(&format!(
        "[{}, {}]",
        hold("KAYNES", 30, 4, 5),
        hold("GONE", 0, 0, 0)
    ));
    let d = desk_with(broker, true);
    let rows = d.kite.holdings().await.unwrap();
    assert_eq!(rows.len(), 1, "a zero total is not a holding");
    let keys: Vec<&str> = rows[0].iter().map(|(k, _)| k).collect();
    assert_eq!(
        keys,
        [
            "symbol",
            "exchange",
            "quantity",
            "pledged_qty",
            "average_price",
            "last_price"
        ]
    );
    assert_eq!(rows[0].get("quantity"), Some(&PyValue::Int(39)));
    assert_eq!(rows[0].get("pledged_qty"), Some(&PyValue::Int(5)));
}

#[tokio::test]
async fn a_same_day_buy_with_no_holding_gets_the_positions_row_shape() {
    let broker = Recorder::new();
    *broker.positions.lock().unwrap() = Some(v(
        r#"{"net": [{"tradingsymbol": "NEW", "quantity": 7, "product": "CNC",
                     "overnight_quantity": 0, "average_price": 0, "last_price": null}]}"#,
    ));
    let d = desk_with(broker, true);
    let rows = d.kite.holdings().await.unwrap();
    assert_eq!(
        PyValue::Map(rows[0].clone()),
        v(
            r#"{"symbol": "NEW", "exchange": "NSE", "quantity": 7, "pledged_qty": 0,
              "average_price": 0.0, "last_price": 0.0}"#
        )
    );
}

// =====================================================================================
// the documented GTT exception (non-negotiable 6) and the untouchables (7)
// =====================================================================================

#[tokio::test]
async fn dry_run_gtt_stop_simulates_end_to_end_and_never_reaches_the_broker() {
    let d = desk(true);
    let out = d
        .kite
        .place_gtt_stop("INFY", 12, 1380.37, 1500.0, "NSE")
        .await
        .unwrap();
    assert_eq!(
        PyValue::Map(out),
        v(r#"{"symbol": "INFY", "status": "DRY_RUN_GTT", "trigger": 1380.37, "qty": 12}"#)
    );
    assert!(
        d.broker.calls().is_empty(),
        "DRY_RUN reached the broker: {:?}",
        d.broker.calls()
    );
}

#[tokio::test]
async fn dry_run_is_the_default() {
    assert!(Settings::defaults().unwrap().dry_run);
}

/// `C.DRY_RUN` is read at the call, not cached at construction: flipping the shared settings
/// back to DRY_RUN (the settings page, an operator) simulates the very next GTT write.
#[tokio::test]
async fn dry_run_is_read_at_each_call_not_at_construction() {
    let d = desk(false);
    d.settings.write().unwrap().dry_run = true;
    let out = d
        .kite
        .place_gtt_stop("INFY", 5, 1380.0, 1500.0, "NSE")
        .await
        .unwrap();
    assert_eq!(out.get("status"), Some(&PyValue::Str(s("DRY_RUN_GTT"))));
    let out = d.kite.delete_gtt(3, "INFY").await.unwrap();
    assert_eq!(
        out.get("status"),
        Some(&PyValue::Str(s("DRY_RUN_GTT_DELETE")))
    );
    assert!(d.broker.calls().is_empty(), "{:?}", d.broker.calls());
}

/// The documented exception is GTT-only: a live stop and a live cancel use the GTT verbs and
/// never an order verb (`place_order` / `modify_order` / `cancel_order`).
#[tokio::test]
async fn the_gtt_exception_never_reaches_an_order_verb() {
    let d = desk(false);
    d.kite
        .place_gtt_stop("INFY", 5, 1380.0, 1500.0, "NSE")
        .await
        .unwrap();
    d.kite.delete_gtt(3, "INFY").await.unwrap();
    let writes = d.broker.writes();
    assert_eq!(writes.len(), 2, "{writes:?}");
    assert!(
        writes
            .iter()
            .all(|w| w.starts_with("place_gtt") || w.starts_with("delete_gtt")),
        "{writes:?}"
    );
}

#[tokio::test]
async fn dry_run_gtt_delete_never_reaches_the_broker() {
    let d = desk(true);
    let out = d.kite.delete_gtt(77, "INFY").await.unwrap();
    assert_eq!(
        PyValue::Map(out),
        v(r#"{"symbol": "INFY", "gtt_id": 77, "status": "DRY_RUN_GTT_DELETE"}"#)
    );
    assert!(d.broker.calls().is_empty());
}

#[tokio::test]
async fn an_untouchable_is_refused_by_the_gtt_path_before_any_network_call() {
    for dry_run in [true, false] {
        let d = desk(dry_run);
        for sym in ["SGBDE31III", "SGBDE31III-GB", "SGBJUN29"] {
            let e = d
                .kite
                .place_gtt_stop(sym, 1, 6000.0, 7000.0, "NSE")
                .await
                .unwrap_err();
            assert_eq!(e.python_type, "UntouchableInstrumentError", "{sym}");
            let e = d.kite.delete_gtt(5, sym).await.unwrap_err();
            assert_eq!(e.python_type, "UntouchableInstrumentError", "{sym}");
        }
        assert!(d.broker.calls().is_empty(), "{:?}", d.broker.calls());
    }
}

#[tokio::test]
async fn an_option_gtt_is_an_overnight_option_and_is_refused() {
    let d = desk(false);
    let e = d
        .kite
        .place_gtt_stop("NIFTY26AUG24000CE", 75, 90.0, 120.0, "NFO")
        .await
        .unwrap_err();
    assert_eq!(e.python_type, "OvernightOptionError");
    assert!(d.broker.calls().is_empty());
}

#[tokio::test]
async fn delete_without_a_symbol_skips_only_the_guard() {
    let d = desk(false);
    let out = d.kite.delete_gtt(9, "").await.unwrap();
    assert_eq!(
        PyValue::Map(out),
        v(r#"{"symbol": "", "gtt_id": 9, "status": "GTT_DELETED"}"#)
    );
    assert_eq!(d.broker.calls(), [s("delete_gtt 9")]);
}

#[tokio::test]
async fn a_live_delete_that_kite_refuses_is_reported_not_raised() {
    let d = desk(false);
    *d.broker.delete_fails.lock().unwrap() = true;
    let out = d.kite.delete_gtt(9, "INFY").await.unwrap();
    assert_eq!(
        PyValue::Map(out),
        v(
            r#"{"symbol": "INFY", "gtt_id": 9, "status": "GTT_DELETE_ERROR", "error": "Invalid trigger"}"#
        )
    );
}

#[tokio::test]
async fn a_live_gtt_snaps_trigger_and_limit_to_the_instruments_tick() {
    // OFSS's tick is 1.00: "Trigger price should be a multiple of tick size 1.00" cost it its
    // stop on the first live arming run.
    let d = desk(false);
    *d.broker.instruments.lock().unwrap() = v(
        r#"[{"tradingsymbol": "OFSS", "tick_size": 1.0}, {"tradingsymbol": "INFY", "tick_size": 0.05}]"#,
    );
    let out = d
        .kite
        .place_gtt_stop("OFSS", 3, 9123.4, 10000.0, "NSE")
        .await
        .unwrap();
    assert_eq!(
        PyValue::Map(out),
        v(
            r#"{"symbol": "OFSS", "status": "GTT_PLACED", "gtt_id": 4242, "trigger": 9123.0, "limit": 9077.0}"#
        )
    );
    assert_eq!(
        d.broker.calls(),
        [s("instruments Some(\"NSE\")"), s("place_gtt OFSS")]
    );
    let params = d.broker.gtt_params.lock().unwrap()[0].clone();
    assert_eq!(
        PyValue::Map(params),
        v(
            r#"{"condition": "{\"exchange\": \"NSE\", \"tradingsymbol\": \"OFSS\", \"trigger_values\": [9123.0], \"last_price\": 10000.0}",
              "orders": "[{\"exchange\": \"NSE\", \"tradingsymbol\": \"OFSS\", \"transaction_type\": \"SELL\", \"quantity\": 3, \"order_type\": \"LIMIT\", \"product\": \"CNC\", \"price\": 9077.0}]",
              "type": "single"}"#
        )
    );
    // The tick table is cached for the process: a second stop costs no instruments call.
    d.kite
        .place_gtt_stop("INFY", 10, 1380.37, 1500.0, "NSE")
        .await
        .unwrap();
    assert_eq!(d.broker.calls().len(), 3);
    let p2 = d.broker.gtt_params.lock().unwrap()[1].clone();
    assert!(
        matches!(p2.get("orders"), Some(PyValue::Str(o)) if o.contains("\"price\": 1373.45")),
        "{p2:?}"
    );
    assert_eq!(d.broker.writes().len(), 2);
}

#[tokio::test]
async fn an_unknown_symbol_uses_the_default_tick() {
    let d = desk(false);
    let out = d
        .kite
        .place_gtt_stop("ZZZ", 1, 100.123, 110.0, "NSE")
        .await
        .unwrap();
    assert_eq!(out.get("trigger"), Some(&PyValue::Float(100.1)));
    assert_eq!(out.get("limit"), Some(&PyValue::Float(99.6)));
}

#[tokio::test]
async fn a_live_gtt_kite_refuses_is_a_gtt_error_not_an_exception() {
    let d = desk(false);
    *d.broker.gtt_answer.lock().unwrap() = Some(Err(
        "Trigger price should be a multiple of tick size 1.00".to_owned(),
    ));
    let out = d
        .kite
        .place_gtt_stop("OFSS", 1, 9000.0, 10000.0, "NSE")
        .await
        .unwrap();
    assert_eq!(
        PyValue::Map(out),
        v(
            r#"{"symbol": "OFSS", "status": "GTT_ERROR", "error": "Trigger price should be a multiple of tick size 1.00"}"#
        )
    );
    *d.broker.gtt_answer.lock().unwrap() = Some(Ok(v(r#"{"status": "ok"}"#)));
    let out = d
        .kite
        .place_gtt_stop("OFSS", 1, 9000.0, 10000.0, "NSE")
        .await
        .unwrap();
    assert_eq!(
        out.get("error"),
        Some(&PyValue::Str("'trigger_id'".to_owned()))
    );
}

// =====================================================================================
// reads: each takes its family's slot (SW20), the LTP cache, quotes' shape
// =====================================================================================

#[tokio::test]
async fn every_read_takes_its_familys_slot() {
    let d = desk(true);
    *d.broker.margins.lock().unwrap() = v(r#"{"equity": {"available": {"live_balance": 1234.5}}}"#);
    *d.broker.quote.lock().unwrap() = Some(v("{}"));
    let k = &d.kite;
    k.holdings().await.unwrap();
    k.trades().await.unwrap();
    assert_eq!(k.available_cash().await.unwrap(), 1234.5);
    k.ltp(&[s("INFY")], "NSE").await.unwrap();
    k.quotes(&[s("INFY")], "NSE").await.unwrap();
    k.instruments("NSE").await.unwrap();
    k.historical(408_065, "2026-10-01", "2026-10-29", "day")
        .await
        .unwrap();
    k.margins(Some("equity")).await.unwrap();
    k.orders().await.unwrap();
    k.order_history("1").await.unwrap();
    k.quote_raw(&[s("NSE:INFY")]).await.unwrap();
    k.get_gtts().await.unwrap();
    assert!(k.is_authed().await);
    assert_eq!(
        k.slots_taken(),
        [
            "general",
            "general",
            "general",
            "general",
            "quote",
            "quote",
            "general",
            "historical",
            "general",
            "general",
            "general",
            "quote",
            "general",
            "general"
        ]
    );
    // login_url costs Kite nothing and takes no slot.
    let _ = k.login_url();
    assert_eq!(k.slots_taken().len(), 14);
    assert!(d.broker.writes().is_empty());
}

#[tokio::test]
async fn is_authed_is_false_when_the_session_is_refused() {
    let d = desk(true);
    *d.broker.profile_fails.lock().unwrap() = true;
    assert!(!d.kite.is_authed().await);
}

#[tokio::test]
async fn ltp_is_served_from_cache_inside_the_ttl_and_refetched_after() {
    let d = desk(true);
    *d.broker.ltp.lock().unwrap() =
        v(r#"{"NSE:INFY": {"last_price": 1500.5}, "NSE:TCS": {"last_price": 4000}}"#);
    let first = d
        .kite
        .ltp(&[s("INFY"), s(""), s("INFY"), s("TCS")], "NSE")
        .await
        .unwrap();
    assert_eq!(PyValue::Map(first), v(r#"{"INFY": 1500.5, "TCS": 4000.0}"#));
    assert_eq!(d.broker.calls(), [s("ltp NSE:INFY,NSE:TCS")]);
    d.clock.advance(4.9);
    let again = d.kite.ltp(&[s("TCS"), s("INFY")], "NSE").await.unwrap();
    assert_eq!(PyValue::Map(again), v(r#"{"TCS": 4000.0, "INFY": 1500.5}"#));
    assert_eq!(
        d.broker.calls().len(),
        1,
        "a poll inside the TTL cost a Kite call"
    );
    // Only the missing name is asked for.
    *d.broker.ltp.lock().unwrap() =
        v(r#"{"NSE:WIPRO": {"last_price": 250.0}, "NSE:INFY": {"last_price": 1.0}}"#);
    d.kite.ltp(&[s("INFY"), s("WIPRO")], "NSE").await.unwrap();
    assert_eq!(d.broker.calls()[1], "ltp NSE:WIPRO");
    d.clock.advance(0.2);
    let late = d.kite.ltp(&[s("INFY")], "NSE").await.unwrap();
    assert_eq!(
        late.get("INFY"),
        Some(&PyValue::Float(1.0)),
        "past the TTL the price is refetched"
    );
}

#[tokio::test]
async fn quotes_are_symbol_keyed_and_a_failed_batch_is_skipped() {
    let d = desk(true);
    let empty = d.kite.quotes(&[s("INFY")], "NSE").await.unwrap();
    assert!(
        empty.is_empty(),
        "a failed batch must not take the panel down"
    );
    *d.broker.quote.lock().unwrap() = Some(v(
        r#"{"NSE:INFY": {"last_price": 1500.5, "net_change": 3.5, "volume": 0, "volume_traded": 99,
                         "ohlc": {"open": 1490, "high": 1510, "low": 1480, "close": 1497}}}"#,
    ));
    let out = d.kite.quotes(&[s("INFY")], "NSE").await.unwrap();
    assert_eq!(
        PyValue::Map(out),
        v(
            r#"{"INFY": {"last_price": 1500.5, "net_change": 3.5, "open": 1490, "high": 1510,
                       "low": 1480, "prev_close": 1497, "volume": 99}}"#
        )
    );
}

// =====================================================================================
// the token: encrypted at rest, the fallback key, picked up without a restart
// =====================================================================================

#[tokio::test]
async fn the_login_exchange_stores_the_token_encrypted_and_uses_it() {
    let d = desk(true);
    d.kite.exchange_token("req-1").await.unwrap();
    assert_eq!(d.broker.calls(), [s("generate_session req-1")]);
    assert_eq!(*d.broker.tokens.lock().unwrap(), [s("tok-from-exchange")]);
    let token_file = d.dir.path().join("kite_token.json");
    let blob = std::fs::read_to_string(&token_file).unwrap();
    assert!(
        !blob.contains("tok-from-exchange"),
        "the token is on disk in the clear"
    );
    let key = d.dir.path().join("kite_token.json.key");
    assert!(
        key.is_file(),
        "no key configured: the fallback key sits beside the token"
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt as _;
        assert_eq!(
            std::fs::metadata(&key).unwrap().permissions().mode() & 0o777,
            0o600
        );
        assert_eq!(
            std::fs::metadata(&token_file).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }
    // A fresh process loads it at start (`kite()`'s first call is `Kite()`).
    let broker = Recorder::new();
    let again = DeskKite::new(
        broker.clone(),
        d.settings.clone(),
        d.clock.clone() as Arc<dyn Clock>,
    );
    again.ready().unwrap();
    assert_eq!(*broker.tokens.lock().unwrap(), [s("tok-from-exchange")]);
    assert!(
        !again.refresh_token_if_changed().unwrap(),
        "nothing changed on disk"
    );
}

#[tokio::test]
async fn a_token_written_by_another_process_is_picked_up_without_a_restart() {
    let d = desk(true);
    assert!(!d.kite.refresh_token_if_changed().unwrap(), "no blob yet");
    // The morning login, by another process.
    let other = Recorder::new();
    let writer = DeskKite::new(other, d.settings.clone(), d.clock.clone() as Arc<dyn Clock>);
    writer.exchange_token("req-2").await.unwrap();
    assert!(d.kite.is_authed().await);
    assert_eq!(*d.broker.tokens.lock().unwrap(), [s("tok-from-exchange")]);
    assert!(!d.kite.refresh_token_if_changed().unwrap());
}

#[test]
fn an_unreadable_token_is_the_same_as_none() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(dir.path().join("kite_token.json"), "not a fernet blob").unwrap();
    let settings: SharedSettings = Arc::new(RwLock::new(settings_for(dir.path(), true)));
    let broker = Recorder::new();
    let at = chrono::DateTime::parse_from_rfc3339("2026-10-30T09:00:00+05:30").unwrap();
    let k = DeskKite::new(broker.clone(), settings, Arc::new(FixedClock::new(at)));
    k.ready()
        .expect("an unreadable token is not a refused start");
    assert!(broker.tokens.lock().unwrap().is_empty());
}

#[test]
fn the_fallback_key_is_generated_once_at_0600_and_reused() {
    let dir = tempfile::tempdir().unwrap();
    let token = dir.path().join("sub").join(".kite_token.json");
    let k1 = token_store::bootstrap_key(&token).unwrap();
    let k2 = token_store::bootstrap_key(&token).unwrap();
    assert_eq!(k1, k2);
    assert_eq!(
        k1.len(),
        44,
        "Fernet.generate_key(): 32 bytes, url-safe base64, padded"
    );
    assert!(k1.ends_with('='));
    let key_path = token_store::key_path(&token);
    assert_eq!(
        key_path,
        dir.path().join("sub").join(".kite_token.json.key")
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt as _;
        assert_eq!(
            std::fs::metadata(&key_path).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }
    assert_eq!(
        token_store::key_path(std::path::Path::new("data/token")),
        std::path::Path::new("data/token.key")
    );
}

#[test]
fn a_configured_key_wins_and_no_key_file_is_written() {
    let dir = tempfile::tempdir().unwrap();
    let token = dir.path().join("t.json");
    let configured = token_store::generate_key();
    let store = token_store::store_for(&token, &configured).unwrap();
    store.save("abc", None).unwrap();
    assert!(!token_store::key_path(&token).exists());
    let other = token_store::store_for(&token, &configured).unwrap();
    assert_eq!(other.load().unwrap().value.expose(), "abc");
    let wrong = token_store::store_for(&token, &token_store::generate_key()).unwrap();
    assert!(wrong.load().is_err(), "a rotated key must not decrypt");
}

// =====================================================================================
// `kite()` is lazy: `Kite()` refuses without KITE_API_KEY, runs once, loads the token then
// =====================================================================================

/// A desk started without `KITE_API_KEY` (a desk started with one has run `Kite()` already, and
/// Python's `_kite` stays built whatever the settings say afterwards).
fn unconfigured() -> Desk {
    let dir = tempfile::tempdir().unwrap();
    let mut st = settings_for(dir.path(), true);
    st.kite_api_key = String::new();
    let settings: SharedSettings = Arc::new(RwLock::new(st));
    let at = chrono::DateTime::parse_from_rfc3339("2026-10-30T11:00:00+05:30").unwrap();
    let clock = Arc::new(FixedClock::new(at));
    let mono = Arc::new(Mono::default());
    let broker = Recorder::new();
    let kite = DeskKite::with_limit_clock(
        broker.clone(),
        settings.clone(),
        clock.clone() as Arc<dyn Clock>,
        mono.clone() as Arc<dyn LimitClock>,
    );
    Desk {
        kite,
        broker,
        clock,
        mono,
        settings,
        dir,
    }
}

fn is_no_key<T: std::fmt::Debug>(r: Result<T, ClientError>) {
    let e = r.expect_err("Kite() must refuse without KITE_API_KEY");
    assert_eq!(e.python_type, "RuntimeError");
    assert_eq!(e.message, NO_API_KEY);
}

/// Python's `kite()` raises `RuntimeError("KITE_API_KEY missing — …")` before any method runs,
/// so every route that reaches the wrapper refuses the same way — the GTT writes included, before
/// their guard and whatever DRY_RUN says — and nothing reaches the broker or the disk.
#[tokio::test]
async fn without_an_api_key_every_method_raises_kites_runtime_error() {
    let d = unconfigured();
    let k = &d.kite;
    is_no_key(k.ready());
    is_no_key(k.login_url());
    is_no_key(k.refresh_token_if_changed());
    is_no_key(k.exchange_token("req").await);
    is_no_key(k.try_is_authed().await);
    assert!(!k.is_authed().await);
    is_no_key(k.holdings().await);
    is_no_key(k.trades().await);
    is_no_key(k.available_cash().await);
    is_no_key(k.ltp(&[s("INFY")], "NSE").await);
    is_no_key(k.quotes(&[s("INFY")], "NSE").await);
    is_no_key(k.instruments("NSE").await);
    is_no_key(k.historical(1, "2026-10-01", "2026-10-02", "day").await);
    is_no_key(k.margins(None).await);
    is_no_key(k.orders().await);
    is_no_key(k.order_history("1").await);
    is_no_key(k.quote_raw(&[s("NSE:INFY")]).await);
    is_no_key(k.get_gtts().await);
    is_no_key(k.tick_size("INFY", "NSE").await);
    is_no_key(k.place_gtt_stop("INFY", 1, 100.0, 110.0, "NSE").await);
    is_no_key(k.delete_gtt(1, "INFY").await);
    // An untouchable is refused too — by `kite()`, which runs before the guard, as in Python.
    is_no_key(k.place_gtt_stop("SGBAUG29", 1, 100.0, 110.0, "NSE").await);
    assert!(d.broker.calls().is_empty(), "{:?}", d.broker.calls());
    assert!(k.slots_taken().is_empty());
    assert!(
        !token_store::key_path(&d.dir.path().join("kite_token.json")).exists(),
        "Kite() never ran, so no fallback key may be written"
    );
    // The key arrives (an operator fills .env and the settings reload): the next call builds it.
    d.settings.write().unwrap().kite_api_key = s("test-key");
    k.ready().unwrap();
    assert!(token_store::key_path(&d.dir.path().join("kite_token.json")).exists());
}

/// Unconfigured, building the gateway's handle loads nothing and writes nothing (no fallback
/// key at boot): Python's `Kite()` would have raised before its token load.
#[tokio::test]
async fn an_unconfigured_handle_writes_nothing_at_boot() {
    let dir = tempfile::tempdir().unwrap();
    let mut st = settings_for(dir.path(), true);
    st.kite_api_key = String::new();
    let at = chrono::DateTime::parse_from_rfc3339("2026-10-30T09:00:00+05:30").unwrap();
    let broker = Recorder::new();
    let _k = DeskKite::new(
        broker.clone(),
        Arc::new(RwLock::new(st)),
        Arc::new(FixedClock::new(at)),
    );
    assert!(!token_store::key_path(&dir.path().join("kite_token.json")).exists());
    assert!(broker.tokens.lock().unwrap().is_empty());
}

/// `Kite()` runs once per process: a second `kite()` does not reload the token (a rotated token
/// is `refresh_token_if_changed`'s job).
#[tokio::test]
async fn kite_is_built_once() {
    let d = desk(true);
    d.kite.exchange_token("req-1").await.unwrap();
    let broker = Recorder::new();
    let k = DeskKite::new(
        broker.clone(),
        d.settings.clone(),
        d.clock.clone() as Arc<dyn Clock>,
    );
    k.ready().unwrap();
    k.ready().unwrap();
    k.login_url().unwrap();
    assert_eq!(broker.tokens.lock().unwrap().len(), 1);
}

/// `FORCE_IPV4` pins when the handle is built — before any session exists, and before the
/// key check (the safe direction).
#[tokio::test]
async fn force_ipv4_pins_when_the_handle_is_built() {
    use baskfy_desk::lanes::exec_core::net;
    let dir = tempfile::tempdir().unwrap();
    let mut st = settings_for(dir.path(), true);
    st.force_ipv4 = true;
    st.kite_api_key = String::new();
    let settings: SharedSettings = Arc::new(RwLock::new(st));
    let at = chrono::DateTime::parse_from_rfc3339("2026-10-30T09:00:00+05:30").unwrap();
    let k = DeskKite::new(Recorder::new(), settings, Arc::new(FixedClock::new(at)));
    assert!(net::ipv4_only());
    is_no_key(k.ready());
}

/// A token store that cannot be built (its fallback key cannot be written) makes `Kite()`
/// raise — Python's `_load_token` calls `store_for` outside its `try` — and leaves nothing
/// built, so the next call tries again.
#[tokio::test]
async fn a_store_that_cannot_be_built_refuses_kite_and_the_next_call_retries() {
    let dir = tempfile::tempdir().unwrap();
    let blocker = dir.path().join("afile");
    std::fs::write(&blocker, "x").unwrap();
    let mut st = settings_for(dir.path(), true);
    st.token_file = blocker.join("kite_token.json").display().to_string();
    let settings: SharedSettings = Arc::new(RwLock::new(st));
    let at = chrono::DateTime::parse_from_rfc3339("2026-10-30T09:00:00+05:30").unwrap();
    let broker = Recorder::new();
    let k = DeskKite::new(
        broker.clone(),
        settings.clone(),
        Arc::new(FixedClock::new(at)),
    );
    let e = k
        .ready()
        .expect_err("the key file cannot be created under a file");
    assert!(
        ["NotADirectoryError", "FileExistsError", "OSError"].contains(&e.python_type.as_str()),
        "{e:?}"
    );
    assert!(e.message.starts_with("[Errno "), "{e:?}");
    let e2 = k.holdings().await.expect_err("still unbuilt");
    assert_eq!(e2.python_type, e.python_type);
    assert!(broker.calls().is_empty());
    settings.write().unwrap().token_file = dir.path().join("t.json").display().to_string();
    k.ready().unwrap();
}

/// The LTP cache ages on the desk clock's `monotonic()` (the harness's settable monotonic, as
/// Python's `app.kite_client.time.monotonic`), never on the read limiter's clock.
#[tokio::test]
async fn the_ltp_cache_ages_on_the_desk_clocks_monotonic_not_the_limiters() {
    let d = desk(true);
    *d.broker.ltp.lock().unwrap() = v(r#"{"NSE:INFY": {"last_price": 1500.5}}"#);
    d.kite.ltp(&[s("INFY")], "NSE").await.unwrap();
    d.mono.advance(3_600.0);
    d.kite.ltp(&[s("INFY")], "NSE").await.unwrap();
    assert_eq!(d.broker.calls().len(), 1, "{:?}", d.broker.calls());
    d.clock.advance(5.0);
    d.kite.ltp(&[s("INFY")], "NSE").await.unwrap();
    assert_eq!(d.broker.calls().len(), 2, "{:?}", d.broker.calls());
}

// =====================================================================================
// `/status`'s session half (`exec_kite::status_session`), through the wrapper
// =====================================================================================

async fn state_with(broker: Arc<Recorder>, api_key: &str) -> (AppState, tempfile::TempDir) {
    let dir = tempfile::tempdir().unwrap();
    let mut st = settings_for(dir.path(), true);
    st.kite_api_key = api_key.to_owned();
    let db = Db::open(DbConfig {
        backend: "sqlite".to_owned(),
        schema: String::new(),
        database_url: String::new(),
        db_path: dir.path().join("desk.db"),
    })
    .await
    .unwrap();
    let at = chrono::DateTime::parse_from_rfc3339("2026-10-30T11:00:00+05:30").unwrap();
    let state = build_state(StateParts {
        settings: st,
        clock: Arc::new(FixedClock::new(at)),
        ids: Arc::new(HarnessIds::new("exec-kite")),
        db,
        kite: broker,
        paths: DeskPaths::under(dir.path()),
        harness: None,
    });
    (state, dir)
}

/// Python's `/status`: `is_authed()` twice (so `profile` twice, each behind a token re-read and
/// a `general` slot), then `available_cash()`.
#[tokio::test]
async fn status_reads_the_session_through_the_wrapper_as_python_does() {
    let broker = Recorder::new();
    *broker.margins.lock().unwrap() = v(r#"{"equity": {"available": {"live_balance": 1234.5}}}"#);
    let (state, _dir) = state_with(broker.clone(), "test-key").await;
    let out = exec_kite::status_session(&state).await;
    assert_eq!(PyValue::Map(out), v(r#"{"authed": true, "cash": 1234.5}"#));
    assert_eq!(
        broker.calls(),
        [s("profile"), s("profile"), s("margins None")]
    );
    let k = exec_kite::kite(&state).unwrap();
    assert_eq!(k.slots_taken(), ["general", "general", "general"]);
}

#[tokio::test]
async fn status_when_the_session_is_refused_reads_no_cash() {
    let broker = Recorder::new();
    *broker.profile_fails.lock().unwrap() = true;
    let (state, _dir) = state_with(broker.clone(), "test-key").await;
    let out = exec_kite::status_session(&state).await;
    assert_eq!(PyValue::Map(out), v(r#"{"authed": false, "cash": null}"#));
    assert_eq!(broker.calls(), [s("profile"), s("profile")]);
}

#[tokio::test]
async fn status_without_an_api_key_is_kites_refusal() {
    let broker = Recorder::new();
    let (state, dir) = state_with(broker.clone(), "").await;
    let out = exec_kite::status_session(&state).await;
    assert_eq!(out.get("authed"), Some(&PyValue::Bool(false)), "{out:?}");
    assert_eq!(out.get("error"), Some(&PyValue::Str(s(NO_API_KEY))));
    assert_eq!(out.len(), 2);
    assert!(broker.calls().is_empty());
    assert!(!token_store::key_path(&dir.path().join("kite_token.json")).exists());
}

#[tokio::test]
async fn status_with_a_malformed_margins_payload_reports_pythons_error() {
    let broker = Recorder::new();
    *broker.margins.lock().unwrap() = v("{}");
    let (state, _dir) = state_with(broker.clone(), "test-key").await;
    let out = exec_kite::status_session(&state).await;
    assert_eq!(
        PyValue::Map(out),
        v(r#"{"authed": false, "error": "'equity'"}"#)
    );
}

// =====================================================================================
// the encryption key: process environment first (even empty), then `.env`
// =====================================================================================

#[test]
fn the_key_resolves_as_os_getenv_after_load_dotenv() {
    use token_store::resolve_env_key;
    assert_eq!(resolve_env_key(None, None), "");
    assert_eq!(resolve_env_key(None, Some("from-dotenv")), "from-dotenv");
    assert_eq!(
        resolve_env_key(Some(s("from-env")), Some("from-dotenv")),
        "from-env"
    );
    // load_dotenv() never overrides a variable the process has, even an empty one.
    assert_eq!(
        resolve_env_key(Some(String::new()), Some("from-dotenv")),
        ""
    );
}

#[test]
fn os_errors_read_as_pythons() {
    let e = std::io::Error::from_raw_os_error(13);
    let (t, m) = token_store::os_error(&e, std::path::Path::new("/x/k.key"));
    assert_eq!(t, "PermissionError");
    assert_eq!(m, "[Errno 13] Permission denied: '/x/k.key'");
    let e = std::io::Error::from_raw_os_error(2);
    assert_eq!(
        token_store::os_error(&e, std::path::Path::new("k")).0,
        "FileNotFoundError"
    );
}

// =====================================================================================
// the lanes' raw reads (`AppState.kite`) take the same limiter's slots (SW20)
// =====================================================================================

#[tokio::test]
async fn lane_reads_take_their_family_slot_from_the_wrappers_limiter() {
    use baskfy_desk::lanes::exec_core::limited_reads;
    let broker = Recorder::new();
    let (state, _dir) = state_with(broker.clone(), "test-key").await;
    let reads = limited_reads(broker.clone(), &state.gateway, &state.settings);
    reads.profile().await.unwrap();
    reads.holdings().await.unwrap();
    reads.ltp(&[s("NSE:INFY")]).await.unwrap();
    reads
        .historical_data(408_065, "2026-10-01", "2026-10-29", "day", false, false)
        .await
        .unwrap();
    reads.get_gtts().await.unwrap();
    reads.basket_order_margins(&[], false, None).await.unwrap();
    let _ = reads.login_url();
    let k = exec_kite::kite(&state).unwrap();
    assert_eq!(
        k.slots_taken(),
        [
            "general",
            "general",
            "quote",
            "historical",
            "general",
            "general"
        ]
    );
    assert_eq!(broker.calls().len(), 7);
    assert!(broker.writes().is_empty());
}

#[tokio::test]
async fn lane_reads_are_spaced_at_the_familys_rate() {
    use baskfy_desk::lanes::exec_core::kite_limits::DeskLimits;
    use baskfy_desk::lanes::exec_core::limited_reads::LimitedReads;
    let broker = Recorder::new();
    let mono = Arc::new(Mono::default());
    let limits = Arc::new(DeskLimits::new(
        mono.clone() as Arc<dyn LimitClock>,
        std::collections::HashMap::new(),
    ));
    let reads = LimitedReads::with_limits(broker.clone(), limits.clone());
    for _ in 0..4 {
        reads.ltp(&[s("NSE:INFY")]).await.unwrap();
    }
    assert_eq!(reads.slots_taken(), ["quote"; 4]);
    let elapsed = mono.now() - 1_000.0;
    assert!(elapsed >= 2.99, "four quote reads in {elapsed}s");
    assert_eq!(broker.calls().len(), 4);
}

#[tokio::test]
async fn lane_reads_without_the_desks_gateway_still_take_a_slot() {
    use baskfy_desk::gateway::Gateway;
    use baskfy_desk::lanes::exec_core::{NotPortedGateway, limited_reads};
    let broker = Recorder::new();
    let dir = tempfile::tempdir().unwrap();
    let settings: SharedSettings = Arc::new(RwLock::new(settings_for(dir.path(), true)));
    let gateway: Arc<dyn Gateway> = Arc::new(NotPortedGateway);
    let reads = limited_reads(broker.clone(), &gateway, &settings);
    reads.orders().await.unwrap();
    reads.trades().await.unwrap();
    assert_eq!(broker.calls(), [s("orders"), s("trades")]);
    assert!(broker.writes().is_empty());
}
