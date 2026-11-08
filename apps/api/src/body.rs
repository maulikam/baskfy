//! Request bodies as FastAPI reads them and pydantic validates them — the write routes' half of
//! what [`crate::query`] is for query strings.
//!
//! FastAPI (`fastapi.routing.get_request_handler` / `request_body_to_args`), for a route with one
//! non-embedded model body:
//!
//! * an **empty** body is the field missing: `{"field": "<root>", "message": "Field required",
//!   "type": "missing"}`;
//! * the body is parsed as JSON only when its `Content-Type` has the main type `application` and
//!   the subtype `json` or `*+json` (`email.message` parses the header). FastAPI 0.13x's
//!   `strict_content_type` defaults to true, so a body with **no** `Content-Type` is not parsed
//!   either: it stays bytes, which the model then refuses (graded: `patch-me-no-content-type`);
//! * a JSON `null` is the field missing, as an empty body is (`json_body` is `None`, not
//!   `Undefined`, and `None` is "no value" to `request_body_to_args`);
//! * `json.loads(bytes)`: the encoding detected as `json.detect_encoding` does (UTF-8, a UTF-8
//!   BOM, UTF-16/32 by BOM or NUL pattern), then CPython's C scanner. A syntax error is
//!   `{"field": "<char index>", "message": "JSON decode error", "type": "json_invalid"}` — the
//!   index is exactly the scanner's (`JSONDecodeError.pos`), reproduced here case by case;
//! * anything else that fails while parsing (bytes that do not decode, nesting beyond the
//!   interpreter's recursion limit, an integer literal over 4,300 digits) is Starlette's
//!   `HTTPException(400, "There was an error parsing the body")`.
//!
//! Then pydantic's model validation in Python mode with `from_attributes=True` (what FastAPI's
//! `ModelField.validate` passes): a non-object is `model_attributes_type`; fields are checked in
//! declaration order, then each extra key in arrival order (`extra="forbid"`).
//!
//! Known limits (DECISIONS-RS R6.7): a lone surrogate escape (`"\ud800"`, which Python keeps in a
//! `str`) is answered with the 500 catch-all, since no Rust `String` can hold it; Python accepts
//! it and fails later, or not at all if the field is then refused for another reason.

use baskfy_db::OrderedJson;

use crate::email_address::validate_email_str;
use crate::problems::{ErrorEntry, Problem, framework, internal_error, invalid_screen_definition};

/// CPython's default recursion limit, less the frames already on the stack when the decoder
/// runs; `json` raises `RecursionError` (not a `JSONDecodeError`) beyond it.
const MAX_DEPTH: usize = 980;
/// `sys.get_int_max_str_digits()` default: `int()` of a longer literal raises `ValueError`.
const INT_MAX_STR_DIGITS: usize = 4300;

/// What the request carried, after FastAPI's content-type rule.
#[derive(Debug, Clone, PartialEq)]
pub enum Received {
    /// An empty body.
    Missing,
    /// A body parsed as JSON.
    Json(OrderedJson),
    /// A body left as bytes (a non-JSON `Content-Type`).
    Raw,
}

/// One entry of FastAPI's validation error list.
fn entry(field: &str, message: &str, kind: &str) -> ErrorEntry {
    vec![
        ("field".to_owned(), OrderedJson::Str(field.to_owned())),
        ("message".to_owned(), OrderedJson::Str(message.to_owned())),
        ("type".to_owned(), OrderedJson::Str(kind.to_owned())),
    ]
}

/// The 400 Starlette answers when parsing fails for a reason that is not JSON syntax.
fn unparseable() -> Problem {
    framework(400, "There was an error parsing the body")
}

/// `email.message.Message().get_content_type()` reduced to "is this JSON?".
fn is_json_content_type(value: &str) -> bool {
    let ctype = value
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    if ctype.matches('/').count() != 1 {
        return false;
    }
    let (main, sub) = ctype.split_once('/').unwrap_or(("", ""));
    main == "application" && (sub == "json" || sub.ends_with("+json"))
}

/// `json.detect_encoding(b)` and the decode, as `Vec<char>` so positions are Python's indices.
fn decode_text(b: &[u8]) -> Option<Vec<char>> {
    const BOM8: &[u8] = &[0xEF, 0xBB, 0xBF];
    let utf16 = |bytes: &[u8], be: bool| -> Option<Vec<char>> {
        if !bytes.len().is_multiple_of(2) {
            return None;
        }
        let units: Vec<u16> = bytes
            .as_chunks::<2>()
            .0
            .iter()
            .map(|c| {
                if be {
                    u16::from_be_bytes([c[0], c[1]])
                } else {
                    u16::from_le_bytes([c[0], c[1]])
                }
            })
            .collect();
        char::decode_utf16(units)
            .collect::<Result<Vec<char>, _>>()
            .ok()
    };
    let utf32 = |bytes: &[u8], be: bool| -> Option<Vec<char>> {
        if !bytes.len().is_multiple_of(4) {
            return None;
        }
        bytes
            .as_chunks::<4>()
            .0
            .iter()
            .map(|c| {
                let v = if be {
                    u32::from_be_bytes([c[0], c[1], c[2], c[3]])
                } else {
                    u32::from_le_bytes([c[0], c[1], c[2], c[3]])
                };
                char::from_u32(v)
            })
            .collect()
    };
    if b.starts_with(&[0x00, 0x00, 0xFE, 0xFF]) {
        return utf32(&b[4..], true);
    }
    if b.starts_with(&[0xFF, 0xFE, 0x00, 0x00]) {
        return utf32(&b[4..], false);
    }
    if b.starts_with(&[0xFE, 0xFF]) {
        return utf16(&b[2..], true);
    }
    if b.starts_with(&[0xFF, 0xFE]) {
        return utf16(&b[2..], false);
    }
    if let Some(rest) = b.strip_prefix(BOM8) {
        return std::str::from_utf8(rest).ok().map(|s| s.chars().collect());
    }
    if b.len() >= 4 {
        if b[0] == 0 {
            return if b[1] != 0 {
                utf16(b, true)
            } else {
                utf32(b, true)
            };
        }
        if b[1] == 0 {
            return if b[2] != 0 || b[3] != 0 {
                utf16(b, false)
            } else {
                utf32(b, false)
            };
        }
    } else if b.len() == 2 {
        if b[0] == 0 {
            return utf16(b, true);
        }
        if b[1] == 0 {
            return utf16(b, false);
        }
    }
    std::str::from_utf8(b).ok().map(|s| s.chars().collect())
}

/// Why the scanner stopped.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Halt {
    /// A `JSONDecodeError` at this index.
    Syntax(usize),
    /// Not a syntax error: Starlette's 400.
    Unparseable,
    /// A lone surrogate (see the module docs).
    LoneSurrogate,
}

/// CPython's `_json` C scanner over a decoded string.
struct Scanner<'a> {
    s: &'a [char],
}

fn is_ws(c: char) -> bool {
    matches!(c, ' ' | '\t' | '\n' | '\r')
}

impl Scanner<'_> {
    fn at(&self, i: usize) -> Option<char> {
        self.s.get(i).copied()
    }

    fn skip_ws(&self, mut i: usize) -> usize {
        while self.at(i).is_some_and(is_ws) {
            i += 1;
        }
        i
    }

    fn has(&self, i: usize, word: &str) -> bool {
        word.chars()
            .enumerate()
            .all(|(k, c)| self.at(i + k) == Some(c))
    }

    /// `scan_once_unicode`: a value at `i`, and the index after it.
    fn value(&self, i: usize, depth: usize) -> Result<(OrderedJson, usize), Halt> {
        if depth > MAX_DEPTH {
            return Err(Halt::Unparseable);
        }
        match self.at(i) {
            None => Err(Halt::Syntax(i)),
            Some('"') => self.string(i + 1).map(|(s, e)| (OrderedJson::Str(s), e)),
            Some('{') => self.object(i + 1, depth),
            Some('[') => self.array(i + 1, depth),
            Some('n') if self.has(i, "null") => Ok((OrderedJson::Null, i + 4)),
            Some('t') if self.has(i, "true") => Ok((OrderedJson::Bool(true), i + 4)),
            Some('f') if self.has(i, "false") => Ok((OrderedJson::Bool(false), i + 5)),
            Some('N') if self.has(i, "NaN") => Ok((OrderedJson::Float(f64::NAN), i + 3)),
            Some('I') if self.has(i, "Infinity") => Ok((OrderedJson::Float(f64::INFINITY), i + 8)),
            Some('-') if self.has(i, "-Infinity") => {
                Ok((OrderedJson::Float(f64::NEG_INFINITY), i + 9))
            }
            Some(_) => self.number(i),
        }
    }

    /// `_match_number_unicode`.
    fn number(&self, start: usize) -> Result<(OrderedJson, usize), Halt> {
        let digit = |i: usize| self.at(i).is_some_and(|c| c.is_ascii_digit());
        let mut i = start;
        if self.at(i) == Some('-') {
            i += 1;
        }
        match self.at(i) {
            Some('1'..='9') => {
                while digit(i) {
                    i += 1;
                }
            }
            Some('0') => i += 1,
            _ => return Err(Halt::Syntax(start)),
        }
        let mut float = false;
        if self.at(i) == Some('.') && digit(i + 1) {
            float = true;
            i += 1;
            while digit(i) {
                i += 1;
            }
        }
        if matches!(self.at(i), Some('e' | 'E')) {
            let e_start = i;
            i += 1;
            if matches!(self.at(i), Some('-' | '+')) {
                i += 1;
            }
            let digits_from = i;
            while digit(i) {
                i += 1;
            }
            if i > digits_from {
                float = true;
            } else {
                i = e_start;
            }
        }
        let text: String = self.s[start..i].iter().collect();
        if float {
            let v: f64 = text.parse().map_err(|_| Halt::Unparseable)?;
            return Ok((OrderedJson::Float(v), i));
        }
        let digits = text.trim_start_matches('-');
        if digits.len() > INT_MAX_STR_DIGITS {
            return Err(Halt::Unparseable);
        }
        let value = if let Ok(v) = text.parse::<i64>() {
            OrderedJson::Int(v)
        } else if let Ok(v) = text.parse::<u64>() {
            OrderedJson::UInt(v)
        } else {
            OrderedJson::BigInt(text)
        };
        Ok((value, i))
    }

    fn hex4(&self, i: usize) -> Option<u32> {
        let mut v = 0u32;
        for k in 0..4 {
            v = (v << 4) | self.at(i + k)?.to_digit(16)?;
        }
        Some(v)
    }

    /// `scanstring_unicode(s, end)`, `end` just past the opening quote.
    fn string(&self, end: usize) -> Result<(String, usize), Halt> {
        let begin = end - 1;
        let len = self.s.len();
        let mut out = String::new();
        let mut i = end;
        loop {
            let Some(c) = self.at(i) else {
                return Err(Halt::Syntax(begin));
            };
            match c {
                '"' => return Ok((out, i + 1)),
                '\\' => {
                    let Some(e) = self.at(i + 1) else {
                        return Err(Halt::Syntax(begin));
                    };
                    if e != 'u' {
                        out.push(match e {
                            '"' => '"',
                            '\\' => '\\',
                            '/' => '/',
                            'b' => '\u{8}',
                            'f' => '\u{c}',
                            'n' => '\n',
                            'r' => '\r',
                            't' => '\t',
                            _ => return Err(Halt::Syntax(i)),
                        });
                        i += 2;
                        continue;
                    }
                    let first = i + 2;
                    // `end = next + 4; if (end >= len)` — four digits and one more character.
                    if first + 4 >= len {
                        return Err(Halt::Syntax(i + 1));
                    }
                    let Some(c1) = self.hex4(first) else {
                        return Err(Halt::Syntax(i + 1));
                    };
                    let mut after = first + 4;
                    let mut code = c1;
                    if (0xD800..0xDC00).contains(&c1)
                        && after + 6 < len
                        && self.at(after) == Some('\\')
                        && self.at(after + 1) == Some('u')
                    {
                        let Some(c2) = self.hex4(after + 2) else {
                            return Err(Halt::Syntax(after + 1));
                        };
                        if (0xDC00..0xE000).contains(&c2) {
                            code = 0x10000 + ((c1 - 0xD800) << 10) + (c2 - 0xDC00);
                            after += 6;
                        }
                    }
                    out.push(char::from_u32(code).ok_or(Halt::LoneSurrogate)?);
                    i = after;
                }
                c if u32::from(c) <= 0x1f => return Err(Halt::Syntax(i)),
                c => {
                    out.push(c);
                    i += 1;
                }
            }
        }
    }

    /// `_parse_object_unicode`, `i` just past `{`.
    fn object(&self, i: usize, depth: usize) -> Result<(OrderedJson, usize), Halt> {
        let mut entries: Vec<(String, OrderedJson)> = Vec::new();
        let mut i = self.skip_ws(i);
        if self.at(i) != Some('}') {
            loop {
                if self.at(i) != Some('"') {
                    return Err(Halt::Syntax(i));
                }
                let (key, next) = self.string(i + 1)?;
                i = self.skip_ws(next);
                if self.at(i) != Some(':') {
                    return Err(Halt::Syntax(i));
                }
                i = self.skip_ws(i + 1);
                let (value, next) = self.value(i, depth + 1)?;
                if let Some(slot) = entries.iter_mut().find(|(k, _)| *k == key) {
                    slot.1 = value;
                } else {
                    entries.push((key, value));
                }
                i = self.skip_ws(next);
                if self.at(i) == Some('}') {
                    break;
                }
                if self.at(i) != Some(',') {
                    return Err(Halt::Syntax(i));
                }
                i = self.skip_ws(i + 1);
            }
        }
        Ok((OrderedJson::Object(entries), i + 1))
    }

    /// `_parse_array_unicode`, `i` just past `[`.
    fn array(&self, i: usize, depth: usize) -> Result<(OrderedJson, usize), Halt> {
        let mut items = Vec::new();
        let mut i = self.skip_ws(i);
        if self.at(i) != Some(']') {
            loop {
                let (value, next) = self.value(i, depth + 1)?;
                items.push(value);
                i = self.skip_ws(next);
                if self.at(i) == Some(']') {
                    break;
                }
                if self.at(i) != Some(',') {
                    return Err(Halt::Syntax(i));
                }
                i = self.skip_ws(i + 1);
            }
        }
        Ok((OrderedJson::Array(items), i + 1))
    }
}

/// `json.loads(text)` over decoded characters: the value, or the halt.
fn loads(s: &[char]) -> Result<OrderedJson, Halt> {
    let scanner = Scanner { s };
    let start = scanner.skip_ws(0);
    let (value, end) = scanner.value(start, 0)?;
    let end = scanner.skip_ws(end);
    if end != s.len() {
        return Err(Halt::Syntax(end));
    }
    Ok(value)
}

/// FastAPI's body read for one model parameter.
///
/// # Errors
/// The `json_invalid` 400, Starlette's parse 400, or the 500 for a lone surrogate.
pub fn receive(content_type: Option<&str>, bytes: &[u8]) -> Result<Received, Problem> {
    if bytes.is_empty() {
        return Ok(Received::Missing);
    }
    if !content_type.is_some_and(is_json_content_type) {
        return Ok(Received::Raw);
    }
    let text = decode_text(bytes).ok_or_else(unparseable)?;
    match loads(&text) {
        Ok(v) => Ok(Received::Json(v)),
        Err(Halt::Syntax(pos)) => Err(invalid_screen_definition(vec![entry(
            &pos.to_string(),
            "JSON decode error",
            "json_invalid",
        )])),
        Err(Halt::Unparseable) => Err(unparseable()),
        Err(Halt::LoneSurrogate) => {
            crate::logging::error(
                "baskfy_api.app",
                "unhandled error",
                &[(
                    "error",
                    "a lone surrogate in a JSON body (not representable)",
                )],
            );
            Err(internal_error())
        }
    }
}

/// One declared field of a body model.
#[derive(Debug, Clone, Copy)]
pub enum FieldKind {
    /// `str` with optional `min_length`/`max_length` (`Field(...)`), required.
    Str {
        /// `min_length`.
        min: Option<usize>,
        /// `max_length`.
        max: Option<usize>,
    },
    /// `str | None = None` with the same constraints.
    OptStr {
        /// `min_length`.
        min: Option<usize>,
        /// `max_length`.
        max: Option<usize>,
    },
    /// `EmailStr` (`schemas.Email`), required.
    Email,
}

/// A validated field value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FieldValue {
    /// A string (for `Email`, pydantic's normalised address).
    Str(String),
    /// An optional field left out or sent as `null`.
    Absent,
}

impl FieldValue {
    /// The string, if any.
    #[must_use]
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Self::Str(s) => Some(s),
            Self::Absent => None,
        }
    }
}

fn plural(n: usize) -> &'static str {
    if n == 1 { "" } else { "s" }
}

/// pydantic's string constraints, counted in characters as Python's `len` counts.
fn check_len(field: &str, s: &str, min: Option<usize>, max: Option<usize>) -> Option<ErrorEntry> {
    let n = s.chars().count();
    if let Some(min) = min
        && n < min
    {
        return Some(entry(
            field,
            &format!("String should have at least {min} character{}", plural(min)),
            "string_too_short",
        ));
    }
    if let Some(max) = max
        && n > max
    {
        return Some(entry(
            field,
            &format!("String should have at most {max} character{}", plural(max)),
            "string_too_long",
        ));
    }
    None
}

/// Validates `received` against a closed (`extra="forbid"`) model with these fields, in
/// declaration order: the values in that order, or FastAPI's 400 with every error.
///
/// # Errors
/// `invalid_screen_definition(errors)` as `app._handle_validation` renders a
/// `RequestValidationError`.
pub fn validate_model(
    received: &Received,
    fields: &[(&str, FieldKind)],
) -> Result<Vec<FieldValue>, Problem> {
    let root = || {
        invalid_screen_definition(vec![entry(
            "<root>",
            "Input should be a valid dictionary or object to extract fields from",
            "model_attributes_type",
        )])
    };
    let entries = match received {
        Received::Missing | Received::Json(OrderedJson::Null) => {
            return Err(invalid_screen_definition(vec![entry(
                "<root>",
                "Field required",
                "missing",
            )]));
        }
        Received::Raw => return Err(root()),
        Received::Json(OrderedJson::Object(entries)) => entries,
        Received::Json(_) => return Err(root()),
    };
    let mut errors: Vec<ErrorEntry> = Vec::new();
    let mut values = Vec::with_capacity(fields.len());
    for (name, kind) in fields {
        let given = entries.iter().find(|(k, _)| k == name).map(|(_, v)| v);
        let value = match (kind, given) {
            (FieldKind::OptStr { .. }, None | Some(OrderedJson::Null)) => Some(FieldValue::Absent),
            (FieldKind::Str { .. } | FieldKind::Email, None) => {
                errors.push(entry(name, "Field required", "missing"));
                None
            }
            (_, Some(OrderedJson::Str(s))) => {
                let problem = match kind {
                    FieldKind::Str { min, max } | FieldKind::OptStr { min, max } => {
                        check_len(name, s, *min, *max)
                            .map(Err)
                            .unwrap_or(Ok(s.clone()))
                    }
                    FieldKind::Email => validate_email_str(s)
                        .map_err(|m| entry(name, &m, "value_error"))
                        .and_then(|e| {
                            check_len(name, &e, None, Some(254))
                                .map(Err)
                                .unwrap_or(Ok(e))
                        }),
                };
                match problem {
                    Ok(v) => Some(FieldValue::Str(v)),
                    Err(e) => {
                        errors.push(e);
                        None
                    }
                }
            }
            (_, Some(_)) => {
                errors.push(entry(name, "Input should be a valid string", "string_type"));
                None
            }
        };
        if let Some(v) = value {
            values.push(v);
        }
    }
    for (key, _) in entries {
        if !fields.iter().any(|(n, _)| n == key) {
            errors.push(entry(
                key,
                "Extra inputs are not permitted",
                "extra_forbidden",
            ));
        }
    }
    if errors.is_empty() {
        Ok(values)
    } else {
        Err(invalid_screen_definition(errors))
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn pos(s: &str) -> Result<OrderedJson, Halt> {
        loads(&s.chars().collect::<Vec<char>>())
    }

    #[test]
    fn syntax_errors_stop_where_cpythons_scanner_stops() {
        // `json.loads(s)` in CPython 3.12: `JSONDecodeError.pos` for each.
        for (s, at) in [
            ("[1,]", 3),
            ("{\"a\":1,}", 7),
            ("\"\\x\"", 1),
            ("\"\\u12\"", 2),
            ("\"\\u12zz\"", 2),
            ("\"abc", 0),
            ("\"a\u{1}b\"", 2),
            ("{\"a\" 1}", 5),
            ("{1:2}", 1),
            ("[1 2]", 3),
            ("01", 1),
            ("-", 0),
            ("1.", 1),
            ("1e", 1),
            ("tru", 0),
            ("nul", 0),
            ("{\"a\":1}x", 7),
            ("  ", 2),
            ("{\"a\":[1,{\"b\":}]}", 13),
            ("[", 1),
            ("{", 1),
            ("\"\\", 0),
            ("\"\\u", 2),
            ("\"\\u1234", 2),
            ("{\"id_token\": ", 13),
            ("{\"name\": \"x\",}", 13),
        ] {
            assert_eq!(pos(s), Err(Halt::Syntax(at)), "{s:?}");
        }
    }

    #[test]
    fn values_are_pythons() {
        assert_eq!(
            pos(" {\"a\": [1, 2.5, -0, 1e400, NaN, \"\\ud83d\\ude00\"], \"a\": null} ").unwrap(),
            OrderedJson::Object(vec![("a".to_owned(), OrderedJson::Null)])
        );
        assert_eq!(pos("\"\\ud800\""), Err(Halt::LoneSurrogate));
        assert!(matches!(
            pos("123456789012345678901234567890").unwrap(),
            OrderedJson::BigInt(_)
        ));
        let deep = "[".repeat(2000);
        assert_eq!(pos(&deep), Err(Halt::Unparseable));
    }

    #[test]
    fn the_content_type_decides_whether_the_body_is_json() {
        assert!(is_json_content_type("application/json"));
        assert!(is_json_content_type("Application/JSON; charset=utf-8"));
        assert!(is_json_content_type("application/merge-patch+json"));
        assert!(!is_json_content_type("text/plain"));
        assert!(!is_json_content_type("application/x-www-form-urlencoded"));
        assert!(!is_json_content_type("json"));
        assert_eq!(receive(Some("text/plain"), b"{}").unwrap(), Received::Raw);
        assert_eq!(receive(None, b"").unwrap(), Received::Missing);
        assert_eq!(receive(None, b"{}").unwrap(), Received::Raw);
        let json = Some("application/json");
        assert!(matches!(
            receive(json, b"\xef\xbb\xbf{}").unwrap(),
            Received::Json(_)
        ));
        assert!(receive(json, b"\xff\xfe\xfd").is_err());
    }
}
