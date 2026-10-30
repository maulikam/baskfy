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
//! No production HTTP client is in this crate yet: the desk's live Kite is a cutover item
//! (R8.0), and no agent ever points one at a live account.

use async_trait::async_trait;

pub use baskfy_providers::kite::{KiteError, KiteExceptionKind};
use baskfy_providers::secret::Secret;

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
            |v: &PyValue| baskfy_providers::kite::py_json_dumps(v).map_err(KiteError::Other);
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
