//! `baskfy_core.costs`: Zerodha's charges on an NSE delivery (CNC) order, as the no-trade band
//! and the plan's `est_costs` read them. Graded on `fixtures/weekly/costs.json`.

use super::pandas::{py_round, py_round_int};
use crate::wire::{PyDict, PyValue};

/// 0.1% of turnover, both sides.
pub const STT_DELIVERY: f64 = 0.001;
/// 0.00297% of turnover.
pub const EXCHANGE_TXN_NSE: f64 = 0.000_029_7;
/// Rs 10 per crore.
pub const SEBI_TURNOVER: f64 = 0.000_001;
/// 0.015% of buy turnover.
pub const STAMP_DUTY_BUY: f64 = 0.000_15;
/// On brokerage + exchange + SEBI.
pub const GST: f64 = 0.18;
/// Zerodha: delivery is free.
pub const BROKERAGE_DELIVERY: f64 = 0.0;
/// Per scrip per day, sell side only.
pub const DP_CHARGE_PER_SELL: f64 = 15.93;

/// `Cost`.
#[derive(Debug, Clone, Copy, PartialEq, Default)]
pub struct Cost {
    /// `stt`.
    pub stt: f64,
    /// `exchange`.
    pub exchange: f64,
    /// `sebi`.
    pub sebi: f64,
    /// `stamp`.
    pub stamp: f64,
    /// `gst`.
    pub gst: f64,
    /// `dp`.
    pub dp: f64,
    /// `brokerage`.
    pub brokerage: f64,
}

impl Cost {
    /// `total` (summed in field order).
    #[must_use]
    pub fn total(&self) -> f64 {
        self.stt + self.exchange + self.sebi + self.stamp + self.gst + self.dp + self.brokerage
    }

    /// `as_dict()`.
    #[must_use]
    pub fn as_dict(&self) -> PyDict {
        let mut d = PyDict::new();
        d.insert("stt", PyValue::Float(py_round(self.stt, 2)));
        d.insert("exchange", PyValue::Float(py_round(self.exchange, 2)));
        d.insert("sebi", PyValue::Float(py_round(self.sebi, 2)));
        d.insert("stamp", PyValue::Float(py_round(self.stamp, 2)));
        d.insert("gst", PyValue::Float(py_round(self.gst, 2)));
        d.insert("dp", PyValue::Float(py_round(self.dp, 2)));
        d.insert("brokerage", PyValue::Float(py_round(self.brokerage, 2)));
        d.insert("total", PyValue::Float(py_round(self.total(), 2)));
        d
    }
}

/// `order_cost(value, side)`.
#[must_use]
pub fn order_cost(value: f64, side: &str) -> Cost {
    let v = value.abs();
    let is_sell = side.to_uppercase() == "SELL";
    let exchange = v * EXCHANGE_TXN_NSE;
    let sebi = v * SEBI_TURNOVER;
    let brokerage = v * BROKERAGE_DELIVERY;
    Cost {
        stt: v * STT_DELIVERY,
        exchange,
        sebi,
        stamp: if is_sell { 0.0 } else { v * STAMP_DUTY_BUY },
        gst: GST * (brokerage + exchange + sebi),
        dp: if is_sell { DP_CHARGE_PER_SELL } else { 0.0 },
        brokerage,
    }
}

/// `cost_pct(value, side)`: percent of the trade; 100 for a zero trade.
#[must_use]
pub fn cost_pct(value: f64, side: &str) -> f64 {
    let v = value.abs();
    if v == 0.0 {
        100.0
    } else {
        order_cost(v, side).total() / v * 100.0
    }
}

/// One order's two inputs to [`plan_cost`]: `int(o.get("delta") or 0)` and
/// `float(o.get("ref_price") or 0.0)`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct CostLine {
    /// `delta`.
    pub delta: i64,
    /// `ref_price`.
    pub ref_price: f64,
}

/// `plan_cost(orders)`: the itemised total, turnover and bps.
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn plan_cost(orders: &[CostLine]) -> PyDict {
    let mut total = Cost::default();
    let (mut buys, mut sells) = (0.0_f64, 0.0_f64);
    // Python starts from `Cost(0, 0, 0, 0, 0, 0, 0)`: ints, which stay ints (and round to ints)
    // until the first counted order adds a float to every field.
    let mut counted = false;
    for o in orders {
        if o.delta == 0 {
            continue;
        }
        counted = true;
        let v = o.delta.unsigned_abs() as f64 * o.ref_price;
        let side = if o.delta > 0 { "BUY" } else { "SELL" };
        let c = order_cost(v, side);
        total = Cost {
            stt: total.stt + c.stt,
            exchange: total.exchange + c.exchange,
            sebi: total.sebi + c.sebi,
            stamp: total.stamp + c.stamp,
            gst: total.gst + c.gst,
            dp: total.dp + c.dp,
            brokerage: total.brokerage + c.brokerage,
        };
        if o.delta > 0 {
            buys += v;
        } else {
            sells += v;
        }
    }
    let turnover = buys + sells;
    let mut d = if counted {
        total.as_dict()
    } else {
        let mut z = PyDict::new();
        for k in [
            "stt",
            "exchange",
            "sebi",
            "stamp",
            "gst",
            "dp",
            "brokerage",
            "total",
        ] {
            z.insert(k, PyValue::Int(0));
        }
        z
    };
    d.insert(
        "turnover",
        py_round_int(turnover).map_or(PyValue::None, PyValue::Int),
    );
    d.insert(
        "bps_of_turnover",
        PyValue::Float(if turnover == 0.0 {
            0.0
        } else {
            py_round(total.total() / turnover * 10000.0, 1)
        }),
    );
    d
}
