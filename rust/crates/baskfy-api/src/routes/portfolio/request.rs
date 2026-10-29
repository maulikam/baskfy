//! The write routes' request plumbing for this lane: the body as bytes with its headers, and the
//! write pool a write method was handed (`session::write_access`).

use axum::extract::{Extension, Request};
use axum::http::{HeaderMap, header};

use crate::problems::{Problem, framework, internal_error};
use crate::session::WritePool;

/// Larger than any upload a route accepts (20 MiB tradebooks), so the route's own size check —
/// which names the size — is what refuses an oversized file, as in Python.
const MAX_BODY_BYTES: usize = 64 * 1024 * 1024;

/// The request's headers, `Content-Type` (latin-1, as Starlette decodes it) and body.
///
/// # Errors
/// Starlette's parse 400 for a body that cannot be read.
pub(crate) async fn read_body(
    request: Request,
) -> Result<(HeaderMap, Option<String>, Vec<u8>), Problem> {
    let headers = request.headers().clone();
    let content_type = headers
        .get(header::CONTENT_TYPE)
        .map(|v| v.as_bytes().iter().map(|&b| char::from(b)).collect());
    let bytes = axum::body::to_bytes(request.into_body(), MAX_BODY_BYTES)
        .await
        .map_err(|_| framework(400, "There was an error parsing the body"))?;
    Ok((headers, content_type, bytes.to_vec()))
}

/// A header as Starlette's `Headers.get` reads it: the first value, latin-1.
pub(crate) fn header_value(headers: &HeaderMap, name: &str) -> Option<String> {
    headers
        .get(name)
        .map(|v| v.as_bytes().iter().map(|&b| char::from(b)).collect())
}

/// The write pool, or the 500 a request with no session gets.
///
/// # Errors
/// `internal_error()` when no write pool is configured.
pub(crate) fn pool(pool: Option<Extension<WritePool>>) -> Result<WritePool, Problem> {
    pool.map(|Extension(p)| p).ok_or_else(internal_error)
}
