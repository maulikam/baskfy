//! The desk's view of Kite Connect (DECISIONS-RS R8.3): `kiteconnect.KiteConnect` as
//! `app/kite_client.py` and `baskfy_execution.gateway` call it.
//!
//! **Two traits, so law 2 is a type, not a convention.** [`KiteReads`] is every read the desk
//! makes (`profile`, `holdings`, `positions`, `margins`, `ltp`/`ohlc`/`quote`, `instruments`,
//! `historical_data`, `orders`, `order_history`, `trades`, `get_gtts`, the basket-margin
//! *calculation*, and the login exchange). [`KiteWrites`] adds the six order verbs
//! (`place_order`, `modify_order`, `cancel_order`, `place_gtt`, `modify_gtt`, `delete_gtt`).
//! `AppState` hands lanes an `Arc<dyn KiteReads>`; only the gateway (`lanes::exec_core`) is
//! built with an `Arc<dyn KiteWrites>`. A page that wanted to place an order would have to be
//! handed the gateway — the one path, guards → risk → rate limit → journal → broker.
//!
//! **The wire shape is the SDK's.** Every method is specified by the request kiteconnect 5.2.1
//! forms ([`KiteCall`]: route name, method, `url_args`, `params`, `is_json`, `query_params`)
//! and the parsing it applies to the answer (`_format_response`, `_parse_instruments`,
//! `_format_historical`, reused from `baskfy_providers::kite`). The replay client
//! ([`crate::fake_kite::ReplayKite`]) records exactly that call, and
//! `tools/parity/desk_harness.py` records the same call from Python by intercepting
//! `KiteConnect._request` — so the two desks' order and GTT calls are compared at the wire.
//!
//! [`KiteHttp`] is the production client: kiteconnect 5.2.1's `_request` over HTTP, built on
//! `baskfy_providers::kite`'s session, headers and response validation. Its writes are reachable
//! only through the gateway, which simulates every one of them under `DRY_RUN`.

use async_trait::async_trait;

pub use baskfy_providers::kite::{KiteError, KiteExceptionKind};
use baskfy_providers::errors::ProviderError;
use baskfy_providers::kite::{
    Body, DEFAULT_ROOT_URI, KITE_HEADER_VERSION, dateutil_parse, encode_query, format_historical,
    format_quote, format_response, parse_instruments, py_json_dumps, requests_error, sdk_headers,
    sdk_http_client, validate_response,
};
use baskfy_providers::net::GuardedClient;
use baskfy_providers::secret::Secret;
use sha2::{Digest, Sha256};

use crate::wire::{PyDict, PyValue};

/// One request as kiteconnect forms it, before HTTP. The harness's unit of comparison.
#[derive(Debug, Clone, PartialEq)]
pub struct KiteCall {
    /// The SDK's route name (`"order.place"`, `"market.quote.ltp"`).
    pub route: String,
    /// `GET` / `POST` / `PUT` / `DELETE`.
    pub method: String,
    /// `url_args` (`{"variety": "regular"}`), or `None`.
    pub url_args: Option<PyDict>,
    /// `params`, or `None`.
    pub params: Option<PyValue>,
    /// `is_json`.
    pub is_json: bool,
    /// `query_params`, or `None`.
    pub query_params: Option<PyDict>,
}

impl KiteCall {
    /// Is this one of the six order/GTT verbs (the calls the harness requires to be equal)?
    #[must_use]
    pub fn is_write(&self) -> bool {
        WRITE_ROUTES.contains(&self.route.as_str())
    }
}

/// The SDK routes that change something at the broker.
pub const WRITE_ROUTES: [&str; 6] = [
    "order.place",
    "order.modify",
    "order.cancel",
    "gtt.place",
    "gtt.modify",
    "gtt.delete",
];

/// `place_order(...)`'s arguments, in the SDK's parameter order (its `locals()` minus `None`).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct OrderParams {
    /// `variety` (`"regular"`).
    pub variety: String,
    /// `exchange`.
    pub exchange: String,
    /// `tradingsymbol`.
    pub tradingsymbol: String,
    /// `transaction_type` (`"BUY"`/`"SELL"`).
    pub transaction_type: String,
    /// `quantity`.
    pub quantity: i64,
    /// `product` (`"CNC"`).
    pub product: String,
    /// `order_type` (`"LIMIT"`/`"MARKET"`).
    pub order_type: String,
    /// `price`.
    pub price: Option<PyValue>,
    /// `validity`.
    pub validity: Option<String>,
    /// `validity_ttl`.
    pub validity_ttl: Option<i64>,
    /// `disclosed_quantity`.
    pub disclosed_quantity: Option<i64>,
    /// `trigger_price`.
    pub trigger_price: Option<PyValue>,
    /// `iceberg_legs`.
    pub iceberg_legs: Option<i64>,
    /// `iceberg_quantity`.
    pub iceberg_quantity: Option<i64>,
    /// `auction_number`.
    pub auction_number: Option<String>,
    /// `algo_id`.
    pub algo_id: Option<String>,
    /// `tag`.
    pub tag: Option<String>,
    /// `market_protection`.
    pub market_protection: Option<PyValue>,
}

impl OrderParams {
    /// The `params` dict the SDK posts: `locals()` in parameter order, `None`s dropped.
    #[must_use]
    pub fn to_params(&self) -> PyDict {
        let mut p = PyDict::new();
        p.insert("variety", PyValue::Str(self.variety.clone()));
        p.insert("exchange", PyValue::Str(self.exchange.clone()));
        p.insert("tradingsymbol", PyValue::Str(self.tradingsymbol.clone()));
        p.insert(
            "transaction_type",
            PyValue::Str(self.transaction_type.clone()),
        );
        p.insert("quantity", PyValue::Int(self.quantity));
        p.insert("product", PyValue::Str(self.product.clone()));
        p.insert("order_type", PyValue::Str(self.order_type.clone()));
        let mut opt = |k: &str, v: Option<PyValue>| {
            if let Some(v) = v
                && v != PyValue::None
            {
                p.insert(k, v);
            }
        };
        opt("price", self.price.clone());
        opt("validity", self.validity.clone().map(PyValue::Str));
        opt("validity_ttl", self.validity_ttl.map(PyValue::Int));
        opt(
            "disclosed_quantity",
            self.disclosed_quantity.map(PyValue::Int),
        );
        opt("trigger_price", self.trigger_price.clone());
        opt("iceberg_legs", self.iceberg_legs.map(PyValue::Int));
        opt("iceberg_quantity", self.iceberg_quantity.map(PyValue::Int));
        opt(
            "auction_number",
            self.auction_number.clone().map(PyValue::Str),
        );
        opt("algo_id", self.algo_id.clone().map(PyValue::Str));
        opt("tag", self.tag.clone().map(PyValue::Str));
        opt("market_protection", self.market_protection.clone());
        p
    }
}

/// One leg of a GTT's `orders` (the keys `_get_gtt_payload` requires).
#[derive(Debug, Clone, PartialEq)]
pub struct GttOrderLeg {
    /// `transaction_type`.
    pub transaction_type: String,
    /// `quantity` (sent as `int(...)`).
    pub quantity: i64,
    /// `order_type`.
    pub order_type: String,
    /// `product`.
    pub product: String,
    /// `price` (sent as `float(...)`).
    pub price: f64,
}

/// `place_gtt(...)` / `modify_gtt(...)`'s arguments.
#[derive(Debug, Clone, PartialEq)]
pub struct GttParams {
    /// `trigger_type` (`"single"` / `"two-leg"`).
    pub trigger_type: String,
    /// `tradingsymbol`.
    pub tradingsymbol: String,
    /// `exchange`.
    pub exchange: String,
    /// `trigger_values` (floats or ints, as the caller passed them).
    pub trigger_values: Vec<PyValue>,
    /// `last_price`.
    pub last_price: PyValue,
    /// `orders`.
    pub orders: Vec<GttOrderLeg>,
}

impl GttParams {
    /// `_get_gtt_payload` then the POST body: `condition` and `orders` as `json.dumps` strings,
    /// and `type`.
    ///
    /// # Errors
    /// `InputException` for a trigger count that does not match the type, as the SDK raises.
    pub fn to_params(&self) -> Result<PyDict, KiteError> {
        let n = self.trigger_values.len();
        if self.trigger_type == "single" && n != 1 {
            return Err(KiteError::kite(
                KiteExceptionKind::Input,
                "invalid `trigger_values` for single leg order type",
            ));
        }
        if self.trigger_type == "two-leg" && n != 2 {
            return Err(KiteError::kite(
                KiteExceptionKind::Input,
                "invalid `trigger_values` for OCO order type",
            ));
        }
        let mut condition = PyDict::new();
        condition.insert("exchange", PyValue::Str(self.exchange.clone()));
        condition.insert("tradingsymbol", PyValue::Str(self.tradingsymbol.clone()));
        condition.insert("trigger_values", PyValue::List(self.trigger_values.clone()));
        condition.insert("last_price", self.last_price.clone());
        let legs: Vec<PyValue> = self
            .orders
            .iter()
            .map(|o| {
                let mut leg = PyDict::new();
                leg.insert("exchange", PyValue::Str(self.exchange.clone()));
                leg.insert("tradingsymbol", PyValue::Str(self.tradingsymbol.clone()));
                leg.insert("transaction_type", PyValue::Str(o.transaction_type.clone()));
                leg.insert("quantity", PyValue::Int(o.quantity));
                leg.insert("order_type", PyValue::Str(o.order_type.clone()));
                leg.insert("product", PyValue::Str(o.product.clone()));
                leg.insert("price", PyValue::Float(o.price));
                PyValue::Map(leg)
            })
            .collect();
        let dumps =
            |v: &PyValue| py_json_dumps(v).map_err(KiteError::Other);
        let mut params = PyDict::new();
        params.insert("condition", PyValue::Str(dumps(&PyValue::Map(condition))?));
        params.insert("orders", PyValue::Str(dumps(&PyValue::List(legs))?));
        params.insert("type", PyValue::Str(self.trigger_type.clone()));
        Ok(params)
    }
}

/// Every read the desk makes of Kite. Answers are the SDK's parsed Python values.
#[async_trait]
pub trait KiteReads: Send + Sync {
    /// `login_url()`.
    fn login_url(&self) -> String;

    /// `set_access_token(token)`.
    fn set_access_token(&self, token: &Secret);

    /// `generate_session(request_token, api_secret)` — the login exchange (a POST, not an
    /// order).
    async fn generate_session(
        &self,
        request_token: &str,
        api_secret: &Secret,
    ) -> Result<PyValue, KiteError>;

    /// `profile()`.
    async fn profile(&self) -> Result<PyValue, KiteError>;

    /// `holdings()`.
    async fn holdings(&self) -> Result<PyValue, KiteError>;

    /// `positions()`.
    async fn positions(&self) -> Result<PyValue, KiteError>;

    /// `margins(segment=None)`.
    async fn margins(&self, segment: Option<&str>) -> Result<PyValue, KiteError>;

    /// `ltp(*instruments)` — `"NSE:INFY"` keys.
    async fn ltp(&self, instruments: &[String]) -> Result<PyValue, KiteError>;

    /// `ohlc(*instruments)`.
    async fn ohlc(&self, instruments: &[String]) -> Result<PyValue, KiteError>;

    /// `quote(*instruments)`.
    async fn quote(&self, instruments: &[String]) -> Result<PyValue, KiteError>;

    /// `instruments(exchange=None)` — the parsed dump.
    async fn instruments(&self, exchange: Option<&str>) -> Result<PyValue, KiteError>;

    /// `historical_data(token, from, to, interval, continuous, oi)`; `from`/`to` as the SDK
    /// sends them (`"%Y-%m-%d %H:%M:%S"` for a datetime, `str(date)` otherwise).
    async fn historical_data(
        &self,
        instrument_token: i64,
        from: &str,
        to: &str,
        interval: &str,
        continuous: bool,
        oi: bool,
    ) -> Result<PyValue, KiteError>;

    /// `orders()`.
    async fn orders(&self) -> Result<PyValue, KiteError>;

    /// `order_history(order_id)`.
    async fn order_history(&self, order_id: &str) -> Result<PyValue, KiteError>;

    /// `trades()`.
    async fn trades(&self) -> Result<PyValue, KiteError>;

    /// `get_gtts()`.
    async fn get_gtts(&self) -> Result<PyValue, KiteError>;

    /// `basket_order_margins(params, consider_positions, mode)` — a calculation, never an order.
    async fn basket_order_margins(
        &self,
        params: &[PyValue],
        consider_positions: bool,
        mode: Option<&str>,
    ) -> Result<PyValue, KiteError>;
}

/// The six verbs that change something at the broker. **Only the gateway holds one** (law 2).
#[async_trait]
pub trait KiteWrites: KiteReads {
    /// `place_order(...)` → the broker's order id.
    async fn place_order(&self, order: &OrderParams) -> Result<String, KiteError>;

    /// `modify_order(variety, order_id, ...)`; `params` are the non-`None` keyword arguments
    /// after `variety` and `order_id`, in the SDK's order.
    async fn modify_order(
        &self,
        variety: &str,
        order_id: &str,
        params: &PyDict,
    ) -> Result<String, KiteError>;

    /// `cancel_order(variety, order_id, parent_order_id=None)`.
    async fn cancel_order(
        &self,
        variety: &str,
        order_id: &str,
        parent_order_id: Option<&str>,
    ) -> Result<String, KiteError>;

    /// `place_gtt(...)` → `{"trigger_id": n}`.
    async fn place_gtt(&self, gtt: &GttParams) -> Result<PyValue, KiteError>;

    /// `modify_gtt(trigger_id, ...)`.
    async fn modify_gtt(&self, trigger_id: i64, gtt: &GttParams) -> Result<PyValue, KiteError>;

    /// `delete_gtt(trigger_id)`.
    async fn delete_gtt(&self, trigger_id: i64) -> Result<PyValue, KiteError>;
}

/// `KiteConnect._default_login_uri`.
pub const LOGIN_URI: &str = "https://kite.zerodha.com/connect/login";

enum Payload<'a> {
    Empty,
    Form(&'a PyDict),
    Json(&'a PyValue),
}

/// `requests`' `urlencode(params, doseq=True)` pairs: `None` dropped, a list repeated.
fn pairs(params: &PyDict) -> Vec<(String, String)> {
    let mut out = Vec::new();
    for (k, v) in params.iter() {
        match v {
            PyValue::None => {}
            PyValue::List(items) => {
                for item in items.iter().filter(|i| **i != PyValue::None) {
                    out.push((k.to_owned(), item.py_str()));
                }
            }
            other => out.push((k.to_owned(), other.py_str())),
        }
    }
    out
}

fn key_error(key: &str) -> KiteError {
    KiteError::Other(ProviderError::foreign("KeyError", format!("'{key}'")))
}

/// `data[key]`.
fn subscript(data: &PyValue, key: &str) -> Result<PyValue, KiteError> {
    match data {
        PyValue::Map(m) => m.get(key).cloned().ok_or_else(|| key_error(key)),
        other => Err(KiteError::Other(ProviderError::foreign(
            "TypeError",
            format!(
                "{} indices must be integers or slices, not str",
                other.type_name()
            ),
        ))),
    }
}

fn order_id(data: &PyValue) -> Result<String, KiteError> {
    Ok(subscript(data, "order_id")?.py_str())
}

fn str_dict<const N: usize>(items: [(&str, &str); N]) -> PyDict {
    let mut d = PyDict::new();
    for (k, v) in items {
        d.insert(k, PyValue::Str(v.to_owned()));
    }
    d
}

/// `kiteconnect.KiteConnect` over HTTP: the desk's production broker.
pub struct KiteHttp {
    api_key: Secret,
    access_token: std::sync::Mutex<Secret>,
    root: reqwest::Url,
    http: GuardedClient,
}

impl std::fmt::Debug for KiteHttp {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("KiteHttp")
            .field("root", &self.root.as_str())
            .finish_non_exhaustive()
    }
}

impl KiteHttp {
    /// `KiteConnect(api_key=...)` against `https://api.kite.trade`.
    ///
    /// # Errors
    /// As [`KiteHttp::with_root`].
    pub fn new(api_key: &str) -> Result<Self, KiteError> {
        Self::with_root(api_key, DEFAULT_ROOT_URI, None)
    }

    /// `KiteConnect(api_key=..., root=..., timeout=...)`.
    ///
    /// # Errors
    /// `ValueError` for an unparseable root; the TLS backend's failure.
    pub fn with_root(api_key: &str, root: &str, timeout: Option<f64>) -> Result<Self, KiteError> {
        let root = reqwest::Url::parse(root)
            .map_err(|e| ProviderError::value_error(format!("invalid Kite root: {e}")))?;
        let (http, _) = sdk_http_client(timeout)?;
        Ok(Self {
            api_key: Secret::new(api_key),
            access_token: std::sync::Mutex::new(Secret::default()),
            root,
            http,
        })
    }

    fn token(&self) -> Secret {
        self.access_token
            .lock()
            .map(|t| t.clone())
            .unwrap_or_default()
    }

    async fn request(
        &self,
        method: reqwest::Method,
        uri: &str,
        query: Option<&PyDict>,
        payload: Payload<'_>,
    ) -> Result<Body, KiteError> {
        let joined = self
            .root
            .join(uri)
            .map_err(|e| ProviderError::value_error(format!("invalid Kite path: {e}")))?;
        let mut url = joined.to_string();
        let query = query.map(pairs).unwrap_or_default();
        if !query.is_empty() {
            url.push(if joined.query().is_some() { '&' } else { '?' });
            url.push_str(&encode_query(&query));
        }
        let mut request = sdk_headers(
            self.http.request(method, &url)?,
            &self.api_key,
            &self.token(),
        );
        match payload {
            Payload::Empty => {}
            Payload::Form(params) => {
                let body = pairs(params);
                if !body.is_empty() {
                    request = request
                        .header("Content-Type", "application/x-www-form-urlencoded")
                        .body(encode_query(&body).into_bytes());
                }
            }
            Payload::Json(body) => {
                request = request
                    .header("Content-Type", "application/json")
                    .body(py_json_dumps(body)?.into_bytes());
            }
        }
        let response = request.send().await.map_err(requests_error)?;
        let status = response.status().as_u16();
        let content_type = response
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .map(|v| String::from_utf8_lossy(v.as_bytes()).into_owned());
        let content = response.bytes().await.map_err(requests_error)?;
        validate_response(status, content_type, &content)
    }

    async fn data(
        &self,
        method: reqwest::Method,
        uri: &str,
        query: Option<&PyDict>,
        payload: Payload<'_>,
    ) -> Result<PyValue, KiteError> {
        Ok(match self.request(method, uri, query, payload).await? {
            Body::Json(v) => v,
            Body::Csv(bytes) => PyValue::Bytes(bytes),
        })
    }

    async fn get(&self, uri: &str, query: Option<&PyDict>) -> Result<PyValue, KiteError> {
        self.data(reqwest::Method::GET, uri, query, Payload::Empty)
            .await
    }

    async fn quotes(&self, uri: &str, instruments: &[String]) -> Result<PyValue, KiteError> {
        let mut q = PyDict::new();
        q.insert(
            "i",
            PyValue::List(instruments.iter().cloned().map(PyValue::Str).collect()),
        );
        self.get(uri, Some(&q)).await
    }

    async fn instrument_dump(&self, uri: &str) -> Result<PyValue, KiteError> {
        match self.get(uri, None).await? {
            PyValue::Bytes(bytes) => parse_instruments(&bytes).map_err(KiteError::Other),
            other => Err(KiteError::Other(ProviderError::foreign(
                "TypeError",
                format!(
                    "initial_value must be str or None, not {}",
                    other.type_name()
                ),
            ))),
        }
    }
}

#[async_trait]
impl KiteReads for KiteHttp {
    fn login_url(&self) -> String {
        format!(
            "{LOGIN_URI}?api_key={}&v={KITE_HEADER_VERSION}",
            self.api_key.expose()
        )
    }

    fn set_access_token(&self, token: &Secret) {
        if let Ok(mut slot) = self.access_token.lock() {
            *slot = token.clone();
        }
    }

    async fn generate_session(
        &self,
        request_token: &str,
        api_secret: &Secret,
    ) -> Result<PyValue, KiteError> {
        let mut h = Sha256::new();
        h.update(self.api_key.expose().as_bytes());
        h.update(request_token.as_bytes());
        h.update(api_secret.expose().as_bytes());
        let checksum: String = h.finalize().iter().map(|b| format!("{b:02x}")).collect();
        let params = str_dict([
            ("api_key", self.api_key.expose()),
            ("request_token", request_token),
            ("checksum", &checksum),
        ]);
        let mut resp = self
            .data(
                reqwest::Method::POST,
                "/session/token",
                None,
                Payload::Form(&params),
            )
            .await?;
        if let PyValue::Map(m) = &resp
            && let Some(token) = m.get("access_token")
        {
            self.set_access_token(&Secret::new(token.py_str()));
        }
        let login_time = subscript(&resp, "login_time")?;
        if let PyValue::Str(t) = &login_time
            && t.chars().count() == 19
        {
            let parsed = dateutil_parse(t).map_err(KiteError::Other)?;
            if let PyValue::Map(m) = &mut resp {
                m.insert("login_time", PyValue::DateTime(parsed));
            }
        }
        Ok(resp)
    }

    async fn profile(&self) -> Result<PyValue, KiteError> {
        self.get("/user/profile", None).await
    }

    async fn holdings(&self) -> Result<PyValue, KiteError> {
        self.get("/portfolio/holdings", None).await
    }

    async fn positions(&self) -> Result<PyValue, KiteError> {
        self.get("/portfolio/positions", None).await
    }

    async fn margins(&self, segment: Option<&str>) -> Result<PyValue, KiteError> {
        match segment.filter(|s| !s.is_empty()) {
            Some(s) => self.get(&format!("/user/margins/{s}"), None).await,
            None => self.get("/user/margins", None).await,
        }
    }

    async fn ltp(&self, instruments: &[String]) -> Result<PyValue, KiteError> {
        self.quotes("/quote/ltp", instruments).await
    }

    async fn ohlc(&self, instruments: &[String]) -> Result<PyValue, KiteError> {
        self.quotes("/quote/ohlc", instruments).await
    }

    async fn quote(&self, instruments: &[String]) -> Result<PyValue, KiteError> {
        format_quote(&self.quotes("/quote", instruments).await?)
    }

    async fn instruments(&self, exchange: Option<&str>) -> Result<PyValue, KiteError> {
        match exchange.filter(|e| !e.is_empty()) {
            Some(e) => self.instrument_dump(&format!("/instruments/{e}")).await,
            None => self.instrument_dump("/instruments").await,
        }
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
        let mut q = str_dict([("from", from), ("to", to), ("interval", interval)]);
        q.insert("continuous", PyValue::Int(i64::from(continuous)));
        q.insert("oi", PyValue::Int(i64::from(oi)));
        let data = self
            .get(
                &format!("/instruments/historical/{instrument_token}/{interval}"),
                Some(&q),
            )
            .await?;
        format_historical(&data).map_err(KiteError::Other)
    }

    async fn orders(&self) -> Result<PyValue, KiteError> {
        format_response(&self.get("/orders", None).await?).map_err(KiteError::Other)
    }

    async fn order_history(&self, order_id: &str) -> Result<PyValue, KiteError> {
        format_response(&self.get(&format!("/orders/{order_id}"), None).await?)
            .map_err(KiteError::Other)
    }

    async fn trades(&self) -> Result<PyValue, KiteError> {
        format_response(&self.get("/trades", None).await?).map_err(KiteError::Other)
    }

    async fn get_gtts(&self) -> Result<PyValue, KiteError> {
        self.get("/gtt/triggers", None).await
    }

    async fn basket_order_margins(
        &self,
        params: &[PyValue],
        consider_positions: bool,
        mode: Option<&str>,
    ) -> Result<PyValue, KiteError> {
        let mut q = PyDict::new();
        q.insert("consider_positions", PyValue::Bool(consider_positions));
        q.insert(
            "mode",
            mode.map_or(PyValue::None, |m| PyValue::Str(m.to_owned())),
        );
        let body = PyValue::List(params.to_vec());
        self.data(
            reqwest::Method::POST,
            "/margins/basket",
            Some(&q),
            Payload::Json(&body),
        )
        .await
    }
}

#[async_trait]
impl KiteWrites for KiteHttp {
    async fn place_order(&self, order: &OrderParams) -> Result<String, KiteError> {
        let params = order.to_params();
        let data = self
            .data(
                reqwest::Method::POST,
                &format!("/orders/{}", order.variety),
                None,
                Payload::Form(&params),
            )
            .await?;
        order_id(&data)
    }

    async fn modify_order(
        &self,
        variety: &str,
        order_id_: &str,
        params: &PyDict,
    ) -> Result<String, KiteError> {
        let mut all = str_dict([("variety", variety), ("order_id", order_id_)]);
        for (k, v) in params.iter() {
            all.insert(k, v.clone());
        }
        let data = self
            .data(
                reqwest::Method::PUT,
                &format!("/orders/{variety}/{order_id_}"),
                None,
                Payload::Form(&all),
            )
            .await?;
        order_id(&data)
    }

    async fn cancel_order(
        &self,
        variety: &str,
        order_id_: &str,
        parent_order_id: Option<&str>,
    ) -> Result<String, KiteError> {
        let mut q = PyDict::new();
        q.insert(
            "parent_order_id",
            parent_order_id.map_or(PyValue::None, |p| PyValue::Str(p.to_owned())),
        );
        let data = self
            .data(
                reqwest::Method::DELETE,
                &format!("/orders/{variety}/{order_id_}"),
                Some(&q),
                Payload::Empty,
            )
            .await?;
        order_id(&data)
    }

    async fn place_gtt(&self, gtt: &GttParams) -> Result<PyValue, KiteError> {
        let params = gtt.to_params()?;
        self.data(
            reqwest::Method::POST,
            "/gtt/triggers",
            None,
            Payload::Form(&params),
        )
        .await
    }

    async fn modify_gtt(&self, trigger_id: i64, gtt: &GttParams) -> Result<PyValue, KiteError> {
        let params = gtt.to_params()?;
        self.data(
            reqwest::Method::PUT,
            &format!("/gtt/triggers/{trigger_id}"),
            None,
            Payload::Form(&params),
        )
        .await
    }

    async fn delete_gtt(&self, trigger_id: i64) -> Result<PyValue, KiteError> {
        self.data(
            reqwest::Method::DELETE,
            &format!("/gtt/triggers/{trigger_id}"),
            None,
            Payload::Empty,
        )
        .await
    }
}
