//! Ports `app.core.websec.DeskSecurity` (DECISIONS-RS R8.1): the browser-facing defences of an
//! interface that can place orders.
//!
//! 1. **Host** must be one of `DESK_ALLOWED_HOSTS` (DNS rebinding) — else 421 with Python's
//!    JSON detail.
//! 2. **Unsafe methods** must carry an `Origin` (or, without one, a `Referer`) naming this app
//!    on an allowed host, plain or with `:DESK_PORT` (CSRF) — else 403.
//! 3. **Optional password** (`DESK_PASSWORD`): HTTP Basic, constant-time compared, every path
//!    but `/status` — else 401 with `WWW-Authenticate: Basic realm="Momentum Desk"`.
//!
//! The secret-file permission check at startup (`check_secret_permissions`) is the session
//! lane's (it touches `.env` and the token file).

use std::collections::BTreeSet;

use axum::body::Body;
use axum::extract::{Request, State};
use axum::http::{HeaderValue, Method, Response, StatusCode};
use axum::middleware::Next;

use crate::app::AppState;
use crate::wire::{PyValue, dict, orjson_dumps};

/// Paths answered before a password exists.
pub const NO_AUTH: &[&str] = &["/status"];

/// `_hosts()`.
#[must_use]
pub fn hosts(allowed: &str) -> BTreeSet<String> {
    let raw = if allowed.is_empty() {
        "127.0.0.1,localhost"
    } else {
        allowed
    };
    raw.split(',')
        .map(|h| h.trim().to_lowercase())
        .filter(|h| !h.is_empty())
        .collect()
}

/// `_origins()`.
#[must_use]
pub fn origins(allowed: &str, port: i64) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    for h in hosts(allowed) {
        for scheme in ["http", "https"] {
            out.insert(format!("{scheme}://{h}"));
            out.insert(format!("{scheme}://{h}:{port}"));
        }
    }
    out
}

fn json_response(status: StatusCode, detail: &str) -> Response<Body> {
    let body = orjson_dumps(&PyValue::Map(dict([(
        "detail",
        PyValue::Str(detail.to_owned()),
    )])))
    .unwrap_or_default();
    let mut r = Response::new(Body::from(body));
    *r.status_mut() = status;
    r.headers_mut().insert(
        axum::http::header::CONTENT_TYPE,
        HeaderValue::from_static("application/json"),
    );
    r
}

/// A header value as Starlette reads it: bytes decoded as Latin-1, so every value has a text
/// form. `HeaderValue::to_str` refuses anything outside visible ASCII, and treating that as an
/// ABSENT header waved a non-ASCII `Host` past the rebinding check and let a non-ASCII `Origin`
/// fall back to the `Referer` (review of lane exec_guards; Python refuses both).
#[must_use]
pub fn latin1(value: &HeaderValue) -> String {
    value.as_bytes().iter().map(|&b| char::from(b)).collect()
}

/// `base64.b64decode(s)` (`validate=False`), as CPython's `binascii.a2b_base64` runs it:
/// characters outside the alphabet are skipped, a `=` before two data characters of a quad is
/// skipped, a pad that completes a quad ends the input, leftover bits are dropped, and a quad
/// left with one data character, or two/three without their pads, is an error (`None`).
fn py_b64decode(encoded: &str) -> Option<Vec<u8>> {
    let sextet = |c: u8| -> Option<u32> {
        match c {
            b'A'..=b'Z' => Some(u32::from(c - b'A')),
            b'a'..=b'z' => Some(u32::from(c - b'a') + 26),
            b'0'..=b'9' => Some(u32::from(c - b'0') + 52),
            b'+' => Some(62),
            b'/' => Some(63),
            _ => None,
        }
    };
    let mut out = Vec::new();
    let (mut quad_pos, mut pads, mut left) = (0_u32, 0_u32, 0_u32);
    for &c in encoded.as_bytes() {
        if c == b'=' {
            if quad_pos >= 2 {
                pads += 1;
                if quad_pos + pads >= 4 {
                    return Some(out);
                }
            }
            continue;
        }
        let Some(v) = sextet(c) else { continue };
        match quad_pos {
            0 => {
                left = v;
                quad_pos = 1;
            }
            1 => {
                out.push(u8::try_from(((left << 2) | (v >> 4)) & 0xff).ok()?);
                left = v & 0x0f;
                quad_pos = 2;
            }
            2 => {
                out.push(u8::try_from(((left << 4) | (v >> 2)) & 0xff).ok()?);
                left = v & 0x03;
                quad_pos = 3;
            }
            _ => {
                out.push(u8::try_from(((left << 6) | v) & 0xff).ok()?);
                left = 0;
                quad_pos = 0;
            }
        }
    }
    (quad_pos == 0).then_some(out)
}

/// `_basic_ok`: constant-time comparison of the Basic password.
#[must_use]
pub fn basic_ok(header: Option<&str>, password: &str) -> bool {
    let Some(header) = header else { return false };
    if !header.to_lowercase().starts_with("basic ") {
        return false;
    }
    let Some((_, encoded)) = header.split_once(' ') else {
        return false;
    };
    let Some(decoded) = py_b64decode(encoded) else {
        return false;
    };
    let decoded = String::from_utf8_lossy(&decoded);
    let given = decoded.split_once(':').map_or("", |(_, p)| p);
    constant_time_eq(given.as_bytes(), password.as_bytes())
}

/// `hmac.compare_digest`: a length mismatch is unequal; equal lengths compare in constant time.
fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

fn python_repr_list(items: &BTreeSet<String>) -> String {
    let parts: Vec<String> = items
        .iter()
        .map(|s| baskfy_providers::py::repr_str(s))
        .collect();
    format!("[{}]", parts.join(", "))
}

/// The middleware (`BaseHTTPMiddleware.dispatch`).
pub async fn layer(State(state): State<AppState>, request: Request, next: Next) -> Response<Body> {
    let (allowed, port, password) = {
        let s = state.settings_snapshot();
        (
            s.desk_allowed_hosts.clone(),
            s.desk_port,
            s.desk_password.clone(),
        )
    };
    let host = request
        .headers()
        .get(axum::http::header::HOST)
        .map(latin1)
        .unwrap_or_default()
        .split(':')
        .next()
        .unwrap_or("")
        .to_lowercase();
    let known = hosts(&allowed);
    if !host.is_empty() && !known.contains(&host) {
        return json_response(
            StatusCode::MISDIRECTED_REQUEST,
            &format!(
                "This desk answers to {}, not {}. A request arriving under another name is how a \
                 browser is tricked into treating this app as the attacker's own site.",
                python_repr_list(&known),
                baskfy_providers::py::repr_str(&host)
            ),
        );
    }
    let safe = matches!(
        *request.method(),
        Method::GET | Method::HEAD | Method::OPTIONS
    );
    if !safe {
        let header = |name| request.headers().get(name).map(latin1);
        let allowed_origins = origins(&allowed, port);
        let ok = match header(axum::http::header::ORIGIN).filter(|o| !o.is_empty()) {
            Some(origin) => allowed_origins.contains(&origin),
            None => {
                let referer = header(axum::http::header::REFERER).unwrap_or_default();
                let referer = referer.as_str();
                allowed_origins
                    .iter()
                    .any(|o| referer.starts_with(&format!("{o}/")) || referer == o)
            }
        };
        if !ok {
            return json_response(
                StatusCode::FORBIDDEN,
                "Refused: this request did not come from the desk. A form on another site can \
                 POST here without asking, so a state-changing request has to declare an origin \
                 this app recognises.",
            );
        }
    }
    if !password.is_empty() && !NO_AUTH.iter().any(|p| request.uri().path().starts_with(p)) {
        let auth = request
            .headers()
            .get(axum::http::header::AUTHORIZATION)
            .map(latin1);
        if !basic_ok(auth.as_deref(), &password) {
            let mut r = Response::new(Body::from("Authentication required."));
            *r.status_mut() = StatusCode::UNAUTHORIZED;
            r.headers_mut().insert(
                axum::http::header::WWW_AUTHENTICATE,
                HeaderValue::from_static("Basic realm=\"Momentum Desk\""),
            );
            return r;
        }
    }
    next.run(request).await
}
