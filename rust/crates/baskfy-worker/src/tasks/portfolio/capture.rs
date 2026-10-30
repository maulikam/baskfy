//! `baskfy_api.broker_trades.capture_kite_trades` as the evening task calls it: today's Zerodha
//! executions read from Kite's **read-only** `GET /trades` (emptied nightly), stored in
//! `broker_trade` and used to date the holdings they account for.
//!
//! The read and its refusals are [`read_kite_trades_today`]; the storage half is the API's own
//! (`baskfy_api::routes::trades::capture_store_and_date` — `ensure_default_broker_account`,
//! `store_fills`, `apply_trade_history`), so the tradebook import and this capture keep one copy.
//!
//! **Law 2.** The only Kite call is `KiteProvider::broker_trades` — a GET. Nothing here can
//! reach an order or a GTT verb.

use std::sync::Arc;

use baskfy_api::routes::trades::{CaptureFill, capture_store_and_date};
use baskfy_core::decimal::WideDecimal;
use baskfy_providers::ProviderError;
use baskfy_providers::factory::{KiteLane, RedisSource, build_kite_read_limiter};
use baskfy_providers::kite::{
    ClientFactory, DEFAULT_ROOT_URI, KiteClient, KiteConnect, KiteProvider, KiteRuntime, TradeFill,
    TradeSide,
};
use baskfy_providers::records::{BrokerAccountFields, BrokerAccountRef, PyDateTime};
use baskfy_providers::secret::Secret;
use baskfy_providers::settings::ProviderSettings;
use baskfy_providers::tokens::AccessTokenStore;
use chrono::{Duration, NaiveDateTime};
use sqlx::PgConnection;

use super::TaskContext;
use crate::error::WorkerError;

/// IST's offset from UTC.
const IST_OFFSET_MINUTES: i64 = 330;

/// `CaptureReport` (what the task reports of it).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CaptureReport {
    /// `captured`.
    pub captured: bool,
    /// `reason`.
    pub reason: String,
    /// `store.inserted` (0 when nothing was stored).
    pub inserted: usize,
    /// `history.dated` (0 when nothing was stored).
    pub dated_holdings: usize,
}

/// `os.environ.get(name, "").strip()`.
fn env_stripped(name: &str) -> String {
    std::env::var(name).unwrap_or_default().trim().to_owned()
}

/// `broker_oauth.dry_run_enabled()`: anything but an explicit false is dry.
fn dry_run_enabled() -> bool {
    let raw = std::env::var("DRY_RUN").unwrap_or_else(|_| "true".to_owned());
    matches!(
        raw.trim().to_lowercase().as_str(),
        "" | "1" | "true" | "yes" | "on"
    )
}

/// `broker_oauth.token_store_path()`.
fn token_store_path() -> String {
    let broker = env_stripped("BASKFY_BROKER_TOKEN_PATH");
    if !broker.is_empty() {
        return broker;
    }
    let kite = env_stripped("BASKFY_KITE_TOKEN_PATH");
    if !kite.is_empty() {
        return kite;
    }
    ".secrets/broker-token.enc".to_owned()
}

fn failed(error: &ProviderError) -> String {
    // The type only: a provider message can name the token path.
    let kind = error.python_type();
    baskfy_api::logging::warning(
        "baskfy_api.broker_trades",
        &format!("trades: the Kite read failed ({kind})"),
        &[],
    );
    format!("the Kite trades read failed ({kind})")
}

/// `read_kite_trades_today()`: today's executions, or the sentence saying why there are none.
/// DRY_RUN reads nothing; a missing key or session is a reason, not an exception.
///
/// # Errors
/// What Python lets escape its `_LIVE_READ_ERRORS` net: a provider setting that does not parse.
pub async fn read_kite_trades_today(
    ctx: &TaskContext,
) -> Result<Result<Vec<TradeFill>, String>, WorkerError> {
    if dry_run_enabled() {
        return Ok(Err("DRY_RUN is on; no broker read was made".to_owned()));
    }
    let api_key = env_stripped("BASKFY_KITE_API_KEY");
    if api_key.is_empty() {
        return Ok(Err(
            "no broker app key is configured on this deployment".to_owned()
        ));
    }
    let path = token_store_path();
    let key = env_stripped("BASKFY_KITE_TOKEN_ENCRYPTION_KEY");
    let store = AccessTokenStore::with_clock(path.clone(), Secret::from(key.clone()), ctx.clock());
    if !store.exists() {
        return Ok(Err("no Kite session has been stored yet".to_owned()));
    }
    if let Err(e) = store.require_fresh(None) {
        return Ok(Err(failed(&e)));
    }
    let mut settings =
        ProviderSettings::from_env().map_err(|e| WorkerError::Settings(e.to_string()))?;
    settings.kite_api_key = Secret::from(api_key);
    settings.kite_token_path = path;
    settings.kite_token_encryption_key = Secret::from(key);
    match kite_trades(ctx, settings, store).await {
        Ok(fills) => Ok(Ok(fills)),
        Err(e) => Ok(Err(failed(&e))),
    }
}

/// `build_kite_provider(settings).broker_trades(BrokerAccountRef(broker_account_id=1,
/// broker_id="zerodha"))`, on the interactive read limiter, with the harness's two seams (the
/// REST root and the token store's clock; DECISIONS-RS R8.3).
async fn kite_trades(
    ctx: &TaskContext,
    settings: ProviderSettings,
    store: AccessTokenStore,
) -> Result<Vec<TradeFill>, ProviderError> {
    let limiter = build_kite_read_limiter(
        &RedisSource::from_settings(&settings),
        &settings,
        KiteLane::Interactive,
    )
    .await?;
    let root = ctx.kite_root().unwrap_or(DEFAULT_ROOT_URI).to_owned();
    let timeout = settings.kite_request_timeout_seconds;
    let factory: ClientFactory = Arc::new(move |api_key: &str| {
        let client: Arc<dyn KiteClient> =
            Arc::new(KiteConnect::with_root(api_key, &root, Some(timeout))?);
        Ok(client)
    });
    let provider = KiteProvider::new(
        settings,
        KiteRuntime {
            rate_limiter: limiter.map(baskfy_providers::factory::KiteReadLimiter::into_limiter),
            client_factory: Some(factory),
            token_store: Some(Arc::new(store)),
            ..KiteRuntime::default()
        },
    )?;
    let account = BrokerAccountRef::new(BrokerAccountFields {
        broker_account_id: 1,
        broker_id: "zerodha".to_owned(),
        kite_user_id: None,
    })
    .map_err(|e| ProviderError::unexpected_payload(e.to_string()))?;
    provider.broker_trades(&account).await
}

/// `_executed_at`, as IST wall time: a naive stamp is IST already; an aware one is converted.
fn executed_at_ist(stamp: Option<&PyDateTime>) -> Option<NaiveDateTime> {
    match stamp? {
        PyDateTime::Naive(n) => Some(*n),
        PyDateTime::Aware(a) => Some(a.naive_utc() + Duration::minutes(IST_OFFSET_MINUTES)),
    }
}

fn wide(value: rust_decimal::Decimal) -> WideDecimal {
    WideDecimal::from(value)
}

/// `capture_kite_trades(session, user_id=)`: read, then store and date on the caller's
/// transaction (the caller commits).
///
/// # Errors
/// A database failure in the storage half (reported as Python's class where known), or a
/// setting that does not parse.
pub async fn capture_kite_trades(
    ctx: &TaskContext,
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<CaptureReport, WorkerError> {
    let fills = match read_kite_trades_today(ctx).await? {
        Ok(fills) => fills,
        Err(reason) => {
            return Ok(CaptureReport {
                captured: false,
                reason,
                inserted: 0,
                dated_holdings: 0,
            });
        }
    };
    let handed: Vec<CaptureFill> = fills
        .iter()
        .map(|f| CaptureFill {
            symbol: f.symbol.clone(),
            exchange: f.exchange.clone(),
            is_buy: f.side == TradeSide::Buy,
            quantity: wide(f.quantity),
            price: wide(f.price),
            trade_date: f.trade_date,
            trade_id: f.trade_id.clone(),
            order_id: f.order_id.clone(),
            isin: f.isin.clone(),
            executed_at: executed_at_ist(f.executed_at.as_ref()),
        })
        .collect();
    let counts = capture_store_and_date(conn, user_id, handed)
        .await
        .map_err(|p| WorkerError::python("Problem", p.detail))?;
    Ok(CaptureReport {
        captured: true,
        reason: format!(
            "{} new of {} trades read from Kite",
            counts.inserted, counts.received
        ),
        inserted: counts.inserted,
        dated_holdings: counts.dated_holdings,
    })
}
