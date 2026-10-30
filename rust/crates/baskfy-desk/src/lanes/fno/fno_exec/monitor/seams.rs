//! The FO monitor's seams (`app.fno_monitor`'s "the seams" and "the Postgres and Kite seams"):
//! the NFO master, the broker's basket margin, the index quote and the exchange calendar with
//! the F&O bhavcopy — behind small traits so a replay drives the engine from fixtures — and
//! their real readers ([`PgMarket`], [`PgKiteInstruments`], [`KiteFoQuotes`],
//! [`KiteIndexQuotes`], [`KiteMargins`]). Every Kite call here is a **read**; none places,
//! modifies or cancels anything (law 2: the order verbs are the gateway's alone).

use std::collections::BTreeMap;
use std::sync::Arc;

use async_trait::async_trait;
use chrono::NaiveDate;
use rust_decimal::Decimal;
use tokio::sync::Mutex;

use super::super::execute::{ExecError, FoQuoteSource};
use super::super::store::{FoQuote, FoStore};
use crate::kite::KiteReads;
use crate::lanes::exec_core::kite_client::DeskKite;
use crate::lanes::options::py::{self, col, int, text};
use crate::lanes::options::store::t;
use crate::storage::{Conn, Param};
use crate::wire::{PyDict, PyValue, dict};

/// `F3_INDEX_NAMES`: F3's underlyings and their index names on NSE.
#[must_use]
pub fn f3_index_name(underlying: &str) -> Option<&'static str> {
    match underlying {
        "NIFTY" => Some("NIFTY 50"),
        "BANKNIFTY" => Some("NIFTY BANK"),
        _ => None,
    }
}

/// `Contract`: one NFO contract as the master lists it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Contract {
    /// `tradingsymbol`.
    pub tradingsymbol: String,
    /// `instrument_token`.
    pub instrument_token: i64,
    /// `lot_size`.
    pub lot_size: i64,
    /// `expiry`.
    pub expiry: NaiveDate,
}

/// `Instruments`: options from `op_contract`, futures from Kite's `instruments("NFO")`.
#[async_trait]
pub trait Instruments: Send + Sync {
    /// `option(underlying, expiry, strike, option_type)`.
    ///
    /// # Errors
    /// The read (Python's exception text).
    async fn option(
        &self,
        underlying: &str,
        expiry: NaiveDate,
        strike: Decimal,
        option_type: &str,
    ) -> Result<Option<Contract>, ExecError>;

    /// `future(symbol, expiry)`.
    ///
    /// # Errors
    /// The read.
    async fn future(&self, symbol: &str, expiry: NaiveDate) -> Result<Option<Contract>, ExecError>;

    /// `next_future(symbol, after)`: the next **monthly** future after `after`.
    ///
    /// # Errors
    /// The read.
    async fn next_future(
        &self,
        symbol: &str,
        after: NaiveDate,
    ) -> Result<Option<Contract>, ExecError>;
}

/// `MarginLeg`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MarginLeg {
    /// `tradingsymbol`.
    pub tradingsymbol: String,
    /// `BUY` / `SELL`.
    pub side: String,
    /// Units.
    pub quantity: i64,
    /// The limit the leg would be priced at.
    pub price: Decimal,
}

/// `MarginQuote`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MarginQuote {
    /// The basket's margin, ₹.
    pub required_inr: Decimal,
    /// Free margin, ₹.
    pub free_inr: Decimal,
}

/// `MarginSource`: `basket_order_margins` for the plan's legs against the free margin, or
/// `None` when unreadable.
#[async_trait]
pub trait MarginSource: Send + Sync {
    /// `margins(legs)`.
    ///
    /// # Errors
    /// What Python lets escape (a figure `Decimal` refuses).
    async fn margins(&self, legs: &[MarginLeg]) -> Result<Option<MarginQuote>, ExecError>;
}

/// A settable answer (tests and replays: `margins=lambda _legs: world.margin`).
#[derive(Debug, Default)]
pub struct StaticMargin(pub std::sync::Mutex<Option<MarginQuote>>);

impl StaticMargin {
    /// A source answering `quote`.
    #[must_use]
    pub fn new(quote: Option<MarginQuote>) -> Self {
        Self(std::sync::Mutex::new(quote))
    }

    /// Change the answer.
    pub fn set(&self, quote: Option<MarginQuote>) {
        if let Ok(mut q) = self.0.lock() {
            *q = quote;
        }
    }
}

#[async_trait]
impl MarginSource for StaticMargin {
    async fn margins(&self, legs: &[MarginLeg]) -> Result<Option<MarginQuote>, ExecError> {
        let _ = legs;
        Ok(self.0.lock().ok().and_then(|q| *q))
    }
}

/// `IndexQuote`: the index now — its last print and the session's open.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct IndexQuote {
    /// The last print.
    pub last: Decimal,
    /// The session's open.
    pub open: Option<Decimal>,
}

/// `IndexSource`: the index's quote by underlying, or `None` without a session. An `Err` is
/// the exception the reader raised (`_f3_index` turns it into a refusal by name).
#[async_trait]
pub trait IndexSource: Send + Sync {
    /// `index_quotes(underlying)`.
    ///
    /// # Errors
    /// The reader's exception text.
    async fn index(&self, underlying: &str) -> Result<Option<IndexQuote>, String>;
}

/// `_no_index`: the default — no index, so F3 refuses by name.
#[derive(Debug, Clone, Copy, Default)]
pub struct NoIndex;

#[async_trait]
impl IndexSource for NoIndex {
    async fn index(&self, underlying: &str) -> Result<Option<IndexQuote>, String> {
        let _ = underlying;
        Ok(None)
    }
}

/// A settable index quote per underlying (tests and replays); `None` for every underlying is
/// `set_all(None)`.
#[derive(Debug, Default)]
pub struct StaticIndex(pub std::sync::Mutex<BTreeMap<String, Option<IndexQuote>>>);

impl StaticIndex {
    /// The same quote for every underlying (`index_quotes=lambda _u: world.index`).
    pub fn set_all(&self, quote: Option<IndexQuote>) {
        if let Ok(mut m) = self.0.lock() {
            m.clear();
            m.insert("*".to_owned(), quote);
        }
    }

    /// One underlying's quote.
    pub fn set(&self, underlying: &str, quote: Option<IndexQuote>) {
        if let Ok(mut m) = self.0.lock() {
            m.insert(underlying.to_owned(), quote);
        }
    }
}

#[async_trait]
impl IndexSource for StaticIndex {
    async fn index(&self, underlying: &str) -> Result<Option<IndexQuote>, String> {
        let m = self
            .0
            .lock()
            .map_err(|_| "RuntimeError: poisoned".to_owned())?;
        Ok(m.get(underlying).or_else(|| m.get("*")).copied().flatten())
    }
}

/// `Print`: one contract's bhavcopy close and settle.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Print {
    /// `close`.
    pub close: Decimal,
    /// `settle`.
    pub settle: Decimal,
}

/// `PrintKey`: `(symbol, expiry, strike or None for a future, 'CE' | 'PE' | 'XX')`.
pub type PrintKey = (String, NaiveDate, Option<Decimal>, String);

/// `Market`: the exchange calendar and the F&O bhavcopy.
#[async_trait]
pub trait Market: Send + Sync {
    /// `sessions()`.
    ///
    /// # Errors
    /// The read.
    async fn sessions(&self) -> Result<Vec<NaiveDate>, ExecError>;

    /// `landed(day)`.
    ///
    /// # Errors
    /// The read.
    async fn landed(&self, day: NaiveDate) -> Result<bool, ExecError>;

    /// `prints(day, keys)`: the keys found (a missing contract is absent, never a zero).
    ///
    /// # Errors
    /// The read.
    async fn prints(
        &self,
        day: NaiveDate,
        keys: &[PrintKey],
    ) -> Result<BTreeMap<PrintKey, Print>, ExecError>;
}

/// `ScanRow`.
#[derive(Debug, Clone, PartialEq)]
pub struct ScanRow {
    /// `fo_scan.id`.
    pub id: i64,
    /// `sleeve`.
    pub sleeve: String,
    /// `trade_date`.
    pub trade_date: NaiveDate,
    /// `symbol`.
    pub symbol: String,
    /// `state`.
    pub state: String,
    /// `rv20`.
    pub rv20: Option<Decimal>,
    /// `detail`.
    pub detail: PyDict,
}

fn data(message: impl Into<String>) -> ExecError {
    ExecError::Other(message.into())
}

/// `scan_candidates(store, sleeves, day, state=CANDIDATE)`: the latest scan before `day` for
/// these sleeves, rows in `state` only.
///
/// # Errors
/// The database.
pub async fn scan_candidates(
    store: &mut FoStore<'_>,
    sleeves: &[&str],
    day: NaiveDate,
    state: &str,
) -> Result<Vec<ScanRow>, ExecError> {
    let names: Vec<String> = sleeves.iter().map(|s| (*s).to_owned()).collect();
    let any = py::text_array_sql(&names);
    let table = store.t("fo_scan");
    let sql = format!(
        "SELECT id, sleeve, trade_date, symbol, state, rv20, detail FROM {table} \
         WHERE user_id = ? AND sleeve::text = ANY({any}) AND state = ? AND trade_date = (\
         SELECT max(trade_date) FROM {table} WHERE user_id = ? \
         AND sleeve::text = ANY({any}) AND trade_date < ?) ORDER BY sleeve, symbol"
    );
    let uid = store.user_id;
    let rows = store
        .rows(
            &sql,
            &[
                Param::Int(uid),
                Param::Text(state.to_owned()),
                Param::Int(uid),
                Param::Date(day),
            ],
        )
        .await?;
    let mut out = Vec::with_capacity(rows.len());
    for r in &rows {
        out.push(ScanRow {
            id: int(col(r, "id")),
            sleeve: text(col(r, "sleeve")),
            trade_date: py::opt_day(col(r, "trade_date"))
                .ok_or_else(|| data("TypeError: trade_date is not a date"))?,
            symbol: text(col(r, "symbol")),
            state: text(col(r, "state")),
            rv20: py::dec(col(r, "rv20")),
            detail: py::json_obj(col(r, "detail")),
        });
    }
    Ok(out)
}

/// `sleeve_capital(store, group)`: `fo_sleeve_config.capital_inr`, `0` without a row.
///
/// # Errors
/// The database.
pub async fn sleeve_capital(store: &mut FoStore<'_>, group: &str) -> Result<Decimal, ExecError> {
    let sql = format!(
        "SELECT capital_inr FROM {} WHERE user_id = ? AND sleeve = ?",
        store.t("fo_sleeve_config")
    );
    let uid = store.user_id;
    let row = store
        .one(&sql, &[Param::Int(uid), Param::Text(group.to_owned())])
        .await?;
    match row {
        None => Ok(Decimal::ZERO),
        Some(r) => py::dec(col(&r, "capital_inr")).ok_or_else(|| {
            data(format!(
                "InvalidOperation: capital_inr {:?} is not a number",
                text(col(&r, "capital_inr"))
            ))
        }),
    }
}

// --- the Postgres seams --------------------------------------------------------------------------

/// `PgMarket`: `trading_day`, `fo_ingest_day` and `fo_contract_daily` (market data, shared).
/// It holds its own connection (the desk's are autocommit, so this reads what the store's
/// connection wrote, as Python's one connection does).
pub struct PgMarket {
    conn: Mutex<Conn>,
    schema: String,
    sessions: Mutex<Option<Vec<NaiveDate>>>,
}

impl PgMarket {
    /// `PgMarket(conn, schema=SCHEMA)`.
    #[must_use]
    pub fn new(conn: Conn) -> Self {
        Self {
            conn: Mutex::new(conn),
            schema: super::super::store::SCHEMA.to_owned(),
            sessions: Mutex::new(None),
        }
    }
}

#[async_trait]
impl Market for PgMarket {
    async fn sessions(&self) -> Result<Vec<NaiveDate>, ExecError> {
        let mut cache = self.sessions.lock().await;
        if let Some(s) = cache.as_ref() {
            return Ok(s.clone());
        }
        let table = t(&self.schema, "trading_day");
        let sql = format!(
            "SELECT date FROM {table} WHERE is_trading_day AND exchange_id = (SELECT \
             min(exchange_id) FROM {table}) ORDER BY date"
        );
        let rows = self
            .conn
            .lock()
            .await
            .execute(&sql, &[])
            .await
            .map_err(|e| data(e.to_string()))?;
        let mut out = Vec::with_capacity(rows.rows.len());
        for r in &rows.rows {
            out.push(
                py::opt_day(col(r, "date"))
                    .ok_or_else(|| data("TypeError: trading_day.date is not a date"))?,
            );
        }
        *cache = Some(out.clone());
        Ok(out)
    }

    async fn landed(&self, day: NaiveDate) -> Result<bool, ExecError> {
        let sql = format!(
            "SELECT status FROM {} WHERE trade_date = ?",
            t(&self.schema, "fo_ingest_day")
        );
        let rows = self
            .conn
            .lock()
            .await
            .execute(&sql, &[Param::Date(day)])
            .await
            .map_err(|e| data(e.to_string()))?;
        Ok(rows
            .rows
            .first()
            .is_some_and(|r| text(col(r, "status")) == "INGESTED"))
    }

    async fn prints(
        &self,
        day: NaiveDate,
        keys: &[PrintKey],
    ) -> Result<BTreeMap<PrintKey, Print>, ExecError> {
        let sql = format!(
            "SELECT close, settle FROM {} WHERE trade_date = ? AND symbol = ? AND expiry = ? \
             AND strike = ? AND option_type = ?",
            t(&self.schema, "fo_contract_daily")
        );
        let mut out = BTreeMap::new();
        let mut conn = self.conn.lock().await;
        for key in keys {
            let (symbol, expiry, strike, kind) = key;
            let rows = conn
                .execute(
                    &sql,
                    &[
                        Param::Date(day),
                        Param::Text(symbol.clone()),
                        Param::Date(*expiry),
                        Param::Decimal(strike.unwrap_or(Decimal::ZERO)),
                        Param::Text(kind.clone()),
                    ],
                )
                .await
                .map_err(|e| data(e.to_string()))?;
            if let Some(r) = rows.rows.first() {
                let close = py::dec(col(r, "close"))
                    .ok_or_else(|| data("InvalidOperation: close is not a number"))?;
                let settle = py::dec(col(r, "settle"))
                    .ok_or_else(|| data("InvalidOperation: settle is not a number"))?;
                out.insert(key.clone(), Print { close, settle });
            }
        }
        Ok(out)
    }
}

/// The futures master's reader: `kite.instruments("NFO")` (a read; one general slot).
#[async_trait]
pub trait NfoDump: Send + Sync {
    /// `kite.instruments("NFO")`.
    ///
    /// # Errors
    /// The read's exception text.
    async fn nfo(&self) -> Result<Vec<PyValue>, String>;
}

#[async_trait]
impl NfoDump for DeskKite {
    async fn nfo(&self) -> Result<Vec<PyValue>, String> {
        self.instruments("NFO")
            .await
            .map_err(|e| format!("{}: {}", e.python_type, e.message))
    }
}

/// `PgKiteInstruments`: options from `op_contract` (FO2 widened it to every F&O underlying);
/// futures from Kite's NFO dump, read once per process (`op_contract` holds options only).
pub struct PgKiteInstruments {
    conn: Option<Mutex<Conn>>,
    kite: Arc<dyn NfoDump>,
    schema: String,
    futures: Mutex<Option<Vec<PyDict>>>,
}

impl PgKiteInstruments {
    /// `PgKiteInstruments(conn, kite, schema=SCHEMA)`; `conn` may be absent where only the
    /// futures are read (`PgKiteInstruments(conn=None, kite=kite)`).
    #[must_use]
    pub fn new(conn: Option<Conn>, kite: Arc<dyn NfoDump>) -> Self {
        Self {
            conn: conn.map(Mutex::new),
            kite,
            schema: super::super::store::SCHEMA.to_owned(),
            futures: Mutex::new(None),
        }
    }

    async fn future_rows(&self, symbol: &str) -> Result<Vec<Contract>, ExecError> {
        let mut cache = self.futures.lock().await;
        if cache.is_none() {
            let dump = self.kite.nfo().await.map_err(ExecError::Other)?;
            let mut rows = Vec::new();
            for r in dump {
                let PyValue::Map(m) = r else { continue };
                if m.get("instrument_type").map(PyValue::py_str).as_deref() == Some("FUT") {
                    rows.push(m);
                }
            }
            *cache = Some(rows);
        }
        let mut out = Vec::new();
        for r in cache.as_deref().unwrap_or_default() {
            if r.get("name").map(PyValue::py_str).as_deref() != Some(symbol) {
                continue;
            }
            let expiry = match r.get("expiry") {
                None | Some(PyValue::None) => continue,
                Some(PyValue::Str(s)) if s.is_empty() => continue,
                Some(v) => py::opt_day_py(Some(v)).ok_or_else(|| {
                    data(format!(
                        "ValueError: Invalid isoformat string: '{}'",
                        v.py_str()
                    ))
                })?,
            };
            let need = |k: &str| {
                r.get(k)
                    .cloned()
                    .ok_or_else(|| data(format!("KeyError: '{k}'")))
            };
            out.push(Contract {
                tradingsymbol: need("tradingsymbol")?.py_str(),
                instrument_token: py::py_int(Some(&need("instrument_token")?), 0),
                lot_size: py::py_int(Some(&need("lot_size")?), 0),
                expiry,
            });
        }
        out.sort_by_key(|c| c.expiry);
        Ok(out)
    }
}

#[async_trait]
impl Instruments for PgKiteInstruments {
    async fn option(
        &self,
        underlying: &str,
        expiry: NaiveDate,
        strike: Decimal,
        option_type: &str,
    ) -> Result<Option<Contract>, ExecError> {
        let Some(conn) = self.conn.as_ref() else {
            return Err(data(
                "AttributeError: 'NoneType' object has no attribute 'execute'",
            ));
        };
        let sql = format!(
            "SELECT tradingsymbol, instrument_token, lot_size, expiry FROM {} WHERE \
             underlying = ? AND expiry = ? AND strike = ? AND option_type = ? AND NOT expired",
            t(&self.schema, "op_contract")
        );
        let rows = conn
            .lock()
            .await
            .execute(
                &sql,
                &[
                    Param::Text(underlying.to_owned()),
                    Param::Date(expiry),
                    Param::Decimal(strike),
                    Param::Text(option_type.to_owned()),
                ],
            )
            .await
            .map_err(|e| data(e.to_string()))?;
        let Some(r) = rows.rows.first() else {
            return Ok(None);
        };
        Ok(Some(Contract {
            tradingsymbol: text(col(r, "tradingsymbol")),
            instrument_token: int(col(r, "instrument_token")),
            lot_size: int(col(r, "lot_size")),
            expiry: py::opt_day(col(r, "expiry"))
                .ok_or_else(|| data("TypeError: expiry is not a date"))?,
        }))
    }

    async fn future(&self, symbol: &str, expiry: NaiveDate) -> Result<Option<Contract>, ExecError> {
        Ok(self
            .future_rows(symbol)
            .await?
            .into_iter()
            .find(|c| c.expiry == expiry))
    }

    async fn next_future(
        &self,
        symbol: &str,
        after: NaiveDate,
    ) -> Result<Option<Contract>, ExecError> {
        let rows = self.future_rows(symbol).await?;
        let listed: Vec<NaiveDate> = rows.iter().map(|c| c.expiry).collect();
        let monthly = baskfy_core::fno::calendar::monthly_expiries(&listed);
        Ok(rows
            .into_iter()
            .find(|c| c.expiry > after && monthly.contains(&c.expiry)))
    }
}

// --- the Kite seams (reads only) ---------------------------------------------------------------

/// The one Kite read the quote seams make: `kite.quote_raw(keys)`.
#[async_trait]
pub trait QuoteRaw: Send + Sync {
    /// `kite.quote_raw(keys)`.
    ///
    /// # Errors
    /// The read's exception text (`"<Type>: <message>"`).
    async fn quote_raw(&self, keys: &[String]) -> Result<PyValue, String>;
}

#[async_trait]
impl QuoteRaw for DeskKite {
    async fn quote_raw(&self, keys: &[String]) -> Result<PyValue, String> {
        Self::quote_raw(self, keys)
            .await
            .map_err(|e| format!("{}: {}", e.python_type, e.message))
    }
}

/// `kite_quotes(kite)`: books for NFO tradingsymbols from `quote_raw`, ≤ 500 keys a call,
/// each key once.
pub struct KiteFoQuotes(pub Arc<dyn QuoteRaw>);

#[async_trait]
impl FoQuoteSource for KiteFoQuotes {
    async fn quotes(&self, symbols: &[String]) -> Result<BTreeMap<String, FoQuote>, ExecError> {
        let mut keys: Vec<String> = Vec::new();
        for s in symbols {
            let k = format!("NFO:{s}");
            if !keys.contains(&k) {
                keys.push(k);
            }
        }
        let mut out = BTreeMap::new();
        for chunk in keys.chunks(500) {
            let payload = self.0.quote_raw(chunk).await.map_err(ExecError::Other)?;
            crate::lanes::fno::desk::books_from_payload(&payload, &mut out)
                .map_err(ExecError::Other)?;
        }
        Ok(out)
    }
}

/// `kite_index_quotes(kite)`: the index's last print and the session's open from `quote_raw`
/// (`NSE:NIFTY 50`).
pub struct KiteIndexQuotes(pub Arc<dyn QuoteRaw>);

#[async_trait]
impl IndexSource for KiteIndexQuotes {
    async fn index(&self, underlying: &str) -> Result<Option<IndexQuote>, String> {
        let Some(name) = f3_index_name(underlying) else {
            return Ok(None);
        };
        let key = format!("NSE:{name}");
        let payload = self.0.quote_raw(std::slice::from_ref(&key)).await?;
        let Some(PyValue::Map(q)) = (if payload.truthy() {
            match &payload {
                PyValue::Map(m) => m.get(&key).cloned(),
                _ => None,
            }
        } else {
            None
        }) else {
            return Ok(None);
        };
        let Some(last) = py::dec_py(q.get("last_price")) else {
            return Ok(None);
        };
        let open = match q.get("ohlc") {
            Some(PyValue::Map(ohlc)) => py::dec_py(ohlc.get("open")),
            _ => None,
        };
        Ok(Some(IndexQuote { last, open }))
    }
}

/// What `kite_margins` reads: one general slot, the raw basket calculation, the free margin.
#[async_trait]
pub trait MarginReads: Send + Sync {
    /// `kite.limits.slot("general")`.
    async fn slot(&self);
    /// `kite.kc.basket_order_margins(orders, consider_positions=False)`.
    ///
    /// # Errors
    /// The read's exception text.
    async fn basket(&self, orders: &[PyValue]) -> Result<PyValue, String>;
    /// `kite.margins("equity")`.
    ///
    /// # Errors
    /// The read's exception text.
    async fn equity(&self) -> Result<PyValue, String>;
}

/// The desk's Kite wrapper and its raw broker, as `kite_margins(kite)` reads them.
pub struct DeskMarginReads {
    /// `kite` (the wrapper: its limiter and `margins`).
    pub kite: Arc<DeskKite>,
    /// `kite.kc` (the raw broker's reads).
    pub kc: Arc<dyn KiteReads>,
}

#[async_trait]
impl MarginReads for DeskMarginReads {
    async fn slot(&self) {
        let _ = self.kite.limits().await.slot("general").await;
    }

    async fn basket(&self, orders: &[PyValue]) -> Result<PyValue, String> {
        self.kc
            .basket_order_margins(orders, false, None)
            .await
            .map_err(|e| {
                let c = crate::lanes::exec_core::kite_client::ClientError::from(e);
                format!("{}: {}", c.python_type, c.message)
            })
    }

    async fn equity(&self) -> Result<PyValue, String> {
        self.kite
            .margins(Some("equity"))
            .await
            .map_err(|e| format!("{}: {}", e.python_type, e.message))
    }
}

/// `kite_margins(kite)`: `basket_order_margins` (NRML, the basket's hedge benefit counted)
/// against the equity segment's free margin. A read; an unreadable answer is `None` and the
/// plan is refused.
pub struct KiteMargins(pub Arc<dyn MarginReads>);

#[allow(clippy::print_stderr)]
fn log_exception(message: &str) {
    eprintln!("ERROR:desk.fno_monitor:{message}");
}

#[async_trait]
impl MarginSource for KiteMargins {
    async fn margins(&self, legs: &[MarginLeg]) -> Result<Option<MarginQuote>, ExecError> {
        let orders: Vec<PyValue> = legs
            .iter()
            .map(|leg| {
                PyValue::Map(dict([
                    ("exchange", PyValue::Str("NFO".to_owned())),
                    ("tradingsymbol", PyValue::Str(leg.tradingsymbol.clone())),
                    ("transaction_type", PyValue::Str(leg.side.clone())),
                    ("variety", PyValue::Str("regular".to_owned())),
                    ("product", PyValue::Str("NRML".to_owned())),
                    ("order_type", PyValue::Str("LIMIT".to_owned())),
                    ("quantity", PyValue::Int(leg.quantity)),
                    (
                        "price",
                        PyValue::Float(crate::lanes::options::execute::to_f64(leg.price)),
                    ),
                ]))
            })
            .collect();
        self.0.slot().await;
        let read = async {
            let basket = self.0.basket(&orders).await?;
            let equity = self.0.equity().await?;
            let free = match &equity {
                PyValue::Map(m) => m.get("net").cloned(),
                other => {
                    return Err(format!(
                        "AttributeError: '{}' object has no attribute 'get'",
                        crate::lanes::weekly::weekly_books::pyconv::type_name(other)
                    ));
                }
            };
            Ok((basket, free))
        }
        .await;
        let (basket, free) = match read {
            Ok(v) => v,
            Err(e) => {
                log_exception(&format!(
                    "FO plan: basket_order_margins could not be read: {e}"
                ));
                return Ok(None);
            }
        };
        let total = match &basket {
            PyValue::Map(b) => match b.get("final") {
                Some(PyValue::Map(f)) => f.get("total").cloned(),
                _ => None,
            },
            _ => None,
        };
        let (Some(total), Some(free)) = (total, free) else {
            return Ok(None);
        };
        if matches!(total, PyValue::None) || matches!(free, PyValue::None) {
            return Ok(None);
        }
        let as_dec = |v: &PyValue| {
            py::dec_py(Some(v)).ok_or_else(|| {
                data(format!(
                    "InvalidOperation: [<class 'decimal.ConversionSyntax'>] {}",
                    v.py_str()
                ))
            })
        };
        Ok(Some(MarginQuote {
            required_inr: as_dec(&total)?,
            free_inr: as_dec(&free)?,
        }))
    }
}

// --- the fixture seams (tests, and the parity harness's replays) --------------------------------

/// Settable books by tradingsymbol (`FakeQuotes`): a symbol not in the book is absent.
#[derive(Debug, Default)]
pub struct SharedBooks(pub std::sync::Mutex<BTreeMap<String, FoQuote>>);

impl SharedBooks {
    /// Put (or replace) these books.
    pub fn update(&self, books: impl IntoIterator<Item = (String, FoQuote)>) {
        if let Ok(mut m) = self.0.lock() {
            m.extend(books);
        }
    }

    /// One book.
    #[must_use]
    pub fn get(&self, symbol: &str) -> Option<FoQuote> {
        self.0.lock().ok().and_then(|m| m.get(symbol).cloned())
    }
}

#[async_trait]
impl FoQuoteSource for SharedBooks {
    async fn quotes(&self, symbols: &[String]) -> Result<BTreeMap<String, FoQuote>, ExecError> {
        let m = self
            .0
            .lock()
            .map_err(|_| ExecError::Other("RuntimeError: poisoned".to_owned()))?;
        Ok(symbols
            .iter()
            .filter_map(|s| m.get(s).map(|q| (s.clone(), q.clone())))
            .collect())
    }
}

/// A fixed NFO master: listed options and futures; the next future is the next **monthly**
/// one listed after the held expiry (`PgKiteInstruments`' rule).
#[derive(Debug, Clone, Default)]
pub struct StaticInstruments {
    /// `(underlying, expiry, strike, option_type, contract)`.
    pub options: Vec<(String, NaiveDate, Decimal, String, Contract)>,
    /// `(symbol, contract)`.
    pub futures: Vec<(String, Contract)>,
}

#[async_trait]
impl Instruments for StaticInstruments {
    async fn option(
        &self,
        underlying: &str,
        expiry: NaiveDate,
        strike: Decimal,
        option_type: &str,
    ) -> Result<Option<Contract>, ExecError> {
        Ok(self
            .options
            .iter()
            .find(|(u, e, k, t, _)| {
                u == underlying && *e == expiry && *k == strike && t == option_type
            })
            .map(|(_, _, _, _, c)| c.clone()))
    }

    async fn future(&self, symbol: &str, expiry: NaiveDate) -> Result<Option<Contract>, ExecError> {
        Ok(self
            .futures
            .iter()
            .find(|(s, c)| s == symbol && c.expiry == expiry)
            .map(|(_, c)| c.clone()))
    }

    async fn next_future(
        &self,
        symbol: &str,
        after: NaiveDate,
    ) -> Result<Option<Contract>, ExecError> {
        let mut rows: Vec<Contract> = self
            .futures
            .iter()
            .filter(|(s, _)| s == symbol)
            .map(|(_, c)| c.clone())
            .collect();
        rows.sort_by_key(|c| c.expiry);
        let listed: Vec<NaiveDate> = rows.iter().map(|c| c.expiry).collect();
        let monthly = baskfy_core::fno::calendar::monthly_expiries(&listed);
        Ok(rows
            .into_iter()
            .find(|c| c.expiry > after && monthly.contains(&c.expiry)))
    }
}

/// A settable calendar and bhavcopy (`FakeMarket`).
#[derive(Debug, Default)]
pub struct StaticMarket {
    /// `sessions()`.
    pub sessions: Vec<NaiveDate>,
    /// The landed days and the prints by day.
    pub state: std::sync::Mutex<(
        Vec<NaiveDate>,
        BTreeMap<NaiveDate, BTreeMap<PrintKey, Print>>,
    )>,
}

impl StaticMarket {
    /// A market over these sessions, nothing landed.
    #[must_use]
    pub fn new(sessions: Vec<NaiveDate>) -> Self {
        Self {
            sessions,
            state: std::sync::Mutex::new((Vec::new(), BTreeMap::new())),
        }
    }

    /// Mark `day` landed and add its prints.
    pub fn land(&self, day: NaiveDate, prints: impl IntoIterator<Item = (PrintKey, Print)>) {
        if let Ok(mut st) = self.state.lock() {
            if !st.0.contains(&day) {
                st.0.push(day);
            }
            st.1.entry(day).or_default().extend(prints);
        }
    }
}

#[async_trait]
impl Market for StaticMarket {
    async fn sessions(&self) -> Result<Vec<NaiveDate>, ExecError> {
        Ok(self.sessions.clone())
    }

    async fn landed(&self, day: NaiveDate) -> Result<bool, ExecError> {
        Ok(self.state.lock().is_ok_and(|st| st.0.contains(&day)))
    }

    async fn prints(
        &self,
        day: NaiveDate,
        keys: &[PrintKey],
    ) -> Result<BTreeMap<PrintKey, Print>, ExecError> {
        let st = self
            .state
            .lock()
            .map_err(|_| ExecError::Other("RuntimeError: poisoned".to_owned()))?;
        let found = st.1.get(&day);
        Ok(keys
            .iter()
            .filter_map(|k| found.and_then(|f| f.get(k)).map(|p| (k.clone(), *p)))
            .collect())
    }
}
