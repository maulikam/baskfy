//! pydantic 2's validation of a request body, as FastAPI runs it (`validate_python(value,
//! from_attributes=True)` over what `json.loads` produced) — for the write routes of the sleeve
//! routers (swing, TWT, VBT, sleeves, options, F&O, overlap, backtests; lane writes-sleeves).
//!
//! [`crate::body::validate_model`] covers the session routes' flat string models. These routers'
//! models carry `int`, `Decimal`, `bool`, `date`, `time`, `Literal`, `StrEnum`, nested models,
//! lists and dicts, with `Field` bounds and model validators, so a request is described here by a
//! [`Model`] of [`Ty`]s and validated into a [`PV`] tree. Every rule below was probed against the
//! tree's pydantic 2 (pydantic-core's lax mode, Python input):
//!
//! * fields in declaration order, then every extra key in arrival order (`extra="forbid"`);
//!   a missing required field is `missing`; a non-dict where a model is expected is
//!   `model_attributes_type` (FastAPI validates with `from_attributes=True`);
//! * a `mode="before"` model validator sees the raw input and may raise a [`Problem`] (an
//!   exception that is not a `ValueError` escapes pydantic and becomes the response); a
//!   `mode="after"` one runs only when every field passed and its `ValueError` is
//!   `Value error, <message>` at the model's own location;
//! * `int`: a bool is 0/1, an integral float is its int (non-finite: `finite_number`; beyond
//!   `i64`: `int_parsing_size`; fractional: `int_from_float`), a string is pydantic's lax parse;
//! * `Decimal`: an int is exact, a float is `Decimal(repr(float))`, a string is `Decimal(s)` after
//!   stripping whitespace; a bool or a container is `decimal_type`; NaN and the infinities are
//!   `finite_number`. The value keeps Python's spelling ([`PyDec`]), because `str(value)` reaches
//!   audit rows and problem details;
//! * `bool`, `str`, `date`, `time`, `Literal[...]` and `StrEnum` as pydantic-core's lax mode reads
//!   a Python object (`date`/`time` parsing follows speedate's messages for the shapes probed).

use std::cmp::Ordering;

use baskfy_db::OrderedJson;
use chrono::{Datelike, NaiveDate, NaiveTime, Timelike};
use rust_decimal::Decimal;

use crate::problems::{ErrorEntry, Problem, invalid_screen_definition};
use crate::routes::common::{PyInt, lax_bool, lax_int};

// ---------------------------------------------------------------------------------------------
// Python's Decimal, as text
// ---------------------------------------------------------------------------------------------

/// A finite Python `Decimal`: sign, coefficient digits and exponent, exactly as `decimal` holds
/// it, so `str(value)` is Python's whatever the scale or magnitude.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct PyDec {
    negative: bool,
    /// The coefficient's digits, no leading zeros (`"0"` for zero).
    coef: String,
    /// The exponent.
    exp: i64,
}

impl PyDec {
    /// `Decimal(text)` for a finite literal (Python's grammar, ASCII digits, `_` between
    /// digits); `None` for anything Python would refuse or read as NaN/Infinity.
    pub(crate) fn parse(text: &str) -> Option<Self> {
        let (negative, body) = match text.as_bytes().first() {
            Some(b'-') => (true, &text[1..]),
            Some(b'+') => (false, &text[1..]),
            _ => (false, text),
        };
        let (mantissa, exponent) = match body.find(['e', 'E']) {
            Some(i) => (&body[..i], Some(&body[i + 1..])),
            None => (body, None),
        };
        let (int_part, frac_part) = match mantissa.split_once('.') {
            Some((i, f)) => (i, f),
            None => (mantissa, ""),
        };
        if int_part.is_empty() && frac_part.is_empty() {
            return None;
        }
        let int_digits = grouped(int_part)?;
        let frac_digits = grouped(frac_part)?;
        let exp_adjust = match exponent {
            None => 0_i64,
            Some(e) => {
                let (eneg, edigits) = match e.as_bytes().first() {
                    Some(b'-') => (true, &e[1..]),
                    Some(b'+') => (false, &e[1..]),
                    _ => (false, e),
                };
                let digits = grouped(edigits)?;
                if digits.is_empty() {
                    return None;
                }
                let v: i64 = digits.parse().ok()?;
                if eneg { -v } else { v }
            }
        };
        let all = format!("{int_digits}{frac_digits}");
        let trimmed = all.trim_start_matches('0');
        let coef = if trimmed.is_empty() { "0" } else { trimmed };
        let exp = exp_adjust.checked_sub(i64::try_from(frac_digits.len()).ok()?)?;
        Some(Self {
            negative,
            coef: coef.to_owned(),
            exp,
        })
    }

    /// `Decimal(int)`.
    pub(crate) fn from_int(i: &PyInt) -> Self {
        let digits = i.canonical.trim_start_matches('-');
        Self {
            negative: i.negative,
            coef: digits.to_owned(),
            exp: 0,
        }
    }

    /// `Decimal(str(x))` — pydantic's `float -> Decimal`; `None` for NaN and the infinities.
    pub(crate) fn from_float(x: f64) -> Option<Self> {
        if !x.is_finite() {
            return None;
        }
        Self::parse(&baskfy_core::py::float_repr(x))
    }

    /// `Decimal(str(d))` of a `rust_decimal` value (its scale kept).
    pub(crate) fn from_decimal(d: Decimal) -> Self {
        Self::parse(&d.to_string()).unwrap_or(Self {
            negative: false,
            coef: "0".to_owned(),
            exp: 0,
        })
    }

    /// Whether the value is zero.
    pub(crate) fn is_zero(&self) -> bool {
        self.coef == "0"
    }

    /// `str(value)` — Python's `Decimal.__str__` (to-scientific-string).
    pub(crate) fn py_str(&self) -> String {
        let n = i64::try_from(self.coef.len()).unwrap_or(1);
        let leftdigits = self.exp + n;
        let dotplace = if self.exp <= 0 && leftdigits > -6 {
            leftdigits
        } else {
            1
        };
        let (intpart, fracpart) = if dotplace <= 0 {
            (
                "0".to_owned(),
                format!(
                    ".{}{}",
                    "0".repeat(usize::try_from(-dotplace).unwrap_or(0)),
                    self.coef
                ),
            )
        } else if dotplace >= n {
            (
                format!(
                    "{}{}",
                    self.coef,
                    "0".repeat(usize::try_from(dotplace - n).unwrap_or(0))
                ),
                String::new(),
            )
        } else {
            let cut = usize::try_from(dotplace).unwrap_or(0);
            (
                self.coef[..cut].to_owned(),
                format!(".{}", &self.coef[cut..]),
            )
        };
        let exp = if leftdigits == dotplace {
            String::new()
        } else {
            let e = leftdigits - dotplace;
            format!("E{}{e}", if e >= 0 { "+" } else { "" })
        };
        format!(
            "{}{intpart}{fracpart}{exp}",
            if self.negative { "-" } else { "" }
        )
    }

    /// The value as a `rust_decimal` (its scale kept), when one can hold it exactly.
    pub(crate) fn to_decimal(&self) -> Option<Decimal> {
        let sign = if self.negative { "-" } else { "" };
        if self.exp >= 0 {
            let zeros = "0".repeat(usize::try_from(self.exp).ok()?);
            Decimal::from_str_exact(&format!("{sign}{}{zeros}", self.coef)).ok()
        } else {
            let scale = usize::try_from(-self.exp).ok()?;
            let coef = if self.coef.len() <= scale {
                format!("{}{}", "0".repeat(scale - self.coef.len() + 1), self.coef)
            } else {
                self.coef.clone()
            };
            let (i, f) = coef.split_at(coef.len() - scale);
            Decimal::from_str_exact(&format!("{sign}{i}.{f}")).ok()
        }
    }

    /// Numeric comparison (`Decimal.__lt__` and friends).
    pub(crate) fn cmp_num(&self, other: &Self) -> Ordering {
        match (self.is_zero(), other.is_zero()) {
            (true, true) => return Ordering::Equal,
            (true, false) => {
                return if other.negative {
                    Ordering::Greater
                } else {
                    Ordering::Less
                };
            }
            (false, true) => {
                return if self.negative {
                    Ordering::Less
                } else {
                    Ordering::Greater
                };
            }
            (false, false) => {}
        }
        if self.negative != other.negative {
            return if self.negative {
                Ordering::Less
            } else {
                Ordering::Greater
            };
        }
        let magnitude = cmp_magnitude(self, other);
        if self.negative {
            magnitude.reverse()
        } else {
            magnitude
        }
    }
}

/// `|a|` against `|b|` for non-zero values.
fn cmp_magnitude(a: &PyDec, b: &PyDec) -> Ordering {
    let strip = |d: &PyDec| -> (String, i64) {
        let t = d.coef.trim_end_matches('0');
        let removed = i64::try_from(d.coef.len() - t.len()).unwrap_or(0);
        (t.to_owned(), d.exp + removed)
    };
    let (ca, ea) = strip(a);
    let (cb, eb) = strip(b);
    let adj_a = ea + i64::try_from(ca.len()).unwrap_or(0);
    let adj_b = eb + i64::try_from(cb.len()).unwrap_or(0);
    if adj_a != adj_b {
        return adj_a.cmp(&adj_b);
    }
    let width = ca.len().max(cb.len());
    let pa = format!("{ca:0<width$}");
    let pb = format!("{cb:0<width$}");
    pa.cmp(&pb)
}

/// ASCII digits with single `_` separators between them; `""` stays `""`.
fn grouped(s: &str) -> Option<String> {
    if s.is_empty() {
        return Some(String::new());
    }
    if s.starts_with('_') || s.ends_with('_') || s.contains("__") {
        return None;
    }
    let out: String = s.chars().filter(|c| *c != '_').collect();
    out.bytes().all(|b| b.is_ascii_digit()).then_some(out)
}

// ---------------------------------------------------------------------------------------------
// Types and values
// ---------------------------------------------------------------------------------------------

/// A `pattern`: its source (as pydantic prints it) and the matcher.
pub(crate) type Pattern = (&'static str, fn(&str) -> bool);

/// A `Field(gt=, ge=, lt=, le=)` bound, held as the text pydantic prints in its message.
#[derive(Debug, Clone, Copy)]
pub(crate) enum Bound {
    /// `gt`.
    Gt(&'static str),
    /// `ge`.
    Ge(&'static str),
    /// `le`.
    Le(&'static str),
}

/// A declared type.
#[derive(Debug, Clone)]
pub(crate) enum Ty {
    /// `int` with bounds.
    Int(Vec<Bound>),
    /// `Decimal` with bounds.
    Dec(Vec<Bound>),
    /// `float` with bounds; the value is held as [`PV::Dec`] of its `repr`.
    Float(Vec<Bound>),
    /// `list[T]` with `min_length` / `max_length`.
    BoundedList(Box<Ty>, Option<usize>, Option<usize>),
    /// `str` with `min_length`, `max_length` and a `pattern` (its source, and the matcher).
    Str {
        /// `min_length`.
        min: Option<usize>,
        /// `max_length`.
        max: Option<usize>,
        /// `pattern`.
        pattern: Option<Pattern>,
    },
    /// `bool`.
    Bool,
    /// `datetime.date`.
    Date,
    /// `datetime.time`.
    Time,
    /// `Literal["A", "B"]` (`literal_error`).
    StrLit(&'static [&'static str]),
    /// `Literal[1, 5, 60]`.
    IntLit(&'static [i64]),
    /// A `StrEnum` (`enum`).
    Enum(&'static [&'static str]),
    /// `T | None`.
    Opt(Box<Ty>),
    /// `list[T]`.
    List(Box<Ty>),
    /// `dict[K, V]`.
    Dict(Box<Ty>, Box<Ty>),
    /// `dict[str, object]` — any JSON object, kept as it arrived.
    AnyDict,
    /// A nested model.
    Model(Box<Model>),
    /// `tuple[T, ...]` (a list is accepted, a string is not).
    Tuple(Box<Ty>),
    /// `tuple[A, B]`.
    Pair(Box<Ty>, Box<Ty>),
}

impl Ty {
    /// `T | None`.
    pub(crate) fn opt(self) -> Self {
        Self::Opt(Box::new(self))
    }

    /// A plain `str` with lengths.
    pub(crate) fn str_len(min: Option<usize>, max: Option<usize>) -> Self {
        Self::Str {
            min,
            max,
            pattern: None,
        }
    }
}

/// What a field holds when the request leaves it out.
#[derive(Debug, Clone)]
pub(crate) enum Default {
    /// No default: `missing`.
    Required,
    /// A default: the field is *unset* (`exclude_unset` drops it) and the handler applies it.
    Unset,
}

/// A model's `mode="after"` validator: `Err(message)` is a `ValueError(message)`.
pub(crate) type After = fn(&[(String, Option<PV>)]) -> Result<(), String>;
/// A model's `mode="before"` validator over the raw input: `Err` escapes as the response.
pub(crate) type Before = fn(&OrderedJson) -> Result<(), Problem>;

/// A request model.
#[derive(Debug, Clone)]
pub(crate) struct Model {
    /// `(name, type, default)` in declaration order.
    pub(crate) fields: Vec<(&'static str, Ty, Default)>,
    /// `extra="forbid"` (otherwise extras are ignored).
    pub(crate) forbid_extra: bool,
    /// `@model_validator(mode="before")`.
    pub(crate) before: Option<Before>,
    /// `@model_validator(mode="after")`.
    pub(crate) after: Option<After>,
}

/// A validated value.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum PV {
    /// `None`.
    Null,
    /// `int`.
    Int(PyInt),
    /// `Decimal`.
    Dec(PyDec),
    /// `str` (also a `Literal`/`StrEnum` member).
    Str(String),
    /// `bool`.
    Bool(bool),
    /// `date`.
    Date(NaiveDate),
    /// `time`, and whether it carries a UTC offset (speedate's `Z`/`+HH:MM`, or a number).
    Time(NaiveTime, bool),
    /// `list`.
    List(Vec<PV>),
    /// `dict`, in arrival order.
    Dict(Vec<(PV, PV)>),
    /// `dict[str, object]` as it arrived.
    Any(OrderedJson),
    /// A model: each field's value, `None` when unset.
    Model(Vec<(String, Option<PV>)>),
}

impl PV {
    /// Python's `str(value)` (what an audit row and a problem detail print).
    pub(crate) fn py_str(&self) -> String {
        match self {
            Self::Null => "None".to_owned(),
            Self::Int(i) => i.canonical.clone(),
            Self::Dec(d) => d.py_str(),
            Self::Str(s) => s.clone(),
            Self::Bool(b) => if *b { "True" } else { "False" }.to_owned(),
            Self::Date(d) => d.format("%Y-%m-%d").to_string(),
            Self::Time(t, tz) => {
                let mut s = format!("{:02}:{:02}:{:02}", t.hour(), t.minute(), t.second());
                let micros = t.nanosecond() / 1000;
                if micros != 0 {
                    s.push_str(&format!(".{micros:06}"));
                }
                if *tz {
                    s.push_str("+00:00");
                }
                s
            }
            Self::List(_) | Self::Dict(_) | Self::Any(_) | Self::Model(_) => String::new(),
        }
    }

    /// The value as a `Decimal` for a comparison (`Decimal(str(value))`).
    pub(crate) fn as_pydec(&self) -> Option<PyDec> {
        match self {
            Self::Dec(d) => Some(d.clone()),
            Self::Int(i) => Some(PyDec::from_int(i)),
            _ => None,
        }
    }

    /// A model's field, when set.
    pub(crate) fn field(&self, name: &str) -> Option<&PV> {
        match self {
            Self::Model(fields) => fields
                .iter()
                .find(|(n, _)| n == name)
                .and_then(|(_, v)| v.as_ref()),
            _ => None,
        }
    }

    /// `model_dump(exclude_unset=True, exclude_none=True)` of a model: the fields the caller
    /// set to something other than `null`, in declaration order.
    pub(crate) fn changes(&self) -> Vec<(String, PV)> {
        match self {
            Self::Model(fields) => fields
                .iter()
                .filter_map(|(n, v)| match v {
                    Some(PV::Null) | None => None,
                    Some(v) => Some((n.clone(), v.clone())),
                })
                .collect(),
            _ => Vec::new(),
        }
    }
}

// ---------------------------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------------------------

/// One location part.
#[derive(Debug, Clone)]
enum Loc {
    Key(String),
    Idx(usize),
}

/// One pydantic error.
#[derive(Debug, Clone)]
struct Err {
    loc: Vec<Loc>,
    msg: String,
    kind: &'static str,
}

/// Why validation stopped.
#[derive(Debug)]
pub(crate) enum Fail {
    /// pydantic's `ValidationError`, as FastAPI's error entries.
    Errors(Vec<ErrorEntry>),
    /// An exception a validator raised (a [`Problem`]), which escapes as the response.
    Raised(Problem),
}

impl Fail {
    /// The response: FastAPI's 400 for errors (with `prefix` entries first — the path and query
    /// errors FastAPI collects before the body's), the problem itself otherwise.
    pub(crate) fn into_problem(self, prefix: Vec<ErrorEntry>) -> Problem {
        match self {
            Self::Errors(errors) => {
                let mut all = prefix;
                all.extend(errors);
                invalid_screen_definition(all)
            }
            Self::Raised(p) => p,
        }
    }
}

/// FastAPI's `{"field", "message", "type"}` entry.
pub(crate) fn entry(field: &str, message: &str, kind: &str) -> ErrorEntry {
    vec![
        ("field".to_owned(), OrderedJson::Str(field.to_owned())),
        ("message".to_owned(), OrderedJson::Str(message.to_owned())),
        ("type".to_owned(), OrderedJson::Str(kind.to_owned())),
    ]
}

fn to_entry(e: &Err) -> ErrorEntry {
    let path: Vec<String> = e
        .loc
        .iter()
        .map(|l| match l {
            Loc::Key(k) => k.clone(),
            Loc::Idx(i) => i.to_string(),
        })
        .collect();
    let field = if path.is_empty() {
        "<root>".to_owned()
    } else {
        path.join(".")
    };
    entry(&field, &e.msg, e.kind)
}

struct Ctx {
    errors: Vec<Err>,
}

impl Ctx {
    fn push(&mut self, loc: &[Loc], msg: impl Into<String>, kind: &'static str) {
        self.errors.push(Err {
            loc: loc.to_vec(),
            msg: msg.into(),
            kind,
        });
    }
}

fn with(loc: &[Loc], part: Loc) -> Vec<Loc> {
    let mut l = loc.to_vec();
    l.push(part);
    l
}

// ---------------------------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------------------------

/// Validates a received body (FastAPI's read already applied: `None` is an empty body or a JSON
/// `null`; `Some(Raw)` is unparsed bytes) against `model`.
///
/// # Errors
/// [`Fail::Errors`] with every error in pydantic's order, or [`Fail::Raised`].
pub(crate) fn validate_body(received: &crate::body::Received, model: &Model) -> Result<PV, Fail> {
    use crate::body::Received;
    let value = match received {
        Received::Missing | Received::Json(OrderedJson::Null) => {
            return Err(Fail::Errors(vec![entry(
                "<root>",
                "Field required",
                "missing",
            )]));
        }
        Received::Raw => {
            return Err(Fail::Errors(vec![entry(
                "<root>",
                "Input should be a valid dictionary or object to extract fields from",
                "model_attributes_type",
            )]));
        }
        Received::Json(v) => v,
    };
    validate_value(value, &Ty::Model(Box::new(model.clone())))
}

/// Validates one JSON value against a type, from the root.
///
/// # Errors
/// As [`validate_body`].
pub(crate) fn validate_value(value: &OrderedJson, ty: &Ty) -> Result<PV, Fail> {
    let mut ctx = Ctx { errors: Vec::new() };
    let out = check(value, ty, &[], &mut ctx).map_err(Fail::Raised)?;
    if ctx.errors.is_empty()
        && let Some(v) = out
    {
        Ok(v)
    } else {
        Err(Fail::Errors(ctx.errors.iter().map(to_entry).collect()))
    }
}

fn check(value: &OrderedJson, ty: &Ty, loc: &[Loc], ctx: &mut Ctx) -> Result<Option<PV>, Problem> {
    Ok(match ty {
        Ty::Opt(inner) => {
            if matches!(value, OrderedJson::Null) {
                Some(PV::Null)
            } else {
                check(value, inner, loc, ctx)?
            }
        }
        Ty::Int(bounds) => int(value, loc, ctx).and_then(|i| {
            let d = PyDec::from_int(&i);
            bounded(&d, bounds, loc, ctx).then_some(PV::Int(i))
        }),
        Ty::Dec(bounds) => decimal(value, loc, ctx)
            .and_then(|d| bounded(&d, bounds, loc, ctx).then_some(PV::Dec(d))),
        Ty::Float(bounds) => float(value, loc, ctx)
            .and_then(|d| bounded(&d, bounds, loc, ctx).then_some(PV::Dec(d))),
        Ty::BoundedList(inner, min, max) => {
            let n = match value {
                OrderedJson::Array(items) => Some(items.len()),
                _ => None,
            };
            let item = |k: usize| if k == 1 { "item" } else { "items" };
            match (n, min, max) {
                (Some(n), _, Some(max)) if n > *max => {
                    ctx.push(
                        loc,
                        format!("List should have at most {max} {} after validation, not {n}", item(*max)),
                        "too_long",
                    );
                    None
                }
                _ => match check(value, &Ty::List(inner.clone()), loc, ctx)? {
                    Some(PV::List(out)) => match min {
                        Some(min) if out.len() < *min => {
                            ctx.push(
                                loc,
                                format!(
                                    "List should have at least {min} {} after validation, not {}",
                                    item(*min),
                                    out.len()
                                ),
                                "too_short",
                            );
                            None
                        }
                        _ => Some(PV::List(out)),
                    },
                    other => other,
                },
            }
        }
        Ty::Str { min, max, pattern } => string(value, *min, *max, *pattern, loc, ctx),
        Ty::Bool => boolean(value, loc, ctx).map(PV::Bool),
        Ty::Date => date(value, loc, ctx).map(PV::Date),
        Ty::Time => time(value, loc, ctx),
        Ty::StrLit(choices) => match value {
            OrderedJson::Str(s) if choices.contains(&s.as_str()) => Some(PV::Str(s.clone())),
            _ => {
                ctx.push(
                    loc,
                    format!("Input should be {}", choice_list(choices, true)),
                    "literal_error",
                );
                None
            }
        },
        Ty::Enum(choices) => match value {
            OrderedJson::Str(s) if choices.contains(&s.as_str()) => Some(PV::Str(s.clone())),
            _ => {
                ctx.push(
                    loc,
                    format!("Input should be {}", choice_list(choices, true)),
                    "enum",
                );
                None
            }
        },
        Ty::IntLit(choices) => {
            let as_int = match value {
                OrderedJson::Bool(b) => Some(i64::from(*b)),
                OrderedJson::Int(i) => Some(*i),
                OrderedJson::Float(f) if f.fract() == 0.0 && f.abs() < 9.0e15 =>
                {
                    #[allow(clippy::cast_possible_truncation)]
                    Some(*f as i64)
                }
                _ => None,
            };
            match as_int {
                Some(i) if choices.contains(&i) => Some(PV::Int(PyInt {
                    value: Some(i128::from(i)),
                    negative: i < 0,
                    canonical: i.to_string(),
                })),
                _ => {
                    let names: Vec<String> = choices.iter().map(i64::to_string).collect();
                    let refs: Vec<&str> = names.iter().map(String::as_str).collect();
                    ctx.push(
                        loc,
                        format!("Input should be {}", choice_list(&refs, false)),
                        "literal_error",
                    );
                    None
                }
            }
        }
        Ty::List(inner) => match value {
            OrderedJson::Array(items) => {
                let mut out = Vec::with_capacity(items.len());
                let mut ok = true;
                for (i, item) in items.iter().enumerate() {
                    match check(item, inner, &with(loc, Loc::Idx(i)), ctx)? {
                        Some(v) => out.push(v),
                        None => ok = false,
                    }
                }
                ok.then_some(PV::List(out))
            }
            _ => {
                ctx.push(loc, "Input should be a valid list", "list_type");
                None
            }
        },
        Ty::Dict(key_ty, value_ty) => match value {
            OrderedJson::Object(entries) => {
                let mut out = Vec::with_capacity(entries.len());
                let mut ok = true;
                for (k, v) in entries {
                    let kloc = with(
                        &with(loc, Loc::Key(k.clone())),
                        Loc::Key("[key]".to_owned()),
                    );
                    let key = check(&OrderedJson::Str(k.clone()), key_ty, &kloc, ctx)?;
                    let val = check(v, value_ty, &with(loc, Loc::Key(k.clone())), ctx)?;
                    match (key, val) {
                        (Some(key), Some(val)) => out.push((key, val)),
                        _ => ok = false,
                    }
                }
                ok.then_some(PV::Dict(out))
            }
            _ => {
                ctx.push(loc, "Input should be a valid dictionary", "dict_type");
                None
            }
        },
        Ty::AnyDict => match value {
            OrderedJson::Object(_) => Some(PV::Any(value.clone())),
            _ => {
                ctx.push(loc, "Input should be a valid dictionary", "dict_type");
                None
            }
        },
        Ty::Model(model) => model_value(value, model, loc, ctx)?,
        Ty::Tuple(inner) => match value {
            OrderedJson::Array(items) => {
                let mut out = Vec::with_capacity(items.len());
                let mut ok = true;
                for (i, item) in items.iter().enumerate() {
                    match check(item, inner, &with(loc, Loc::Idx(i)), ctx)? {
                        Some(v) => out.push(v),
                        None => ok = false,
                    }
                }
                ok.then_some(PV::List(out))
            }
            _ => {
                ctx.push(loc, "Input should be a valid tuple", "tuple_type");
                None
            }
        },
        Ty::Pair(a, b) => match value {
            OrderedJson::Array(items) if items.len() > 2 => {
                ctx.push(
                    loc,
                    format!(
                        "Tuple should have at most 2 items after validation, not {}",
                        items.len()
                    ),
                    "too_long",
                );
                None
            }
            OrderedJson::Array(items) => {
                let mut out = Vec::with_capacity(2);
                let mut ok = true;
                for (i, ty) in [a, b].into_iter().enumerate() {
                    let here = with(loc, Loc::Idx(i));
                    match items.get(i) {
                        None => {
                            ctx.push(&here, "Field required", "missing");
                            ok = false;
                        }
                        Some(item) => match check(item, ty, &here, ctx)? {
                            Some(v) => out.push(v),
                            None => ok = false,
                        },
                    }
                }
                ok.then_some(PV::List(out))
            }
            _ => {
                ctx.push(loc, "Input should be a valid tuple", "tuple_type");
                None
            }
        },
    })
}

fn model_value(
    value: &OrderedJson,
    model: &Model,
    loc: &[Loc],
    ctx: &mut Ctx,
) -> Result<Option<PV>, Problem> {
    if let Some(before) = model.before {
        before(value)?;
    }
    let OrderedJson::Object(entries) = value else {
        ctx.push(
            loc,
            "Input should be a valid dictionary or object to extract fields from",
            "model_attributes_type",
        );
        return Ok(None);
    };
    let before = ctx.errors.len();
    let mut fields = Vec::with_capacity(model.fields.len());
    for (name, ty, default) in &model.fields {
        let given = entries.iter().find(|(k, _)| k == name).map(|(_, v)| v);
        match given {
            None => match default {
                Default::Required => {
                    ctx.push(
                        &with(loc, Loc::Key((*name).to_owned())),
                        "Field required",
                        "missing",
                    );
                }
                Default::Unset => fields.push(((*name).to_owned(), None)),
            },
            Some(v) => {
                if let Some(pv) = check(v, ty, &with(loc, Loc::Key((*name).to_owned())), ctx)? {
                    fields.push(((*name).to_owned(), Some(pv)));
                }
            }
        }
    }
    if model.forbid_extra {
        for (key, _) in entries {
            if !model.fields.iter().any(|(n, _, _)| n == key) {
                ctx.push(
                    &with(loc, Loc::Key(key.clone())),
                    "Extra inputs are not permitted",
                    "extra_forbidden",
                );
            }
        }
    }
    if ctx.errors.len() != before {
        return Ok(None);
    }
    if let Some(after) = model.after
        && let Err(message) = after(&fields)
    {
        ctx.push(loc, format!("Value error, {message}"), "value_error");
        return Ok(None);
    }
    Ok(Some(PV::Model(fields)))
}

/// `'A', 'B' or 'C'` (strings quoted with `repr`) / `1, 5 or 60`.
fn choice_list(choices: &[&str], quoted: bool) -> String {
    let shown: Vec<String> = choices
        .iter()
        .map(|c| {
            if quoted {
                baskfy_providers::py::repr_str(c)
            } else {
                (*c).to_owned()
            }
        })
        .collect();
    match shown.split_last() {
        None => String::new(),
        Some((last, [])) => last.clone(),
        Some((last, rest)) => format!("{} or {last}", rest.join(", ")),
    }
}

fn bounded(d: &PyDec, bounds: &[Bound], loc: &[Loc], ctx: &mut Ctx) -> bool {
    for b in bounds {
        let (limit, ok, msg, kind) = match *b {
            Bound::Gt(t) => (
                t,
                Ordering::is_gt as fn(Ordering) -> bool,
                "greater than",
                "greater_than",
            ),
            Bound::Ge(t) => (
                t,
                Ordering::is_ge as fn(Ordering) -> bool,
                "greater than or equal to",
                "greater_than_equal",
            ),
            Bound::Le(t) => (
                t,
                Ordering::is_le as fn(Ordering) -> bool,
                "less than or equal to",
                "less_than_equal",
            ),
        };
        let Some(limit_d) = PyDec::parse(limit) else {
            continue;
        };
        if !ok(d.cmp_num(&limit_d)) {
            ctx.push(loc, format!("Input should be {msg} {limit}"), kind);
            return false;
        }
    }
    true
}

fn int(value: &OrderedJson, loc: &[Loc], ctx: &mut Ctx) -> Option<PyInt> {
    let from_i128 = |v: i128| PyInt {
        value: Some(v),
        negative: v < 0,
        canonical: v.to_string(),
    };
    match value {
        OrderedJson::Bool(b) => Some(from_i128(i128::from(*b))),
        OrderedJson::Int(i) => Some(from_i128(i128::from(*i))),
        OrderedJson::UInt(u) => Some(from_i128(i128::from(*u))),
        OrderedJson::BigInt(s) => lax_int(s),
        OrderedJson::Float(f) => {
            if !f.is_finite() {
                ctx.push(loc, "Input should be a finite number", "finite_number");
                None
            } else if f.fract() != 0.0 {
                ctx.push(
                    loc,
                    "Input should be a valid integer, got a number with a fractional part",
                    "int_from_float",
                );
                None
            } else if f.abs() >= 9.223_372_036_854_775e18 {
                ctx.push(
                    loc,
                    "Unable to parse input string as an integer, exceeded maximum size",
                    "int_parsing_size",
                );
                None
            } else {
                #[allow(clippy::cast_possible_truncation)]
                let v = *f as i64;
                Some(from_i128(i128::from(v)))
            }
        }
        OrderedJson::Str(s) => {
            let parsed = lax_int(s);
            if parsed.is_none() {
                ctx.push(
                    loc,
                    "Input should be a valid integer, unable to parse string as an integer",
                    "int_parsing",
                );
            }
            parsed
        }
        _ => {
            ctx.push(loc, "Input should be a valid integer", "int_type");
            None
        }
    }
}

fn decimal(value: &OrderedJson, loc: &[Loc], ctx: &mut Ctx) -> Option<PyDec> {
    let not_finite = |ctx: &mut Ctx| {
        ctx.push(loc, "Input should be a finite number", "finite_number");
        None
    };
    match value {
        OrderedJson::Int(i) => PyDec::parse(&i.to_string()),
        OrderedJson::UInt(u) => PyDec::parse(&u.to_string()),
        OrderedJson::BigInt(s) => PyDec::parse(s),
        OrderedJson::Float(f) => {
            if f.is_finite() {
                PyDec::from_float(*f)
            } else {
                not_finite(ctx)
            }
        }
        OrderedJson::Str(s) => {
            let t = s.trim();
            if let Some(d) = PyDec::parse(t) {
                return Some(d);
            }
            let lower = t.trim_start_matches(['+', '-']).to_ascii_lowercase();
            if matches!(lower.as_str(), "nan" | "snan" | "inf" | "infinity") {
                return not_finite(ctx);
            }
            ctx.push(loc, "Input should be a valid decimal", "decimal_parsing");
            None
        }
        _ => {
            ctx.push(
                loc,
                "Decimal input should be an integer, float, string or Decimal object",
                "decimal_type",
            );
            None
        }
    }
}

// pydantic's lax `float`: a bool or an int converts, a string parses as Python's `float()`.
fn float(value: &OrderedJson, loc: &[Loc], ctx: &mut Ctx) -> Option<PyDec> {
    let finite = |f: f64, ctx: &mut Ctx| {
        let d = PyDec::from_float(f);
        if d.is_none() {
            ctx.push(loc, "Input should be a finite number", "finite_number");
        }
        d
    };
    match value {
        OrderedJson::Bool(b) => PyDec::parse(if *b { "1.0" } else { "0.0" }),
        OrderedJson::Int(i) => finite(*i as f64, ctx),
        OrderedJson::UInt(u) => finite(*u as f64, ctx),
        OrderedJson::BigInt(s) => finite(s.parse::<f64>().unwrap_or(f64::INFINITY), ctx),
        OrderedJson::Float(f) => finite(*f, ctx),
        OrderedJson::Str(s) => match s.trim().parse::<f64>() {
            Ok(f) => finite(f, ctx),
            Err(_) => {
                ctx.push(
                    loc,
                    "Input should be a valid number, unable to parse string as a number",
                    "float_parsing",
                );
                None
            }
        },
        _ => {
            ctx.push(loc, "Input should be a valid number", "float_type");
            None
        }
    }
}

fn string(
    value: &OrderedJson,
    min: Option<usize>,
    max: Option<usize>,
    pattern: Option<Pattern>,
    loc: &[Loc],
    ctx: &mut Ctx,
) -> Option<PV> {
    let OrderedJson::Str(s) = value else {
        ctx.push(loc, "Input should be a valid string", "string_type");
        return None;
    };
    let n = s.chars().count();
    let plural = |k: usize| if k == 1 { "" } else { "s" };
    if let Some(min) = min
        && n < min
    {
        ctx.push(
            loc,
            format!("String should have at least {min} character{}", plural(min)),
            "string_too_short",
        );
        return None;
    }
    if let Some(max) = max
        && n > max
    {
        ctx.push(
            loc,
            format!("String should have at most {max} character{}", plural(max)),
            "string_too_long",
        );
        return None;
    }
    if let Some((source, matcher)) = pattern
        && !matcher(s)
    {
        ctx.push(
            loc,
            format!("String should match pattern '{source}'"),
            "string_pattern_mismatch",
        );
        return None;
    }
    Some(PV::Str(s.clone()))
}

fn boolean(value: &OrderedJson, loc: &[Loc], ctx: &mut Ctx) -> Option<bool> {
    let unable = |ctx: &mut Ctx| {
        ctx.push(
            loc,
            "Input should be a valid boolean, unable to interpret input",
            "bool_parsing",
        );
        None
    };
    match value {
        OrderedJson::Bool(b) => Some(*b),
        OrderedJson::Int(0) => Some(false),
        OrderedJson::Int(1) => Some(true),
        OrderedJson::Int(_) | OrderedJson::UInt(_) | OrderedJson::BigInt(_) => unable(ctx),
        OrderedJson::Float(f)
            if f.to_bits() == 0.0_f64.to_bits() || f.to_bits() == (-0.0_f64).to_bits() =>
        {
            Some(false)
        }
        OrderedJson::Float(f) if f.to_bits() == 1.0_f64.to_bits() => Some(true),
        OrderedJson::Str(s) => lax_bool(s).or_else(|| unable(ctx)),
        _ => {
            ctx.push(loc, "Input should be a valid boolean", "bool_type");
            None
        }
    }
}

// ---------------------------------------------------------------------------------------------
// Dates and times (speedate's parser, as pydantic-core calls it)
// ---------------------------------------------------------------------------------------------

/// A parsed time-of-day: the time, and whether an offset was given.
type ParsedTime = (NaiveTime, bool);

fn two_digits(b: &[u8], at: usize) -> Option<u32> {
    let hi = *b.get(at)?;
    let lo = *b.get(at + 1)?;
    (hi.is_ascii_digit() && lo.is_ascii_digit())
        .then(|| u32::from(hi - b'0') * 10 + u32::from(lo - b'0'))
}

/// speedate's `Time::parse_bytes_offset`: the time, its offset flag, and how many bytes it used
/// (the caller decides whether the rest is allowed).
fn parse_time_prefix(b: &[u8]) -> Result<(ParsedTime, usize), &'static str> {
    if b.len() < 5 {
        return Err("input is too short");
    }
    let hour = two_digits(b, 0).ok_or("invalid character in hour")?;
    if b[2] != b':' {
        return Err("invalid time separator, expected `:`");
    }
    let minute = two_digits(b, 3).ok_or("invalid character in minute")?;
    if hour > 23 {
        return Err("hour value is outside expected range of 0-23");
    }
    if minute > 59 {
        return Err("minute value is outside expected range of 0-59");
    }
    let mut pos = 5;
    let mut second = 0;
    let mut micros = 0;
    if b.get(pos) == Some(&b':') {
        if b.len() < pos + 3 {
            return Err("input is too short");
        }
        second = two_digits(b, pos + 1).ok_or("invalid character in second")?;
        if second > 59 {
            return Err("second value is outside expected range of 0-59");
        }
        pos += 3;
        if matches!(b.get(pos), Some(b'.' | b',')) {
            pos += 1;
            let start = pos;
            let mut digits = 0;
            while let Some(c) = b.get(pos).filter(|c| c.is_ascii_digit()) {
                if digits < 6 {
                    micros = micros * 10 + u32::from(c - b'0');
                    digits += 1;
                }
                pos += 1;
            }
            if pos == start {
                return Err("second fraction digits missing after `.`");
            }
            for _ in digits..6 {
                micros *= 10;
            }
        }
    }
    let mut tz = false;
    match b.get(pos) {
        Some(b'Z' | b'z') => {
            tz = true;
            pos += 1;
        }
        Some(b'+' | b'-') => {
            let h = two_digits(b, pos + 1).ok_or("invalid timezone hour")?;
            let mut next = pos + 3;
            if b.get(next) == Some(&b':') {
                next += 1;
            }
            let m = two_digits(b, next).ok_or("invalid timezone minute")?;
            if h > 23 || m > 59 {
                return Err("timezone offset must be less than 24 hours");
            }
            tz = true;
            pos = next + 2;
        }
        _ => {}
    }
    let t = NaiveTime::from_hms_micro_opt(hour, minute, second, micros)
        .ok_or("hour value is outside expected range of 0-23")?;
    Ok(((t, tz), pos))
}

fn time(value: &OrderedJson, loc: &[Loc], ctx: &mut Ctx) -> Option<PV> {
    let fail = |ctx: &mut Ctx, why: &str| {
        ctx.push(
            loc,
            format!("Input should be in a valid time format, {why}"),
            "time_parsing",
        );
        None
    };
    let seconds = match value {
        OrderedJson::Str(s) => {
            return match parse_time_prefix(s.as_bytes()) {
                Ok(((t, tz), used)) if used == s.len() => Some(PV::Time(t, tz)),
                Ok(_) => fail(ctx, "unexpected extra characters at the end of the input"),
                Err(why) => fail(ctx, why),
            };
        }
        OrderedJson::Int(i) => {
            #[allow(clippy::cast_precision_loss)]
            let f = *i as f64;
            f
        }
        OrderedJson::Float(f) => *f,
        _ => {
            ctx.push(loc, "Input should be a valid time", "time_type");
            return None;
        }
    };
    if seconds < 0.0 {
        return fail(ctx, "time in seconds should be positive");
    }
    if seconds >= 86_400.0 {
        return fail(ctx, "numeric times may not exceed 86,399 seconds");
    }
    #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
    let whole = seconds.trunc() as u32;
    #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
    let micros = ((seconds - seconds.trunc()) * 1_000_000.0).round() as u32;
    NaiveTime::from_num_seconds_from_midnight_opt(whole, micros * 1000).map(|t| PV::Time(t, true))
}

fn days_in_month(year: i32, month: u32) -> u32 {
    match month {
        2 => {
            if (year % 4 == 0 && year % 100 != 0) || year % 400 == 0 {
                29
            } else {
                28
            }
        }
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

/// speedate's `Date::parse_bytes_rfc3339` over the first ten bytes.
fn parse_date_prefix(b: &[u8]) -> Result<(i32, u32, u32), &'static str> {
    if b.len() < 10 {
        return Err("input is too short");
    }
    let year_digits = &b[..4];
    if !year_digits.iter().all(u8::is_ascii_digit) {
        return Err("invalid character in year");
    }
    let year = year_digits
        .iter()
        .fold(0_i32, |acc, c| acc * 10 + i32::from(c - b'0'));
    if b[4] != b'-' {
        return Err("invalid date separator, expected `-`");
    }
    let month = two_digits(b, 5).ok_or("invalid character in month")?;
    if b[7] != b'-' {
        return Err("invalid date separator, expected `-`");
    }
    let day = two_digits(b, 8).ok_or("invalid character in day")?;
    if !(1..=12).contains(&month) {
        return Err("month value is outside expected range of 1-12");
    }
    if day == 0 || day > days_in_month(year, month) {
        return Err("day value is outside expected range");
    }
    Ok((year, month, day))
}

fn date(value: &OrderedJson, loc: &[Loc], ctx: &mut Ctx) -> Option<NaiveDate> {
    let inexact = |ctx: &mut Ctx| {
        ctx.push(
            loc,
            "Datetimes provided to dates should have zero time - e.g. be exact dates",
            "date_from_datetime_inexact",
        );
        None
    };
    let parsing = |ctx: &mut Ctx, why: &str| {
        ctx.push(
            loc,
            format!("Input should be a valid date or datetime, {why}"),
            "date_from_datetime_parsing",
        );
        None
    };
    let from_seconds = |ctx: &mut Ctx, secs: i64| {
        // speedate reads a large number as milliseconds.
        let secs = if secs.abs() > 20_000_000_000 {
            if secs % 1000 != 0 {
                return inexact(ctx);
            }
            secs / 1000
        } else {
            secs
        };
        if secs % 86_400 != 0 {
            return inexact(ctx);
        }
        NaiveDate::from_ymd_opt(1970, 1, 1)
            .and_then(|epoch| epoch.checked_add_signed(chrono::Duration::days(secs / 86_400)))
    };
    match value {
        OrderedJson::Bool(_) => {
            ctx.push(loc, "Input should be a valid date", "date_type");
            None
        }
        OrderedJson::Int(i) => from_seconds(ctx, *i),
        OrderedJson::Float(f) => {
            if f.fract() != 0.0 || !f.is_finite() {
                inexact(ctx)
            } else {
                #[allow(clippy::cast_possible_truncation)]
                from_seconds(ctx, *f as i64)
            }
        }
        OrderedJson::Str(s) => {
            let b = s.as_bytes();
            if !b.is_empty() && b.iter().all(u8::is_ascii_digit) && b.len() <= 18 {
                return s.parse::<i64>().ok().and_then(|v| from_seconds(ctx, v));
            }
            let (y, m, d) = match parse_date_prefix(b) {
                Ok(v) => v,
                Err(why) => return parsing(ctx, why),
            };
            if b.len() > 10 {
                if !matches!(b[10], b'T' | b't' | b'_' | b' ') {
                    return parsing(
                        ctx,
                        "invalid datetime separator, expected `T`, `t`, `_` or space",
                    );
                }
                match parse_time_prefix(&b[11..]) {
                    Ok(((t, _), used)) if used == b.len() - 11 => {
                        if t != NaiveTime::MIN {
                            return inexact(ctx);
                        }
                    }
                    Ok(_) => {
                        return parsing(ctx, "unexpected extra characters at the end of the input");
                    }
                    Err(why) => return parsing(ctx, why),
                }
            }
            if y == 0 {
                ctx.push(
                    loc,
                    "Input should be a valid date in the format YYYY-MM-DD, year 0 is out of range",
                    "date_parsing",
                );
                return None;
            }
            NaiveDate::from_ymd_opt(y, m, d)
        }
        _ => {
            ctx.push(loc, "Input should be a valid date", "date_type");
            None
        }
    }
}

/// Python's `str.strip()`: every character `str.isspace()` accepts, which (beyond Unicode
/// `White_Space`) includes the separators U+001C..U+001F.
pub(crate) fn py_strip(s: &str) -> &str {
    s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

/// `date.isoformat()` / `str(date)`.
pub(crate) fn iso_date(d: NaiveDate) -> String {
    format!("{:04}-{:02}-{:02}", d.year(), d.month(), d.day())
}

/// `Decimal(text)` for a constant (a settings value or a bound), as a [`PyDec`].
pub(crate) fn dec_const(d: Decimal) -> PyDec {
    PyDec::from_decimal(d)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::unreachable, clippy::panic)]
    use super::*;

    fn dec(s: &str) -> PyDec {
        PyDec::parse(s).unwrap()
    }

    #[test]
    fn decimal_str_is_pythons() {
        for (input, expected) in [
            ("0.5", "0.5"),
            ("100.0", "100.0"),
            ("1e2", "1E+2"),
            ("0.50", "0.50"),
            ("1.5e-7", "1.5E-7"),
            ("0.000001", "0.000001"),
            ("0.0000001", "1E-7"),
            ("007", "7"),
            ("-0", "-0"),
            ("0.00", "0.00"),
            ("1_000", "1000"),
            ("1.2345678901234567e+19", "1.2345678901234567E+19"),
            ("12345678901234567890123", "12345678901234567890123"),
        ] {
            assert_eq!(dec(input).py_str(), expected, "{input}");
        }
        assert!(PyDec::parse("1__0").is_none());
        assert!(PyDec::parse("abc").is_none());
        assert!(PyDec::parse(".").is_none());
        assert_eq!(PyDec::from_float(1e16).unwrap().py_str(), "1E+16");
        assert_eq!(PyDec::from_float(0.1).unwrap().py_str(), "0.1");
    }

    #[test]
    fn decimals_compare_numerically() {
        assert_eq!(dec("0.50").cmp_num(&dec("0.5")), Ordering::Equal);
        assert_eq!(dec("1E+2").cmp_num(&dec("99.99")), Ordering::Greater);
        assert_eq!(dec("-1").cmp_num(&dec("0")), Ordering::Less);
        assert_eq!(dec("-2").cmp_num(&dec("-1")), Ordering::Less);
        assert_eq!(dec("0.001").cmp_num(&dec("0.01")), Ordering::Less);
        assert_eq!(dec("1E+2").to_decimal().unwrap().to_string(), "100");
        assert_eq!(dec("0.050").to_decimal().unwrap().to_string(), "0.050");
    }

    #[test]
    fn bounds_and_types_report_as_pydantic() {
        let model = Model {
            fields: vec![
                ("i", Ty::Int(vec![Bound::Gt("0")]).opt(), Default::Unset),
                ("b", Ty::Bool.opt(), Default::Unset),
                ("s", Ty::str_len(None, Some(3)).opt(), Default::Unset),
            ],
            forbid_extra: true,
            before: None,
            after: None,
        };
        let got = validate_value(
            &OrderedJson::parse(r#"{"zz":1,"s":5,"i":0}"#).unwrap(),
            &Ty::Model(Box::new(model)),
        );
        let Err(Fail::Errors(errors)) = got else {
            unreachable!("expected errors")
        };
        let fields: Vec<String> = errors
            .iter()
            .map(|e| match &e[0].1 {
                OrderedJson::Str(s) => s.clone(),
                _ => String::new(),
            })
            .collect();
        assert_eq!(fields, ["i", "s", "zz"]);
    }

    #[test]
    fn times_and_dates_parse_as_speedate() {
        let mut ctx = Ctx { errors: Vec::new() };
        let t = time(
            &OrderedJson::Str("15:10:00.123456789".to_owned()),
            &[],
            &mut ctx,
        );
        assert_eq!(t.unwrap().py_str(), "15:10:00.123456");
        assert!(time(&OrderedJson::Str("15:60".to_owned()), &[], &mut ctx).is_none());
        assert_eq!(
            ctx.errors.last().unwrap().msg,
            "Input should be in a valid time format, minute value is outside expected range of 0-59"
        );
        assert_eq!(
            date(
                &OrderedJson::Str("2026-11-02T00:00".to_owned()),
                &[],
                &mut ctx
            ),
            NaiveDate::from_ymd_opt(2026, 11, 2)
        );
        assert!(date(&OrderedJson::Str("2026-02-29".to_owned()), &[], &mut ctx).is_none());
        assert_eq!(
            date(&OrderedJson::Int(86_400), &[], &mut ctx),
            NaiveDate::from_ymd_opt(1970, 1, 2)
        );
    }
}
