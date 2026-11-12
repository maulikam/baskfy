//! The one guarded function that raises a resting GTT stop for the trail job. Guards first
//! (untouchables, `EXCLUDED_SYMBOLS`, up-only, below the price), then the master flag, then the
//! gateway's `modify_gtt_quantity` — its own guards, rate limit, DRY_RUN simulation and journal.

use crate::gateway::{Gateway, ModifyGtt, TenantIds, gtt_status};
use crate::lanes::exec_core::guards::assert_tradeable;
use crate::settings::Settings;
use crate::wire::PyValue;

/// What the trail asks for: the same GTT, the same quantity, a higher trigger.
#[derive(Debug, Clone, PartialEq)]
pub struct TrailReq {
    /// Sleeve.
    pub strategy: String,
    /// Trading symbol.
    pub symbol: String,
    /// Exchange series.
    pub series: Option<String>,
    /// Broker GTT id.
    pub gtt_id: i64,
    /// The GTT's own sell-leg quantity, unchanged.
    pub qty: i64,
    /// Resting trigger.
    pub old_trigger: f64,
    /// Proposed trigger.
    pub new_trigger: f64,
    /// Last close.
    pub last_price: f64,
    /// Journal id.
    pub client_id: String,
    /// The sleeve's own sell-limit fraction, so the leg keeps its rule.
    pub limit_fraction: Option<f64>,
}

/// How it ended.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    /// A guard refused before any network call.
    Refused(String),
    /// `BASKFY_GTT_TRAIL_ENABLED` is off: nothing was sent.
    FlagOff,
    /// DRY_RUN: the gateway simulated the modify.
    Simulated,
    /// The broker accepted the new trigger.
    Modified,
    /// The gateway or broker failed; the old trigger still rests.
    Error(String),
}

impl Outcome {
    /// The journal's outcome word.
    #[must_use]
    pub fn label(&self) -> &'static str {
        match self {
            Self::Refused(_) => "REFUSED",
            Self::FlagOff => "FLAG_OFF",
            Self::Simulated => "SIMULATED",
            Self::Modified => "MODIFIED",
            Self::Error(_) => "ERROR",
        }
    }

    /// The words beside it.
    #[must_use]
    pub fn detail(&self) -> String {
        match self {
            Self::Refused(s) | Self::Error(s) => s.clone(),
            Self::FlagOff => "BASKFY_GTT_TRAIL_ENABLED is off; nothing was sent".to_owned(),
            Self::Simulated => {
                "DRY_RUN: simulated by the gateway; the broker was not called".to_owned()
            }
            Self::Modified => String::new(),
        }
    }
}

/// The guards, without the network: `Some(why)` refuses.
#[must_use]
pub fn refusal(req: &TrailReq, excluded: &[String]) -> Option<String> {
    let sym = req.symbol.trim().to_uppercase();
    if let Err(e) = assert_tradeable(&sym, req.series.as_deref()) {
        return Some(e.message);
    }
    if excluded.iter().any(|x| x.trim().eq_ignore_ascii_case(&sym)) {
        return Some(format!("{sym}: in EXCLUDED_SYMBOLS; never touched"));
    }
    if req.gtt_id <= 0 {
        return Some(format!("{sym}: no broker GTT id"));
    }
    if req.qty <= 0 {
        return Some(format!("{sym}: GTT quantity {} is not a stop", req.qty));
    }
    let ok = |v: f64| v.is_finite() && v > 0.0;
    if !ok(req.old_trigger) || !ok(req.new_trigger) || !ok(req.last_price) {
        return Some(format!("{sym}: trigger or price missing"));
    }
    if req.new_trigger <= req.old_trigger {
        return Some(format!(
            "{sym}: new trigger {} is not above the resting {}; a stop never falls",
            req.new_trigger, req.old_trigger
        ));
    }
    if req.new_trigger >= req.last_price {
        return Some(format!(
            "{sym}: new trigger {} is at or above the last price {}; it would sell at once",
            req.new_trigger, req.last_price
        ));
    }
    None
}

/// Raise one GTT's trigger, guarded. `excluded` is `EXCLUDED_SYMBOLS`.
pub async fn raise_gtt_trigger(
    gateway: &dyn Gateway,
    settings: &Settings,
    excluded: &[String],
    req: &TrailReq,
) -> Outcome {
    if let Some(why) = refusal(req, excluded) {
        return Outcome::Refused(why);
    }
    if !settings.gtt_trail_enabled {
        return Outcome::FlagOff;
    }
    let tenant = match TenantIds::new(settings.sole_user_id, settings.sole_broker_account_id) {
        Ok(t) => t,
        Err(e) => return Outcome::Refused(e.0),
    };
    let res = gateway
        .modify_gtt_quantity(ModifyGtt {
            gtt_id: req.gtt_id,
            symbol: req.symbol.trim().to_uppercase(),
            qty: req.qty,
            trigger: req.new_trigger,
            last_price: req.last_price,
            exchange: "NSE".to_owned(),
            series: req.series.clone(),
            client_id: Some(req.client_id.clone()),
            tenant: Some(tenant),
            plan_tenant: Some(tenant),
            limit_fraction: req.limit_fraction,
            fo_plan: None,
            floor_trigger: None,
        })
        .await;
    let out = match res {
        Ok(d) => d,
        Err(e) => return Outcome::Error(e.message),
    };
    let status = match out.get("status") {
        Some(PyValue::Str(s)) => s.clone(),
        _ => String::new(),
    };
    let error = match out.get("error") {
        Some(PyValue::Str(s)) => s.clone(),
        _ => String::new(),
    };
    match status.as_str() {
        gtt_status::GTT_MODIFIED if !settings.dry_run => Outcome::Modified,
        gtt_status::GTT_MODIFIED | gtt_status::DRY_RUN_GTT_MODIFY => Outcome::Simulated,
        "BLOCKED" => Outcome::Refused(error),
        other => Outcome::Error(if error.is_empty() {
            format!("gateway answered {other}")
        } else {
            error
        }),
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

    use std::sync::Mutex;

    use async_trait::async_trait;

    use super::*;
    use crate::gateway::{CancelOrder, DeleteGtt, GatewayError, GttStop, PlaceOrder};
    use crate::wire::{PyDict, dict};

    /// Records every modify; answers as the real gateway does under the given DRY_RUN.
    struct Fake {
        dry: bool,
        calls: Mutex<Vec<ModifyGtt>>,
    }

    fn nope() -> GatewayError {
        GatewayError {
            python_type: "AssertionError".to_owned(),
            message: "the trail only modifies".to_owned(),
        }
    }

    #[async_trait]
    impl Gateway for Fake {
        async fn place(&self, _o: PlaceOrder) -> Result<PyDict, GatewayError> {
            Err(nope())
        }
        async fn place_gtt_stop(&self, _s: GttStop) -> Result<PyDict, GatewayError> {
            Err(nope())
        }
        async fn delete_gtt(&self, _r: DeleteGtt) -> Result<PyDict, GatewayError> {
            Err(nope())
        }
        async fn cancel_order(&self, _r: CancelOrder) -> Result<PyDict, GatewayError> {
            Err(nope())
        }
        async fn modify_gtt_quantity(&self, r: ModifyGtt) -> Result<PyDict, GatewayError> {
            if let Ok(mut c) = self.calls.lock() {
                c.push(r);
            }
            let st = if self.dry {
                gtt_status::DRY_RUN_GTT_MODIFY
            } else {
                gtt_status::GTT_MODIFIED
            };
            Ok(dict([("status", PyValue::Str(st.to_owned()))]))
        }
    }

    fn fake(dry: bool) -> Fake {
        Fake {
            dry,
            calls: Mutex::new(Vec::new()),
        }
    }

    fn settings(dry: bool, flag: bool) -> Settings {
        let mut s = Settings::dry_defaults().unwrap_or_else(|e| panic!("{e:?}"));
        s.dry_run = dry;
        s.gtt_trail_enabled = flag;
        s.sole_user_id = 1;
        s.sole_broker_account_id = 1;
        s
    }

    fn req(symbol: &str) -> TrailReq {
        TrailReq {
            strategy: "twt".to_owned(),
            symbol: symbol.to_owned(),
            series: Some("EQ".to_owned()),
            gtt_id: 344_073_747,
            qty: 204,
            old_trigger: 488.6,
            new_trigger: 613.8,
            last_price: 653.9,
            client_id: "trail:twt:9:613.8".to_owned(),
            limit_fraction: Some(0.97),
        }
    }

    fn calls(g: &Fake) -> usize {
        g.calls.lock().map_or(0, |c| c.len())
    }

    #[tokio::test]
    async fn flag_off_sends_nothing() {
        let g = fake(false);
        let out = raise_gtt_trigger(&g, &settings(false, false), &[], &req("SUNTV")).await;
        assert_eq!(out, Outcome::FlagOff);
        assert_eq!(calls(&g), 0);
    }

    #[tokio::test]
    async fn dry_run_is_simulated_through_the_gateway() {
        let g = fake(true);
        let out = raise_gtt_trigger(&g, &settings(true, true), &[], &req("SUNTV")).await;
        assert_eq!(out, Outcome::Simulated);
        let sent = g.calls.lock().map(|c| c.clone()).unwrap_or_default();
        assert_eq!(sent.len(), 1);
        assert_eq!(sent[0].qty, 204, "the GTT's own quantity, unchanged");
        assert!((sent[0].trigger - 613.8).abs() < 1e-9);
        assert_eq!(sent[0].gtt_id, 344_073_747);
    }

    #[tokio::test]
    async fn live_is_modified() {
        let g = fake(false);
        let out = raise_gtt_trigger(&g, &settings(false, true), &[], &req("SUNTV")).await;
        assert_eq!(out, Outcome::Modified);
    }

    #[tokio::test]
    async fn untouchables_and_excluded_refused_before_any_call() {
        let g = fake(false);
        let s = settings(false, true);
        for sym in ["SGBDE31III", "SGBMAR29"] {
            let out = raise_gtt_trigger(&g, &s, &[], &req(sym)).await;
            assert!(matches!(out, Outcome::Refused(_)), "{sym}: {out:?}");
        }
        let out = raise_gtt_trigger(&g, &s, &["SUNTV".to_owned()], &req("SUNTV")).await;
        assert!(matches!(out, Outcome::Refused(_)), "{out:?}");
        assert_eq!(calls(&g), 0);
    }

    #[tokio::test]
    async fn never_lowers_and_never_above_price() {
        let g = fake(false);
        let s = settings(false, true);
        let mut down = req("SUNTV");
        down.new_trigger = 480.0;
        assert!(matches!(
            raise_gtt_trigger(&g, &s, &[], &down).await,
            Outcome::Refused(_)
        ));
        let mut above = req("SUNTV");
        above.new_trigger = 660.0;
        assert!(matches!(
            raise_gtt_trigger(&g, &s, &[], &above).await,
            Outcome::Refused(_)
        ));
        assert_eq!(calls(&g), 0);
    }
}
