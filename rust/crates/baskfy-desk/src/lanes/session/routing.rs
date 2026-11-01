//! Starlette's `Router(redirect_slashes=True)`, app-wide: a path no route matches whose
//! slash-toggled twin one does (any verb) is a 307 to the twin.

use axum::Router;
use axum::body::Body;
use axum::extract::Request;
use axum::http::{HeaderMap, Response, StatusCode, Uri, header};
use axum::middleware::Next;
use tower::ServiceExt as _;

use crate::app::AppState;
use crate::lanes::analytics::web::{self, redirect_307};

#[derive(Debug, Clone, Copy)]
struct NoRoute;

async fn matched(_req: Request, _next: Next) -> Response<Body> {
    Response::new(Body::empty())
}

async fn no_route() -> Response<Body> {
    let mut r = Response::new(Body::empty());
    *r.status_mut() = StatusCode::NOT_FOUND;
    r.extensions_mut().insert(NoRoute);
    r
}

/// A copy of `routes` that only answers whether a path matches: no handler ever runs.
pub fn probe(routes: Router<AppState>, state: AppState) -> Router {
    routes
        .route_layer(axum::middleware::from_fn(matched))
        .fallback(no_route)
        .with_state(state)
}

/// `str.rstrip("/")` for a trailing slash, else the path plus one.
#[must_use]
pub fn twin(path: &str) -> String {
    if path.ends_with('/') {
        path.trim_end_matches('/').to_owned()
    } else {
        format!("{path}/")
    }
}

fn percent_decode(raw: &str) -> String {
    let bytes = raw.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%'
            && let (Some(h), Some(l)) = (
                bytes.get(i + 1).and_then(|b| char::from(*b).to_digit(16)),
                bytes.get(i + 2).and_then(|b| char::from(*b).to_digit(16)),
            )
        {
            out.push(u8::try_from(h * 16 + l).unwrap_or(b'?'));
            i += 3;
            continue;
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// `URL(scope=redirect_scope)`: `http://<Host><twin>[?query]`.
#[must_use]
pub fn location(headers: &HeaderMap, uri: &Uri, twin_path: &str) -> String {
    let host = headers
        .get(header::HOST)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("127.0.0.1");
    let query = uri.query().map(|q| format!("?{q}")).unwrap_or_default();
    format!("http://{host}{}{query}", percent_decode(twin_path))
}

/// The redirect for this request, if its twin matches a route.
pub async fn redirect_for(probe: Router, headers: &HeaderMap, uri: &Uri) -> Option<Response<Body>> {
    let path = uri.path();
    if path == "/" {
        return None;
    }
    let twin_path = twin(path);
    if twin_path.is_empty() {
        return None;
    }
    let req = Request::builder()
        .method(axum::http::Method::GET)
        .uri(&twin_path)
        .body(Body::empty())
        .ok()?;
    let resp = probe.oneshot(req).await.ok()?;
    if resp.extensions().get::<NoRoute>().is_some() {
        return None;
    }
    Some(redirect_307(&location(headers, uri, &twin_path)))
}

/// The app's fallback: the slash redirect, else Starlette's 404.
pub async fn fallback(probe: Router, req: Request) -> Response<Body> {
    let (parts, _) = req.into_parts();
    match redirect_for(probe, &parts.headers, &parts.uri).await {
        Some(r) => r,
        None => web::detail(StatusCode::NOT_FOUND, "Not Found"),
    }
}

/// Redirect to the twin unconditionally (a path whose twin is a mount axum cannot see).
pub async fn toggle(headers: HeaderMap, uri: Uri) -> Response<Body> {
    redirect_307(&location(&headers, &uri, &twin(uri.path())))
}
