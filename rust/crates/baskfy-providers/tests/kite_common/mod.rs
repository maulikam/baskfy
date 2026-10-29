//! Shared seams for the Kite tests: the fake clients of `test_kite.py`, `test_kite_options.py`
//! and `test_kite_fno_master.py`, the counting limiter, the conftest settings, and a loopback
//! HTTP server that records every request byte for byte (the network block allows loopback).
#![allow(
    dead_code,
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::missing_panics_doc
)]

use std::collections::VecDeque;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use baskfy_providers::clock::{Clock, Sleeper, SystemClock};
use baskfy_providers::errors::ProviderError;
use baskfy_providers::kite::{
    HistoricalBound, KiteClient, KiteError, KiteExceptionKind, KiteMap, KiteProvider, KiteRuntime,
    KiteValue,
};
use baskfy_providers::ratelimit::RateLimiter;
use baskfy_providers::records::{BrokerAccountFields, BrokerAccountRef, PyDateTime};
use baskfy_providers::retry::{RetryHooks, RetryPolicy};
use baskfy_providers::secret::Secret;
use baskfy_providers::settings::ProviderSettings;
use baskfy_providers::tokens::{AccessTokenStore, ist};
use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime, Timelike};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};

// --- values ----------------------------------------------------------------------------------

/// A dict literal.
pub fn dict(pairs: Vec<(&str, KiteValue)>) -> KiteValue {
    KiteValue::Map(pairs.into_iter().collect::<KiteMap>())
}

pub fn date(y: i32, m: u32, d: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, d).unwrap()
}

pub fn naive(y: i32, mo: u32, d: u32, h: u32, mi: u32, s: u32) -> NaiveDateTime {
    date(y, mo, d).and_hms_opt(h, mi, s).unwrap()
}

pub fn aware(dt: NaiveDateTime, offset_seconds: i32) -> DateTime<FixedOffset> {
    dt.and_local_timezone(FixedOffset::east_opt(offset_seconds).unwrap())
        .single()
        .unwrap()
}

// --- conftest ----------------------------------------------------------------------------------

/// A temp dir that lives as long as the settings that point into it.
pub struct Env {
    pub dir: tempfile::TempDir,
    pub settings: ProviderSettings,
}

/// `settings`: nothing configured except paths, so tests start from "unavailable".
pub fn settings() -> Env {
    settings_in(tempfile::tempdir().unwrap())
}

/// `settings` in a directory named the way pytest names `tmp_path` (`<test name>0`), for the
/// assertions that read the token path back out of a message.
pub fn settings_named(test: &str) -> Env {
    settings_in(
        tempfile::Builder::new()
            .prefix(&format!("{test}0"))
            .tempdir()
            .unwrap(),
    )
}

fn settings_in(dir: tempfile::TempDir) -> Env {
    let settings = ProviderSettings {
        kite_token_path: dir.path().join("kite-token.enc").display().to_string(),
        ..ProviderSettings::default()
    };
    Env { dir, settings }
}

/// `configured_settings`: Kite credentials and an encryption key present.
pub fn configured_settings() -> Env {
    configure(settings())
}

/// `configured_settings` under a pytest-style `tmp_path` name.
pub fn configured_settings_named(test: &str) -> Env {
    configure(settings_named(test))
}

fn configure(mut env: Env) -> Env {
    env.settings.kite_api_key = Secret::new("test-key");
    env.settings.kite_api_secret = Secret::new("test-secret");
    env.settings.kite_token_encryption_key = Secret::new(fernet::Fernet::generate_key());
    env
}

/// `TODAY`: this morning, in IST — not a pinned date (tokens expire by IST calendar day).
pub fn today() -> DateTime<FixedOffset> {
    SystemClock
        .now()
        .with_timezone(&ist())
        .with_hour(9)
        .and_then(|t| t.with_minute(0))
        .and_then(|t| t.with_second(0))
        .and_then(|t| t.with_nanosecond(0))
        .unwrap()
}

pub fn store_for(settings: &ProviderSettings) -> Arc<AccessTokenStore> {
    Arc::new(AccessTokenStore::new(
        settings.kite_token_path.clone(),
        settings.kite_token_encryption_key.clone(),
    ))
}

/// `stored_token`: a fresh token issued this morning.
pub fn stored_token(settings: &ProviderSettings) -> Arc<AccessTokenStore> {
    let store = store_for(settings);
    store.save("fresh-token", Some(today())).unwrap();
    store
}

pub fn account(broker_account_id: i64, broker_id: &str) -> BrokerAccountRef {
    BrokerAccountRef::new(BrokerAccountFields {
        broker_account_id,
        broker_id: broker_id.to_owned(),
        kite_user_id: None,
    })
    .unwrap()
}

/// `_ref()`.
pub fn ref_() -> BrokerAccountRef {
    account(1, "zerodha")
}

// --- limiter and sleeper -----------------------------------------------------------------------

/// `UnlimitedBucket` / `CountingLimiter`: never blocks, counts tokens.
#[derive(Debug, Default)]
pub struct CountingLimiter {
    pub acquired: AtomicUsize,
}

impl CountingLimiter {
    pub fn count(&self) -> usize {
        self.acquired.load(Ordering::SeqCst)
    }
}

#[async_trait]
impl RateLimiter for CountingLimiter {
    async fn acquire(&self, tokens: f64) -> Result<f64, ProviderError> {
        #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
        self.acquired.fetch_add(tokens as usize, Ordering::SeqCst);
        Ok(0.0)
    }
}

/// `sleeper=sleeps.append`.
#[derive(Debug, Default)]
pub struct Sleeps(pub Mutex<Vec<f64>>);

impl Sleeps {
    pub fn taken(&self) -> Vec<f64> {
        self.0.lock().unwrap().clone()
    }
}

#[async_trait]
impl Sleeper for Sleeps {
    async fn sleep(&self, seconds: f64) {
        self.0.lock().unwrap().push(seconds);
    }
}

pub fn policy(max_attempts: u32, base_seconds: f64) -> RetryPolicy {
    RetryPolicy::new(max_attempts, base_seconds, 30.0).unwrap()
}

/// `build_provider`: the fake client, an unlimited bucket, identity jitter.
pub fn build_provider(
    settings: &ProviderSettings,
    client: Arc<dyn KiteClient>,
    store: Arc<AccessTokenStore>,
    retry: Option<RetryPolicy>,
    sleeps: Option<Arc<Sleeps>>,
) -> KiteProvider {
    let hooks = RetryHooks {
        sleeper: sleeps.unwrap_or_default(),
        jitter: Some(Arc::new(|ceiling| ceiling)),
        ..RetryHooks::default()
    };
    KiteProvider::new(
        settings.clone(),
        KiteRuntime {
            rate_limiter: Some(Arc::new(CountingLimiter::default())),
            client_factory: Some(factory(client)),
            token_store: Some(store),
            retry_policy: Some(retry.unwrap_or_else(|| policy(3, 0.5))),
            retry_hooks: Some(hooks),
            holdings_account_id: None,
        },
    )
    .unwrap()
}

/// `client_factory=lambda _api_key: client`.
pub fn factory(client: Arc<dyn KiteClient>) -> baskfy_providers::kite::ClientFactory {
    Arc::new(move |_api_key: &str| Ok(Arc::clone(&client)))
}

// --- FakeKiteClient (test_kite.py) ---------------------------------------------------------

/// Records calls, and fails in whatever way the test asks for.
#[derive(Default)]
pub struct FakeKiteClient {
    pub candles: Vec<KiteValue>,
    pub instruments: Vec<KiteValue>,
    pub raises: Option<(KiteExceptionKind, String)>,
    pub fail_times: Option<usize>,
    pub positions: Vec<KiteValue>,
    pub margins: Option<KiteValue>,
    pub quotes: KiteMap,
    pub trades: Vec<KiteValue>,
    pub calls: Mutex<FakeCalls>,
}

#[derive(Debug, Default, Clone)]
pub struct FakeCalls {
    pub access_tokens: Vec<String>,
    pub historical_calls: Vec<(i64, HistoricalBound, HistoricalBound, String)>,
    pub instrument_calls: usize,
    pub holdings_calls: usize,
    pub margin_segments: Vec<Option<String>>,
    pub quote_batches: Vec<Vec<String>>,
}

impl FakeKiteClient {
    pub fn calls(&self) -> FakeCalls {
        self.calls.lock().unwrap().clone()
    }

    fn maybe_raise(&self) -> Result<(), KiteError> {
        let Some((kind, message)) = &self.raises else {
            return Ok(());
        };
        let calls = self.calls.lock().unwrap();
        let made = calls.historical_calls.len() + calls.instrument_calls;
        match self.fail_times {
            None => Err(KiteError::kite(*kind, message.clone())),
            Some(n) if made <= n => Err(KiteError::kite(*kind, message.clone())),
            Some(_) => Ok(()),
        }
    }
}

fn candle_date(candle: &KiteValue) -> NaiveDate {
    match candle.get("date").unwrap() {
        KiteValue::Date(d) => *d,
        other => panic!("candle date must be a date, got {other:?}"),
    }
}

#[async_trait]
impl KiteClient for FakeKiteClient {
    fn set_access_token(&self, access_token: &Secret) {
        self.calls
            .lock()
            .unwrap()
            .access_tokens
            .push(access_token.expose().to_owned());
    }

    async fn instruments(&self, _exchange: Option<&str>) -> Result<KiteValue, KiteError> {
        self.calls.lock().unwrap().instrument_calls += 1;
        self.maybe_raise()?;
        Ok(KiteValue::List(self.instruments.clone()))
    }

    async fn historical_data(
        &self,
        instrument_token: i64,
        from_date: HistoricalBound,
        to_date: HistoricalBound,
        interval: &str,
    ) -> Result<KiteValue, KiteError> {
        let (HistoricalBound::Date(lo), HistoricalBound::Date(hi)) = (from_date, to_date) else {
            panic!("daily history is asked for by date");
        };
        self.calls.lock().unwrap().historical_calls.push((
            instrument_token,
            from_date,
            to_date,
            interval.to_owned(),
        ));
        self.maybe_raise()?;
        Ok(KiteValue::List(
            self.candles
                .iter()
                .filter(|c| lo <= candle_date(c) && candle_date(c) <= hi)
                .cloned()
                .collect(),
        ))
    }

    async fn holdings(&self) -> Result<KiteValue, KiteError> {
        self.calls.lock().unwrap().holdings_calls += 1;
        self.maybe_raise()?;
        Ok(KiteValue::List(self.positions.clone()))
    }

    async fn margins(&self, segment: Option<&str>) -> Result<KiteValue, KiteError> {
        self.calls
            .lock()
            .unwrap()
            .margin_segments
            .push(segment.map(str::to_owned));
        self.maybe_raise()?;
        Ok(self
            .margins
            .clone()
            .unwrap_or_else(|| KiteValue::Map(KiteMap::new())))
    }

    async fn quote(&self, instruments: &[String]) -> Result<KiteValue, KiteError> {
        self.calls
            .lock()
            .unwrap()
            .quote_batches
            .push(instruments.to_vec());
        self.maybe_raise()?;
        Ok(KiteValue::Map(
            instruments
                .iter()
                .filter_map(|k| self.quotes.get(k).map(|v| (k.clone(), v.clone())))
                .collect(),
        ))
    }

    async fn trades(&self) -> Result<KiteValue, KiteError> {
        self.maybe_raise()?;
        Ok(KiteValue::List(self.trades.clone()))
    }
}

// --- PlainKite / OptionsFakeKite (test_kite_options.py) ------------------------------------

/// Kite Connect's documented shapes; records every call. With `basket` it is
/// `OptionsFakeKite` (Kite's margin calculator present); without, `PlainKite`.
#[derive(Default)]
pub struct OptionsKite {
    pub quotes: KiteMap,
    pub candles: Vec<KiteValue>,
    pub margins: Option<KiteValue>,
    pub basket: Option<KiteValue>,
    pub calls: Mutex<OptionsCalls>,
}

#[derive(Debug, Default, Clone)]
pub struct OptionsCalls {
    pub quote_batches: Vec<Vec<String>>,
    pub historical_calls: Vec<(i64, NaiveDateTime, NaiveDateTime, String)>,
    pub basket_calls: Vec<(Vec<KiteValue>, bool)>,
}

impl OptionsKite {
    pub fn calls(&self) -> OptionsCalls {
        self.calls.lock().unwrap().clone()
    }
}

#[async_trait]
impl KiteClient for OptionsKite {
    fn set_access_token(&self, _access_token: &Secret) {}

    async fn instruments(&self, _exchange: Option<&str>) -> Result<KiteValue, KiteError> {
        Ok(KiteValue::List(Vec::new()))
    }

    async fn historical_data(
        &self,
        instrument_token: i64,
        from_date: HistoricalBound,
        to_date: HistoricalBound,
        interval: &str,
    ) -> Result<KiteValue, KiteError> {
        let (HistoricalBound::DateTime(lo), HistoricalBound::DateTime(hi)) = (from_date, to_date)
        else {
            panic!("minute history is asked for by datetime");
        };
        self.calls.lock().unwrap().historical_calls.push((
            instrument_token,
            lo,
            hi,
            interval.to_owned(),
        ));
        let mut out = Vec::new();
        for candle in &self.candles {
            let KiteValue::DateTime(PyDateTime::Aware(stamp)) = candle.get("date").unwrap() else {
                panic!("candle stamps are aware");
            };
            let wall = stamp.naive_local();
            if lo <= wall && wall <= hi {
                out.push(candle.clone());
            }
        }
        Ok(KiteValue::List(out))
    }

    async fn holdings(&self) -> Result<KiteValue, KiteError> {
        Ok(KiteValue::List(Vec::new()))
    }

    async fn margins(&self, _segment: Option<&str>) -> Result<KiteValue, KiteError> {
        Ok(self
            .margins
            .clone()
            .unwrap_or_else(|| KiteValue::Map(KiteMap::new())))
    }

    async fn quote(&self, instruments: &[String]) -> Result<KiteValue, KiteError> {
        self.calls
            .lock()
            .unwrap()
            .quote_batches
            .push(instruments.to_vec());
        Ok(KiteValue::Map(
            instruments
                .iter()
                .filter_map(|k| self.quotes.get(k).map(|v| (k.clone(), v.clone())))
                .collect(),
        ))
    }

    async fn trades(&self) -> Result<KiteValue, KiteError> {
        Ok(KiteValue::List(Vec::new()))
    }

    fn has_basket_order_margins(&self) -> bool {
        self.basket.is_some()
    }

    async fn basket_order_margins(
        &self,
        params: &[KiteValue],
        consider_positions: bool,
        _mode: Option<&str>,
    ) -> Result<KiteValue, KiteError> {
        self.calls
            .lock()
            .unwrap()
            .basket_calls
            .push((params.to_vec(), consider_positions));
        Ok(self.basket.clone().unwrap_or(KiteValue::None))
    }
}

/// `_provider` of `test_kite_options.py` / `test_kite_fno_master.py`: a fresh token and the
/// minimum credentials; the limiter is returned so a test can count it.
pub fn options_provider(
    client: Arc<dyn KiteClient>,
    limiter: Arc<CountingLimiter>,
) -> (tempfile::TempDir, KiteProvider) {
    let dir = tempfile::tempdir().unwrap();
    let key = fernet::Fernet::generate_key();
    let path = dir.path().join("kite-token.enc").display().to_string();
    let store = Arc::new(AccessTokenStore::new(
        path.clone(),
        Secret::new(key.clone()),
    ));
    store.save("tok", None).unwrap();
    let settings = ProviderSettings {
        kite_api_key: Secret::new("k"),
        kite_api_secret: Secret::new("s"),
        kite_token_encryption_key: Secret::new(key),
        kite_token_path: path,
        ..ProviderSettings::default()
    };
    let provider = KiteProvider::new(
        settings,
        KiteRuntime {
            rate_limiter: Some(limiter),
            client_factory: Some(factory(client)),
            token_store: Some(store),
            ..KiteRuntime::default()
        },
    )
    .unwrap();
    (dir, provider)
}

// --- DumpKite (test_kite_fno_master.py) -----------------------------------------------------

/// Serves one NFO dump and counts the calls. No order verb exists on this fake.
pub struct DumpKite {
    pub rows: Vec<KiteValue>,
    pub calls: Mutex<Vec<Option<String>>>,
}

#[async_trait]
impl KiteClient for DumpKite {
    fn set_access_token(&self, _access_token: &Secret) {}

    async fn instruments(&self, exchange: Option<&str>) -> Result<KiteValue, KiteError> {
        self.calls.lock().unwrap().push(exchange.map(str::to_owned));
        Ok(KiteValue::List(self.rows.clone()))
    }

    async fn historical_data(
        &self,
        _instrument_token: i64,
        _from_date: HistoricalBound,
        _to_date: HistoricalBound,
        _interval: &str,
    ) -> Result<KiteValue, KiteError> {
        panic!("not read by the master")
    }

    async fn holdings(&self) -> Result<KiteValue, KiteError> {
        panic!("not read by the master")
    }

    async fn margins(&self, _segment: Option<&str>) -> Result<KiteValue, KiteError> {
        panic!("not read by the master")
    }

    async fn quote(&self, _instruments: &[String]) -> Result<KiteValue, KiteError> {
        panic!("not read by the master")
    }

    async fn trades(&self) -> Result<KiteValue, KiteError> {
        panic!("not read by the master")
    }
}

// --- a loopback HTTP/1.1 server that records requests ---------------------------------------

/// One request as it arrived on the wire.
#[derive(Debug, Clone)]
pub struct Captured {
    pub method: String,
    pub target: String,
    pub version: String,
    /// In wire order, names as sent.
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Captured {
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }
}

/// One canned answer.
#[derive(Debug, Clone)]
pub struct Canned {
    pub status: u16,
    pub content_type: Option<String>,
    pub body: Vec<u8>,
    /// Accept, read the request, and never answer.
    pub hang: bool,
}

impl Canned {
    pub fn json(body: &str) -> Self {
        Self {
            status: 200,
            content_type: Some("application/json".to_owned()),
            body: body.as_bytes().to_vec(),
            hang: false,
        }
    }

    pub fn with(status: u16, content_type: Option<&str>, body: &[u8]) -> Self {
        Self {
            status,
            content_type: content_type.map(str::to_owned),
            body: body.to_vec(),
            hang: false,
        }
    }

    pub fn hang() -> Self {
        Self {
            status: 200,
            content_type: None,
            body: Vec::new(),
            hang: true,
        }
    }
}

/// The server: answers each request with the next canned response (the last one repeats).
pub struct MockKite {
    pub addr: SocketAddr,
    requests: Arc<Mutex<Vec<Captured>>>,
}

impl MockKite {
    pub async fn start(responses: Vec<Canned>) -> Self {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let requests = Arc::new(Mutex::new(Vec::new()));
        let queue = Arc::new(Mutex::new(VecDeque::from(responses)));
        let seen = Arc::clone(&requests);
        tokio::spawn(async move {
            loop {
                let Ok((stream, _)) = listener.accept().await else {
                    return;
                };
                let seen = Arc::clone(&seen);
                let queue = Arc::clone(&queue);
                tokio::spawn(async move {
                    serve(stream, seen, queue).await;
                });
            }
        });
        Self { addr, requests }
    }

    pub fn root(&self) -> String {
        format!("http://{}", self.addr)
    }

    pub fn requests(&self) -> Vec<Captured> {
        self.requests.lock().unwrap().clone()
    }
}

async fn serve(
    stream: tokio::net::TcpStream,
    seen: Arc<Mutex<Vec<Captured>>>,
    queue: Arc<Mutex<VecDeque<Canned>>>,
) {
    let mut reader = BufReader::new(stream);
    let mut request_line = String::new();
    if reader.read_line(&mut request_line).await.unwrap_or(0) == 0 {
        return;
    }
    let mut parts = request_line.trim_end().splitn(3, ' ');
    let method = parts.next().unwrap_or_default().to_owned();
    let target = parts.next().unwrap_or_default().to_owned();
    let version = parts.next().unwrap_or_default().to_owned();
    let mut headers = Vec::new();
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line).await.unwrap_or(0) == 0 {
            break;
        }
        let line = line.trim_end_matches(['\r', '\n']);
        if line.is_empty() {
            break;
        }
        if let Some((k, v)) = line.split_once(':') {
            headers.push((k.to_owned(), v.trim_start().to_owned()));
        }
    }
    let length = headers
        .iter()
        .find(|(k, _)| k.eq_ignore_ascii_case("content-length"))
        .and_then(|(_, v)| v.parse::<usize>().ok())
        .unwrap_or(0);
    let mut body = vec![0_u8; length];
    reader.read_exact(&mut body).await.unwrap();
    seen.lock().unwrap().push(Captured {
        method,
        target,
        version,
        headers,
        body,
    });
    let canned = {
        let mut q = queue.lock().unwrap();
        if q.len() > 1 {
            q.pop_front().unwrap()
        } else {
            q.front().cloned().unwrap()
        }
    };
    if canned.hang {
        tokio::time::sleep(std::time::Duration::from_secs(60)).await;
        return;
    }
    let mut head = format!(
        "HTTP/1.1 {} X\r\nContent-Length: {}\r\nConnection: close\r\n",
        canned.status,
        canned.body.len()
    );
    if let Some(ct) = &canned.content_type {
        head.push_str(&format!("Content-Type: {ct}\r\n"));
    }
    head.push_str("\r\n");
    let mut stream = reader.into_inner();
    stream.write_all(head.as_bytes()).await.unwrap();
    stream.write_all(&canned.body).await.unwrap();
    stream.shutdown().await.ok();
}
