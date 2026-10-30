//! The write routes of `baskfy_api.routers.brokers`: `POST /brokers/{broker_id}/connect` and
//! `POST /brokers/{broker_id}/sync-holdings`. Neither reaches an order path (law 2): connect
//! builds a login URL, sync-holdings reads holdings.
//!
//! **Ported in full:** connect (every refusal, and the live branch: a one-time `state` minted and
//! registered as `broker_oauth.register_oauth_state` does, in this process and — when
//! `BASKFY_BROKER_OAUTH_STATE_PATH` is set — in the same file Python's callback re-reads), and
//! every sync-holdings answer that needs no broker session (an unwired broker, `DRY_RUN`, no app
//! key, no stored session, an unusable session; fixture rows from
//! `BASKFY_BROKER_HOLDINGS_FIXTURE`). **Not ported:** the live Kite holdings read and the write
//! into the broker's holding group that follows it (`broker_holdings_sync`): with a fresh stored
//! session, `DRY_RUN=false` and an app key, Rust answers 503 and Python keeps serving the route
//! (a cutover gate, DECISIONS-RS R6.22).

use std::sync::{LazyLock, Mutex};

use axum::extract::{Extension, Path, State};
use axum::response::Response;
use base64::Engine;
use baskfy_core::decimal::WideDecimal;
use baskfy_db::OrderedJson;
use rand::RngCore;
use serde::Serialize;

use super::{
    BROKER_OAUTH_REVIEW, BROKERS, BrokerDef, OAUTH_COMPLETABLE, env_stripped, token_store_path,
};
use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, ProblemType};
use crate::query::Validation;
use crate::routes::portfolios::portfolio::{params, pyfmt, tenant};
use crate::state::AppState;
use crate::wire;

/// `KITE_AUTHORIZE_URL`.
const KITE_AUTHORIZE_URL: &str = "https://kite.zerodha.com/connect/login";
/// `STATE_TTL_SECONDS`.
const STATE_TTL_SECONDS: f64 = 30.0 * 60.0;

/// `_WIRED_AUTHORIZE`.
fn authorize_base(broker_id: &str) -> Option<&'static str> {
    match broker_id {
        "zerodha" => Some(KITE_AUTHORIZE_URL),
        "upstox" => Some("https://api.upstox.com/v2/login/authorization/dialog"),
        "angelone" => Some("https://smartapi.angelbroking.com/publisher-login"),
        "fyers" => Some("https://api-t1.fyers.in/api/v3/generate-authcode"),
        "dhan" => Some("https://auth.dhan.co/login/consentApp-login"),
        _ => None,
    }
}

/// `ConnectOut`.
#[derive(Debug, Clone, Serialize)]
struct ConnectOut {
    broker_id: String,
    oauth_available: bool,
    redirect_url: Option<String>,
    state: Option<String>,
    reason: String,
}

fn refused(broker_id: &str, reason: String) -> ConnectOut {
    ConnectOut {
        broker_id: broker_id.to_owned(),
        oauth_available: false,
        redirect_url: None,
        state: None,
        reason,
    }
}

fn not_found_broker(broker_id: &str) -> Problem {
    Problem::new(
        ProblemType::NotFound,
        format!(
            "No broker with id {}.",
            baskfy_providers::py::repr_str(broker_id)
        ),
    )
}

/// The path's `broker_id` (`Path(min_length=2, max_length=32)`).
fn validate_broker_id(raw: &str) -> Result<(), Problem> {
    let mut v = Validation::new();
    params::str_value(&mut v, "broker_id", raw, Some(2), Some(32));
    v.finish()
}

/// One pending OAuth state (`OauthPending`).
#[derive(Debug, Clone)]
struct Pending {
    state: String,
    user_id: i64,
    broker_id: String,
    created_at: f64,
}

/// `_pending`, in insertion order.
static PENDING: LazyLock<Mutex<Vec<Pending>>> = LazyLock::new(|| Mutex::new(Vec::new()));

fn now_seconds() -> f64 {
    let now = crate::entitlements::utc_now();
    #[allow(clippy::cast_precision_loss)]
    let seconds = now.timestamp() as f64 + f64::from(now.timestamp_subsec_micros()) / 1e6;
    seconds
}

/// `_load_states_locked`: merge the state file's unexpired entries into memory.
fn load_states(pending: &mut Vec<Pending>, path: &std::path::Path) {
    let Ok(text) = std::fs::read_to_string(path) else {
        return;
    };
    let Ok(OrderedJson::Object(entries)) = OrderedJson::parse(&text) else {
        return;
    };
    let now = now_seconds();
    for (state, body) in entries {
        let OrderedJson::Object(fields) = body else {
            continue;
        };
        let get = |k: &str| fields.iter().find(|(n, _)| n == k).map(|(_, v)| v);
        let (Some(s), Some(user), Some(broker), Some(created)) = (
            get("state"),
            get("user_id"),
            get("broker_id"),
            get("created_at"),
        ) else {
            continue;
        };
        let created_at = match created {
            OrderedJson::Float(f) => *f,
            #[allow(clippy::cast_precision_loss)]
            OrderedJson::Int(i) => *i as f64,
            _ => continue,
        };
        let OrderedJson::Int(user_id) = user else {
            continue;
        };
        if now - created_at <= STATE_TTL_SECONDS {
            let entry = Pending {
                state: pyfmt::py_str(s),
                user_id: *user_id,
                broker_id: pyfmt::py_str(broker),
                created_at,
            };
            match pending.iter_mut().find(|p| p.state == state) {
                Some(slot) => *slot = entry,
                None => pending.push(entry),
            }
        }
    }
}

/// `_persist_states_locked`: `json.dumps(payload)`, owner-only.
fn persist_states(pending: &[Pending], path: &std::path::Path) {
    let body: Vec<String> = pending
        .iter()
        .map(|p| {
            let s = serde_json::to_string(&p.state).unwrap_or_default();
            let b = serde_json::to_string(&p.broker_id).unwrap_or_default();
            format!(
                "{s}: {{\"state\": {s}, \"user_id\": {}, \"broker_id\": {b}, \"created_at\": {}}}",
                p.user_id,
                baskfy_core::py::float_repr(p.created_at)
            )
        })
        .collect();
    if let Some(parent) = path.parent()
        && !parent.as_os_str().is_empty()
    {
        let _ = std::fs::create_dir_all(parent);
    }
    let text = format!("{{{}}}", body.join(", "));
    if let Err(e) = std::fs::write(path, text) {
        crate::logging::warning(
            "baskfy_api.broker_oauth",
            "oauth state file not written",
            &[("error", &e.to_string())],
        );
        return;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
    }
}

/// `register_oauth_state(state=, user_id=, broker_id=)`.
fn register_oauth_state(state: &str, user_id: i64, broker_id: &str) {
    let file = env_stripped("BASKFY_BROKER_OAUTH_STATE_PATH");
    let file = (!file.is_empty()).then(|| std::path::PathBuf::from(file));
    let created_at = now_seconds();
    let Ok(mut pending) = PENDING.lock() else {
        return;
    };
    if let Some(path) = &file {
        load_states(&mut pending, path);
    }
    pending.retain(|p| created_at - p.created_at <= STATE_TTL_SECONDS);
    let entry = Pending {
        state: state.to_owned(),
        user_id,
        broker_id: broker_id.to_owned(),
        created_at,
    };
    match pending.iter_mut().find(|p| p.state == state) {
        Some(slot) => *slot = entry,
        None => pending.push(entry),
    }
    if let Some(path) = &file {
        persist_states(&pending, path);
    }
}

/// `urllib.parse.quote_plus`.
fn quote_plus(s: &str) -> String {
    const SAFE: &percent_encoding::AsciiSet = &percent_encoding::NON_ALPHANUMERIC
        .remove(b'_')
        .remove(b'.')
        .remove(b'-')
        .remove(b'~')
        .remove(b' ');
    percent_encoding::utf8_percent_encode(s, SAFE)
        .to_string()
        .replace(' ', "+")
}

/// `kite_login_url(api_key=, user_id=, broker_id=, authorize_base=)`: `(url, state)`.
fn kite_login_url(api_key: &str, user_id: i64, broker_id: &str, base: &str) -> (String, String) {
    let mut bytes = [0u8; 24];
    rand::rng().fill_bytes(&mut bytes);
    let state = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes);
    register_oauth_state(&state, user_id, broker_id);
    let redirect_params = format!("state={}", quote_plus(&state));
    let query = format!(
        "api_key={}&v=3&redirect_params={}",
        quote_plus(api_key),
        quote_plus(&redirect_params)
    );
    (format!("{base}?{query}"), state)
}

/// `kite_login_url(api_key=, user_id=)` with its defaults (`broker_id="zerodha"`,
/// `authorize_base=KITE_AUTHORIZE_URL`): the one builder, reached by the worker's 08:45 login
/// nudge through [`super::kite_login_url_for_worker`] (wave 6, lane ops_alerts).
pub(crate) fn kite_login_url_default(api_key: &str, user_id: i64) -> (String, String) {
    kite_login_url(api_key, user_id, "zerodha", KITE_AUTHORIZE_URL)
}

/// `POST /brokers/{broker_id}/connect`: begin the redirect flow, or explain why it cannot start.
///
/// # Errors
/// 401 anonymous; 400 for an id outside 2..=32 characters; 404 for an unknown broker.
pub async fn connect_broker(
    Extension(principal): Extension<Principal>,
    Path(broker_id): Path<String>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    validate_broker_id(&broker_id)?;
    let user_id = principal.require_user()?;
    let Some(broker) = BROKERS.iter().find(|b| b.id == broker_id) else {
        return Err(not_found_broker(&broker_id));
    };
    if !BROKER_OAUTH_REVIEW.signed_off {
        return Ok(wire::json(&refused(
            &broker_id,
            "Live broker login opens after Baskfy's regulatory posture (D3) is recorded. The \
             catalog and this page are ready; the redirect is not."
                .to_owned(),
        )));
    }
    let Some(base) = authorize_base(&broker_id) else {
        return Ok(wire::json(&refused(
            &broker_id,
            format!(
                "{} is on the catalog; its OAuth adapter is not wired yet.",
                broker.name
            ),
        )));
    };
    if !OAUTH_COMPLETABLE.contains(&broker_id.as_str()) {
        return Ok(wire::json(&refused(
            &broker_id,
            format!(
                "{name}'s authorize URL is known, but Baskfy holds no {name} app credential and \
                 cannot redeem its request_token, so the login would start and could not finish. \
                 Zerodha is the only broker whose OAuth completes today.",
                name = broker.name
            ),
        )));
    }
    let api_key = env_stripped("BASKFY_KITE_API_KEY");
    let api_secret = env_stripped("BASKFY_KITE_API_SECRET");
    if api_key.is_empty() || api_secret.is_empty() {
        let absent: Vec<&str> = [
            ("BASKFY_KITE_API_KEY", &api_key),
            ("BASKFY_KITE_API_SECRET", &api_secret),
        ]
        .iter()
        .filter(|(_, v)| v.is_empty())
        .map(|(n, _)| *n)
        .collect();
        let verb = if absent.len() == 1 { "is" } else { "are" };
        return Ok(wire::json(&refused(
            &broker_id,
            format!(
                "Connecting {} needs a Kite **Connect** app, and {} {verb} not set on this \
                 deployment. A Kite **Publisher** key will not do: Publisher embeds a basket you \
                 confirm inside Kite and issues no API secret, so it cannot complete the token \
                 exchange this login ends with. Baskfy's basket hand-off uses Publisher and is \
                 unaffected.",
                broker.name,
                absent.join(" and ")
            ),
        )));
    }
    let (url, state) = kite_login_url(&api_key, user_id, &broker_id, base);
    Ok(wire::json(&ConnectOut {
        broker_id,
        oauth_available: true,
        redirect_url: Some(url),
        state: Some(state),
        reason: String::new(),
    }))
}

/// `HoldingRow`, as `holding_row_to_dict` puts it on the wire (`BrokerHoldingOut`).
#[derive(Debug, Clone, Serialize)]
struct BrokerHoldingOut {
    symbol: String,
    exchange: String,
    quantity: String,
    t1_quantity: String,
    collateral_quantity: String,
    average_price: String,
    last_price: Option<String>,
    product: String,
    total_quantity: String,
}

/// `SyncHoldingsOut`.
#[derive(Debug, Clone, Serialize)]
struct SyncHoldingsOut {
    broker_id: String,
    holdings: Vec<BrokerHoldingOut>,
    dry_run: bool,
    persisted: bool,
    written: i64,
    portfolio_id: Option<i64>,
    sync_note: String,
    unresolved: Vec<String>,
    source: &'static str,
    degraded: bool,
    note: String,
}

/// `HoldingsResult`.
struct HoldingsResult {
    rows: Vec<BrokerHoldingOut>,
    source: &'static str,
    degraded: bool,
    detail: String,
}

/// `dry_run_enabled()`: only an explicit false opts out.
pub(crate) fn dry_run_enabled() -> bool {
    let raw = std::env::var("DRY_RUN")
        .unwrap_or_else(|_| "true".to_owned())
        .trim()
        .to_lowercase();
    ["", "1", "true", "yes", "on"].contains(&raw.as_str())
}

/// `Decimal(str(value))` for a decoded JSON value: `None` is the `InvalidOperation` Python does
/// not catch (a 500).
fn decimal_of(value: &OrderedJson) -> Option<WideDecimal> {
    let text = match value {
        OrderedJson::Str(s) => s.clone(),
        other => pyfmt::py_str(other),
    };
    match crate::routes::portfolios::portfolio::model::py_decimal(&text) {
        crate::routes::portfolios::portfolio::model::PyDecimal::Finite(d) => Some(d),
        _ => None,
    }
}

/// `_fixture_holdings()`: rows from `BASKFY_BROKER_HOLDINGS_FIXTURE`, or `Err` for Python's
/// uncaught `InvalidOperation`.
fn fixture_holdings() -> Result<Vec<BrokerHoldingOut>, Problem> {
    let raw = env_stripped("BASKFY_BROKER_HOLDINGS_FIXTURE");
    if raw.is_empty() {
        return Ok(Vec::new());
    }
    let Ok(text) = std::fs::read_to_string(&raw) else {
        return Ok(Vec::new());
    };
    let Ok(OrderedJson::Array(items)) = OrderedJson::parse(&text) else {
        return Ok(Vec::new());
    };
    let mut rows = Vec::new();
    for item in &items {
        let OrderedJson::Object(fields) = item else {
            continue;
        };
        let get = |k: &str| fields.iter().rev().find(|(n, _)| n == k).map(|(_, v)| v);
        let Some(symbol) = get("symbol") else {
            continue;
        };
        let text_of =
            |k: &str, default: &str| get(k).map_or_else(|| default.to_owned(), pyfmt::py_str);
        let zero = OrderedJson::Int(0);
        let num = |k: &str, default: &OrderedJson| -> Result<WideDecimal, Problem> {
            decimal_of(get(k).unwrap_or(default))
                .ok_or_else(|| super::super::portfolios::portfolio::fail("InvalidOperation"))
        };
        let quantity = num("quantity", &zero)?;
        let t1 = num("t1_quantity", &zero)?;
        let collateral = num("collateral_quantity", &zero)?;
        let average = num("average_price", &OrderedJson::Str("0".to_owned()))?;
        let last = match get("last_price") {
            None | Some(OrderedJson::Null) => None,
            Some(v) => Some(
                decimal_of(v)
                    .ok_or_else(|| super::super::portfolios::portfolio::fail("InvalidOperation"))?,
            ),
        };
        rows.push(BrokerHoldingOut {
            symbol: crate::routes::portfolios::portfolio::model::py_strip(&pyfmt::py_str(symbol))
                .to_uppercase(),
            exchange: crate::routes::portfolios::portfolio::model::py_strip(&text_of(
                "exchange", "NSE",
            ))
            .to_uppercase(),
            total_quantity: quantity.add(&t1).add(&collateral).to_sci_string(),
            quantity: quantity.to_sci_string(),
            t1_quantity: t1.to_sci_string(),
            collateral_quantity: collateral.to_sci_string(),
            average_price: average.to_sci_string(),
            last_price: last.map(|l| l.to_sci_string()),
            product: text_of("product", "CNC"),
        });
    }
    Ok(rows)
}

fn fixture_result(detail: &str, degraded: bool) -> Result<HoldingsResult, Problem> {
    let rows = fixture_holdings()?;
    let source = if rows.is_empty() { "empty" } else { "fixture" };
    Ok(HoldingsResult {
        rows,
        source,
        degraded,
        detail: detail.to_owned(),
    })
}

/// `holdings_for_broker(broker_id)`, every branch that reads no broker. `Err` for the live read.
fn holdings_for_broker(broker_id: &str) -> Result<HoldingsResult, Problem> {
    if broker_id != "zerodha" {
        return Ok(HoldingsResult {
            rows: Vec::new(),
            source: "unwired",
            degraded: false,
            detail: String::new(),
        });
    }
    if dry_run_enabled() {
        return fixture_result("", false);
    }
    if env_stripped("BASKFY_KITE_API_KEY").is_empty() {
        return fixture_result("no broker app key is configured on this deployment", false);
    }
    let store = baskfy_providers::tokens::AccessTokenStore::new(
        token_store_path(),
        env_stripped("BASKFY_KITE_TOKEN_ENCRYPTION_KEY").into(),
    );
    if !store.exists() {
        return fixture_result("this account has not connected a broker session yet", false);
    }
    if let Err(e) = store.require_fresh(None) {
        let kind = e.python_type().to_owned();
        crate::logging::warning(
            "baskfy_api.broker_holdings",
            "holdings: stored broker session unusable; serving fixture",
            &[("broker_id", broker_id), ("error", &kind)],
        );
        return fixture_result(
            &format!("the stored broker session could not be used ({kind})"),
            true,
        );
    }
    Err(live_read_not_ported())
}

/// The cutover gate: a live broker read is served by the Python API (DECISIONS-RS R6.22).
pub(crate) fn live_read_not_ported() -> Problem {
    crate::logging::warning(
        "baskfy_api.routers.brokers",
        "a live broker session exists; the live read is not ported, Python serves it",
        &[],
    );
    Problem::new(
        ProblemType::PipelineDegraded,
        "This deployment's live broker read is served by the Python API.",
    )
}

/// `_holdings_note(result, broker_name=, dry_run=)`.
fn holdings_note(result: &HoldingsResult, broker_name: &str, dry_run: bool) -> String {
    let text = match (result.source, result.degraded) {
        ("live", _) => format!("live holdings reported by {broker_name}"),
        ("unwired", _) => format!("unwired: {broker_name} has no holdings adapter in this build"),
        ("fixture", true) => format!(
            "degraded: {broker_name} could not be reached, so these are fixture holdings — \
             sample numbers, not your holdings"
        ),
        ("fixture", false) => {
            "fixture holdings (DRY_RUN or BASKFY_BROKER_HOLDINGS_FIXTURE)".to_owned()
        }
        (_, true) => format!(
            "degraded: {broker_name} could not be reached and no fixture is configured, so the \
             list is empty"
        ),
        _ if dry_run => {
            "DRY_RUN: empty holdings (set BASKFY_BROKER_HOLDINGS_FIXTURE for a fixture list)"
                .to_owned()
        }
        _ => "empty: no holdings were returned for this broker".to_owned(),
    };
    if result.detail.is_empty() {
        text
    } else {
        format!("{text} ({})", result.detail)
    }
}

/// `POST /brokers/{broker_id}/sync-holdings`: holdings labelled with where they came from.
///
/// # Errors
/// 401 anonymous; 400 for the id; the sole-tenant guard's 503/404; 404 for an unknown broker;
/// 503 where a live session would be read (not ported).
pub async fn sync_holdings(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(broker_id): Path<String>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    validate_broker_id(&broker_id)?;
    let user_id = principal.require_user()?;
    tenant::scoped_sole_user_id(&state, Some(user_id), Some("broker holdings sync")).await?;
    let Some(broker): Option<&BrokerDef> = BROKERS.iter().find(|b| b.id == broker_id) else {
        return Err(not_found_broker(&broker_id));
    };
    let result = holdings_for_broker(&broker_id)?;
    let dry = dry_run_enabled();
    let note = holdings_note(&result, broker.name, dry);
    let sync_note = format!(
        "not persisted: holdings source is {}{}. Only a live read is written to a portfolio — a \
         fixture behind a real total would be indistinguishable from your own positions.",
        baskfy_providers::py::repr_str(result.source),
        if result.degraded { " and degraded" } else { "" }
    );
    Ok(wire::json(&SyncHoldingsOut {
        broker_id,
        holdings: result.rows,
        dry_run: dry,
        persisted: false,
        written: 0,
        portfolio_id: None,
        sync_note,
        unresolved: Vec::new(),
        source: result.source,
        degraded: result.degraded,
        note,
    }))
}
