//! A JSON value in Python's shape: object keys in arrival order, ints and floats apart.
//!
//! Why not `serde_json::Value`: the workspace builds `serde_json` without `preserve_order`, so a
//! `Value` object is a `BTreeMap` and re-serialises its keys **sorted**. Python reads a `jsonb`
//! column with `json.loads` into a `dict`, which keeps the order Postgres stored (jsonb's own:
//! shorter keys first, then bytewise), and FastAPI writes that order back out. A response that
//! echoes a `jsonb` column must therefore keep the order it was read in — this type does.
//!
//! Numbers follow Python's `json` module: an integer literal stays an integer (`Int`/`UInt`), any
//! other number is a `float` (`Float`) and is written back with `repr(float)` — so jsonb's
//! `1.50` comes back out as `1.5`, exactly as `json.dumps(json.loads("1.50"))` does.
//!
//! An integer literal of any size stays an exact integer (`BigInt` beyond `i64`/`u64`), as Python's
//! unbounded `int` does: a stored `1000000000000000000000` echoes as that integer, never `1e+21`
//! (DECISIONS-RS R5.36.1, closed at wave 4c's foundation). The text is parsed here by hand,
//! because serde_json (built without `arbitrary_precision`) turns such a literal into an `f64`
//! before any visitor sees it; serde's `Deserialize` goes through the same parser by way of
//! `RawValue`, so a `jsonb` column and a request body read identically.
//!
//! Still refused, as before: the `NaN`/`Infinity` tokens and a lone surrogate escape (Python's
//! `json` accepts both; neither can come out of Postgres's `jsonb`, R5.36.2).

use serde::de::{self, Deserialize, Deserializer};
use serde::ser::{Serialize, SerializeMap, SerializeSeq, Serializer};

/// One JSON value, Python-shaped. See the module docs.
#[derive(Debug, Clone, PartialEq)]
pub enum OrderedJson {
    /// `null` / `None`.
    Null,
    /// `true` / `false`.
    Bool(bool),
    /// An integer literal that fits `i64`.
    Int(i64),
    /// A non-negative integer literal above `i64::MAX` that fits `u64`.
    UInt(u64),
    /// An integer literal beyond `i64`/`u64`, kept exactly: its canonical decimal digits (with a
    /// leading `-` when negative), as Python's `str(int)` prints it.
    BigInt(String),
    /// Any other number: a Python `float`.
    Float(f64),
    /// A string.
    Str(String),
    /// An array.
    Array(Vec<OrderedJson>),
    /// An object, keys in arrival order (a repeated key keeps its first position and its last
    /// value, as a Python `dict` built by `json.loads` does).
    Object(Vec<(String, OrderedJson)>),
}

impl OrderedJson {
    /// The member `key` of an object.
    #[must_use]
    pub fn get(&self, key: &str) -> Option<&OrderedJson> {
        match self {
            Self::Object(entries) => entries.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    /// Parses JSON text in Python's shape.
    ///
    /// # Errors
    /// `serde_json`'s parse error.
    pub fn parse(text: &str) -> Result<Self, serde_json::Error> {
        let mut parser = Parser {
            bytes: text.as_bytes(),
            pos: 0,
        };
        parser.ws();
        let value = parser.value(0)?;
        parser.ws();
        if parser.pos != parser.bytes.len() {
            return Err(parser.err("Extra data"));
        }
        Ok(value)
    }

    /// `json.dumps(value, separators=(",", ":"), ensure_ascii=False)` — FastAPI's rendering.
    ///
    /// # Errors
    /// A float that is not finite (Python's `allow_nan=False` refuses it too).
    pub fn to_python_json(&self) -> Result<String, serde_json::Error> {
        serde_json::to_string(self)
    }
}

impl Serialize for OrderedJson {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        match self {
            Self::Null => serializer.serialize_unit(),
            Self::Bool(b) => serializer.serialize_bool(*b),
            Self::Int(i) => serializer.serialize_i64(*i),
            Self::UInt(u) => serializer.serialize_u64(*u),
            Self::BigInt(digits) => {
                let raw = serde_json::value::RawValue::from_string(digits.clone())
                    .map_err(serde::ser::Error::custom)?;
                raw.serialize(serializer)
            }
            Self::Float(f) => {
                if !f.is_finite() {
                    return Err(serde::ser::Error::custom(
                        "Out of range float values are not JSON compliant",
                    ));
                }
                let text = baskfy_core::py::float_repr(*f);
                let raw = serde_json::value::RawValue::from_string(text)
                    .map_err(serde::ser::Error::custom)?;
                raw.serialize(serializer)
            }
            Self::Str(s) => serializer.serialize_str(s),
            Self::Array(items) => {
                let mut seq = serializer.serialize_seq(Some(items.len()))?;
                for item in items {
                    seq.serialize_element(item)?;
                }
                seq.end()
            }
            Self::Object(entries) => {
                let mut map = serializer.serialize_map(Some(entries.len()))?;
                for (k, v) in entries {
                    map.serialize_entry(k, v)?;
                }
                map.end()
            }
        }
    }
}

impl<'de> Deserialize<'de> for OrderedJson {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let raw = Box::<serde_json::value::RawValue>::deserialize(d)?;
        Self::parse(raw.get()).map_err(de::Error::custom)
    }
}

/// Python's `json.loads` over UTF-8 text: the grammar of RFC 8259, objects in arrival order, an
/// integer literal exact at any size.
struct Parser<'a> {
    bytes: &'a [u8],
    pos: usize,
}

/// `json.loads` recursion is bounded by the interpreter's stack (about a thousand levels);
/// this bound is far above any document the API reads.
const MAX_DEPTH: usize = 512;

impl Parser<'_> {
    fn err(&self, what: &str) -> serde_json::Error {
        de::Error::custom(format!("{what}: char {}", self.pos))
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.pos).copied()
    }

    fn ws(&mut self) {
        while let Some(b' ' | b'\t' | b'\n' | b'\r') = self.peek() {
            self.pos += 1;
        }
    }

    fn literal(
        &mut self,
        word: &[u8],
        value: OrderedJson,
    ) -> Result<OrderedJson, serde_json::Error> {
        if self.bytes.get(self.pos..self.pos + word.len()) == Some(word) {
            self.pos += word.len();
            Ok(value)
        } else {
            Err(self.err("Expecting value"))
        }
    }

    fn value(&mut self, depth: usize) -> Result<OrderedJson, serde_json::Error> {
        if depth > MAX_DEPTH {
            return Err(self.err("maximum nesting depth exceeded"));
        }
        match self.peek() {
            Some(b'{') => self.object(depth),
            Some(b'[') => self.array(depth),
            Some(b'"') => Ok(OrderedJson::Str(self.string()?)),
            Some(b't') => self.literal(b"true", OrderedJson::Bool(true)),
            Some(b'f') => self.literal(b"false", OrderedJson::Bool(false)),
            Some(b'n') => self.literal(b"null", OrderedJson::Null),
            Some(b'-' | b'0'..=b'9') => self.number(),
            _ => Err(self.err("Expecting value")),
        }
    }

    fn object(&mut self, depth: usize) -> Result<OrderedJson, serde_json::Error> {
        self.pos += 1;
        let mut entries: Vec<(String, OrderedJson)> = Vec::new();
        self.ws();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(OrderedJson::Object(entries));
        }
        loop {
            self.ws();
            if self.peek() != Some(b'"') {
                return Err(self.err("Expecting property name enclosed in double quotes"));
            }
            let key = self.string()?;
            self.ws();
            if self.peek() != Some(b':') {
                return Err(self.err("Expecting ':' delimiter"));
            }
            self.pos += 1;
            self.ws();
            let value = self.value(depth + 1)?;
            if let Some(slot) = entries.iter_mut().find(|(k, _)| *k == key) {
                slot.1 = value;
            } else {
                entries.push((key, value));
            }
            self.ws();
            match self.peek() {
                Some(b',') => self.pos += 1,
                Some(b'}') => {
                    self.pos += 1;
                    return Ok(OrderedJson::Object(entries));
                }
                _ => return Err(self.err("Expecting ',' delimiter")),
            }
        }
    }

    fn array(&mut self, depth: usize) -> Result<OrderedJson, serde_json::Error> {
        self.pos += 1;
        let mut items = Vec::new();
        self.ws();
        if self.peek() == Some(b']') {
            self.pos += 1;
            return Ok(OrderedJson::Array(items));
        }
        loop {
            self.ws();
            items.push(self.value(depth + 1)?);
            self.ws();
            match self.peek() {
                Some(b',') => self.pos += 1,
                Some(b']') => {
                    self.pos += 1;
                    return Ok(OrderedJson::Array(items));
                }
                _ => return Err(self.err("Expecting ',' delimiter")),
            }
        }
    }

    fn hex4(&mut self) -> Result<u32, serde_json::Error> {
        let digits = self
            .bytes
            .get(self.pos..self.pos + 4)
            .and_then(|d| std::str::from_utf8(d).ok())
            .filter(|d| d.bytes().all(|b| b.is_ascii_hexdigit()))
            .ok_or_else(|| self.err("Invalid \\uXXXX escape"))?;
        let value =
            u32::from_str_radix(digits, 16).map_err(|_| self.err("Invalid \\uXXXX escape"))?;
        self.pos += 4;
        Ok(value)
    }

    fn string(&mut self) -> Result<String, serde_json::Error> {
        self.pos += 1;
        let mut out = String::new();
        loop {
            let start = self.pos;
            while let Some(b) = self.peek() {
                if b == b'"' || b == b'\\' || b < 0x20 {
                    break;
                }
                self.pos += 1;
            }
            let chunk = std::str::from_utf8(&self.bytes[start..self.pos])
                .map_err(|_| self.err("Invalid UTF-8"))?;
            out.push_str(chunk);
            match self.peek() {
                Some(b'"') => {
                    self.pos += 1;
                    return Ok(out);
                }
                Some(b'\\') => {
                    self.pos += 1;
                    let escape = self.peek().ok_or_else(|| self.err("Unterminated string"))?;
                    self.pos += 1;
                    match escape {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{8}'),
                        b'f' => out.push('\u{c}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let first = self.hex4()?;
                            let code = if (0xD800..0xDC00).contains(&first)
                                && self.bytes.get(self.pos..self.pos + 2) == Some(b"\\u")
                            {
                                self.pos += 2;
                                let second = self.hex4()?;
                                if (0xDC00..0xE000).contains(&second) {
                                    0x10000 + ((first - 0xD800) << 10) + (second - 0xDC00)
                                } else {
                                    return Err(self.err("lone surrogate"));
                                }
                            } else {
                                first
                            };
                            out.push(
                                char::from_u32(code).ok_or_else(|| self.err("lone surrogate"))?,
                            );
                        }
                        _ => return Err(self.err("Invalid \\escape")),
                    }
                }
                Some(_) => return Err(self.err("Invalid control character")),
                None => return Err(self.err("Unterminated string")),
            }
        }
    }

    fn digits(&mut self) -> usize {
        let start = self.pos;
        while let Some(b'0'..=b'9') = self.peek() {
            self.pos += 1;
        }
        self.pos - start
    }

    fn number(&mut self) -> Result<OrderedJson, serde_json::Error> {
        let start = self.pos;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        match self.peek() {
            Some(b'0') => self.pos += 1,
            Some(b'1'..=b'9') => {
                self.digits();
            }
            _ => return Err(self.err("Expecting value")),
        }
        let mut integral = true;
        if self.peek() == Some(b'.') && matches!(self.bytes.get(self.pos + 1), Some(b'0'..=b'9')) {
            self.pos += 1;
            self.digits();
            integral = false;
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            let mark = self.pos;
            self.pos += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.pos += 1;
            }
            if self.digits() == 0 {
                // Python's scanner stops before an `e` with no exponent digits.
                self.pos = mark;
            } else {
                integral = false;
            }
        }
        let text = std::str::from_utf8(&self.bytes[start..self.pos])
            .map_err(|_| self.err("Invalid number"))?;
        if integral {
            if let Ok(i) = text.parse::<i64>() {
                return Ok(OrderedJson::Int(i));
            }
            if let Ok(u) = text.parse::<u64>() {
                return Ok(OrderedJson::UInt(u));
            }
            return Ok(OrderedJson::BigInt(
                if text == "-0" { "0" } else { text }.to_owned(),
            ));
        }
        text.parse::<f64>()
            .map(OrderedJson::Float)
            .map_err(|_| self.err("Invalid number"))
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn key_order_survives_a_round_trip() {
        let v = OrderedJson::parse(r#"{"zeta": 1, "alpha": {"b": 2, "a": 3}}"#).unwrap();
        assert_eq!(
            v.to_python_json().unwrap(),
            r#"{"zeta":1,"alpha":{"b":2,"a":3}}"#
        );
    }

    #[test]
    fn numbers_are_pythons() {
        // json.dumps(json.loads('[1.50, 2, 1e5, 1e-7, -0.0, 18446744073709551615]'))
        let v = OrderedJson::parse("[1.50, 2, 1e5, 1e-7, -0.0, 18446744073709551615]").unwrap();
        assert_eq!(
            v.to_python_json().unwrap(),
            "[1.5,2,100000.0,1e-07,-0.0,18446744073709551615]"
        );
    }

    #[test]
    fn integers_of_any_size_stay_exact() {
        // json.dumps(json.loads('[1000000000000000000000, -99999999999999999999, 1e21, -0]'))
        let v = OrderedJson::parse("[1000000000000000000000, -99999999999999999999, 1e21, -0]")
            .unwrap();
        assert_eq!(
            v.to_python_json().unwrap(),
            "[1000000000000000000000,-99999999999999999999,1e+21,0]"
        );
        assert_eq!(
            v,
            OrderedJson::Array(vec![
                OrderedJson::BigInt("1000000000000000000000".to_owned()),
                OrderedJson::BigInt("-99999999999999999999".to_owned()),
                OrderedJson::Float(1e21),
                OrderedJson::Int(0),
            ])
        );
    }

    #[test]
    fn serde_reads_through_the_same_parser() {
        // What sqlx's `Json<OrderedJson>` does with a jsonb column.
        let v: OrderedJson = serde_json::from_str(
            r#"{"b": 18446744073709551616, "a": [1.50, "\u00e9\ud83d\ude00"]}"#,
        )
        .unwrap();
        assert_eq!(
            v.to_python_json().unwrap(),
            "{\"b\":18446744073709551616,\"a\":[1.5,\"é😀\"]}"
        );
    }

    #[test]
    fn malformed_text_is_refused() {
        for bad in [
            "",
            "{",
            "[1,]",
            "{\"a\" 1}",
            "01",
            "1.",
            "-",
            "tru",
            "\"a",
            "[1] x",
            "NaN",
            "Infinity",
            "\"\\ud800\"",
            "\"\u{1}\"",
            "{1: 2}",
        ] {
            assert!(OrderedJson::parse(bad).is_err(), "{bad:?}");
        }
        assert_eq!(
            OrderedJson::parse(" [1e5] ").unwrap(),
            OrderedJson::Array(vec![OrderedJson::Float(1e5)])
        );
    }

    #[test]
    fn a_repeated_key_keeps_its_first_place_and_last_value() {
        // json.loads('{"a": 1, "b": 2, "a": 3}') == {"a": 3, "b": 2}
        let v = OrderedJson::parse(r#"{"a": 1, "b": 2, "a": 3}"#).unwrap();
        assert_eq!(v.to_python_json().unwrap(), r#"{"a":3,"b":2}"#);
    }

    #[test]
    fn non_ascii_is_written_raw_as_fastapi_does() {
        let v = OrderedJson::parse(r#"{"label": "₹ é"}"#).unwrap();
        assert_eq!(v.to_python_json().unwrap(), "{\"label\":\"₹ é\"}");
    }
}
