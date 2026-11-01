//! HTTP caching for the published analytics reads. Ports `baskfy_api.http_cache`.
//!
//! A `200` to a `GET` under one of [`CACHEABLE_PREFIXES`] gets `ETag: W/"v{data_version}-
//! {sha256(body)[:32]}"`, `Cache-Control: private, max-age=0, must-revalidate,
//! stale-while-revalidate=N` and `Authorization` in `Vary`; an `If-None-Match` that matches is a
//! `304` with the same headers and no body. Everything else passes through untouched.

use axum::body::{Body, to_bytes};
use axum::extract::Request;
use axum::http::{HeaderValue, Method, StatusCode, header};
use axum::middleware::Next;
use axum::response::Response;
use sha2::{Digest, Sha256};

use crate::settings::API_PREFIX;

/// `CACHEABLE_PREFIXES` (below `/api/v1`).
pub const CACHEABLE_PREFIXES: [&str; 5] = [
    "/meta/",
    "/instruments",
    "/indices/",
    "/market-health",
    "/listings",
];

/// `UNCACHEABLE_SUFFIXES`.
pub const UNCACHEABLE_SUFFIXES: [&str; 4] = ["/csv", "/export", "/download", "/events"];

/// `DATA_VERSION_HEADER`.
pub const DATA_VERSION_HEADER: &str = "X-Baskfy-Data-Version";
/// `AS_OF_HEADER`.
pub const AS_OF_HEADER: &str = "X-Baskfy-As-Of";

/// `MAX_BUFFER_BYTES`.
pub const MAX_BUFFER_BYTES: usize = 4 * 1024 * 1024;

const DIGEST_CHARS: usize = 32;

/// `snapshot_headers(as_of, data_version)`.
#[must_use]
pub fn snapshot_headers(
    as_of: Option<chrono::NaiveDate>,
    data_version: Option<i64>,
) -> Vec<(&'static str, String)> {
    let mut out = Vec::new();
    if let Some(d) = as_of {
        out.push((AS_OF_HEADER, crate::wire::py_date(d)));
    }
    if let Some(v) = data_version {
        out.push((DATA_VERSION_HEADER, v.to_string()));
    }
    out
}

/// `is_cacheable_path(path)`.
#[must_use]
pub fn is_cacheable_path(path: &str) -> bool {
    let Some(tail) = path.strip_prefix(API_PREFIX) else {
        return false;
    };
    if UNCACHEABLE_SUFFIXES.iter().any(|s| tail.ends_with(s)) {
        return false;
    }
    CACHEABLE_PREFIXES.iter().any(|p| tail.starts_with(p))
}

/// `compute_etag(body, data_version)`.
#[must_use]
pub fn compute_etag(body: &[u8], data_version: Option<&str>) -> String {
    let digest = Sha256::digest(body);
    let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
    let prefix = match data_version {
        Some(v) if !v.is_empty() => format!("v{v}-"),
        _ => String::new(),
    };
    format!("W/\"{prefix}{}\"", &hex[..DIGEST_CHARS])
}

fn strip_weak(token: &str) -> &str {
    token.strip_prefix("W/").unwrap_or(token)
}

/// `etag_matches(if_none_match, etag)`: RFC 9110 weak comparison; `*` matches anything.
#[must_use]
pub fn etag_matches(if_none_match: Option<&str>, etag: &str) -> bool {
    let Some(header) = if_none_match.filter(|h| !h.is_empty()) else {
        return false;
    };
    let candidate = strip_weak(etag);
    header
        .split(',')
        .map(str::trim)
        .any(|t| t == "*" || strip_weak(t) == candidate)
}

/// `cache_control(max_stale_seconds)`.
#[must_use]
pub fn cache_control(max_stale_seconds: i64) -> String {
    format!("private, max-age=0, must-revalidate, stale-while-revalidate={max_stale_seconds}")
}

/// `_merged_vary(existing)`.
#[must_use]
pub fn merged_vary(existing: Option<&str>) -> String {
    let mut parts: Vec<&str> = existing
        .unwrap_or("")
        .split(',')
        .map(str::trim)
        .filter(|p| !p.is_empty())
        .collect();
    if !parts
        .iter()
        .any(|p| p.eq_ignore_ascii_case("authorization"))
    {
        parts.push("Authorization");
    }
    parts.join(", ")
}

/// The middleware (`register_http_cache`). `stale_seconds` is
/// `BASKFY_HTTP_STALE_WHILE_REVALIDATE_SECONDS`.
pub async fn layer(stale_seconds: i64, request: Request, next: Next) -> Response {
    let method = request.method().clone();
    let path = crate::app::decoded_path(request.uri());
    let if_none_match = request
        .headers()
        .get(header::IF_NONE_MATCH)
        .and_then(|v| v.to_str().ok())
        .map(str::to_owned);
    let response = next.run(request).await;
    if method != Method::GET && method != Method::HEAD {
        return response;
    }
    if response.status() != StatusCode::OK || !is_cacheable_path(&path) {
        return response;
    }
    let (mut parts, body) = response.into_parts();
    let Ok(bytes) = to_bytes(body, usize::MAX).await else {
        return Response::from_parts(parts, Body::empty());
    };
    if bytes.len() > MAX_BUFFER_BYTES {
        return Response::from_parts(parts, Body::from(bytes));
    }
    let version = parts
        .headers
        .get(DATA_VERSION_HEADER)
        .and_then(|v| v.to_str().ok())
        .map(str::to_owned);
    let etag = compute_etag(&bytes, version.as_deref());
    let vary = merged_vary(
        parts
            .headers
            .get(header::VARY)
            .and_then(|v| v.to_str().ok()),
    );
    let h = &mut parts.headers;
    if let Ok(v) = HeaderValue::from_str(&etag) {
        h.insert(header::ETAG, v);
    }
    if let Ok(v) = HeaderValue::from_str(&cache_control(stale_seconds)) {
        h.insert(header::CACHE_CONTROL, v);
    }
    if let Ok(v) = HeaderValue::from_str(&vary) {
        h.insert(header::VARY, v);
    }
    if etag_matches(if_none_match.as_deref(), &etag) {
        h.remove(header::CONTENT_LENGTH);
        parts.status = StatusCode::NOT_MODIFIED;
        return Response::from_parts(parts, Body::empty());
    }
    h.insert(header::CONTENT_LENGTH, HeaderValue::from(bytes.len()));
    Response::from_parts(parts, Body::from(bytes))
}

#[cfg(test)]
mod tests {
    use super::*;

    // Ports test_http_cache.py's pure half.
    #[test]
    fn the_tag_names_the_data_version_and_is_weak() {
        assert!(compute_etag(br#"{"data":[]}"#, Some("7")).starts_with("W/\"v7-"));
        let body = br#"{"data":[]}"#;
        assert_ne!(compute_etag(body, Some("7")), compute_etag(body, Some("8")));
        assert_ne!(
            compute_etag(br#"{"a":1}"#, Some("7")),
            compute_etag(br#"{"a":2}"#, Some("7"))
        );
        assert!(compute_etag(b"[]", None).starts_with("W/\""));
        assert!(compute_etag(b"[]", Some("1")).starts_with("W/"));
        // hashlib.sha256(b"[]").hexdigest()[:32]
        assert_eq!(
            compute_etag(b"[]", None),
            "W/\"4f53cda18c2baa0c0354bb5f9a3ecbe5\""
        );
    }

    #[test]
    fn if_none_match() {
        let tag = compute_etag(b"x", Some("3"));
        assert!(etag_matches(Some(&tag), &tag));
        assert!(etag_matches(Some(&format!("\"other\", {tag}")), &tag));
        assert!(etag_matches(Some("*"), &tag));
        assert!(etag_matches(Some(tag.trim_start_matches("W/")), &tag));
        assert!(!etag_matches(None, &tag));
        assert!(!etag_matches(Some(&compute_etag(b"x", Some("2"))), &tag));
    }

    #[test]
    fn scope() {
        for p in [
            "/meta/status",
            "/meta/factors",
            "/instruments/CUPID",
            "/instruments/CUPID/history",
            "/indices/dashboard",
            "/market-health",
            "/market-health/history",
            "/listings",
        ] {
            assert!(is_cacheable_path(&format!("{API_PREFIX}{p}")), "{p}");
        }
        for p in [
            "/screens",
            "/screens/abc/run",
            "/me",
            "/invoices",
            "/portfolios",
            "/backtests",
            "/plans",
        ] {
            assert!(!is_cacheable_path(&format!("{API_PREFIX}{p}")), "{p}");
        }
        assert!(!is_cacheable_path(&format!(
            "{API_PREFIX}/instruments/CUPID/csv"
        )));
        assert!(!is_cacheable_path("/healthz"));
    }

    #[test]
    fn cache_control_and_snapshot_headers() {
        let c = cache_control(60);
        assert!(c.contains("private") && c.contains("max-age=0") && c.contains("must-revalidate"));
        assert!(c.contains("stale-while-revalidate=60"));
        let d = chrono::NaiveDate::from_ymd_opt(2026, 9, 4);
        assert_eq!(
            snapshot_headers(d, Some(18)),
            vec![
                (AS_OF_HEADER, "2026-09-04".to_owned()),
                (DATA_VERSION_HEADER, "18".to_owned())
            ]
        );
        assert!(snapshot_headers(None, None).is_empty());
        assert_eq!(merged_vary(None), "Authorization");
        assert_eq!(merged_vary(Some("Origin")), "Origin, Authorization");
        assert_eq!(merged_vary(Some("authorization")), "authorization");
    }
}
