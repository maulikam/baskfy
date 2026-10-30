//! The swing sleeve's own gateway, cached per live base gateway.

use std::sync::Arc;

use super::execute::{SWING_JOURNAL_NAME, swing_gates};
use crate::app::AppState;
use crate::gateway::Gateway;

type SwingGatewayCache = std::sync::Mutex<Vec<(std::sync::Weak<dyn Gateway>, Arc<dyn Gateway>)>>;

static SWING_GATEWAYS: SwingGatewayCache = std::sync::Mutex::new(Vec::new());

// Keyed by a weak reference, never a bare address: a dropped state's address can be reused.
pub(crate) fn swing_sleeve_gateway(
    state: &AppState,
    band: crate::gateway::StopBand,
) -> Arc<dyn Gateway> {
    let base = Arc::downgrade(&state.gateway);
    if let Ok(mut cache) = SWING_GATEWAYS.lock() {
        cache.retain(|(w, _)| w.strong_count() > 0);
        if let Some((_, gw)) = cache.iter().find(|(w, _)| w.ptr_eq(&base)) {
            return gw.clone();
        }
    }
    let settings = state.settings.clone();
    let journal = std::path::Path::new(crate::gateway::JOURNAL)
        .parent()
        .map_or_else(
            || std::path::PathBuf::from(SWING_JOURNAL_NAME),
            |p| p.join(SWING_JOURNAL_NAME),
        );
    let spec = crate::gateway::SleeveGatewaySpec {
        derived: crate::gateway::DerivedGateway {
            gates: Arc::new(move || {
                let s = match settings.read() {
                    Ok(s) => s.clone(),
                    Err(p) => p.into_inner().clone(),
                };
                swing_gates(&s)
            }),
            journal_path: state.paths.resolve(&journal.to_string_lossy()),
            fo_coverage: false,
        },
        stop_band: band,
    };
    let Some(gw) = state.gateway.derive_sleeve(spec) else {
        return Arc::new(crate::lanes::twt::NoSleeveGateway("swing"));
    };
    if let Ok(mut cache) = SWING_GATEWAYS.lock() {
        cache.push((base, gw.clone()));
    }
    gw
}
