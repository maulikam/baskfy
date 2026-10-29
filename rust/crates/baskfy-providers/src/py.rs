//! Python's number and string formatting, where a provider message or a Redis argument depends
//! on it. No Python twin: these are the format specs `baskfy_providers` writes inline
//! (`f"{x:.2f}"`, `f"{x:g}"`, `f"{key!r}"`) and the `%.14g` Lua uses when Redis stores a number.

/// `f"{x:.{precision}f}"` — Python's fixed-point format (correctly rounded, half to even on an
/// exact binary tie, which is also what Rust's `{:.N}` does), with Python's `nan`/`inf` spelling.
#[must_use]
pub fn fixed(x: f64, precision: usize) -> String {
    if x.is_nan() {
        return "nan".to_owned();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf" } else { "-inf" }.to_owned();
    }
    format!("{x:.precision$}")
}

/// `f"{x:g}"` — six significant digits, trailing zeros stripped (Python's general format).
#[must_use]
pub fn general(x: f64) -> String {
    c_general(x, 6)
}

/// Lua 5.1's `tostring(number)` and Redis's number-to-argument conversion: C's `"%.14g"`.
///
/// Redis stores a Lua number passed to `redis.call` in this form, so a value written by the
/// rate-limit scripts and read back is rounded to 14 significant digits. The in-memory script
/// backend reproduces that, so its arithmetic is the server's.
#[must_use]
pub fn lua_number(x: f64) -> String {
    c_general(x, 14)
}

/// C's / Python's `%.{p}g`: scientific when the exponent is below -4 or at least `p`.
fn c_general(x: f64, precision: usize) -> String {
    if x.is_nan() {
        return "nan".to_owned();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf" } else { "-inf" }.to_owned();
    }
    let p = precision.max(1);
    // `{:.N e}` is correctly rounded; its exponent is the one %g decides on.
    let sci = format!("{:.*e}", p - 1, x);
    let Some((mantissa, exp_text)) = sci.split_once('e') else {
        return sci;
    };
    let exponent: i64 = exp_text.parse().unwrap_or(0);
    let p_i = i64::try_from(p).unwrap_or(i64::MAX);
    if exponent < -4 || exponent >= p_i {
        let m = strip_fraction_zeros(mantissa);
        let sign = if exponent < 0 { '-' } else { '+' };
        let magnitude = exponent.unsigned_abs();
        return format!("{m}e{sign}{magnitude:02}");
    }
    let decimals = usize::try_from(p_i - 1 - exponent).unwrap_or(0);
    strip_fraction_zeros(&format!("{x:.decimals$}"))
}

fn strip_fraction_zeros(text: &str) -> String {
    if !text.contains('.') {
        return text.to_owned();
    }
    text.trim_end_matches('0').trim_end_matches('.').to_owned()
}

/// `repr(s)` for a Python `str`: single quotes unless the text holds a single quote and no
/// double quote, backslash escapes for the quote, `\\`, `\n`, `\r`, `\t` and other control
/// characters (`\xNN`). Non-ASCII printable characters are kept, as Python 3 does.
#[must_use]
pub fn repr_str(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(s.len() + 2);
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
            c if u32::from(c) < 0x20 || u32::from(c) == 0x7f => {
                out.push_str(&format!("\\x{:02x}", u32::from(c)));
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// `repr(x)` for a Python `float`: the shortest round-tripping digits, Python's exponent form
/// (`1e-07`, `1e+16`) and `.0` on whole numbers below 1e16.
#[must_use]
pub fn repr_float(x: f64) -> String {
    if x.is_nan() {
        return "nan".to_owned();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf" } else { "-inf" }.to_owned();
    }
    // Rust's `{:e}` gives the shortest round-trip digits in scientific form.
    let sci = format!("{x:e}");
    let Some((mantissa, exp_text)) = sci.split_once('e') else {
        return sci;
    };
    let exponent: i64 = exp_text.parse().unwrap_or(0);
    let negative = mantissa.starts_with('-');
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let sign = if negative { "-" } else { "" };
    if (-4..16).contains(&exponent) {
        let n = i64::try_from(digits.len()).unwrap_or(i64::MAX);
        let body = if exponent >= n - 1 {
            let zeros = usize::try_from(exponent - (n - 1)).unwrap_or(0);
            format!("{digits}{}.0", "0".repeat(zeros))
        } else if exponent >= 0 {
            let split = usize::try_from(exponent + 1).unwrap_or(0);
            format!("{}.{}", &digits[..split], &digits[split..])
        } else {
            let zeros = usize::try_from(-exponent - 1).unwrap_or(0);
            format!("0.{}{digits}", "0".repeat(zeros))
        };
        return format!("{sign}{body}");
    }
    let mut m = digits[..1].to_owned();
    if digits.len() > 1 {
        m.push('.');
        m.push_str(&digits[1..]);
    }
    let esign = if exponent < 0 { '-' } else { '+' };
    format!("{sign}{m}e{esign}{:02}", exponent.unsigned_abs())
}

/// `json.dumps(s)` for a `str` with Python's defaults (`ensure_ascii=True`): non-ASCII as
/// `\uXXXX` (surrogate pairs above the BMP), the short escapes JSON names, other control
/// characters as `\u00XX`.
#[must_use]
pub fn json_str(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
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
            c if c.is_ascii() && u32::from(c) >= 0x20 => out.push(c),
            c => {
                let mut units = [0_u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
        }
    }
    out.push('"');
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fixed_matches_python() {
        assert_eq!(fixed(0.125, 2), "0.12");
        assert_eq!(fixed(0.375, 2), "0.38");
        assert_eq!(fixed(2.5, 0), "2");
        assert_eq!(fixed(30.0, 0), "30");
        assert_eq!(fixed(1.05, 1), "1.1");
        assert_eq!(fixed(f64::NAN, 1), "nan");
    }

    #[test]
    fn general_matches_python() {
        assert_eq!(general(2.0), "2");
        assert_eq!(general(3.0), "3");
        assert_eq!(general(0.5), "0.5");
        assert_eq!(general(1234567.0), "1.23457e+06");
        assert_eq!(general(0.0001), "0.0001");
        assert_eq!(general(0.00001), "1e-05");
        assert_eq!(general(100000.0), "100000");
    }

    #[test]
    fn lua_number_is_percent_14g() {
        assert_eq!(lua_number(3.0), "3");
        assert_eq!(lua_number(0.333_333_333_333_333_3), "0.33333333333333");
        assert_eq!(lua_number(123_456.789_012_345_67), "123456.78901235");
        assert_eq!(lua_number(1e20), "1e+20");
    }

    #[test]
    fn repr_float_matches_python() {
        assert_eq!(repr_float(3.0), "3.0");
        assert_eq!(repr_float(0.1), "0.1");
        assert_eq!(repr_float(1e-7), "1e-07");
        assert_eq!(repr_float(1e16), "1e+16");
        assert_eq!(repr_float(123.456), "123.456");
        assert_eq!(repr_float(-0.0001), "-0.0001");
        assert_eq!(repr_float(1.5e300), "1.5e+300");
    }

    #[test]
    fn repr_str_matches_python() {
        assert_eq!(repr_str("baskfy:ratelimit:kite"), "'baskfy:ratelimit:kite'");
        assert_eq!(repr_str("it's"), "\"it's\"");
        assert_eq!(repr_str("a\nb"), "'a\\nb'");
    }
}
