//! Ports `app.strategies.intraday_skeleton` — the READY-BUT-DISABLED intraday engine. Its
//! `signal()` returns 0, so it never sends anything; were it to, the order would be MIS through
//! the gateway, which refuses MIS unless `INTRADAY_ENABLED` (non-negotiable 5, enforced in the
//! gateway, not here).

use std::sync::Arc;

use crate::gateway::{Gateway, GatewayError, PlaceOrder};
use crate::wire::{PyDict, PyValue};

/// `IntradayMomentumBurst`.
pub struct IntradayMomentumBurst {
    gw: Arc<dyn Gateway>,
    /// Followed tokens.
    pub tokens: Vec<i64>,
}

impl IntradayMomentumBurst {
    /// `name`.
    pub const NAME: &'static str = "intraday_burst";
    /// `products`.
    pub const PRODUCTS: [&'static str; 1] = ["MIS"];

    /// `IntradayMomentumBurst(gateway, tokens)`.
    #[must_use]
    pub fn new(gateway: Arc<dyn Gateway>, tokens: Option<Vec<i64>>) -> Self {
        Self {
            gw: gateway,
            tokens: tokens.unwrap_or_default(),
        }
    }

    /// `signal(tick)`: `+qty` / `-qty` / `0`. The skeleton's alpha is 0, always.
    #[must_use]
    pub const fn signal(&self, _tick: &PyDict) -> i64 {
        0
    }

    /// `on_tick(tick)`: a non-zero signal becomes one MIS LIMIT through the gateway.
    ///
    /// # Errors
    /// The gateway's exception.
    pub async fn on_tick(&self, tick: &PyDict) -> Result<Option<PyDict>, GatewayError> {
        let qty = self.signal(tick);
        if qty == 0 {
            return Ok(None);
        }
        let symbol = match tick.get("tradingsymbol") {
            Some(PyValue::Str(s)) => s.clone(),
            _ => String::new(),
        };
        let price = match tick.get("last_price") {
            Some(PyValue::Float(f)) => Some(*f),
            #[allow(clippy::cast_precision_loss)]
            Some(PyValue::Int(i)) => Some(*i as f64),
            _ => None,
        };
        let order = PlaceOrder {
            symbol,
            qty: qty.abs(),
            side: if qty > 0 { "BUY" } else { "SELL" }.to_owned(),
            product: "MIS".to_owned(),
            order_type: "LIMIT".to_owned(),
            price,
            ..PlaceOrder::default()
        };
        self.gw.place(order).await.map(Some)
    }

    /// `generate_targets(context)`: the event-driven engine has no batch hook.
    #[must_use]
    pub const fn generate_targets(&self) -> Vec<PyDict> {
        Vec::new()
    }
}
