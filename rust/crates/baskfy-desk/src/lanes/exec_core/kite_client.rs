//! Ports `app/kite_client.py`'s `Kite`: the desk's broker wrapper — reads (each taking its
//! family's slot, [`super::kite_limits`]), the LTP cache, holdings that count T1, collateral
//! and today's CNC buys (non-negotiables 2 and 3), the token at rest, and the **documented GTT
//! exception** of non-negotiable 6 (`place_gtt_stop` / `delete_gtt` with their own guard, used
//! by the weekly `/stops/arm`).
//!
//! **Not ported, deliberately:** `place_cnc_order`. It places an order around the gateway (no
//! risk, no rate limit, no journal), and nothing in the desk calls it; the stricter boundary
//! wins (law 2; the integrator records it in DECISIONS-RS, ⚠ UNREVIEWED). Every order goes
//! through [`super::order_gateway`].

use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use baskfy_providers::secret::Secret;
use tokio::sync::OnceCell;

use super::Broker;
use super::gtt::{TickSizes, to_tick};
use super::guards::{assert_not_overnight_option, assert_tradeable};
use super::kite_limits::{DeskLimits, LimitClock, SharedSpacers, TokioClock};
use crate::clock::Clock;
use crate::kite::KiteError;
use crate::settings::{Settings, SharedSettings};
use crate::wire::{PyDict, PyValue, dict};

/// `LTP_CACHE_SECONDS`.
pub const LTP_CACHE_SECONDS: f64 = 5.0;
/// `QUOTE_BATCH_SIZE`.
pub const QUOTE_BATCH_SIZE: usize = 500;
/// `LTP_BATCH_SIZE`.
pub const LTP_BATCH_SIZE: usize = 1000;

/// What a wrapper call raised (`type(exc).__name__`, `str(exc)`).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{python_type}: {message}")]
pub struct ClientError {
    /// The class.
    pub python_type: String,
    /// The message.
    pub message: String,
}

impl From<KiteError> for ClientError {
    fn from(e: KiteError) -> Self {
        Self {
            python_type: e.python_type().to_owned(),
            message: e.to_string(),
        }
    }
}

impl From<super::guards::GuardError> for ClientError {
    fn from(e: super::guards::GuardError) -> Self {
        Self {
            python_type: e.kind.python_type().to_owned(),
            message: e.message,
        }
    }
}

fn runtime(message: String) -> ClientError {
    ClientError {
        python_type: "RuntimeError".to_owned(),
        message,
    }
}

fn s(v: &str) -> PyValue {
    PyValue::Str(v.to_owned())
}

fn get<'a>(m: &'a PyDict, k: &str) -> &'a PyValue {
    m.get(k).unwrap_or(&PyValue::None)
}

/// `a + b` on two Python numbers (ints stay ints).
fn py_add(a: &PyValue, b: &PyValue) -> PyValue {
    #[allow(clippy::cast_precision_loss)]
    match (a, b) {
        (PyValue::Int(x), PyValue::Int(y)) => PyValue::Int(x + y),
        (PyValue::Float(x), PyValue::Float(y)) => PyValue::Float(x + y),
        (PyValue::Int(x), PyValue::Float(y)) | (PyValue::Float(y), PyValue::Int(x)) => {
            PyValue::Float(*x as f64 + y)
        }
        (PyValue::Bool(x), other) => py_add(&PyValue::Int(i64::from(*x)), other),
        (other, PyValue::Bool(y)) => py_add(other, &PyValue::Int(i64::from(*y))),
        _ => a.clone(),
    }
}

fn int_of(v: &PyValue) -> i64 {
    #[allow(clippy::cast_possible_truncation)]
    match v {
        PyValue::Int(i) => *i,
        PyValue::Float(f) => f.trunc() as i64,
        PyValue::Bool(b) => i64::from(*b),
        PyValue::Str(st) => st.trim().parse().unwrap_or(0),
        _ => 0,
    }
}

fn is_positive(v: &PyValue) -> bool {
    match v {
        PyValue::Int(i) => *i > 0,
        PyValue::Float(f) => *f > 0.0,
        PyValue::Bool(b) => *b,
        _ => false,
    }
}

fn snapshot(settings: &SharedSettings) -> Settings {
    match settings.read() {
        Ok(s) => s.clone(),
        Err(poisoned) => poisoned.into_inner().clone(),
    }
}

/// `app.kite_client.Kite`.
pub struct DeskKite {
    kc: Arc<Broker>,
    settings: SharedSettings,
    clock: Arc<dyn Clock>,
    limit_clock: Arc<dyn LimitClock>,
    limits: OnceCell<Arc<DeskLimits>>,
    shared: Arc<SharedSpacers>,
    ltp_cache: Mutex<std::collections::HashMap<String, (f64, f64)>>,
    ticks: Mutex<TickSizes>,
    token_mtime: Mutex<Option<std::time::SystemTime>>,
    slots_taken: Mutex<Vec<&'static str>>,
}

impl std::fmt::Debug for DeskKite {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DeskKite").finish_non_exhaustive()
    }
}

impl DeskKite {
    /// `Kite()`: pin IPv4 first when `FORCE_IPV4` (before the session exists), then load the
    /// stored token.
    #[must_use]
    pub fn new(kc: Arc<Broker>, settings: SharedSettings, clock: Arc<dyn Clock>) -> Self {
        Self::with_limit_clock(kc, settings, clock, Arc::new(TokioClock::default()))
    }

    /// As [`DeskKite::new`] with the limiter's clock injected (tests).
    #[must_use]
    pub fn with_limit_clock(
        kc: Arc<Broker>,
        settings: SharedSettings,
        clock: Arc<dyn Clock>,
        limit_clock: Arc<dyn LimitClock>,
    ) -> Self {
        let s = snapshot(&settings);
        if s.force_ipv4 {
            super::net::force_ipv4();
        }
        let shared = Arc::new(SharedSpacers::new(
            s.shared_read_limits,
            &s.shared_read_limits_redis_url,
            limit_clock.clone(),
        ));
        let me = Self {
            kc,
            settings,
            clock,
            limit_clock,
            limits: OnceCell::new(),
            shared,
            ltp_cache: Mutex::new(std::collections::HashMap::new()),
            ticks: Mutex::new(TickSizes::default()),
            token_mtime: Mutex::new(None),
            slots_taken: Mutex::new(Vec::new()),
        };
        me.load_token();
        me
    }

    /// `KITE_API_KEY missing` is Python's constructor error; callers that need a configured
    /// client ask here.
    ///
    /// # Errors
    /// `RuntimeError` when `KITE_API_KEY` is empty.
    pub fn ensure_configured(&self) -> Result<(), ClientError> {
        if snapshot(&self.settings).kite_api_key.is_empty() {
            return Err(runtime(
                "KITE_API_KEY missing — copy .env.example to .env and fill it.".to_owned(),
            ));
        }
        Ok(())
    }

    /// The read limiter, built on first use with the box's shared spacers.
    pub async fn limits(&self) -> Arc<DeskLimits> {
        self.limits
            .get_or_init(|| async {
                Arc::new(DeskLimits::new(
                    self.limit_clock.clone(),
                    self.shared.get().await,
                ))
            })
            .await
            .clone()
    }

    /// Use a given limiter (tests; `DeskLimits(shared={})`).
    ///
    /// # Errors
    /// A limiter is already in place.
    pub fn set_limits(&self, limits: Arc<DeskLimits>) -> Result<(), ClientError> {
        self.limits
            .set(limits)
            .map_err(|_| runtime("the read limiter is already built".to_owned()))
    }

    /// `shared_mode()` for `/status`.
    pub async fn shared_mode(&self) -> &'static str {
        self.shared.mode().await
    }

    async fn slot(&self, family: &'static str) {
        if let Ok(mut log) = self.slots_taken.lock() {
            log.push(family);
        }
        let _ = self.limits().await.slot(family).await;
    }

    /// Every read-limiter family this client has taken a slot from, in order (SW20's "no read
    /// leaves without a slot", observable in tests).
    #[must_use]
    pub fn slots_taken(&self) -> Vec<&'static str> {
        self.slots_taken
            .lock()
            .map(|l| l.clone())
            .unwrap_or_default()
    }

    fn token_path(&self) -> PathBuf {
        PathBuf::from(snapshot(&self.settings).token_file)
    }

    fn store(&self) -> Option<baskfy_providers::tokens::AccessTokenStore> {
        super::token_store::store_for(&self.token_path(), "").ok()
    }

    fn mtime(path: &std::path::Path) -> Option<std::time::SystemTime> {
        std::fs::metadata(path).and_then(|m| m.modified()).ok()
    }

    /// `_load_token()`: a token that cannot be read is the same as none (log in again).
    fn load_token(&self) {
        let Some(store) = self.store() else {
            return;
        };
        let seen = if store.exists() {
            match store.load() {
                Ok(token) => {
                    self.kc.set_access_token(&token.value);
                    Self::mtime(store.path())
                }
                Err(_) => None,
            }
        } else {
            None
        };
        if let Ok(mut m) = self.token_mtime.lock() {
            *m = seen;
        }
    }

    /// `refresh_token_if_changed()`: re-read the blob when somebody else wrote a newer token.
    pub fn refresh_token_if_changed(&self) -> bool {
        // `store_for` first, as Python does: it is what bootstraps the fallback key file.
        let Some(store) = self.store() else {
            return false;
        };
        let seen = Self::mtime(store.path());
        let known = self.token_mtime.lock().map(|m| *m).unwrap_or(None);
        if seen.is_none() || seen == known {
            return false;
        }
        self.load_token();
        true
    }

    /// `login_url()`.
    #[must_use]
    pub fn login_url(&self) -> String {
        self.kc.login_url()
    }

    /// `exchange_token(request_token)`: the login exchange, then the token encrypted at rest.
    ///
    /// # Errors
    /// The exchange, a reply with no `access_token`, or the store.
    pub async fn exchange_token(&self, request_token: &str) -> Result<(), ClientError> {
        let secret = Secret::new(snapshot(&self.settings).kite_api_secret);
        let data = self.kc.generate_session(request_token, &secret).await?;
        let token = match &data {
            PyValue::Map(m) => match m.get("access_token") {
                Some(PyValue::Str(t)) => t.clone(),
                _ => {
                    return Err(ClientError {
                        python_type: "KeyError".to_owned(),
                        message: "'access_token'".to_owned(),
                    });
                }
            },
            _ => {
                return Err(ClientError {
                    python_type: "TypeError".to_owned(),
                    message: "the session reply is not a dict".to_owned(),
                });
            }
        };
        self.kc.set_access_token(&Secret::new(token.clone()));
        let store =
            super::token_store::store_for(&self.token_path(), "").map_err(|e| ClientError {
                python_type: "OSError".to_owned(),
                message: e.to_string(),
            })?;
        store.save(&token, None).map_err(|e| ClientError {
            python_type: e.python_type().to_owned(),
            message: e.to_string(),
        })?;
        Ok(())
    }

    /// `is_authed()`: one cheap `profile` call.
    pub async fn is_authed(&self) -> bool {
        self.refresh_token_if_changed();
        self.slot("general").await;
        self.kc.profile().await.is_ok()
    }

    /// `holdings()`: what is owned right now — `quantity + t1_quantity + collateral_quantity`
    /// (non-negotiable 2; pledged shares are counted and surfaced as `pledged_qty`, never a
    /// gate — non-negotiable 3), plus today's POSITIVE CNC day buys (net of overnight carry),
    /// which Kite moves into holdings only at T+1.
    ///
    /// # Errors
    /// The holdings read, or `RuntimeError` when positions cannot be read (refuse rather than
    /// under-report).
    pub async fn holdings(&self) -> Result<Vec<PyDict>, ClientError> {
        self.refresh_token_if_changed();
        let mut out: Vec<(String, PyDict)> = Vec::new();
        self.slot("general").await;
        let rows = match self.kc.holdings().await? {
            PyValue::List(rows) => rows,
            _ => Vec::new(),
        };
        let zero = PyValue::Int(0);
        for h in &rows {
            let PyValue::Map(h) = h else { continue };
            let total = py_add(
                &py_add(get(h, "quantity"), h.get("t1_quantity").unwrap_or(&zero)),
                h.get("collateral_quantity").unwrap_or(&zero),
            );
            if !is_positive(&total) {
                continue;
            }
            let sym = match get(h, "tradingsymbol") {
                PyValue::Str(t) => t.clone(),
                other => format!("{other:?}"),
            };
            let row = dict([
                ("symbol", s(&sym)),
                ("exchange", get(h, "exchange").clone()),
                ("quantity", total),
                (
                    "pledged_qty",
                    h.get("collateral_quantity")
                        .cloned()
                        .unwrap_or(PyValue::Int(0)),
                ),
                ("average_price", get(h, "average_price").clone()),
                ("last_price", get(h, "last_price").clone()),
            ]);
            match out.iter_mut().find(|(k, _)| *k == sym) {
                Some((_, slot)) => *slot = row,
                None => out.push((sym, row)),
            }
        }
        self.slot("general").await;
        let positions = self.kc.positions().await.map_err(|exc| {
            runtime(format!(
                "could not read positions, so today's purchases cannot be counted: {exc}"
            ))
        })?;
        let net = match &positions {
            PyValue::Map(m) => match m.get("net") {
                Some(PyValue::List(rows)) => rows.clone(),
                _ => Vec::new(),
            },
            _ => Vec::new(),
        };
        for p in &net {
            let PyValue::Map(p) = p else { continue };
            let product = match p.get("product") {
                Some(PyValue::Str(st)) => st.clone(),
                _ => String::new(),
            };
            if product != "CNC" {
                continue;
            }
            let qty = int_of(get(p, "quantity")) - int_of(get(p, "overnight_quantity"));
            if qty <= 0 {
                continue;
            }
            let sym = match get(p, "tradingsymbol") {
                PyValue::Str(t) => t.clone(),
                other => format!("{other:?}"),
            };
            if let Some((_, row)) = out.iter_mut().find(|(k, _)| *k == sym) {
                let q = py_add(get(row, "quantity"), &PyValue::Int(qty));
                row.insert("quantity", q);
            } else {
                let or_zero = |k: &str| {
                    let v = get(p, k);
                    if v.truthy() {
                        v.clone()
                    } else {
                        PyValue::Float(0.0)
                    }
                };
                out.push((
                    sym.clone(),
                    dict([
                        ("symbol", s(&sym)),
                        (
                            "exchange",
                            p.get("exchange").cloned().unwrap_or_else(|| s("NSE")),
                        ),
                        ("quantity", PyValue::Int(qty)),
                        ("pledged_qty", PyValue::Int(0)),
                        ("average_price", or_zero("average_price")),
                        ("last_price", or_zero("last_price")),
                    ]),
                ));
            }
        }
        Ok(out.into_iter().map(|(_, r)| r).collect())
    }

    async fn list_of(&self, v: Result<PyValue, KiteError>) -> Result<Vec<PyValue>, ClientError> {
        Ok(match v? {
            PyValue::List(rows) => rows,
            _ => Vec::new(),
        })
    }

    /// `trades()`: today's executed trades (the book is same-day only).
    ///
    /// # Errors
    /// The read.
    pub async fn trades(&self) -> Result<Vec<PyValue>, ClientError> {
        self.slot("general").await;
        let r = self.kc.trades().await;
        self.list_of(r).await
    }

    /// `available_cash()`: `margins()["equity"]["available"]["live_balance"]`.
    ///
    /// # Errors
    /// The read, or a payload without that path.
    pub async fn available_cash(&self) -> Result<f64, ClientError> {
        self.slot("general").await;
        let m = self.kc.margins(None).await?;
        let mut cur = &m;
        for k in ["equity", "available", "live_balance"] {
            cur = match cur {
                PyValue::Map(d) => d.get(k).ok_or_else(|| ClientError {
                    python_type: "KeyError".to_owned(),
                    message: format!("'{k}'"),
                })?,
                _ => {
                    return Err(ClientError {
                        python_type: "TypeError".to_owned(),
                        message: "margins payload is not a dict".to_owned(),
                    });
                }
            };
        }
        #[allow(clippy::cast_precision_loss)]
        match cur {
            PyValue::Float(f) => Ok(*f),
            PyValue::Int(i) => Ok(*i as f64),
            _ => Err(ClientError {
                python_type: "TypeError".to_owned(),
                message: "live_balance is not a number".to_owned(),
            }),
        }
    }

    /// `ltp(symbols, exchange)`: last prices, a `LTP_CACHE_SECONDS` cache first; only the names
    /// with no fresh answer cost a (quote-family) call.
    ///
    /// # Errors
    /// The read.
    pub async fn ltp(&self, symbols: &[String], exchange: &str) -> Result<PyDict, ClientError> {
        self.refresh_token_if_changed();
        let mut wanted: Vec<&String> = Vec::new();
        for sym in symbols {
            if !sym.is_empty() && !wanted.contains(&sym) {
                wanted.push(sym);
            }
        }
        let now = self.clock.monotonic();
        let mut out = PyDict::new();
        let mut missing: Vec<String> = Vec::new();
        {
            let cache = self
                .ltp_cache
                .lock()
                .map_err(|_| runtime("ltp cache poisoned".to_owned()))?;
            for sym in wanted {
                match cache.get(&format!("{exchange}:{sym}")) {
                    Some((at, price)) if now - at < LTP_CACHE_SECONDS => {
                        out.insert(sym.clone(), PyValue::Float(*price));
                    }
                    _ => missing.push(sym.clone()),
                }
            }
        }
        for chunk in missing.chunks(LTP_BATCH_SIZE) {
            let keys: Vec<String> = chunk
                .iter()
                .map(|sym| format!("{exchange}:{sym}"))
                .collect();
            self.slot("quote").await;
            let data = self.kc.ltp(&keys).await?;
            let fetched = self.clock.monotonic();
            if let PyValue::Map(data) = data {
                for (key, value) in data.iter() {
                    let price = match value {
                        PyValue::Map(v) => match v.get("last_price") {
                            Some(PyValue::Float(f)) => *f,
                            #[allow(clippy::cast_precision_loss)]
                            Some(PyValue::Int(i)) => *i as f64,
                            _ => continue,
                        },
                        _ => continue,
                    };
                    if let Ok(mut cache) = self.ltp_cache.lock() {
                        cache.insert(key.to_owned(), (fetched, price));
                    }
                    let sym = key.split_once(':').map_or(key, |(_, s)| s);
                    out.insert(sym, PyValue::Float(price));
                }
            }
        }
        Ok(out)
    }

    /// `quotes(symbols, exchange)`: the desk's shape — chunked under 500, symbol-keyed, a
    /// failed batch skipped (a missing name must not take the panel down).
    pub async fn quotes(&self, symbols: &[String], exchange: &str) -> PyDict {
        self.refresh_token_if_changed();
        let mut clean: Vec<&String> = Vec::new();
        for sym in symbols {
            if !sym.is_empty() && !clean.contains(&sym) {
                clean.push(sym);
            }
        }
        let mut out = PyDict::new();
        for chunk in clean.chunks(QUOTE_BATCH_SIZE) {
            let keys: Vec<String> = chunk
                .iter()
                .map(|sym| format!("{exchange}:{sym}"))
                .collect();
            self.slot("quote").await;
            let Ok(PyValue::Map(data)) = self.kc.quote(&keys).await else {
                continue;
            };
            for (k, v) in data.iter() {
                let PyValue::Map(v) = v else { continue };
                let ohlc = match v.get("ohlc") {
                    Some(PyValue::Map(o)) => o.clone(),
                    _ => PyDict::new(),
                };
                let volume = match v.get("volume") {
                    Some(x) if x.truthy() => x.clone(),
                    _ => get(v, "volume_traded").clone(),
                };
                let sym = k.split_once(':').map_or(k, |(_, s)| s);
                out.insert(
                    sym,
                    PyValue::Map(dict([
                        ("last_price", get(v, "last_price").clone()),
                        ("net_change", get(v, "net_change").clone()),
                        ("open", get(&ohlc, "open").clone()),
                        ("high", get(&ohlc, "high").clone()),
                        ("low", get(&ohlc, "low").clone()),
                        ("prev_close", get(&ohlc, "close").clone()),
                        ("volume", volume),
                    ])),
                );
            }
        }
        out
    }

    /// `instruments(exchange)`: one general slot.
    ///
    /// # Errors
    /// The read.
    pub async fn instruments(&self, exchange: &str) -> Result<Vec<PyValue>, ClientError> {
        self.slot("general").await;
        let r = self.kc.instruments(Some(exchange)).await;
        self.list_of(r).await
    }

    /// `historical(token, start, end, interval)`: the 3 req/s family.
    ///
    /// # Errors
    /// The read.
    pub async fn historical(
        &self,
        token: i64,
        start: &str,
        end: &str,
        interval: &str,
    ) -> Result<Vec<PyValue>, ClientError> {
        self.refresh_token_if_changed();
        self.slot("historical").await;
        let r = self
            .kc
            .historical_data(token, start, end, interval, false, false)
            .await;
        self.list_of(r).await
    }

    /// `margins(segment)`: the raw payload.
    ///
    /// # Errors
    /// The read.
    pub async fn margins(&self, segment: Option<&str>) -> Result<PyValue, ClientError> {
        self.refresh_token_if_changed();
        self.slot("general").await;
        Ok(self.kc.margins(segment.filter(|s| !s.is_empty())).await?)
    }

    /// `orders()`.
    ///
    /// # Errors
    /// The read.
    pub async fn orders(&self) -> Result<Vec<PyValue>, ClientError> {
        self.refresh_token_if_changed();
        self.slot("general").await;
        let r = self.kc.orders().await;
        self.list_of(r).await
    }

    /// `order_history(order_id)`.
    ///
    /// # Errors
    /// The read.
    pub async fn order_history(&self, order_id: &str) -> Result<Vec<PyValue>, ClientError> {
        self.refresh_token_if_changed();
        self.slot("general").await;
        let r = self.kc.order_history(order_id).await;
        self.list_of(r).await
    }

    /// `quote_raw(keys)`: Kite's own `quote` shape, one quote slot.
    ///
    /// # Errors
    /// The read.
    pub async fn quote_raw(&self, keys: &[String]) -> Result<PyValue, ClientError> {
        self.refresh_token_if_changed();
        self.slot("quote").await;
        Ok(match self.kc.quote(keys).await? {
            PyValue::None => PyValue::Map(PyDict::new()),
            other => other,
        })
    }

    /// `get_gtts()`: every resting GTT (a read).
    ///
    /// # Errors
    /// The read.
    pub async fn get_gtts(&self) -> Result<Vec<PyValue>, ClientError> {
        self.refresh_token_if_changed();
        self.slot("general").await;
        let r = self.kc.get_gtts().await;
        self.list_of(r).await
    }

    /// `tick_size(symbol, exchange)`: cached per exchange for the process.
    ///
    /// # Errors
    /// The instrument read.
    pub async fn tick_size(&self, symbol: &str, exchange: &str) -> Result<f64, ClientError> {
        let cached = self.ticks.lock().is_ok_and(|t| t.cached(exchange));
        if !cached {
            let dump = self.instruments(exchange).await?;
            if let Ok(mut t) = self.ticks.lock() {
                t.remember(exchange, &dump);
            }
        }
        Ok(self
            .ticks
            .lock()
            .map_or(crate::gateway::DEFAULT_TICK, |t| t.get(symbol, exchange)))
    }

    /// `delete_gtt(gtt_id, symbol)`: the documented exception — its own untouchable guard (when
    /// a symbol is named), DRY_RUN simulated.
    ///
    /// # Errors
    /// `UntouchableInstrumentError`.
    pub async fn delete_gtt(&self, gtt_id: i64, symbol: &str) -> Result<PyDict, ClientError> {
        if !symbol.is_empty() {
            assert_tradeable(symbol, None)?;
        }
        let out = |status: &str| {
            dict([
                ("symbol", s(symbol)),
                ("gtt_id", PyValue::Int(gtt_id)),
                ("status", s(status)),
            ])
        };
        if snapshot(&self.settings).dry_run {
            return Ok(out("DRY_RUN_GTT_DELETE"));
        }
        match self.kc.delete_gtt(gtt_id).await {
            Ok(_) => Ok(out("GTT_DELETED")),
            Err(exc) => {
                let mut d = out("GTT_DELETE_ERROR");
                d.insert("error", s(&exc.to_string()));
                Ok(d)
            }
        }
    }

    /// `place_gtt_stop(symbol, qty, trigger, last_price, exchange)`: the documented exception
    /// of non-negotiable 6 — the untouchable and overnight-option guards first, DRY_RUN
    /// simulated (the unsnapped trigger), else snapped to the tick with the limit at 99.5%.
    ///
    /// # Errors
    /// `UntouchableInstrumentError` / `OvernightOptionError`.
    pub async fn place_gtt_stop(
        &self,
        symbol: &str,
        qty: i64,
        trigger: f64,
        last_price: f64,
        exchange: &str,
    ) -> Result<PyDict, ClientError> {
        assert_tradeable(symbol, None)?;
        assert_not_overnight_option(symbol, exchange, "CNC")?;
        if snapshot(&self.settings).dry_run {
            return Ok(dict([
                ("symbol", s(symbol)),
                ("status", s("DRY_RUN_GTT")),
                ("trigger", PyValue::Float(trigger)),
                ("qty", PyValue::Int(qty)),
            ]));
        }
        let error = |text: String| {
            dict([
                ("symbol", s(symbol)),
                ("status", s("GTT_ERROR")),
                ("error", s(&text)),
            ])
        };
        let tick = match self.tick_size(symbol, exchange).await {
            Ok(t) => t,
            Err(e) => return Ok(error(e.message)),
        };
        let trig = to_tick(trigger, tick);
        let limit = to_tick(trig * 0.995, tick);
        let params = super::gtt::gtt_params(symbol, exchange, qty, trig, limit, last_price, None);
        match self.kc.place_gtt(&params).await {
            Ok(PyValue::Map(gid)) => match gid.get("trigger_id") {
                Some(id) => Ok(dict([
                    ("symbol", s(symbol)),
                    ("status", s("GTT_PLACED")),
                    ("gtt_id", id.clone()),
                    ("trigger", PyValue::Float(trig)),
                    ("limit", PyValue::Float(limit)),
                ])),
                None => Ok(error("'trigger_id'".to_owned())),
            },
            Ok(_) => Ok(error("'trigger_id'".to_owned())),
            Err(exc) => Ok(error(exc.to_string())),
        }
    }
}
