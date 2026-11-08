//! Ports `baskfy_execution.gtt`: GTT stops as values and pure helpers the gateway enforces
//! (non-negotiable 4). The stop LEVEL is never computed here — it is `stop_from_vol` at plan
//! time ([`super::stops::stop_from_vol`]); the gateway refuses a trigger that is not a stop and
//! journals one outside the band.
//!
//! The status vocabulary is [`crate::gateway::gtt_status`], preserved exactly (the desk's
//! `/stops/arm` matches on those literals).

use std::collections::HashMap;

use crate::gateway::{DEFAULT_TICK, StopBand, gtt_status};
use crate::kite::{GttOrderLeg, GttParams};
use crate::wire::PyValue;

/// `GTT_PLACED_STATUSES`: the stop rests (or was faithfully simulated).
pub const GTT_PLACED_STATUSES: [&str; 2] = [gtt_status::GTT_PLACED, gtt_status::DRY_RUN_GTT];
/// `GTT_DELETED_STATUSES`: the trigger is gone.
pub const GTT_DELETED_STATUSES: [&str; 2] =
    [gtt_status::GTT_DELETED, gtt_status::DRY_RUN_GTT_DELETE];
/// `GTT_MODIFIED_STATUSES`: the GTT covers the new quantity.
pub const GTT_MODIFIED_STATUSES: [&str; 2] =
    [gtt_status::GTT_MODIFIED, gtt_status::DRY_RUN_GTT_MODIFY];
/// `ORDER_CANCELLED_STATUSES`: the order no longer rests.
pub const ORDER_CANCELLED_STATUSES: [&str; 2] = [
    gtt_status::ORDER_CANCELLED,
    gtt_status::ORDER_CANCEL_DRY_RUN,
];

/// `_BAND_EPSILON`: a stop exactly on the boundary is inside the band.
const BAND_EPSILON: f64 = 1e-9;

/// Python's `round(x, ndigits)` on a float: the correctly rounded decimal (ties to even on the
/// exact binary value), read back as the nearest double. Non-finite values pass through.
#[must_use]
pub fn py_round(x: f64, ndigits: usize) -> f64 {
    if !x.is_finite() {
        return x;
    }
    format!("{x:.ndigits$}").parse::<f64>().unwrap_or(x)
}

/// Python's `round(x)` with no digits: ties to even, as a float (callers multiply it back).
#[must_use]
pub fn py_round0(x: f64) -> f64 {
    x.round_ties_even()
}

/// `to_tick(price, tick)`: snap to the nearest valid tick, rounded to the paisa.
#[must_use]
pub fn to_tick(price: f64, tick: f64) -> f64 {
    if tick <= 0.0 {
        return py_round(price, 2);
    }
    let steps = py_round0(price / tick);
    py_round(steps * tick, 2)
}

/// `TickSizes`: per-exchange tick sizes, cached for the life of the gateway. An exchange is
/// remembered only from a non-empty dump (the gateway refuses an empty one first).
#[derive(Debug, Default, Clone)]
pub struct TickSizes {
    by_exchange: HashMap<String, HashMap<String, f64>>,
}

impl TickSizes {
    /// `cached(exchange)`.
    #[must_use]
    pub fn cached(&self, exchange: &str) -> bool {
        self.by_exchange.contains_key(exchange)
    }

    /// `remember(exchange, dump)`: `{tradingsymbol: float(tick_size or 0.05)}` over every row
    /// that names a tradingsymbol.
    pub fn remember(&mut self, exchange: &str, instruments: &[PyValue]) {
        let mut ticks = HashMap::new();
        for row in instruments {
            let PyValue::Map(m) = row else { continue };
            let Some(sym) = m.get("tradingsymbol").filter(|v| v.truthy()) else {
                continue;
            };
            let sym = match sym {
                PyValue::Str(s) => s.clone(),
                PyValue::Int(i) => i.to_string(),
                other => format!("{other:?}"),
            };
            // `float(i.get("tick_size") or 0.05)`: a falsy tick (absent, None, 0) is the default.
            #[allow(clippy::cast_precision_loss)]
            let tick = match m.get("tick_size") {
                Some(v) if v.truthy() => match v {
                    PyValue::Float(f) => *f,
                    PyValue::Int(i) => *i as f64,
                    PyValue::Bool(_) => 1.0,
                    PyValue::Str(s) => s.trim().parse::<f64>().unwrap_or(DEFAULT_TICK),
                    _ => DEFAULT_TICK,
                },
                _ => DEFAULT_TICK,
            };
            ticks.insert(sym, tick);
        }
        self.by_exchange.insert(exchange.to_owned(), ticks);
    }

    /// `get(symbol, exchange)`, `0.05` when unknown.
    #[must_use]
    pub fn get(&self, symbol: &str, exchange: &str) -> f64 {
        self.by_exchange
            .get(exchange)
            .and_then(|t| t.get(symbol))
            .copied()
            .unwrap_or(DEFAULT_TICK)
    }
}

/// `refuse_stop(...)`: why this GTT must not be placed at all, or `""` when it is a stop.
#[must_use]
pub fn refuse_stop(symbol: &str, qty: i64, trigger: f64, last_price: f64) -> String {
    let repr = baskfy_core::py::float_repr;
    if qty <= 0 {
        return format!("{symbol}: GTT quantity must be positive, got {qty}");
    }
    if !trigger.is_finite() || trigger <= 0.0 {
        return format!(
            "{symbol}: GTT trigger must be a finite positive number, got {}",
            repr(trigger)
        );
    }
    if !last_price.is_finite() || last_price <= 0.0 {
        return format!(
            "{symbol}: no finite last price, so the trigger {} cannot be checked against anything",
            repr(trigger)
        );
    }
    if trigger >= last_price {
        return format!(
            "{symbol}: trigger {} is at or above the last price {} -- that is not a stop, it would \
             fire immediately and sell the position",
            repr(trigger),
            repr(last_price)
        );
    }
    String::new()
}

/// `band_finding(...)`: `"too_far"` / `"too_close"` / `""`, the desk's review page's epsilon.
/// `too_close` only against a band whose floor is the weekly book's (AF 3.9).
#[must_use]
pub fn band_finding(trigger: f64, last_price: f64, band: StopBand) -> &'static str {
    if !trigger.is_finite() || !last_price.is_finite() || last_price <= 0.0 {
        return "";
    }
    let drop = 1.0 - trigger / last_price;
    if drop > band.max_pct() + BAND_EPSILON {
        return "too_far";
    }
    if drop < band.min_pct() - BAND_EPSILON {
        if band.min_pct() + BAND_EPSILON < StopBand::default().min_pct() {
            return "";
        }
        return "too_close";
    }
    ""
}

/// `drop_pct(...)`: how far below the last price, in percent, rounded to 2.
#[must_use]
pub fn drop_pct(trigger: f64, last_price: f64) -> f64 {
    if !trigger.is_finite() || !last_price.is_finite() || last_price <= 0.0 {
        return 0.0;
    }
    py_round((1.0 - trigger / last_price) * 100.0, 2)
}

/// `gtt_params(kc, ...)`: everything `kc.place_gtt` / `kc.modify_gtt` is called with — the
/// single SELL LIMIT leg a stop fires, CNC unless `product` names the FO carry product.
#[must_use]
pub fn gtt_params(
    symbol: &str,
    exchange: &str,
    qty: i64,
    trigger: f64,
    limit: f64,
    last_price: f64,
    product: Option<&str>,
) -> GttParams {
    GttParams {
        trigger_type: "single".to_owned(),
        tradingsymbol: symbol.to_owned(),
        exchange: exchange.to_owned(),
        trigger_values: vec![PyValue::Float(trigger)],
        last_price: PyValue::Float(last_price),
        orders: vec![GttOrderLeg {
            transaction_type: "SELL".to_owned(),
            quantity: qty,
            order_type: "LIMIT".to_owned(),
            product: product.unwrap_or("CNC").to_owned(),
            price: limit,
        }],
    }
}
