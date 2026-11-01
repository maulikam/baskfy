//! What Starlette and FastAPI do around the views lane's handlers: `TemplateResponse`,
//! `RedirectResponse` (with its URL quoting), `ORJSONResponse`, `HTTPException`, the query
//! string and `await request.form()` — so each handler reads like its Python.

use axum::body::Body;
use axum::http::{HeaderValue, Response, StatusCode, header};
use minijinja::Value;

use crate::app::AppState;
use crate::storage::{DbValue, Row, StorageError};
use crate::wire::{PyDict, PyValue, dict, orjson_dumps};

/// `HTMLResponse(text)`: `text/html; charset=utf-8`.
#[must_use]
pub fn html(status: StatusCode, text: String) -> Response<Body> {
    let mut r = Response::new(Body::from(text));
    *r.status_mut() = status;
    r.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/html; charset=utf-8"),
    );
    r
}

/// `ORJSONResponse(value)`.
#[must_use]
pub fn json(status: StatusCode, value: &PyValue) -> Response<Body> {
    match orjson_dumps(value) {
        Ok(text) => {
            let mut r = Response::new(Body::from(text));
            *r.status_mut() = status;
            r.headers_mut().insert(
                header::CONTENT_TYPE,
                HeaderValue::from_static("application/json"),
            );
            r
        }
        Err(e) => internal_error(&e.to_string()),
    }
}

/// `HTTPException(status, detail)` → `{"detail": ...}`.
#[must_use]
pub fn detail(status: StatusCode, message: &str) -> Response<Body> {
    json(
        status,
        &PyValue::Map(dict([("detail", PyValue::Str(message.to_owned()))])),
    )
}

/// An unhandled exception: Starlette's `ServerErrorMiddleware` answers a plain-text 500.
#[must_use]
pub fn internal_error(message: &str) -> Response<Body> {
    let _ = message;
    let mut r = Response::new(Body::from("Internal Server Error"));
    *r.status_mut() = StatusCode::INTERNAL_SERVER_ERROR;
    r.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/plain; charset=utf-8"),
    );
    r
}

/// The bytes `urllib.parse.quote(url, safe=":/%#?=@[]!$&'()*+,;")` leaves alone.
fn starlette_safe(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b"_.-~:/%#?=@[]!$&'()*+,;".contains(&b)
}

/// `RedirectResponse`'s `quote(str(url), safe=...)`.
#[must_use]
pub fn quote_location(url: &str) -> String {
    use std::fmt::Write as _;
    let mut out = String::with_capacity(url.len());
    for b in url.bytes() {
        if starlette_safe(b) {
            out.push(char::from(b));
        } else {
            let _ = write!(out, "%{b:02X}");
        }
    }
    out
}

/// `RedirectResponse(url, status_code=303)`.
#[must_use]
pub fn redirect_303(url: &str) -> Response<Body> {
    let mut r = Response::new(Body::empty());
    *r.status_mut() = StatusCode::SEE_OTHER;
    if let Ok(v) = HeaderValue::from_str(&quote_location(url)) {
        r.headers_mut().insert(header::LOCATION, v);
    }
    r.headers_mut()
        .insert(header::CONTENT_LENGTH, HeaderValue::from_static("0"));
    r
}

/// `RedirectResponse(url)`: Starlette's default 307.
#[must_use]
pub fn redirect_307(url: &str) -> Response<Body> {
    let mut r = redirect_303(url);
    *r.status_mut() = StatusCode::TEMPORARY_REDIRECT;
    r
}

fn hex(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

/// `urllib.parse.unquote_plus` (invalid UTF-8 replaced, as Python's default `errors`).
#[must_use]
pub fn unquote_plus(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            b'%' => {
                let hi = bytes.get(i + 1).copied().and_then(hex);
                let lo = bytes.get(i + 2).copied().and_then(hex);
                if let (Some(hi), Some(lo)) = (hi, lo) {
                    out.push(hi * 16 + lo);
                    i += 3;
                } else {
                    out.push(b'%');
                    i += 1;
                }
            }
            b => {
                out.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// `parse_qsl(text, keep_blank_values=True)`: the pairs, in order.
#[must_use]
pub fn parse_qsl(text: &str) -> Vec<(String, String)> {
    text.split('&')
        .filter(|p| !p.is_empty())
        .map(|p| {
            let (k, v) = p.split_once('=').unwrap_or((p, ""));
            (unquote_plus(k), unquote_plus(v))
        })
        .collect()
}

/// A query string or a form: Starlette's `ImmutableMultiDict`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct MultiDict(pub Vec<(String, String)>);

impl MultiDict {
    /// From the request's query string.
    #[must_use]
    pub fn from_query(uri: &axum::http::Uri) -> Self {
        Self(parse_qsl(uri.query().unwrap_or_default()))
    }

    /// From an `application/x-www-form-urlencoded` body.
    #[must_use]
    pub fn from_form(body: &[u8]) -> Self {
        Self(parse_qsl(&String::from_utf8_lossy(body)))
    }

    /// `d.get(key)`: the **last** value for the key (Starlette's `_dict`).
    #[must_use]
    pub fn get(&self, key: &str) -> Option<&str> {
        self.0
            .iter()
            .rev()
            .find(|(k, _)| k == key)
            .map(|(_, v)| v.as_str())
    }

    /// `d.getlist(key)`.
    #[must_use]
    pub fn getlist(&self, key: &str) -> Vec<String> {
        self.0
            .iter()
            .filter(|(k, _)| k == key)
            .map(|(_, v)| v.clone())
            .collect()
    }

    /// `d.items()`: each key once, at its first position, with its last value.
    #[must_use]
    pub fn items(&self) -> Vec<(String, String)> {
        let mut out: Vec<(String, String)> = Vec::new();
        for (k, v) in &self.0 {
            if let Some(slot) = out.iter_mut().find(|(kk, _)| kk == k) {
                slot.1.clone_from(v);
            } else {
                out.push((k.clone(), v.clone()));
            }
        }
        out
    }

    /// A FastAPI `str = ""` query parameter.
    #[must_use]
    pub fn str_or_empty(&self, key: &str) -> String {
        self.get(key).unwrap_or_default().to_owned()
    }
}

/// FastAPI's 422 for a missing required field (`loc` is `["query", name]` or `["body", name]`).
#[must_use]
pub fn missing_field(location: &str, name: &str) -> Response<Body> {
    let item = PyValue::Map(dict([
        ("type", PyValue::Str("missing".to_owned())),
        (
            "loc",
            PyValue::List(vec![
                PyValue::Str(location.to_owned()),
                PyValue::Str(name.to_owned()),
            ]),
        ),
        ("msg", PyValue::Str("Field required".to_owned())),
        ("input", PyValue::None),
    ]));
    json(
        StatusCode::UNPROCESSABLE_ENTITY,
        &PyValue::Map(dict([("detail", PyValue::List(vec![item]))])),
    )
}

/// FastAPI's 422 for a path or query integer that does not parse.
#[must_use]
pub fn int_parsing(location: &str, name: &str, input: &str) -> Response<Body> {
    let item = PyValue::Map(dict([
        ("type", PyValue::Str("int_parsing".to_owned())),
        (
            "loc",
            PyValue::List(vec![
                PyValue::Str(location.to_owned()),
                PyValue::Str(name.to_owned()),
            ]),
        ),
        (
            "msg",
            PyValue::Str(
                "Input should be a valid integer, unable to parse string as an integer".to_owned(),
            ),
        ),
        ("input", PyValue::Str(input.to_owned())),
    ]));
    json(
        StatusCode::UNPROCESSABLE_ENTITY,
        &PyValue::Map(dict([("detail", PyValue::List(vec![item]))])),
    )
}

/// One column as Python sees it after `dict(row)`.
#[must_use]
pub fn db_to_py(v: &DbValue) -> PyValue {
    use baskfy_providers::records::PyDateTime;
    match v {
        DbValue::Null => PyValue::None,
        DbValue::Int(i) => PyValue::Int(*i),
        DbValue::Float(f) => PyValue::Float(*f),
        DbValue::Text(s) => PyValue::Str(s.clone()),
        DbValue::Bytes(b) => PyValue::Bytes(b.clone()),
        DbValue::Bool(b) => PyValue::Bool(*b),
        DbValue::Aware(t) => PyValue::DateTime(PyDateTime::Aware(*t)),
        DbValue::Naive(t) => PyValue::DateTime(PyDateTime::Naive(*t)),
        DbValue::Json(s) => PyValue::from_json(s.as_bytes()).unwrap_or(PyValue::Str(s.clone())),
    }
}

/// `dict(row)`.
#[must_use]
pub fn row_dict(row: &Row) -> PyDict {
    let mut out = PyDict::new();
    for (k, v) in &row.0 {
        out.insert(k.clone(), db_to_py(v));
    }
    out
}

/// `[dict(r) for r in rows]`.
#[must_use]
pub fn rows_py(rows: &[Row]) -> PyValue {
    PyValue::List(rows.iter().map(|r| PyValue::Map(row_dict(r))).collect())
}

/// `{"url": {"path": ...}}` — what the desk's templates read off `request`.
#[must_use]
pub fn request_value(path: &str) -> PyValue {
    PyValue::Map(dict([(
        "url",
        PyValue::Map(dict([("path", PyValue::Str(path.to_owned()))])),
    )]))
}

/// `templates.TemplateResponse(request, name, context)`.
#[must_use]
pub fn render(state: &AppState, path: &str, name: &str, context: PyDict) -> Response<Body> {
    let mut ctx = context;
    ctx.insert("request", request_value(path));
    let value: Value = crate::templates::py_value(&PyValue::Map(ctx));
    match state.templates.render(name, value) {
        Ok(text) => html(StatusCode::OK, text),
        Err(e) => internal_error(&e.to_string()),
    }
}

/// A storage error is an unhandled exception in Python: a 500.
#[must_use]
pub fn storage_500(e: &StorageError) -> Response<Body> {
    internal_error(&e.to_string())
}

/// `datetime.now().isoformat(timespec="seconds")` on the desk's (IST) clock.
#[must_use]
pub fn now_iso_seconds(state: &AppState) -> String {
    state
        .clock
        .now_ist()
        .naive_local()
        .format("%Y-%m-%dT%H:%M:%S")
        .to_string()
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn quote_matches_starlette() {
        assert_eq!(
            quote_location("/settings?saved=2 changed: A, B"),
            "/settings?saved=2%20changed:%20A,%20B"
        );
        assert_eq!(quote_location("/x?e=₹ \"q\""), "/x?e=%E2%82%B9%20%22q%22");
    }

    #[test]
    fn multidict_last_value_first_position() {
        let d = MultiDict::from_form(b"a=1&b=2&a=3&c=%E2%82%B9+x");
        assert_eq!(d.get("a"), Some("3"));
        assert_eq!(
            d.items(),
            vec![
                ("a".to_owned(), "3".to_owned()),
                ("b".to_owned(), "2".to_owned()),
                ("c".to_owned(), "₹ x".to_owned())
            ]
        );
        assert_eq!(d.getlist("a"), vec!["1".to_owned(), "3".to_owned()]);
    }
}
