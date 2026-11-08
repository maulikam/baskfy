//! Ports `app/core/net.py`: which address family the broker sees us on.
//!
//! Kite authorises order placement against an IP allowlist; on 19 Aug 2026 every order was
//! refused because the resolver preferred IPv6 while the allowlist held the IPv4 address.
//! Python patches `urllib3`'s `allowed_gai_family` process-wide; this build has no live Kite
//! HTTP client (R8.0), so the pin is a process-wide switch [`ipv4_only`] that the live client,
//! when it is ported, must read before it builds its connection pool (bind to `0.0.0.0`).
//!
//! It does not make the address STATIC (SEBI's retail-algo framework wants that; a residential
//! line does not provide it).

use std::sync::atomic::{AtomicBool, Ordering};

static IPV4_ONLY: AtomicBool = AtomicBool::new(false);

/// `force_ipv4()`: pin outbound HTTP to IPv4. True if this call changed anything; applying it
/// twice is a no-op.
pub fn force_ipv4() -> bool {
    !IPV4_ONLY.swap(true, Ordering::SeqCst)
}

/// Is outbound HTTP pinned to IPv4?
#[must_use]
pub fn ipv4_only() -> bool {
    IPV4_ONLY.load(Ordering::SeqCst)
}

/// The local address a client binds to when pinned: `0.0.0.0` (IPv4), else `None` (the
/// resolver's choice).
#[must_use]
pub fn local_bind() -> Option<std::net::IpAddr> {
    ipv4_only().then_some(std::net::IpAddr::V4(std::net::Ipv4Addr::UNSPECIFIED))
}

/// `outbound_ip(timeout)`: the address the world sees, read-only, never on the order path.
/// The fetch is injected (this build makes no network call of its own); a failure is `None`.
pub async fn outbound_ip<F, Fut>(fetch: F) -> Option<String>
where
    F: FnOnce() -> Fut,
    Fut: std::future::Future<Output = Result<String, String>>,
{
    fetch().await.ok().map(|s| s.trim().to_owned())
}
