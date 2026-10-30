//! The half of `baskfy_api.backtests` the job writes with: the three CSV artefacts, the stored
//! payload (`build_payload`: metrics, the metrics hash, the fragility spread, the assumptions
//! panel, the monthly returns and the downsampled curves) and the SSE progress frame.
//!
//! Every `Decimal` prints as Python's `str(Decimal)` ([`WideDecimal::to_sci_string`]); every
//! statistic is `baskfy_core::backtest_metrics`'s, so the hash is the core's.

use std::collections::BTreeMap;

use baskfy_core::backtest::engine::{
    BacktestConfig, BacktestResult, CashPolicy, ConfigValue, FragilityReport, HoldingSnapshot,
    Trade, TradeReason, TradeSide,
};
use baskfy_core::backtest::helpers::WideDecimal;
use baskfy_core::backtest_metrics::{
    self as bm, BacktestOutcome as MetricsInput, EQUITY_CURVE_POINTS, Json, Metrics, MetricsConfig,
};
use baskfy_core::error::CoreError;
use baskfy_db::OrderedJson;
use chrono::NaiveDate;
use rust_decimal::Decimal;

use super::loader::Stop;
use crate::error::WorkerError;

/// docs/10 §Artefacts. `trades` is the one docs/04 gives a column to.
pub(super) const ARTEFACTS: [&str; 3] = ["trades", "holdings", "equity"];

const TRADE_COLUMNS: [&str; 9] = [
    "date",
    "symbol",
    "side",
    "quantity",
    "price",
    "notional",
    "cost",
    "reason",
    "realised_pnl",
];
const HOLDING_COLUMNS: [&str; 10] = [
    "rebalance_date",
    "executed_on",
    "symbol",
    "name",
    "rank",
    "target_weight",
    "quantity",
    "price",
    "value",
    "actual_weight",
];
const EQUITY_COLUMNS: [&str; 6] = [
    "date",
    "equity",
    "cash",
    "invested",
    "drawdown",
    "benchmark",
];

/// The drawdown curve keeps one point in this many (`len // 1500`).
const DRAWDOWN_POINTS: usize = 1500;

/// `artefact_key(public_id, artefact)`: `backtests/{public_id}/{artefact}.csv`.
pub(super) fn artefact_key(public_id: &str, artefact: &str) -> String {
    format!("backtests/{public_id}/{artefact}.csv")
}

/// `progress_key(public_id)`.
pub(super) fn progress_key(public_id: &str) -> String {
    format!("backtest:progress:{public_id}")
}

/// `events_channel(public_id)`.
pub(super) fn events_channel(public_id: &str) -> String {
    format!("backtest:events:{public_id}")
}

/// `str(Decimal)`.
pub(super) fn dec(value: Decimal) -> String {
    WideDecimal::from(value).to_sci_string()
}

fn date(day: NaiveDate) -> String {
    day.format("%Y-%m-%d").to_string()
}

/// A core error the way the job sees it: `ValueError` (a broken `zip(strict=True)`) is caught,
/// decimal arithmetic and polars are not.
pub(super) fn core_stop(e: &CoreError) -> Stop {
    match e {
        CoreError::InvalidInput(m) | CoreError::MissingColumn(m) => Stop::caught("ValueError", m),
        CoreError::Arithmetic(m) => Stop::Raised(WorkerError::python("InvalidOperation", m)),
        CoreError::Polars(p) => Stop::Raised(WorkerError::python("PolarsError", p.to_string())),
    }
}

// ---------------------------------------------------------------------------------------------
// CSV artefacts
// ---------------------------------------------------------------------------------------------

/// `_cell(value)`: `None` is empty; a cell holding `,`, `"` or a newline is quoted.
fn cell(value: Option<String>) -> String {
    let Some(text) = value else {
        return String::new();
    };
    if text.contains([',', '"', '\n']) {
        format!("\"{}\"", text.replace('"', "\"\""))
    } else {
        text
    }
}

/// `_csv(header, rows)`: RFC 4180 with `\n` line endings, a trailing newline.
fn csv(header: &[&str], rows: Vec<Vec<Option<String>>>) -> Vec<u8> {
    let mut lines = vec![header.join(",")];
    for row in rows {
        lines.push(row.into_iter().map(cell).collect::<Vec<_>>().join(","));
    }
    let mut text = lines.join("\n");
    text.push('\n');
    text.into_bytes()
}

fn side(value: TradeSide) -> &'static str {
    match value {
        TradeSide::Buy => "buy",
        TradeSide::Sell => "sell",
    }
}

fn reason(value: TradeReason) -> &'static str {
    match value {
        TradeReason::Enter => "enter",
        TradeReason::Exit => "exit",
        TradeReason::Rebalance => "rebalance",
        TradeReason::Delist => "delist",
    }
}

/// `trades_csv(trades)`.
pub(super) fn trades_csv(trades: &[Trade]) -> Vec<u8> {
    csv(
        &TRADE_COLUMNS,
        trades
            .iter()
            .map(|t| {
                vec![
                    Some(date(t.date)),
                    Some(t.symbol.clone()),
                    Some(side(t.side).to_owned()),
                    Some(t.quantity.to_string()),
                    Some(dec(t.price)),
                    Some(dec(t.notional)),
                    Some(dec(t.cost)),
                    Some(reason(t.reason).to_owned()),
                    t.realised_pnl.map(dec),
                ]
            })
            .collect(),
    )
}

/// `holdings_csv(holdings)`.
pub(super) fn holdings_csv(holdings: &[HoldingSnapshot]) -> Vec<u8> {
    csv(
        &HOLDING_COLUMNS,
        holdings
            .iter()
            .map(|h| {
                vec![
                    Some(date(h.rebalance_date)),
                    Some(date(h.executed_on)),
                    Some(h.symbol.clone()),
                    Some(h.name.clone()),
                    h.rank.map(|r| r.to_string()),
                    Some(dec(h.target_weight)),
                    Some(h.quantity.to_string()),
                    Some(dec(h.price)),
                    Some(dec(h.value)),
                    Some(dec(h.actual_weight)),
                ]
            })
            .collect(),
    )
}

/// `f"{x:.10f}"`.
fn fixed10(x: f64) -> String {
    if x.is_nan() {
        "nan".to_owned()
    } else if x.is_infinite() {
        if x > 0.0 { "inf" } else { "-inf" }.to_owned()
    } else {
        format!("{x:.10}")
    }
}

/// `equity_csv(result)`: one row per day, the drawdown at ten places.
///
/// # Errors
/// `zip(strict=True)` over series of different lengths (`ValueError`).
pub(super) fn equity_csv(result: &BacktestResult) -> Result<Vec<u8>, Stop> {
    let drawdowns =
        bm::drawdown_series(&result.dates, &result.equity).map_err(|e| core_stop(&e))?;
    let n = result.dates.len();
    let lengths = [
        result.equity.len(),
        result.cash.len(),
        result.invested.len(),
        result.benchmark.len(),
        drawdowns.len(),
    ];
    if lengths.iter().any(|l| *l != n) {
        return Err(Stop::caught(
            "ValueError",
            "zip() argument 2 is shorter than argument 1",
        ));
    }
    let mut rows = Vec::with_capacity(n);
    for i in 0..n {
        rows.push(vec![
            result.dates.get(i).map(|d| date(*d)),
            result.equity.get(i).map(|v| dec(*v)),
            result.cash.get(i).map(|v| dec(*v)),
            result.invested.get(i).map(|v| dec(*v)),
            drawdowns.get(i).map(|p| fixed10(p.drawdown)),
            result.benchmark.get(i).copied().flatten().map(dec),
        ]);
    }
    Ok(csv(&EQUITY_COLUMNS, rows))
}

/// `artefact_bytes(result)`, in `ARTEFACTS` order.
pub(super) fn artefact_bytes(
    result: &BacktestResult,
) -> Result<Vec<(&'static str, Vec<u8>)>, Stop> {
    let [trades, holdings, equity] = ARTEFACTS;
    Ok(vec![
        (trades, trades_csv(&result.trades)),
        (holdings, holdings_csv(&result.holdings)),
        (equity, equity_csv(result)?),
    ])
}

// ---------------------------------------------------------------------------------------------
// Metrics over the engine's result
// ---------------------------------------------------------------------------------------------

/// The engine's result as `backtest_metrics` reads it (R2.3: the metrics port takes its own
/// subset of the result).
fn metrics_input(result: &BacktestResult) -> MetricsInput {
    MetricsInput {
        config: MetricsConfig {
            initial_capital: result.config.initial_capital,
            risk_free_rate: result.config.risk_free_rate,
            risk_free_curve: result.config.risk_free_curve.clone(),
        },
        dates: result.dates.clone(),
        equity: result.equity.clone(),
        invested: result.invested.clone(),
        benchmark: result.benchmark.clone(),
        trades: result
            .trades
            .iter()
            .map(|t| bm::Trade {
                date: t.date,
                instrument_id: t.instrument_id,
                symbol: t.symbol.clone(),
                side: match t.side {
                    TradeSide::Buy => bm::TradeSide::Buy,
                    TradeSide::Sell => bm::TradeSide::Sell,
                },
                quantity: t.quantity,
                price: t.price,
                notional: t.notional,
                cost: t.cost,
                reason: match t.reason {
                    TradeReason::Enter => bm::TradeReason::Enter,
                    TradeReason::Exit => bm::TradeReason::Exit,
                    TradeReason::Rebalance => bm::TradeReason::Rebalance,
                    TradeReason::Delist => bm::TradeReason::Delist,
                },
                realised_pnl: t.realised_pnl,
            })
            .collect(),
        delisting_count: result.delistings.len(),
        total_costs: result.total_costs,
        dividends_credited: result.dividends_credited,
    }
}

/// `compute_metrics(result)`.
pub(super) fn compute_metrics(result: &BacktestResult) -> Result<Metrics, Stop> {
    bm::compute_metrics(&metrics_input(result)).map_err(|e| core_stop(&e))
}

/// A core `Json` value as the stored `jsonb` takes it.
pub(super) fn ordered(value: &Json) -> OrderedJson {
    match value {
        Json::Null => OrderedJson::Null,
        Json::Bool(b) => OrderedJson::Bool(*b),
        Json::Int(i) => OrderedJson::Int(*i),
        Json::Float(f) => OrderedJson::Float(*f),
        Json::Str(s) => OrderedJson::Str(s.clone()),
        Json::Array(items) => OrderedJson::Array(items.iter().map(ordered).collect()),
        Json::Object(map) => {
            OrderedJson::Object(map.iter().map(|(k, v)| (k.clone(), ordered(v))).collect())
        }
    }
}

fn get(dict: &Json, key: &str) -> Json {
    match dict {
        Json::Object(map) => map.get(key).cloned().unwrap_or(Json::Null),
        _ => Json::Null,
    }
}

/// Python's `round(x, ndigits)` on a float (correctly rounded, half-even on the exact value).
fn py_round(x: f64, ndigits: usize) -> f64 {
    if !x.is_finite() {
        return x;
    }
    format!("{x:.ndigits$}").parse::<f64>().unwrap_or(x)
}

/// `float(Decimal)`.
fn float_of_wide(value: &WideDecimal) -> f64 {
    value.to_sci_string().parse::<f64>().unwrap_or(f64::NAN)
}

/// `fragility_payload(report)`: the base run and each perturbation, with `blind_pct` (M43).
fn fragility_payload(report: Option<&FragilityReport>) -> Result<Vec<OrderedJson>, Stop> {
    let Some(report) = report else {
        return Ok(Vec::new());
    };
    let mut rows = Vec::new();
    for run in std::iter::once(&report.base).chain(report.variants.iter()) {
        let metrics = compute_metrics(&run.result)?.as_dict();
        let blind = float_of_wide(&run.result.blind_fraction()?) * 100.0;
        rows.push(OrderedJson::Object(vec![
            ("label".to_owned(), OrderedJson::Str(run.label.clone())),
            (
                "description".to_owned(),
                OrderedJson::Str(run.description.clone()),
            ),
            ("cagr".to_owned(), ordered(&get(&metrics, "cagr"))),
            (
                "total_return".to_owned(),
                ordered(&get(&metrics, "total_return")),
            ),
            (
                "max_drawdown".to_owned(),
                ordered(&get(&metrics, "max_drawdown")),
            ),
            (
                "final_equity".to_owned(),
                OrderedJson::Str(dec(run.result.final_equity())),
            ),
            ("trades".to_owned(), count(run.result.trades.len())),
            ("blind_pct".to_owned(), OrderedJson::Float(blind)),
            (
                "rebalances".to_owned(),
                count(run.result.rebalance_dates.len()),
            ),
        ]));
    }
    Ok(rows)
}

fn count(n: usize) -> OrderedJson {
    OrderedJson::Int(i64::try_from(n).unwrap_or(i64::MAX))
}

/// `_sharpe_assumption(config)`.
fn sharpe_assumption(config: &BacktestConfig) -> String {
    match (
        config.risk_free_curve.first(),
        config.risk_free_curve.last(),
    ) {
        (Some((first, _)), Some((last, _))) => format!(
            "Sharpe and Sortino subtract the OECD MEI India 3-month short rate (IR3TIB), \
             forward-filled from monthly observations {}-{}. That series is a published proxy \
             for the 91-day T-bill, not the RBI auction cutoff.",
            date(*first),
            date(*last)
        ),
        _ => "Sharpe is excess over zero: no overlapping T-bill observation was attached to this \
              run."
            .to_owned(),
    }
}

/// `assumptions(config, result)`: docs/10's assumptions panel, in prose.
pub(super) fn assumptions(config: &BacktestConfig, result: &BacktestResult) -> Vec<String> {
    let costs = &config.costs;
    let cash_earns = if config.cash_policy == CashPolicy::Benchmark {
        "the benchmark's return."
    } else {
        "nothing."
    };
    let mut lines = vec![
        "Every order is decided on the rebalance date's close and filled at the NEXT trading \
         day's opening price. Nothing is ever filled at the price that triggered it."
            .to_owned(),
        format!(
            "Costs of {} bps ({} brokerage + {} STT + {} slippage) are charged on the traded \
             notional of every fill, on both the buy and the sell leg. The impact model is flat, \
             not size-dependent.",
            costs.total_bps().to_sci_string(),
            dec(costs.brokerage_bps),
            dec(costs.stt_bps),
            dec(costs.slippage_bps)
        ),
        format!(
            "Share counts are whole. What rounding leaves behind stays in cash and earns {cash_earns}"
        ),
        format!(
            "Prices are adjusted for splits and bonuses. They are NOT adjusted for cash \
             dividends, so every return here is a PRICE return and is lower than a total return \
             by roughly the dividend yield. The dividend policy is '{}'.",
            config.dividends.value()
        ),
        "Index membership before the first NSE constituent file NSE publishes is RECONSTRUCTED \
         from the earliest file available (index_member_daily.source). Any part of this run \
         that falls in that period rests on membership we inferred, not membership we observed."
            .to_owned(),
        "A delisted holding is sold at its last available close and the loss is taken. Nothing \
         is forward-filled and nothing is dropped."
            .to_owned(),
        sharpe_assumption(config),
        "Past backtest results do not predict future results.".to_owned(),
    ];
    if config.risk_overlay.enabled {
        lines.insert(
            3,
            "The index_above_200dma risk overlay is on: the book goes to cash whenever the \
             benchmark closes below its 200-day moving average."
                .to_owned(),
        );
    }
    lines.extend(result.notes.iter().cloned());
    lines
}

/// `json.loads(config.model_dump_json())` — what `metrics_hash` hashes beside the metrics.
pub(super) fn config_json(config: &BacktestConfig) -> Json {
    fn s(text: impl Into<String>) -> Json {
        Json::Str(text.into())
    }
    fn d(value: Decimal) -> Json {
        Json::Str(dec(value))
    }
    fn enum_value<T: serde::Serialize>(value: &T) -> Json {
        match serde_json::to_value(value) {
            Ok(serde_json::Value::String(text)) => Json::Str(text),
            _ => Json::Null,
        }
    }
    fn obj<const N: usize>(entries: [(&str, Json); N]) -> Json {
        Json::Object(
            entries
                .into_iter()
                .map(|(k, v)| (k.to_owned(), v))
                .collect(),
        )
    }
    fn value(v: &ConfigValue) -> Json {
        match v {
            ConfigValue::Null => Json::Null,
            ConfigValue::Bool(b) => Json::Bool(*b),
            ConfigValue::Int(i) => Json::Int(*i),
            ConfigValue::Float(f) => Json::Float(*f),
            ConfigValue::Str(t) => Json::Str(t.clone()),
            ConfigValue::List(items) => Json::Array(items.iter().map(value).collect()),
            ConfigValue::Map(map) => {
                Json::Object(map.iter().map(|(k, x)| (k.clone(), value(x))).collect())
            }
        }
    }
    let definition = config.screen_definition.as_ref().map_or(Json::Null, |m| {
        Json::Object(
            m.iter()
                .map(|(k, v)| (k.clone(), value(v)))
                .collect::<BTreeMap<_, _>>(),
        )
    });
    obj([
        (
            "screen_public_id",
            config
                .screen_public_id
                .clone()
                .map_or(Json::Null, Json::Str),
        ),
        ("screen_definition", definition),
        ("start", s(date(config.start))),
        ("end", s(date(config.end))),
        ("initial_capital", d(config.initial_capital)),
        (
            "rebalance",
            obj([
                ("frequency", enum_value(&config.rebalance.frequency)),
                ("day", enum_value(&config.rebalance.day)),
            ]),
        ),
        (
            "selection",
            obj([
                ("top_n", Json::Int(config.selection.top_n)),
                ("hold_buffer", Json::Int(config.selection.hold_buffer)),
            ]),
        ),
        ("weighting", enum_value(&config.weighting)),
        (
            "position_limits",
            obj([
                ("max_weight", d(config.position_limits.max_weight)),
                ("min_weight", d(config.position_limits.min_weight)),
            ]),
        ),
        (
            "costs",
            obj([
                ("brokerage_bps", d(config.costs.brokerage_bps)),
                ("stt_bps", d(config.costs.stt_bps)),
                ("slippage_bps", d(config.costs.slippage_bps)),
                ("impact_model", enum_value(&config.costs.impact_model)),
            ]),
        ),
        ("cash_policy", enum_value(&config.cash_policy)),
        ("benchmark", s(config.benchmark.clone())),
        (
            "risk_overlay",
            obj([
                ("enabled", Json::Bool(config.risk_overlay.enabled)),
                ("rule", enum_value(&config.risk_overlay.rule)),
            ]),
        ),
        ("dividends", s(config.dividends.value())),
        ("risk_free_rate", d(config.risk_free_rate)),
        (
            "risk_free_curve",
            Json::Array(
                config
                    .risk_free_curve
                    .iter()
                    .map(|(day, rate)| Json::Array(vec![s(date(*day)), d(*rate)]))
                    .collect(),
            ),
        ),
    ])
}

/// `StoredResult`: exactly what goes into the `backtest` row.
pub(super) struct StoredResult {
    pub(super) metrics: OrderedJson,
    pub(super) equity_curve: OrderedJson,
    pub(super) trades_key: String,
    pub(super) metrics_hash: String,
}

/// `build_payload(public_id, config, result, fragility, extra_notes=...)`.
pub(super) fn build_payload(
    public_id: &str,
    config: &BacktestConfig,
    result: &BacktestResult,
    fragility: Option<&FragilityReport>,
    extra_notes: &[String],
) -> Result<StoredResult, Stop> {
    let metrics = compute_metrics(result)?;
    let hash = bm::metrics_hash(&metrics, &config_json(config), result.data_version);
    let Json::Object(dict) = metrics.as_dict() else {
        return Err(Stop::caught("ValueError", "metrics are not a mapping"));
    };
    let mut payload: Vec<(String, OrderedJson)> =
        dict.iter().map(|(k, v)| (k.clone(), ordered(v))).collect();
    payload.push(("metrics_hash".to_owned(), OrderedJson::Str(hash.clone())));
    payload.push((
        "data_version".to_owned(),
        result
            .data_version
            .map_or(OrderedJson::Null, OrderedJson::Int),
    ));
    payload.push((
        "rebalance_count".to_owned(),
        count(result.rebalance_dates.len()),
    ));
    payload.push((
        "fragility".to_owned(),
        OrderedJson::Array(fragility_payload(fragility)?),
    ));
    let mut panel: Vec<String> = Vec::new();
    for line in assumptions(config, result)
        .into_iter()
        .chain(extra_notes.iter().cloned())
    {
        if !panel.contains(&line) {
            panel.push(line);
        }
    }
    payload.push((
        "assumptions".to_owned(),
        OrderedJson::Array(panel.into_iter().map(OrderedJson::Str).collect()),
    ));
    let months = bm::monthly_returns(&result.dates, &result.equity).map_err(|e| core_stop(&e))?;
    payload.push((
        "monthly_returns".to_owned(),
        OrderedJson::Array(
            months
                .iter()
                .map(|m| {
                    OrderedJson::Object(vec![
                        ("month".to_owned(), OrderedJson::Str(m.key())),
                        ("return".to_owned(), OrderedJson::Float(py_round(m.ret, 10))),
                    ])
                })
                .collect(),
        ),
    ));
    let points = bm::downsample_curve(
        &result.dates,
        &result.equity,
        &result.benchmark,
        EQUITY_CURVE_POINTS,
    )
    .map_err(|e| core_stop(&e))?;
    let drawdowns =
        bm::drawdown_series(&result.dates, &result.equity).map_err(|e| core_stop(&e))?;
    let stride = (drawdowns.len() / DRAWDOWN_POINTS).max(1);
    let mut kept: Vec<usize> = (0..drawdowns.len()).step_by(stride).collect();
    if let Some(last) = drawdowns.len().checked_sub(1)
        && kept.last() != Some(&last)
    {
        kept.push(last);
    }
    let curve = OrderedJson::Object(vec![
        (
            "points".to_owned(),
            OrderedJson::Array(
                points
                    .iter()
                    .map(|p| {
                        OrderedJson::Object(vec![
                            ("date".to_owned(), OrderedJson::Str(date(p.date))),
                            ("equity".to_owned(), OrderedJson::Str(dec(p.equity))),
                            (
                                "benchmark".to_owned(),
                                p.benchmark
                                    .map_or(OrderedJson::Null, |b| OrderedJson::Str(dec(b))),
                            ),
                        ])
                    })
                    .collect(),
            ),
        ),
        (
            "drawdown".to_owned(),
            OrderedJson::Array(
                kept.iter()
                    .filter_map(|i| drawdowns.get(*i))
                    .map(|p| {
                        OrderedJson::Object(vec![
                            ("date".to_owned(), OrderedJson::Str(date(p.date))),
                            (
                                "drawdown".to_owned(),
                                OrderedJson::Float(py_round(p.drawdown, 10)),
                            ),
                        ])
                    })
                    .collect(),
            ),
        ),
    ]);
    Ok(StoredResult {
        metrics: OrderedJson::Object(payload),
        equity_curve: curve,
        trades_key: artefact_key(public_id, "trades"),
        metrics_hash: hash,
    })
}

/// `progress_frame(public_id, status, stage=, completed=, total=, as_of=, detail=)`:
/// `json.dumps(body, separators=(",", ":"), sort_keys=True)`.
pub(super) fn progress_frame(
    public_id: &str,
    status: &str,
    stage: &str,
    completed: usize,
    total: usize,
    as_of: Option<NaiveDate>,
    detail: Option<&str>,
) -> String {
    // `0 if total <= 0 else min(100, round(100 * completed / total))`: half-even on the exact
    // quotient.
    let scaled = 100 * completed;
    let percent = match (scaled.checked_div(total), scaled.checked_rem(total)) {
        (Some(q), Some(r)) => {
            let rounded = match (2 * r).cmp(&total) {
                std::cmp::Ordering::Less => q,
                std::cmp::Ordering::Greater => q + 1,
                std::cmp::Ordering::Equal => q + (q % 2),
            };
            rounded.min(100)
        }
        _ => 0,
    };
    let int = |n: usize| Json::Int(i64::try_from(n).unwrap_or(i64::MAX));
    let mut body = BTreeMap::new();
    body.insert("public_id".to_owned(), Json::Str(public_id.to_owned()));
    body.insert("status".to_owned(), Json::Str(status.to_owned()));
    body.insert("stage".to_owned(), Json::Str(stage.to_owned()));
    body.insert("completed".to_owned(), int(completed));
    body.insert("total".to_owned(), int(total));
    body.insert("percent".to_owned(), int(percent));
    body.insert(
        "as_of".to_owned(),
        as_of.map_or(Json::Null, |d| Json::Str(date(d))),
    );
    body.insert(
        "detail".to_owned(),
        detail.map_or(Json::Null, |t| Json::Str(t.to_owned())),
    );
    Json::Object(body).to_canonical()
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::panic)]

    use super::*;
    use baskfy_core::backtest::engine::{FragilityRun, SelectionSpec};

    fn d(y: i32, m: u32, day: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, day).unwrap()
    }

    fn config() -> BacktestConfig {
        serde_json::from_str(
            r#"{"start": "2024-01-01", "end": "2024-12-31", "screen_public_id": "abc",
                "costs": {"brokerage_bps": "3", "stt_bps": "10", "slippage_bps": "15"}}"#,
        )
        .unwrap()
    }

    fn result(config: &BacktestConfig) -> BacktestResult {
        BacktestResult {
            config: config.clone(),
            dates: vec![d(2024, 1, 1), d(2024, 1, 2), d(2024, 2, 1)],
            equity: vec![
                Decimal::new(100_000_000, 2),
                Decimal::new(99_000_000, 2),
                Decimal::new(101_000_000, 2),
            ],
            cash: vec![Decimal::new(100_000_000, 2), Decimal::ZERO, Decimal::ZERO],
            invested: vec![
                Decimal::ZERO,
                Decimal::new(99_000_000, 2),
                Decimal::new(101_000_000, 2),
            ],
            benchmark: vec![
                Some(Decimal::new(250_005, 1)),
                None,
                Some(Decimal::new(25_100, 0)),
            ],
            trades: vec![Trade {
                date: d(2024, 1, 2),
                instrument_id: 7,
                symbol: "A,B\"C".to_owned(),
                side: TradeSide::Buy,
                quantity: 10,
                price: Decimal::new(1_234_500, 4),
                notional: Decimal::new(123_450, 2),
                cost: Decimal::new(346, 2),
                reason: TradeReason::Enter,
                realised_pnl: None,
            }],
            holdings: vec![HoldingSnapshot {
                rebalance_date: d(2024, 1, 1),
                executed_on: d(2024, 1, 2),
                instrument_id: 7,
                symbol: "AB".to_owned(),
                name: "A B Ltd".to_owned(),
                rank: None,
                target_weight: Decimal::new(50_000, 6),
                quantity: 10,
                price: Decimal::new(1_234_500, 4),
                value: Decimal::new(123_450, 2),
                actual_weight: Decimal::new(1_234, 6),
            }],
            delistings: vec![],
            total_costs: Decimal::new(346, 2),
            dividends_credited: Decimal::ZERO,
            rebalance_dates: vec![d(2024, 1, 1), d(2024, 2, 1)],
            blind_rebalances: vec![d(2024, 2, 1)],
            adjustment_coverage: None,
            unexecuted_fills: vec![],
            notes: vec!["a note".to_owned()],
            data_version: Some(44),
        }
    }

    #[test]
    fn the_trade_log_has_docs_10s_columns_and_quotes_what_it_must() {
        let text = String::from_utf8(trades_csv(&result(&config()).trades)).unwrap();
        assert_eq!(
            text,
            "date,symbol,side,quantity,price,notional,cost,reason,realised_pnl\n\
             2024-01-02,\"A,B\"\"C\",buy,10,123.4500,1234.50,3.46,enter,\n"
        );
    }

    #[test]
    fn holdings_and_equity_print_decimals_as_python_does() {
        let r = result(&config());
        let holdings = String::from_utf8(holdings_csv(&r.holdings)).unwrap();
        assert_eq!(
            holdings,
            "rebalance_date,executed_on,symbol,name,rank,target_weight,quantity,price,value,\
             actual_weight\n2024-01-01,2024-01-02,AB,A B Ltd,,0.050000,10,123.4500,1234.50,\
             0.001234\n"
        );
        let equity = String::from_utf8(equity_csv(&r).unwrap()).unwrap();
        assert_eq!(
            equity,
            "date,equity,cash,invested,drawdown,benchmark\n\
             2024-01-01,1000000.00,1000000.00,0,0.0000000000,25000.5\n\
             2024-01-02,990000.00,0,990000.00,-0.0100000000,\n\
             2024-02-01,1010000.00,0,1010000.00,0.0000000000,25100\n"
        );
    }

    #[test]
    fn the_frame_is_sorted_compact_json() {
        assert_eq!(
            progress_frame(
                "ab",
                "running",
                "screening",
                1,
                3,
                Some(d(2024, 1, 2)),
                None
            ),
            "{\"as_of\":\"2024-01-02\",\"completed\":1,\"detail\":null,\"percent\":33,\
             \"public_id\":\"ab\",\"stage\":\"screening\",\"status\":\"running\",\"total\":3}"
        );
        assert_eq!(
            progress_frame("ab", "failed", "failed", 0, 1, None, Some("é")),
            "{\"as_of\":null,\"completed\":0,\"detail\":\"\\u00e9\",\"percent\":0,\
             \"public_id\":\"ab\",\"stage\":\"failed\",\"status\":\"failed\",\"total\":1}"
        );
        // round(100 * 1 / 8) = round(12.5) = 12 (half-even).
        assert!(progress_frame("a", "running", "s", 1, 8, None, None).contains("\"percent\":12"));
        assert!(progress_frame("a", "running", "s", 0, 0, None, None).contains("\"percent\":0"));
    }

    #[test]
    fn the_assumptions_name_costs_dividends_and_the_rate_and_end_with_the_runs_notes() {
        let c = config();
        let lines = assumptions(&c, &result(&c));
        assert!(lines[1].starts_with("Costs of 28 bps (3 brokerage + 10 STT + 15 slippage)"));
        assert!(lines[3].ends_with("The dividend policy is 'ignore'."));
        assert_eq!(
            lines[6],
            "Sharpe is excess over zero: no overlapping T-bill observation was attached to this run."
        );
        assert_eq!(lines.last().map(String::as_str), Some("a note"));
        let mut overlay = c.clone();
        overlay.risk_overlay.enabled = true;
        assert!(assumptions(&overlay, &result(&overlay))[3].starts_with("The index_above_200dma"));
    }

    #[test]
    fn the_payload_dedupes_notes_and_hashes_the_unattached_config() {
        let c = config();
        let r = result(&c);
        let stored =
            build_payload("p", &c, &r, None, &["a note".to_owned(), "x".to_owned()]).unwrap();
        let assumptions = stored.metrics.get("assumptions").unwrap();
        let OrderedJson::Array(items) = assumptions else {
            panic!()
        };
        assert_eq!(
            items
                .iter()
                .filter(|i| **i == OrderedJson::Str("a note".into()))
                .count(),
            1
        );
        assert_eq!(items.last(), Some(&OrderedJson::Str("x".into())));
        assert_eq!(stored.trades_key, "backtests/p/trades.csv");
        let again = build_payload("q", &c, &r, None, &[]).unwrap();
        assert_eq!(stored.metrics_hash, again.metrics_hash);
        assert_eq!(
            stored.metrics.get("metrics_hash"),
            Some(&OrderedJson::Str(stored.metrics_hash.clone()))
        );
        assert_eq!(
            stored.metrics.get("rebalance_count"),
            Some(&OrderedJson::Int(2))
        );
    }

    #[test]
    fn the_fragility_rows_carry_blind_pct() {
        let c = config();
        let r = result(&c);
        let run = |label: &str| FragilityRun {
            label: label.to_owned(),
            description: "d".to_owned(),
            result: r.clone(),
        };
        let report = FragilityReport {
            base: run("base"),
            variants: vec![run("costs_minus_25pct")],
        };
        let rows = fragility_payload(Some(&report)).unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].get("blind_pct"), Some(&OrderedJson::Float(50.0)));
        assert_eq!(
            rows[0].get("final_equity"),
            Some(&OrderedJson::Str("1010000.00".into()))
        );
        assert_eq!(
            rows[1].get("label"),
            Some(&OrderedJson::Str("costs_minus_25pct".into()))
        );
    }

    #[test]
    fn the_config_dump_is_pydantics_json_mode() {
        let mut c = config();
        c.selection = SelectionSpec {
            top_n: 5,
            hold_buffer: 0,
        };
        c.risk_free_curve = vec![(d(2024, 1, 1), Decimal::new(653_400, 7))];
        let text = config_json(&c).to_canonical();
        assert_eq!(
            text,
            "{\"benchmark\":\"nifty-500\",\"cash_policy\":\"hold_cash\",\"costs\":{\"brokerage_bps\":\"3\",\
             \"impact_model\":\"fixed\",\"slippage_bps\":\"15\",\"stt_bps\":\"10\"},\"dividends\":\"ignore\",\
             \"end\":\"2024-12-31\",\"initial_capital\":\"1000000\",\"position_limits\":{\"max_weight\":\"0.10\",\
             \"min_weight\":\"0.01\"},\"rebalance\":{\"day\":\"last_trading_day\",\"frequency\":\"monthly\"},\
             \"risk_free_curve\":[[\"2024-01-01\",\"0.0653400\"]],\"risk_free_rate\":\"0\",\"risk_overlay\":\
             {\"enabled\":false,\"rule\":\"index_above_200dma\"},\"screen_definition\":null,\
             \"screen_public_id\":\"abc\",\"selection\":{\"hold_buffer\":0,\"top_n\":5},\"start\":\
             \"2024-01-01\",\"weighting\":\"equal\"}"
        );
    }
}
