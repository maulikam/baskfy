//! Python's two string-formatting languages, for the desk's templates (DECISIONS-RS R8.4).
//!
//! The desk's 18 templates format numbers 133 times with Jinja's `format` filter
//! (`'%.1f'|format(x)` = Python's `'%.1f' % (x,)`) and 80 times with `str.format`
//! (`'{:,.0f}'.format(x)`). Neither exists in minijinja, and the output must be Python's to the
//! byte, so both are implemented here for the types templates pass (int, float, bool, None,
//! str): `%`-conversions `d i u f F e E g G s r x X o %` with flags `-+ 0#`, width and
//! precision; and the format-spec mini-language `[[fill]align][sign][#][0][width][,|_][.prec]
//! [type]` with types `d f F e E g G % s` and none. Rounding is Python's (correctly rounded,
//! ties to even on the exact binary value), which Rust's fixed formatting shares.
//!
//! Unsupported pieces (`*` widths, `%c`, mapping keys `%(name)s`, `n`, `x`/`b`/`o`/`c` in a
//! format spec, `=` alignment with a fill) return a [`FmtError`] naming them, so a lane that
//! needs one adds it rather than getting a guess.

use std::fmt::Write as _;

/// One argument, by its Python type.
#[derive(Debug, Clone, PartialEq)]
pub enum PyArg {
    /// `None`.
    None,
    /// `bool`.
    Bool(bool),
    /// `int`.
    Int(i64),
    /// `float`.
    Float(f64),
    /// `str`.
    Str(String),
}

/// What Python would raise (`TypeError`/`ValueError`), or an unported feature.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{python_type}: {message}")]
pub struct FmtError {
    /// `TypeError`, `ValueError`, or `NotImplemented` for an unported feature.
    pub python_type: &'static str,
    /// The message.
    pub message: String,
}

fn type_error(message: impl Into<String>) -> FmtError {
    FmtError {
        python_type: "TypeError",
        message: message.into(),
    }
}

fn value_error(message: impl Into<String>) -> FmtError {
    FmtError {
        python_type: "ValueError",
        message: message.into(),
    }
}

fn unported(what: &str) -> FmtError {
    FmtError {
        python_type: "NotImplemented",
        message: format!("{what} is not ported (pyfmt.rs)"),
    }
}

/// `str(value)`.
#[must_use]
pub fn py_str(arg: &PyArg) -> String {
    match arg {
        PyArg::None => "None".to_owned(),
        PyArg::Bool(true) => "True".to_owned(),
        PyArg::Bool(false) => "False".to_owned(),
        PyArg::Int(i) => i.to_string(),
        PyArg::Float(f) => baskfy_core::py::float_repr(*f),
        PyArg::Str(s) => s.clone(),
    }
}

/// `repr(value)`.
#[must_use]
pub fn py_repr(arg: &PyArg) -> String {
    match arg {
        PyArg::Str(s) => baskfy_providers::py::repr_str(s),
        other => py_str(other),
    }
}

fn type_name(arg: &PyArg) -> &'static str {
    match arg {
        PyArg::None => "NoneType",
        PyArg::Bool(_) => "bool",
        PyArg::Int(_) => "int",
        PyArg::Float(_) => "float",
        PyArg::Str(_) => "str",
    }
}

/// `float(arg)` for `%f`-style conversions: ints and bools convert, the rest raise.
fn as_float(arg: &PyArg) -> Result<f64, FmtError> {
    #[allow(clippy::cast_precision_loss)]
    match arg {
        PyArg::Float(f) => Ok(*f),
        PyArg::Int(i) => Ok(*i as f64),
        PyArg::Bool(b) => Ok(if *b { 1.0 } else { 0.0 }),
        other => Err(type_error(format!(
            "must be real number, not {}",
            type_name(other)
        ))),
    }
}

/// `int(arg)` for `%d`: floats truncate, bools are 0/1, the rest raise.
fn as_int_text(arg: &PyArg) -> Result<String, FmtError> {
    match arg {
        PyArg::Int(i) => Ok(i.to_string()),
        PyArg::Bool(b) => Ok(if *b { "1" } else { "0" }.to_owned()),
        PyArg::Float(f) => {
            if f.is_nan() {
                return Err(value_error("cannot convert float NaN to integer"));
            }
            if f.is_infinite() {
                return Err(FmtError {
                    python_type: "OverflowError",
                    message: "cannot convert float infinity to integer".to_owned(),
                });
            }
            let t = f.trunc();
            Ok(if t == 0.0 {
                "0".to_owned()
            } else {
                format!("{t:.0}")
            })
        }
        other => Err(type_error(format!(
            "%d format: a real number is required, not {}",
            type_name(other)
        ))),
    }
}

/// Python's `'%.<p>e'`: mantissa with `p` digits, exponent signed and at least two digits.
fn exp_format(x: f64, precision: usize, upper: bool, alternate: bool) -> String {
    let raw = format!("{x:.precision$e}");
    let (mantissa, exp) = raw.split_once('e').unwrap_or((raw.as_str(), "0"));
    let exp: i32 = exp.parse().unwrap_or(0);
    let mut mantissa = mantissa.to_owned();
    if alternate && precision == 0 {
        mantissa.push('.');
    }
    let e = if upper { 'E' } else { 'e' };
    let sign = if exp < 0 { '-' } else { '+' };
    format!("{mantissa}{e}{sign}{:02}", exp.unsigned_abs())
}

/// Python's `'%.<p>g'` / `format(x, '.<p>g')` (no `#`): significant digits, trailing zeros
/// stripped, scientific when the exponent is `< -4` or `>= p`.
fn general_format(x: f64, precision: usize, upper: bool, alternate: bool) -> String {
    general_format_with(x, precision, upper, alternate, false)
}

/// `'g'`, or (with `add_dot_0`) the empty type with a precision: CPython switches to the
/// exponent one digit earlier and puts `.0` on an integral fixed result.
fn general_format_with(
    x: f64,
    precision: usize,
    upper: bool,
    alternate: bool,
    add_dot_0: bool,
) -> String {
    let p = precision.max(1);
    if x == 0.0 {
        let sign = if x.is_sign_negative() { "-" } else { "" };
        if alternate {
            return format!("{sign}0.{}", "0".repeat(p - 1));
        }
        if add_dot_0 {
            return if p == 1 {
                format!("{sign}0{}+00", if upper { 'E' } else { 'e' })
            } else {
                format!("{sign}0.0")
            };
        }
        return format!("{sign}0");
    }
    let sci = format!("{x:.prec$e}", prec = p - 1);
    let exp: i32 = sci
        .split_once('e')
        .and_then(|(_, e)| e.parse().ok())
        .unwrap_or(0);
    let p_i = i32::try_from(p).unwrap_or(i32::MAX);
    let strip = |s: String| -> String {
        if alternate || !s.contains('.') {
            return s;
        }
        let t = s.trim_end_matches('0');
        t.trim_end_matches('.').to_owned()
    };
    let limit = if add_dot_0 { p_i - 1 } else { p_i };
    if exp < -4 || exp >= limit {
        let body = exp_format(x, p - 1, upper, alternate);
        let (m, e) = body.split_at(body.find(['e', 'E']).unwrap_or(body.len()));
        format!("{}{e}", strip(m.to_owned()))
    } else {
        let decimals = usize::try_from(p_i - 1 - exp).unwrap_or(0);
        let fixed = strip(format!("{x:.decimals$}"));
        if add_dot_0 && !fixed.contains('.') {
            format!("{fixed}.0")
        } else {
            fixed
        }
    }
}

fn special_float(x: f64, upper: bool) -> Option<String> {
    if x.is_nan() {
        return Some(if upper { "NAN" } else { "nan" }.to_owned());
    }
    if x.is_infinite() {
        let body = if upper { "INF" } else { "inf" };
        return Some(if x < 0.0 {
            format!("-{body}")
        } else {
            body.to_owned()
        });
    }
    None
}

#[derive(Default)]
struct Flags {
    left: bool,
    plus: bool,
    space: bool,
    zero: bool,
    alternate: bool,
}

fn pad(body: String, width: usize, flags: &Flags, numeric: bool) -> String {
    let len = body.chars().count();
    if len >= width {
        return body;
    }
    let fill = width - len;
    if flags.left {
        return format!("{body}{}", " ".repeat(fill));
    }
    if flags.zero && numeric {
        let (sign, digits) = match body.chars().next() {
            Some(c @ ('-' | '+' | ' ')) => (c.to_string(), body[1..].to_owned()),
            _ => (String::new(), body),
        };
        return format!("{sign}{}{digits}", "0".repeat(fill));
    }
    format!("{}{body}", " ".repeat(fill))
}

fn signed(body: String, negative: bool, flags: &Flags) -> String {
    if negative {
        body
    } else if flags.plus {
        format!("+{body}")
    } else if flags.space {
        format!(" {body}")
    } else {
        body
    }
}

/// Python's `fmt % args` (`args` is the tuple; a single value is a one-tuple).
///
/// # Errors
/// What Python raises: too few/many arguments, a wrong type, an unsupported conversion.
pub fn percent_format(fmt: &str, args: &[PyArg]) -> Result<String, FmtError> {
    let chars: Vec<char> = fmt.chars().collect();
    let mut out = String::with_capacity(fmt.len() + 8);
    let mut next = args.iter();
    let mut i = 0;
    while let Some(&c) = chars.get(i) {
        i += 1;
        if c != '%' {
            out.push(c);
            continue;
        }
        let mut flags = Flags::default();
        while let Some(&f) = chars.get(i) {
            match f {
                '-' => flags.left = true,
                '+' => flags.plus = true,
                ' ' => flags.space = true,
                '0' => flags.zero = true,
                '#' => flags.alternate = true,
                '(' => return Err(unported("%(name) mapping keys")),
                _ => break,
            }
            i += 1;
        }
        let mut width = 0usize;
        while let Some(d) = chars.get(i).and_then(|c| c.to_digit(10)) {
            width = width * 10 + d as usize;
            i += 1;
        }
        if chars.get(i) == Some(&'*') {
            return Err(unported("'*' width"));
        }
        let mut precision: Option<usize> = None;
        if chars.get(i) == Some(&'.') {
            i += 1;
            let mut p = 0usize;
            while let Some(d) = chars.get(i).and_then(|c| c.to_digit(10)) {
                p = p * 10 + d as usize;
                i += 1;
            }
            precision = Some(p);
        }
        // Length modifiers are accepted and ignored, as Python does.
        while matches!(chars.get(i), Some('h' | 'l' | 'L')) {
            i += 1;
        }
        let Some(&conv) = chars.get(i) else {
            return Err(value_error("incomplete format"));
        };
        i += 1;
        if conv == '%' {
            out.push('%');
            continue;
        }
        let Some(arg) = next.next() else {
            return Err(type_error("not enough arguments for format string"));
        };
        let piece = match conv {
            's' | 'r' | 'a' => {
                let mut s = if conv == 's' {
                    py_str(arg)
                } else {
                    py_repr(arg)
                };
                if let Some(p) = precision {
                    s = s.chars().take(p).collect();
                }
                pad(s, width, &flags, false)
            }
            'd' | 'i' | 'u' => {
                let text = as_int_text(arg)?;
                let negative = text.starts_with('-');
                let mut digits = text.trim_start_matches('-').to_owned();
                if let Some(p) = precision
                    && digits.len() < p
                {
                    digits = format!("{}{digits}", "0".repeat(p - digits.len()));
                }
                let body = if negative {
                    format!("-{digits}")
                } else {
                    digits
                };
                pad(signed(body, negative, &flags), width, &flags, true)
            }
            'x' | 'X' | 'o' => {
                let v: i64 = match arg {
                    PyArg::Int(v) => *v,
                    PyArg::Bool(b) => i64::from(*b),
                    _ => {
                        return Err(type_error(format!(
                            "%{conv} format: an integer is required, not {}",
                            type_name(arg)
                        )));
                    }
                };
                let magnitude = v.unsigned_abs();
                let mut digits = match conv {
                    'x' => format!("{magnitude:x}"),
                    'X' => format!("{magnitude:X}"),
                    _ => format!("{magnitude:o}"),
                };
                if flags.alternate {
                    digits = format!(
                        "{}{digits}",
                        match conv {
                            'x' => "0x",
                            'X' => "0X",
                            _ => "0o",
                        }
                    );
                }
                let body = if v < 0 { format!("-{digits}") } else { digits };
                pad(signed(body, v < 0, &flags), width, &flags, true)
            }
            'f' | 'F' | 'e' | 'E' | 'g' | 'G' => {
                let x = as_float(arg)?;
                let upper = conv.is_ascii_uppercase();
                let p = precision.unwrap_or(6);
                let body = special_float(x, upper).unwrap_or_else(|| match conv {
                    'f' | 'F' => {
                        let mut s = format!("{x:.p$}");
                        if flags.alternate && p == 0 {
                            s.push('.');
                        }
                        s
                    }
                    'e' | 'E' => exp_format(x, p, upper, flags.alternate),
                    _ => general_format(x, p, upper, flags.alternate),
                });
                let negative = body.starts_with('-');
                let numeric = !(x.is_nan() || x.is_infinite());
                pad(signed(body, negative, &flags), width, &flags, numeric)
            }
            'c' => return Err(unported("%c")),
            other => {
                return Err(value_error(format!(
                    "unsupported format character '{other}' (0x{:x}) at index {}",
                    other as u32,
                    i - 1
                )));
            }
        };
        out.push_str(&piece);
    }
    if next.next().is_some() {
        return Err(type_error(
            "not all arguments converted during string formatting",
        ));
    }
    Ok(out)
}

/// A parsed format spec.
#[derive(Debug, Default)]
struct Spec {
    fill: Option<char>,
    align: Option<char>,
    sign: Option<char>,
    alternate: bool,
    zero: bool,
    width: usize,
    grouping: Option<char>,
    precision: Option<usize>,
    kind: Option<char>,
}

fn parse_spec(spec: &str) -> Result<Spec, FmtError> {
    let chars: Vec<char> = spec.chars().collect();
    let mut s = Spec::default();
    let mut i = 0;
    let is_align = |c: char| matches!(c, '<' | '>' | '^' | '=');
    if chars.len() >= 2 && chars.get(1).copied().is_some_and(is_align) {
        s.fill = chars.first().copied();
        s.align = chars.get(1).copied();
        i = 2;
    } else if chars.first().copied().is_some_and(is_align) {
        s.align = chars.first().copied();
        i = 1;
    }
    if let Some(&c @ ('+' | '-' | ' ')) = chars.get(i) {
        s.sign = Some(c);
        i += 1;
    }
    if chars.get(i) == Some(&'z') {
        return Err(unported("'z' in a format spec"));
    }
    if chars.get(i) == Some(&'#') {
        s.alternate = true;
        i += 1;
    }
    if chars.get(i) == Some(&'0') {
        s.zero = true;
        i += 1;
    }
    while let Some(d) = chars.get(i).and_then(|c| c.to_digit(10)) {
        s.width = s.width * 10 + d as usize;
        i += 1;
    }
    if let Some(&c @ (',' | '_')) = chars.get(i) {
        s.grouping = Some(c);
        i += 1;
    }
    if chars.get(i) == Some(&'.') {
        i += 1;
        let mut p = 0usize;
        let start = i;
        while let Some(d) = chars.get(i).and_then(|c| c.to_digit(10)) {
            p = p * 10 + d as usize;
            i += 1;
        }
        if i == start {
            return Err(value_error("Format specifier missing precision"));
        }
        s.precision = Some(p);
    }
    if let Some(&c) = chars.get(i) {
        s.kind = Some(c);
        i += 1;
    }
    if i != chars.len() {
        return Err(value_error("Invalid format specifier"));
    }
    Ok(s)
}

/// Insert `sep` every three digits of the integer part of a plain number (`"-1234.5"`).
fn group(number: &str, sep: char) -> String {
    let (sign, rest) = number.strip_prefix('-').map_or(("", number), |r| ("-", r));
    let split = rest
        .find(|c: char| !c.is_ascii_digit())
        .unwrap_or(rest.len());
    let (int, frac) = rest.split_at(split);
    let mut grouped = String::with_capacity(int.len() + int.len() / 3);
    for (n, ch) in int.chars().enumerate() {
        if n > 0 && (int.len() - n) % 3 == 0 {
            grouped.push(sep);
        }
        grouped.push(ch);
    }
    format!("{sign}{grouped}{frac}")
}

fn apply_width(body: String, negative: bool, spec: &Spec, numeric: bool) -> String {
    let sign = if negative {
        "-"
    } else {
        match spec.sign {
            Some('+') => "+",
            Some(' ') => " ",
            _ => "",
        }
    };
    let digits = body.strip_prefix('-').unwrap_or(&body).to_owned();
    let len = sign.chars().count() + digits.chars().count();
    if len >= spec.width {
        return format!("{sign}{digits}");
    }
    let fill_n = spec.width - len;
    let zero = spec.zero && spec.align.is_none() && numeric;
    let align = spec.align.unwrap_or(if numeric { '>' } else { '<' });
    let fill = if zero { '0' } else { spec.fill.unwrap_or(' ') };
    let filler = |n: usize| fill.to_string().repeat(n);
    if zero || align == '=' {
        return format!("{sign}{}{digits}", filler(fill_n));
    }
    match align {
        '<' => format!("{sign}{digits}{}", filler(fill_n)),
        '^' => {
            let left = fill_n / 2;
            format!("{}{sign}{digits}{}", filler(left), filler(fill_n - left))
        }
        _ => format!("{}{sign}{digits}", filler(fill_n)),
    }
}

/// Python's `format(value, spec)` — what `'{:<spec>}'.format(value)` calls.
///
/// # Errors
/// Python's `ValueError` for a spec the type refuses, or an unported feature.
pub fn format_value(value: &PyArg, spec: &str) -> Result<String, FmtError> {
    let s = parse_spec(spec)?;
    match value {
        PyArg::Str(text) => {
            if s.kind.is_some_and(|k| k != 's') || s.sign.is_some() || s.grouping.is_some() {
                return Err(value_error(format!(
                    "Unknown format code '{}' for object of type 'str'",
                    s.kind.unwrap_or('s')
                )));
            }
            let t: String = match s.precision {
                Some(p) => text.chars().take(p).collect(),
                None => text.clone(),
            };
            Ok(apply_width(t, false, &s, false))
        }
        PyArg::None => {
            if spec.is_empty() {
                Ok("None".to_owned())
            } else {
                Err(type_error(
                    "unsupported format string passed to NoneType.__format__",
                ))
            }
        }
        PyArg::Bool(b) if s.kind.is_none() && spec.is_empty() => {
            Ok(if *b { "True" } else { "False" }.to_owned())
        }
        PyArg::Int(_) | PyArg::Bool(_) => {
            let v = match value {
                PyArg::Int(i) => *i,
                PyArg::Bool(b) => i64::from(*b),
                _ => 0,
            };
            match s.kind {
                None | Some('d') => {
                    if s.precision.is_some() {
                        return Err(value_error(
                            "Precision not allowed in integer format specifier",
                        ));
                    }
                    let mut body = v.unsigned_abs().to_string();
                    if let Some(sep) = s.grouping {
                        body = group(&body, sep);
                    }
                    Ok(apply_width(body, v < 0, &s, true))
                }
                #[allow(clippy::cast_precision_loss)]
                Some('f' | 'F' | 'e' | 'E' | 'g' | 'G' | '%') => {
                    format_value(&PyArg::Float(v as f64), spec)
                }
                Some(k) => Err(unported(&format!("format code '{k}' for int"))),
            }
        }
        PyArg::Float(x) => {
            let x = *x;
            let kind = s.kind;
            let upper = kind.is_some_and(|k| k.is_ascii_uppercase());
            if let Some(special) = special_float(x, upper) {
                let negative = special.starts_with('-');
                return Ok(apply_width(
                    special.trim_start_matches('-').to_owned(),
                    negative,
                    &s,
                    true,
                ));
            }
            let negative = x.is_sign_negative();
            let magnitude = x.abs();
            let mut body = match kind {
                Some('f' | 'F') => format!("{magnitude:.p$}", p = s.precision.unwrap_or(6)),
                Some('e' | 'E') => {
                    exp_format(magnitude, s.precision.unwrap_or(6), upper, s.alternate)
                }
                Some('g' | 'G') => {
                    general_format(magnitude, s.precision.unwrap_or(6), upper, s.alternate)
                }
                Some('%') => format!("{:.p$}%", magnitude * 100.0, p = s.precision.unwrap_or(6)),
                None => match s.precision {
                    None => baskfy_core::py::float_repr(magnitude),
                    Some(p) => general_format_with(magnitude, p, false, s.alternate, true),
                },
                Some('d') => {
                    return Err(value_error(
                        "Unknown format code 'd' for object of type 'float'",
                    ));
                }
                Some(k) => return Err(unported(&format!("format code '{k}' for float"))),
            };
            if let Some(sep) = s.grouping {
                let (num, tail) = body
                    .find(['e', 'E', '%'])
                    .map_or((body.as_str(), ""), |at| body.split_at(at));
                body = format!("{}{tail}", group(num, sep));
            }
            Ok(apply_width(body, negative, &s, true))
        }
    }
}

/// Python's `template.format(*args)` for the auto-numbered and indexed fields templates use
/// (`'{}'`, `'{0}'`, `'{:,.0f}'`, `'{0:.2f}'`), with `{{`/`}}` escapes. Attribute/index access
/// inside a field (`{0.x}`, `{0[1]}`) and conversions (`!r`) are unported.
///
/// # Errors
/// Python's `IndexError`/`ValueError`, or an unported feature.
pub fn str_format(template: &str, args: &[PyArg]) -> Result<String, FmtError> {
    let chars: Vec<char> = template.chars().collect();
    let mut out = String::with_capacity(template.len() + 8);
    let mut auto = 0usize;
    let mut i = 0;
    while let Some(&c) = chars.get(i) {
        match c {
            '{' if chars.get(i + 1) == Some(&'{') => {
                out.push('{');
                i += 2;
            }
            '}' if chars.get(i + 1) == Some(&'}') => {
                out.push('}');
                i += 2;
            }
            '}' => return Err(value_error("Single '}' encountered in format string")),
            '{' => {
                let Some(end) = chars[i + 1..].iter().position(|&c| c == '}') else {
                    return Err(value_error("expected '}' before end of string"));
                };
                let field: String = chars[i + 1..i + 1 + end].iter().collect();
                i += end + 2;
                let (name, spec) = field.split_once(':').unwrap_or((field.as_str(), ""));
                if name.contains(['.', '[', '!']) {
                    return Err(unported("attribute, index or conversion in a format field"));
                }
                let index = if name.is_empty() {
                    let n = auto;
                    auto += 1;
                    n
                } else {
                    name.parse::<usize>()
                        .map_err(|_| unported("a named format field"))?
                };
                let Some(arg) = args.get(index) else {
                    return Err(FmtError {
                        python_type: "IndexError",
                        message: format!(
                            "Replacement index {index} out of range for positional args tuple"
                        ),
                    });
                };
                let _ = write!(out, "{}", format_value(arg, spec)?);
            }
            other => {
                out.push(other);
                i += 1;
            }
        }
    }
    Ok(out)
}
