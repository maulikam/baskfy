//! Ports `baskfy_worker.options.backtest_run` (OP12): one sleeve at one tier over stored days,
//! appended to `op_backtest_run`. The computation is `baskfy_core::options::backtest_suite`.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::PathBuf;
use std::time::Duration;

use baskfy_core::options::backtest::{BacktestResult, Tier};
use baskfy_core::options::backtest_suite::{run_tier, tier1_sensitivity};
use baskfy_core::options::bars::Bar;
use baskfy_core::options::calendar::Contract;
use baskfy_core::options::chain::{Level, OptionQuote};
use baskfy_core::options::config::{OptionType, OptionsCeilings, OptionsConfig, Sleeve};
use baskfy_core::options::replay_day::{ModelChain, SnapshotSource, StoredChain};
use baskfy_core::options::scan::{DayContext, MarketDay, Snapshot};
use baskfy_db::OrderedJson;
use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime, Offset as _, Utc};
use rust_decimal::{Decimal, RoundingStrategy};
use sqlx::{PgConnection, Row as _};

use super::common::{dec_str, iso_date};
use crate::error::WorkerError;
use crate::pyjson::{int, object, s};
use crate::settings::WorkerSettings;
use crate::steps::sleeves::for_backtests::core_err;

const NIFTY_50: &str = "NIFTY 50";
const INDIA_VIX: &str = "INDIA VIX";
const NIFTY_50_SLUG: &str = "nifty-50";
const INDIA_VIX_SLUG: &str = "india-vix";
const DAILY_HISTORY: i64 = 120;

fn ist() -> FixedOffset {
    FixedOffset::east_opt(19_800).unwrap_or_else(|| Utc.fix())
}

/// `Sleeve(value)` for a JSON argument.
pub(super) fn sleeve_of(value: &OrderedJson) -> Result<Sleeve, WorkerError> {
    if let OrderedJson::Str(text) = value
        && let Some(found) = Sleeve::ALL.iter().find(|s| s.value() == text)
    {
        return Ok(*found);
    }
    Err(WorkerError::python(
        "ValueError",
        format!("{} is not a valid Sleeve", py_repr(value)),
    ))
}

/// `TIERS[value]`.
pub(super) fn tier_of(value: &OrderedJson) -> Result<Tier, WorkerError> {
    match value {
        OrderedJson::Str(t) if t == "1" => Ok(Tier::Signals),
        OrderedJson::Str(t) if t == "2" => Ok(Tier::Modelled),
        OrderedJson::Str(t) if t == "3" => Ok(Tier::Observed),
        other => Err(WorkerError::python("KeyError", py_repr(other))),
    }
}

/// `repr()` of a JSON value as Python holds it.
fn py_repr(value: &OrderedJson) -> String {
    match value {
        OrderedJson::Null => "None".to_owned(),
        OrderedJson::Bool(true) => "True".to_owned(),
        OrderedJson::Bool(false) => "False".to_owned(),
        OrderedJson::Int(i) => i.to_string(),
        OrderedJson::UInt(u) => u.to_string(),
        OrderedJson::BigInt(b) => b.to_string(),
        OrderedJson::Float(f) => baskfy_providers::py::repr_float(*f),
        OrderedJson::Str(t) => baskfy_providers::py::repr_str(t),
        OrderedJson::Array(items) => format!(
            "[{}]",
            items.iter().map(py_repr).collect::<Vec<_>>().join(", ")
        ),
        OrderedJson::Object(pairs) => format!(
            "{{{}}}",
            pairs
                .iter()
                .map(|(k, v)| format!("{}: {}", baskfy_providers::py::repr_str(k), py_repr(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

/// `options_ceilings(settings)`.
pub(super) fn ceilings(settings: &WorkerSettings) -> OptionsCeilings {
    OptionsCeilings {
        risk_per_trade_inr_max: settings.options_risk_per_trade_inr_max,
        risk_pct_max: settings.options_risk_pct_max,
        max_lots_max: settings.options_max_lots_max,
        book_daily_loss_inr_max: settings.options_book_daily_loss_inr_max,
        book_monthly_loss_inr_max: settings.options_book_monthly_loss_inr_max,
        hard_exit_latest: settings.options_hard_exit_latest,
    }
}

/// `git_sha()`: the checkout's commit, `None` outside a git tree or after five seconds.
pub(super) async fn git_sha() -> Option<String> {
    let dir: PathBuf = std::env::current_exe().ok()?.parent()?.to_path_buf();
    let mut child = std::process::Command::new("git")
        .args(["rev-parse", "HEAD"])
        .current_dir(dir)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .spawn()
        .ok()?;
    let mut waited = Duration::ZERO;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if waited < Duration::from_secs(5) => {
                tokio::time::sleep(Duration::from_millis(20)).await;
                waited += Duration::from_millis(20);
            }
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    };
    if !status.success() {
        return None;
    }
    let mut out = String::new();
    std::io::Read::read_to_string(&mut child.stdout.take()?, &mut out).ok()?;
    let sha: String = out.trim().chars().take(40).collect();
    (!sha.is_empty()).then_some(sha)
}

fn at_ist(day: NaiveDate, t: NaiveTime) -> Result<DateTime<FixedOffset>, WorkerError> {
    day.and_time(t)
        .and_local_timezone(ist())
        .single()
        .ok_or_else(|| WorkerError::python("ValueError", "no IST moment"))
}

/// `session_bounds(day)`: 09:15 and 15:30 IST.
fn session_bounds(
    day: NaiveDate,
    options: &OptionsConfig,
) -> Result<(DateTime<FixedOffset>, DateTime<FixedOffset>), WorkerError> {
    Ok((
        at_ist(day, options.calendar.market_open)?,
        at_ist(day, options.calendar.market_close)?,
    ))
}

/// `trading_days(session, start, end)`: the days with stored NIFTY 50 minute bars.
async fn trading_days(
    conn: &mut PgConnection,
    start: NaiveDate,
    end: NaiveDate,
    options: &OptionsConfig,
) -> Result<Vec<NaiveDate>, WorkerError> {
    let (first, _) = session_bounds(start, options)?;
    let (_, last) = session_bounds(end, options)?;
    let rows: Vec<NaiveDate> = sqlx::query_scalar(
        "SELECT date(timezone('Asia/Kolkata', op_index_minute.ts)) AS date_1 \
         FROM op_index_minute JOIN instrument ON instrument.id = op_index_minute.instrument_id \
         WHERE instrument.symbol = $1 AND op_index_minute.ts >= $2 AND op_index_minute.ts < $3 \
         GROUP BY date(timezone('Asia/Kolkata', op_index_minute.ts)) \
         ORDER BY date(timezone('Asia/Kolkata', op_index_minute.ts))",
    )
    .bind(NIFTY_50)
    .bind(first)
    .bind(last)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows)
}

fn option_type(text: &str) -> Result<OptionType, WorkerError> {
    match text {
        "CE" => Ok(OptionType::Ce),
        "PE" => Ok(OptionType::Pe),
        other => Err(WorkerError::python(
            "ValueError",
            format!(
                "{} is not a valid OptionType",
                baskfy_providers::py::repr_str(other)
            ),
        )),
    }
}

/// `load_bars(session, symbol, day)`.
async fn load_bars(
    conn: &mut PgConnection,
    symbol: &str,
    day: NaiveDate,
    options: &OptionsConfig,
) -> Result<Vec<Bar>, WorkerError> {
    let (start, end) = session_bounds(day, options)?;
    let rows = sqlx::query(
        "SELECT op_index_minute.ts, op_index_minute.open, op_index_minute.high, \
         op_index_minute.low, op_index_minute.close FROM op_index_minute \
         JOIN instrument ON instrument.id = op_index_minute.instrument_id \
         WHERE instrument.symbol = $1 AND op_index_minute.ts >= $2 AND op_index_minute.ts < $3 \
         ORDER BY op_index_minute.ts",
    )
    .bind(symbol)
    .bind(start)
    .bind(end)
    .fetch_all(&mut *conn)
    .await?;
    rows.iter()
        .map(|r| {
            let ts: DateTime<Utc> = r.try_get("ts")?;
            Ok(Bar {
                ts: ts.fixed_offset(),
                open: r.try_get("open")?,
                high: r.try_get("high")?,
                low: r.try_get("low")?,
                close: r.try_get("close")?,
            })
        })
        .collect()
}

/// `daily_levels(session, slug, before, limit)`: oldest first.
async fn daily_levels(
    conn: &mut PgConnection,
    slug: &str,
    before: NaiveDate,
    limit: i64,
) -> Result<Vec<Decimal>, WorkerError> {
    let rows: Vec<Option<Decimal>> = sqlx::query_scalar(
        "SELECT index_snapshot_daily.level FROM index_snapshot_daily \
         JOIN index_def ON index_def.id = index_snapshot_daily.index_id \
         WHERE index_def.slug = $1 AND index_snapshot_daily.date < $2 \
         AND index_snapshot_daily.level IS NOT NULL \
         ORDER BY index_snapshot_daily.date DESC LIMIT $3",
    )
    .bind(slug)
    .bind(before)
    .bind(limit)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows.into_iter().rev().flatten().collect())
}

/// `vix_previous_close(session, day)`.
async fn vix_previous_close(
    conn: &mut PgConnection,
    day: NaiveDate,
    options: &OptionsConfig,
) -> Result<Option<Decimal>, WorkerError> {
    if let Some(last) = daily_levels(conn, INDIA_VIX_SLUG, day, 1).await?.pop() {
        return Ok(Some(last));
    }
    let (start, _) = session_bounds(day, options)?;
    let close: Option<Decimal> = sqlx::query_scalar(
        "SELECT op_index_minute.close FROM op_index_minute \
         JOIN instrument ON instrument.id = op_index_minute.instrument_id \
         WHERE instrument.symbol = $1 AND op_index_minute.ts < $2 \
         ORDER BY op_index_minute.ts DESC LIMIT 1",
    )
    .bind(INDIA_VIX)
    .bind(start)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(close)
}

/// `market_day(session, day, withdrawn, config)`: the master as the day saw it.
async fn market_day(
    conn: &mut PgConnection,
    day: NaiveDate,
    withdrawn: &BTreeSet<NaiveDate>,
    options: &OptionsConfig,
) -> Result<MarketDay, WorkerError> {
    let rows = sqlx::query(
        "SELECT op_contract.instrument_token, op_contract.tradingsymbol, op_contract.underlying, \
         op_contract.expiry, op_contract.strike, op_contract.option_type, op_contract.lot_size, \
         op_contract.tick_size FROM op_contract \
         WHERE op_contract.underlying = $1 AND op_contract.expiry >= $2",
    )
    .bind(&options.calendar.underlying)
    .bind(day)
    .fetch_all(&mut *conn)
    .await?;
    let mut contracts = Vec::new();
    for r in &rows {
        let expiry: NaiveDate = r.try_get("expiry")?;
        if withdrawn.contains(&expiry) {
            continue;
        }
        let kind: String = r.try_get("option_type")?;
        contracts.push(Contract {
            instrument_token: r.try_get("instrument_token")?,
            tradingsymbol: r.try_get("tradingsymbol")?,
            underlying: r.try_get("underlying")?,
            expiry,
            strike: r.try_get("strike")?,
            option_type: option_type(&kind)?,
            lot_size: i64::from(r.try_get::<i32, _>("lot_size")?),
            tick_size: r.try_get("tick_size")?,
        });
    }
    Ok(MarketDay {
        trade_date: day,
        trading_day: true,
        contracts,
        bars: load_bars(conn, NIFTY_50, day, options).await?,
        daily_closes: daily_levels(conn, NIFTY_50_SLUG, day, DAILY_HISTORY).await?,
        vix_prev_close: vix_previous_close(conn, day, options).await?,
    })
}

fn invalid_operation() -> WorkerError {
    WorkerError::python("InvalidOperation", "[<class 'decimal.ConversionSyntax'>]")
}

/// `_levels(raw)`: a depth side as the pure core's levels.
pub(super) fn levels(raw: Option<&OrderedJson>) -> Result<Vec<Level>, WorkerError> {
    let Some(OrderedJson::Array(items)) = raw else {
        return Ok(Vec::new());
    };
    let mut out = Vec::new();
    for item in items {
        let OrderedJson::Object(_) = item else {
            continue;
        };
        let quantity = match item.get("quantity") {
            Some(OrderedJson::Int(q)) => *q,
            Some(OrderedJson::Bool(b)) => i64::from(*b),
            _ => continue,
        };
        let text = match item.get("price") {
            Some(OrderedJson::Str(t)) => t.clone(),
            Some(OrderedJson::Int(i)) => i.to_string(),
            Some(OrderedJson::Float(f)) => baskfy_providers::py::repr_float(*f),
            Some(OrderedJson::UInt(u)) => u.to_string(),
            Some(OrderedJson::BigInt(b)) => b.to_string(),
            Some(OrderedJson::Bool(_)) => return Err(invalid_operation()),
            _ => continue,
        };
        let wide: baskfy_core::decimal::WideDecimal =
            text.trim().parse().map_err(|_| invalid_operation())?;
        let price = wide.try_to_decimal().map_err(|_| invalid_operation())?;
        out.push(Level { price, quantity });
    }
    Ok(out)
}

/// `stored_chain(session, day)`: every stored minute of the day, as the collector wrote it.
async fn stored_chain(
    conn: &mut PgConnection,
    day: NaiveDate,
    options: &OptionsConfig,
) -> Result<StoredChain, WorkerError> {
    let (start, end) = session_bounds(day, options)?;
    let rows = sqlx::query(
        "SELECT op_chain_snapshot.ts, op_chain_snapshot.instrument_token, \
         op_chain_snapshot.expiry, op_chain_snapshot.strike, op_chain_snapshot.option_type, \
         op_chain_snapshot.spot, op_chain_snapshot.bid, op_chain_snapshot.ask, \
         op_chain_snapshot.last, op_chain_snapshot.depth_json, op_chain_snapshot.volume, \
         op_chain_snapshot.oi FROM op_chain_snapshot \
         WHERE op_chain_snapshot.ts >= $1 AND op_chain_snapshot.ts < $2 \
         ORDER BY op_chain_snapshot.ts",
    )
    .bind(start)
    .bind(end)
    .fetch_all(&mut *conn)
    .await?;
    let mut order: Vec<DateTime<FixedOffset>> = Vec::new();
    let mut by_minute: HashMap<DateTime<FixedOffset>, (Option<Decimal>, Vec<OptionQuote>)> =
        HashMap::new();
    for r in &rows {
        let ts: DateTime<Utc> = r.try_get("ts")?;
        let key = ts.with_timezone(&ist());
        let spot: Option<Decimal> = r.try_get("spot")?;
        let depth = baskfy_db::row::opt_jsonb(r, "depth_json").map_err(WorkerError::from)?;
        let kind: String = r.try_get("option_type")?;
        let quote = OptionQuote {
            instrument_token: r.try_get("instrument_token")?,
            expiry: r.try_get("expiry")?,
            strike: r.try_get("strike")?,
            option_type: option_type(&kind)?,
            bid: r.try_get("bid")?,
            ask: r.try_get("ask")?,
            bids: levels(depth.as_ref().and_then(|d| d.get("buy")))?,
            asks: levels(depth.as_ref().and_then(|d| d.get("sell")))?,
            oi: r.try_get::<Option<i64>, _>("oi")?.unwrap_or(0),
            ts: ts.fixed_offset(),
            last: r.try_get("last")?,
            volume: r.try_get::<Option<i64>, _>("volume")?.unwrap_or(0),
        };
        let entry = by_minute.entry(key).or_insert_with(|| {
            order.push(key);
            (None, Vec::new())
        });
        if entry.0.is_none() {
            entry.0 = spot;
        }
        entry.1.push(quote);
    }
    let mut minutes = BTreeMap::new();
    for ts in order {
        if let Some((spot, quotes)) = by_minute.remove(&ts) {
            minutes.insert(ts, Snapshot { ts, spot, quotes });
        }
    }
    Ok(StoredChain { minutes })
}

fn money(value: Option<Decimal>) -> OrderedJson {
    value.map_or(OrderedJson::Null, |v| s(dec_str(v)))
}

fn count(n: i64) -> OrderedJson {
    int(n)
}

/// `result_json(result)`.
pub(super) fn result_json(result: &BacktestResult) -> Vec<(String, OrderedJson)> {
    vec![
        ("sessions".to_owned(), count(result.sessions)),
        ("traded".to_owned(), count(result.traded)),
        (
            "skipped_by_reason".to_owned(),
            skipped_json(&result.skipped_by_reason),
        ),
        (
            "by_year".to_owned(),
            OrderedJson::Array(
                result
                    .by_year
                    .iter()
                    .map(|(y, n, t)| OrderedJson::Array(vec![int(i64::from(*y)), int(*n), int(*t)]))
                    .collect(),
            ),
        ),
        ("win_rate".to_owned(), money(result.win_rate)),
        ("expectancy_r".to_owned(), money(result.expectancy_r)),
        ("net_pnl_inr".to_owned(), money(result.net_pnl_inr)),
        ("max_drawdown_r".to_owned(), money(result.max_drawdown_r)),
        (
            "caveats".to_owned(),
            OrderedJson::Array(result.caveats.iter().map(s).collect()),
        ),
    ]
}

/// `dict(result.skipped_by_reason)`: the last count wins for a repeated reason, first position kept.
fn skipped_json(pairs: &[(String, i64)]) -> OrderedJson {
    let mut out: Vec<(String, OrderedJson)> = Vec::new();
    for (reason, n) in pairs {
        crate::pyjson::update(&mut out, reason, int(*n));
    }
    OrderedJson::Object(out)
}

/// `options_json(options)`: the rules as plain JSON.
///
/// # Errors
/// A config that does not serialise (never, for a dataclass of plain values).
pub(super) fn options_json(options: &OptionsConfig) -> Result<OrderedJson, WorkerError> {
    let text = serde_json::to_string(options)
        .map_err(|e| WorkerError::python("TypeError", e.to_string()))?;
    OrderedJson::parse(&text).map_err(|e| WorkerError::python("TypeError", e.to_string()))
}

fn four(value: Option<Decimal>) -> Option<Decimal> {
    value.map(|v| v.round_dp_with_strategy(4, RoundingStrategy::MidpointNearestEven))
}

fn two(value: Option<Decimal>) -> Option<Decimal> {
    value.map(|v| v.round_dp_with_strategy(2, RoundingStrategy::MidpointNearestEven))
}

/// What one run asks for.
pub(super) struct OptionsRun {
    pub user_id: i64,
    pub sleeve: Sleeve,
    pub tier: Tier,
    pub start: NaiveDate,
    pub end: NaiveDate,
    pub options: OptionsConfig,
    pub ceilings: OptionsCeilings,
    pub sha: Option<String>,
}

fn withdrawn_in(detail: &OrderedJson) -> bool {
    match detail {
        OrderedJson::Object(pairs) => pairs.iter().any(|(k, _)| k == "withdrawn_on"),
        OrderedJson::Array(items) => items.contains(&s("withdrawn_on")),
        OrderedJson::Str(t) => t.contains("withdrawn_on"),
        _ => false,
    }
}

/// `run_backtest(session, user_id, sleeve, tier, start, end, options=, ceilings=, sha=)`:
/// runs and appends one `op_backtest_run` row; returns `BacktestReport.as_dict()`.
///
/// # Errors
/// `ValueError` when the run ends before it starts; a database failure; a core refusal.
pub(super) async fn run_backtest(
    conn: &mut PgConnection,
    run: &OptionsRun,
) -> Result<OrderedJson, WorkerError> {
    if run.end < run.start {
        return Err(WorkerError::python(
            "ValueError",
            "the run ends before it starts",
        ));
    }
    let options = &run.options;
    let expiries = sqlx::query(
        "SELECT op_expiry.expiry_date, op_expiry.detail FROM op_expiry \
         WHERE op_expiry.underlying = $1",
    )
    .bind(&options.calendar.underlying)
    .fetch_all(&mut *conn)
    .await?;
    let mut withdrawn = BTreeSet::new();
    for r in &expiries {
        let detail = baskfy_db::row::jsonb(r, "detail").map_err(WorkerError::from)?;
        if withdrawn_in(&detail) {
            withdrawn.insert(r.try_get::<NaiveDate, _>("expiry_date")?);
        }
    }
    let events: Vec<NaiveDate> = sqlx::query_scalar(
        "SELECT op_event_day.date FROM op_event_day WHERE op_event_day.user_id = $1",
    )
    .bind(run.user_id)
    .fetch_all(&mut *conn)
    .await?;
    let days = trading_days(conn, run.start, run.end, options).await?;
    let trading = i64::try_from(days.len()).unwrap_or(i64::MAX);
    let mut uncalendared: i64 = 0;
    let mut markets = Vec::new();
    for day in days {
        let market = market_day(conn, day, &withdrawn, options).await?;
        if market.contracts.is_empty() {
            uncalendared += 1;
            continue;
        }
        markets.push(market);
    }
    let mut sources: HashMap<NaiveDate, StoredChain> = HashMap::new();
    if run.tier == Tier::Observed {
        for market in &markets {
            let chain = stored_chain(conn, market.trade_date, options).await?;
            sources.insert(market.trade_date, chain);
        }
    }
    let context = DayContext {
        event_days: events
            .into_iter()
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect(),
        ..DayContext::default()
    };
    let tier = run.tier;
    let mut source_for = |market: &MarketDay| -> Box<dyn SnapshotSource> {
        if tier == Tier::Modelled {
            Box::new(ModelChain::new(market.clone(), options.clone()))
        } else {
            Box::new(sources.remove(&market.trade_date).unwrap_or_default())
        }
    };
    let result = run_tier(
        run.sleeve,
        tier,
        &markets,
        &context,
        options,
        &run.ceilings,
        if tier == Tier::Signals {
            None
        } else {
            Some(&mut source_for)
        },
    )
    .map_err(core_err)?;
    let mut params = vec![
        ("trading_days".to_owned(), int(trading)),
        ("uncalendared".to_owned(), int(uncalendared)),
        ("options".to_owned(), options_json(options)?),
    ];
    if tier == Tier::Signals {
        let rows = tier1_sensitivity(run.sleeve, &markets, &context, options).map_err(core_err)?;
        params.push((
            "sensitivity".to_owned(),
            OrderedJson::Array(
                rows.iter()
                    .map(|x| {
                        object([
                            ("threshold", s(format!("{}.{}", x.group, x.field))),
                            ("factor", s(dec_str(x.factor))),
                            ("value", s(dec_str(x.value))),
                            ("sessions", int(x.sessions)),
                            ("signals", int(x.signals)),
                        ])
                    })
                    .collect(),
            ),
        ));
    }
    let params_text = super::common::jsonb_text(&OrderedJson::Object(params))?;
    let skipped_text = super::common::jsonb_text(&skipped_json(&result.skipped_by_reason))?;
    let tier_number: i16 = match tier {
        Tier::Signals => 1,
        Tier::Modelled => 2,
        Tier::Observed => 3,
    };
    let to_i32 = |n: i64| {
        i32::try_from(n).map_err(|_| WorkerError::python("DBAPIError", "integer out of range"))
    };
    let run_id: i64 = sqlx::query_scalar(
        "INSERT INTO op_backtest_run (user_id, sleeve, tier, params_json, date_from, date_to, \
         sessions, signals, traded, skipped_by_reason_json, win_rate, expectancy_r, \
         net_pnl_inr, max_drawdown_r, caveats, git_sha) VALUES ($1, CAST($2 AS op_sleeve), $3, \
         CAST($4 AS JSONB), $5, $6, $7, $8, $9, CAST($10 AS JSONB), $11, $12, $13, $14, $15, \
         $16) RETURNING op_backtest_run.id",
    )
    .bind(run.user_id)
    .bind(run.sleeve.value())
    .bind(tier_number)
    .bind(params_text)
    .bind(run.start)
    .bind(run.end)
    .bind(to_i32(result.sessions)?)
    .bind(to_i32(result.traded)?)
    .bind(to_i32(if tier == Tier::Signals {
        0
    } else {
        result.traded
    })?)
    .bind(skipped_text)
    .bind(four(result.win_rate))
    .bind(four(result.expectancy_r))
    .bind(two(result.net_pnl_inr))
    .bind(two(result.max_drawdown_r))
    .bind(result.caveats.join("\n"))
    .bind(run.sha.as_deref())
    .fetch_one(&mut *conn)
    .await?;
    let mut out = vec![
        ("sleeve".to_owned(), s(run.sleeve.value())),
        ("tier".to_owned(), s(tier.value())),
        ("from".to_owned(), s(iso_date(run.start))),
        ("to".to_owned(), s(iso_date(run.end))),
        ("trading_days".to_owned(), int(trading)),
        ("uncalendared".to_owned(), int(uncalendared)),
        ("run_id".to_owned(), int(run_id)),
    ];
    for (k, v) in result_json(&result) {
        crate::pyjson::update(&mut out, &k, v);
    }
    Ok(OrderedJson::Object(out))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::panic)]
mod tests {
    use super::*;

    fn err_of<T>(r: Result<T, WorkerError>) -> (String, String) {
        match r {
            Ok(_) => panic!("expected an error"),
            Err(e) => (e.python_type(), e.to_string()),
        }
    }

    #[test]
    fn a_sleeve_and_a_tier_are_read_as_python_reads_them() {
        assert_eq!(sleeve_of(&s("O3B")).unwrap(), Sleeve::O3b);
        assert_eq!(
            err_of(sleeve_of(&s("o3b"))),
            (
                "ValueError".to_owned(),
                "'o3b' is not a valid Sleeve".to_owned()
            )
        );
        assert_eq!(
            err_of(sleeve_of(&OrderedJson::Int(2))).1,
            "2 is not a valid Sleeve"
        );
        assert_eq!(tier_of(&s("3")).unwrap(), Tier::Observed);
        assert_eq!(
            err_of(tier_of(&s("4"))),
            ("KeyError".to_owned(), "'4'".to_owned())
        );
        assert_eq!(err_of(tier_of(&OrderedJson::Int(1))).1, "1");
    }

    #[test]
    fn depth_levels_keep_only_well_formed_entries() {
        let raw = OrderedJson::parse(
            r#"[{"price": "101.5", "quantity": 75}, {"price": 99, "quantity": 50},
                {"price": 98.25, "quantity": 25}, {"price": "1", "quantity": "x"}, 7]"#,
        )
        .unwrap();
        let got = levels(Some(&raw)).unwrap();
        assert_eq!(
            got.iter()
                .map(|l| (l.price.to_string(), l.quantity))
                .collect::<Vec<_>>(),
            vec![
                ("101.5".to_owned(), 75),
                ("99".to_owned(), 50),
                ("98.25".to_owned(), 25)
            ]
        );
        assert!(levels(Some(&s("buy"))).unwrap().is_empty());
        assert!(levels(None).unwrap().is_empty());
        let bad = OrderedJson::parse(r#"[{"price": "abc", "quantity": 1}]"#).unwrap();
        assert_eq!(err_of(levels(Some(&bad))).0, "InvalidOperation");
    }

    #[test]
    fn the_result_renders_as_result_json() {
        let result = BacktestResult {
            sleeve: Sleeve::O2,
            tier: Tier::Modelled,
            date_from: None,
            date_to: None,
            sessions: 5,
            traded: 2,
            skipped_by_reason: vec![("EVENT_DAY".to_owned(), 1), ("NO_SIGNAL".to_owned(), 2)],
            by_year: vec![(2026, 5, 2)],
            win_rate: Some(Decimal::new(5000, 4)),
            expectancy_r: None,
            net_pnl_inr: Some(Decimal::new(-12345, 2)),
            max_drawdown_r: Some(Decimal::new(10, 1)),
            caveats: vec!["a".to_owned(), "b".to_owned()],
        };
        let text = crate::pyjson::dumps(&OrderedJson::Object(result_json(&result))).unwrap();
        assert_eq!(
            text,
            r#"{"sessions": 5, "traded": 2, "skipped_by_reason": {"EVENT_DAY": 1, "NO_SIGNAL": 2}, "by_year": [[2026, 5, 2]], "win_rate": "0.5000", "expectancy_r": null, "net_pnl_inr": "-123.45", "max_drawdown_r": "1.0", "caveats": ["a", "b"]}"#
        );
    }

    #[test]
    fn the_rules_render_as_pythons_asdict() {
        let json = options_json(&OptionsConfig::default()).unwrap();
        let get = |path: &[&str]| {
            path.iter()
                .try_fold(&json, |v, k| v.get(k))
                .cloned()
                .unwrap_or(OrderedJson::Null)
        };
        assert_eq!(get(&["calendar", "market_open"]), s("09:15:00"));
        assert_eq!(get(&["chain", "min_premium"]), s("0.50"));
        assert_eq!(get(&["chain", "oi_unit"]), s("UNITS"));
        assert_eq!(get(&["condor_monthly", "variant"]), s("MONTHLY"));
        assert_eq!(get(&["condor_weekly", "tier3_min_sessions"]), int(20));
        assert_eq!(get(&["directional", "vix_max"]), s("22"));
        assert_eq!(
            get(&["expiry_setups", "o3a_enabled"]),
            OrderedJson::Bool(true)
        );
        assert_eq!(get(&["chain", "rate"]), OrderedJson::Float(0.065));
    }

    #[test]
    fn rounding_is_half_even_as_quantize() {
        assert_eq!(
            four(Some(Decimal::new(123_455, 5))),
            Some(Decimal::new(12346, 4))
        );
        assert_eq!(
            four(Some(Decimal::new(123_445, 5))),
            Some(Decimal::new(12344, 4))
        );
        assert_eq!(two(Some(Decimal::new(1005, 3))), Some(Decimal::new(100, 2)));
        assert_eq!(two(None), None);
    }

    #[test]
    fn a_withdrawn_expiry_is_named_by_its_detail() {
        assert!(withdrawn_in(
            &OrderedJson::parse(r#"{"withdrawn_on": "2026-10-01"}"#).unwrap()
        ));
        assert!(!withdrawn_in(
            &OrderedJson::parse(r#"{"listed_on": "2026-10-01"}"#).unwrap()
        ));
    }
}

/// `test_options_backtest_task.py`'s database tests, on a clone of the bed inside one rolled-back
/// transaction. Run with `BASKFY_RS_SCRATCH_DATABASE_URL=postgres://…/rst_… -- --ignored`.
#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
mod db {
    use super::*;
    use baskfy_core::options::backtest::{TIER1_CAVEAT, TIER2_CAVEAT};
    use sqlx::{Connection as _, Postgres, Transaction};

    const DAY: (i32, u32, u32) = (2031, 7, 8);
    const AFTER: (i32, u32, u32) = (2031, 8, 5);

    fn d((y, m, day): (i32, u32, u32)) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, day).unwrap()
    }

    async fn clone_conn() -> PgConnection {
        let url = std::env::var("BASKFY_RS_SCRATCH_DATABASE_URL")
            .unwrap_or_else(|_| panic!("needs BASKFY_RS_SCRATCH_DATABASE_URL (a bed clone)"));
        let name = url
            .rsplit('/')
            .next()
            .unwrap_or("")
            .split('?')
            .next()
            .unwrap_or("");
        assert!(
            name.starts_with("rst_"),
            "refusing {name:?}: only an rst_* clone"
        );
        PgConnection::connect(&url).await.unwrap()
    }

    async fn exec(tx: &mut Transaction<'_, Postgres>, sql: &str) {
        sqlx::raw_sql(sql).execute(&mut **tx).await.unwrap();
    }

    /// Two days of NIFTY 50 minutes (the first calendared by a weekly and a monthly expiry, the
    /// second past every listed expiry) and a previous close for each index.
    async fn seed(tx: &mut Transaction<'_, Postgres>) -> i64 {
        let user: i64 = sqlx::query_scalar("SELECT min(id) FROM app_user")
            .fetch_one(&mut **tx)
            .await
            .unwrap();
        for day in ["2031-07-08", "2031-08-05"] {
            exec(
                tx,
                &format!(
                    "INSERT INTO op_index_minute (instrument_id, ts, open, high, low, close, source) \
                     SELECT i.id, TIMESTAMPTZ '{day} 09:15:00+05:30' + make_interval(mins => g), \
                     24000 + g % 7, 24004 + g % 7, 23996 + g % 7, 24001 + g % 7, 'KITE_HIST' \
                     FROM instrument i, generate_series(0, 374) g WHERE i.symbol = 'NIFTY 50'"
                ),
            )
            .await;
        }
        let mut contracts = Vec::new();
        let mut token = 990_100_000_i64;
        for (expiry, code) in [("2031-07-08", "31708"), ("2031-07-29", "31JUL")] {
            for strike in (23_000..=25_000).step_by(50) {
                for kind in ["CE", "PE"] {
                    token += 1;
                    contracts.push(format!(
                        "({token}, 'NIFTY{code}{strike}{kind}', 'NIFTY', DATE '{expiry}', {strike}, \
                         '{kind}', 75, 0.05, DATE '2031-06-01', DATE '2031-07-08', false)"
                    ));
                }
            }
        }
        exec(
            tx,
            &format!(
                "INSERT INTO op_contract (instrument_token, tradingsymbol, underlying, expiry, strike, \
                 option_type, lot_size, tick_size, first_seen, last_seen, expired) VALUES {}",
                contracts.join(", ")
            ),
        )
        .await;
        user
    }

    fn run(
        user_id: i64,
        sleeve: Sleeve,
        tier: Tier,
        start: NaiveDate,
        end: NaiveDate,
    ) -> OptionsRun {
        OptionsRun {
            user_id,
            sleeve,
            tier,
            start,
            end,
            options: OptionsConfig::default(),
            ceilings: OptionsCeilings::default(),
            sha: Some("abc123".to_owned()),
        }
    }

    type Stored = (
        String,
        i16,
        i32,
        i32,
        i32,
        String,
        Option<String>,
        serde_json::Value,
    );

    async fn row(tx: &mut Transaction<'_, Postgres>, id: i64) -> Stored {
        sqlx::query_as(
            "SELECT sleeve::text, tier, sessions, signals, traded, caveats, git_sha, params_json \
             FROM op_backtest_run WHERE id = $1",
        )
        .bind(id)
        .fetch_one(&mut **tx)
        .await
        .unwrap()
    }

    fn run_id(report: &OrderedJson) -> i64 {
        match report.get("run_id") {
            Some(OrderedJson::Int(i)) => *i,
            other => panic!("run_id {other:?}"),
        }
    }

    #[tokio::test]
    #[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
    async fn tier_one_writes_one_row_and_counts_the_uncalendared_day() {
        let mut conn = clone_conn().await;
        let mut tx = conn.begin().await.unwrap();
        let user = seed(&mut tx).await;
        let report = run_backtest(
            &mut tx,
            &run(user, Sleeve::O3b, Tier::Signals, d(DAY), d(AFTER)),
        )
        .await
        .unwrap();
        assert_eq!(report.get("trading_days"), Some(&int(2)));
        assert_eq!(report.get("uncalendared"), Some(&int(1)));
        assert_eq!(report.get("tier"), Some(&s("1")));
        let (sleeve, tier, _, _, traded, caveats, sha, params) =
            row(&mut tx, run_id(&report)).await;
        assert_eq!((sleeve.as_str(), tier, traded), ("O3B", 1, 0));
        assert_eq!(caveats, TIER1_CAVEAT);
        assert_eq!(sha.as_deref(), Some("abc123"));
        assert_eq!(params["uncalendared"], 1);
        let thresholds: BTreeSet<String> = params["sensitivity"]
            .as_array()
            .unwrap()
            .iter()
            .map(|x| x["threshold"].as_str().unwrap().to_owned())
            .collect();
        assert_eq!(params["sensitivity"].as_array().unwrap().len(), 4);
        assert_eq!(
            thresholds,
            [
                "expiry_setups.o3b_gap_max_pct",
                "expiry_setups.o3b_gap_min_pct"
            ]
            .into_iter()
            .map(str::to_owned)
            .collect()
        );
        tx.rollback().await.unwrap();
    }

    #[tokio::test]
    #[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
    async fn an_event_day_is_skipped_as_the_live_scan_would() {
        let mut conn = clone_conn().await;
        let mut tx = conn.begin().await.unwrap();
        let user = seed(&mut tx).await;
        sqlx::query("INSERT INTO op_event_day (user_id, date, reason, source) VALUES ($1, $2, 'MANUAL', 'USER')")
            .bind(user)
            .bind(d(DAY))
            .execute(&mut *tx)
            .await
            .unwrap();
        let report = run_backtest(
            &mut tx,
            &run(user, Sleeve::O3b, Tier::Signals, d(DAY), d(DAY)),
        )
        .await
        .unwrap();
        assert_eq!(
            report.get("skipped_by_reason"),
            Some(&object([("EVENT_DAY", int(1))]))
        );
        tx.rollback().await.unwrap();
    }

    #[tokio::test]
    #[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
    async fn tier_two_stores_the_model_caveat_and_a_withdrawn_expiry_uncalendars() {
        let mut conn = clone_conn().await;
        let mut tx = conn.begin().await.unwrap();
        let user = seed(&mut tx).await;
        let report = run_backtest(
            &mut tx,
            &run(user, Sleeve::O3b, Tier::Modelled, d(DAY), d(DAY)),
        )
        .await
        .unwrap();
        let (_, tier, _, _, _, caveats, _, params) = row(&mut tx, run_id(&report)).await;
        assert_eq!(tier, 2);
        assert!(caveats.lines().any(|l| l == TIER2_CAVEAT), "{caveats}");
        assert!(params.get("sensitivity").is_none());
        exec(
            &mut tx,
            "INSERT INTO op_expiry (underlying, expiry_date, kind, lot_size, first_seen, seen_on, detail) \
             VALUES ('NIFTY', DATE '2031-07-08', 'WEEKLY', 75, DATE '2031-06-01', DATE '2031-07-08', \
             '{\"withdrawn_on\": \"2031-07-01\"}'), ('NIFTY', DATE '2031-07-29', 'MONTHLY', 75, \
             DATE '2031-06-01', DATE '2031-07-08', '{\"withdrawn_on\": \"2031-07-01\"}')",
        )
        .await;
        let again = run_backtest(
            &mut tx,
            &run(user, Sleeve::O3b, Tier::Signals, d(DAY), d(DAY)),
        )
        .await
        .unwrap();
        assert_eq!(again.get("uncalendared"), Some(&int(1)));
        assert_eq!(again.get("sessions"), Some(&int(0)));
        tx.rollback().await.unwrap();
    }

    #[tokio::test]
    #[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
    async fn the_stored_chain_reads_back_by_ist_minute_with_its_depth() {
        let mut conn = clone_conn().await;
        let mut tx = conn.begin().await.unwrap();
        exec(
            &mut tx,
            "CREATE TABLE op_chain_snapshot_rst_test PARTITION OF op_chain_snapshot FOR VALUES \
             FROM ('2031-07-08 00:00:00+05:30') TO ('2031-07-09 00:00:00+05:30')",
        )
        .await;
        exec(
            &mut tx,
            "INSERT INTO op_chain_snapshot (ts, instrument_token, expiry, strike, option_type, spot, \
             bid, ask, last, depth_json, volume, oi, source) VALUES \
             (TIMESTAMPTZ '2031-07-08 09:45:00+05:30', 990100001, DATE '2031-07-08', 24000, 'CE', NULL, 100.5, 101, NULL, \
              '{\"buy\": [{\"price\": \"100.5\", \"quantity\": 75}], \"sell\": [{\"price\": \"101\", \"quantity\": 150}]}', 0, 900, 'QUOTE'), \
             (TIMESTAMPTZ '2031-07-08 09:45:00+05:30', 990100002, DATE '2031-07-08', 24000, 'PE', 24001.5, 90, 91, 90.5, NULL, NULL, NULL, 'QUOTE'), \
             (TIMESTAMPTZ '2031-07-08 09:46:00+05:30', 990100001, DATE '2031-07-08', 24000, 'CE', 24002, 99, 100, NULL, NULL, 5, 10, 'QUOTE'), \
             (TIMESTAMPTZ '2031-07-08 15:30:00+05:30', 990100001, DATE '2031-07-08', 24000, 'CE', 24002, 99, 100, NULL, NULL, 5, 10, 'QUOTE')",
        )
        .await;
        let chain = stored_chain(&mut tx, d(DAY), &OptionsConfig::default())
            .await
            .unwrap();
        assert_eq!(chain.minutes.len(), 2, "15:30 is past the session");
        let first = chain.minutes.values().next().unwrap();
        assert_eq!(first.ts.offset().local_minus_utc(), 19_800);
        assert_eq!(first.spot, Some(Decimal::new(240_015, 1)));
        assert_eq!(first.quotes.len(), 2);
        let ce = &first.quotes[0];
        assert_eq!(
            (ce.oi, ce.volume, ce.bids.len(), ce.asks.len()),
            (900, 0, 1, 1)
        );
        assert_eq!(ce.asks[0].quantity, 150);
        let pe = &first.quotes[1];
        assert_eq!(
            (pe.oi, pe.volume, pe.last),
            (0, 0, Some(Decimal::new(905, 1)))
        );
        tx.rollback().await.unwrap();
    }

    #[tokio::test]
    #[ignore = "needs BASKFY_RS_SCRATCH_DATABASE_URL"]
    async fn a_run_that_ends_before_it_starts_writes_nothing() {
        let mut conn = clone_conn().await;
        let mut tx = conn.begin().await.unwrap();
        let before: i64 = sqlx::query_scalar("SELECT count(*) FROM op_backtest_run")
            .fetch_one(&mut *tx)
            .await
            .unwrap();
        let err = run_backtest(
            &mut tx,
            &run(1, Sleeve::O1m, Tier::Signals, d(AFTER), d(DAY)),
        )
        .await
        .err()
        .unwrap();
        assert_eq!(
            (err.python_type(), err.to_string()),
            (
                "ValueError".to_owned(),
                "the run ends before it starts".to_owned()
            )
        );
        let after: i64 = sqlx::query_scalar("SELECT count(*) FROM op_backtest_run")
            .fetch_one(&mut *tx)
            .await
            .unwrap();
        assert_eq!(before, after);
        tx.rollback().await.unwrap();
    }
}
