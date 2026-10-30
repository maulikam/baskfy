//! The HTTP faces the options and FO desks share: FastAPI's `Form(...)` validation (its 422
//! bodies, probed from fastapi 0.141.1 / pydantic 2.13.4), Starlette's `JSONResponse` bytes
//! (`json.dumps(..., ensure_ascii=False, separators=(",", ":"))` — not the app's default
//! orjson, because `options_desk`/`fno_desk` import Starlette's class), `TemplateResponse`, and
//! Starlette's 500.

use std::fmt::Write as _;

use axum::body::Body;
use axum::http::{HeaderValue, Response, StatusCode};

use crate::app::AppState;
use crate::wire::{PyDict, PyValue, dict};

/// `application/x-www-form-urlencoded` pairs, in order (`+` is a space, `%XX` decoded).
#[must_use]
pub fn parse_form(body: &[u8]) -> Vec<(String, String)> {
    fn decode(s: &str) -> String {
        let bytes = s.as_bytes();
        let mut out = Vec::with_capacity(bytes.len());
        let mut i = 0;
        while i < bytes.len() {
            match bytes[i] {
                b'+' => out.push(b' '),
                b'%' if i + 2 < bytes.len() => {
                    let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).unwrap_or("");
                    if let Ok(b) = u8::from_str_radix(hex, 16) {
                        out.push(b);
                        i += 2;
                    } else {
                        out.push(b'%');
                    }
                }
                b => out.push(b),
            }
            i += 1;
        }
        String::from_utf8_lossy(&out).into_owned()
    }
    let text = String::from_utf8_lossy(body);
    text.split('&')
        .filter(|p| !p.is_empty())
        .map(|p| match p.split_once('=') {
            Some((k, v)) => (decode(k), decode(v)),
            None => (decode(p), String::new()),
        })
        .collect()
}

/// A request's form fields, read as FastAPI reads them: only a form body has fields; the last
/// value of a repeated key wins; an empty string is a missing field.
#[derive(Debug, Default)]
pub struct Form {
    pairs: Vec<(String, String)>,
    errors: Vec<PyValue>,
}

impl Form {
    /// From the request's content type and body.
    #[must_use]
    pub fn new(content_type: Option<&str>, body: &[u8]) -> Self {
        let is_form = content_type.is_some_and(|c| {
            let c = c.to_ascii_lowercase();
            c.starts_with("application/x-www-form-urlencoded")
        });
        Self {
            pairs: if is_form {
                parse_form(body)
            } else {
                Vec::new()
            },
            errors: Vec::new(),
        }
    }

    fn raw(&self, name: &str) -> Option<&str> {
        self.pairs
            .iter()
            .rev()
            .find(|(k, _)| k == name)
            .map(|(_, v)| v.as_str())
            .filter(|v| !v.is_empty())
    }

    fn missing(&mut self, name: &str) {
        self.errors.push(PyValue::Map(dict([
            ("type", PyValue::Str("missing".to_owned())),
            (
                "loc",
                PyValue::List(vec![
                    PyValue::Str("body".to_owned()),
                    PyValue::Str(name.to_owned()),
                ]),
            ),
            ("msg", PyValue::Str("Field required".to_owned())),
            ("input", PyValue::None),
        ])));
    }

    /// `name: str = Form(...)`.
    pub fn required(&mut self, name: &str) -> String {
        if let Some(v) = self.raw(name) {
            return v.to_owned();
        }
        self.missing(name);
        String::new()
    }

    /// `name: str = Form(default)`.
    #[must_use]
    pub fn optional(&self, name: &str, default: &str) -> String {
        self.raw(name).unwrap_or(default).to_owned()
    }

    /// `name: int = Form(...)`, pydantic's lax int: surrounding whitespace allowed, a float
    /// with no fractional part allowed.
    pub fn required_int(&mut self, name: &str) -> i64 {
        let Some(raw) = self.raw(name).map(ToOwned::to_owned) else {
            self.missing(name);
            return 0;
        };
        let t = raw.trim();
        let digits = t.strip_prefix(['+', '-']).unwrap_or(t);
        if !digits.is_empty()
            && digits.chars().all(|c| c.is_ascii_digit() || c == '_')
            && !digits.starts_with('_')
            && !digits.ends_with('_')
            && let Ok(v) = t.replace('_', "").parse::<i64>()
        {
            return v;
        }
        if let Ok(f) = t.parse::<f64>()
            && f.is_finite()
        {
            if f.fract() == 0.0 {
                #[allow(clippy::cast_possible_truncation)]
                return f as i64;
            }
            self.errors.push(PyValue::Map(dict([
                ("type", PyValue::Str("int_from_float".to_owned())),
                (
                    "loc",
                    PyValue::List(vec![
                        PyValue::Str("body".to_owned()),
                        PyValue::Str(name.to_owned()),
                    ]),
                ),
                (
                    "msg",
                    PyValue::Str(
                        "Input should be a valid integer, got a number with a fractional part"
                            .to_owned(),
                    ),
                ),
                ("input", PyValue::Str(raw.clone())),
            ])));
            return 0;
        }
        self.errors.push(PyValue::Map(dict([
            ("type", PyValue::Str("int_parsing".to_owned())),
            (
                "loc",
                PyValue::List(vec![
                    PyValue::Str("body".to_owned()),
                    PyValue::Str(name.to_owned()),
                ]),
            ),
            (
                "msg",
                PyValue::Str(
                    "Input should be a valid integer, unable to parse string as an integer"
                        .to_owned(),
                ),
            ),
            ("input", PyValue::Str(raw.clone())),
        ])));
        0
    }

    /// FastAPI's 422 when any field failed.
    #[must_use]
    pub fn rejection(&self) -> Option<Response<Body>> {
        if self.errors.is_empty() {
            return None;
        }
        Some(json(
            422,
            &PyValue::Map(dict([("detail", PyValue::List(self.errors.clone()))])),
        ))
    }
}

fn json_str(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

fn encode(value: &PyValue, out: &mut String) -> Result<(), String> {
    match value {
        PyValue::None => out.push_str("null"),
        PyValue::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        PyValue::Int(i) => {
            let _ = write!(out, "{i}");
        }
        PyValue::Float(f) => {
            if !f.is_finite() {
                return Err("ValueError: Out of range float values are not JSON compliant".into());
            }
            out.push_str(&baskfy_core::py::float_repr(*f));
        }
        PyValue::Str(s) => json_str(s, out),
        PyValue::List(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                encode(item, out)?;
            }
            out.push(']');
        }
        PyValue::Map(m) => {
            out.push('{');
            for (i, (k, v)) in m.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                json_str(k, out);
                out.push(':');
                encode(v, out)?;
            }
            out.push('}');
        }
        other => {
            return Err(format!(
                "TypeError: Object of type {} is not JSON serializable",
                other.py_str()
            ));
        }
    }
    Ok(())
}

/// Starlette's `JSONResponse.render`: compact `json.dumps`, non-ASCII kept.
///
/// # Errors
/// A value `json.dumps` refuses.
pub fn starlette_json(value: &PyValue) -> Result<String, String> {
    let mut out = String::new();
    encode(value, &mut out)?;
    Ok(out)
}

/// `JSONResponse(content, status_code)`.
#[must_use]
pub fn json(status: u16, value: &PyValue) -> Response<Body> {
    match starlette_json(value) {
        Ok(text) => {
            let mut r = Response::new(Body::from(text));
            *r.status_mut() = StatusCode::from_u16(status).unwrap_or(StatusCode::OK);
            r.headers_mut().insert(
                axum::http::header::CONTENT_TYPE,
                HeaderValue::from_static("application/json"),
            );
            r
        }
        Err(e) => internal_error(&e),
    }
}

/// `{"code": code, "detail": detail}` with `status`.
#[must_use]
pub fn refusal(status: u16, code: &str, detail: &str) -> Response<Body> {
    json(
        status,
        &PyValue::Map(dict([
            ("code", PyValue::Str(code.to_owned())),
            ("detail", PyValue::Str(detail.to_owned())),
        ])),
    )
}

/// Starlette's `ServerErrorMiddleware` answer to an unhandled exception.
#[must_use]
pub fn internal_error(_why: &str) -> Response<Body> {
    let mut r = Response::new(Body::from("Internal Server Error"));
    *r.status_mut() = StatusCode::INTERNAL_SERVER_ERROR;
    r.headers_mut().insert(
        axum::http::header::CONTENT_TYPE,
        HeaderValue::from_static("text/plain; charset=utf-8"),
    );
    r
}

/// `templates.TemplateResponse(request, name, context)`.
#[must_use]
pub fn render(state: &AppState, path: &str, name: &str, context: PyDict) -> Response<Body> {
    let mut ctx = context;
    ctx.insert(
        "request",
        PyValue::Map(dict([(
            "url",
            PyValue::Map(dict([("path", PyValue::Str(path.to_owned()))])),
        )])),
    );
    let value = crate::templates::py_value(&PyValue::Map(ctx));
    match state.templates.render(name, value) {
        Ok(text) => {
            let mut r = Response::new(Body::from(text));
            r.headers_mut().insert(
                axum::http::header::CONTENT_TYPE,
                HeaderValue::from_static("text/html; charset=utf-8"),
            );
            r
        }
        Err(e) => internal_error(&e.to_string()),
    }
}

/// The request's content type.
#[must_use]
pub fn content_type(headers: &axum::http::HeaderMap) -> Option<String> {
    headers
        .get(axum::http::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .map(ToOwned::to_owned)
}
