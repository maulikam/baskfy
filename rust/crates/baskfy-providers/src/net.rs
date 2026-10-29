//! The one door to the network, and the test suite's lock on it. Ports the guarantee of
//! `decile-blueprint/network_guard.py` (Prompt 2 acceptance criterion 1: "zero network calls in
//! the test suite").
//!
//! Python monkeypatches `socket.connect`, `create_connection` and `getaddrinfo` for the whole
//! test session. Rust cannot patch the standard library, so the guarantee is built the other way
//! round (DECISIONS-RS R4.5):
//!
//! * every HTTP client in this crate is built from [`client_builder`] / [`GuardedClient`], whose
//!   DNS resolver and redirect policy consult the block, and whose request constructors check
//!   the URL's host (an IP literal never reaches a resolver);
//! * every Redis connection goes through [`guard_url`] first;
//! * `tests/network_is_blocked.rs` scans `src/` so no other client constructor exists, and scans
//!   `tests/` so every test binary arms the block with [`block_network`] before anything else.
//!
//! Loopback stays usable: the Redis- and database-backed tests talk to containers on the
//! developer's own machine, which are fixtures, not the network.

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};
use std::sync::atomic::{AtomicBool, Ordering};

use crate::errors::ProviderError;
use crate::py::repr_str;

/// Hosts a test may still reach: the local containers `make up` provides (`ALLOWED_HOSTS`).
pub const ALLOWED_HOSTS: [&str; 4] = ["127.0.0.1", "::1", "localhost", "0.0.0.0"];

/// What a connection is aimed at: a host/port pair, or a filesystem socket path.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Address<'a> {
    /// An `AF_UNIX` path.
    Unix(&'a str),
    /// A host name or IP literal and a port.
    Host(&'a str, u16),
}

/// `is_loopback`: true for socket paths and for the [`ALLOWED_HOSTS`].
#[must_use]
pub fn is_loopback(address: Address<'_>) -> bool {
    match address {
        Address::Unix(_) => true,
        Address::Host(host, _) => ALLOWED_HOSTS.contains(&host),
    }
}

/// `NetworkAccessBlocked`: a test tried to reach something other than loopback.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error(
    "{place} tried to reach ({host}, {port}). The test suite makes no network calls \
     (PROMPTS.md Prompt 2, acceptance criterion 1) — use FixtureProvider, or inject a fake \
     client through the provider's Runtime object."
)]
pub struct NetworkAccessBlocked {
    place: String,
    host: String,
    port: u16,
}

impl NetworkAccessBlocked {
    fn new(place: &str, host: &str, port: u16) -> Self {
        Self {
            place: place.to_owned(),
            host: repr_str(host),
            port,
        }
    }
}

impl From<NetworkAccessBlocked> for ProviderError {
    fn from(value: NetworkAccessBlocked) -> Self {
        let message = value.to_string();
        Self::foreign("NetworkAccessBlocked", message).with_source(value)
    }
}

static BLOCKED: AtomicBool = AtomicBool::new(false);

/// Arms the block for the rest of this process: every non-loopback resolve, request, redirect
/// or Redis connection is refused. Tests call it first; production never does. There is no
/// disarm — a test binary that armed it stays armed (the Python fixture is session-wide too).
pub fn block_network() {
    BLOCKED.store(true, Ordering::SeqCst);
}

/// Is the block armed?
#[must_use]
pub fn network_blocked() -> bool {
    BLOCKED.load(Ordering::SeqCst)
}

/// Refuses `host:port` when the block is armed and the host is not loopback.
///
/// # Errors
/// [`NetworkAccessBlocked`] naming `place` and the address.
pub fn check(place: &str, host: &str, port: u16) -> Result<(), NetworkAccessBlocked> {
    if network_blocked() && !is_loopback(Address::Host(host, port)) {
        return Err(NetworkAccessBlocked::new(place, host, port));
    }
    Ok(())
}

/// Refuses a URL whose host is not loopback while the block is armed. A URL with no host
/// (`redis+unix:///run/redis.sock`, `unix:`) is a socket path and passes.
///
/// # Errors
/// [`ProviderError`] (`ValueError`) for an unparseable URL; `NetworkAccessBlocked` otherwise.
pub fn guard_url(place: &str, url: &str) -> Result<(), ProviderError> {
    let parsed = reqwest::Url::parse(url)
        .map_err(|e| ProviderError::value_error(format!("invalid URL {}: {e}", repr_str(url))))?;
    guard_parsed(place, &parsed).map_err(ProviderError::from)
}

fn guard_parsed(place: &str, url: &reqwest::Url) -> Result<(), NetworkAccessBlocked> {
    let Some(host) = url.host_str() else {
        return Ok(());
    };
    let host = host.trim_start_matches('[').trim_end_matches(']');
    check(place, host, url.port_or_known_default().unwrap_or(0))
}

/// The DNS resolver every client uses: refuses non-loopback names while the block is armed,
/// answers loopback names itself, and otherwise asks the system resolver.
#[derive(Debug, Clone, Copy, Default)]
pub struct GuardedResolver;

impl GuardedResolver {
    /// Resolves `host` (port 0, as reqwest asks), refusing it when the block forbids it.
    ///
    /// # Errors
    /// `NetworkAccessBlocked`, or the system resolver's failure.
    pub async fn lookup(&self, host: &str) -> Result<Vec<SocketAddr>, ProviderError> {
        check("getaddrinfo", host, 0)?;
        if let Some(addrs) = loopback_addrs(host) {
            return Ok(addrs);
        }
        let found = tokio::net::lookup_host((host, 0))
            .await
            .map_err(|e| {
                ProviderError::upstream_unavailable(format!(
                    "could not resolve {}: {e}",
                    repr_str(host)
                ))
            })?
            .collect();
        Ok(found)
    }
}

fn loopback_addrs(host: &str) -> Option<Vec<SocketAddr>> {
    match host {
        "localhost" => Some(vec![
            SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 0),
            SocketAddr::new(IpAddr::V6(Ipv6Addr::LOCALHOST), 0),
        ]),
        other => other
            .parse::<IpAddr>()
            .ok()
            .map(|ip| vec![SocketAddr::new(ip, 0)]),
    }
}

impl reqwest::dns::Resolve for GuardedResolver {
    fn resolve(&self, name: reqwest::dns::Name) -> reqwest::dns::Resolving {
        let resolver = *self;
        let host = name.as_str().to_owned();
        Box::pin(async move {
            let addrs = resolver.lookup(&host).await?;
            let iter: reqwest::dns::Addrs = Box::new(addrs.into_iter());
            Ok(iter)
        })
    }
}

/// A `reqwest::ClientBuilder` with the guarded resolver and a redirect policy that refuses a
/// non-loopback hop while the block is armed (at most ten hops, reqwest's default). The kite
/// and nse ports add their timeouts, headers and cookie jar to this, never to a bare builder.
pub fn client_builder() -> reqwest::ClientBuilder {
    let policy = reqwest::redirect::Policy::custom(|attempt| {
        if let Err(blocked) = guard_parsed("redirect", attempt.url()) {
            return attempt.error(blocked);
        }
        if attempt.previous().len() >= 10 {
            return attempt.error("too many redirects");
        }
        attempt.follow()
    });
    reqwest::Client::builder()
        .dns_resolver(std::sync::Arc::new(GuardedResolver))
        .redirect(policy)
}

/// An HTTP client whose every request is checked against the block before it is built.
#[derive(Debug, Clone)]
pub struct GuardedClient {
    inner: reqwest::Client,
}

impl GuardedClient {
    /// Builds the client from a builder that came from [`client_builder`].
    ///
    /// # Errors
    /// `UpstreamUnavailable` when the TLS backend cannot initialise.
    pub fn from_builder(builder: reqwest::ClientBuilder) -> Result<Self, ProviderError> {
        let inner = builder.build().map_err(|e| {
            ProviderError::upstream_unavailable(format!("could not build the HTTP client: {e}"))
        })?;
        Ok(Self { inner })
    }

    /// A request to `url`, refused up front when the block forbids its host.
    ///
    /// # Errors
    /// `ValueError` for a bad URL; `NetworkAccessBlocked` when the block refuses it.
    pub fn request(
        &self,
        method: reqwest::Method,
        url: &str,
    ) -> Result<reqwest::RequestBuilder, ProviderError> {
        let parsed = reqwest::Url::parse(url).map_err(|e| {
            ProviderError::value_error(format!("invalid URL {}: {e}", repr_str(url)))
        })?;
        guard_parsed("http request", &parsed)?;
        Ok(self.inner.request(method, parsed))
    }

    /// `GET url`.
    ///
    /// # Errors
    /// As [`GuardedClient::request`].
    pub fn get(&self, url: &str) -> Result<reqwest::RequestBuilder, ProviderError> {
        self.request(reqwest::Method::GET, url)
    }

    /// `POST url`.
    ///
    /// # Errors
    /// As [`GuardedClient::request`].
    pub fn post(&self, url: &str) -> Result<reqwest::RequestBuilder, ProviderError> {
        self.request(reqwest::Method::POST, url)
    }
}
