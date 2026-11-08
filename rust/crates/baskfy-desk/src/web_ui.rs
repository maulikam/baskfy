//! DESK-UI (6 Nov 2026): the console's HTML pages live in the web app now. A browser GET for a
//! page is sent there; JSON reads (`Accept: application/json`) and every POST are untouched.
//! Outermost layer, so `path_prefix` never prefixes these Locations with `/desk`.

use axum::body::Body;
use axum::extract::Request;
use axum::http::header::{ACCEPT, LOCATION};
use axum::http::{HeaderValue, Method, Response, StatusCode};
use axum::middleware::Next;

/// Desk page → web route. `/ops/job/{id}` is handled by prefix below.
const PAGES: [(&str, &str); 16] = [
    ("/", "/rebalance"),
    ("/indices", "/market/indices"),
    ("/performance", "/performance"),
    ("/regime", "/regime"),
    ("/regime/backtest", "/regime/backtest"),
    ("/tradebook", "/tradebook"),
    ("/stops", "/stops"),
    ("/reconcile", "/reconcile"),
    ("/lifecycle", "/lifecycle"),
    ("/swing", "/swing"),
    ("/twt", "/twt"),
    ("/vbt", "/vbt"),
    ("/fno", "/options/desk"),
    ("/nifty-options", "/options/nifty"),
    ("/ops", "/ops"),
    ("/settings", "/desk-settings"),
];

/// True when `BASKFY_DESK_WEB_UI=true`; off by default so the HTML pages (and their tests) stay.
#[must_use]
pub fn enabled() -> bool {
    std::env::var("BASKFY_DESK_WEB_UI").is_ok_and(|v| v.eq_ignore_ascii_case("true"))
}

/// The web route for a desk page path, if it has one.
#[must_use]
pub fn target(path: &str) -> Option<String> {
    let trimmed = if path.len() > 1 {
        path.trim_end_matches('/')
    } else {
        path
    };
    if let Some(id) = trimmed.strip_prefix("/ops/job/") {
        return (!id.is_empty() && !id.contains('/')).then(|| format!("/ops/job/{id}"));
    }
    PAGES
        .iter()
        .find(|(desk, _)| *desk == trimmed)
        .map(|(_, web)| (*web).to_owned())
}

/// The middleware: a browser GET for a moved page gets a 303 to its web route.
pub async fn layer(request: Request, next: Next) -> Response<Body> {
    let wants_json = request
        .headers()
        .get(ACCEPT)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.contains("application/json"));
    if request.method() == Method::GET
        && !wants_json
        && let Some(web) = target(request.uri().path())
    {
        let location = match request.uri().query() {
            Some(q) if !q.is_empty() => format!("{web}?{q}"),
            _ => web,
        };
        if let Ok(v) = HeaderValue::from_str(&location) {
            let mut r = Response::new(Body::empty());
            *r.status_mut() = StatusCode::SEE_OTHER;
            r.headers_mut().insert(LOCATION, v);
            return r;
        }
    }
    next.run(request).await
}
