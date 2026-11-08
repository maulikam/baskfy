//! Ports the parts of `baskfy_core.portfolio_nav` the read routes call: the chain-linked wealth
//! index (in Python's `localcontext(prec=60)`), daily moves, drawdown, the headline TWR /
//! since-grouped figures and the benchmark comparison.

use baskfy_core::decimal::Rounding;
use chrono::NaiveDate;

use super::ledger::{
    DomainResult, LedgerPortfolio, MetricKind, PortfolioSource, ReturnFigure, headline_kind, money,
};
use super::pydec::Dec;

/// `_WIDE_PRECISION`.
const WIDE_PRECISION: i64 = 60;

/// `_quantise_return`: six places, half-up.
pub(crate) fn quantise_return(value: &Dec) -> Dec {
    value
        .quantize(-6, Rounding::HalfUp)
        .unwrap_or_else(|| value.clone())
}

/// `RETURN_PRECISION` with the context's rounding (`.quantize(RETURN_PRECISION)`).
pub(crate) fn quantize_return_even(value: &Dec) -> Dec {
    value
        .quantize(-6, Rounding::HalfEven)
        .unwrap_or_else(|| value.clone())
}

/// `NavPoint`.
#[derive(Debug, Clone)]
pub(crate) struct NavPoint {
    pub(crate) on: NaiveDate,
    pub(crate) value: Dec,
    pub(crate) net_flow: Dec,
}

impl NavPoint {
    /// With `__post_init__`'s refusal of a negative value.
    pub(crate) fn new(on: NaiveDate, value: Dec, net_flow: Dec) -> DomainResult<Self> {
        if value < Dec::zero() {
            return Err(format!(
                "a portfolio's end-of-day value cannot be negative; got {value} on {on}"
            ));
        }
        Ok(Self {
            on,
            value,
            net_flow,
        })
    }
}

/// `DayPnl`.
#[derive(Debug, Clone)]
pub(crate) struct DayPnl {
    pub(crate) on: NaiveDate,
    pub(crate) amount: Dec,
    pub(crate) pct: Option<Dec>,
}

/// `DrawdownPoint`.
#[derive(Debug, Clone)]
pub(crate) struct DrawdownPoint {
    pub(crate) on: NaiveDate,
    pub(crate) index: Dec,
    pub(crate) peak: Dec,
    pub(crate) drawdown: Dec,
}

/// `MaxDrawdown`.
#[derive(Debug, Clone)]
pub(crate) struct MaxDrawdown {
    pub(crate) peak_on: NaiveDate,
    pub(crate) trough_on: NaiveDate,
    pub(crate) drawdown: Dec,
}

/// `_ordered`: by date (stable), refusing two marks on one day.
fn ordered(points: &[NavPoint]) -> DomainResult<Vec<NavPoint>> {
    let mut out = points.to_vec();
    out.sort_by_key(|p| p.on);
    for pair in out.windows(2) {
        if pair[0].on == pair[1].on {
            return Err(format!("two end-of-day valuations for {}", pair[1].on));
        }
    }
    Ok(out)
}

/// `_growth_factors` inside the 60-digit context.
fn wealth_index(ordered: &[NavPoint]) -> DomainResult<Vec<Dec>> {
    let mut index = vec![Dec::int(1)];
    for pair in ordered.windows(2) {
        let (previous, current) = (&pair[0], &pair[1]);
        let factor = if previous.value.is_zero() {
            Dec::int(1)
        } else {
            current
                .value
                .sub_p(&current.net_flow, WIDE_PRECISION)
                .div_p(&previous.value, WIDE_PRECISION)
                .ok_or("division by zero")?
        };
        let last = index.last().cloned().unwrap_or_else(|| Dec::int(1));
        index.push(last.mul_p(&factor, WIDE_PRECISION));
    }
    Ok(index)
}

fn figure_from_series(
    portfolio: &LedgerPortfolio,
    points: &[NavPoint],
    kind: MetricKind,
) -> DomainResult<ReturnFigure> {
    let ordered = ordered(points)?;
    if let Some(first) = ordered.first()
        && first.on < portfolio.started_on
    {
        return Err(format!(
            "the series starts on {}, before {} began on {}",
            first.on, portfolio.name, portfolio.started_on
        ));
    }
    match ordered.len() {
        0 => ReturnFigure::new(
            kind,
            portfolio.started_on,
            None,
            false,
            Some("No end-of-day valuation for this portfolio yet".to_owned()),
        ),
        1 => ReturnFigure::new(
            kind,
            portfolio.started_on,
            None,
            false,
            Some(
                "Only one end-of-day valuation so far; a return needs a start and an end"
                    .to_owned(),
            ),
        ),
        _ => {
            let index = wealth_index(&ordered)?;
            let last = index.last().cloned().unwrap_or_else(|| Dec::int(1));
            ReturnFigure::new(
                kind,
                portfolio.started_on,
                Some(quantise_return(&last.sub(&Dec::int(1)))),
                false,
                None,
            )
        }
    }
}

/// `twr_figure`.
pub(crate) fn twr_figure(
    portfolio: &LedgerPortfolio,
    points: &[NavPoint],
) -> DomainResult<ReturnFigure> {
    if portfolio.source == PortfolioSource::HoldingGroup {
        return Err(format!("{} is a holding group", portfolio.name));
    }
    figure_from_series(portfolio, points, headline_kind(portfolio.source))
}

/// `since_grouped_figure`.
pub(crate) fn since_grouped_figure(
    portfolio: &LedgerPortfolio,
    points: &[NavPoint],
) -> DomainResult<ReturnFigure> {
    if portfolio.source != PortfolioSource::HoldingGroup {
        return Err(format!("{} is not a holding group", portfolio.name));
    }
    figure_from_series(portfolio, points, MetricKind::SinceGrouped)
}

/// `drawdown_series`.
pub(crate) fn drawdown_series(points: &[NavPoint]) -> DomainResult<Vec<DrawdownPoint>> {
    let ordered = ordered(points)?;
    if ordered.is_empty() {
        return Ok(Vec::new());
    }
    let index = wealth_index(&ordered)?;
    let mut peak = index[0].clone();
    let mut series = Vec::with_capacity(ordered.len());
    for (point, level) in ordered.iter().zip(&index) {
        if *level > peak {
            peak = level.clone();
        }
        let dd = level
            .div(&peak)
            .ok_or("division by zero")?
            .sub(&Dec::int(1));
        series.push(DrawdownPoint {
            on: point.on,
            index: quantise_return(level),
            peak: quantise_return(&peak),
            drawdown: quantise_return(&dd),
        });
    }
    Ok(series)
}

/// `max_drawdown`.
pub(crate) fn max_drawdown(points: &[NavPoint]) -> DomainResult<Option<MaxDrawdown>> {
    let series = drawdown_series(points)?;
    if series.len() < 2 {
        return Ok(None);
    }
    let mut worst = &series[0];
    let mut worst_peak_on = series[0].on;
    let mut running_peak_on = series[0].on;
    for point in &series {
        if point.drawdown.is_zero() {
            running_peak_on = point.on;
        }
        if point.drawdown < worst.drawdown {
            worst = point;
            worst_peak_on = running_peak_on;
        }
    }
    Ok(Some(MaxDrawdown {
        peak_on: worst_peak_on,
        trough_on: worst.on,
        drawdown: worst.drawdown.clone(),
    }))
}

/// `daily_pnl`.
pub(crate) fn daily_pnl(points: &[NavPoint]) -> DomainResult<Vec<DayPnl>> {
    let ordered = ordered(points)?;
    let mut rows = Vec::new();
    for pair in ordered.windows(2) {
        let (previous, current) = (&pair[0], &pair[1]);
        let mv = current.value.sub(&current.net_flow).sub(&previous.value);
        let pct = if previous.value.is_zero() {
            None
        } else {
            Some(quantise_return(
                &mv.div(&previous.value).ok_or("division by zero")?,
            ))
        };
        rows.push(DayPnl {
            on: current.on,
            amount: money(&mv),
            pct,
        });
    }
    Ok(rows)
}

/// `BenchmarkComparison`.
#[derive(Debug, Clone)]
pub(crate) struct BenchmarkComparison {
    pub(crate) name: String,
    pub(crate) portfolio: ReturnFigure,
    pub(crate) benchmark: ReturnFigure,
    pub(crate) difference: Option<Dec>,
}

/// `benchmark_comparison(figure, benchmark, name=...)`.
pub(crate) fn benchmark_comparison(
    figure: &ReturnFigure,
    benchmark: &[NavPoint],
    name: &str,
) -> DomainResult<BenchmarkComparison> {
    if figure.is_model {
        return Err("a benchmark difference cannot be taken against a model figure".to_owned());
    }
    let ordered = ordered(benchmark)?;
    for point in &ordered {
        if !point.net_flow.is_zero() {
            return Err(format!(
                "the benchmark series carries a cash flow on {}",
                point.on
            ));
        }
    }
    if let Some(first) = ordered.first()
        && first.on < figure.since
    {
        return Err("the benchmark series starts before the portfolio".to_owned());
    }
    let index_return = if ordered.len() < 2 {
        None
    } else {
        let index = wealth_index(&ordered)?;
        let last = index.last().cloned().unwrap_or_else(|| Dec::int(1));
        Some(quantise_return(&last.sub(&Dec::int(1))))
    };
    let benchmark_figure = match &index_return {
        None => ReturnFigure::new(
            figure.kind,
            figure.since,
            None,
            false,
            Some("No index history for this period yet".to_owned()),
        )?,
        Some(r) => ReturnFigure::new(figure.kind, figure.since, Some(r.clone()), false, None)?,
    };
    let difference = match (&figure.value, &index_return) {
        (Some(v), Some(r)) => Some(quantise_return(&v.sub(r))),
        _ => None,
    };
    Ok(BenchmarkComparison {
        name: name.to_owned(),
        portfolio: figure.clone(),
        benchmark: benchmark_figure,
        difference,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn p(d: u32, v: &str, f: &str) -> NavPoint {
        NavPoint {
            on: NaiveDate::from_ymd_opt(2026, 9, d).unwrap_or_default(),
            value: Dec::lit(v),
            net_flow: Dec::lit(f),
        }
    }

    #[test]
    fn a_transfer_is_not_a_profit() {
        // Ports test_portfolio_nav.py: a flow lands at the close and earns nothing that day.
        let points = [
            p(1, "100.00", "0"),
            p(2, "210.00", "100.00"),
            p(3, "99.00", "0"),
        ];
        let moves = daily_pnl(&points).unwrap_or_default();
        assert_eq!(moves[0].amount.to_sci_string(), "10.00");
        assert_eq!(
            moves[0].pct.as_ref().map(Dec::to_sci_string),
            Some("0.100000".to_owned())
        );
        let dd = max_drawdown(&points).ok().flatten();
        assert_eq!(
            dd.map(|d| d.drawdown.to_sci_string()),
            Some("-0.528571".to_owned())
        );
        assert!(ordered(&[p(1, "1", "0"), p(1, "2", "0")]).is_err());
    }
}
