//! pydantic's request-body validation for the writes-screens lane (wave 4c): the typed fields
//! the screens, curated-basket, manager and watchlist write routes declare, validated in Python
//! (lax) mode with `from_attributes=True`, exactly as FastAPI 0.141 + pydantic 2.13 run them.
//!
//! [`crate::body`] reads the body (content-type rule, CPython's JSON scanner) and validates the
//! string-only models of the session routes; this module adds the rest of what the lane's models
//! use — `int`, `Decimal`, `bool`, `Literal`, `date`, `list[...]`, `dict[str, ...]`, nested models
//! and `ScreenDefinition` (through [`baskfy_core::screener::ScreenDefinition::model_validate`]) —
//! with pydantic's error types, messages and locations, probed against the live Python
//! (DECISIONS-RS, lane writes-screens R6.12):
//!
//! * a model's fields are checked in declaration order, then each extra key in arrival order when
//!   the model forbids extras (`schemas._In`), none when it ignores them (pydantic's default);
//! * a nested model that is not an object is `model_attributes_type` (FastAPI validates with
//!   `from_attributes=True`, which every nested model inherits — `ScreenDefinition`'s own
//!   `model_type` errors are renamed accordingly);
//! * a list's `min_length` is checked only when every item validated ("after validation");
//! * `int` takes a bool, an integral float and pydantic's lax string; `Decimal` takes an int, a
//!   float (`Decimal(str(f))`), a string (`Decimal(text)`, Unicode digits and `_` included) but
//!   not a bool, and refuses a non-finite value; `bool` takes `0`/`1`, `0.0`/`1.0` and the lax
//!   string set; `date` takes pydantic's string forms and a Unix timestamp.
//!
//! Errors render as `app._handle_validation` renders them: `field` is the location without its
//! leading `body`, joined with `.` (`<root>` when empty).

use baskfy_core::decimal::WideDecimal;
use baskfy_core::screener::{PyDecimal, ScreenDefinition, ScreenError};
use baskfy_db::OrderedJson;
use chrono::NaiveDate;

use crate::body::Received;
use crate::problems::{ErrorEntry, Problem, invalid_screen_definition};
use crate::routes::common::lax_int;

/// One declared type.
#[derive(Debug, Clone)]
pub enum Ty {
    /// `str` with `min_length`/`max_length`.
    Str(Option<usize>, Option<usize>),
    /// `int` with `gt`/`ge`/`le` (the bound's printed form is the integer).
    Int {
        /// `gt`.
        gt: Option<i128>,
        /// `ge`.
        ge: Option<i128>,
        /// `le`.
        le: Option<i128>,
    },
    /// `Decimal` with `gt`/`ge`/`le`, each the bound's `str()` (`"0"`, `"1000"`).
    Dec {
        /// `gt`.
        gt: Option<&'static str>,
        /// `ge`.
        ge: Option<&'static str>,
        /// `le`.
        le: Option<&'static str>,
    },
    /// `bool`.
    Bool,
    /// `Literal[...]` of strings.
    Lit(&'static [&'static str]),
    /// `datetime.date`.
    Date,
    /// `list[T]` with an optional `min_length`.
    List(Box<Ty>, Option<usize>),
    /// `dict[str, T]`.
    Dict(Box<Ty>),
    /// A nested model.
    Model(Model),
    /// `baskfy_core.screen_definition.ScreenDefinition`.
    Definition,
    /// `T | None`.
    Opt(Box<Ty>),
}

/// One field: its name, type, and whether leaving it out is an error.
#[derive(Debug, Clone)]
pub struct Field {
    /// The field name (no aliases in this lane's models).
    pub name: &'static str,
    /// The declared type.
    pub ty: Ty,
    /// `True` when the field has no default.
    pub required: bool,
}

/// A required field.
#[must_use]
pub fn req(name: &'static str, ty: Ty) -> Field {
    Field {
        name,
        ty,
        required: true,
    }
}

/// A field with a default (absent → [`V::Absent`]; the caller applies the default).
#[must_use]
pub fn opt(name: &'static str, ty: Ty) -> Field {
    Field {
        name,
        ty,
        required: false,
    }
}

/// A model.
#[derive(Debug, Clone)]
pub struct Model {
    /// Fields in declaration order.
    pub fields: Vec<Field>,
    /// `extra="forbid"`.
    pub forbid_extra: bool,
}

/// A validated value.
#[derive(Debug, Clone)]
pub enum V {
    /// A defaulted field that was left out.
    Absent,
    /// `None`.
    None,
    /// A string.
    Str(String),
    /// An integer (saturated to `i128`; Python's is unbounded).
    Int(i128),
    /// A `Decimal`, exactly as pydantic built it.
    Dec(WideDecimal),
    /// A bool.
    Bool(bool),
    /// A date.
    Date(NaiveDate),
    /// A list.
    List(Vec<V>),
    /// A dict, keys in arrival order.
    Dict(Vec<(String, V)>),
    /// A nested model's fields, in declaration order.
    Model(Vec<(&'static str, V)>),
    /// A validated screen definition and the JSON it came from.
    Definition(Box<ScreenDefinition>),
}

impl V {
    /// The named field of a model value.
    #[must_use]
    pub fn get(&self, name: &str) -> &V {
        match self {
            V::Model(fields) => fields
                .iter()
                .find(|(k, _)| *k == name)
                .map_or(&V::Absent, |(_, v)| v),
            _ => &V::Absent,
        }
    }

    /// The string, if this is one.
    #[must_use]
    pub fn str(&self) -> Option<&str> {
        match self {
            V::Str(s) => Some(s),
            _ => None,
        }
    }

    /// The integer, if this is one.
    #[must_use]
    pub fn int(&self) -> Option<i128> {
        match self {
            V::Int(i) => Some(*i),
            _ => None,
        }
    }

    /// The decimal, if this is one.
    #[must_use]
    pub fn dec(&self) -> Option<&WideDecimal> {
        match self {
            V::Dec(d) => Some(d),
            _ => None,
        }
    }

    /// The bool, if this is one.
    #[must_use]
    pub fn bool(&self) -> Option<bool> {
        match self {
            V::Bool(b) => Some(*b),
            _ => None,
        }
    }

    /// The date, if this is one.
    #[must_use]
    pub fn date(&self) -> Option<NaiveDate> {
        match self {
            V::Date(d) => Some(*d),
            _ => None,
        }
    }

    /// The list items (empty for anything else).
    #[must_use]
    pub fn items(&self) -> &[V] {
        match self {
            V::List(items) => items,
            _ => &[],
        }
    }

    /// The dict entries (empty for anything else).
    #[must_use]
    pub fn entries(&self) -> &[(String, V)] {
        match self {
            V::Dict(entries) => entries,
            _ => &[],
        }
    }
}

/// The error accumulator (location parts, as pydantic's `loc`).
#[derive(Debug, Default)]
pub struct Errors {
    entries: Vec<ErrorEntry>,
}

fn field_path(loc: &[String]) -> String {
    let joined = loc.join(".");
    if joined.is_empty() {
        "<root>".to_owned()
    } else {
        joined
    }
}

impl Errors {
    /// A fresh accumulator.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Records one error at `loc`.
    pub fn push(&mut self, loc: &[String], message: &str, kind: &str) {
        self.entries.push(vec![
            ("field".to_owned(), OrderedJson::Str(field_path(loc))),
            ("message".to_owned(), OrderedJson::Str(message.to_owned())),
            ("type".to_owned(), OrderedJson::Str(kind.to_owned())),
        ]);
    }

    /// `RequestValidationError` → the 400, or `Ok` when empty.
    ///
    /// # Errors
    /// The accumulated errors.
    pub fn finish(self) -> Result<(), Problem> {
        if self.entries.is_empty() {
            Ok(())
        } else {
            Err(invalid_screen_definition(self.entries))
        }
    }
}

fn at(loc: &[String], key: impl Into<String>) -> Vec<String> {
    let mut out = loc.to_vec();
    out.push(key.into());
    out
}

const ATTRS: &str = "Input should be a valid dictionary or object to extract fields from";

fn plural(n: usize, word: &str) -> String {
    if n == 1 {
        word.to_owned()
    } else {
        format!("{word}s")
    }
}

/// An integer `int()` would read from a JSON integer literal of any size, saturated.
fn json_int(value: &OrderedJson) -> Option<i128> {
    match value {
        OrderedJson::Int(i) => Some(i128::from(*i)),
        OrderedJson::UInt(u) => Some(i128::from(*u)),
        OrderedJson::BigInt(s) => Some(s.parse::<i128>().unwrap_or(if s.starts_with('-') {
            i128::MIN
        } else {
            i128::MAX
        })),
        _ => None,
    }
}

/// The canonical digits of a JSON integer literal.
fn json_int_text(value: &OrderedJson) -> Option<String> {
    match value {
        OrderedJson::Int(i) => Some(i.to_string()),
        OrderedJson::UInt(u) => Some(u.to_string()),
        OrderedJson::BigInt(s) => Some(s.clone()),
        _ => None,
    }
}

/// An integral finite float as `int(f)` would give it, saturated.
fn integral_float(f: f64) -> Option<i128> {
    if !f.is_finite() || f.fract() != 0.0 {
        return None;
    }
    // `f` is integral; beyond i128 only the sign matters to a bound.
    let text = format!("{f:.0}");
    Some(
        text.parse::<i128>()
            .unwrap_or(if f < 0.0 { i128::MIN } else { i128::MAX }),
    )
}

/// `Decimal(text)` (pydantic strips whitespace first; `Decimal` itself keeps `_` and Unicode
/// digits), or the error pydantic reports.
fn decimal_from_text(text: &str) -> Result<WideDecimal, (&'static str, &'static str)> {
    const PARSING: (&str, &str) = ("decimal_parsing", "Input should be a valid decimal");
    match PyDecimal::parse(text) {
        Some(PyDecimal::Finite(d)) => d.py_str().parse::<WideDecimal>().map_err(|_| PARSING),
        Some(PyDecimal::NonFinite(_)) => Err(("finite_number", "Input should be a finite number")),
        None => Err(PARSING),
    }
}

fn days_from_epoch(days: i64) -> Option<NaiveDate> {
    NaiveDate::from_ymd_opt(1970, 1, 1)?.checked_add_signed(chrono::TimeDelta::try_days(days)?)
}

const INEXACT: &str = "Datetimes provided to dates should have zero time - e.g. be exact dates";

/// speedate's `UNIX_0000` and `UNIX_9999`: the seconds of 0000-01-01T00:00:00 and
/// 9999-12-31T23:59:59.
const UNIX_0000: i64 = -62_167_219_200;
const UNIX_9999: i64 = 253_402_300_799;
/// speedate's `MS_WATERSHED`: an absolute timestamp above it is milliseconds.
const MS_WATERSHED: i64 = 20_000_000_000;

fn timestamp_parsing(why: &str) -> (&'static str, String) {
    (
        "date_from_datetime_parsing",
        format!("Input should be a valid date or datetime, {why}"),
    )
}

/// pydantic-core's `int_as_datetime(ts, micro)` → `date_from_datetime`, i.e. speedate's
/// `DateTime::from_timestamp` (the millisecond watershed, the microsecond carry, the
/// 0000/9999 range — checked before exactness) and then "exact midnight only"; a year-0 midnight
/// is refused when it becomes a Python `date`. Probed against the live pydantic in review.
fn date_from_timestamp(
    timestamp: i64,
    microsecond: u32,
) -> Result<NaiveDate, (&'static str, String)> {
    let small = || timestamp_parsing("dates before 0000 are not supported as unix timestamps");
    let large = || timestamp_parsing("dates after 9999 are not supported as unix timestamps");
    let magnitude = timestamp.checked_abs().ok_or_else(small)?;
    let (mut second, extra) = if magnitude <= MS_WATERSHED {
        (timestamp, 0_i64)
    } else {
        let mut s = timestamp / 1000;
        let mut us = (timestamp % 1000) * 1000;
        if us < 0 {
            s -= 1;
            us += 1_000_000;
        }
        (s, us)
    };
    let mut total = i64::from(microsecond) + extra;
    if total >= 1_000_000 {
        second = second.checked_add(total / 1_000_000).ok_or_else(large)?;
        total %= 1_000_000;
    }
    if second < UNIX_0000 {
        return Err(small());
    }
    if second > UNIX_9999 {
        return Err(large());
    }
    if second.rem_euclid(86_400) != 0 || total != 0 {
        return Err(("date_from_datetime_inexact", INEXACT.to_owned()));
    }
    let date = days_from_epoch(second.div_euclid(86_400)).ok_or_else(large)?;
    if chrono::Datelike::year(&date) == 0 {
        return Err((
            "date_parsing",
            "Input should be a valid date in the format YYYY-MM-DD, year 0 is out of range"
                .to_owned(),
        ));
    }
    Ok(date)
}

/// A JSON integer as a date: beyond `i64` only its sign matters (it is out of range either way).
fn date_from_int(value: i128) -> Result<NaiveDate, (&'static str, String)> {
    let clamped = i64::try_from(value).unwrap_or(if value < 0 { i64::MIN } else { i64::MAX });
    date_from_timestamp(clamped, 0)
}

/// pydantic-core's `float_as_datetime`: NaN refused; `floor()` (saturating, as Rust's `as`
/// casts in pydantic-core itself) plus the rounded microseconds of `|fract|`.
fn date_from_float(f: f64) -> Result<NaiveDate, (&'static str, String)> {
    if f.is_nan() {
        return Err(timestamp_parsing("NaN values not permitted"));
    }
    #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
    let (seconds, micro) = (
        f.floor() as i64,
        (f.fract().abs() * 1_000_000.0).round() as u32,
    );
    date_from_timestamp(seconds, micro)
}

fn literal_message(choices: &[&str]) -> String {
    let quoted: Vec<String> = choices
        .iter()
        .map(|c| baskfy_providers::py::repr_str(c))
        .collect();
    let joined = match quoted.as_slice() {
        [] => String::new(),
        [one] => one.clone(),
        [init @ .., last] => format!("{} or {last}", init.join(", ")),
    };
    format!("Input should be {joined}")
}

/// A JSON value as `screens::payload_of` hands it to the core's validator.
fn payload_of(value: &OrderedJson) -> baskfy_core::screener::Payload {
    super::payload_of(value)
}

fn decimal_bound_errors(
    e: &mut Errors,
    loc: &[String],
    d: &WideDecimal,
    gt: Option<&str>,
    ge: Option<&str>,
    le: Option<&str>,
) -> bool {
    let bound = |s: &str| s.parse::<WideDecimal>().ok();
    if let Some(g) = gt
        && let Some(b) = bound(g)
        && *d <= b
    {
        e.push(
            loc,
            &format!("Input should be greater than {g}"),
            "greater_than",
        );
        return false;
    }
    if let Some(g) = ge
        && let Some(b) = bound(g)
        && *d < b
    {
        e.push(
            loc,
            &format!("Input should be greater than or equal to {g}"),
            "greater_than_equal",
        );
        return false;
    }
    if let Some(l) = le
        && let Some(b) = bound(l)
        && *d > b
    {
        e.push(
            loc,
            &format!("Input should be less than or equal to {l}"),
            "less_than_equal",
        );
        return false;
    }
    true
}

fn str_len_ok(
    e: &mut Errors,
    loc: &[String],
    s: &str,
    min: Option<usize>,
    max: Option<usize>,
) -> bool {
    let n = s.chars().count();
    if let Some(m) = min
        && n < m
    {
        e.push(
            loc,
            &format!("String should have at least {m} {}", plural(m, "character")),
            "string_too_short",
        );
        return false;
    }
    if let Some(m) = max
        && n > m
    {
        e.push(
            loc,
            &format!("String should have at most {m} {}", plural(m, "character")),
            "string_too_long",
        );
        return false;
    }
    true
}

/// Validates `value` as `ty` at `loc`; `None` when an error was recorded.
pub fn validate(e: &mut Errors, ty: &Ty, value: &OrderedJson, loc: &[String]) -> Option<V> {
    match ty {
        Ty::Opt(inner) => {
            if *value == OrderedJson::Null {
                Some(V::None)
            } else {
                validate(e, inner, value, loc)
            }
        }
        Ty::Str(min, max) => match value {
            OrderedJson::Str(s) => str_len_ok(e, loc, s, *min, *max).then(|| V::Str(s.clone())),
            _ => {
                e.push(loc, "Input should be a valid string", "string_type");
                None
            }
        },
        Ty::Int { gt, ge, le } => {
            let n = match value {
                OrderedJson::Bool(b) => Some(i128::from(*b)),
                OrderedJson::Int(_) | OrderedJson::UInt(_) | OrderedJson::BigInt(_) => {
                    json_int(value)
                }
                OrderedJson::Float(f) if !f.is_finite() => {
                    e.push(loc, "Input should be a finite number", "finite_number");
                    return None;
                }
                OrderedJson::Float(f) => {
                    let Some(n) = integral_float(*f) else {
                        e.push(
                            loc,
                            "Input should be a valid integer, got a number with a fractional part",
                            "int_from_float",
                        );
                        return None;
                    };
                    Some(n)
                }
                OrderedJson::Str(s) => {
                    let Some(p) = lax_int(s) else {
                        e.push(
                            loc,
                            "Input should be a valid integer, unable to parse string as an integer",
                            "int_parsing",
                        );
                        return None;
                    };
                    Some(p.saturated())
                }
                _ => {
                    e.push(loc, "Input should be a valid integer", "int_type");
                    return None;
                }
            }?;
            if let Some(g) = gt
                && n <= *g
            {
                e.push(
                    loc,
                    &format!("Input should be greater than {g}"),
                    "greater_than",
                );
                return None;
            }
            if let Some(g) = ge
                && n < *g
            {
                e.push(
                    loc,
                    &format!("Input should be greater than or equal to {g}"),
                    "greater_than_equal",
                );
                return None;
            }
            if let Some(l) = le
                && n > *l
            {
                e.push(
                    loc,
                    &format!("Input should be less than or equal to {l}"),
                    "less_than_equal",
                );
                return None;
            }
            Some(V::Int(n))
        }
        Ty::Dec { gt, ge, le } => {
            let parsed = match value {
                OrderedJson::Int(_) | OrderedJson::UInt(_) | OrderedJson::BigInt(_) => {
                    json_int_text(value).map_or(
                        Err(("decimal_parsing", "Input should be a valid decimal")),
                        |t| decimal_from_text(&t),
                    )
                }
                OrderedJson::Float(f) if !f.is_finite() => {
                    Err(("finite_number", "Input should be a finite number"))
                }
                OrderedJson::Float(f) => decimal_from_text(&baskfy_core::py::float_repr(*f)),
                OrderedJson::Str(s) => decimal_from_text(s),
                _ => Err((
                    "decimal_type",
                    "Decimal input should be an integer, float, string or Decimal object",
                )),
            };
            match parsed {
                Ok(d) => decimal_bound_errors(e, loc, &d, *gt, *ge, *le).then_some(V::Dec(d)),
                Err((kind, message)) => {
                    e.push(loc, message, kind);
                    None
                }
            }
        }
        Ty::Bool => {
            let b = match value {
                OrderedJson::Bool(b) => Some(*b),
                OrderedJson::Int(0) => Some(false),
                OrderedJson::Int(1) => Some(true),
                OrderedJson::Int(_) | OrderedJson::UInt(_) | OrderedJson::BigInt(_) => {
                    e.push(
                        loc,
                        "Input should be a valid boolean, unable to interpret input",
                        "bool_parsing",
                    );
                    return None;
                }
                // pydantic's float -> bool: exactly 0.0 or 1.0 (-0.0 included).
                OrderedJson::Float(f) if f.abs().to_bits() == 0.0_f64.to_bits() => Some(false),
                OrderedJson::Float(f) if f.to_bits() == 1.0_f64.to_bits() => Some(true),
                OrderedJson::Str(s) => {
                    let Some(b) = crate::routes::common::lax_bool(s) else {
                        e.push(
                            loc,
                            "Input should be a valid boolean, unable to interpret input",
                            "bool_parsing",
                        );
                        return None;
                    };
                    Some(b)
                }
                _ => None,
            };
            if b.is_none() {
                e.push(loc, "Input should be a valid boolean", "bool_type");
            }
            b.map(V::Bool)
        }
        Ty::Lit(choices) => match value {
            OrderedJson::Str(s) if choices.contains(&s.as_str()) => Some(V::Str(s.clone())),
            _ => {
                e.push(loc, &literal_message(choices), "literal_error");
                None
            }
        },
        Ty::Date => {
            let result = match value {
                OrderedJson::Str(s) => baskfy_core::screener::lax_date_from_str(s),
                OrderedJson::Int(_) | OrderedJson::UInt(_) | OrderedJson::BigInt(_) => {
                    json_int(value).map_or_else(
                        || Err(("date_type", "Input should be a valid date".to_owned())),
                        date_from_int,
                    )
                }
                OrderedJson::Float(f) => date_from_float(*f),
                _ => Err(("date_type", "Input should be a valid date".to_owned())),
            };
            match result {
                Ok(d) => Some(V::Date(d)),
                Err((kind, message)) => {
                    e.push(loc, &message, kind);
                    None
                }
            }
        }
        Ty::List(item, min_len) => {
            let OrderedJson::Array(items) = value else {
                e.push(loc, "Input should be a valid list", "list_type");
                return None;
            };
            let before = e.entries.len();
            let mut out = Vec::with_capacity(items.len());
            for (i, it) in items.iter().enumerate() {
                if let Some(v) = validate(e, item, it, &at(loc, i.to_string())) {
                    out.push(v);
                }
            }
            if e.entries.len() != before {
                return None;
            }
            if let Some(m) = min_len
                && items.len() < *m
            {
                e.push(
                    loc,
                    &format!(
                        "List should have at least {m} {} after validation, not {}",
                        plural(*m, "item"),
                        items.len()
                    ),
                    "too_short",
                );
                return None;
            }
            Some(V::List(out))
        }
        Ty::Dict(inner) => {
            let OrderedJson::Object(entries) = value else {
                e.push(loc, "Input should be a valid dictionary", "dict_type");
                return None;
            };
            let before = e.entries.len();
            let mut out = Vec::with_capacity(entries.len());
            for (k, v) in entries {
                if let Some(v) = validate(e, inner, v, &at(loc, k.clone())) {
                    out.push((k.clone(), v));
                }
            }
            (e.entries.len() == before).then_some(V::Dict(out))
        }
        Ty::Model(model) => {
            let OrderedJson::Object(entries) = value else {
                e.push(loc, ATTRS, "model_attributes_type");
                return None;
            };
            validate_fields(e, model, entries, loc)
        }
        Ty::Definition => {
            match ScreenDefinition::model_validate(&payload_of(value)) {
                Ok(d) => Some(V::Definition(Box::new(d))),
                Err(ScreenError::Validation(err)) => {
                    for detail in err.errors {
                        let mut full = loc.to_vec();
                        full.extend(detail.loc.iter().cloned());
                        if detail.kind == "model_type" {
                            e.push(&full, ATTRS, "model_attributes_type");
                        } else {
                            e.push(&full, &detail.msg, &detail.kind);
                        }
                    }
                    None
                }
                Err(other) => {
                    // Not a ValidationError: the core raised something pydantic would not (no
                    // stored or posted definition reaches here on the bed).
                    e.push(loc, &other.to_string(), "value_error");
                    None
                }
            }
        }
    }
}

fn validate_fields(
    e: &mut Errors,
    model: &Model,
    entries: &[(String, OrderedJson)],
    loc: &[String],
) -> Option<V> {
    let before = e.entries.len();
    let mut out = Vec::with_capacity(model.fields.len());
    for field in &model.fields {
        let given = entries
            .iter()
            .find(|(k, _)| k == field.name)
            .map(|(_, v)| v);
        match given {
            None if field.required => {
                e.push(&at(loc, field.name), "Field required", "missing");
            }
            None => out.push((field.name, V::Absent)),
            Some(v) => {
                if let Some(v) = validate(e, &field.ty, v, &at(loc, field.name)) {
                    out.push((field.name, v));
                }
            }
        }
    }
    if model.forbid_extra {
        for (k, _) in entries {
            if !model.fields.iter().any(|f| f.name == k) {
                e.push(
                    &at(loc, k.clone()),
                    "Extra inputs are not permitted",
                    "extra_forbidden",
                );
            }
        }
    }
    (e.entries.len() == before).then_some(V::Model(out))
}

/// FastAPI's body parameter: `received` validated as `model` at the root (`body`), its errors
/// appended to `e` after any path-parameter errors already there.
///
/// * a required body that is missing (empty, or JSON `null`) is `missing` at `<root>`;
/// * an optional body (`Model | None = None`) that is missing is `V::None`;
/// * anything but an object is `model_attributes_type` at `<root>`.
pub fn body(e: &mut Errors, received: &Received, model: &Model, required: bool) -> Option<V> {
    match received {
        Received::Missing | Received::Json(OrderedJson::Null) => {
            if required {
                e.push(&[], "Field required", "missing");
                None
            } else {
                Some(V::None)
            }
        }
        Received::Json(OrderedJson::Object(entries)) => validate_fields(e, model, entries, &[]),
        Received::Raw | Received::Json(_) => {
            e.push(&[], ATTRS, "model_attributes_type");
            None
        }
    }
}

/// A path parameter declared `int` (`investment_id: int`): pydantic's lax string rule, the
/// value saturated to `i128`.
pub fn path_int(e: &mut Errors, name: &str, raw: &str) -> Option<i128> {
    match lax_int(raw) {
        Some(p) => Some(p.saturated()),
        None => {
            e.push(
                &[name.to_owned()],
                "Input should be a valid integer, unable to parse string as an integer",
                "int_parsing",
            );
            None
        }
    }
}

/// A path parameter declared `Annotated[str, Path(min_length=, max_length=)]`.
pub fn path_str(e: &mut Errors, name: &str, raw: &str, min: Option<usize>, max: Option<usize>) {
    let _ = str_len_ok(e, &[name.to_owned()], raw, min, max);
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn json(s: &str) -> OrderedJson {
        OrderedJson::parse(s).unwrap()
    }

    fn errors_of(ty: &Ty, input: &str) -> Vec<(String, String, String)> {
        let mut e = Errors::new();
        let _ = validate(&mut e, ty, &json(input), &["x".to_owned()]);
        e.entries
            .iter()
            .map(|entry| {
                let get = |k: &str| match entry.iter().find(|(key, _)| key == k) {
                    Some((_, OrderedJson::Str(s))) => s.clone(),
                    _ => String::new(),
                };
                (get("field"), get("type"), get("message"))
            })
            .collect()
    }

    #[test]
    fn integers_follow_pydantics_lax_python_mode() {
        let ty = Ty::Int {
            gt: None,
            ge: Some(1),
            le: Some(28),
        };
        for ok in ["true", "5.0", "\"5\"", "\" 7 \"", "\"5_0\"", "\"5.0\""] {
            let mut e = Errors::new();
            assert!(
                validate(
                    &mut e,
                    &Ty::Int {
                        gt: None,
                        ge: None,
                        le: None
                    },
                    &json(ok),
                    &[]
                )
                .is_some(),
                "{ok}"
            );
        }
        assert_eq!(errors_of(&ty, "5.5")[0].1, "int_from_float");
        assert_eq!(errors_of(&ty, "\"1e3\"")[0].1, "int_parsing");
        assert_eq!(errors_of(&ty, "null")[0].1, "int_type");
        assert_eq!(
            errors_of(&ty, "29")[0].2,
            "Input should be less than or equal to 28"
        );
        assert_eq!(
            errors_of(&ty, "0")[0].2,
            "Input should be greater than or equal to 1"
        );
    }

    #[test]
    fn decimals_keep_pythons_exponent_and_refuse_bools() {
        let ty = Ty::Dec {
            gt: Some("0"),
            ge: None,
            le: None,
        };
        let mut e = Errors::new();
        let v = validate(&mut e, &ty, &json("\"1e3\""), &[]).unwrap();
        assert_eq!(v.dec().unwrap().to_sci_string(), "1E+3");
        let v = validate(&mut e, &ty, &json("0.30000000000000004"), &[]).unwrap();
        assert_eq!(v.dec().unwrap().to_sci_string(), "0.30000000000000004");
        let v = validate(&mut e, &ty, &json("\" 1_000 \""), &[]).unwrap();
        assert_eq!(v.dec().unwrap().to_sci_string(), "1000");
        assert_eq!(errors_of(&ty, "true")[0].1, "decimal_type");
        assert_eq!(errors_of(&ty, "\"abc\"")[0].1, "decimal_parsing");
        assert_eq!(errors_of(&ty, "\"Infinity\"")[0].1, "finite_number");
        assert_eq!(errors_of(&ty, "0")[0].2, "Input should be greater than 0");
    }

    #[test]
    fn bools_literals_dates_lists_and_models() {
        assert_eq!(errors_of(&Ty::Bool, "2")[0].1, "bool_parsing");
        assert_eq!(errors_of(&Ty::Bool, "0.5")[0].1, "bool_type");
        assert_eq!(errors_of(&Ty::Bool, "\" true\"")[0].1, "bool_parsing");
        assert!(errors_of(&Ty::Bool, "\"YES\"").is_empty());
        assert_eq!(
            errors_of(&Ty::Lit(&["x", "y", "z"]), "\"q\"")[0].2,
            "Input should be 'x', 'y' or 'z'"
        );
        assert_eq!(
            errors_of(&Ty::Lit(&["NONE", "AIF"]), "5")[0].2,
            "Input should be 'NONE' or 'AIF'"
        );
        assert!(errors_of(&Ty::Date, "0").is_empty());
        assert_eq!(
            errors_of(&Ty::Date, "1700000000")[0].1,
            "date_from_datetime_inexact"
        );
        assert_eq!(errors_of(&Ty::Date, "true")[0].1, "date_type");
        // pydantic 2.13, probed in review: the watershed, the carry, the range, year 0.
        for (input, kind) in [
            ("1e400", "date_from_datetime_parsing"),
            ("-1e400", "date_from_datetime_parsing"),
            ("1e300", "date_from_datetime_parsing"),
            ("9223372036854775808", "date_from_datetime_parsing"),
            (
                "-1000000000000000000000000000000",
                "date_from_datetime_parsing",
            ),
            ("-9223372036854775808", "date_from_datetime_parsing"),
            ("253402300800000", "date_from_datetime_parsing"),
            ("-62167219201000", "date_from_datetime_parsing"),
            ("-62167219200000", "date_parsing"),
            ("253402300799999", "date_from_datetime_inexact"),
            ("86400.5", "date_from_datetime_inexact"),
            ("-86400.0000001", "date_from_datetime_inexact"),
            ("20000000000", "date_from_datetime_inexact"),
            ("1700006400001", "date_from_datetime_inexact"),
        ] {
            assert_eq!(errors_of(&Ty::Date, input)[0].1, kind, "{input}");
        }
        for ok in [
            "86399.9999999",
            "-0.0",
            "1700006400",
            "1700006400000",
            "-86400",
        ] {
            assert!(errors_of(&Ty::Date, ok).is_empty(), "{ok}");
        }
        let list = Ty::List(
            Box::new(Ty::Int {
                gt: None,
                ge: None,
                le: None,
            }),
            Some(2),
        );
        assert_eq!(
            errors_of(&list, "[]")[0].2,
            "List should have at least 2 items after validation, not 0"
        );
        // An item error suppresses the length check.
        assert_eq!(errors_of(&list, "[\"a\"]").len(), 1);
        let model = Ty::Model(Model {
            fields: vec![req("a", Ty::Str(Some(1), None))],
            forbid_extra: true,
        });
        let got = errors_of(&model, "{\"b\": 1}");
        assert_eq!(
            got[0],
            (
                "x.a".to_owned(),
                "missing".to_owned(),
                "Field required".to_owned()
            )
        );
        assert_eq!(got[1].1, "extra_forbidden");
        assert_eq!(errors_of(&model, "5")[0].1, "model_attributes_type");
    }
}
