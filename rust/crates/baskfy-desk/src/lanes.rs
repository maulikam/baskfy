//! The wave-D porting lanes (DECISIONS-RS R8.5): one module per lane, each a declared stub
//! its lane owns. The app assembles them here; lanes never edit this file.

pub mod analytics;
pub mod exec_core;
pub mod fno;
pub mod options;
pub mod session;
pub mod swing;
pub mod twt;
pub mod vbt;
pub mod weekly;

use axum::Router;

use crate::app::AppState;

/// Every lane's routes, merged.
pub fn routes() -> Router<AppState> {
    Router::new()
        .merge(exec_core::routes())
        .merge(weekly::routes())
        .merge(analytics::routes())
        .merge(swing::routes())
        .merge(twt::routes())
        .merge(vbt::routes())
        .merge(fno::routes())
        .merge(options::routes())
        .merge(session::routes())
}

/// Every lane's template globals.
pub fn register_templates(templates: &mut crate::templates::Templates) {
    exec_core::register_templates(templates);
    weekly::register_templates(templates);
    analytics::register_templates(templates);
    swing::register_templates(templates);
    twt::register_templates(templates);
    vbt::register_templates(templates);
    fno::register_templates(templates);
    options::register_templates(templates);
    session::register_templates(templates);
}

/// Run a background service by name (`baskfy-desk service <name>`).
///
/// # Errors
/// An unknown name, or the service's own failure.
pub async fn run_service(name: &str, state: &AppState) -> Result<(), String> {
    if let Some(result) = exec_core::run_service(name, state).await {
        return result;
    }
    if let Some(result) = weekly::run_service(name, state).await {
        return result;
    }
    if let Some(result) = analytics::run_service(name, state).await {
        return result;
    }
    if let Some(result) = swing::run_service(name, state).await {
        return result;
    }
    if let Some(result) = twt::run_service(name, state).await {
        return result;
    }
    if let Some(result) = vbt::run_service(name, state).await {
        return result;
    }
    if let Some(result) = fno::run_service(name, state).await {
        return result;
    }
    if let Some(result) = options::run_service(name, state).await {
        return result;
    }
    if let Some(result) = session::run_service(name, state).await {
        return result;
    }
    Err(format!("no desk service named {name:?}"))
}
