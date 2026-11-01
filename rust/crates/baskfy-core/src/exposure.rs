//! Port of `baskfy_core.exposure`: the R1-R4 regime engine and the sleeve allocation solver.

macro_rules! str_enum {
    ($(#[$m:meta])* $name:ident { $($var:ident => $val:literal),+ $(,)? }) => {
        $(#[$m])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
        pub enum $name {
            $(#[doc = $val] $var),+
        }

        impl $name {
            /// Every value, in declaration order.
            pub const ALL: &'static [$name] = &[$($name::$var),+];

            /// The Python enum's `.value`.
            #[must_use]
            pub fn as_str(self) -> &'static str {
                match self {
                    $($name::$var => $val),+
                }
            }

            /// `Enum(value)`.
            #[must_use]
            pub fn parse(s: &str) -> Option<Self> {
                match s {
                    $($val => Some($name::$var),)+
                    _ => None,
                }
            }
        }

        impl ::serde::Serialize for $name {
            fn serialize<S: ::serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
                s.serialize_str(self.as_str())
            }
        }

        impl<'de> ::serde::Deserialize<'de> for $name {
            fn deserialize<D: ::serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
                let s = <String as ::serde::Deserialize>::deserialize(d)?;
                Self::parse(&s).ok_or_else(|| {
                    ::serde::de::Error::custom(format!("{s:?} is not a valid {}", stringify!($name)))
                })
            }
        }

        impl ::std::fmt::Display for $name {
            fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> ::std::fmt::Result {
                f.write_str(self.as_str())
            }
        }
    };
}

pub mod allocation;
pub mod regime;
mod sha256;

use std::fmt::Write as _;

use serde::de::{MapAccess, Visitor};
use serde::ser::SerializeMap;
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use thiserror::Error;

use crate::error::CoreError;

/// A Python exception raised by the exposure engine, by class.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum ExposureError {
    /// `ConfigError` (a `ValueError`).
    #[error("{0}")]
    Config(String),
    /// `AllocationError` (a `ValueError`).
    #[error("{0}")]
    Allocation(String),
    /// `KeyError`; the message is the key's repr.
    #[error("{0}")]
    Key(String),
    /// `ZeroDivisionError`.
    #[error("{0}")]
    ZeroDivision(String),
}

impl ExposureError {
    /// The Python exception class name.
    #[must_use]
    pub fn python_type(&self) -> &'static str {
        match self {
            Self::Config(_) => "ConfigError",
            Self::Allocation(_) => "AllocationError",
            Self::Key(_) => "KeyError",
            Self::ZeroDivision(_) => "ZeroDivisionError",
        }
    }
}

impl From<ExposureError> for CoreError {
    fn from(e: ExposureError) -> Self {
        CoreError::InvalidInput(format!("{}: {e}", e.python_type()))
    }
}

/// `Result` with [`ExposureError`].
pub type ExposureResult<T> = Result<T, ExposureError>;

/// A string-keyed map that keeps insertion order, like a Python `dict`.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct StrMap<V>(pub Vec<(String, V)>);

impl<V> StrMap<V> {
    /// An empty map.
    #[must_use]
    pub fn new() -> Self {
        Self(Vec::new())
    }

    /// `d.get(k)`.
    #[must_use]
    pub fn get(&self, key: &str) -> Option<&V> {
        self.0.iter().find(|(k, _)| k == key).map(|(_, v)| v)
    }

    /// `d.get(k)` mutably.
    pub fn get_mut(&mut self, key: &str) -> Option<&mut V> {
        self.0.iter_mut().find(|(k, _)| k == key).map(|(_, v)| v)
    }

    /// `k in d`.
    #[must_use]
    pub fn contains_key(&self, key: &str) -> bool {
        self.0.iter().any(|(k, _)| k == key)
    }

    /// `d[k] = v`: replaces in place, or appends a new key.
    pub fn insert(&mut self, key: impl Into<String>, value: V) {
        let key = key.into();
        match self.get_mut(&key) {
            Some(slot) => *slot = value,
            None => self.0.push((key, value)),
        }
    }

    /// `d.pop(k)`.
    pub fn remove(&mut self, key: &str) -> Option<V> {
        let i = self.0.iter().position(|(k, _)| k == key)?;
        Some(self.0.remove(i).1)
    }

    /// `d.items()` in insertion order.
    pub fn iter(&self) -> impl Iterator<Item = (&str, &V)> {
        self.0.iter().map(|(k, v)| (k.as_str(), v))
    }

    /// `d.keys()` in insertion order.
    pub fn keys(&self) -> impl Iterator<Item = &str> {
        self.0.iter().map(|(k, _)| k.as_str())
    }

    /// `d.values()` in insertion order.
    pub fn values(&self) -> impl Iterator<Item = &V> {
        self.0.iter().map(|(_, v)| v)
    }

    /// `len(d)`.
    #[must_use]
    pub fn len(&self) -> usize {
        self.0.len()
    }

    /// `not d`.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    /// The entries sorted by key, as `dict(sorted(d.items()))`.
    #[must_use]
    pub fn sorted(&self) -> Self
    where
        V: Clone,
    {
        let mut v = self.0.clone();
        v.sort_by(|a, b| a.0.cmp(&b.0));
        Self(v)
    }
}

impl<K: Into<String>, V> FromIterator<(K, V)> for StrMap<V> {
    fn from_iter<I: IntoIterator<Item = (K, V)>>(iter: I) -> Self {
        let mut m = Self::new();
        for (k, v) in iter {
            m.insert(k, v);
        }
        m
    }
}

impl<V: Serialize> Serialize for StrMap<V> {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        let mut m = s.serialize_map(Some(self.0.len()))?;
        for (k, v) in &self.0 {
            m.serialize_entry(k, v)?;
        }
        m.end()
    }
}

impl<'de, V: Deserialize<'de>> Deserialize<'de> for StrMap<V> {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct V2<V>(std::marker::PhantomData<V>);
        impl<'de, V: Deserialize<'de>> Visitor<'de> for V2<V> {
            type Value = StrMap<V>;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("a map with string keys")
            }
            fn visit_map<A: MapAccess<'de>>(self, mut a: A) -> Result<Self::Value, A::Error> {
                let mut m = StrMap::new();
                while let Some((k, v)) = a.next_entry::<String, V>()? {
                    m.insert(k, v);
                }
                Ok(m)
            }
        }
        d.deserialize_map(V2(std::marker::PhantomData))
    }
}

/// CPython 3.12's `sum()` over floats: Neumaier-compensated.
pub(crate) fn py_sum(values: impl IntoIterator<Item = f64>) -> f64 {
    let mut total = 0.0_f64;
    let mut c = 0.0_f64;
    for x in values {
        let t = total + x;
        if total.abs() >= x.abs() {
            c += (total - t) + x;
        } else {
            c += (x - t) + total;
        }
        total = t;
    }
    if c != 0.0 && c.is_finite() {
        total += c;
    }
    total
}

/// Python's `max(a, b)`: `a` unless `b > a`.
pub(crate) fn py_max(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

/// Python's `min(a, b)`: `a` unless `b < a`.
pub(crate) fn py_min(a: f64, b: f64) -> f64 {
    if b < a { b } else { a }
}

/// Python's `repr(float)`.
pub(crate) fn fr(x: f64) -> String {
    crate::py::float_repr(x)
}

/// Python's `round(x, ndigits)` on a float: the double nearest the correctly rounded decimal.
#[must_use]
pub(crate) fn py_round(x: f64, ndigits: usize) -> f64 {
    if !x.is_finite() {
        return x;
    }
    format!("{x:.ndigits$}").parse::<f64>().unwrap_or(x)
}

/// Python's `repr(str)`.
#[must_use]
pub(crate) fn py_str_repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::new();
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                let _ = write!(out, "\\x{:02x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// `json.dumps(s)` with `ensure_ascii=True`.
pub(crate) fn json_str(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
                let mut buf = [0_u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    let _ = write!(out, "\\u{unit:04x}");
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// `json.dumps(x)` for a float.
pub(crate) fn json_float(x: f64) -> String {
    if x.is_nan() {
        "NaN".to_owned()
    } else if x.is_infinite() {
        if x > 0.0 { "Infinity" } else { "-Infinity" }.to_owned()
    } else {
        crate::py::float_repr(x)
    }
}

/// Lower-case hex SHA-256 of `data`.
#[must_use]
pub(crate) fn sha256_hex(data: &[u8]) -> String {
    let mut out = String::with_capacity(64);
    for b in sha256::digest(data) {
        let _ = write!(out, "{b:02x}");
    }
    out
}
