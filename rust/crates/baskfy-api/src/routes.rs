//! One module per Python router (`baskfy_api/routers/*.py`), plus [`ops`] for `app.py`'s own
//! `/health`. Each lane porter owns its module file and never edits this one: every module's
//! `router()` is already merged below, in `app._mount_routers`' order (`curated_versions` and
//! `watchlist` where `explore` includes them). Route paths are relative to `/api/v1`.
//!
//! Matching differs from Starlette in one way porters must know: Starlette takes the **first**
//! route in registration order that matches, axum (matchit) prefers a static segment over a
//! parameter wherever they were registered. Where a Python router registers `/{id}` before a
//! static sibling, the porter checks which one Python actually serves and records it.
//!
//! `public` is declared and **never merged**: the public read API stays shut (D9).

use axum::Router;

use crate::state::AppState;

pub mod admin;
pub mod alerts;
pub mod api_keys;
pub mod auth;
pub mod backtests;
pub mod baskets;
pub mod billing;
pub mod brokers;
pub mod curated_costs;
pub mod curated_create;
pub mod curated_customize;
pub mod curated_drift;
pub mod curated_engage;
pub mod curated_from_screen;
pub mod curated_investments;
pub mod curated_plans;
pub mod curated_sip;
pub mod curated_trending;
pub mod curated_versions;
pub mod desk;
pub mod explore;
pub mod fno;
pub mod instruments;
pub mod kite;
pub mod managers;
pub mod market_data;
pub mod meta;
pub mod ops;
pub mod options;
pub mod overlap;
pub mod portfolio_overview;
pub mod portfolios;
pub mod public;
pub mod screens;
pub mod search;
pub mod sleeve_state;
pub mod sleeves;
pub mod support;
pub mod swing;
pub mod track_b;
pub mod trades;
pub mod twt;
pub mod vbt;
pub mod watchlist;
pub mod webhook_endpoints;

/// Every versioned router, merged in `_mount_routers` order (`public` excluded, D9).
pub fn api_router() -> Router<AppState> {
    Router::new()
        .merge(meta::router())
        .merge(screens::router())
        .merge(instruments::router())
        .merge(overlap::router())
        .merge(search::router())
        .merge(market_data::router())
        .merge(auth::router())
        .merge(billing::router())
        .merge(portfolios::router())
        .merge(portfolio_overview::router())
        .merge(trades::router())
        .merge(backtests::router())
        .merge(baskets::router())
        .merge(kite::router())
        .merge(desk::router())
        .merge(sleeves::router())
        .merge(sleeve_state::router())
        .merge(support::router())
        .merge(admin::router())
        .merge(api_keys::router())
        .merge(alerts::router())
        .merge(webhook_endpoints::router())
        .merge(brokers::router())
        .merge(explore::router())
        .merge(curated_versions::router())
        .merge(watchlist::router())
        .merge(managers::router())
        .merge(curated_plans::router())
        .merge(curated_customize::router())
        .merge(curated_engage::router())
        .merge(curated_trending::router())
        .merge(curated_create::router())
        .merge(curated_from_screen::router())
        .merge(curated_investments::router())
        .merge(curated_sip::router())
        .merge(curated_costs::router())
        .merge(curated_drift::router())
        .merge(track_b::router())
        .merge(swing::router())
        .merge(vbt::router())
        .merge(twt::router())
        .merge(options::router())
        .merge(fno::router())
}
