//! Python's `dict` and `json.dumps`, as the worker's graded payloads need them.
//!
//! A step's `detail` is a Python `dict` mutated with `dict.update` (a key already present keeps
//! its position and takes the new value; a new key goes last), and an alert email's body is
//! `json.dumps(payload, indent=2, default=str)` — `ensure_ascii`, `", "`/`": "` separators with
//! newlines, `repr(float)`. Both are reproduced here over [`OrderedJson`].

use std::fmt::Write as _;

use baskfy_db::OrderedJson;

/// `d.update({key: value})` on an object's entries.
pub fn update(entries: &mut Vec<(String, OrderedJson)>, key: &str, value: OrderedJson) {
    if let Some(slot) = entries.iter_mut().find(|(k, _)| k == key) {
        slot.1 = value;
    } else {
        entries.push((key.to_owned(), value));
    }
}

/// A JSON object from `(key, value)` pairs, later duplicates updating earlier ones.
#[must_use]
pub fn object<K: Into<String>>(pairs: impl IntoIterator<Item = (K, OrderedJson)>) -> OrderedJson {
    let mut entries = Vec::new();
    for (k, v) in pairs {
        update(&mut entries, &k.into(), v);
    }
    OrderedJson::Object(entries)
}

/// A JSON string.
#[must_use]
pub fn s(text: impl Into<String>) -> OrderedJson {
    OrderedJson::Str(text.into())
}

/// A JSON integer.
#[must_use]
pub fn int(value: i64) -> OrderedJson {
    OrderedJson::Int(value)
}

/// `None` or a string.
#[must_use]
pub fn opt_s(text: Option<impl Into<String>>) -> OrderedJson {
    text.map_or(OrderedJson::Null, |t| OrderedJson::Str(t.into()))
}

/// A list of strings.
#[must_use]
pub fn strings<S: Into<String>>(items: impl IntoIterator<Item = S>) -> OrderedJson {
    OrderedJson::Array(
        items
            .into_iter()
            .map(|i| OrderedJson::Str(i.into()))
            .collect(),
    )
}

/// `json.dumps(value)` with Python's default separators (`", "`, `": "`) and `ensure_ascii`.
///
/// # Errors
/// A non-finite float (`allow_nan` would print `NaN`; no graded payload carries one).
pub fn dumps(value: &OrderedJson) -> Result<String, String> {
    let mut out = String::new();
    write_value(&mut out, value, None, 0)?;
    Ok(out)
}

/// `json.dumps(value, indent=2)` (`ensure_ascii`, `","` item separator before each newline).
///
/// # Errors
/// As [`dumps`].
pub fn dumps_indent2(value: &OrderedJson) -> Result<String, String> {
    let mut out = String::new();
    write_value(&mut out, value, Some(2), 0)?;
    Ok(out)
}

fn newline(out: &mut String, indent: Option<usize>, level: usize) {
    if let Some(width) = indent {
        out.push('\n');
        out.push_str(&" ".repeat(width * level));
    }
}

fn write_value(
    out: &mut String,
    value: &OrderedJson,
    indent: Option<usize>,
    level: usize,
) -> Result<(), String> {
    match value {
        OrderedJson::Null => out.push_str("null"),
        OrderedJson::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        OrderedJson::Int(i) => {
            let _ = write!(out, "{i}");
        }
        OrderedJson::UInt(u) => {
            let _ = write!(out, "{u}");
        }
        OrderedJson::BigInt(digits) => out.push_str(digits),
        OrderedJson::Float(f) => {
            if !f.is_finite() {
                return Err("Out of range float values are not JSON compliant".to_owned());
            }
            out.push_str(&baskfy_core::py::float_repr(*f));
        }
        OrderedJson::Str(text) => write_str(out, text),
        OrderedJson::Array(items) => {
            if items.is_empty() {
                out.push_str("[]");
                return Ok(());
            }
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                    if indent.is_none() {
                        out.push(' ');
                    }
                }
                newline(out, indent, level + 1);
                write_value(out, item, indent, level + 1)?;
            }
            newline(out, indent, level);
            out.push(']');
        }
        OrderedJson::Object(entries) => {
            if entries.is_empty() {
                out.push_str("{}");
                return Ok(());
            }
            out.push('{');
            for (i, (k, v)) in entries.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                    if indent.is_none() {
                        out.push(' ');
                    }
                }
                newline(out, indent, level + 1);
                write_str(out, k);
                out.push_str(": ");
                write_value(out, v, indent, level + 1)?;
            }
            newline(out, indent, level);
            out.push('}');
        }
    }
    Ok(())
}

/// A JSON string with `ensure_ascii=True` escapes (`py_encode_basestring_ascii`).
fn write_str(out: &mut String, text: &str) {
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (' '..='~').contains(&c) => out.push(c),
            c => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    let _ = write!(out, "\\u{unit:04x}");
                }
            }
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn update_keeps_position_and_appends_new_keys() {
        let mut d = vec![("a".to_owned(), int(1)), ("b".to_owned(), int(2))];
        update(&mut d, "a", int(3));
        update(&mut d, "c", int(4));
        assert_eq!(
            dumps(&OrderedJson::Object(d)).unwrap(),
            r#"{"a": 3, "b": 2, "c": 4}"#
        );
    }

    #[test]
    fn indent_two_is_pythons() {
        // json.dumps({"a": [1, 2.5, "é"], "b": {}, "c": [], "d": None}, indent=2)
        let v = object([
            (
                "a",
                OrderedJson::Array(vec![int(1), OrderedJson::Float(2.5), s("é")]),
            ),
            ("b", OrderedJson::Object(vec![])),
            ("c", OrderedJson::Array(vec![])),
            ("d", OrderedJson::Null),
        ]);
        assert_eq!(
            dumps_indent2(&v).unwrap(),
            "{\n  \"a\": [\n    1,\n    2.5,\n    \"\\u00e9\"\n  ],\n  \"b\": {},\n  \"c\": [],\n  \"d\": null\n}"
        );
    }

    #[test]
    fn astral_characters_are_surrogate_pairs() {
        assert_eq!(dumps(&s("\u{1F600}")).unwrap(), "\"\\ud83d\\ude00\"");
    }
}
