//! Port of `baskfy_api.opportunities_views`: JSON shaping (`canonical_json` spelling: decimals
//! unquoted at their own scale).

use baskfy_core::opportunities::config::{DataStatus, QuoteRules, TriggerStatus};
use baskfy_core::opportunities::options::{MODEL_NOTE, OptionCandidate};
use baskfy_core::opportunities::ranking::ScoreBreakdown;
use baskfy_core::opportunities::swing::SwingOpportunity;
use baskfy_core::opportunities::validation::ValidationResult;
use chrono::{DateTime, NaiveDate, Utc};
use rust_decimal::Decimal;

use crate::problems::{Problem, internal_error};
use crate::routes::sleeves::common::{J, Obj, quantize_j};
use crate::wire;

use super::service::{MAX_RESULTS, MAX_UNIVERSE, OptionsAnalysis, ProviderState, SwingScan};

/// Python's `round(x, ndigits)` on a float.
pub(crate) fn py_round(x: f64, ndigits: usize) -> f64 {
    if !x.is_finite() {
        return x;
    }
    format!("{x:.ndigits$}").parse::<f64>().unwrap_or(x)
}

fn round_j(x: Option<f64>, ndigits: usize) -> J {
    x.map_or(J::Null, |v| J::Float(py_round(v, ndigits)))
}

/// `iso(value)`: UTC with a `Z`.
pub(crate) fn iso(t: DateTime<Utc>) -> J {
    J::Str(wire::py_datetime(t))
}

fn strs(items: &[String]) -> J {
    J::Arr(items.iter().map(|s| J::Str(s.clone())).collect())
}

fn score_json(score: &ScoreBreakdown) -> J {
    Obj::new()
        .put("total", J::Float(score.total))
        .put(
            "components",
            J::Arr(
                score
                    .components
                    .iter()
                    .map(|c| {
                        Obj::new()
                            .put("key", J::s(&c.key))
                            .put("label", J::s(&c.label))
                            .put("value", J::Float(c.value))
                            .put("weight", J::Float(py_round(c.weight, 4)))
                            .put("contribution", J::Float(c.contribution()))
                            .done()
                    })
                    .collect(),
            ),
        )
        .done()
}

fn opportunity_json(
    opp: &SwingOpportunity,
    scanned_at: DateTime<Utc>,
    status: DataStatus,
    benchmark: &str,
) -> J {
    let ev = &opp.evaluation;
    let triggered = ev.status == TriggerStatus::Triggered;
    Obj::new()
        .put("id", J::Str(opp.id()))
        .put("instrument_id", J::Int(opp.instrument_id))
        .put("symbol", J::s(&opp.symbol))
        .put("name", J::s(&opp.name))
        .put("exchange", J::s("NSE"))
        .put("sector", J::opt_str(opp.sector.clone()))
        .put("setup", J::s(ev.setup.value()))
        .put("status", J::s(ev.status.value()))
        .put(
            "actionable",
            J::Bool(opp.actionable && status == DataStatus::Fresh),
        )
        .put("close", J::Dec(opp.close))
        .put(
            "evidence",
            J::Arr(
                ev.evidence
                    .iter()
                    .map(|e| {
                        Obj::new()
                            .put("key", J::s(&e.key))
                            .put("label", J::s(&e.label))
                            .put("value", round_j(e.value, 2))
                            .put("threshold", round_j(e.threshold, 2))
                            .put("unit", J::s(&e.unit))
                            .put("passed", J::Bool(e.passed))
                            .done()
                    })
                    .collect(),
            ),
        )
        .put(
            "trigger",
            Obj::new()
                .put("condition", J::s(&ev.trigger_condition))
                .put("level", J::Dec(opp.trigger_level))
                .put("status", J::s(ev.status.value()))
                .put(
                    "triggered_on",
                    if triggered { J::Date(opp.data_as_of) } else { J::Null },
                )
                .done(),
        )
        .put(
            "entry",
            Obj::new()
                .put("basis", J::s(&ev.entry_basis))
                .put("price", J::Dec(opp.entry))
                .done(),
        )
        .put("invalidation", J::Dec(opp.invalidation))
        .put(
            "targets",
            J::Arr(
                opp.targets
                    .iter()
                    .map(|(label, price, r)| {
                        Obj::new()
                            .put("label", J::s(label))
                            .put("price", J::Dec(*price))
                            .put("r_multiple", J::Dec(*r))
                            .done()
                    })
                    .collect(),
            ),
        )
        .put("risk_per_share", J::Dec(opp.risk_per_share))
        .put("reward_per_share", J::Dec(opp.reward_per_share))
        .put("risk_reward", J::Dec(opp.risk_reward))
        .put(
            "horizon",
            Obj::new()
                .put("min_sessions", J::Int(ev.horizon.0))
                .put("max_sessions", J::Int(ev.horizon.1))
                .done(),
        )
        .put(
            "liquidity",
            Obj::new()
                .put("avg_turnover_cr_20d", J::Dec(opp.avg_turnover_cr))
                .put("avg_volume_20d", J::Int(opp.avg_volume))
                .done(),
        )
        .put(
            "volatility",
            Obj::new()
                .put("atr_14", J::Dec(opp.atr))
                .put("atr_pct", J::Dec(opp.atr_pct))
                .put("hv_20_pct", J::opt_dec(opp.hv_pct))
                .done(),
        )
        .put(
            "relative_strength",
            Obj::new()
                .put("benchmark", J::s(benchmark))
                .put("excess_return_pct", J::opt_dec(opp.rs_excess_pct))
                .put("lookback", J::Int(opp.rs_lookback))
                .done(),
        )
        .put("score", score_json(&opp.score))
        .put("data_as_of", J::Date(opp.data_as_of))
        .put("scanned_at", iso(scanned_at))
        .put("limitations", strs(&opp.limitations))
        .done()
}

/// `scan_json(scan)`.
pub(crate) fn scan_json(scan: &SwingScan) -> J {
    let fresh = &scan.freshness;
    let c = scan.result.counts;
    Obj::new()
        .put("scan_id", J::s(&scan.scan_id))
        .put("scanned_at", iso(scan.scanned_at))
        .put("data_as_of", J::opt_date(fresh.data_as_of))
        .put("data_status", J::s(fresh.status.value()))
        .put("stale_reason", J::opt_str(fresh.reason.clone()))
        .put("universe", J::s(&scan.universe))
        .put(
            "counts",
            Obj::new()
                .put("universe", J::Int(c.universe))
                .put("evaluated", J::Int(c.evaluated))
                .put("insufficient_history", J::Int(c.insufficient_history))
                .put("qualified", J::Int(c.qualified))
                .put("pending", J::Int(c.pending))
                .put("triggered", J::Int(c.triggered))
                .done(),
        )
        .put("warnings", strs(&scan.warnings))
        .put(
            "opportunities",
            J::Arr(
                scan.result
                    .opportunities
                    .iter()
                    .map(|o| opportunity_json(o, scan.scanned_at, fresh.status, "nifty-500"))
                    .collect(),
            ),
        )
        .done()
}

/// `provider_json(state)`.
pub(crate) fn provider_json(state: &ProviderState) -> J {
    Obj::new()
        .put("available", J::Bool(state.available))
        .put("reason", J::opt_str(state.reason.clone()))
        .put("dependency", J::opt_str(state.dependency.clone()))
        .done()
}

fn q2(d: Decimal) -> J {
    quantize_j(d, 2)
}

fn candidate_json(c: &OptionCandidate, status: DataStatus) -> Result<J, Problem> {
    let fail = |e: baskfy_core::error::CoreError| {
        crate::logging::error(
            "baskfy_api.opportunities",
            "candidate did not encode",
            &[("error", &e.to_string())],
        );
        internal_error()
    };
    let legs = c
        .legs
        .iter()
        .map(|leg| {
            let contract = &leg.quote.contract;
            Obj::new()
                .put("tradingsymbol", J::s(&contract.tradingsymbol))
                .put("strike", J::Dec(leg.strike()))
                .put(
                    "option_type",
                    J::s(match leg.option_type() {
                        baskfy_core::options::config::OptionType::Ce => "CE",
                        baskfy_core::options::config::OptionType::Pe => "PE",
                    }),
                )
                .put(
                    "side",
                    J::s(match leg.side {
                        baskfy_core::options::config::Side::Buy => "BUY",
                        baskfy_core::options::config::Side::Sell => "SELL",
                    }),
                )
                .put("bid", J::Dec(leg.quote.bid))
                .put("ask", J::Dec(leg.quote.ask))
                .put("exec_price", J::Dec(leg.exec_price()))
                .put("quote_age_s", J::Int(leg.quote.quote_age_s))
                .put("spread_pct", J::Dec(leg.quote.spread_pct))
                .put("volume", J::Int(contract.volume.unwrap_or(0)))
                .put("oi", J::Int(contract.oi.unwrap_or(0)))
                .put("iv", leg.iv.map_or(J::Null, |iv| J::Float(py_round(iv * 100.0, 2))))
                .put("delta", round_j(leg.delta, 4))
                .put(
                    "greeks_source",
                    if leg.iv.is_some() { J::s("MODEL_BLACK76") } else { J::Null },
                )
                .done()
        })
        .collect();
    Ok(Obj::new()
        .put("id", J::Str(c.id()))
        .put("strategy", J::s(c.strategy.value()))
        .put("expiry", J::Date(c.expiry))
        .put("dte", J::Int(c.dte))
        .put("lot_size", J::Int(c.lot_size))
        .put("lots", J::Int(c.lots))
        .put("actionable", J::Bool(c.actionable && status == DataStatus::Fresh))
        .put("legs", J::Arr(legs))
        .put("net_debit_per_unit", q2(c.net_debit_per_unit))
        .put("net_debit_per_lot", q2(c.net_debit_per_lot().map_err(fail)?))
        .put("max_loss_per_unit", q2(c.max_loss_per_unit))
        .put("max_loss_per_lot", q2(c.max_loss_per_lot().map_err(fail)?))
        .put(
            "max_profit_per_unit",
            c.max_profit_per_unit.map_or(J::Null, q2),
        )
        .put(
            "max_profit_per_lot",
            c.max_profit_per_lot().map_err(fail)?.map_or(J::Null, q2),
        )
        .put("breakevens", J::Arr(c.breakevens.iter().map(|b| J::Dec(*b)).collect()))
        .put(
            "costs",
            Obj::new()
                .put("entry_inr", J::Dec(c.entry_cost_inr))
                .put("round_trip_inr", J::Dec(c.round_trip_cost_inr))
                .done(),
        )
        .put(
            "max_loss_after_costs_per_lot",
            q2(c.max_loss_after_costs_per_lot().map_err(fail)?),
        )
        .put(
            "payoff",
            J::Arr(
                c.payoff
                    .iter()
                    .map(|p| {
                        Obj::new()
                            .put("underlying", J::Dec(p.underlying))
                            .put("pnl_per_lot", J::Dec(p.pnl_per_lot))
                            .done()
                    })
                    .collect(),
            ),
        )
        .put("score", score_json(&c.score))
        .put("limitations", strs(&c.limitations))
        .done())
}

/// `options_json(symbol, analysis)`.
pub(crate) fn options_json(symbol: &str, a: &OptionsAnalysis) -> Result<J, Problem> {
    let (candidates, rejected, mut warnings) = match &a.result {
        None => (Vec::new(), Vec::new(), a.warnings.clone()),
        Some(r) => (
            r.candidates
                .iter()
                .map(|c| candidate_json(c, a.status))
                .collect::<Result<Vec<_>, _>>()?,
            r.rejected
                .iter()
                .map(|x| {
                    Obj::new()
                        .put("tradingsymbol", J::s(&x.tradingsymbol))
                        .put("reason", J::s(&x.reason))
                        .done()
                })
                .collect(),
            a.warnings.clone(),
        ),
    };
    if let Some(r) = &a.result {
        warnings.extend(r.warnings.iter().cloned());
    }
    Ok(Obj::new()
        .put("provider", provider_json(&a.provider))
        .put(
            "underlying",
            Obj::new()
                .put("symbol", J::Str(symbol.to_uppercase()))
                .put("spot", J::opt_dec(a.spot))
                .put(
                    "spot_as_of",
                    a.spot_as_of
                        .map_or(J::Null, |t| iso(t.with_timezone(&Utc))),
                )
                .done(),
        )
        .put("analyzed_at", iso(a.analyzed_at))
        .put("data_status", J::s(a.status.value()))
        .put("candidates", J::Arr(candidates))
        .put("rejected", J::Arr(rejected))
        .put("warnings", strs(&warnings))
        .put("model_note", J::s(MODEL_NOTE))
        .done())
}

fn period(start: NaiveDate, end: NaiveDate) -> J {
    Obj::new()
        .put("start", J::Date(start))
        .put("end", J::Date(end))
        .done()
}

/// `validation_json(result)`.
pub(crate) fn validation_json(r: &ValidationResult) -> J {
    let trades = r.trades.len();
    Obj::new()
        .put("available", J::Bool(true))
        .put(
            "reason",
            if trades > 0 {
                J::Null
            } else {
                J::s("No triggered signals in the period for this universe.")
            },
        )
        .put("setup", J::s(r.setup.value()))
        .put("period", period(r.start, r.end))
        .put("assumptions", strs(&r.assumptions))
        .put("trades", J::Int(i64::try_from(trades).unwrap_or(i64::MAX)))
        .put("wins", J::Int(r.wins()))
        .put("skipped_gap", J::Int(r.skipped_gap))
        .put("hit_rate_pct", round_j(r.hit_rate_pct(), 2))
        .put("avg_return_pct", round_j(r.avg_return_pct(), 3))
        .put("avg_r", round_j(r.avg_r(), 3))
        .put("expectancy_r", round_j(r.avg_r(), 3))
        .put("max_adverse_r", round_j(r.max_adverse_r(), 3))
        .put(
            "by_year",
            J::Arr(
                r.by_year()
                    .iter()
                    .map(|y| {
                        Obj::new()
                            .put("year", J::Int(i64::from(y.year)))
                            .put("trades", J::Int(y.trades))
                            .put("avg_r", round_j(y.avg_r, 3))
                            .done()
                    })
                    .collect(),
            ),
        )
        .done()
}

/// `unavailable_validation(setup, start, end, reason)`.
pub(crate) fn unavailable_validation(setup: &str, start: NaiveDate, end: NaiveDate, reason: &str) -> J {
    Obj::new()
        .put("available", J::Bool(false))
        .put("reason", J::s(reason))
        .put("setup", J::s(setup))
        .put("period", period(start, end))
        .put("assumptions", J::Arr(Vec::new()))
        .put("trades", J::Int(0))
        .put("wins", J::Int(0))
        .put("skipped_gap", J::Int(0))
        .put("hit_rate_pct", J::Null)
        .put("avg_return_pct", J::Null)
        .put("avg_r", J::Null)
        .put("expectancy_r", J::Null)
        .put("max_adverse_r", J::Null)
        .put("by_year", J::Arr(Vec::new()))
        .done()
}

/// `quote_rules_json(rules)`.
pub(crate) fn quote_rules_json(rules: &QuoteRules) -> J {
    Obj::new()
        .put("max_spread_pct", J::Dec(rules.max_spread_pct))
        .put("max_quote_age_s", J::Int(rules.max_quote_age_s))
        .put("min_oi", J::Int(rules.min_oi))
        .put("min_volume", J::Int(rules.min_volume))
        .done()
}

/// `LIMITS`.
pub(crate) fn limits() -> J {
    Obj::new()
        .put("max_results", J::Int(MAX_RESULTS))
        .put(
            "max_universe",
            J::Int(i64::try_from(MAX_UNIVERSE).unwrap_or(i64::MAX)),
        )
        .done()
}

/// `sse(event, data)`.
pub(crate) fn sse(event: &str, data: &J) -> String {
    format!("event: {event}\ndata: {}\n\n", data.canonical())
}

/// The `progress` frame of the scan stream.
pub(crate) fn progress_frame(stage: &str, done: i64, total: i64) -> String {
    sse(
        "progress",
        &Obj::new()
            .put("stage", J::s(stage))
            .put("done", J::Int(done))
            .put("total", J::Int(total))
            .done(),
    )
}
