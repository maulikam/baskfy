//! `GET /admin/providers` — `baskfy_api.admin.provider_health`: `providers doctor` over HTTP.
//!
//! Python builds the production provider stack (`build_provider_stack()`, Kite on the bulk lane,
//! NSE, fixtures) and renders `doctor_report(stack)`, one line per registered adapter. Building
//! the stack makes **no network call** by construction (Prompt 2 acceptance criterion 4); the
//! Kite and NSE adapters only ask Redis for their rate limiter, as Python's do.

use std::sync::Arc;

use axum::extract::Extension;
use axum::response::Response;
use baskfy_providers::factory::{RedisSource, build_provider_stack};
use baskfy_providers::ports::ProviderHealth;
use baskfy_providers::settings::get_provider_settings;
use serde::Serialize;

use crate::auth::{Principal, require_staff};
use crate::problems::{Problem, internal_error};
use crate::wire;

/// `ProviderHealthOut`.
#[derive(Debug, Clone, Serialize)]
pub struct ProviderHealthOut {
    /// `name`.
    pub name: String,
    /// `available`.
    pub available: bool,
    /// `detail`.
    pub detail: String,
    /// `serves_now`: sorted capability values.
    pub serves_now: Vec<String>,
    /// `can_serve`: sorted capability values.
    pub can_serve: Vec<String>,
}

/// `ProviderHealthListOut`.
#[derive(Debug, Clone, Serialize)]
pub struct ProviderHealthListOut {
    /// `data`.
    pub data: Vec<ProviderHealthOut>,
}

fn sorted_values(set: &baskfy_providers::ports::CapabilitySet) -> Vec<String> {
    let mut values: Vec<String> = set.iter().map(|c| c.value().to_owned()).collect();
    values.sort();
    values
}

fn out(report: &ProviderHealth) -> ProviderHealthOut {
    ProviderHealthOut {
        name: report.name.clone(),
        available: report.available,
        detail: report.detail.clone(),
        serves_now: sorted_values(&report.served_capabilities()),
        can_serve: sorted_values(&report.capabilities),
    }
}

/// The production stack, built the way `build_provider_stack()` builds it.
///
/// # Errors
/// A provider-settings or construction failure: Python raises out of the handler (the 500).
pub async fn production_stack()
-> Result<Arc<baskfy_providers::composite::CompositeProvider>, Problem> {
    let settings = get_provider_settings().map_err(|e| {
        crate::logging::error(
            "baskfy_api.app",
            "unhandled error",
            &[("error", &e.to_string())],
        );
        internal_error()
    })?;
    let source = RedisSource::from_settings(settings);
    let stack = build_provider_stack(&source, settings, None)
        .await
        .map_err(|e| {
            crate::logging::error(
                "baskfy_api.app",
                "unhandled error",
                &[("error", &e.to_string())],
            );
            internal_error()
        })?;
    Ok(Arc::new(stack))
}

/// `GET /admin/providers` (`get_provider_health`).
///
/// # Errors
/// The staff gate; 500 when the stack cannot be built.
pub async fn get_provider_health(
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    require_staff(&principal)?;
    let stack = production_stack().await?;
    let data = stack.health_reports().iter().map(out).collect();
    Ok(wire::json(&ProviderHealthListOut { data }))
}
