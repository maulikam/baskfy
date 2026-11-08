//! JSON answers for the web app's ports of the desk pages (`Accept: application/json` only);
//! a browser form post still gets the desk's 303.

use axum::body::Body;
use axum::http::{HeaderMap, Response, StatusCode, header};

use super::web::{json, redirect_303};
use crate::wire::{PyDict, PyValue, dict};

/// The request asked for JSON.
#[must_use]
pub fn wants_json(headers: &HeaderMap) -> bool {
    headers
        .get(header::ACCEPT)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|a| a.contains("application/json"))
}

/// A form handler's outcome: 200 `{ok, message}`, 400 `{detail}` when refused, else the 303.
#[must_use]
pub fn outcome(headers: &HeaderMap, ok: bool, url: &str, message: &str) -> Response<Body> {
    if !wants_json(headers) {
        return redirect_303(url);
    }
    if ok {
        json(
            StatusCode::OK,
            &PyValue::Map(dict([
                ("ok", PyValue::Bool(true)),
                ("message", PyValue::Str(message.to_owned())),
            ])),
        )
    } else {
        json(
            StatusCode::BAD_REQUEST,
            &PyValue::Map(dict([("detail", PyValue::Str(message.to_owned()))])),
        )
    }
}

/// A page's template context as JSON.
#[must_use]
pub fn context(ctx: PyDict) -> Response<Body> {
    json(StatusCode::OK, &PyValue::Map(ctx))
}
