//! `/opportunities/*` — the scanner and the AG-UI Ask panel. Ports
//! `baskfy_api.routers.opportunities` (`APIRouter(prefix="/opportunities")`).
//!
//! READ-ONLY: no route here places, plans or confirms anything; the only broker reads are option
//! quotes and the NFO instrument master.

use axum::Router;
use axum::body::Body;
use axum::extract::{Extension, Request, State};
use axum::http::{HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use baskfy_core::opportunities::config::{OptionsParams, SetupType, StrategyKind, SwingFilters};

use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, ProblemType};
use crate::routes::sleeves::common::{J, Obj, canonical_response};
use crate::state::AppState;

mod agui;
mod schemas;
mod service;
mod views;

use schemas::{OptionsIn, SwingScanIn, ValidateIn};
use service::ScanError;

const SURFACE: &str = "opportunities";

async fn body_of(request: Request) -> Result<crate::body::Received, Problem> {
    let (content_type, bytes) = super::sleeves::writes::read_body(request).await?;
    crate::body::receive(content_type.as_deref(), &bytes)
}

fn sse_response(rx: tokio::sync::mpsc::Receiver<String>) -> Response {
    let stream = futures_util::stream::unfold(rx, |mut rx| async move {
        rx.recv()
            .await
            .map(|frame| (Ok::<_, std::convert::Infallible>(frame), rx))
    });
    let mut response = (StatusCode::OK, Body::from_stream(stream)).into_response();
    let headers = response.headers_mut();
    headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/event-stream; charset=utf-8"),
    );
    headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    response
}

fn scan_problem(e: ScanError) -> Problem {
    match e {
        ScanError::Unavailable(msg) => Problem::new(ProblemType::NotFound, msg),
        ScanError::Failed(p) => p,
        ScanError::Cancelled => crate::problems::internal_error(),
    }
}

fn swing_defaults() -> J {
    let d = SwingScanIn::default();
    Obj::new()
        .put("universe", J::Str(d.universe))
        .put("sectors", J::Arr(Vec::new()))
        .put("setups", J::Arr(d.setups.into_iter().map(J::Str).collect()))
        .put("min_turnover_cr", J::Float(d.min_turnover_cr))
        .put("min_price", J::Float(d.min_price))
        .put("horizon_min", J::Int(d.horizon_min))
        .put("horizon_max", J::Int(d.horizon_max))
        .put("atr_pct_min", J::Float(d.atr_pct_min))
        .put("atr_pct_max", J::Float(d.atr_pct_max))
        .put("min_rr", J::Float(d.min_rr))
        .put("limit", J::Int(d.limit))
        .done()
}

fn pairs(rows: Vec<(String, String)>) -> J {
    J::Arr(
        rows.into_iter()
            .map(|(slug, name)| Obj::new().put("slug", J::Str(slug)).put("name", J::Str(name)).done())
            .collect(),
    )
}

/// `GET /opportunities/meta`: scanner parameters and the options provider's state.
///
/// # Errors
/// 401 anonymous; 500 a database failure.
pub async fn get_meta(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let pool = state.db.pool();
    let universes = service::universes(pool).await?;
    let sectors = service::sectors(pool).await?;
    let provider = service::provider_state()?;
    let strategies = [
        StrategyKind::LongCall,
        StrategyKind::LongPut,
        StrategyKind::BullCallSpread,
        StrategyKind::BearPutSpread,
    ];
    let body = Obj::new()
        .put("universes", pairs(universes))
        .put("sectors", pairs(sectors))
        .put("setups", J::Arr(SetupType::ALL.iter().map(|s| J::s(s.value())).collect()))
        .put("strategies", J::Arr(strategies.iter().map(|s| J::s(s.value())).collect()))
        .put("swing_defaults", swing_defaults())
        .put("quote_rules_defaults", views::quote_rules_json(&Default::default()))
        .put("options_provider", views::provider_json(&provider))
        .put("limits", views::limits())
        .put(
            "options_params",
            Obj::new()
                .put("max_expiries", J::Int(OptionsParams::default().max_expiries))
                .done(),
        )
        .done();
    Ok(canonical_response(&body))
}

/// `POST /opportunities/swing/scan`: swing setups on completed daily bars.
///
/// # Errors
/// 401; 400 a bad body; 404 an unknown universe; 500.
pub async fn post_swing_scan(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    request: Request,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let body = SwingScanIn::from_body(&body_of(request).await?)?;
    let now = crate::entitlements::utc_now();
    let scan = service::run_swing_scan(state.db.pool(), &body.universe, &body.filters(), now, None)
        .await
        .map_err(scan_problem)?;
    Ok(canonical_response(&views::scan_json(&scan)))
}

/// `POST /opportunities/swing/scan/stream`: the scan with server-sent progress.
///
/// # Errors
/// 401; 400 a bad body.
pub async fn post_swing_scan_stream(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    request: Request,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let body = SwingScanIn::from_body(&body_of(request).await?)?;
    let (tx, rx) = tokio::sync::mpsc::channel::<String>(16);
    let db = state.db.clone();
    tokio::spawn(async move {
        let now = crate::entitlements::utc_now();
        let result =
            service::run_swing_scan(db.pool(), &body.universe, &body.filters(), now, Some(&tx)).await;
        let frame = match result {
            Ok(scan) => views::sse("result", &views::scan_json(&scan)),
            Err(ScanError::Unavailable(msg)) => views::sse(
                "error",
                &Obj::new()
                    .put("title", J::s("Not found"))
                    .put("detail", J::Str(msg))
                    .put("status", J::Int(404))
                    .done(),
            ),
            Err(ScanError::Cancelled | ScanError::Failed(_)) => return,
        };
        let _ = tx.send(frame).await;
    });
    Ok(sse_response(rx))
}

/// `POST /opportunities/swing/validate`: a point-in-time test of one setup's signals.
///
/// # Errors
/// 401; 400 a bad body; 500.
pub async fn post_swing_validate(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    request: Request,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let body = ValidateIn::from_body(&body_of(request).await?)?;
    let filters = SwingFilters {
        horizon_min: 1,
        horizon_max: body.horizon_max,
        min_rr: 0.0,
        ..SwingFilters::default()
    };
    let result = service::run_validation(
        state.db.pool(),
        body.setup,
        &body.universe,
        body.start,
        body.end,
        filters,
        body.slippage_bps,
    )
    .await;
    let out = match result {
        Ok(r) => views::validation_json(&r),
        Err(ScanError::Unavailable(msg)) => {
            views::unavailable_validation(body.setup.value(), body.start, body.end, &msg)
        }
        Err(e) => return Err(scan_problem(e)),
    };
    Ok(canonical_response(&out))
}

/// `POST /opportunities/options/analyze`: listed option structures for one underlying.
///
/// # Errors
/// 401; 400 a bad body; 404 not the sole tenant; 500.
pub async fn post_options_analyze(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    request: Request,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let body = OptionsIn::from_body(&body_of(request).await?)?;
    crate::routes::common::scoped_sole_user_id(&state.db, principal.user_id, Some(SURFACE)).await?;
    let now = crate::entitlements::utc_now();
    let analysis =
        service::run_options_analysis(&body.request(), now, body.rules.max_quote_age_s).await?;
    Ok(canonical_response(&views::options_json(&body.symbol, &analysis)?))
}

/// `POST /opportunities/agui`: an AG-UI run (conversational, read-only scanning).
///
/// # Errors
/// 401; 400 an invalid `RunAgentInput`; 404 a thread another user owns.
pub async fn post_agui(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    request: Request,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let (_, bytes) = super::sleeves::writes::read_body(request).await?;
    let run = agui::parse_run_input(&bytes).map_err(|e| {
        Problem::new(ProblemType::BadRequest, format!("Invalid AG-UI RunAgentInput: {e}"))
    })?;
    let user_id = principal.require_user()?;
    if !agui::claim_thread(&run.thread_id, user_id) {
        return Err(Problem::new(ProblemType::NotFound, "No such conversation."));
    }
    let (tx, rx) = tokio::sync::mpsc::channel::<String>(16);
    tokio::spawn(agui::run(run, state.db.clone(), user_id, tx));
    Ok(sse_response(rx))
}

/// The router (`prefix="/opportunities"`).
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/opportunities/meta", get(get_meta))
        .route("/opportunities/swing/scan", post(post_swing_scan))
        .route("/opportunities/swing/scan/stream", post(post_swing_scan_stream))
        .route("/opportunities/swing/validate", post(post_swing_validate))
        .route("/opportunities/options/analyze", post(post_options_analyze))
        .route("/opportunities/agui", post(post_agui))
}
