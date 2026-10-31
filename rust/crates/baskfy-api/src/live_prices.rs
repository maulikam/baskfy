//! `baskfy_api.live_prices`: live marks from the broker book, then read-only Kite quotes (M82,
//! LV1.1). Every Kite read goes through `baskfy_providers`' rate-limited provider; nothing here
//! can place an order (law 2).

use std::collections::HashMap;
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, Instant};

use baskfy_providers::factory::{KiteLane, RedisSource, build_kite_provider};
use baskfy_providers::kite::KiteProvider;
use baskfy_providers::records::{BrokerAccountFields, BrokerAccountRef, PyDateTime};
use baskfy_providers::tokens::AccessTokenStore;
use rust_decimal::Decimal;

/// `CACHE_TTL_SECONDS`.
pub const CACHE_TTL: Duration = Duration::from_secs(20);
/// `MAX_LIVE_MARKS`.
pub const MAX_LIVE_MARKS: usize = 500;
/// `SIMULATED_TOKEN_PREFIX`.
const SIMULATED_TOKEN_PREFIX: &str = "sim_";

/// `LiveQuote`: a last price with the exchange's previous close and print time.
#[derive(Debug, Clone, PartialEq)]
pub struct LiveQuote {
    /// `last_price`.
    pub last_price: Decimal,
    /// `prev_close` (positive, or `None`).
    pub prev_close: Option<Decimal>,
    /// `as_of`.
    pub as_of: Option<PyDateTime>,
}

struct Memo<T> {
    at: Option<Instant>,
    value: HashMap<String, T>,
}

impl<T: Clone> Memo<T> {
    fn new() -> Self {
        Self {
            at: None,
            value: HashMap::new(),
        }
    }

    fn fresh(&self) -> bool {
        self.at.is_some_and(|at| at.elapsed() < CACHE_TTL)
    }

    fn snapshot(&self) -> HashMap<String, T> {
        if self.fresh() {
            self.value.clone()
        } else {
            HashMap::new()
        }
    }

    fn merge(&mut self, value: HashMap<String, T>) {
        if !self.fresh() {
            self.value.clear();
            self.at = Some(Instant::now());
        }
        self.value.extend(value);
    }
}

/// `_memo`: the broker book's prices (`None` until a live read).
type BookMemo = (Option<Instant>, Option<HashMap<String, Decimal>>);
static BOOK_MEMO: LazyLock<Mutex<BookMemo>> = LazyLock::new(|| Mutex::new((None, None)));
static QUOTE_MEMO: LazyLock<Mutex<Memo<Decimal>>> = LazyLock::new(|| Mutex::new(Memo::new()));
static DETAIL_MEMO: LazyLock<Mutex<Memo<LiveQuote>>> = LazyLock::new(|| Mutex::new(Memo::new()));

fn env_stripped(name: &str) -> String {
    std::env::var(name).unwrap_or_default().trim().to_owned()
}

fn truthy_default_on(name: &str) -> bool {
    let raw = std::env::var(name).unwrap_or_else(|_| "true".to_owned());
    matches!(
        raw.trim().to_lowercase().as_str(),
        "" | "1" | "true" | "yes" | "on"
    )
}

/// `market_data_enabled()`: `BASKFY_LIVE_QUOTES`, on unless explicitly off.
#[must_use]
pub fn market_data_enabled() -> bool {
    truthy_default_on("BASKFY_LIVE_QUOTES")
}

/// `dry_run_enabled()`: only an explicit false opts out.
#[must_use]
pub fn dry_run_enabled() -> bool {
    truthy_default_on("DRY_RUN")
}

/// `token_store_path()`: broker-specific env, then the shared Kite path, then the default.
#[must_use]
pub fn token_store_path() -> std::path::PathBuf {
    let broker = env_stripped("BASKFY_BROKER_TOKEN_PATH");
    let kite = env_stripped("BASKFY_KITE_TOKEN_PATH");
    let raw = if !broker.is_empty() {
        broker
    } else if !kite.is_empty() {
        kite
    } else {
        ".secrets/broker-token.enc".to_owned()
    };
    std::path::PathBuf::from(raw)
}

/// `token_encryption_key()`.
#[must_use]
pub fn token_encryption_key() -> String {
    env_stripped("BASKFY_KITE_TOKEN_ENCRYPTION_KEY")
}

/// `token_store_for()`.
#[must_use]
pub fn token_store_for() -> AccessTokenStore {
    AccessTokenStore::new(token_store_path(), token_encryption_key().into())
}

/// `is_simulated_token`.
#[must_use]
pub fn is_simulated_token(token: &str) -> bool {
    token.starts_with(SIMULATED_TOKEN_PREFIX)
}

fn now_ist() -> chrono::DateTime<chrono::FixedOffset> {
    crate::entitlements::utc_now().with_timezone(&baskfy_providers::tokens::ist())
}

/// `quotes_permitted()`: market data enabled, an app key, and a real, unexpired session.
///
/// # Errors
/// A stored token that cannot be read for a reason other than "absent" (Python raises it).
pub fn quotes_permitted() -> Result<bool, baskfy_providers::ProviderError> {
    if !market_data_enabled() || env_stripped("BASKFY_KITE_API_KEY").is_empty() {
        return Ok(false);
    }
    match token_store_for().load() {
        Ok(token) => Ok(!(is_simulated_token(token.value.expose()) || token.is_expired(now_ist()))),
        Err(e) if e.kind() == baskfy_providers::ErrorKind::CredentialsMissing => Ok(false),
        Err(e) => Err(e),
    }
}

/// `build_kite_provider(settings)` with the API's key, token path and encryption key: the
/// interactive lane of the shared `baskfy:ratelimit:kite:*` read clock.
///
/// # Errors
/// The settings', the limiter's, the provider's.
pub async fn kite_provider(api_key: &str) -> Result<KiteProvider, String> {
    let base = baskfy_providers::settings::get_provider_settings().map_err(|e| e.to_string())?;
    let mut settings = base.clone();
    settings.kite_api_key = api_key.into();
    settings.kite_token_path = token_store_path().to_string_lossy().into_owned();
    settings.kite_token_encryption_key = token_encryption_key().into();
    let source = RedisSource::from_settings(&settings);
    build_kite_provider(&source, &settings, None, KiteLane::Interactive)
        .await
        .map_err(|e| e.python_type().to_owned())
}

/// One broker holding as `normalize_holding` builds it.
#[derive(Debug, Clone, PartialEq)]
pub struct LiveHolding {
    /// `symbol` (stripped, upper-cased).
    pub symbol: String,
    /// `exchange`.
    pub exchange: String,
    /// `quantity`.
    pub quantity: Decimal,
    /// `t1_quantity`.
    pub t1_quantity: Decimal,
    /// `collateral_quantity`.
    pub collateral_quantity: Decimal,
    /// `average_price` (`or 0`).
    pub average_price: Decimal,
    /// `last_price`.
    pub last_price: Option<Decimal>,
    /// `product`.
    pub product: String,
}

/// `fetch_kite_holdings(api_key=, broker_id=)`: `GET /portfolio/holdings` through the provider.
///
/// # Errors
/// The exception class name of whatever refused the read (`_LIVE_FETCH_ERRORS`).
pub async fn fetch_kite_holdings(
    api_key: &str,
    broker_id: &str,
) -> Result<Vec<LiveHolding>, String> {
    let provider = kite_provider(api_key).await?;
    let account = BrokerAccountRef::new(BrokerAccountFields {
        broker_account_id: 1,
        broker_id: broker_id.to_owned(),
        kite_user_id: None,
    })
    .map_err(|_| "ValueError".to_owned())?;
    let records = provider
        .broker_holdings(&account)
        .await
        .map_err(|e| e.python_type().to_owned())?;
    Ok(records
        .into_iter()
        .map(|r| LiveHolding {
            symbol: r.symbol.trim().to_uppercase(),
            exchange: r.exchange.trim().to_uppercase(),
            quantity: r.quantity,
            t1_quantity: r.t1_quantity,
            collateral_quantity: r.collateral_quantity,
            average_price: r.average_price.unwrap_or(Decimal::ZERO),
            last_price: r.last_price,
            product: r.product.clone(),
        })
        .collect())
}

/// `holdings_for_broker(broker_id)` as the price overlay reads it: the live rows, or `None` for
/// anything that is not a live, undegraded read.
async fn live_book(broker_id: &str) -> Option<Vec<LiveHolding>> {
    if broker_id != "zerodha" || dry_run_enabled() {
        return None;
    }
    let api_key = env_stripped("BASKFY_KITE_API_KEY");
    if api_key.is_empty() {
        return None;
    }
    let store = token_store_for();
    if !store.exists() || store.require_fresh(None).is_err() {
        return None;
    }
    match fetch_kite_holdings(&api_key, broker_id).await {
        Ok(rows) if !rows.is_empty() => Some(rows),
        _ => None,
    }
}

/// `live_prices_by_symbol(broker_id)`: `{symbol: last_price}` from a live broker book, or `{}`.
pub async fn live_prices_by_symbol(broker_id: &str) -> HashMap<String, Decimal> {
    if let Ok(memo) = BOOK_MEMO.lock()
        && let (Some(at), Some(value)) = (&memo.0, &memo.1)
        && at.elapsed() < CACHE_TTL
    {
        return value.clone();
    }
    let Some(rows) = live_book(broker_id).await else {
        return HashMap::new();
    };
    let prices: HashMap<String, Decimal> = rows
        .into_iter()
        .filter_map(|r| match r.last_price {
            Some(p) if p > Decimal::ZERO => Some((r.symbol, p)),
            _ => None,
        })
        .collect();
    if let Ok(mut memo) = BOOK_MEMO.lock() {
        *memo = (Some(Instant::now()), Some(prices.clone()));
    }
    prices
}

/// `_quote_details(symbols)`: read-only quotes with the previous close; `{}` on any failure.
async fn quote_details(symbols: &[String]) -> HashMap<String, LiveQuote> {
    let wanted: Vec<&str> = symbols
        .iter()
        .map(String::as_str)
        .filter(|s| !s.is_empty())
        .collect();
    let api_key = env_stripped("BASKFY_KITE_API_KEY");
    if wanted.is_empty() || api_key.is_empty() {
        return HashMap::new();
    }
    let Ok(provider) = kite_provider(&api_key).await else {
        return HashMap::new();
    };
    let Ok(records) = provider.quotes(&wanted, "NSE").await else {
        return HashMap::new();
    };
    let mut out = HashMap::new();
    for r in records {
        if r.last_price > Decimal::ZERO {
            out.insert(
                r.symbol.trim().to_uppercase(),
                LiveQuote {
                    last_price: r.last_price,
                    prev_close: r.prev_close.filter(|p| *p > Decimal::ZERO),
                    as_of: r.as_of,
                },
            );
        }
    }
    out
}

/// `live_quotes_by_symbol(symbols)`: last prices for names the book did not cover.
///
/// # Errors
/// As [`quotes_permitted`].
pub async fn live_quotes_by_symbol(
    symbols: &[String],
) -> Result<HashMap<String, Decimal>, baskfy_providers::ProviderError> {
    let wanted: Vec<String> = symbols
        .iter()
        .map(|s| s.trim().to_uppercase())
        .filter(|s| !s.is_empty())
        .collect();
    if wanted.is_empty() {
        return Ok(HashMap::new());
    }
    let mut cached = QUOTE_MEMO.lock().map(|m| m.snapshot()).unwrap_or_default();
    let missing: Vec<String> = wanted
        .iter()
        .filter(|s| !cached.contains_key(*s))
        .cloned()
        .collect();
    if !missing.is_empty() && quotes_permitted()? {
        let fetched: HashMap<String, Decimal> = quote_details(&missing)
            .await
            .into_iter()
            .map(|(k, q)| (k, q.last_price))
            .collect();
        if !fetched.is_empty() {
            if let Ok(mut memo) = QUOTE_MEMO.lock() {
                memo.merge(fetched.clone());
            }
            cached.extend(fetched);
        }
    }
    Ok(wanted
        .into_iter()
        .filter_map(|s| cached.get(&s).map(|p| (s, *p)))
        .collect())
}

/// `live_quote_details(symbols)`: the screens' overlay — at most [`MAX_LIVE_MARKS`] names.
///
/// # Errors
/// As [`quotes_permitted`].
pub async fn live_quote_details(
    symbols: &[String],
) -> Result<HashMap<String, LiveQuote>, baskfy_providers::ProviderError> {
    let mut wanted: Vec<String> = Vec::new();
    for raw in symbols {
        let symbol = raw.trim().to_uppercase();
        if !symbol.is_empty() && !wanted.contains(&symbol) {
            wanted.push(symbol);
        }
        if wanted.len() >= MAX_LIVE_MARKS {
            break;
        }
    }
    if wanted.is_empty() {
        return Ok(HashMap::new());
    }
    let mut cached = DETAIL_MEMO.lock().map(|m| m.snapshot()).unwrap_or_default();
    let missing: Vec<String> = wanted
        .iter()
        .filter(|s| !cached.contains_key(*s))
        .cloned()
        .collect();
    if !missing.is_empty() && quotes_permitted()? {
        let fetched = quote_details(&missing).await;
        if !fetched.is_empty() {
            if let Ok(mut memo) = DETAIL_MEMO.lock() {
                memo.merge(fetched.clone());
            }
            cached.extend(fetched);
        }
    }
    Ok(wanted
        .into_iter()
        .filter_map(|s| cached.remove(&s).map(|q| (s, q)))
        .collect())
}

/// `live_marks_for_symbols(symbols)`: book first, then quotes, at most [`MAX_LIVE_MARKS`].
///
/// # Errors
/// As [`quotes_permitted`].
pub async fn live_marks_for_symbols(
    symbols: &[String],
) -> Result<HashMap<String, Decimal>, baskfy_providers::ProviderError> {
    let mut wanted: Vec<String> = Vec::new();
    for raw in symbols {
        let symbol = raw.trim().to_uppercase();
        if symbol.is_empty() || wanted.contains(&symbol) {
            continue;
        }
        wanted.push(symbol);
        if wanted.len() >= MAX_LIVE_MARKS {
            break;
        }
    }
    if wanted.is_empty() {
        return Ok(HashMap::new());
    }
    let mut by_book = live_prices_by_symbol("zerodha").await;
    let missing: Vec<String> = wanted
        .iter()
        .filter(|s| !by_book.contains_key(*s))
        .cloned()
        .collect();
    if !missing.is_empty() {
        by_book.extend(live_quotes_by_symbol(&missing).await?);
    }
    Ok(wanted
        .into_iter()
        .filter_map(|s| by_book.get(&s).map(|p| (s, *p)))
        .collect())
}

/// `live_prices_by_instrument(session, ids)`: the same marks keyed by instrument id. `symbols`
/// is `(id, symbol)` for the ids that exist, in the database's order.
///
/// # Errors
/// As [`quotes_permitted`].
pub async fn live_prices_by_instrument_rows(
    rows: &[(i64, String)],
) -> Result<HashMap<i64, Decimal>, baskfy_providers::ProviderError> {
    if rows.is_empty() {
        return Ok(HashMap::new());
    }
    let by_symbol = live_prices_by_symbol("zerodha").await;
    let mut mapped: HashMap<i64, Decimal> = HashMap::new();
    let mut missing: Vec<String> = Vec::new();
    for (id, symbol) in rows {
        match by_symbol.get(symbol) {
            Some(p) => {
                mapped.insert(*id, *p);
            }
            None => missing.push(symbol.clone()),
        }
    }
    if !missing.is_empty() {
        let quoted = live_quotes_by_symbol(&missing).await?;
        for (id, symbol) in rows {
            if !mapped.contains_key(id)
                && let Some(p) = quoted.get(symbol)
            {
                mapped.insert(*id, *p);
            }
        }
    }
    Ok(mapped)
}

/// `live_prices_by_instrument(session, ids)` over a pool: reads `instrument.symbol` for `ids`.
///
/// # Errors
/// The database's (as text); a token-store failure (as its Python class).
pub async fn live_prices_by_instrument(
    pool: &sqlx::PgPool,
    ids: &[i64],
) -> Result<HashMap<i64, Decimal>, String> {
    if ids.is_empty() {
        return Ok(HashMap::new());
    }
    let rows: Vec<(i64, String)> =
        sqlx::query_as("SELECT id, symbol FROM instrument WHERE id = ANY($1)")
            .bind(ids)
            .fetch_all(pool)
            .await
            .map_err(|e| e.to_string())?;
    live_prices_by_instrument_rows(&rows)
        .await
        .map_err(|e| e.python_type().to_owned())
}
