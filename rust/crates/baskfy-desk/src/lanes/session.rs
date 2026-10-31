//! Lane **session** — the Kite session and the platform: login/callback, token store, session
//! supervisor, lifecycle, telemetry, /status, /static (ported by the wave-D *views* lane, which
//! also owns `analytics.rs`).
//!
//! **STATUS (views lane):** ported and graded by `tools/parity/desk_diff.py`
//! (`desk_cases/views.json`): `GET /status`, `GET /login`, `GET /static/{path}` (Starlette's
//! `StaticFiles`), and [`startup`] (`main._apply_stored_settings` — the integrator calls it once
//! after `build_state`, R8.7).
//!
//! **STATUS (session lane, 30 Oct 2026):** also ported and graded (`desk_cases/session.json`,
//! 14 of 14 EQUAL, with and without `DESK_PATH_PREFIX=/desk`): `GET /callback` (the refusals;
//! the exchange is `exec_kite`'s `DeskKite::exchange_token`, then `ops::start("autorun")`), the
//! frozen lab's `/options`, `/options/run`, `/options/data` (404 for their verb, before any form
//! is read; FastAPI's 405 with `Allow: GET` otherwise — HEAD included, as on every `@app.get`
//! here). Modules: [`supervisor`] (`session_supervisor.py`, its 18 tests), [`telemetry`]
//! (`telemetry.py`: the library-absent branch, which is what the box ran), [`ticker`]
//! (`core/ticker.py`: the bus; no live websocket). `core/path_prefix.py` is the foundation's
//! `crate::path_prefix`.
//!
//! **Review (30 Oct 2026):** `/static` is Starlette's *mount*, so it now answers every verb as
//! `StaticFiles` does (405 as `{"detail"}` with no `Allow`; `..` folded by `normpath`;
//! `If-Modified-Since`; `Range` → 206/400/416), and Starlette's `redirect_slashes` 307 is
//! reproduced for this lane's paths (`/static`, `/status/`, …). Graded by
//! `desk_cases/session_edges.json` (default fixture), `desk_cases/session_variants/refused.json`
//! (`--fixture desk_fixtures/session_refused`: no session, a refused exchange) and
//! `desk_cases/session_variants/no_key.json` (`--env KITE_API_KEY=`).
//!
//! **D2 (session):** Starlette's `redirect_slashes` app-wide ([`routing`]), `/` ([`home`]), `/reconcile*`
//! ([`reconcile`]), `/tradebook*` ([`tradebook`]), `/status` through `exec_kite::status_session`,
//! `app.main`'s import-time boot ([`boot`]) and the `session-supervisor` service over the real
//! reconcile pass. `/status?ip=1` reports `outbound_ip: null` (no live network lookup).
//!

use std::path::{Path, PathBuf};

use axum::Router;
use axum::body::Body;
use axum::extract::{Path as AxPath, State};
use axum::http::{HeaderMap, HeaderValue, Method, Response, StatusCode, Uri, header};
use axum::routing::{any, get};

use axum::routing::post;

use crate::app::AppState;
use crate::lanes::analytics::web::{self, MultiDict, json, redirect_307};
use crate::wire::{PyValue, dict};

pub mod boot;
pub mod home;
pub mod reconcile;
pub mod routing;
pub mod supervisor;
pub mod telemetry;
pub mod ticker;
pub mod tradebook;

/// The lane's routes.
pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/status", get_only(get(status)))
        .route("/login", get_only(get(login)))
        .route("/callback", get_only(get(callback)))
        .route("/options", get_only(get(options_frozen)))
        .route("/options/run", post(options_frozen))
        .route("/options/data", get_only(get(options_frozen)))
        .route("/static/{*path}", any(static_file))
        .route("/static/", any(static_root))
        // The mount's bare path: axum's catch-all never matches it, so the app-wide slash
        // redirect (`routing`) cannot see its twin.
        .route("/static", any(routing::toggle))
        .merge(home::routes())
        .merge(reconcile::routes())
        .merge(tradebook::routes())
}

/// FastAPI's `@app.get`: the route's methods are `{"GET"}` alone, so `HEAD` (which axum's `get`
/// would answer) and every other verb get Starlette's 405 with `Allow: GET`.
pub(crate) fn get_only(
    router: axum::routing::MethodRouter<AppState>,
) -> axum::routing::MethodRouter<AppState> {
    router.head(get_405).fallback(get_405)
}

async fn get_405() -> Response<Body> {
    let mut r = web::detail(StatusCode::METHOD_NOT_ALLOWED, "Method Not Allowed");
    r.headers_mut()
        .insert(header::ALLOW, HeaderValue::from_static("GET"));
    r
}

/// The lane's template globals and filters.
pub fn register_templates(templates: &mut crate::templates::Templates) {
    let _ = templates;
}

/// Run one of the lane's background services by name; `None` if the name is not this lane's.
pub async fn run_service(name: &str, state: &AppState) -> Option<Result<(), String>> {
    match name {
        "session-supervisor" => Some(run_supervisor(state).await),
        _ => None,
    }
}

/// `python -m app.session_supervisor`: `from_desk()` imports `app.main` (so its boot runs),
/// then `run_forever` over the real reconcile pass and TWT's flag-gated drain.
async fn run_supervisor(state: &AppState) -> Result<(), String> {
    boot::boot(state).await;
    let pass = std::sync::Arc::new(
        crate::lanes::weekly::weekly_books::reconcile_hooks::DeskReconcilePass::new(state.clone()),
    );
    let deps = supervisor::DeskDeps::from_desk(state, pass).ok_or_else(|| {
        "the desk's gateway is not wired (no Kite wrapper or risk manager)".to_owned()
    })?;
    let mut sup = supervisor::Supervisor::new(std::sync::Arc::new(deps));
    let clock = supervisor::DeskWallClock(state.clock.clone());
    supervisor::run_forever(&mut sup, &clock, None).await;
    Ok(())
}

// ---------------------------------------------------------------------------------------------
// /callback — the login exchange, then the post-login collection
// ---------------------------------------------------------------------------------------------

#[allow(clippy::print_stderr)]
fn warn(line: &str) {
    eprintln!("WARNING:root:{line}");
}

/// `GET /callback?request_token=`: exchange the token, then start (never await) the autorun
/// collection; a failed autostart never costs the login. Redirects to `/` with the outcome.
async fn callback(State(state): State<AppState>, uri: Uri) -> Response<Body> {
    let q = MultiDict::from_query(&uri);
    let request_token = q.str_or_empty("request_token");
    if request_token.is_empty() {
        return web::detail(StatusCode::BAD_REQUEST, "Missing request_token");
    }
    if state.settings_snapshot().kite_api_key.is_empty() {
        return web::internal_error(NO_KEY);
    }
    let Some(kite) = state
        .gateway
        .as_any()
        .and_then(|a| a.downcast_ref::<crate::lanes::exec_core::desk::DeskGateway>())
        .map(crate::lanes::exec_core::desk::DeskGateway::kite)
    else {
        return web::internal_error("the desk's Kite wrapper is not wired");
    };
    if let Err(e) = kite.exchange_token(&request_token).await {
        return web::internal_error(&e.to_string());
    }
    let (started, why) = match post_login_autorun(&state).await {
        Ok(name) => (name, "post-login collection".to_owned()),
        Err(e) => {
            warn(&format!("post-login autorun did not start: {e}"));
            (String::new(), e.chars().take(120).collect())
        }
    };
    redirect_307(&format!("/?logged_in=1&autorun={started}&note={why}"))
}

async fn post_login_autorun(state: &AppState) -> Result<String, String> {
    let mut conn = state.db.connect().await.map_err(|e| e.to_string())?;
    conn.migrate().await.map_err(|e| e.to_string())?;
    crate::lanes::analytics::ops::start(&mut conn, state, "autorun", &[])
        .await
        .map(|s| s.name)
        .map_err(|e| e.to_string())
}

// ---------------------------------------------------------------------------------------------
// /options* — the frozen strangle lab (M6)
// ---------------------------------------------------------------------------------------------

/// `_options_view()`: with `OPTIONS_ENABLED` off (the default, non-negotiable 5) or the lab
/// frozen (`app.analytics.options_view` is not in the live tree, so the import fails even with
/// the flag on), every lab route answers 404 — before any form is read.
async fn options_frozen(State(state): State<AppState>) -> Response<Body> {
    let _enabled = crate::lanes::options::lab_enabled(&state.settings_snapshot());
    web::detail(StatusCode::NOT_FOUND, "Not Found")
}

/// `main._apply_stored_settings()`: migrate, then push the stored overrides onto the shared
/// settings. Never fails a boot (Python logs a warning and carries on); returns the keys
/// applied, or the warning.
pub async fn startup(state: &AppState) -> Result<Vec<String>, String> {
    let mut conn = state.db.connect().await.map_err(|e| e.to_string())?;
    conn.migrate().await.map_err(|e| e.to_string())?;
    crate::lanes::analytics::desk_settings::apply_to_config(&mut conn, &state.settings)
        .await
        .map(|m| m.into_keys().collect())
        .map_err(|e| e.to_string())
}

// ---------------------------------------------------------------------------------------------
// /status
// ---------------------------------------------------------------------------------------------

/// `kite_limits.shared_mode()`: `"shared"` when the shared read limiter's Redis answers a PING.
async fn shared_mode(settings: &crate::settings::Settings) -> &'static str {
    if !settings.shared_read_limits || settings.shared_read_limits_redis_url.is_empty() {
        return "per-process";
    }
    if redis_ping(&settings.shared_read_limits_redis_url).await {
        "shared"
    } else {
        "per-process"
    }
}

/// A RESP `PING` with redis-py's 2-second socket timeouts (`SHARED_SOCKET_TIMEOUT_SECONDS`).
async fn redis_ping(url: &str) -> bool {
    use tokio::io::{AsyncReadExt as _, AsyncWriteExt as _};
    let Some(rest) = url.strip_prefix("redis://") else {
        return false;
    };
    let authority = rest.split('/').next().unwrap_or_default();
    let host_port = authority.rsplit('@').next().unwrap_or_default();
    let address = if host_port.contains(':') {
        host_port.to_owned()
    } else {
        format!("{host_port}:6379")
    };
    let timeout = std::time::Duration::from_secs(2);
    let attempt = async {
        let mut s = tokio::net::TcpStream::connect(&address).await.ok()?;
        s.write_all(b"*1\r\n$4\r\nPING\r\n").await.ok()?;
        let mut buf = [0_u8; 16];
        let n = s.read(&mut buf).await.ok()?;
        Some(buf.get(..n)?.starts_with(b"+PONG"))
    };
    matches!(tokio::time::timeout(timeout, attempt).await, Ok(Some(true)))
}

const NO_KEY: &str = "KITE_API_KEY missing — copy .env.example to .env and fill it.";

/// `GET /status[?ip=1]`: the flags, the limiter mode, then the session half through the desk's
/// Kite wrapper (`exec_kite::status_session`).
async fn status(State(state): State<AppState>, uri: Uri) -> Response<Body> {
    let q = MultiDict::from_query(&uri);
    let settings = state.settings_snapshot();
    let mut out = dict([
        ("dry_run", PyValue::Bool(settings.dry_run)),
        ("force_ipv4", PyValue::Bool(settings.force_ipv4)),
        (
            "read_limiter",
            PyValue::Str(shared_mode(&settings).await.to_owned()),
        ),
    ]);
    if !q.str_or_empty("ip").is_empty() {
        out.insert("outbound_ip", PyValue::None);
    }
    let session = crate::lanes::exec_core::exec_kite::status_session(&state).await;
    for (k, v) in session.iter() {
        out.insert(k, v.clone());
    }
    json(StatusCode::OK, &PyValue::Map(out))
}

async fn login(State(state): State<AppState>) -> Response<Body> {
    if state.settings_snapshot().kite_api_key.is_empty() {
        return web::internal_error(NO_KEY);
    }
    redirect_307(&state.kite.login_url())
}

// ---------------------------------------------------------------------------------------------
// /static — Starlette's StaticFiles(directory="app/static")
// ---------------------------------------------------------------------------------------------

/// `mimetypes.guess_type` for the desk's assets, with Starlette's `; charset=utf-8` on `text/*`;
/// an extension `mimetypes` does not know is FileResponse's `application/octet-stream`.
fn media_type(path: &Path) -> &'static str {
    match path
        .extension()
        .and_then(|e| e.to_str())
        .map(str::to_ascii_lowercase)
        .as_deref()
    {
        Some("css") => "text/css; charset=utf-8",
        Some("js" | "mjs") => "text/javascript; charset=utf-8",
        Some("html" | "htm") => "text/html; charset=utf-8",
        Some("txt") => "text/plain; charset=utf-8",
        Some("svg") => "image/svg+xml",
        Some("png") => "image/png",
        Some("jpg" | "jpeg") => "image/jpeg",
        Some("gif") => "image/gif",
        Some("webp") => "image/webp",
        Some("ico") => "image/x-icon",
        Some("woff2") => "font/woff2",
        Some("woff") => "font/woff",
        Some("ttf") => "font/ttf",
        Some("otf") => "font/otf",
        Some("json") => "application/json",
        _ => "application/octet-stream",
    }
}

/// `StaticFiles.get_path`: `os.path.normpath(os.path.join(*route_path.split("/")))` — empty and
/// `.` parts dropped, `..` folded into the part before it (a leading `..` is kept), `"."` when
/// nothing is left.
fn normpath(rel: &str) -> String {
    let mut parts: Vec<&str> = Vec::new();
    for part in rel.split('/') {
        match part {
            "" | "." => {}
            ".." => {
                if parts.last().is_some_and(|p| *p != "..") {
                    parts.pop();
                } else {
                    parts.push("..");
                }
            }
            p => parts.push(p),
        }
    }
    if parts.is_empty() {
        ".".to_owned()
    } else {
        parts.join("/")
    }
}

/// Starlette's `lookup_path` over [`normpath`]'s result: `realpath(join(dir, path))`, refused
/// unless it stays under the directory; then a regular file, or nothing.
fn lookup(dir: &Path, rel: &str) -> Option<PathBuf> {
    let path = normpath(rel);
    if path.starts_with('/') || path.starts_with('\\') || path.contains('\0') {
        return None;
    }
    let real = dir.join(&path).canonicalize().ok()?;
    let base = dir.canonicalize().ok()?;
    if !real.starts_with(&base) || !real.is_file() {
        return None;
    }
    Some(real)
}

fn not_found() -> Response<Body> {
    web::detail(StatusCode::NOT_FOUND, "Not Found")
}

/// Python's `int(text)` for a range bound: whitespace, an optional sign, digits with single
/// underscores between them.
fn py_int(text: &str) -> Option<i128> {
    let t = text.trim();
    let (sign, digits) = match t.strip_prefix('-') {
        Some(rest) => (-1, rest),
        None => (1, t.strip_prefix('+').unwrap_or(t)),
    };
    if digits.is_empty()
        || digits.starts_with('_')
        || digits.ends_with('_')
        || digits.contains("__")
        || !digits.chars().all(|c| c.is_ascii_digit() || c == '_')
    {
        return None;
    }
    digits
        .replace('_', "")
        .parse::<i128>()
        .ok()
        .map(|n| n * sign)
}

/// Why a `Range` header was refused (`MalformedRangeHeader`, `RangeNotSatisfiable`).
enum RangeRefusal {
    Malformed(&'static str),
    NotSatisfiable,
}

/// `FileResponse._parse_range_header`: the byte ranges `[start, end)` asked for, merged when
/// more than one; empty means "serve the whole file".
fn parse_range_header(http_range: &str, size: i128) -> Result<Vec<(i128, i128)>, RangeRefusal> {
    let Some((units, range)) = http_range.split_once('=') else {
        return Err(RangeRefusal::Malformed("Malformed range header."));
    };
    if units.trim().to_lowercase() != "bytes" {
        return Err(RangeRefusal::Malformed("Only support bytes range"));
    }
    if range.matches(',').count() + 1 > 100 {
        return Ok(Vec::new());
    }
    let mut ranges: Vec<(i128, i128)> = Vec::new();
    for part in range.split(',') {
        let part = part.trim();
        if part.is_empty() || part == "-" || !part.contains('-') {
            continue;
        }
        let Some((start_str, end_str)) = part.split_once('-') else {
            continue;
        };
        let (start_str, end_str) = (start_str.trim(), end_str.trim());
        let start = if start_str.is_empty() {
            match py_int(end_str) {
                Some(e) => (size - e).max(0),
                None => continue,
            }
        } else {
            match py_int(start_str) {
                Some(s) => s,
                None => continue,
            }
        };
        let end = if !start_str.is_empty() && !end_str.is_empty() {
            match py_int(end_str) {
                Some(e) if e < size => e + 1,
                Some(_) => size,
                None => continue,
            }
        } else {
            size
        };
        ranges.push((start, end));
    }
    if ranges.is_empty() {
        return Err(RangeRefusal::Malformed(
            "Range header: range must be requested",
        ));
    }
    if ranges.iter().any(|(s, _)| !(0 <= *s && *s < size)) {
        return Err(RangeRefusal::NotSatisfiable);
    }
    if ranges.iter().any(|(s, e)| s >= e) {
        return Err(RangeRefusal::Malformed(
            "Range header: start must be less than end",
        ));
    }
    if ranges.len() == 1 {
        return Ok(ranges);
    }
    ranges.sort_unstable();
    let mut merged: Vec<(i128, i128)> = Vec::new();
    for (s, e) in ranges {
        match merged.last_mut() {
            Some(last) if s <= last.1 => last.1 = last.1.max(e),
            _ => merged.push((s, e)),
        }
    }
    Ok(merged)
}

fn plain_text(status: StatusCode, body: &'static str) -> Response<Body> {
    let mut r = Response::new(Body::from(body));
    *r.status_mut() = status;
    r.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/plain; charset=utf-8"),
    );
    r
}

fn slice(bytes: &[u8], start: i128, end: i128) -> &[u8] {
    let s = usize::try_from(start).unwrap_or(0);
    let e = usize::try_from(end).unwrap_or(0).min(bytes.len());
    bytes.get(s..e).unwrap_or_default()
}

/// `app.mount("/static", StaticFiles(directory="app/static"))`: a mount, not an `@app.get`, so
/// every verb reaches it — `GET`/`HEAD` are served, anything else is the `HTTPException(405)`
/// FastAPI renders as `{"detail": "Method Not Allowed"}` with no `Allow`. `If-None-Match` /
/// `If-Modified-Since` give the 304, and `FileResponse` answers `Range` (206, 400, 416).
/// `/static/`: the mount with an empty path — `.` is the directory itself, so 404.
async fn static_root(method: Method) -> Response<Body> {
    if method != Method::GET && method != Method::HEAD {
        return web::detail(StatusCode::METHOD_NOT_ALLOWED, "Method Not Allowed");
    }
    not_found()
}

#[allow(clippy::too_many_lines)]
async fn static_file(
    State(state): State<AppState>,
    AxPath(rel): AxPath<String>,
    method: Method,
    headers: HeaderMap,
) -> Response<Body> {
    use md5::{Digest as _, Md5};
    if method != Method::GET && method != Method::HEAD {
        return web::detail(StatusCode::METHOD_NOT_ALLOWED, "Method Not Allowed");
    }
    let Some(path) = lookup(&state.paths.static_dir, &rel) else {
        return not_found();
    };
    let Ok(meta) = std::fs::metadata(&path) else {
        return not_found();
    };
    let mtime = meta
        .modified()
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map_or(0.0, |d| d.as_secs_f64());
    let size = meta.len();
    let etag_base = format!("{}-{size}", baskfy_core::py::float_repr(mtime));
    let digest = Md5::digest(etag_base.as_bytes());
    let etag = format!(
        "\"{}\"",
        digest.iter().fold(String::new(), |mut s, b| {
            use std::fmt::Write as _;
            let _ = write!(s, "{b:02x}");
            s
        })
    );
    let modified = meta.modified().ok();
    let last_modified = modified.map(httpdate::fmt_http_date).unwrap_or_default();
    let header_str = |name: header::HeaderName| {
        headers
            .get(name)
            .and_then(|v| v.to_str().ok())
            .map(str::to_owned)
    };
    // `StaticFiles.is_not_modified`: If-None-Match decides when present; otherwise
    // If-Modified-Since at or after Last-Modified (both at one-second resolution).
    let not_modified = match header_str(header::IF_NONE_MATCH).filter(|v| !v.is_empty()) {
        Some(v) => v
            .split(',')
            .map(|t| {
                let t = t.trim();
                t.strip_prefix("W/").unwrap_or(t)
            })
            .any(|t| t == etag),
        None => match (
            header_str(header::IF_MODIFIED_SINCE).and_then(|v| httpdate::parse_http_date(&v).ok()),
            httpdate::parse_http_date(&last_modified).ok(),
        ) {
            (Some(since), Some(last)) => since >= last,
            _ => false,
        },
    };
    if not_modified {
        let mut r = Response::new(Body::empty());
        *r.status_mut() = StatusCode::NOT_MODIFIED;
        if let Ok(v) = HeaderValue::from_str(&etag) {
            r.headers_mut().insert(header::ETAG, v);
        }
        return r;
    }
    let content_type = media_type(&path);
    let head_only = method == Method::HEAD;
    let read = || std::fs::read(&path).ok();
    let base = |status: StatusCode, body: Body, length: u64| {
        let mut r = Response::new(body);
        *r.status_mut() = status;
        let h = r.headers_mut();
        h.insert(header::CONTENT_TYPE, HeaderValue::from_static(content_type));
        h.insert(header::ACCEPT_RANGES, HeaderValue::from_static("bytes"));
        if let Ok(v) = HeaderValue::from_str(&length.to_string()) {
            h.insert(header::CONTENT_LENGTH, v);
        }
        if let Ok(v) = HeaderValue::from_str(&last_modified) {
            h.insert(header::LAST_MODIFIED, v);
        }
        if let Ok(v) = HeaderValue::from_str(&etag) {
            h.insert(header::ETAG, v);
        }
        r
    };
    let whole = || {
        if head_only {
            return base(StatusCode::OK, Body::empty(), size);
        }
        match read() {
            Some(bytes) => base(StatusCode::OK, Body::from(bytes), size),
            None => not_found(),
        }
    };
    let Some(http_range) = header_str(header::RANGE) else {
        return whole();
    };
    if let Some(if_range) = header_str(header::IF_RANGE)
        && if_range != last_modified
        && if_range != etag
    {
        return whole();
    }
    let total = i128::from(size);
    let ranges = match parse_range_header(&http_range, total) {
        Ok(r) => r,
        Err(RangeRefusal::Malformed(why)) => return plain_text(StatusCode::BAD_REQUEST, why),
        Err(RangeRefusal::NotSatisfiable) => {
            let mut r = plain_text(StatusCode::RANGE_NOT_SATISFIABLE, "");
            if let Ok(v) = HeaderValue::from_str(&format!("bytes */{size}")) {
                r.headers_mut().insert(header::CONTENT_RANGE, v);
            }
            return r;
        }
    };
    match ranges.as_slice() {
        [] => whole(),
        [(start, end)] => {
            let length = u64::try_from(end - start).unwrap_or(0);
            let body = if head_only {
                Body::empty()
            } else {
                match read() {
                    Some(bytes) => Body::from(slice(&bytes, *start, *end).to_vec()),
                    None => return not_found(),
                }
            };
            let mut r = base(StatusCode::PARTIAL_CONTENT, body, length);
            if let Ok(v) = HeaderValue::from_str(&format!("bytes {start}-{}/{size}", end - 1)) {
                r.headers_mut().insert(header::CONTENT_RANGE, v);
            }
            r
        }
        many => {
            // `generate_multipart`, with `token_hex(13)` for the boundary.
            let boundary: String =
                rand::random::<[u8; 13]>()
                    .iter()
                    .fold(String::new(), |mut s, b| {
                        use std::fmt::Write as _;
                        let _ = write!(s, "{b:02x}");
                        s
                    });
            let bytes = if head_only {
                Vec::new()
            } else {
                match read() {
                    Some(b) => b,
                    None => return not_found(),
                }
            };
            let mut body: Vec<u8> = Vec::new();
            let mut length: i128 = 0;
            for (start, end) in many {
                let part = format!(
                    "--{boundary}\r\nContent-Type: {content_type}\r\nContent-Range: bytes \
                     {start}-{}/{size}\r\n\r\n",
                    end - 1
                );
                length += i128::try_from(part.len()).unwrap_or(0) + (end - start) + 2;
                if !head_only {
                    body.extend_from_slice(part.as_bytes());
                    body.extend_from_slice(slice(&bytes, *start, *end));
                    body.extend_from_slice(b"\r\n");
                }
            }
            let closing = format!("--{boundary}--");
            length += i128::try_from(closing.len()).unwrap_or(0);
            if !head_only {
                body.extend_from_slice(closing.as_bytes());
            }
            let mut r = base(
                StatusCode::PARTIAL_CONTENT,
                Body::from(body),
                u64::try_from(length).unwrap_or(0),
            );
            if let Ok(v) =
                HeaderValue::from_str(&format!("multipart/byteranges; boundary={boundary}"))
            {
                r.headers_mut().insert(header::CONTENT_TYPE, v);
            }
            r
        }
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::panic)]
    use super::*;

    #[test]
    fn lookup_refuses_escapes() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("app.css"), "x").unwrap();
        assert!(lookup(dir.path(), "app.css").is_some());
        assert!(lookup(dir.path(), "../app.css").is_none());
        assert!(lookup(dir.path(), "/etc/passwd").is_none());
        assert!(lookup(dir.path(), "missing.css").is_none());
        assert!(lookup(dir.path(), "").is_none());
    }

    #[test]
    fn media_types_match_starlette() {
        assert_eq!(media_type(Path::new("a.css")), "text/css; charset=utf-8");
        assert_eq!(media_type(Path::new("f.woff2")), "font/woff2");
        assert_eq!(media_type(Path::new("i.ico")), "image/x-icon");
        assert_eq!(media_type(Path::new("x.map")), "application/octet-stream");
        assert_eq!(media_type(Path::new("noext")), "application/octet-stream");
    }

    #[test]
    fn get_path_is_normpath_of_the_joined_parts() {
        assert_eq!(normpath("/app.css"), "app.css");
        assert_eq!(normpath("//app.css"), "app.css");
        assert_eq!(normpath("/fonts/../app.css"), "app.css");
        assert_eq!(normpath("/./app.css"), "app.css");
        assert_eq!(normpath("/../main.py"), "../main.py");
        assert_eq!(normpath("/a/../../b"), "../b");
        assert_eq!(normpath("/"), ".");
        assert_eq!(normpath(""), ".");
    }

    #[test]
    fn a_dotdot_that_lands_back_inside_is_served_one_that_escapes_is_not() {
        let root = tempfile::tempdir().unwrap();
        let dir = root.path().join("static");
        std::fs::create_dir_all(dir.join("fonts")).unwrap();
        std::fs::write(dir.join("app.css"), "x").unwrap();
        std::fs::write(root.path().join("main.py"), "x").unwrap();
        assert!(lookup(&dir, "fonts/../app.css").is_some());
        assert!(lookup(&dir, "../static/app.css").is_some());
        assert!(lookup(&dir, "../main.py").is_none());
        assert!(lookup(&dir, "fonts").is_none(), "a directory is not served");
    }

    #[test]
    fn range_headers_parse_as_starlette() {
        let ok = |h: &str| match parse_range_header(h, 100) {
            Ok(r) => r,
            Err(_) => panic!("refused {h}"),
        };
        let malformed = |h: &str| match parse_range_header(h, 100) {
            Err(RangeRefusal::Malformed(m)) => m,
            _ => panic!("not malformed: {h}"),
        };
        assert_eq!(ok("bytes=0-9"), vec![(0, 10)]);
        assert_eq!(ok("bytes=90-"), vec![(90, 100)]);
        assert_eq!(ok("bytes=-10"), vec![(90, 100)]);
        assert_eq!(ok("bytes=-1000"), vec![(0, 100)]);
        assert_eq!(ok("bytes=0-1000"), vec![(0, 100)]);
        assert_eq!(ok(" Bytes =0-9"), vec![(0, 10)]);
        assert_eq!(ok("bytes=0-9,5-19,50-59"), vec![(0, 20), (50, 60)]);
        assert_eq!(malformed("bytes"), "Malformed range header.");
        assert_eq!(malformed("items=0-9"), "Only support bytes range");
        assert_eq!(
            malformed("bytes=x-y"),
            "Range header: range must be requested"
        );
        assert_eq!(
            malformed("bytes=9-3"),
            "Range header: start must be less than end"
        );
        assert!(matches!(
            parse_range_header("bytes=100-", 100),
            Err(RangeRefusal::NotSatisfiable)
        ));
        let many = vec!["0-1"; 101].join(",");
        assert!(ok(&format!("bytes={many}")).is_empty());
    }
}
