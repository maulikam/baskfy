//! Lane **exec_kite**: `app.kite_client.Kite` against the live Python's own answers.
//!
//! `fixtures/exec_kite/goldens.json` is written by `tools/parity/exec_kite_goldens.py`: for each
//! case a scripted broker reply (well-formed or not), the method called, the value Python
//! returned or the exception it raised (`type(exc).__name__`, `str(exc)`), and the broker calls
//! Python made. This test replays every case through [`DeskKite`] over the same script and
//! requires the same value, the same exception and the same calls — wave D2's review found the
//! port skipping malformed entries where Python raises; these cases pin what Python does.
//!
//! No network: the broker is a script. The live GTT cases run with DRY_RUN off against that
//! script only (the documented GTT exception's own path), never Kite.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::sync::{Arc, Mutex, RwLock};

use async_trait::async_trait;
use baskfy_desk::clock::{Clock, FixedClock};
use baskfy_desk::kite::{
    GttParams, KiteError, KiteExceptionKind, KiteReads, KiteWrites, OrderParams,
};
use baskfy_desk::lanes::exec_core::exec_kite::kite_client::DeskKite;
use baskfy_desk::lanes::exec_core::kite_limits::LimitClock;
use baskfy_desk::settings::{Settings, SharedSettings};
use baskfy_desk::wire::{PyDict, PyValue};
use baskfy_providers::secret::Secret;

const GOLDENS: &str = include_str!("../fixtures/exec_kite/goldens.json");

/// The case's `replies`, answered by method name; `{"__raise__": [type, message]}` raises.
struct Scripted {
    replies: PyDict,
    calls: Mutex<Vec<String>>,
}

impl Scripted {
    fn answer(&self, name: &str) -> Result<PyValue, KiteError> {
        self.calls.lock().unwrap().push(name.to_owned());
        let reply = self.replies.get(name).cloned().unwrap_or(PyValue::None);
        if let PyValue::Map(m) = &reply
            && let Some(PyValue::List(spec)) = m.get("__raise__")
        {
            let message = match spec.get(1) {
                Some(PyValue::Str(t)) => t.clone(),
                _ => String::new(),
            };
            return Err(KiteError::kite(KiteExceptionKind::General, message));
        }
        Ok(reply)
    }
}

#[async_trait]
impl KiteReads for Scripted {
    fn login_url(&self) -> String {
        String::new()
    }
    fn set_access_token(&self, _token: &Secret) {}
    async fn generate_session(&self, _rt: &str, _s: &Secret) -> Result<PyValue, KiteError> {
        self.answer("generate_session")
    }
    async fn profile(&self) -> Result<PyValue, KiteError> {
        self.answer("profile")
    }
    async fn holdings(&self) -> Result<PyValue, KiteError> {
        self.answer("holdings")
    }
    async fn positions(&self) -> Result<PyValue, KiteError> {
        self.answer("positions")
    }
    async fn margins(&self, _segment: Option<&str>) -> Result<PyValue, KiteError> {
        self.answer("margins")
    }
    async fn ltp(&self, _i: &[String]) -> Result<PyValue, KiteError> {
        self.answer("ltp")
    }
    async fn ohlc(&self, _i: &[String]) -> Result<PyValue, KiteError> {
        self.answer("ohlc")
    }
    async fn quote(&self, _i: &[String]) -> Result<PyValue, KiteError> {
        self.answer("quote")
    }
    async fn instruments(&self, _e: Option<&str>) -> Result<PyValue, KiteError> {
        self.answer("instruments")
    }
    async fn historical_data(
        &self,
        _token: i64,
        _from: &str,
        _to: &str,
        _interval: &str,
        _c: bool,
        _oi: bool,
    ) -> Result<PyValue, KiteError> {
        self.answer("historical_data")
    }
    async fn orders(&self) -> Result<PyValue, KiteError> {
        self.answer("orders")
    }
    async fn order_history(&self, _id: &str) -> Result<PyValue, KiteError> {
        self.answer("order_history")
    }
    async fn trades(&self) -> Result<PyValue, KiteError> {
        self.answer("trades")
    }
    async fn get_gtts(&self) -> Result<PyValue, KiteError> {
        self.answer("get_gtts")
    }
    async fn basket_order_margins(
        &self,
        _p: &[PyValue],
        _c: bool,
        _m: Option<&str>,
    ) -> Result<PyValue, KiteError> {
        self.answer("basket_order_margins")
    }
}

#[async_trait]
impl KiteWrites for Scripted {
    async fn place_order(&self, _order: &OrderParams) -> Result<String, KiteError> {
        panic!("an order verb was reached from the Kite wrapper")
    }
    async fn modify_order(&self, _v: &str, _id: &str, _p: &PyDict) -> Result<String, KiteError> {
        panic!("an order verb was reached from the Kite wrapper")
    }
    async fn cancel_order(
        &self,
        _v: &str,
        _id: &str,
        _p: Option<&str>,
    ) -> Result<String, KiteError> {
        panic!("an order verb was reached from the Kite wrapper")
    }
    async fn place_gtt(&self, _gtt: &GttParams) -> Result<PyValue, KiteError> {
        self.answer("place_gtt")
    }
    async fn modify_gtt(&self, _id: i64, _g: &GttParams) -> Result<PyValue, KiteError> {
        panic!("modify_gtt is not the wrapper's")
    }
    async fn delete_gtt(&self, _id: i64) -> Result<PyValue, KiteError> {
        self.answer("delete_gtt")
    }
}

/// A limiter clock whose sleeps cost nothing.
#[derive(Default)]
struct Mono(Mutex<f64>);

#[async_trait]
impl LimitClock for Mono {
    fn now(&self) -> f64 {
        1_000.0 + *self.0.lock().unwrap()
    }
    async fn sleep(&self, seconds: f64) {
        *self.0.lock().unwrap() += seconds.max(0.0);
    }
}

fn field<'a>(case: &'a PyDict, key: &str) -> &'a PyValue {
    case.get(key)
        .unwrap_or_else(|| panic!("case without {key}"))
}

fn str_of(v: &PyValue) -> String {
    match v {
        PyValue::Str(s) => s.clone(),
        other => panic!("not a str: {other:?}"),
    }
}

fn strings(v: &PyValue) -> Vec<String> {
    match v {
        PyValue::List(items) => items.iter().map(str_of).collect(),
        other => panic!("not a list: {other:?}"),
    }
}

#[allow(clippy::cast_precision_loss)]
fn f64_of(v: &PyValue) -> f64 {
    match v {
        PyValue::Float(f) => *f,
        PyValue::Int(i) => *i as f64,
        other => panic!("not a number: {other:?}"),
    }
}

fn i64_of(v: &PyValue) -> i64 {
    match v {
        PyValue::Int(i) => *i,
        other => panic!("not an int: {other:?}"),
    }
}

async fn run(k: &DeskKite, method: &str, args: &[PyValue]) -> Result<PyValue, (String, String)> {
    let e = |e: baskfy_desk::lanes::exec_core::exec_kite::kite_client::ClientError| {
        (e.python_type, e.message)
    };
    let rows = |r: Vec<PyDict>| PyValue::List(r.into_iter().map(PyValue::Map).collect());
    match method {
        "holdings" => k.holdings().await.map(rows).map_err(e),
        "ltp" => k
            .ltp(&strings(&args[0]), "NSE")
            .await
            .map(PyValue::Map)
            .map_err(e),
        "quotes" => k
            .quotes(&strings(&args[0]), "NSE")
            .await
            .map(PyValue::Map)
            .map_err(e),
        "trades" => k.trades().await.map(PyValue::List).map_err(e),
        "available_cash" => k.available_cash().await.map(PyValue::Float).map_err(e),
        "margins" => {
            let segment = args.first().map(str_of);
            k.margins(segment.as_deref()).await.map_err(e)
        }
        "quote_raw" => k.quote_raw(&strings(&args[0])).await.map_err(e),
        "instruments" => k.instruments("NSE").await.map(PyValue::List).map_err(e),
        "historical" => k
            .historical(408_065, "2026-10-01", "2026-10-29", "day")
            .await
            .map(PyValue::List)
            .map_err(e),
        "orders" => k.orders().await.map(PyValue::List).map_err(e),
        "order_history" => k.order_history("1").await.map(PyValue::List).map_err(e),
        "get_gtts" => k.get_gtts().await.map(PyValue::List).map_err(e),
        "place_gtt_stop" => k
            .place_gtt_stop(
                &str_of(&args[0]),
                i64_of(&args[1]),
                f64_of(&args[2]),
                f64_of(&args[3]),
                "NSE",
            )
            .await
            .map(PyValue::Map)
            .map_err(e),
        "delete_gtt" => k
            .delete_gtt(i64_of(&args[0]), &str_of(&args[1]))
            .await
            .map(PyValue::Map)
            .map_err(e),
        other => panic!("unknown method {other}"),
    }
}

#[tokio::test]
async fn every_broker_reply_shape_answers_as_pythons_kite_does() {
    let doc = PyValue::from_json(GOLDENS.as_bytes()).unwrap();
    let PyValue::Map(doc) = doc else { panic!() };
    let PyValue::List(cases) = field(&doc, "cases") else {
        panic!()
    };
    assert!(cases.len() >= 170, "the golden set shrank: {}", cases.len());
    let mut failures = Vec::new();
    for case in cases {
        let PyValue::Map(case) = case else { panic!() };
        let name = str_of(field(case, "name"));
        let method = str_of(field(case, "method"));
        let PyValue::List(args) = field(case, "args") else {
            panic!()
        };
        let PyValue::Map(replies) = field(case, "replies") else {
            panic!()
        };
        let live = field(case, "live") == &PyValue::Bool(true);

        let dir = tempfile::tempdir().unwrap();
        let mut st = Settings::defaults().unwrap();
        st.dry_run = !live;
        st.force_ipv4 = false;
        st.shared_read_limits = false;
        st.shared_read_limits_redis_url = String::new();
        st.kite_api_key = "goldens-key".to_owned();
        st.token_file = dir.path().join(".kite_token.json").display().to_string();
        let settings: SharedSettings = Arc::new(RwLock::new(st));
        let broker = Arc::new(Scripted {
            replies: replies.clone(),
            calls: Mutex::new(Vec::new()),
        });
        let at = chrono::DateTime::parse_from_rfc3339("2026-10-30T11:00:00+05:30").unwrap();
        let k = DeskKite::with_limit_clock(
            broker.clone(),
            settings,
            Arc::new(FixedClock::new(at)) as Arc<dyn Clock>,
            Arc::new(Mono::default()),
        );
        let got = run(&k, &method, args).await;
        let want: Result<PyValue, (String, String)> = match case.get("ok") {
            Some(v) => Ok(v.clone()),
            None => {
                let raised = strings(field(case, "raise"));
                Err((raised[0].clone(), raised[1].clone()))
            }
        };
        if got != want {
            failures.push(format!("{name}:\n  python {want:?}\n  rust   {got:?}"));
        }
        let calls = broker.calls.lock().unwrap().clone();
        let want_calls = strings(field(case, "calls"));
        if calls != want_calls {
            failures.push(format!(
                "{name}: broker calls\n  python {want_calls:?}\n  rust   {calls:?}"
            ));
        }
    }
    assert!(
        failures.is_empty(),
        "{} of {} cases differ:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n")
    );
}
