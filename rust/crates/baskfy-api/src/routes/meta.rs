//! `/meta/*` — docs/07 §Metadata. Ports `baskfy_api.routers.meta`.
//!
//! Reference data served straight from `baskfy_core` with no database (`/meta/factors`,
//! `/meta/columns`, `/meta/universes`, `/meta/ranking-presets`), the trading calendar
//! (`/meta/trading-days`), the freshness of the data (`/meta/status`) and the screens' live price
//! overlay (`/meta/live-marks`).
//!
//! This file also hosts the lane's shared private module, [`reference`].

#[path = "reference/mod.rs"]
pub(crate) mod reference;

use axum::Router;
use axum::extract::{RawQuery, State};
use axum::response::Response;
use axum::routing::get;
use chrono::NaiveDate;
use serde::Serialize;

use axum::Extension;
use baskfy_db::OrderedJson;
use chrono::{DateTime, Datelike, FixedOffset, NaiveTime, Utc, Weekday};

use crate::auth::{Principal, require_authenticated};
use crate::live_prices::LiveQuote;
use crate::problems::{Problem, ProblemType};
use crate::query::{QueryParams, Validation};
use crate::state::AppState;
use crate::wire;
use baskfy_providers::records::PyDateTime;

/// `MAX_TRADING_DAY_SPAN`: five 366-day years.
pub const MAX_TRADING_DAY_SPAN_DAYS: i64 = 5 * 366;

/// `baskfy_core.seed_data.NSE_EXCHANGE_ID`.
pub const NSE_EXCHANGE_ID: i16 = 1;

/// `FAILED_RUN_STATUSES`.
pub const FAILED_RUN_STATUSES: [&str; 2] = ["failed", "aborted"];

/// `RUNNING_RUN_STATUS`.
pub const RUNNING_RUN_STATUS: &str = "running";

/// `STALE_AFTER_SECONDS`.
pub const STALE_AFTER_SECONDS: i64 = 120;

/// `FactorOut`.
#[derive(Debug, Clone, Serialize)]
pub struct FactorOut {
    /// `key`.
    pub key: String,
    /// `label`.
    pub label: String,
    /// `family` (the enum value).
    pub family: &'static str,
    /// `unit` (the enum value).
    pub unit: baskfy_core::factor_registry::FactorUnit,
    /// `higher_is_better`.
    pub higher_is_better: bool,
    /// `preference`.
    pub preference: baskfy_core::factor_registry::FactorPreference,
    /// `rankable`.
    pub rankable: bool,
    /// `weight_family`.
    pub weight_family: baskfy_core::factor_registry::WeightFamily,
    /// `validation_status`.
    pub validation_status: baskfy_core::factor_registry::ValidationStatus,
    /// `definition`.
    pub definition: String,
}

/// `GET /meta/factors`: the factor registry, every entry, in registry order.
pub async fn get_factors() -> Response {
    let out: Vec<FactorOut> = baskfy_core::factor_registry::factors()
        .iter()
        .map(|f| FactorOut {
            key: f.key.clone(),
            label: f.label.clone(),
            family: f.family.value(),
            unit: f.unit,
            higher_is_better: f.higher_is_better,
            preference: f.preference,
            rankable: f.rankable,
            weight_family: f.weight_family,
            validation_status: f.validation_status,
            definition: f.definition.clone(),
        })
        .collect();
    wire::json(&out)
}

/// `GET /meta/columns`: the selectable result columns (`ColumnOut` is `ResultColumn`'s shape).
pub async fn get_columns() -> Response {
    wire::json(&baskfy_core::factor_registry::columns())
}

/// `TradingDaysOut`: `from` (aliased), `to`, `dates`.
#[derive(Debug, Clone, Serialize)]
pub struct TradingDaysOut {
    /// `from_` serialised by its alias `from`.
    #[serde(rename = "from", serialize_with = "wire::ser::date")]
    pub from: NaiveDate,
    /// `to`.
    #[serde(serialize_with = "wire::ser::date")]
    pub to: NaiveDate,
    /// `dates`.
    #[serde(serialize_with = "wire::ser::dates")]
    pub dates: Vec<NaiveDate>,
}

/// `GET /meta/trading-days?from=&to=`: NSE trading days in an inclusive range.
///
/// # Errors
/// 400 for a missing or invalid date (FastAPI's validation), 422 `no-trading-day` for a reversed
/// or over-long range, 500 for a database failure.
pub async fn get_trading_days(
    State(state): State<AppState>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let from_raw = v.required(&q, "from");
    let from = v.date("from", from_raw);
    let to_raw = v.required(&q, "to");
    let to = v.date("to", to_raw);
    v.finish()?;
    let (Some(from), Some(to)) = (from, to) else {
        return Err(crate::problems::internal_error());
    };
    if to < from {
        return Err(Problem::new(
            ProblemType::NoTradingDay,
            format!(
                "`to` ({}) is before `from` ({}).",
                wire::py_date(to),
                wire::py_date(from)
            ),
        ));
    }
    if (to - from).num_days() > MAX_TRADING_DAY_SPAN_DAYS {
        return Err(Problem::new(
            ProblemType::NoTradingDay,
            format!("Range exceeds the {MAX_TRADING_DAY_SPAN_DAYS}-day maximum for one request."),
        ));
    }
    let dates: Vec<NaiveDate> = sqlx::query_scalar(
        "SELECT date FROM trading_day WHERE exchange_id = $1 AND is_trading_day IS true \
         AND date >= $2 AND date <= $3 ORDER BY date ASC",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(from)
    .bind(to)
    .fetch_all(state.db.pool())
    .await
    .map_err(|e| crate::db_failure(&e.into()))?;
    Ok(wire::json(&TradingDaysOut { from, to, dates }))
}

/// `UniverseOut`.
#[derive(Debug, Clone, Serialize)]
pub struct UniverseOut {
    /// `index_id`.
    pub index_id: i64,
    /// `slug`.
    pub slug: &'static str,
    /// `name`.
    pub name: &'static str,
    /// `sort_order` (the universe's `ui_order`).
    pub sort_order: i64,
    /// `market_health`.
    pub market_health: bool,
}

/// `GET /meta/universes`: the fifteen universes, in UI order.
pub async fn get_universes() -> Response {
    let mut universes = reference::universes::UNIVERSES.to_vec();
    universes.sort_by_key(|u| u.ui_order);
    let out: Vec<UniverseOut> = universes
        .into_iter()
        .map(|u| UniverseOut {
            index_id: u.index_id,
            slug: u.slug,
            name: u.name,
            sort_order: u.ui_order,
            market_health: u.market_health,
        })
        .collect();
    wire::json(&out)
}

/// `RankingPresetOut`.
#[derive(Debug, Clone, Serialize)]
pub struct RankingPresetOut {
    /// `key`.
    pub key: &'static str,
    /// `label`.
    pub label: &'static str,
    /// `description`.
    pub description: &'static str,
    /// `status`.
    pub status: &'static str,
    /// `sort_by`.
    pub sort_by: String,
    /// `patch` (JSON, in the patch's own key order).
    pub patch: OrderedJson,
}

/// `GET /meta/ranking-presets`: the named presets with their promotion status.
pub async fn get_ranking_presets() -> Response {
    let out: Vec<RankingPresetOut> = reference::universes::presets()
        .into_iter()
        .map(|p| RankingPresetOut {
            key: p.key,
            label: p.label,
            description: p.description,
            status: p.status,
            sort_by: p.sort_by(),
            patch: p.patch,
        })
        .collect();
    wire::json(&out)
}

/// `PipelineRunOut`.
#[derive(Debug, Clone, Serialize)]
pub struct PipelineRunOut {
    /// `trade_date`.
    #[serde(serialize_with = "wire::ser::date")]
    pub trade_date: NaiveDate,
    /// `status`.
    pub status: String,
    /// `started_at`.
    #[serde(serialize_with = "wire::ser::datetime")]
    pub started_at: DateTime<Utc>,
    /// `finished_at`.
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    pub finished_at: Option<DateTime<Utc>>,
    /// `data_version`.
    pub data_version: Option<i64>,
}

/// `StatusOut`.
#[derive(Debug, Clone, Serialize)]
pub struct StatusOut {
    /// `as_of`.
    #[serde(serialize_with = "wire::ser::opt_date")]
    pub as_of: Option<NaiveDate>,
    /// `data_version`.
    pub data_version: i64,
    /// `last_pipeline_run`.
    pub last_pipeline_run: Option<PipelineRunOut>,
    /// `degraded`.
    pub degraded: bool,
    /// `pipeline_running`.
    pub pipeline_running: bool,
    /// `data_start_date`.
    #[serde(serialize_with = "wire::ser::date")]
    pub data_start_date: NaiveDate,
    /// `live_quotes`.
    pub live_quotes: bool,
    /// `session_day`.
    pub session_day: bool,
    /// `market_open`.
    pub market_open: bool,
}

/// `swing_health.is_session_day`: a day the calendar does not carry is a session.
async fn is_session_day(state: &AppState, day: NaiveDate) -> Result<bool, Problem> {
    let known: Option<bool> = sqlx::query_scalar(
        "SELECT is_trading_day FROM trading_day WHERE exchange_id = $1 AND date = $2",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(day)
    .fetch_optional(state.db.pool())
    .await
    .map_err(reference::db)?;
    Ok(known.unwrap_or(true))
}

/// `market_hours_cb.is_nse_session_open(now, {today} if session_day else set())`.
fn session_open(now: DateTime<FixedOffset>, session_day: bool) -> bool {
    if !session_day {
        return false;
    }
    let clock = now.time();
    let open = NaiveTime::from_hms_opt(9, 15, 0).unwrap_or_default();
    let close = NaiveTime::from_hms_opt(15, 30, 0).unwrap_or_default();
    open <= clock && clock <= close
}

/// `_session_day_and_market_open`: the calendar and the IST clock, at `now`.
async fn session_day_and_market_open(
    state: &AppState,
    now: DateTime<FixedOffset>,
) -> Result<(bool, bool), Problem> {
    let today = now.date_naive();
    let weekday = !matches!(today.weekday(), Weekday::Sat | Weekday::Sun);
    let session_day = weekday && is_session_day(state, today).await?;
    Ok((session_day, session_open(now, session_day)))
}

/// `GET /meta/status`: `{as_of, data_version, last_pipeline_run}` plus the freshness flags.
///
/// # Errors
/// 500 for a database failure.
pub async fn get_status(State(state): State<AppState>) -> Result<Response, Problem> {
    let row = sqlx::query(
        "SELECT trade_date, status, started_at, finished_at, data_version FROM pipeline_run \
         ORDER BY trade_date DESC, started_at DESC, id DESC LIMIT 1",
    )
    .fetch_optional(state.db.pool())
    .await
    .map_err(reference::db)?;
    let last = match row {
        None => None,
        Some(r) => {
            use sqlx::Row;
            Some(PipelineRunOut {
                trade_date: baskfy_db::row::date(&r, "trade_date")
                    .map_err(|e| reference::dbe(&e))?,
                status: r.try_get("status").map_err(reference::db)?,
                started_at: baskfy_db::row::timestamptz(&r, "started_at")
                    .map_err(|e| reference::dbe(&e))?,
                finished_at: baskfy_db::row::opt_timestamptz(&r, "finished_at")
                    .map_err(|e| reference::dbe(&e))?,
                data_version: r.try_get("data_version").map_err(reference::db)?,
            })
        }
    };
    let (session_day, market_open) =
        session_day_and_market_open(&state, reference::now_ist()).await?;
    let out = StatusOut {
        as_of: reference::latest_published_date(&state).await?,
        data_version: reference::current_data_version(&state).await?,
        degraded: last
            .as_ref()
            .is_some_and(|r| FAILED_RUN_STATUSES.contains(&r.status.as_str())),
        pipeline_running: last
            .as_ref()
            .is_some_and(|r| r.status == RUNNING_RUN_STATUS),
        last_pipeline_run: last,
        data_start_date: reference::data_start_date(),
        live_quotes: reference::quotes_permitted()?,
        session_day,
        market_open,
    };
    Ok(wire::json(&out))
}

/// `LiveMarksOut` (the closed shapes: `quotes` and `marks` are empty objects).
#[derive(Debug, Clone, Serialize)]
pub struct LiveMarksOut {
    /// `live`.
    pub live: bool,
    /// `reason`: `market_closed` / `no_session` / `unavailable`.
    pub reason: Option<&'static str>,
    /// `market_open`.
    pub market_open: bool,
    /// `as_of`.
    #[serde(serialize_with = "wire::ser::opt_date")]
    pub as_of: Option<NaiveDate>,
    /// `quotes`.
    pub quotes: OrderedJson,
    /// `live_overlay`.
    pub live_overlay: bool,
    /// `marks`.
    pub marks: OrderedJson,
    /// `served_at` (IST-aware).
    #[serde(serialize_with = "wire::ser::datetime_fixed")]
    pub served_at: DateTime<FixedOffset>,
    /// `requested`.
    pub requested: i64,
    /// `covered`.
    pub covered: i64,
    /// `stale_after_seconds`.
    pub stale_after_seconds: i64,
}

/// `_requested_names(symbols)`: distinct, stripped, upper-cased, in order.
fn requested_names(symbols: &str) -> Vec<String> {
    let mut names: Vec<String> = Vec::new();
    for part in symbols.split(',') {
        let symbol = reference::py_upper(reference::py_strip(part));
        if !symbol.is_empty() && !names.contains(&symbol) {
            names.push(symbol);
        }
    }
    names
}

/// `STALE_AFTER_SECONDS`, as a duration.
const STALE_AFTER: i64 = STALE_AFTER_SECONDS;

/// `_is_stale(as_of, served_at)`: a naive stamp is read as IST; no stamp is not stale.
fn is_stale(as_of: Option<&PyDateTime>, served_at: DateTime<FixedOffset>) -> bool {
    let stamp = match as_of {
        None => return false,
        Some(PyDateTime::Aware(t)) => *t,
        Some(PyDateTime::Naive(n)) => {
            let ist = baskfy_providers::tokens::ist();
            match n.and_local_timezone(ist).single() {
                Some(t) => t,
                None => return false,
            }
        }
    };
    let micros = (served_at - stamp).num_microseconds().unwrap_or(i64::MAX);
    micros > STALE_AFTER * 1_000_000
}

/// `as_of` as pydantic writes a datetime.
fn py_datetime_json(t: &PyDateTime) -> OrderedJson {
    match t {
        PyDateTime::Aware(a) => OrderedJson::Str(wire::py_datetime_fixed(*a)),
        PyDateTime::Naive(n) => OrderedJson::Str(wire::py_naive_datetime(*n)),
    }
}

/// `_live_quote_out(quote, served_at)`.
fn live_quote_out(quote: &LiveQuote, served_at: DateTime<FixedOffset>) -> OrderedJson {
    let change = quote.prev_close.and_then(|prev| {
        let pct = (quote.last_price - prev).checked_div(prev)? * rust_decimal::Decimal::from(100);
        let mut r =
            pct.round_dp_with_strategy(2, rust_decimal::RoundingStrategy::MidpointNearestEven);
        r.rescale(2);
        Some(r)
    });
    let dec = |d: rust_decimal::Decimal| OrderedJson::Str(wire::py_decimal(d));
    OrderedJson::Object(vec![
        ("last_price".to_owned(), dec(quote.last_price)),
        (
            "prev_close".to_owned(),
            quote.prev_close.map_or(OrderedJson::Null, dec),
        ),
        (
            "change_pct".to_owned(),
            change.map_or(OrderedJson::Null, dec),
        ),
        (
            "as_of".to_owned(),
            quote
                .as_of
                .as_ref()
                .map_or(OrderedJson::Null, py_datetime_json),
        ),
        (
            "stale".to_owned(),
            OrderedJson::Bool(is_stale(quote.as_of.as_ref(), served_at)),
        ),
    ])
}

fn closed_marks(
    reason: &'static str,
    market_open: bool,
    as_of: Option<NaiveDate>,
    now: DateTime<FixedOffset>,
    requested: usize,
) -> LiveMarksOut {
    LiveMarksOut {
        live: false,
        reason: Some(reason),
        market_open,
        as_of,
        quotes: OrderedJson::Object(Vec::new()),
        live_overlay: false,
        marks: OrderedJson::Object(Vec::new()),
        served_at: now,
        requested: i64::try_from(requested).unwrap_or(i64::MAX),
        covered: 0,
        stale_after_seconds: STALE_AFTER_SECONDS,
    }
}

/// `GET /meta/live-marks?symbols=`: live last prices while the session is open and a Kite
/// session exists; otherwise the closed answer, which keeps the published close.
///
/// # Errors
/// 401 for an anonymous caller, 500 for a database or token-store failure.
pub async fn get_live_marks(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let names = requested_names(q.get("symbols").unwrap_or(""));
    let as_of = reference::latest_published_date(&state).await?;
    let (_, market_open) = session_day_and_market_open(&state, reference::now_ist()).await?;
    let now = reference::now_ist();
    if !market_open {
        return Ok(wire::json(&closed_marks(
            "market_closed",
            false,
            as_of,
            now,
            names.len(),
        )));
    }
    if !reference::quotes_permitted()? {
        return Ok(wire::json(&closed_marks(
            "no_session",
            true,
            as_of,
            now,
            names.len(),
        )));
    }
    let details = crate::live_prices::live_quote_details(&names)
        .await
        .map_err(|_| crate::problems::internal_error())?;
    let covered = names.iter().filter(|n| details.contains_key(*n)).count();
    if details.is_empty() || covered == 0 {
        return Ok(wire::json(&closed_marks(
            "unavailable",
            true,
            as_of,
            now,
            names.len(),
        )));
    }
    let order: Vec<&String> = names.iter().filter(|n| details.contains_key(*n)).collect();
    let mut quotes = Vec::with_capacity(order.len());
    let mut marks = Vec::with_capacity(order.len());
    for name in order {
        if let Some(quote) = details.get(name) {
            quotes.push((name.clone(), live_quote_out(quote, now)));
            marks.push((
                name.clone(),
                OrderedJson::Str(wire::py_decimal(quote.last_price)),
            ));
        }
    }
    let out = LiveMarksOut {
        live: true,
        reason: None,
        market_open: true,
        as_of,
        quotes: OrderedJson::Object(quotes),
        live_overlay: true,
        marks: OrderedJson::Object(marks),
        served_at: now,
        requested: i64::try_from(names.len()).unwrap_or(i64::MAX),
        covered: i64::try_from(covered).unwrap_or(i64::MAX),
        stale_after_seconds: STALE_AFTER_SECONDS,
    };
    Ok(wire::json(&out))
}

/// The `/meta` routes.
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/meta/factors", get(get_factors))
        .route("/meta/ranking-presets", get(get_ranking_presets))
        .route("/meta/columns", get(get_columns))
        .route("/meta/universes", get(get_universes))
        .route("/meta/trading-days", get(get_trading_days))
        .route("/meta/status", get(get_status))
        .route("/meta/live-marks", get(get_live_marks))
}
