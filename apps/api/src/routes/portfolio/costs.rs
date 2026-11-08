//! Ports `baskfy_core.portfolio_costs.estimate_portfolio_costs` and the `baskfy_core.costs`
//! cost model it calls. The components are Python `float`s (`costs.py` is float arithmetic),
//! summed in the same order and then `Decimal(str(round(x, 2)))`'d, so they print alike.

use std::collections::HashSet;

use chrono::NaiveDate;

use super::pydec::Dec;

const STT_DELIVERY: f64 = 0.001;
const EXCHANGE_TXN_NSE: f64 = 2.97e-05;
const SEBI_TURNOVER: f64 = 1e-06;
const STAMP_DUTY_BUY: f64 = 0.00015;
const GST: f64 = 0.18;
const BROKERAGE_DELIVERY: f64 = 0.0;
/// `DP_CHARGE_PER_SELL`.
const DP_CHARGE_PER_SELL: f64 = 15.93;

/// `ESTIMATE_CAVEAT`.
pub(crate) const ESTIMATE_CAVEAT: &str = "An estimate of what these trades cost at current \
statutory rates, not a billed amount. Returns shown elsewhere are before these charges, not after \
them.";

/// `TradeFlow`.
pub(crate) struct TradeFlow {
    pub(crate) on: NaiveDate,
    pub(crate) kind: String,
    pub(crate) value: Dec,
    pub(crate) instrument_id: Option<i64>,
}

/// `PortfolioCosts`.
pub(crate) struct PortfolioCosts {
    pub(crate) stt: Dec,
    pub(crate) exchange: Dec,
    pub(crate) sebi: Dec,
    pub(crate) stamp: Dec,
    pub(crate) gst: Dec,
    pub(crate) dp: Dec,
    pub(crate) brokerage: Dec,
    pub(crate) total: Dec,
    pub(crate) turnover: Dec,
    pub(crate) trades: usize,
    pub(crate) sell_scrip_days: usize,
}

impl PortfolioCosts {
    /// `bps_of_turnover`.
    pub(crate) fn bps_of_turnover(&self) -> Option<Dec> {
        if self.turnover.is_zero() {
            return None;
        }
        let ratio = self.total.div(&self.turnover)?.mul(&Dec::int(10000));
        ratio.quantize(-2, baskfy_core::decimal::Rounding::HalfEven)
    }
}

/// Python's `round(x, 2)`: the exact binary value rounded half-even to two places (CPython
/// rounds the exact expansion, so `round(0.125, 2)` is `0.12`), as the nearest double.
fn py_round2(x: f64) -> f64 {
    if !x.is_finite() {
        return x;
    }
    // 80 places is the exact expansion of every double above ~1e-28; below that it rounds to 0.
    let exact = Dec::lit(&format!("{x:.80}"));
    exact
        .quantize(-2, baskfy_core::decimal::Rounding::HalfEven)
        .and_then(|d| d.to_sci_string().parse().ok())
        .unwrap_or(x)
}

/// `_money(value)`: `Decimal(str(round(value, 2)))`.
fn money_of(value: f64) -> Dec {
    Dec::lit(&baskfy_core::py::float_repr(py_round2(value)))
}

/// `float(Decimal)`: correctly rounded.
fn to_f64(d: &Dec) -> f64 {
    d.to_sci_string().parse().unwrap_or(0.0)
}

/// `estimate_portfolio_costs`: `None` when nothing was bought or sold.
pub(crate) fn estimate_portfolio_costs(flows: &[TradeFlow]) -> Option<PortfolioCosts> {
    let trades: Vec<&TradeFlow> = flows
        .iter()
        .filter(|f| {
            let k = f.kind.to_uppercase();
            (k == "BUY" || k == "SELL") && !f.value.is_zero()
        })
        .collect();
    if trades.is_empty() {
        return None;
    }
    let (mut stt, mut exchange, mut sebi, mut stamp, mut gst, mut brokerage) =
        (0.0f64, 0.0f64, 0.0f64, 0.0f64, 0.0f64, 0.0f64);
    let mut turnover = Dec::int(0);
    let mut sell_scrip_days: HashSet<(Option<i64>, NaiveDate)> = HashSet::new();
    for trade in &trades {
        let value = trade.value.abs();
        turnover = turnover.add(&value);
        let is_sell = trade.kind.to_uppercase() == "SELL";
        let v = to_f64(&value).abs();
        let ex = v * EXCHANGE_TXN_NSE;
        let se = v * SEBI_TURNOVER;
        let br = v * BROKERAGE_DELIVERY;
        stt += v * STT_DELIVERY;
        exchange += ex;
        sebi += se;
        stamp += if is_sell { 0.0 } else { v * STAMP_DUTY_BUY };
        gst += GST * (br + ex + se);
        brokerage += br;
        if is_sell {
            sell_scrip_days.insert((trade.instrument_id, trade.on));
        }
    }
    #[allow(clippy::cast_precision_loss)]
    let dp = money_of(DP_CHARGE_PER_SELL * sell_scrip_days.len() as f64);
    let parts = [
        money_of(stt),
        money_of(exchange),
        money_of(sebi),
        money_of(stamp),
        money_of(gst),
        dp,
        money_of(brokerage),
    ];
    let total = parts.iter().fold(Dec::int(0), |acc, p| acc.add(p));
    let [stt, exchange, sebi, stamp, gst, dp, brokerage] = parts;
    Some(PortfolioCosts {
        stt,
        exchange,
        sebi,
        stamp,
        gst,
        dp,
        brokerage,
        total,
        turnover,
        trades: trades.len(),
        sell_scrip_days: sell_scrip_days.len(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn charges_match_the_float_model() {
        // CPython: estimate_portfolio_costs([TradeFlow(d, "BUY", Decimal("100000.00"), 1),
        //   TradeFlow(d, "SELL", Decimal("50000.00"), 1)]) -> stt 150.0, exchange 4.46,
        //   sebi 0.15, stamp 15.0, gst 0.83, dp 15.93, brokerage 0.0, total 186.37.
        let d = NaiveDate::from_ymd_opt(2026, 9, 1).unwrap_or_default();
        let flows = [
            TradeFlow {
                on: d,
                kind: "BUY".to_owned(),
                value: Dec::lit("100000.00"),
                instrument_id: Some(1),
            },
            TradeFlow {
                on: d,
                kind: "SELL".to_owned(),
                value: Dec::lit("50000.00"),
                instrument_id: Some(1),
            },
        ];
        let Some(c) = estimate_portfolio_costs(&flows) else {
            unreachable!("two trades")
        };
        let got: Vec<String> = [
            &c.stt,
            &c.exchange,
            &c.sebi,
            &c.stamp,
            &c.gst,
            &c.dp,
            &c.brokerage,
            &c.total,
        ]
        .iter()
        .map(|d| d.to_sci_string())
        .collect();
        assert_eq!(
            got,
            [
                "150.0", "4.46", "0.15", "15.0", "0.83", "15.93", "0.0", "186.37"
            ]
        );
        assert_eq!(c.turnover.to_sci_string(), "150000.00");
        assert_eq!(
            c.bps_of_turnover().map(|d| d.to_sci_string()),
            Some("12.42".to_owned())
        );
        assert!(estimate_portfolio_costs(&[]).is_none());
        // CPython: round(0.125, 2) == 0.12, round(2.675, 2) == 2.67 (binary 2.67499...).
        assert_eq!(py_round2(0.125).to_bits(), 0.12f64.to_bits());
        assert_eq!(py_round2(2.675).to_bits(), 2.67f64.to_bits());
    }
}
