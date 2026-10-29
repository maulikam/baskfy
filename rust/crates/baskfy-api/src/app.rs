//! The application: routes, Starlette's routing rules, and the middleware in `app.py`'s order.
//! Ports `baskfy_api.app.create_app` (the wiring; the routes live in [`crate::routes`]).

use std::sync::{Arc, OnceLock};

use axum::Router;
use axum::body::Body;
use axum::extract::{Request, State};
use axum::http::{HeaderValue, Method, StatusCode, Uri, header};
use axum::middleware::{Next, from_fn, from_fn_with_state};
use axum::response::{IntoResponse, Response};
use percent_encoding::{AsciiSet, CONTROLS, utf8_percent_encode};
use tower::{Layer, Service};

use crate::auth;
use crate::cors::{self, Cors};
use crate::http_cache;
use crate::logging::{self, REQUEST_ID, REQUEST_ID_HEADER};
use crate::problems::{self, OutsideMiddleware, PendingProblem};
use crate::ratelimit;
use crate::routes;
use crate::settings::API_PREFIX;
use crate::state::AppState;

/// `TITLE`.
pub const TITLE: &str = "Baskfy API";
/// `VERSION`.
pub const VERSION: &str = "0.1.0";

/// Characters re-encoded when the decoded path is put back into the URI: anything a URI path may
/// not carry raw, and `%` itself, so a later decode (axum's `Path`) yields Python's path exactly.
const PATH_UNSAFE: &AsciiSet = &CONTROLS
    .add(b' ')
    .add(b'"')
    .add(b'#')
    .add(b'%')
    .add(b'<')
    .add(b'>')
    .add(b'?')
    .add(b'[')
    .add(b'\\')
    .add(b']')
    .add(b'^')
    .add(b'`')
    .add(b'{')
    .add(b'|')
    .add(b'}');

/// What `urllib.parse.quote(url, safe=":/%#?=@[]!$&'()*+,;")` leaves alone (Starlette's
/// `RedirectResponse`): unreserved characters and the safe set.
const REDIRECT_QUOTE: &AsciiSet = &percent_encoding::NON_ALPHANUMERIC
    .remove(b'_')
    .remove(b'.')
    .remove(b'-')
    .remove(b'~')
    .remove(b':')
    .remove(b'/')
    .remove(b'%')
    .remove(b'#')
    .remove(b'?')
    .remove(b'=')
    .remove(b'@')
    .remove(b'[')
    .remove(b']')
    .remove(b'!')
    .remove(b'$')
    .remove(b'&')
    .remove(b'\'')
    .remove(b'(')
    .remove(b')')
    .remove(b'*')
    .remove(b'+')
    .remove(b',')
    .remove(b';');

/// The method a `HEAD` request is routed as: FastAPI's `APIRoute`s never answer `HEAD` (a GET
/// route is `{"GET"}` only), so `HEAD` must reach no handler — `405` on a known path, `404` on
/// an unknown one. axum would otherwise serve `HEAD` from the `GET` handler.
const HEAD_UNROUTED: &[u8] = b"BASKFY-HEAD";

/// `request.url.path`: uvicorn's `unquote(raw_path)` (UTF-8, invalid bytes replaced).
#[must_use]
pub fn decoded_path(uri: &Uri) -> String {
    percent_encoding::percent_decode_str(uri.path())
        .decode_utf8_lossy()
        .into_owned()
}

fn latin1(bytes: &[u8]) -> String {
    bytes.iter().map(|&b| char::from(b)).collect()
}

fn with_path(uri: &Uri, decoded: &str) -> Option<Uri> {
    let encoded = utf8_percent_encode(decoded, PATH_UNSAFE).to_string();
    let pq = match uri.query() {
        Some(q) => format!("{encoded}?{q}"),
        None => encoded,
    };
    let mut parts = uri.clone().into_parts();
    parts.path_and_query = Some(pq.parse().ok()?);
    Uri::from_parts(parts).ok()
}

/// Starlette routes on the decoded path: `/api/v1/meta%2Fcolumns` reaches `/meta/columns`.
/// Also routes `HEAD` as an unrouted method (see [`HEAD_UNROUTED`]).
async fn starlette_routing(mut request: Request, next: Next) -> Response {
    let decoded = decoded_path(request.uri());
    if let Some(uri) = with_path(request.uri(), &decoded) {
        *request.uri_mut() = uri;
    }
    if request.method() == Method::HEAD
        && let Ok(m) = Method::from_bytes(HEAD_UNROUTED)
    {
        *request.method_mut() = m;
    }
    let mut response = next.run(request).await;
    // axum names the allowed methods on a 405; Starlette's handler drops the exception's headers.
    response.headers_mut().remove(header::ALLOW);
    response
}

/// Marks a request sent through the router only to learn whether a path matches.
#[derive(Debug, Clone, Copy)]
struct Probe;

/// Marks the answer to a [`Probe`] that matched a route (any method).
#[derive(Debug, Clone, Copy)]
struct ProbeHit;

fn probe_hit() -> Response {
    let mut r = StatusCode::NO_CONTENT.into_response();
    r.extensions_mut().insert(ProbeHit);
    r
}

/// Outermost route layer: a probe stops here, before any dependency or handler runs.
async fn probe_layer(request: Request, next: Next) -> Response {
    if request.extensions().get::<Probe>().is_some() {
        return probe_hit();
    }
    next.run(request).await
}

/// FastAPI's router-level dependency on `/api/v1` (`Depends(enforce_rate_limit)`): resolve the
/// principal (which may refuse the token), then charge the rate limit. Only matched routes run
/// it — an unrouted path is a 404 without touching the database or the bucket.
async fn versioned_dependencies(
    State(state): State<AppState>,
    mut request: Request,
    next: Next,
) -> Response {
    let authorization = request
        .headers()
        .get(header::AUTHORIZATION)
        .map(|v| latin1(v.as_bytes()));
    let principal =
        match auth::current_principal(authorization.as_deref(), &state.settings, &state.db).await {
            Ok(p) => p,
            Err(problem) => return problem.into_response(),
        };
    if let Some(limiter) = &state.limiter {
        let forwarded = request
            .headers()
            .get("x-forwarded-for")
            .map(|v| latin1(v.as_bytes()));
        let peer = request
            .extensions()
            .get::<axum::extract::ConnectInfo<std::net::SocketAddr>>()
            .map(|c| c.0);
        let ip = ratelimit::client_ip(forwarded.as_deref(), peer);
        let path = decoded_path(request.uri());
        if let Err(problem) = limiter
            .enforce(&principal, ip.as_deref(), &path, &state.settings)
            .await
        {
            return problem.into_response();
        }
    }
    request.extensions_mut().insert(principal);
    next.run(request).await
}

/// Renders a handler's [`Problem`](problems::Problem) with the request path as `instance`
/// (FastAPI's exception handlers read `request.url.path`).
async fn problem_layer(request: Request, next: Next) -> Response {
    let path = decoded_path(request.uri());
    let response = next.run(request).await;
    match response.extensions().get::<PendingProblem>() {
        Some(PendingProblem(problem)) => (**problem).clone().into_response_for(&path),
        None => response,
    }
}

/// The router itself, kept so the fallback can probe the slash-toggled path.
type RouterCell = Arc<OnceLock<Router>>;

/// Starlette's `Router.app` after no route matched: the slash redirect, else the 404.
async fn fallback(cell: RouterCell, request: Request) -> Response {
    if request.extensions().get::<Probe>().is_some() {
        return StatusCode::NOT_FOUND.into_response();
    }
    let path = decoded_path(request.uri());
    if path != "/"
        && let Some(router) = cell.get()
    {
        let toggled = if path.ends_with('/') {
            path.trim_end_matches('/').to_owned()
        } else {
            format!("{path}/")
        };
        if let Some(uri) = with_path(request.uri(), &toggled) {
            let mut probe = Request::new(Body::empty());
            *probe.method_mut() = request.method().clone();
            *probe.uri_mut() = uri;
            probe.extensions_mut().insert(Probe);
            let answer = router.clone().call(probe).await;
            if let Ok(answer) = answer
                && answer.extensions().get::<ProbeHit>().is_some()
            {
                return redirect(&request, &toggled);
            }
        }
    }
    problems::framework(404, "Not Found").into_response()
}

/// `RedirectResponse(url=str(URL(scope=redirect_scope)))`: 307, `location` quoted as
/// Starlette quotes it, an empty body.
fn redirect(request: &Request, path: &str) -> Response {
    let host = request
        .headers()
        .get(header::HOST)
        .map_or_else(|| "127.0.0.1".to_owned(), |h| latin1(h.as_bytes()));
    let mut url = format!("http://{host}{path}");
    if let Some(q) = request.uri().query() {
        url.push('?');
        url.push_str(q);
    }
    let quoted = utf8_percent_encode(&url, REDIRECT_QUOTE).to_string();
    let mut response = StatusCode::TEMPORARY_REDIRECT.into_response();
    if let Ok(v) = HeaderValue::from_str(&quoted) {
        response.headers_mut().insert(header::LOCATION, v);
    }
    response
}

async fn method_not_allowed(request: Request) -> Response {
    if request.extensions().get::<Probe>().is_some() {
        return probe_hit();
    }
    problems::framework(405, "Method Not Allowed").into_response()
}

/// `_request_context`: honour or mint `X-Request-Id`, keep it for the logs, echo it back.
async fn request_context(request: Request, next: Next) -> Response {
    let inbound = request
        .headers()
        .get(REQUEST_ID_HEADER)
        .filter(|v| !v.is_empty())
        .cloned();
    let request_id = inbound
        .as_ref()
        .map_or_else(logging::new_request_id, |v| latin1(v.as_bytes()));
    let mut response = REQUEST_ID
        .scope(request_id.clone(), next.run(request))
        .await;
    if response.extensions().get::<OutsideMiddleware>().is_none() {
        let value = inbound.or_else(|| HeaderValue::from_str(&request_id).ok());
        if let Some(v) = value {
            response.headers_mut().insert(REQUEST_ID_HEADER, v);
        }
    }
    response
}

/// uvicorn's access line: `127.0.0.1:49168 - "GET /health HTTP/1.1" 200`.
async fn access_log(request: Request, next: Next) -> Response {
    let peer = request
        .extensions()
        .get::<axum::extract::ConnectInfo<std::net::SocketAddr>>()
        .map_or_else(|| "-".to_owned(), |c| c.0.to_string());
    let line = format!(
        "{peer} - \"{} {} {:?}\"",
        request.method(),
        request.uri().path_and_query().map_or("/", |p| p.as_str()),
        request.version()
    );
    let response = next.run(request).await;
    logging::info(
        "uvicorn.access",
        &format!("{line} {}", response.status().as_u16()),
        &[],
    );
    response
}

/// `create_app(settings)`: every route, the fallbacks, and the middleware stack. Serve it with
/// `into_make_service_with_connect_info::<SocketAddr>()` so the rate limit sees the peer.
pub fn build_app(state: AppState) -> Router {
    let cell: RouterCell = Arc::new(OnceLock::new());
    let api = routes::api_router()
        .route_layer(from_fn_with_state(state.clone(), versioned_dependencies))
        .route_layer(from_fn(probe_layer));
    let ops = routes::ops::router().route_layer(from_fn(probe_layer));
    let fallback_cell = Arc::clone(&cell);
    let router: Router = Router::new()
        .nest(API_PREFIX, api)
        .merge(ops)
        .fallback(move |request: Request| fallback(Arc::clone(&fallback_cell), request))
        .method_not_allowed_fallback(method_not_allowed)
        .with_state(state.clone());
    // The probe goes through routing only: no problem rendering, no middleware.
    let _ = cell.set(router.clone());
    let stale = state.settings.http_stale_while_revalidate_seconds;
    let cors = Arc::new(Cors::new(&state.settings.cors_origins));
    // `Router::layer` runs *after* routing, so the decoded-path rewrite (which must decide the
    // route) wraps the router as a service instead; the outer layers then wrap that.
    let routed = from_fn(starlette_routing).layer(router.layer(from_fn(problem_layer)));
    Router::new()
        .fallback_service(routed)
        .layer(from_fn(move |r, n| http_cache::layer(stale, r, n)))
        .layer(from_fn(request_context))
        .layer(from_fn_with_state(cors, |State(c), r, n| {
            cors::layer(c, r, n)
        }))
        .layer(from_fn(access_log))
}
