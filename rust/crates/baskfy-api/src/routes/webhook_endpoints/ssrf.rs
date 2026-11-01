//! The webhook URL checks: `routers.webhook_endpoints._check_url` and
//! `baskfy_api.webhooks.assert_public_webhook_url` (AUDIT 2.6, `test_webhook_ssrf.py`), with the
//! parts of CPython they stand on reproduced: `urllib.parse.urlsplit` (scheme, netloc, and the
//! `hostname`/`port` properties, including the `ValueError`s they raise), `ipaddress` 3.12's
//! "not public" predicates, and the `idna` codec's label check `socket.getaddrinfo` applies.
//!
//! Refused: a scheme other than http/https; no host; plain http in production; `localhost`,
//! `*.localhost`, `metadata.google.internal`; a literal IP that is private, loopback,
//! link-local, reserved, multicast or unspecified (an IPv4-mapped IPv6 address by its IPv4); and
//! a name that **resolves** to any such address (every address the resolver returns is checked,
//! so a DNS answer that mixes a public and a private address is refused). Resolution goes
//! through the system resolver (`getaddrinfo`, as Python's `socket.getaddrinfo`), off the async
//! runtime.
//!
//! **Approximated** (DECISIONS-RS R6.35): a non-ASCII network location is not NFKC-checked and
//! not IDNA-encoded before resolution (Python may raise, or resolve the punycode); a name that
//! resolves to a scoped IPv6 address prints without its `%zone` in the refusal.

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

/// Why a URL was refused.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Refusal {
    /// A 400 with this sentence (`_bad_request(detail)`).
    BadRequest(String),
    /// A `ValueError` Python does not catch: the 500 catch-all (the reason is logged).
    Unhandled(String),
}

/// `urlsplit(url)`'s `scheme` and `netloc`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Split {
    /// The scheme, lower-cased.
    pub(crate) scheme: String,
    /// The network location.
    pub(crate) netloc: String,
}

fn is_scheme_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.')
}

/// `urllib.parse.urlsplit(url)` up to the network location (CPython 3.12).
///
/// # Errors
/// The `ValueError`s it raises for unbalanced or invalid brackets.
pub(crate) fn urlsplit(url: &str) -> Result<Split, String> {
    let url: String = url
        .trim_start_matches(|c: char| c <= ' ')
        .chars()
        .filter(|c| !matches!(c, '\t' | '\r' | '\n'))
        .collect();
    let mut scheme = String::new();
    let mut rest: &str = &url;
    if let Some(i) = url.find(':')
        && i > 0
        && url.chars().next().is_some_and(|c| c.is_ascii_alphabetic())
        && url[..i].chars().all(is_scheme_char)
    {
        scheme = url[..i].to_lowercase();
        rest = &url[i + 1..];
    }
    let mut netloc = String::new();
    if let Some(after) = rest.strip_prefix("//") {
        let end = after.find(['/', '?', '#']).unwrap_or(after.len());
        netloc = after[..end].to_owned();
        let open = netloc.contains('[');
        let close = netloc.contains(']');
        if open != close {
            return Err("Invalid IPv6 URL".to_owned());
        }
        if open && close {
            check_bracketed_netloc(&netloc)?;
        }
    }
    Ok(Split { scheme, netloc })
}

/// `_check_bracketed_netloc`.
fn check_bracketed_netloc(netloc: &str) -> Result<(), String> {
    let host_port = netloc.rsplit_once('@').map_or(netloc, |(_, h)| h);
    if let Some((before, bracketed)) = host_port.split_once('[') {
        if !before.is_empty() {
            return Err("Invalid IPv6 URL".to_owned());
        }
        let (hostname, port) = bracketed.split_once(']').unwrap_or((bracketed, ""));
        if !port.is_empty() && !port.starts_with(':') {
            return Err("Invalid IPv6 URL".to_owned());
        }
        if let Some(future) = hostname.strip_prefix('v') {
            let ok = future.split_once('.').is_some_and(|(hex, tail)| {
                !hex.is_empty() && hex.chars().all(|c| c.is_ascii_hexdigit()) && !tail.is_empty()
            });
            if !ok {
                return Err("IPvFuture address is invalid".to_owned());
            }
        } else {
            match ip_address(hostname) {
                Some(Ip::V4(_)) => {
                    return Err("An IPv4 address cannot be in brackets".to_owned());
                }
                Some(Ip::V6(..)) => {}
                None => {
                    return Err(format!(
                        "{} does not appear to be an IPv4 or IPv6 address",
                        baskfy_providers::py::repr_str(hostname)
                    ));
                }
            }
        }
    }
    Ok(())
}

/// `SplitResult._hostinfo`: the raw host and port text.
fn hostinfo(netloc: &str) -> (&str, Option<&str>) {
    let info = netloc.rsplit_once('@').map_or(netloc, |(_, h)| h);
    let (host, port) = match info.split_once('[') {
        Some((_, bracketed)) => {
            let (host, after) = bracketed.split_once(']').unwrap_or((bracketed, ""));
            (host, after.split_once(':').map_or("", |(_, p)| p))
        }
        None => info.split_once(':').unwrap_or((info, "")),
    };
    (host, if port.is_empty() { None } else { Some(port) })
}

/// `SplitResult.hostname`: lower-cased, a zone kept as written; `None` when empty.
pub(crate) fn hostname(netloc: &str) -> Option<String> {
    let (host, _) = hostinfo(netloc);
    if host.is_empty() {
        return None;
    }
    Some(match host.split_once('%') {
        Some((h, zone)) => format!("{}%{zone}", h.to_lowercase()),
        None => host.to_lowercase(),
    })
}

/// `SplitResult.port`.
///
/// # Errors
/// The `ValueError` Python raises for a non-numeric or out-of-range port.
pub(crate) fn port(netloc: &str) -> Result<Option<u16>, String> {
    let (_, port) = hostinfo(netloc);
    let Some(port) = port else {
        return Ok(None);
    };
    if !port.chars().all(|c| c.is_ascii_digit()) {
        return Err(format!(
            "Port could not be cast to integer value as {}",
            baskfy_providers::py::repr_str(port)
        ));
    }
    match port.trim_start_matches('0').parse::<u64>() {
        Ok(p) if p <= 65535 => Ok(u16::try_from(p).ok()),
        Err(_) if port.trim_start_matches('0').is_empty() => Ok(Some(0)),
        _ => Err("Port out of range 0-65535".to_owned()),
    }
}

/// A parsed `ipaddress.ip_address`: IPv4, or IPv6 with its zone.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Ip {
    /// IPv4.
    V4(Ipv4Addr),
    /// IPv6 and its `%zone`, if any.
    V6(Ipv6Addr, Option<String>),
}

impl Ip {
    /// `str(address)`.
    pub(crate) fn display(&self) -> String {
        match self {
            Self::V4(a) => a.to_string(),
            Self::V6(a, Some(zone)) => format!("{a}%{zone}"),
            Self::V6(a, None) => a.to_string(),
        }
    }
}

/// `IPv4Address(text)`: four dotted decimal octets, no leading zeros, each 0–255.
fn ipv4(text: &str) -> Option<Ipv4Addr> {
    let parts: Vec<&str> = text.split('.').collect();
    if parts.len() != 4 {
        return None;
    }
    let mut octets = [0u8; 4];
    for (slot, p) in octets.iter_mut().zip(&parts) {
        if p.is_empty() || p.len() > 3 || !p.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        if p.len() > 1 && p.starts_with('0') {
            return None;
        }
        *slot = p.parse::<u8>().ok()?;
    }
    Some(Ipv4Addr::from(octets))
}

/// `ipaddress.ip_address(text)`, or `None` for `ValueError`.
pub(crate) fn ip_address(text: &str) -> Option<Ip> {
    if let Some(v4) = ipv4(text) {
        return Some(Ip::V4(v4));
    }
    let (addr, zone) = match text.split_once('%') {
        Some((a, z)) => {
            if z.is_empty() || z.contains('%') {
                return None;
            }
            (a, Some(z.to_owned()))
        }
        None => (text, None),
    };
    if !addr.is_ascii() || addr.contains(['[', ']']) {
        return None;
    }
    addr.parse::<Ipv6Addr>().ok().map(|a| Ip::V6(a, zone))
}

fn in_v4(a: Ipv4Addr, net: [u8; 4], prefix: u32) -> bool {
    let mask = if prefix == 0 {
        0
    } else {
        u32::MAX << (32 - prefix)
    };
    (u32::from(a) & mask) == (u32::from(Ipv4Addr::from(net)) & mask)
}

fn in_v6(a: Ipv6Addr, net: &str, prefix: u32) -> bool {
    let Ok(net) = net.parse::<Ipv6Addr>() else {
        return false;
    };
    let mask = if prefix == 0 {
        0
    } else {
        u128::MAX << (128 - prefix)
    };
    (u128::from(a) & mask) == (u128::from(net) & mask)
}

/// `_is_blocked_ip` for IPv4 (CPython 3.12's `_IPv4Constants`).
fn blocked_v4(a: Ipv4Addr) -> bool {
    const PRIVATE: [([u8; 4], u32); 14] = [
        ([0, 0, 0, 0], 8),
        ([10, 0, 0, 0], 8),
        ([127, 0, 0, 0], 8),
        ([169, 254, 0, 0], 16),
        ([172, 16, 0, 0], 12),
        ([192, 0, 0, 0], 24),
        ([192, 0, 0, 170], 31),
        ([192, 0, 2, 0], 24),
        ([192, 168, 0, 0], 16),
        ([198, 18, 0, 0], 15),
        ([198, 51, 100, 0], 24),
        ([203, 0, 113, 0], 24),
        ([240, 0, 0, 0], 4),
        ([255, 255, 255, 255], 32),
    ];
    const EXCEPTIONS: [([u8; 4], u32); 2] = [([192, 0, 0, 9], 32), ([192, 0, 0, 10], 32)];
    let private = PRIVATE.iter().any(|(n, p)| in_v4(a, *n, *p))
        && !EXCEPTIONS.iter().any(|(n, p)| in_v4(a, *n, *p));
    private
        || in_v4(a, [127, 0, 0, 0], 8)
        || in_v4(a, [169, 254, 0, 0], 16)
        || in_v4(a, [240, 0, 0, 0], 4)
        || in_v4(a, [224, 0, 0, 0], 4)
        || u32::from(a) == 0
}

/// `_is_blocked_ip` for IPv6 (CPython 3.12's `_IPv6Constants`; a mapped address by its IPv4).
fn blocked_v6(a: Ipv6Addr) -> bool {
    let bits = u128::from(a);
    if bits >> 32 == 0xFFFF {
        let low = u32::try_from(bits & 0xFFFF_FFFF).unwrap_or(0);
        return blocked_v4(Ipv4Addr::from(low));
    }
    const PRIVATE: [(&str, u32); 11] = [
        ("::1", 128),
        ("::", 128),
        ("::ffff:0:0", 96),
        ("64:ff9b:1::", 48),
        ("100::", 64),
        ("2001::", 23),
        ("2001:db8::", 32),
        ("2002::", 16),
        ("3fff::", 20),
        ("fc00::", 7),
        ("fe80::", 10),
    ];
    const EXCEPTIONS: [(&str, u32); 6] = [
        ("2001:1::1", 128),
        ("2001:1::2", 128),
        ("2001:3::", 32),
        ("2001:4:112::", 48),
        ("2001:20::", 28),
        ("2001:30::", 28),
    ];
    const RESERVED: [(&str, u32); 15] = [
        ("::", 8),
        ("100::", 8),
        ("200::", 7),
        ("400::", 6),
        ("800::", 5),
        ("1000::", 4),
        ("4000::", 3),
        ("6000::", 3),
        ("8000::", 3),
        ("a000::", 3),
        ("c000::", 3),
        ("e000::", 4),
        ("f000::", 5),
        ("f800::", 6),
        ("fe00::", 9),
    ];
    let private = PRIVATE.iter().any(|(n, p)| in_v6(a, n, *p))
        && !EXCEPTIONS.iter().any(|(n, p)| in_v6(a, n, *p));
    private
        || bits == 1
        || in_v6(a, "fe80::", 10)
        || RESERVED.iter().any(|(n, p)| in_v6(a, n, *p))
        || in_v6(a, "ff00::", 8)
        || bits == 0
}

/// `_is_blocked_ip(address)`.
pub(crate) fn is_blocked(ip: &Ip) -> bool {
    match ip {
        Ip::V4(a) => blocked_v4(*a),
        Ip::V6(a, _) => blocked_v6(*a),
    }
}

/// The `idna` codec's check on an ASCII host before `getaddrinfo` (a `UnicodeError`, which
/// Python does not catch here): every label but the last 1–63 characters, the last under 64.
fn idna_ascii_ok(host: &str) -> bool {
    if host.is_empty() || !host.is_ascii() {
        return true;
    }
    let labels: Vec<&str> = host.split('.').collect();
    let (last, rest) = labels.split_last().unwrap_or((&"", &[]));
    rest.iter().all(|l| !l.is_empty() && l.len() < 64) && last.len() < 64
}

/// `resolve_webhook_host(host, port)`: every address `getaddrinfo` returns, as `ip_address`
/// reads it.
async fn resolve(host: &str, port: u16) -> Result<Vec<Ip>, Refusal> {
    if host.contains('\0') {
        return Err(Refusal::Unhandled("embedded null character".to_owned()));
    }
    if !idna_ascii_ok(host) {
        return Err(Refusal::Unhandled("label empty or too long".to_owned()));
    }
    match tokio::net::lookup_host((host, port)).await {
        Ok(addrs) => Ok(addrs
            .map(|a| match a.ip() {
                IpAddr::V4(v4) => Ip::V4(v4),
                IpAddr::V6(v6) => Ip::V6(v6, None),
            })
            .collect()),
        Err(_) => Err(Refusal::BadRequest(format!(
            "webhook host {} does not resolve",
            baskfy_providers::py::repr_str(host)
        ))),
    }
}

/// `assert_public_webhook_url(url)`.
///
/// # Errors
/// The `WebhookUrlNotPublic` sentence, or an unhandled `ValueError`.
pub(crate) async fn assert_public(url: &str) -> Result<(), Refusal> {
    let split = urlsplit(url).map_err(Refusal::Unhandled)?;
    let Some(host) = hostname(&split.netloc) else {
        return Err(Refusal::BadRequest(
            "A webhook URL must name a host.".to_owned(),
        ));
    };
    let lowered = host.to_lowercase();
    if lowered == "localhost"
        || lowered == "metadata.google.internal"
        || lowered.ends_with(".localhost")
    {
        return Err(Refusal::BadRequest(format!(
            "webhook host {} is not a public address",
            baskfy_providers::py::repr_str(&host)
        )));
    }
    if let Some(literal) = ip_address(&host)
        && is_blocked(&literal)
    {
        return Err(Refusal::BadRequest(format!(
            "webhook URL targets a non-public address ({})",
            literal.display()
        )));
    }
    let given = port(&split.netloc).map_err(Refusal::Unhandled)?;
    let port = match given {
        Some(p) if p != 0 => p,
        _ => {
            if split.scheme == "https" {
                443
            } else {
                80
            }
        }
    };
    for ip in resolve(&host, port).await? {
        if is_blocked(&ip) {
            return Err(Refusal::BadRequest(format!(
                "webhook URL resolves to a non-public address ({})",
                ip.display()
            )));
        }
    }
    Ok(())
}

/// The checks `getaddrinfo` applies to a host before it resolves anything (an embedded NUL, the
/// `idna` codec's label lengths) — Python's uncaught `ValueError`/`UnicodeError`. For a resolver
/// passed to [`assert_public_resolving`] (wave 6, lane ops_alerts).
///
/// # Errors
/// [`Refusal::Unhandled`] with Python's message.
pub(crate) fn host_encodable(host: &str) -> Result<(), Refusal> {
    if host.contains('\0') {
        return Err(Refusal::Unhandled("embedded null character".to_owned()));
    }
    if !idna_ascii_ok(host) {
        return Err(Refusal::Unhandled("label empty or too long".to_owned()));
    }
    Ok(())
}

/// [`assert_public`] with the resolution step injected: `resolve(host, port)` answers every
/// address the name resolves to, or the refusal (`"webhook host '…' does not resolve"`). The
/// worker's webhook sweep passes its own (the delivery-time half of AUDIT 2.6, through the
/// guarded resolver, so the harness's network block reaches it); everything before and after the
/// resolution is [`assert_public`]'s, unchanged (wave 6, lane ops_alerts).
///
/// # Errors
/// As [`assert_public`].
pub(crate) async fn assert_public_resolving<F, Fut>(url: &str, resolve: F) -> Result<(), Refusal>
where
    F: FnOnce(String, u16) -> Fut,
    Fut: std::future::Future<Output = Result<Vec<IpAddr>, Refusal>>,
{
    let split = urlsplit(url).map_err(Refusal::Unhandled)?;
    let Some(host) = hostname(&split.netloc) else {
        return Err(Refusal::BadRequest(
            "A webhook URL must name a host.".to_owned(),
        ));
    };
    let lowered = host.to_lowercase();
    if lowered == "localhost"
        || lowered == "metadata.google.internal"
        || lowered.ends_with(".localhost")
    {
        return Err(Refusal::BadRequest(format!(
            "webhook host {} is not a public address",
            baskfy_providers::py::repr_str(&host)
        )));
    }
    if let Some(literal) = ip_address(&host)
        && is_blocked(&literal)
    {
        return Err(Refusal::BadRequest(format!(
            "webhook URL targets a non-public address ({})",
            literal.display()
        )));
    }
    let given = port(&split.netloc).map_err(Refusal::Unhandled)?;
    let port = match given {
        Some(p) if p != 0 => p,
        _ => {
            if split.scheme == "https" {
                443
            } else {
                80
            }
        }
    };
    for address in resolve(host, port).await? {
        let ip = match address {
            IpAddr::V4(v4) => Ip::V4(v4),
            IpAddr::V6(v6) => Ip::V6(v6, None),
        };
        if is_blocked(&ip) {
            return Err(Refusal::BadRequest(format!(
                "webhook URL resolves to a non-public address ({})",
                ip.display()
            )));
        }
    }
    Ok(())
}

/// `_check_url(url, settings)`: the scheme, the host, https in production, then
/// [`assert_public`].
///
/// # Errors
/// As [`assert_public`], plus the three scheme/host sentences.
pub(crate) async fn check_url(url: &str, environment: &str) -> Result<(), Refusal> {
    let split = urlsplit(url).map_err(Refusal::Unhandled)?;
    if split.scheme != "http" && split.scheme != "https" {
        return Err(Refusal::BadRequest(
            "A webhook URL must be http:// or https://.".to_owned(),
        ));
    }
    if split.netloc.is_empty() {
        return Err(Refusal::BadRequest(
            "A webhook URL must name a host.".to_owned(),
        ));
    }
    if split.scheme == "http" && environment == "production" {
        return Err(Refusal::BadRequest(
            "A webhook URL must be https:// in production.".to_owned(),
        ));
    }
    assert_public(url).await
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn refused(url: &str) -> String {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        match rt.block_on(check_url(url, "test")) {
            Err(Refusal::BadRequest(s) | Refusal::Unhandled(s)) => s,
            Ok(()) => "ok".to_owned(),
        }
    }

    #[test]
    fn private_and_loopback_literals_are_refused() {
        // test_webhook_ssrf.py: loopback, RFC 1918, link-local metadata, IPv6 loopback.
        assert_eq!(
            refused("http://127.0.0.1/hook"),
            "webhook URL targets a non-public address (127.0.0.1)"
        );
        assert_eq!(
            refused("https://10.1.2.3/x"),
            "webhook URL targets a non-public address (10.1.2.3)"
        );
        assert_eq!(
            refused("http://169.254.169.254/latest/meta-data"),
            "webhook URL targets a non-public address (169.254.169.254)"
        );
        assert_eq!(
            refused("http://[::1]:8080/x"),
            "webhook URL targets a non-public address (::1)"
        );
        assert_eq!(
            refused("http://[::ffff:192.168.1.1]/x"),
            "webhook URL targets a non-public address (::ffff:192.168.1.1)"
        );
        assert_eq!(
            refused("http://LOCALHOST/x"),
            "webhook host 'localhost' is not a public address"
        );
        assert_eq!(
            refused("http://metadata.google.internal/x"),
            "webhook host 'metadata.google.internal' is not a public address"
        );
    }

    #[test]
    fn a_public_literal_passes_and_shapes_are_checked() {
        assert_eq!(refused("https://8.8.8.8/hook"), "ok");
        assert_eq!(
            refused("ftp://8.8.8.8/x"),
            "A webhook URL must be http:// or https://."
        );
        assert_eq!(refused("https:///x"), "A webhook URL must name a host.");
        assert_eq!(refused("https://:443/x"), "A webhook URL must name a host.");
        assert_eq!(refused("https://[::1/x"), "Invalid IPv6 URL");
        assert_eq!(
            refused("https://8.8.8.8:99999/x"),
            "Port out of range 0-65535"
        );
        assert_eq!(
            refused("https://8.8.8.8:x/x"),
            "Port could not be cast to integer value as 'x'"
        );
        assert_eq!(refused("https://a..b/x"), "label empty or too long");
    }

    #[test]
    fn ipaddress_reads_as_python_reads() {
        assert!(ip_address("010.0.0.1").is_none());
        assert!(ip_address("1.2.3").is_none());
        assert_eq!(ip_address("fe80::1%lo0").unwrap().display(), "fe80::1%lo0");
        assert!(is_blocked(&ip_address("100::1").unwrap()));
        assert!(!is_blocked(&ip_address("100.64.0.1").unwrap()));
        assert!(!is_blocked(&ip_address("192.0.0.9").unwrap()));
        assert!(!is_blocked(&ip_address("2001:4860:4860::8888").unwrap()));
        assert!(is_blocked(&ip_address("2001:db8::1").unwrap()));
    }
}
