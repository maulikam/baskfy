//! CORS. Ports Starlette 1.6's `CORSMiddleware` as `baskfy_api.app` configures it.
//!
//! Hand-ported rather than taken from a crate because the wire details are Starlette's: a
//! preflight answers `200 text/plain "OK"` (or `400 "Disallowed CORS origin, method, headers"`),
//! the allowed headers are the configured ones **plus** the four CORS-safelisted ones, sorted;
//! `Vary: Origin` is appended to an existing `Vary`; and a simple response gets
//! `Access-Control-Allow-Credentials` and `-Expose-Headers` even when its origin is not allowed.

use axum::extract::Request;
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode, header};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};

use crate::problems::OutsideMiddleware;

/// `allow_methods` in `register_middleware`.
pub const ALLOW_METHODS: [&str; 5] = ["GET", "POST", "PATCH", "DELETE", "OPTIONS"];

/// `allow_headers` in `register_middleware`.
pub const ALLOW_HEADERS: [&str; 7] = [
    "Authorization",
    "Content-Type",
    "X-Request-Id",
    "Idempotency-Key",
    "X-CSRF-Token",
    "traceparent",
    "tracestate",
];

/// `expose_headers` in `register_middleware`.
pub const EXPOSE_HEADERS: [&str; 4] = [
    "X-Request-Id",
    "X-Trace-Id",
    "X-Baskfy-As-Of",
    "X-Baskfy-Data-Version",
];

/// `SAFELISTED_HEADERS`.
pub const SAFELISTED_HEADERS: [&str; 4] = [
    "Accept",
    "Accept-Language",
    "Content-Language",
    "Content-Type",
];

/// `max_age`'s default.
pub const MAX_AGE: u32 = 600;

/// The configured middleware.
#[derive(Debug, Clone)]
pub struct Cors {
    origins: Vec<String>,
    allow_all: bool,
    /// The sorted, de-duplicated allowed headers, as configured (for the response header).
    allow_headers: Vec<String>,
}

impl Cors {
    /// `CORSMiddleware(allow_origins=cors_origins, allow_credentials=True, ...)`.
    #[must_use]
    pub fn new(origins: &[String]) -> Self {
        let mut headers: Vec<String> = SAFELISTED_HEADERS
            .iter()
            .chain(ALLOW_HEADERS.iter())
            .map(|h| (*h).to_owned())
            .collect();
        headers.sort();
        headers.dedup();
        Self {
            allow_all: origins.iter().any(|o| o == "*"),
            origins: origins.to_vec(),
            allow_headers: headers,
        }
    }

    fn is_allowed_origin(&self, origin: &str) -> bool {
        self.allow_all || self.origins.iter().any(|o| o == origin)
    }

    fn preflight(&self, headers: &HeaderMap) -> Response {
        let get = |n: &str| headers.get(n).and_then(|v| v.to_str().ok());
        let origin = get("origin").unwrap_or("");
        let method = get("access-control-request-method").unwrap_or("");
        let requested = get("access-control-request-headers");
        let mut out: Vec<(&str, String)> = vec![
            ("vary", "Origin".to_owned()),
            ("access-control-allow-methods", ALLOW_METHODS.join(", ")),
            ("access-control-max-age", MAX_AGE.to_string()),
            (
                "access-control-allow-headers",
                self.allow_headers.join(", "),
            ),
            ("access-control-allow-credentials", "true".to_owned()),
        ];
        let mut failures: Vec<&str> = Vec::new();
        if self.is_allowed_origin(origin) {
            out.push(("access-control-allow-origin", origin.to_owned()));
        } else {
            failures.push("origin");
        }
        if !ALLOW_METHODS.contains(&method) {
            failures.push("method");
        }
        if let Some(req) = requested {
            let allowed: Vec<String> = self
                .allow_headers
                .iter()
                .map(|h| h.to_lowercase())
                .collect();
            if req
                .to_lowercase()
                .split(',')
                .any(|h| !allowed.contains(&h.trim().to_owned()))
            {
                failures.push("headers");
            }
        }
        if get("access-control-request-private-network").is_some() {
            failures.push("private-network");
        }
        let (status, text) = if failures.is_empty() {
            (StatusCode::OK, "OK".to_owned())
        } else {
            (
                StatusCode::BAD_REQUEST,
                format!("Disallowed CORS {}", failures.join(", ")),
            )
        };
        let mut response = (status, text).into_response();
        let h = response.headers_mut();
        for (name, value) in out {
            if let Ok(v) = HeaderValue::from_str(&value) {
                h.insert(name, v);
            }
        }
        h.insert(
            header::CONTENT_TYPE,
            HeaderValue::from_static("text/plain; charset=utf-8"),
        );
        response
    }

    fn decorate(&self, origin: &str, response: &mut Response) {
        let h = response.headers_mut();
        h.insert(
            "access-control-allow-credentials",
            HeaderValue::from_static("true"),
        );
        if let Ok(v) = HeaderValue::from_str(&EXPOSE_HEADERS.join(", ")) {
            h.insert("access-control-expose-headers", v);
        }
        if self.is_allowed_origin(origin) {
            if let Ok(v) = HeaderValue::from_str(origin) {
                h.insert("access-control-allow-origin", v);
            }
            let vary = match h.get(header::VARY).and_then(|v| v.to_str().ok()) {
                Some(existing) => format!("{existing}, Origin"),
                None => "Origin".to_owned(),
            };
            if let Ok(v) = HeaderValue::from_str(&vary) {
                h.insert(header::VARY, v);
            }
        }
    }
}

/// The middleware: outermost, so a preflight never reaches the app (and carries no request id).
pub async fn layer(cors: std::sync::Arc<Cors>, request: Request, next: Next) -> Response {
    let Some(origin) = request
        .headers()
        .get(header::ORIGIN)
        .map(|v| String::from_utf8_lossy(v.as_bytes()).into_owned())
    else {
        return next.run(request).await;
    };
    if request.method() == Method::OPTIONS
        && request
            .headers()
            .contains_key("access-control-request-method")
    {
        return cors.preflight(request.headers());
    }
    let mut response = next.run(request).await;
    if response.extensions().get::<OutsideMiddleware>().is_none() {
        cors.decorate(&origin, &mut response);
    }
    response
}
