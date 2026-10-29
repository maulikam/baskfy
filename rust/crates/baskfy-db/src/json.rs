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
//! Known limit (DECISIONS-RS R5.3): an integer outside `u64`/`i64` is read as a float here, where
//! Python keeps an exact `int`. No jsonb column in the schema holds one today.

use std::fmt;

use serde::de::{self, Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};
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
    /// A non-negative integer literal above `i64::MAX`.
    UInt(u64),
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
        serde_json::from_str(text)
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

struct OrderedVisitor;

impl<'de> Visitor<'de> for OrderedVisitor {
    type Value = OrderedJson;

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("any JSON value")
    }

    fn visit_unit<E: de::Error>(self) -> Result<OrderedJson, E> {
        Ok(OrderedJson::Null)
    }

    fn visit_none<E: de::Error>(self) -> Result<OrderedJson, E> {
        Ok(OrderedJson::Null)
    }

    fn visit_some<D: Deserializer<'de>>(self, d: D) -> Result<OrderedJson, D::Error> {
        OrderedJson::deserialize(d)
    }

    fn visit_bool<E: de::Error>(self, v: bool) -> Result<OrderedJson, E> {
        Ok(OrderedJson::Bool(v))
    }

    fn visit_i64<E: de::Error>(self, v: i64) -> Result<OrderedJson, E> {
        Ok(OrderedJson::Int(v))
    }

    fn visit_u64<E: de::Error>(self, v: u64) -> Result<OrderedJson, E> {
        Ok(i64::try_from(v).map_or(OrderedJson::UInt(v), OrderedJson::Int))
    }

    fn visit_f64<E: de::Error>(self, v: f64) -> Result<OrderedJson, E> {
        Ok(OrderedJson::Float(v))
    }

    fn visit_str<E: de::Error>(self, v: &str) -> Result<OrderedJson, E> {
        Ok(OrderedJson::Str(v.to_owned()))
    }

    fn visit_string<E: de::Error>(self, v: String) -> Result<OrderedJson, E> {
        Ok(OrderedJson::Str(v))
    }

    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<OrderedJson, A::Error> {
        let mut items = Vec::new();
        while let Some(item) = seq.next_element::<OrderedJson>()? {
            items.push(item);
        }
        Ok(OrderedJson::Array(items))
    }

    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<OrderedJson, A::Error> {
        let mut entries: Vec<(String, OrderedJson)> = Vec::new();
        while let Some((key, value)) = map.next_entry::<String, OrderedJson>()? {
            if let Some(slot) = entries.iter_mut().find(|(k, _)| *k == key) {
                slot.1 = value;
            } else {
                entries.push((key, value));
            }
        }
        Ok(OrderedJson::Object(entries))
    }
}

impl<'de> Deserialize<'de> for OrderedJson {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        d.deserialize_any(OrderedVisitor)
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
