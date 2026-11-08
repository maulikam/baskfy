//! The desk's Python values and the bytes they become (DECISIONS-RS R8.1).
//!
//! **One dynamic value.** A plan, a gateway result, a journal line and a Kite payload are Python
//! dicts in the desk; here they are [`PyValue`] (= `baskfy_providers::kite::KiteValue`, the
//! Python object model the provider port already grades: ordered `dict`, `int`/`float`/`bool`
//! kept apart, `date`/`datetime`, `bytes`). Lanes may use typed structs and convert at the edge.
//!
//! **Three encoders, because the desk uses three:**
//! * [`orjson_dumps`] — every route's JSON (`ORJSONResponse` is the app's default response
//!   class): compact, UTF-8 unescaped, floats in orjson's notation ([`orjson_float`]:
//!   positional for a decimal exponent in `[-5, 16)`, else `1e-6`/`1e+16` with no zero
//!   padding), NaN/inf → `null`, `datetime` → ISO 8601.
//! * [`py_json_dumps`] — `json.dumps(...)` with Python's defaults (the order journal, the
//!   `execution_<plan>.json` log, Kite's GTT `condition`/`orders` payloads).
//! * `json.dumps(..., indent=2)` for files is the owning lane's (one call site).

use std::fmt::Write as _;

pub use baskfy_providers::kite::{KiteMap as PyDict, KiteValue as PyValue, py_json_dumps};
use baskfy_providers::records::PyDateTime;

/// A value orjson refuses (`TypeError: Type is not JSON serializable`).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("TypeError: Type is not JSON serializable: {0}")]
pub struct WireError(pub String);

/// orjson's float text: shortest round-trip digits (Python's `repr` digits), positional for a
/// decimal exponent in `[-5, 16)` with `.0` on an integral value, else `<digits>e<sign><exp>`
/// without zero padding and with `+` on a positive exponent. Not finite → `None` (orjson writes
/// `null`).
#[must_use]
pub fn orjson_float(x: f64) -> Option<String> {
    if !x.is_finite() {
        return None;
    }
    let repr = baskfy_core::py::float_repr(x);
    let Some((mantissa, exp)) = repr.split_once('e') else {
        return Some(repr);
    };
    let exp: i32 = exp.parse().unwrap_or(0);
    if exp == -5 {
        // Python writes 1e-05; orjson keeps it positional.
        let (sign, digits) = mantissa
            .strip_prefix('-')
            .map_or(("", mantissa), |m| ("-", m));
        let digits: String = digits.chars().filter(char::is_ascii_digit).collect();
        return Some(format!("{sign}0.0000{digits}"));
    }
    let sign = if exp < 0 { "-" } else { "+" };
    Some(format!("{mantissa}e{sign}{}", exp.unsigned_abs()))
}

fn orjson_str(s: &str, out: &mut String) {
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

fn iso_datetime(dt: &PyDateTime) -> String {
    match dt {
        PyDateTime::Naive(t) => {
            if t.and_utc().timestamp_subsec_micros() == 0 {
                t.format("%Y-%m-%dT%H:%M:%S").to_string()
            } else {
                t.format("%Y-%m-%dT%H:%M:%S%.6f").to_string()
            }
        }
        PyDateTime::Aware(t) => {
            if t.timestamp_subsec_micros() == 0 {
                t.format("%Y-%m-%dT%H:%M:%S%:z").to_string()
            } else {
                t.format("%Y-%m-%dT%H:%M:%S%.6f%:z").to_string()
            }
        }
    }
}

fn orjson_into(value: &PyValue, out: &mut String) -> Result<(), WireError> {
    match value {
        PyValue::None => out.push_str("null"),
        PyValue::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        PyValue::Int(i) => {
            let _ = write!(out, "{i}");
        }
        PyValue::Float(f) => out.push_str(&orjson_float(*f).unwrap_or_else(|| "null".to_owned())),
        PyValue::Str(s) => orjson_str(s, out),
        PyValue::Date(d) => {
            let _ = write!(out, "\"{}\"", d.format("%Y-%m-%d"));
        }
        PyValue::DateTime(dt) => {
            out.push('"');
            out.push_str(&iso_datetime(dt));
            out.push('"');
        }
        PyValue::Bytes(_) => return Err(WireError("bytes".to_owned())),
        PyValue::List(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                orjson_into(item, out)?;
            }
            out.push(']');
        }
        PyValue::Map(map) => {
            out.push('{');
            for (i, (k, v)) in map.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                orjson_str(k, out);
                out.push(':');
                orjson_into(v, out)?;
            }
            out.push('}');
        }
    }
    Ok(())
}

/// `orjson.dumps(value)` as `ORJSONResponse.render` calls it (UTF-8 text).
///
/// # Errors
/// A value orjson cannot serialise (`bytes`).
pub fn orjson_dumps(value: &PyValue) -> Result<String, WireError> {
    let mut out = String::new();
    orjson_into(value, &mut out)?;
    Ok(out)
}

/// Build a [`PyDict`] from `(key, value)` pairs, in order.
#[must_use]
pub fn dict<K: Into<String>>(pairs: impl IntoIterator<Item = (K, PyValue)>) -> PyDict {
    let mut map = PyDict::new();
    for (k, v) in pairs {
        map.insert(k, v);
    }
    map
}
