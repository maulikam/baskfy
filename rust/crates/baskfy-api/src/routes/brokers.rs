//! ``/brokers`` — the connect catalog (M41 / P5.8). Ports the GET routes of
//! `baskfy_api.routers.brokers` (Python declares `APIRouter(prefix="/brokers", tags=["brokers"])`)
//! and the catalog in `baskfy_core.broker_connections`.
//!
//! Ported: `GET /brokers`, `GET /brokers/{broker_id}`, and `GET /brokers/callback` **up to its
//! refusals** — validation, the sole-tenant guard, the D3 gate and the one-time `state` check.
//! A `state` is minted only by `POST /brokers/{id}/connect`, which this wave does not serve, so
//! the Rust process holds no pending state and answers every callback "Invalid or expired OAuth
//! state." — what a second Python worker without the shared state file answers too. The success
//! path (token exchange, encrypted store, holdings sync) writes, and stays on Python until the
//! write wave; DECISIONS-RS, api-portfolio P5. The write routes `POST /{broker_id}/connect` and
//! `POST /{broker_id}/sync-holdings` are in [`writes`] (wave 4c; the live holdings read is a
//! cutover gate, DECISIONS-RS R6.22). Nothing here reaches an order path (law 2).

use axum::Router;
use axum::extract::{Extension, Path, RawQuery, State};
use axum::response::Response;
use axum::routing::{get, post};
use baskfy_db::OrderedJson;
use serde::Serialize;

pub(crate) mod writes;

use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, ProblemType};
use crate::query::{QueryParams, Validation};
use crate::routes::portfolios::portfolio::{params, tenant};
use crate::state::AppState;
use crate::wire;

/// `OAUTH_CALLBACK_PATH`.
pub const OAUTH_CALLBACK_PATH: &str = "/api/v1/brokers/callback";

/// `SIMULATED_TOKEN_PREFIX` (`baskfy_api.broker_oauth`).
pub const SIMULATED_TOKEN_PREFIX: &str = "sim_";

/// `_WIRED_AUTHORIZE`'s keys: brokers whose authorize URL is known.
pub const WIRED_AUTHORIZE: [&str; 5] = ["zerodha", "upstox", "angelone", "fyers", "dhan"];

/// `_OAUTH_COMPLETABLE`: brokers whose OAuth can finish here.
pub const OAUTH_COMPLETABLE: [&str; 1] = ["zerodha"];

/// `BROKER_OAUTH_REVIEW` (docs/06 D3, posture B).
pub struct BrokerOauthReview {
    /// `requirement`.
    pub requirement: &'static str,
    /// `signed_off`.
    pub signed_off: bool,
    /// `decision_reference`.
    pub decision_reference: &'static str,
}

/// THE GATE — `baskfy_core.broker_connections.BROKER_OAUTH_REVIEW`.
pub const BROKER_OAUTH_REVIEW: BrokerOauthReview = BrokerOauthReview {
    requirement: "Posture B (DECISIONS-MERGE.md §D3): publish baskets; user executes in their own \
                  broker account after confirm. OAuth + encrypted holdings sync are allowed. The \
                  web app still never places orders — packages/execution only.",
    signed_off: true,
    decision_reference: "DECISIONS-MERGE.md §D3",
};

/// `BrokerDef` — one tile on the connect grid.
#[derive(Debug, Clone, Copy)]
pub struct BrokerDef {
    /// `id`.
    pub id: &'static str,
    /// `name`.
    pub name: &'static str,
    /// `short_name`.
    pub short_name: &'static str,
    /// `mark`.
    pub mark: &'static str,
    /// `color`.
    pub color: &'static str,
    /// `blurb`.
    pub blurb: &'static str,
    /// `api_name`.
    pub api_name: &'static str,
    /// `docs_url`.
    pub docs_url: &'static str,
    /// `capabilities` (`oauth`, `holdings_sync`, `trading`).
    pub capabilities: (&'static str, &'static str, &'static str),
    /// `sort_order`.
    pub sort_order: i64,
}

// One positional row per broker keeps the catalogue reading like Python's `BROKERS` tuple.
#[allow(clippy::too_many_arguments)]
const fn planned(
    id: &'static str,
    name: &'static str,
    short_name: &'static str,
    mark: &'static str,
    color: &'static str,
    blurb: &'static str,
    api_name: &'static str,
    docs_url: &'static str,
    oauth: &'static str,
    sort_order: i64,
) -> BrokerDef {
    BrokerDef {
        id,
        name,
        short_name,
        mark,
        color,
        blurb,
        api_name,
        docs_url,
        capabilities: (oauth, "planned", "planned"),
        sort_order,
    }
}

/// `BROKERS`, already in `broker_catalog()`'s `sort_order`.
pub const BROKERS: [BrokerDef; 10] = [
    BrokerDef {
        id: "zerodha",
        name: "Zerodha",
        short_name: "Zerodha",
        mark: "Z",
        color: "#387ed1",
        blurb: "Kite Publisher — send a basket to your own Kite and confirm it there. Holdings \
                are read back from the session your morning Kite login produces.",
        api_name: "Kite Publisher",
        docs_url: "https://kite.trade/docs/connect/v3/publisher/",
        capabilities: ("ready", "ready", "handoff"),
        sort_order: 1,
    },
    planned(
        "hdfc",
        "HDFC Securities",
        "HDFC",
        "H",
        "#004c8f",
        "HDFC Securities APIs — partner access; holdings import when empanelled.",
        "HDFC Securities API",
        "https://www.hdfcsec.com/",
        "partner",
        2,
    ),
    planned(
        "kotak",
        "Kotak Securities",
        "Kotak",
        "K",
        "#ed1c24",
        "Kotak Neo — planned: holdings sync once a Neo adapter and partner app key exist.",
        "Kotak Neo",
        "https://www.kotaksecurities.com/markets/trading-platforms/neo/",
        "ready",
        3,
    ),
    planned(
        "icici",
        "ICICI Direct",
        "ICICI",
        "I",
        "#f58220",
        "Breeze API — planned: holdings sync once a Breeze adapter and app key exist.",
        "Breeze API",
        "https://www.icicidirect.com/idirectcontent/Markets/MarketOverview.aspx",
        "ready",
        4,
    ),
    planned(
        "upstox",
        "Upstox",
        "Upstox",
        "U",
        "#5a2d82",
        "Upstox API v2 — planned: holdings sync once an Upstox adapter and app key exist.",
        "Upstox API",
        "https://upstox.com/developer/api-documentation/",
        "ready",
        5,
    ),
    planned(
        "angelone",
        "Angel One",
        "Angel",
        "A",
        "#e85d04",
        "SmartAPI — planned: holdings sync once a SmartAPI adapter and app key exist.",
        "SmartAPI",
        "https://smartapi.angelbroking.com/",
        "ready",
        6,
    ),
    planned(
        "groww",
        "Groww",
        "Groww",
        "G",
        "#00b386",
        "Groww — connection planned once a public trading API is generally available.",
        "Groww API",
        "https://groww.in/",
        "planned",
        7,
    ),
    planned(
        "fyers",
        "Fyers",
        "Fyers",
        "F",
        "#1a1a2e",
        "Fyers API v3 — planned: holdings sync once a Fyers adapter and app key exist.",
        "Fyers API",
        "https://myapi.fyers.in/docsv3",
        "ready",
        8,
    ),
    planned(
        "fivepaisa",
        "5paisa",
        "5paisa",
        "5",
        "#1d4ed8",
        "5paisa OpenAPI — planned: holdings sync once a 5paisa adapter and app key exist.",
        "5paisa OpenAPI",
        "https://www.5paisa.com/developerapi",
        "ready",
        9,
    ),
    planned(
        "dhan",
        "Dhan",
        "Dhan",
        "D",
        "#0f766e",
        "DhanHQ — planned: holdings sync once a Dhan adapter and access token exist.",
        "DhanHQ",
        "https://dhanhq.co/docs/",
        "ready",
        10,
    ),
];

/// `BrokerCapabilitiesOut`.
#[derive(Debug, Clone, Serialize)]
pub struct BrokerCapabilitiesOut {
    /// `oauth`.
    pub oauth: &'static str,
    /// `holdings_sync`.
    pub holdings_sync: &'static str,
    /// `trading`.
    pub trading: &'static str,
}

/// `BrokerOut`.
#[derive(Debug, Clone, Serialize)]
pub struct BrokerOut {
    /// `id`.
    pub id: &'static str,
    /// `name`.
    pub name: &'static str,
    /// `short_name`.
    pub short_name: &'static str,
    /// `mark`.
    pub mark: &'static str,
    /// `color`.
    pub color: &'static str,
    /// `blurb`.
    pub blurb: &'static str,
    /// `api_name`.
    pub api_name: &'static str,
    /// `docs_url`.
    pub docs_url: &'static str,
    /// `capabilities`.
    pub capabilities: BrokerCapabilitiesOut,
    /// `sort_order`.
    pub sort_order: i64,
    /// `connected`.
    pub connected: bool,
    /// `connection_status`.
    pub connection_status: &'static str,
    /// `adapter_wired`.
    pub adapter_wired: bool,
}

/// `BrokerGateOut`.
#[derive(Debug, Clone, Serialize)]
pub struct BrokerGateOut {
    /// `live_oauth_enabled`.
    pub live_oauth_enabled: bool,
    /// `requirement`.
    pub requirement: &'static str,
    /// `signed_off`.
    pub signed_off: bool,
    /// `decision_reference`.
    pub decision_reference: &'static str,
    /// `connect_configured`.
    pub connect_configured: bool,
}

/// `BrokerListOut`.
#[derive(Debug, Clone, Serialize)]
pub struct BrokerListOut {
    /// `gate`.
    pub gate: BrokerGateOut,
    /// `brokers`.
    pub brokers: Vec<BrokerOut>,
    /// `adapters_wired`.
    pub adapters_wired: usize,
}

pub(crate) fn env_stripped(name: &str) -> String {
    std::env::var(name).unwrap_or_default().trim().to_owned()
}

/// `_connect_configured`: key, secret, and a redirect that comes back to this deployment.
fn connect_configured(state: &AppState) -> bool {
    if env_stripped("BASKFY_KITE_API_KEY").is_empty()
        || env_stripped("BASKFY_KITE_API_SECRET").is_empty()
    {
        return false;
    }
    let origin = state.settings.web_origin.trim_end_matches('/');
    let configured = env_stripped("BASKFY_BROKER_OAUTH_REDIRECT");
    let redirect = if configured.is_empty() {
        format!("{origin}{OAUTH_CALLBACK_PATH}")
    } else {
        configured
    };
    redirect.starts_with(origin)
}

fn gate_out(state: &AppState) -> BrokerGateOut {
    BrokerGateOut {
        live_oauth_enabled: BROKER_OAUTH_REVIEW.signed_off,
        requirement: BROKER_OAUTH_REVIEW.requirement,
        signed_off: BROKER_OAUTH_REVIEW.signed_off,
        decision_reference: BROKER_OAUTH_REVIEW.decision_reference,
        connect_configured: connect_configured(state),
    }
}

/// `token_store_path()`: broker-specific env, then the shared Kite path, then the default.
pub(crate) fn token_store_path() -> std::path::PathBuf {
    let broker = env_stripped("BASKFY_BROKER_TOKEN_PATH");
    let kite = env_stripped("BASKFY_KITE_TOKEN_PATH");
    let raw = if !broker.is_empty() {
        broker
    } else if !kite.is_empty() {
        kite
    } else {
        ".secrets/broker-token.enc".to_owned()
    };
    std::path::PathBuf::from(raw)
}

/// `_connection_state`: is there a usable session behind this tile, right now? No network call.
fn connection_state(broker_id: &str) -> Result<(bool, &'static str), Problem> {
    if !OAUTH_COMPLETABLE.contains(&broker_id) {
        return Ok((false, "not_connected"));
    }
    let store = baskfy_providers::tokens::AccessTokenStore::new(
        token_store_path(),
        env_stripped("BASKFY_KITE_TOKEN_ENCRYPTION_KEY").into(),
    );
    let token = match store.load() {
        Ok(t) => t,
        Err(e) if e.kind() == baskfy_providers::ErrorKind::CredentialsMissing => {
            return Ok((false, "not_connected"));
        }
        Err(e) => {
            crate::logging::error(
                "baskfy_api.app",
                "unhandled error",
                &[("error", &e.to_string())],
            );
            return Err(crate::problems::internal_error());
        }
    };
    if token.value.expose().starts_with(SIMULATED_TOKEN_PREFIX) {
        return Ok((false, "simulated"));
    }
    let now = crate::entitlements::utc_now().with_timezone(&baskfy_providers::tokens::ist());
    if token.is_expired(now) {
        return Ok((false, "expired"));
    }
    Ok((true, "connected"))
}

fn broker_out(broker: &BrokerDef) -> Result<BrokerOut, Problem> {
    let (connected, status) = connection_state(broker.id)?;
    Ok(BrokerOut {
        id: broker.id,
        name: broker.name,
        short_name: broker.short_name,
        mark: broker.mark,
        color: broker.color,
        blurb: broker.blurb,
        api_name: broker.api_name,
        docs_url: broker.docs_url,
        capabilities: BrokerCapabilitiesOut {
            oauth: broker.capabilities.0,
            holdings_sync: broker.capabilities.1,
            trading: broker.capabilities.2,
        },
        sort_order: broker.sort_order,
        connected,
        connection_status: status,
        adapter_wired: WIRED_AUTHORIZE.contains(&broker.id),
    })
}

fn bad_request(detail: &str) -> Problem {
    Problem::new(ProblemType::BadRequest, detail).with(
        "errors",
        OrderedJson::Array(vec![OrderedJson::Object(vec![(
            "message".to_owned(),
            OrderedJson::Str(detail.to_owned()),
        )])]),
    )
}

/// `GET /brokers`: the ten brokers on the grid and the D3 gate. Never starts OAuth.
///
/// # Errors
/// 401 anonymous; 500 when a stored token cannot be read for a reason other than "absent".
pub async fn list_brokers(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?.require_user()?;
    let mut brokers = Vec::with_capacity(BROKERS.len());
    for b in &BROKERS {
        brokers.push(broker_out(b)?);
    }
    let adapters_wired = brokers.iter().filter(|b| b.adapter_wired).count();
    Ok(wire::json(&BrokerListOut {
        gate: gate_out(&state),
        brokers,
        adapters_wired,
    }))
}

/// `GET /brokers/{broker_id}`.
///
/// # Errors
/// 401 anonymous; 400 for an id outside 2..=32 characters; 404 for an unknown id.
pub async fn get_one_broker(
    Extension(principal): Extension<Principal>,
    Path(broker_id): Path<String>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let mut v = Validation::new();
    params::str_value(&mut v, "broker_id", &broker_id, Some(2), Some(32));
    v.finish()?;
    principal.require_user()?;
    let Some(broker) = BROKERS.iter().find(|b| b.id == broker_id) else {
        return Err(Problem::new(
            ProblemType::NotFound,
            format!(
                "No broker with id {}.",
                baskfy_providers::py::repr_str(&broker_id)
            ),
        ));
    };
    Ok(wire::json(&broker_out(broker)?))
}

/// `GET /brokers/callback?request_token=&state=` — the refusals only (see the module docs).
///
/// # Errors
/// 401 anonymous; 400 for missing or mis-sized parameters; the sole-tenant guard's 503/404; the
/// 400 "Invalid or expired OAuth state." for every state this process could hold.
pub async fn oauth_callback(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    if let Some(t) = v.required(&q, "request_token") {
        params::str_value(&mut v, "request_token", t, Some(8), Some(128));
    }
    if let Some(s) = v.required(&q, "state") {
        params::str_value(&mut v, "state", s, Some(8), Some(128));
    }
    v.finish()?;
    let user_id = principal.require_user()?;
    tenant::scoped_sole_user_id(&state, Some(user_id), Some("broker connect")).await?;
    if !BROKER_OAUTH_REVIEW.signed_off {
        return Err(bad_request(
            "Broker OAuth is not signed off; callback is closed.",
        ));
    }
    // consume_oauth_state(state): this process never minted one (connect is not served here).
    Err(bad_request("Invalid or expired OAuth state."))
}

/// The GET routes of `baskfy_api.routers.brokers`, relative to `/api/v1`. `/callback` is
/// registered before `/{broker_id}`, as in Python (axum prefers the static segment anyway).
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/brokers", get(list_brokers))
        .route("/brokers/callback", get(oauth_callback))
        .route("/brokers/{broker_id}", get(get_one_broker))
        // The write routes' paths, with no method: a GET there is Starlette's 405.
        .route("/brokers/{broker_id}/connect", post(writes::connect_broker))
        .route(
            "/brokers/{broker_id}/sync-holdings",
            post(writes::sync_holdings),
        )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_catalog_is_honest_about_wiring() {
        // Ports test_brokers.py's catalog assertions.
        assert_eq!(BROKERS.len(), 10);
        let order: Vec<i64> = BROKERS.iter().map(|b| b.sort_order).collect();
        assert_eq!(order, (1..=10).collect::<Vec<_>>());
        // Only Zerodha may claim a ready holdings sync.
        for b in &BROKERS {
            assert_eq!(b.capabilities.1 == "ready", b.id == "zerodha", "{}", b.id);
        }
        assert_eq!(
            BROKERS
                .iter()
                .filter(|b| WIRED_AUTHORIZE.contains(&b.id))
                .count(),
            5
        );
        // A broker without a completable OAuth never reads the token store.
        assert_eq!(
            connection_state("upstox").ok(),
            Some((false, "not_connected"))
        );
    }
}
