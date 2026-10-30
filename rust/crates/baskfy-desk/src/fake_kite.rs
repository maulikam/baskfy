//! The replay Kite (DECISIONS-RS R8.3): [`KiteReads`] + [`KiteWrites`] over a recorded fixture,
//! **recording every call instead of sending it**. Nothing here opens a socket.
//!
//! Its twin is `tools/parity/desk_fake_kite.py`, which replaces `KiteConnect._request` in the
//! Python desk (harness-only; the desk's code is unchanged). Both read the same fixture
//! directory and answer by the same rules, and both append the same JSON line per call to the
//! same kind of log, so `desk_diff.py` can require the two desks' order/GTT calls to be EQUAL.
//!
//! **The fixture** (`<dir>/kite.json`, language-neutral; built from the bed and the tests'
//! fixtures, never from `portfolio.db` or a live account):
//!
//! ```json
//! {
//!   "session": {"authed": true},
//!   "routes": {
//!     "user.profile":       {"data": {...}},
//!     "portfolio.holdings": {"data": [...]},
//!     "order.info":         {"by_url_arg": "order_id", "answers": {"<id>": [...]}},
//!     "order.margins.basket": {"error": {"type": "InputException", "message": "...", "code": 400}}
//!   },
//!   "quotes": {"NSE:INFY": {"instrument_token": 408065, "last_price": 1500.5, "ohlc": {...}}},
//!   "historical": {"408065": {"day": [["2026-10-01T00:00:00+0530", 1, 2, 0.5, 1.5, 1000]]}}
//! }
//! ```
//! plus `<dir>/instruments/<EXCHANGE>.csv` (Kite's dump format) for `instruments(exchange)`.
//!
//! **The rules** (the Python twin applies the same):
//! * `user.profile` with `session.authed == false` raises `TokenException` (403) — "not logged
//!   in"; otherwise `routes[route]`: `data`, or `error`, or `answers[url_args[by_url_arg]]`.
//! * `market.quote*`: one entry per requested key present in `quotes`, in request order,
//!   shaped per route (`ltp`: token + last price; `ohlc`: + `ohlc`; `quote`: the record).
//! * `market.historical`: `historical[token][interval]` candles whose date (first 10 chars)
//!   lies within `from[:10]..=to[:10]`, then `_format_historical`.
//! * `market.instruments`: the CSV, then `_parse_instruments`.
//! * `api.token`: `{"access_token": "replay-access-token", "user_id": "REPLAY", "login_time":
//!   null}` unless the fixture records otherwise.
//! * Writes (`order.place|modify|cancel`, `gtt.place|modify|delete`): an `error` recorded for
//!   the route raises it; otherwise `order.place` answers `{"order_id": "REPLAY<6 digits>"}`
//!   counting from 1, `gtt.place` `{"trigger_id": 900000 + n}`, modify/cancel/delete echo the
//!   id they were given.
//! * A route with no recording raises `DataException("replay kite has no recording for route
//!   '<route>'")` — loud, never a guess.
//!
//! **The log line** (`json.dumps` with Python's defaults): `{"seq", "route", "method",
//! "url_args", "params", "is_json", "query_params"}`, with the values of `api_key`, `checksum`,
//! `access_token` and `request_token` in `params` replaced by `"<redacted>"`.

use std::collections::BTreeMap;
use std::io::Write as _;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::sync::atomic::{AtomicI64, Ordering};

use async_trait::async_trait;
use baskfy_providers::kite::{
    format_historical, format_response, parse_instruments, py_json_dumps,
};
use baskfy_providers::secret::Secret;

use crate::kite::{
    GttParams, KiteCall, KiteError, KiteExceptionKind, KiteReads, KiteWrites, OrderParams,
};
use crate::wire::{PyDict, PyValue, dict};

/// Keys whose values never reach the call log.
pub const REDACTED_PARAMS: [&str; 4] = ["api_key", "checksum", "access_token", "request_token"];

/// What went wrong loading a fixture.
#[derive(Debug, thiserror::Error)]
#[error("replay fixture {path}: {message}")]
pub struct FixtureError {
    /// The file.
    pub path: String,
    /// Why.
    pub message: String,
}

/// The replay client.
#[derive(Debug)]
pub struct ReplayKite {
    dir: PathBuf,
    fixture: PyDict,
    calls: Mutex<Vec<KiteCall>>,
    log_path: Option<PathBuf>,
    orders_placed: AtomicI64,
    gtts_placed: AtomicI64,
    api_key: String,
}

fn get<'a>(map: &'a PyDict, key: &str) -> Option<&'a PyValue> {
    map.get(key)
}

fn as_map(v: Option<&PyValue>) -> Option<&PyDict> {
    match v {
        Some(PyValue::Map(m)) => Some(m),
        _ => None,
    }
}

fn data_exception(message: impl Into<String>) -> KiteError {
    KiteError::kite(KiteExceptionKind::Data, message)
}

fn recorded_error(error: &PyDict) -> KiteError {
    let kind = match error.get("type") {
        Some(PyValue::Str(s)) => KiteExceptionKind::from_class_name(s),
        _ => KiteExceptionKind::General,
    };
    let message = match error.get("message") {
        Some(PyValue::Str(s)) => s.clone(),
        _ => String::new(),
    };
    let code = match error.get("code") {
        Some(PyValue::Int(c)) => u16::try_from(*c).unwrap_or(500),
        _ => kind.default_code(),
    };
    KiteError::Kite {
        kind,
        message,
        code,
    }
}

fn redact(params: &PyValue) -> PyValue {
    match params {
        PyValue::Map(m) => PyValue::Map(dict(m.iter().map(|(k, v)| {
            let v = if REDACTED_PARAMS.contains(&k) {
                PyValue::Str("<redacted>".to_owned())
            } else {
                v.clone()
            };
            (k.to_owned(), v)
        }))),
        other => other.clone(),
    }
}

impl ReplayKite {
    /// Load `<dir>/kite.json`; append each call to `log_path` when given.
    ///
    /// # Errors
    /// A missing or malformed fixture.
    pub fn load(
        dir: &Path,
        log_path: Option<PathBuf>,
        api_key: &str,
    ) -> Result<Self, FixtureError> {
        let path = dir.join("kite.json");
        let err = |message: String| FixtureError {
            path: path.display().to_string(),
            message,
        };
        let bytes = std::fs::read(&path).map_err(|e| err(e.to_string()))?;
        let PyValue::Map(fixture) = PyValue::from_json(&bytes).map_err(err)? else {
            return Err(err("the top level must be an object".to_owned()));
        };
        Ok(Self {
            dir: dir.to_path_buf(),
            fixture,
            calls: Mutex::new(Vec::new()),
            log_path,
            orders_placed: AtomicI64::new(0),
            gtts_placed: AtomicI64::new(0),
            api_key: api_key.to_owned(),
        })
    }

    /// Every call so far, in order.
    #[must_use]
    pub fn calls(&self) -> Vec<KiteCall> {
        self.calls.lock().map(|c| c.clone()).unwrap_or_default()
    }

    /// Only the order/GTT calls — what must be EQUAL between the two desks.
    #[must_use]
    pub fn write_calls(&self) -> Vec<KiteCall> {
        self.calls()
            .into_iter()
            .filter(KiteCall::is_write)
            .collect()
    }

    /// One call as its log line.
    ///
    /// # Errors
    /// A value `json.dumps` refuses.
    pub fn log_line(seq: usize, call: &KiteCall) -> Result<String, KiteError> {
        let opt_map = |m: &Option<PyDict>| m.clone().map_or(PyValue::None, PyValue::Map);
        let line = PyValue::Map(dict([
            ("seq", PyValue::Int(i64::try_from(seq).unwrap_or(i64::MAX))),
            ("route", PyValue::Str(call.route.clone())),
            ("method", PyValue::Str(call.method.clone())),
            ("url_args", opt_map(&call.url_args)),
            ("params", call.params.as_ref().map_or(PyValue::None, redact)),
            ("is_json", PyValue::Bool(call.is_json)),
            ("query_params", opt_map(&call.query_params)),
        ]));
        py_json_dumps(&line).map_err(KiteError::Other)
    }

    fn record(&self, call: &KiteCall) -> Result<(), KiteError> {
        let seq = {
            let mut calls = self
                .calls
                .lock()
                .map_err(|_| data_exception("replay kite call log poisoned"))?;
            calls.push(call.clone());
            calls.len()
        };
        if let Some(path) = &self.log_path {
            let line = Self::log_line(seq, call)?;
            let mut f = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(path)
                .map_err(|e| data_exception(format!("replay kite log: {e}")))?;
            writeln!(f, "{line}").map_err(|e| data_exception(format!("replay kite log: {e}")))?;
        }
        Ok(())
    }

    fn call(
        route: &str,
        method: &str,
        url_args: Option<PyDict>,
        params: Option<PyValue>,
        is_json: bool,
        query_params: Option<PyDict>,
    ) -> KiteCall {
        KiteCall {
            route: route.to_owned(),
            method: method.to_owned(),
            url_args,
            params,
            is_json,
            query_params,
        }
    }

    /// `routes[route]` for a recorded read (or a recorded error on a write).
    fn recorded(&self, call: &KiteCall) -> Result<PyValue, KiteError> {
        let routes = as_map(get(&self.fixture, "routes"));
        let Some(entry) = routes.and_then(|r| as_map(r.get(&call.route))) else {
            return Err(data_exception(format!(
                "replay kite has no recording for route '{}'",
                call.route
            )));
        };
        if let Some(error) = as_map(entry.get("error")) {
            return Err(recorded_error(error));
        }
        if let Some(PyValue::Str(arg)) = entry.get("by_url_arg") {
            let key = call
                .url_args
                .as_ref()
                .and_then(|u| u.get(arg))
                .map_or_else(|| "None".to_owned(), PyValue::py_str);
            return as_map(entry.get("answers"))
                .and_then(|a| a.get(&key))
                .cloned()
                .ok_or_else(|| {
                    data_exception(format!(
                        "replay kite has no recording for route '{}' with {arg}={key}",
                        call.route
                    ))
                });
        }
        entry.get("data").cloned().ok_or_else(|| {
            data_exception(format!("replay kite: route '{}' has no data", call.route))
        })
    }

    fn recorded_write_error(&self, route: &str) -> Option<KiteError> {
        let routes = as_map(get(&self.fixture, "routes"))?;
        let entry = as_map(routes.get(route))?;
        as_map(entry.get("error")).map(recorded_error)
    }

    fn quotes(&self, route: &str, instruments: &[String]) -> Result<PyValue, KiteError> {
        let call = Self::call(
            route,
            "GET",
            None,
            Some(PyValue::Map(dict([(
                "i",
                PyValue::List(instruments.iter().cloned().map(PyValue::Str).collect()),
            )]))),
            false,
            None,
        );
        self.record(&call)?;
        if let Some(e) = self.recorded_write_error(route) {
            return Err(e);
        }
        let table = as_map(get(&self.fixture, "quotes"));
        let mut out = PyDict::new();
        for key in instruments {
            if out.get(key).is_some() {
                continue;
            }
            let Some(PyValue::Map(q)) = table.and_then(|t| t.get(key)) else {
                continue;
            };
            let shaped = match route {
                "market.quote.ltp" => dict(
                    ["instrument_token", "last_price"]
                        .into_iter()
                        .filter_map(|k| q.get(k).map(|v| (k, v.clone()))),
                ),
                "market.quote.ohlc" => dict(
                    ["instrument_token", "last_price", "ohlc"]
                        .into_iter()
                        .filter_map(|k| q.get(k).map(|v| (k, v.clone()))),
                ),
                _ => q.clone(),
            };
            out.insert(key.clone(), PyValue::Map(shaped));
        }
        Ok(PyValue::Map(out))
    }

    fn checked_write(&self, call: &KiteCall) -> Result<(), KiteError> {
        self.record(call)?;
        match self.recorded_write_error(&call.route) {
            Some(e) => Err(e),
            None => Ok(()),
        }
    }
}

#[async_trait]
impl KiteReads for ReplayKite {
    fn login_url(&self) -> String {
        format!(
            "https://kite.zerodha.com/connect/login?api_key={}&v=3",
            self.api_key
        )
    }

    fn set_access_token(&self, _token: &Secret) {}

    async fn generate_session(
        &self,
        request_token: &str,
        _api_secret: &Secret,
    ) -> Result<PyValue, KiteError> {
        let call = Self::call(
            "api.token",
            "POST",
            None,
            Some(PyValue::Map(dict([
                ("api_key", PyValue::Str(self.api_key.clone())),
                ("request_token", PyValue::Str(request_token.to_owned())),
                ("checksum", PyValue::Str("<computed>".to_owned())),
            ]))),
            false,
            None,
        );
        self.record(&call)?;
        let recorded = as_map(get(&self.fixture, "routes"))
            .is_some_and(|r| matches!(r.get("api.token"), Some(PyValue::Map(_))));
        if recorded {
            return self.recorded(&call);
        }
        Ok(PyValue::Map(dict([
            (
                "access_token",
                PyValue::Str("replay-access-token".to_owned()),
            ),
            ("user_id", PyValue::Str("REPLAY".to_owned())),
            ("login_time", PyValue::None),
        ])))
    }

    async fn profile(&self) -> Result<PyValue, KiteError> {
        let call = Self::call("user.profile", "GET", None, None, false, None);
        self.record(&call)?;
        let authed = as_map(get(&self.fixture, "session"))
            .and_then(|s| s.get("authed"))
            .is_none_or(|v| *v != PyValue::Bool(false));
        if !authed {
            return Err(KiteError::Kite {
                kind: KiteExceptionKind::Token,
                message: "Incorrect `api_key` or `access_token`.".to_owned(),
                code: 403,
            });
        }
        self.recorded(&call)
    }

    async fn holdings(&self) -> Result<PyValue, KiteError> {
        let call = Self::call("portfolio.holdings", "GET", None, None, false, None);
        self.record(&call)?;
        self.recorded(&call)
    }

    async fn positions(&self) -> Result<PyValue, KiteError> {
        let call = Self::call("portfolio.positions", "GET", None, None, false, None);
        self.record(&call)?;
        self.recorded(&call)
    }

    async fn margins(&self, segment: Option<&str>) -> Result<PyValue, KiteError> {
        let call = match segment.filter(|s| !s.is_empty()) {
            Some(s) => Self::call(
                "user.margins.segment",
                "GET",
                Some(dict([("segment", PyValue::Str(s.to_owned()))])),
                None,
                false,
                None,
            ),
            None => Self::call("user.margins", "GET", None, None, false, None),
        };
        self.record(&call)?;
        if call.route == "user.margins.segment" {
            let all = self.recorded(&Self::call("user.margins", "GET", None, None, false, None))?;
            let key = segment.unwrap_or_default();
            return match all {
                PyValue::Map(m) => m.get(key).cloned().ok_or_else(|| {
                    data_exception(format!("replay kite has no margins for segment '{key}'"))
                }),
                _ => Err(data_exception("replay kite: user.margins is not an object")),
            };
        }
        self.recorded(&call)
    }

    async fn ltp(&self, instruments: &[String]) -> Result<PyValue, KiteError> {
        self.quotes("market.quote.ltp", instruments)
    }

    async fn ohlc(&self, instruments: &[String]) -> Result<PyValue, KiteError> {
        self.quotes("market.quote.ohlc", instruments)
    }

    async fn quote(&self, instruments: &[String]) -> Result<PyValue, KiteError> {
        self.quotes("market.quote", instruments)
    }

    async fn instruments(&self, exchange: Option<&str>) -> Result<PyValue, KiteError> {
        let (call, file) = match exchange {
            Some(ex) => (
                Self::call(
                    "market.instruments",
                    "GET",
                    Some(dict([("exchange", PyValue::Str(ex.to_owned()))])),
                    None,
                    false,
                    None,
                ),
                format!("{ex}.csv"),
            ),
            None => (
                Self::call("market.instruments.all", "GET", None, None, false, None),
                "ALL.csv".to_owned(),
            ),
        };
        self.record(&call)?;
        let path = self.dir.join("instruments").join(&file);
        let bytes = std::fs::read(&path).map_err(|_| {
            data_exception(format!(
                "replay kite has no recording for route '{}'",
                call.route
            ))
        })?;
        parse_instruments(&bytes).map_err(KiteError::Other)
    }

    async fn historical_data(
        &self,
        instrument_token: i64,
        from: &str,
        to: &str,
        interval: &str,
        continuous: bool,
        oi: bool,
    ) -> Result<PyValue, KiteError> {
        let call = Self::call(
            "market.historical",
            "GET",
            Some(dict([
                ("instrument_token", PyValue::Int(instrument_token)),
                ("interval", PyValue::Str(interval.to_owned())),
            ])),
            Some(PyValue::Map(dict([
                ("from", PyValue::Str(from.to_owned())),
                ("to", PyValue::Str(to.to_owned())),
                ("interval", PyValue::Str(interval.to_owned())),
                ("continuous", PyValue::Int(i64::from(continuous))),
                ("oi", PyValue::Int(i64::from(oi))),
            ]))),
            false,
            None,
        );
        self.record(&call)?;
        let lo: String = from.chars().take(10).collect();
        let hi: String = to.chars().take(10).collect();
        let series = as_map(get(&self.fixture, "historical"))
            .and_then(|h| as_map(h.get(&instrument_token.to_string())))
            .and_then(|t| t.get(interval));
        let Some(PyValue::List(candles)) = series else {
            return Err(data_exception(format!(
                "replay kite has no recording for route 'market.historical' with \
                 instrument_token={instrument_token} interval={interval}"
            )));
        };
        let kept: Vec<PyValue> = candles
            .iter()
            .filter(|c| match c {
                PyValue::List(cells) => match cells.first() {
                    Some(PyValue::Str(stamp)) => {
                        let day: String = stamp.chars().take(10).collect();
                        day >= lo && day <= hi
                    }
                    _ => false,
                },
                _ => false,
            })
            .cloned()
            .collect();
        format_historical(&PyValue::Map(dict([("candles", PyValue::List(kept))])))
            .map_err(KiteError::Other)
    }

    async fn orders(&self) -> Result<PyValue, KiteError> {
        let call = Self::call("orders", "GET", None, None, false, None);
        self.record(&call)?;
        format_response(&self.recorded(&call)?).map_err(KiteError::Other)
    }

    async fn order_history(&self, order_id: &str) -> Result<PyValue, KiteError> {
        let call = Self::call(
            "order.info",
            "GET",
            Some(dict([("order_id", PyValue::Str(order_id.to_owned()))])),
            None,
            false,
            None,
        );
        self.record(&call)?;
        format_response(&self.recorded(&call)?).map_err(KiteError::Other)
    }

    async fn trades(&self) -> Result<PyValue, KiteError> {
        let call = Self::call("trades", "GET", None, None, false, None);
        self.record(&call)?;
        format_response(&self.recorded(&call)?).map_err(KiteError::Other)
    }

    async fn get_gtts(&self) -> Result<PyValue, KiteError> {
        let call = Self::call("gtt", "GET", None, None, false, None);
        self.record(&call)?;
        self.recorded(&call)
    }

    async fn basket_order_margins(
        &self,
        params: &[PyValue],
        consider_positions: bool,
        mode: Option<&str>,
    ) -> Result<PyValue, KiteError> {
        let call = Self::call(
            "order.margins.basket",
            "POST",
            None,
            Some(PyValue::List(params.to_vec())),
            true,
            Some(dict([
                ("consider_positions", PyValue::Bool(consider_positions)),
                (
                    "mode",
                    mode.map_or(PyValue::None, |m| PyValue::Str(m.to_owned())),
                ),
            ])),
        );
        self.record(&call)?;
        self.recorded(&call)
    }
}

#[async_trait]
impl KiteWrites for ReplayKite {
    async fn place_order(&self, order: &OrderParams) -> Result<String, KiteError> {
        let call = Self::call(
            "order.place",
            "POST",
            Some(dict([("variety", PyValue::Str(order.variety.clone()))])),
            Some(PyValue::Map(order.to_params())),
            false,
            None,
        );
        self.checked_write(&call)?;
        let n = self.orders_placed.fetch_add(1, Ordering::SeqCst) + 1;
        Ok(format!("REPLAY{n:06}"))
    }

    async fn modify_order(
        &self,
        variety: &str,
        order_id: &str,
        params: &PyDict,
    ) -> Result<String, KiteError> {
        let mut all = dict([
            ("variety", PyValue::Str(variety.to_owned())),
            ("order_id", PyValue::Str(order_id.to_owned())),
        ]);
        for (k, v) in params.iter() {
            if *v != PyValue::None {
                all.insert(k, v.clone());
            }
        }
        let call = Self::call(
            "order.modify",
            "PUT",
            Some(dict([
                ("variety", PyValue::Str(variety.to_owned())),
                ("order_id", PyValue::Str(order_id.to_owned())),
            ])),
            Some(PyValue::Map(all)),
            false,
            None,
        );
        self.checked_write(&call)?;
        Ok(order_id.to_owned())
    }

    async fn cancel_order(
        &self,
        variety: &str,
        order_id: &str,
        parent_order_id: Option<&str>,
    ) -> Result<String, KiteError> {
        let call = Self::call(
            "order.cancel",
            "DELETE",
            Some(dict([
                ("variety", PyValue::Str(variety.to_owned())),
                ("order_id", PyValue::Str(order_id.to_owned())),
            ])),
            Some(PyValue::Map(dict([(
                "parent_order_id",
                parent_order_id.map_or(PyValue::None, |p| PyValue::Str(p.to_owned())),
            )]))),
            false,
            None,
        );
        self.checked_write(&call)?;
        Ok(order_id.to_owned())
    }

    async fn place_gtt(&self, gtt: &GttParams) -> Result<PyValue, KiteError> {
        let call = Self::call(
            "gtt.place",
            "POST",
            None,
            Some(PyValue::Map(gtt.to_params()?)),
            false,
            None,
        );
        self.checked_write(&call)?;
        let n = self.gtts_placed.fetch_add(1, Ordering::SeqCst) + 1;
        Ok(PyValue::Map(dict([(
            "trigger_id",
            PyValue::Int(900_000 + n),
        )])))
    }

    async fn modify_gtt(&self, trigger_id: i64, gtt: &GttParams) -> Result<PyValue, KiteError> {
        let call = Self::call(
            "gtt.modify",
            "PUT",
            Some(dict([("trigger_id", PyValue::Int(trigger_id))])),
            Some(PyValue::Map(gtt.to_params()?)),
            false,
            None,
        );
        self.checked_write(&call)?;
        Ok(PyValue::Map(dict([(
            "trigger_id",
            PyValue::Int(trigger_id),
        )])))
    }

    async fn delete_gtt(&self, trigger_id: i64) -> Result<PyValue, KiteError> {
        let call = Self::call(
            "gtt.delete",
            "DELETE",
            Some(dict([("trigger_id", PyValue::Int(trigger_id))])),
            None,
            false,
            None,
        );
        self.checked_write(&call)?;
        Ok(PyValue::Map(dict([(
            "trigger_id",
            PyValue::Int(trigger_id),
        )])))
    }
}

/// Read a call log back (`desk_diff.py` does the same in Python): one JSON object per line.
///
/// # Errors
/// An unreadable file or a malformed line.
pub fn read_log(path: &Path) -> Result<Vec<BTreeMap<String, serde_json::Value>>, FixtureError> {
    let text = std::fs::read_to_string(path).map_err(|e| FixtureError {
        path: path.display().to_string(),
        message: e.to_string(),
    })?;
    text.lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| {
            serde_json::from_str(l).map_err(|e| FixtureError {
                path: path.display().to_string(),
                message: e.to_string(),
            })
        })
        .collect()
}
