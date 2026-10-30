//! The desk's pages: `Jinja2Templates(directory="app/templates")` on minijinja
//! (DECISIONS-RS R8.4).
//!
//! The templates are **read from the Python tree, unchanged** (`kite-momentum-rebalancer/
//! app/templates`): one copy, so a page edit lands in both desks. What makes minijinja print what
//! Jinja2 prints:
//!
//! * **Autoescape everything** (Starlette's `Jinja2Templates` turns it on for every template),
//!   with **markupsafe's** five escapes (`&<>"'` → `&amp; &lt; &gt; &#34; &#39;`) — minijinja's
//!   own also escapes `/`, which Jinja2 does not. [`escape_html`].
//! * **Python's `str()` for every printed value** ([`py_str_value`]): `None`, `True`/`False`,
//!   `1.0`, `1e-05`, `['a', 1]`, `{'k': 2}` — minijinja would print `none`, `true`, `1`, `[“a”…]`.
//! * **Python's formatting**: the `format` filter is `str % args` ([`crate::pyfmt::percent_format`]),
//!   and `'...'.format(...)` is `str.format` ([`crate::pyfmt::str_format`]) — both used over two
//!   hundred times in the desk's pages.
//! * **Jinja2's `tojson`**: `json.dumps(sort_keys=True)` then `<>&'` as `<>&'`.
//! * `fromjson` (the desk's own filter: `json.loads`), the `asset_v` and `options_enabled`
//!   globals, and Python's `dict.items()`/`.get()`/`str.startswith()` etc. (minijinja-contrib's
//!   pycompat, after the desk's own `format`).
//! * [`PyDate`]: a `date`/`datetime` with `.isoformat()`, `.strftime()` and Python's `str()`.
//!
//! Lanes add their own globals (the fno lane's `fno_badge`) through [`Templates::env_mut`]
//! when the app is built. `tests/templates.rs` compiles every desk template and renders probe
//! snippets against Jinja2's own output (`fixtures/template_probes.json`).

use std::fmt::Write as _;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime};
use minijinja::value::{Object, Value, ValueKind};
use minijinja::{AutoEscape, Environment, Error, ErrorKind, State};

use crate::pyfmt::{PyArg, percent_format, str_format};

/// markupsafe's `escape`: `& < > " '` and nothing else.
#[must_use]
pub fn escape_html(text: &str) -> String {
    let mut out = String::with_capacity(text.len() + 8);
    for c in text.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&#34;"),
            '\'' => out.push_str("&#39;"),
            c => out.push(c),
        }
    }
    out
}

fn py_repr_value(v: &Value) -> String {
    match v.kind() {
        ValueKind::String => baskfy_providers::py::repr_str(v.as_str().unwrap_or_default()),
        _ => py_str_value(v),
    }
}

/// Python's `str(value)` for a template value.
#[must_use]
pub fn py_str_value(v: &Value) -> String {
    match v.kind() {
        ValueKind::Undefined => String::new(),
        ValueKind::None => "None".to_owned(),
        ValueKind::Bool => if v.is_true() { "True" } else { "False" }.to_owned(),
        ValueKind::Number => {
            if v.is_integer() {
                v.to_string()
            } else {
                f64::try_from(v.clone()).map_or_else(|_| v.to_string(), baskfy_core::py::float_repr)
            }
        }
        ValueKind::String => v.as_str().unwrap_or_default().to_owned(),
        ValueKind::Seq | ValueKind::Iterable => {
            if let Some(obj) = v.as_object()
                && obj.downcast_ref::<PyDate>().is_some()
            {
                return v.to_string();
            }
            let items: Vec<String> = v
                .try_iter()
                .map(|it| it.map(|x| py_repr_value(&x)).collect())
                .unwrap_or_default();
            if v.is_tuple() {
                if items.len() == 1 {
                    format!("({},)", items.join(", "))
                } else {
                    format!("({})", items.join(", "))
                }
            } else {
                format!("[{}]", items.join(", "))
            }
        }
        ValueKind::Map => {
            let mut parts = Vec::new();
            if let Ok(keys) = v.try_iter() {
                for k in keys {
                    let val = v.get_item(&k).unwrap_or_default();
                    parts.push(format!("{}: {}", py_repr_value(&k), py_repr_value(&val)));
                }
            }
            format!("{{{}}}", parts.join(", "))
        }
        _ => v.to_string(),
    }
}

/// A template value as a [`PyArg`] (what the formatting functions take).
fn to_arg(v: &Value) -> PyArg {
    match v.kind() {
        ValueKind::None | ValueKind::Undefined => PyArg::None,
        ValueKind::Bool => PyArg::Bool(v.is_true()),
        ValueKind::Number if v.is_integer() => v
            .as_i64()
            .map_or_else(|| PyArg::Str(v.to_string()), PyArg::Int),
        ValueKind::Number => f64::try_from(v.clone()).map_or(PyArg::None, PyArg::Float),
        _ => PyArg::Str(py_str_value(v)),
    }
}

fn fmt_error(e: &crate::pyfmt::FmtError) -> Error {
    Error::new(ErrorKind::InvalidOperation, e.to_string())
}

/// Jinja2's `format` filter: `soft_str(value) % args`.
fn format_filter(value: &Value, args: &[Value]) -> Result<Value, Error> {
    let fmt = py_str_value(value);
    let args: Vec<PyArg> = args.iter().map(to_arg).collect();
    percent_format(&fmt, &args)
        .map(Value::from)
        .map_err(|e| fmt_error(&e))
}

fn py_json(v: &Value, sort_keys: bool) -> Result<String, Error> {
    Ok(match v.kind() {
        ValueKind::Undefined | ValueKind::None => "null".to_owned(),
        ValueKind::Bool => if v.is_true() { "true" } else { "false" }.to_owned(),
        ValueKind::Number => {
            if v.is_integer() {
                v.to_string()
            } else {
                let f = f64::try_from(v.clone()).unwrap_or(f64::NAN);
                if f.is_nan() {
                    "NaN".to_owned()
                } else if f.is_infinite() {
                    if f > 0.0 { "Infinity" } else { "-Infinity" }.to_owned()
                } else {
                    baskfy_core::py::float_repr(f)
                }
            }
        }
        ValueKind::String => baskfy_providers::py::json_str(v.as_str().unwrap_or_default()),
        ValueKind::Seq | ValueKind::Iterable => {
            let items: Result<Vec<String>, Error> =
                v.try_iter()?.map(|x| py_json(&x, sort_keys)).collect();
            format!("[{}]", items?.join(", "))
        }
        ValueKind::Map => {
            let mut keys: Vec<Value> = v.try_iter()?.collect();
            if sort_keys {
                keys.sort_by_key(py_str_value);
            }
            let mut parts = Vec::with_capacity(keys.len());
            for k in keys {
                let val = v.get_item(&k)?;
                parts.push(format!(
                    "{}: {}",
                    baskfy_providers::py::json_str(&py_str_value(&k)),
                    py_json(&val, sort_keys)?
                ));
            }
            format!("{{{}}}", parts.join(", "))
        }
        _ => baskfy_providers::py::json_str(&py_str_value(v)),
    })
}

/// Jinja2's `tojson`: `htmlsafe_json_dumps(value, sort_keys=True)`.
fn tojson_filter(value: &Value) -> Result<Value, Error> {
    let text = py_json(value, true)?
        .replace('<', "\\u003c")
        .replace('>', "\\u003e")
        .replace('&', "\\u0026")
        .replace('\'', "\\u0027");
    Ok(Value::from_safe_string(text))
}

/// The desk's `fromjson` filter (`json.loads`).
fn fromjson_filter(value: &str) -> Result<Value, Error> {
    let parsed: serde_json::Value = serde_json::from_str(value)
        .map_err(|e| Error::new(ErrorKind::InvalidOperation, format!("JSONDecodeError: {e}")))?;
    Ok(Value::from(minijinja::value::Serde(parsed)))
}

fn escape_filter(value: &Value) -> Value {
    if value.is_safe() {
        return value.clone();
    }
    Value::from_safe_string(escape_html(&py_str_value(value)))
}

/// `date` / `datetime` as templates see them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PyDate {
    /// `datetime.date`.
    Date(NaiveDate),
    /// A naive `datetime.datetime`.
    Naive(NaiveDateTime),
    /// An aware `datetime.datetime`.
    Aware(DateTime<FixedOffset>),
}

impl PyDate {
    /// `.isoformat()` (`T` separator for a datetime).
    #[must_use]
    pub fn isoformat(&self, sep: char) -> String {
        let micros = |n: u32| n / 1000;
        match self {
            Self::Date(d) => d.format("%Y-%m-%d").to_string(),
            Self::Naive(t) => {
                let base = t.format(&format!("%Y-%m-%d{sep}%H:%M:%S")).to_string();
                let us = micros(t.and_utc().timestamp_subsec_nanos());
                if us == 0 {
                    base
                } else {
                    format!("{base}.{us:06}")
                }
            }
            Self::Aware(t) => {
                let base = t.format(&format!("%Y-%m-%d{sep}%H:%M:%S")).to_string();
                let us = micros(t.timestamp_subsec_nanos());
                let frac = if us == 0 {
                    String::new()
                } else {
                    format!(".{us:06}")
                };
                format!("{base}{frac}{}", t.format("%:z"))
            }
        }
    }

    fn strftime(&self, fmt: &str) -> String {
        let mut out = String::new();
        let _ = match self {
            Self::Date(d) => write!(out, "{}", d.format(fmt)),
            Self::Naive(t) => write!(out, "{}", t.format(fmt)),
            Self::Aware(t) => write!(out, "{}", t.format(fmt)),
        };
        out
    }
}

impl Object for PyDate {
    fn get_value(self: &Arc<Self>, key: &Value) -> Option<Value> {
        use chrono::{Datelike, Timelike};
        let (date, time) = match self.as_ref() {
            Self::Date(d) => (*d, None),
            Self::Naive(t) => (t.date(), Some(t.time())),
            Self::Aware(t) => (t.date_naive(), Some(t.time())),
        };
        Some(match key.as_str()? {
            "year" => Value::from(date.year()),
            "month" => Value::from(date.month()),
            "day" => Value::from(date.day()),
            "hour" => Value::from(time?.hour()),
            "minute" => Value::from(time?.minute()),
            "second" => Value::from(time?.second()),
            _ => return None,
        })
    }

    fn call_method(
        self: &Arc<Self>,
        _state: &mut State<'_, '_>,
        method: &str,
        args: &[Value],
    ) -> Result<Value, Error> {
        match method {
            "isoformat" => {
                let sep = args
                    .first()
                    .and_then(|v| v.as_str())
                    .and_then(|s| s.chars().next())
                    .unwrap_or('T');
                Ok(Value::from(self.isoformat(sep)))
            }
            "strftime" => {
                let fmt = args.first().and_then(|v| v.as_str()).unwrap_or_default();
                Ok(Value::from(self.strftime(fmt)))
            }
            "date" => match self.as_ref() {
                Self::Naive(t) => Ok(Value::from_object(Self::Date(t.date()))),
                Self::Aware(t) => Ok(Value::from_object(Self::Date(t.date_naive()))),
                Self::Date(_) => Err(Error::from(ErrorKind::UnknownMethod)),
            },
            "weekday" => {
                use chrono::Datelike;
                let d = match self.as_ref() {
                    Self::Date(d) => *d,
                    Self::Naive(t) => t.date(),
                    Self::Aware(t) => t.date_naive(),
                };
                Ok(Value::from(d.weekday().num_days_from_monday()))
            }
            _ => Err(Error::from(ErrorKind::UnknownMethod)),
        }
    }

    fn render(self: &Arc<Self>, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // str(date) is isoformat(); str(datetime) uses a space.
        f.write_str(&self.isoformat(' '))
    }
}

fn unknown_method(
    state: &mut State<'_, '_>,
    value: &Value,
    method: &str,
    args: &[Value],
) -> Result<Value, Error> {
    if method == "format" && value.kind() == ValueKind::String {
        let template = value.as_str().unwrap_or_default();
        let args: Vec<PyArg> = args.iter().map(to_arg).collect();
        return str_format(template, &args)
            .map(Value::from)
            .map_err(|e| fmt_error(&e));
    }
    minijinja_contrib::pycompat::unknown_method_callback(state, value, method, args)
}

fn formatter(
    out: &mut minijinja::Output<'_>,
    state: &mut State<'_, '_>,
    value: &Value,
) -> Result<(), Error> {
    let text = if let Some(obj) = value.as_object()
        && obj.downcast_ref::<PyDate>().is_some()
    {
        value.to_string()
    } else {
        py_str_value(value)
    };
    let escape = !value.is_safe() && !matches!(state.auto_escape(), AutoEscape::None);
    let written = if escape {
        out.write_str(&escape_html(&text))
    } else {
        out.write_str(&text)
    };
    written.map_err(|_| Error::new(ErrorKind::WriteFailure, "could not write output"))
}

/// A desk Python value as a template value: dicts keep their order, `date`/`datetime` become
/// [`PyDate`], `bytes` print as Python would not need to (they never reach a page).
#[must_use]
pub fn py_value(v: &crate::wire::PyValue) -> Value {
    use baskfy_providers::records::PyDateTime;

    use crate::wire::PyValue;
    match v {
        PyValue::None => Value::from(()),
        PyValue::Bool(b) => Value::from(*b),
        PyValue::Int(i) => Value::from(*i),
        PyValue::Float(f) => Value::from(*f),
        PyValue::Str(s) => Value::from(s.clone()),
        PyValue::Bytes(b) => Value::from_bytes(b.clone()),
        PyValue::Date(d) => Value::from_object(PyDate::Date(*d)),
        PyValue::DateTime(PyDateTime::Naive(t)) => Value::from_object(PyDate::Naive(*t)),
        PyValue::DateTime(PyDateTime::Aware(t)) => Value::from_object(PyDate::Aware(*t)),
        PyValue::List(items) => Value::from(items.iter().map(py_value).collect::<Vec<_>>()),
        PyValue::Map(map) => {
            Value::from_pairs(map.iter().map(|(k, v)| (k.to_owned(), py_value(v))))
        }
    }
}

/// The desk's template environment.
#[derive(Debug)]
pub struct Templates {
    env: Environment<'static>,
    dir: PathBuf,
}

/// `ASSET_V`: the first 8 hex digits of `md5(app/static/app.css)`, or `"dev"` when unreadable.
#[must_use]
pub fn asset_version(static_dir: &Path) -> String {
    use md5::{Digest, Md5};
    std::fs::read(static_dir.join("app.css")).map_or_else(
        |_| "dev".to_owned(),
        |bytes| {
            let digest = Md5::digest(&bytes);
            digest.iter().take(4).fold(String::new(), |mut s, b| {
                let _ = write!(s, "{b:02x}");
                s
            })
        },
    )
}

impl Templates {
    /// Load `dir` (the desk's `app/templates`) with the Jinja2-compatible setup above.
    #[must_use]
    pub fn new(dir: &Path, asset_v: &str, options_enabled: bool) -> Self {
        let mut env = Environment::new();
        let owned: PathBuf = dir.into();
        env.set_loader(minijinja::path_loader(owned));
        env.set_auto_escape_callback(|_| AutoEscape::Html);
        env.set_formatter(formatter);
        env.set_unknown_method_callback(unknown_method);
        env.add_filter(
            "format",
            |value: Value, args: minijinja::value::Rest<Value>| format_filter(&value, &args),
        );
        env.add_filter("tojson", |value: Value| tojson_filter(&value));
        env.add_filter("fromjson", |value: String| fromjson_filter(&value));
        env.add_filter("e", |value: Value| escape_filter(&value));
        env.add_filter("escape", |value: Value| escape_filter(&value));
        env.add_global("asset_v", Value::from(asset_v.to_owned()));
        env.add_global("options_enabled", Value::from(options_enabled));
        Self {
            env,
            dir: dir.to_path_buf(),
        }
    }

    /// The directory templates load from.
    #[must_use]
    pub fn dir(&self) -> &Path {
        &self.dir
    }

    /// For lanes registering their own globals (`fno_badge`) while the app is built.
    pub fn env_mut(&mut self) -> &mut Environment<'static> {
        &mut self.env
    }

    /// `templates.TemplateResponse(request, name, context)`'s body.
    ///
    /// # Errors
    /// A missing template or a render error.
    pub fn render(&self, name: &str, context: Value) -> Result<String, Error> {
        self.env.get_template(name)?.render(context)
    }

    /// Load and compile `name` (and what it extends) without rendering.
    ///
    /// # Errors
    /// A missing template or a syntax error.
    pub fn check(&self, name: &str) -> Result<(), Error> {
        self.env.get_template(name).map(|_| ())
    }

    /// Render a template source string with the same environment (tests and probes).
    ///
    /// # Errors
    /// A syntax or render error.
    pub fn render_str(&self, source: &str, context: Value) -> Result<String, Error> {
        self.env.render_str(source, context)
    }
}
