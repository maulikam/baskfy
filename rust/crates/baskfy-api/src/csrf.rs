//! Double-submit CSRF for the two cookie-authenticated mutations (`POST /auth/refresh`,
//! `POST /auth/logout`). Ports `baskfy_api.csrf`'s check and names; the cookie *writers* arrive
//! with the auth lane (they belong to routes that write).

use subtle_free::constant_time_eq;

use crate::problems::{Problem, ProblemType};

/// `REFRESH_COOKIE`: the httpOnly credential.
pub const REFRESH_COOKIE: &str = "baskfy_refresh";
/// `CSRF_COOKIE`: the readable half of the double submit.
pub const CSRF_COOKIE: &str = "baskfy_csrf";
/// `CSRF_HEADER`.
pub const CSRF_HEADER: &str = "X-CSRF-Token";
/// `CSRF_TOKEN_BYTES`.
pub const CSRF_TOKEN_BYTES: usize = 16;
/// `REFRESH_COOKIE_PATH`.
pub const REFRESH_COOKIE_PATH: &str = "/api/v1/auth";

/// `request.cookies.get(name)`: Starlette's `cookie_parser` — `;`-separated `key=value` pairs,
/// whitespace-trimmed, a pair without `=` read as a value with an empty key, the last one of a
/// name wins, values unquoted only of surrounding double quotes.
#[must_use]
pub fn cookie(header: Option<&str>, name: &str) -> Option<String> {
    let mut found = None;
    for chunk in header?.split(';') {
        let (key, value) = match chunk.split_once('=') {
            Some((k, v)) => (k.trim(), v.trim()),
            None => ("", chunk.trim()),
        };
        if key.is_empty() && value.is_empty() {
            continue;
        }
        if key == name {
            let unquoted = value
                .strip_prefix('"')
                .and_then(|v| v.strip_suffix('"'))
                .unwrap_or(value);
            found = Some(unquoted.to_owned());
        }
    }
    found
}

/// `require_csrf(request)`: 401 unless the `X-CSRF-Token` header equals the readable cookie,
/// compared in constant time.
///
/// # Errors
/// The 401 `unauthenticated` problem with `WWW-Authenticate: Bearer`.
pub fn require_csrf(cookie_header: Option<&str>, csrf_header: Option<&str>) -> Result<(), Problem> {
    let from_cookie = cookie(cookie_header, CSRF_COOKIE).unwrap_or_default();
    let header = csrf_header.unwrap_or_default();
    if from_cookie.is_empty()
        || header.is_empty()
        || !constant_time_eq(from_cookie.as_bytes(), header.as_bytes())
    {
        return Err(Problem::new(
            ProblemType::Unauthenticated,
            "This request is missing its CSRF token.",
        )
        .header("WWW-Authenticate", "Bearer"));
    }
    Ok(())
}

mod subtle_free {
    //! `hmac.compare_digest` for equal-length inputs; unequal lengths compare false at once
    //! (as Python's does — the length is not secret).
    pub(super) fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
        if a.len() != b.len() {
            return false;
        }
        a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_header_must_match_the_readable_cookie() {
        let jar = Some("baskfy_refresh=r; baskfy_csrf=abc");
        assert!(require_csrf(jar, Some("abc")).is_ok());
        assert!(require_csrf(jar, Some("abd")).is_err());
        assert!(require_csrf(jar, None).is_err());
        assert!(require_csrf(Some("baskfy_refresh=r"), Some("abc")).is_err());
        assert!(require_csrf(None, Some("")).is_err());
    }

    #[test]
    fn cookies_parse_as_starlette_parses_them() {
        assert_eq!(
            cookie(Some(" a=1 ; b=\"2\"; a=3"), "a").as_deref(),
            Some("3")
        );
        assert_eq!(cookie(Some("a=1; b=\"2\""), "b").as_deref(), Some("2"));
        assert_eq!(cookie(Some("a=1"), "z"), None);
    }
}
