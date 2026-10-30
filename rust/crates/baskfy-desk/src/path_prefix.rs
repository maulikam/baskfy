//! Ports `app.core.path_prefix` (DECISIONS-RS R8.1): the desk served under `/desk` on the app's
//! own address. Caddy strips the prefix on the way in; this puts it back on every root-relative
//! URL the desk hands the browser — `href=`/`action=`/`src=` in markup, `fetch('/…')` in inline
//! script, and `Location:` on redirects. Off (every byte untouched) while `DESK_PATH_PREFIX` is
//! empty. Only `text/html` and `javascript` bodies without a `Content-Encoding` are rewritten;
//! the body is buffered and `Content-Length` recomputed. It never reads or changes a form value,
//! an amount or an order field.

use axum::body::{Body, to_bytes};
use axum::extract::{Request, State};
use axum::http::header::{CONTENT_ENCODING, CONTENT_LENGTH, CONTENT_TYPE, LOCATION};
use axum::http::{HeaderValue, Response};
use axum::middleware::Next;

/// `normalise`: `"desk"`, `"/desk/"`, `" /desk "` → `"/desk"`; blank or `/` → `""` (off).
#[must_use]
pub fn normalise(prefix: &str) -> String {
    let stripped = prefix.trim().trim_matches('/');
    if stripped.is_empty() {
        String::new()
    } else {
        format!("/{stripped}")
    }
}

/// `rewrite_location`: prefix a root-relative redirect; leave absolute, protocol-relative and
/// already-prefixed ones alone.
#[must_use]
pub fn rewrite_location(location: &str, prefix: &str) -> String {
    if !location.starts_with('/') || location.starts_with("//") {
        return location.to_owned();
    }
    if location == prefix || location.starts_with(&format!("{prefix}/")) {
        return location.to_owned();
    }
    format!("{prefix}{location}")
}

/// The lookahead `/(?!/|<prefix>(?:/|["'`?#]))` at `rest` (the text after the `/`).
fn should_prefix(rest: &str, bare: &str) -> bool {
    if rest.starts_with('/') {
        return false;
    }
    if let Some(after) = rest.strip_prefix(bare)
        && after.starts_with(['/', '"', '\'', '`', '?', '#'])
    {
        return false;
    }
    true
}

/// One `re.sub` pass: every `<lead>/` whose lead matches `is_lead` gets the prefix.
fn rewrite_after(text: &str, prefix: &str, leads: &[&str]) -> String {
    let bare = prefix.trim_start_matches('/');
    let mut out = String::with_capacity(text.len() + 64);
    let mut i = 0;
    'outer: while i < text.len() {
        for lead in leads {
            if text[i..].starts_with(lead) {
                let slash = i + lead.len();
                if text[slash..].starts_with('/') && should_prefix(&text[slash + 1..], bare) {
                    out.push_str(lead);
                    out.push_str(prefix);
                    out.push('/');
                    i = slash + 1;
                    continue 'outer;
                }
            }
        }
        let Some(c) = text[i..].chars().next() else {
            break;
        };
        out.push(c);
        i += c.len_utf8();
    }
    out
}

/// `fetch(\s*['"`]` then `/…`.
fn rewrite_fetch(text: &str, prefix: &str) -> String {
    let bare = prefix.trim_start_matches('/');
    let mut out = String::with_capacity(text.len() + 64);
    let mut i = 0;
    while i < text.len() {
        if text[i..].starts_with("fetch(") {
            let mut j = i + "fetch(".len();
            while let Some(c) = text[j..].chars().next().filter(|c| c.is_whitespace()) {
                j += c.len_utf8();
            }
            if let Some(q) = text[j..]
                .chars()
                .next()
                .filter(|c| matches!(c, '\'' | '"' | '`'))
            {
                let slash = j + q.len_utf8();
                if text[slash..].starts_with('/') && should_prefix(&text[slash + 1..], bare) {
                    out.push_str(&text[i..slash]);
                    out.push_str(prefix);
                    out.push('/');
                    i = slash + 1;
                    continue;
                }
            }
        }
        let Some(c) = text[i..].chars().next() else {
            break;
        };
        out.push(c);
        i += c.len_utf8();
    }
    out
}

/// `rewrite_body`: the three substitutions, in Python's order.
#[must_use]
pub fn rewrite_body(text: &str, prefix: &str) -> String {
    let once = rewrite_after(text, prefix, &["href=\"", "action=\"", "src=\""]);
    let twice = rewrite_after(&once, prefix, &["href='", "action='", "src='"]);
    rewrite_fetch(&twice, prefix)
}

/// The middleware (outermost, so it sees the final bytes). `prefix` is already normalised;
/// an empty one passes everything through untouched.
pub async fn layer(State(prefix): State<String>, request: Request, next: Next) -> Response<Body> {
    let response = next.run(request).await;
    if prefix.is_empty() {
        return response;
    }
    let (mut parts, body) = response.into_parts();
    if let Some(loc) = parts.headers.get(LOCATION).and_then(|v| v.to_str().ok()) {
        let rewritten = rewrite_location(loc, &prefix);
        if let Ok(v) = HeaderValue::from_str(&rewritten) {
            parts.headers.insert(LOCATION, v);
        }
    }
    let ctype = parts
        .headers
        .get(CONTENT_TYPE)
        .map(|v| v.as_bytes().to_vec())
        .unwrap_or_default();
    let textual = ctype.windows(9).any(|w| w == b"text/html")
        || ctype.windows(10).any(|w| w == b"javascript");
    if !textual || parts.headers.contains_key(CONTENT_ENCODING) {
        return Response::from_parts(parts, body);
    }
    let Ok(bytes) = to_bytes(body, usize::MAX).await else {
        return Response::from_parts(parts, Body::empty());
    };
    let rewritten = rewrite_body(&String::from_utf8_lossy(&bytes), &prefix).into_bytes();
    parts.headers.remove(CONTENT_LENGTH);
    if let Ok(v) = HeaderValue::from_str(&rewritten.len().to_string()) {
        parts.headers.insert(CONTENT_LENGTH, v);
    }
    Response::from_parts(parts, Body::from(rewritten))
}
