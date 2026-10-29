//! Double-submit CSRF for the two cookie-authenticated mutations (`POST /auth/refresh`,
//! `POST /auth/logout`). Ports `baskfy_api.csrf`: the check, the names, and the cookie writers
//! (`set_auth_cookies`, `clear_auth_cookies`) with Starlette's `Set-Cookie` spelling.

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

#[allow(clippy::too_many_arguments)] // one per `set_cookie` keyword Starlette takes
/// One `Set-Cookie` value as Starlette's `Response.set_cookie` writes it: `http.cookies.Morsel`
/// output, attributes in sorted key order (`Domain`, `expires`, `HttpOnly`, `Max-Age`, `Path`,
/// `SameSite`, `Secure`), an empty value written `""`.
fn morsel(
    name: &str,
    value: &str,
    max_age: i64,
    path: &str,
    domain: Option<&str>,
    expires_now: bool,
    secure: bool,
    httponly: bool,
) -> String {
    let mut out = format!("{name}={}", if value.is_empty() { "\"\"" } else { value });
    if let Some(d) = domain {
        out.push_str(&format!("; Domain={d}"));
    }
    if expires_now {
        // `expires=0` -> `email.utils.formatdate(time.time() + 0, usegmt=True)`.
        let now = crate::entitlements::utc_now();
        out.push_str(&format!(
            "; expires={}",
            now.format("%a, %d %b %Y %H:%M:%S GMT")
        ));
    }
    if httponly {
        out.push_str("; HttpOnly");
    }
    out.push_str(&format!("; Max-Age={max_age}; Path={path}; SameSite=lax"));
    if secure {
        out.push_str("; Secure");
    }
    out
}

/// `set_auth_cookies(response, refresh_token=, csrf_token=, ttl_seconds=, settings=)`: the two
/// `Set-Cookie` values, refresh first.
#[must_use]
pub fn set_auth_cookies(
    refresh_token: &str,
    csrf_token: &str,
    ttl_seconds: i64,
    settings: &crate::settings::Settings,
) -> [String; 2] {
    let domain = settings.cookie_domain.as_deref().filter(|d| !d.is_empty());
    [
        morsel(
            REFRESH_COOKIE,
            refresh_token,
            ttl_seconds,
            REFRESH_COOKIE_PATH,
            domain,
            false,
            settings.cookie_secure,
            true,
        ),
        morsel(
            CSRF_COOKIE,
            csrf_token,
            ttl_seconds,
            "/",
            domain,
            false,
            settings.cookie_secure,
            false,
        ),
    ]
}

/// `clear_auth_cookies(response, settings)`: Starlette's `delete_cookie` for both (an empty
/// value, `expires` now, `Max-Age=0`, never `Secure`/`HttpOnly`).
#[must_use]
pub fn clear_auth_cookies(settings: &crate::settings::Settings) -> [String; 2] {
    let domain = settings.cookie_domain.as_deref().filter(|d| !d.is_empty());
    [
        morsel(
            REFRESH_COOKIE,
            "",
            0,
            REFRESH_COOKIE_PATH,
            domain,
            true,
            false,
            false,
        ),
        morsel(CSRF_COOKIE, "", 0, "/", domain, true, false, false),
    ]
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
