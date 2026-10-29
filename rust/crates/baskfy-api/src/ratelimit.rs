//! HTTP rate limiting. Ports `baskfy_api.ratelimit` (docs/07 §Conventions, docs/11 §Security).
//!
//! The token bucket is `baskfy_providers.ratelimit.TAKE_SCRIPT`, run **byte for byte** against
//! the same Redis keys Python uses (`ratelimit:api:user:<public_id>`, `ratelimit:api:ip:<ip>`,
//! `ratelimit:auth:ip:<ip>`, `ratelimit:api:webhook:<ip>`), with the same arguments in redis-py's
//! encoding: `[limit, rate, now, 1]` → `str(int)`, `repr(float)`, `repr(float)`, `"1"`. During the
//! strangler a Python and a Rust process therefore draw from one bucket per caller.
//!
//! `now` is `time.monotonic()` (DECISIONS-RS R4.4: Linux `CLOCK_MONOTONIC`; on macOS Python reads
//! a different clock, so a shared bucket across a native-Mac Python and Rust drifts — the parity
//! harness runs with limits high enough never to bite).
//!
//! **Fails closed.** An unreachable Redis refuses the request with 503 `pipeline-degraded`, never
//! serves it unmetered. Redis is connected lazily and reconnected on demand, as redis-py's
//! `Redis.from_url` is.

use std::sync::Arc;
use std::time::Duration;

use baskfy_providers::clock::{Clock, system_clock};
use baskfy_providers::py::repr_float;
use baskfy_providers::ratelimit::{RedisScriptBackend, ScriptBackend, ScriptValue, TAKE_SCRIPT};
use baskfy_providers::secret::Secret;
use tokio::sync::Mutex;

use crate::auth::{Principal, PrincipalKind};
use crate::logging;
use crate::problems::{Problem, ProblemType, rate_limited};
use crate::settings::Settings;

/// `SECONDS_PER_MINUTE`.
pub const SECONDS_PER_MINUTE: f64 = 60.0;
/// `KEY_PREFIX`.
pub const KEY_PREFIX: &str = "ratelimit:api:";
/// `AUTH_KEY_PREFIX`.
pub const AUTH_KEY_PREFIX: &str = "ratelimit:auth:";
/// `AUTH_PATH_PREFIX`.
pub const AUTH_PATH_PREFIX: &str = "/auth/";
/// `WEBHOOK_PATH_PREFIX`.
pub const WEBHOOK_PATH_PREFIX: &str = "/webhooks/";

/// How long one connect attempt may take before the request is refused as degraded (the same
/// bound as the providers' factory, R4.13).
pub const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);

/// `Decision`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Decision {
    /// `allowed`.
    pub allowed: bool,
    /// `limit_per_minute`.
    pub limit_per_minute: i64,
    /// `retry_after_seconds`.
    pub retry_after_seconds: i64,
}

/// `auth_bucket_key(client_ip)`.
#[must_use]
pub fn auth_bucket_key(client_ip: Option<&str>) -> String {
    format!("{AUTH_KEY_PREFIX}ip:{}", client_ip.unwrap_or("unknown"))
}

/// `is_auth_path(path)` — a substring test, as Python's.
#[must_use]
pub fn is_auth_path(path: &str) -> bool {
    path.contains(AUTH_PATH_PREFIX)
}

/// `is_webhook_path(path)`.
#[must_use]
pub fn is_webhook_path(path: &str) -> bool {
    path.contains(WEBHOOK_PATH_PREFIX)
}

/// `bucket_key(principal, client_ip)`: per user when known, per IP otherwise.
#[must_use]
pub fn bucket_key(principal: &Principal, client_ip: Option<&str>) -> String {
    match &principal.public_id {
        Some(p) => format!("{KEY_PREFIX}user:{p}"),
        None => format!("{KEY_PREFIX}ip:{}", client_ip.unwrap_or("unknown")),
    }
}

/// `limit_for(principal, settings)`.
#[must_use]
pub fn limit_for(principal: &Principal, settings: &Settings) -> i64 {
    if principal.kind == PrincipalKind::ApiKey {
        return settings.rate_limit_api_key_per_minute;
    }
    if principal.is_authenticated() {
        return settings.rate_limit_authenticated_per_minute;
    }
    settings.rate_limit_anonymous_per_minute
}

/// `_decode(raw)`: `{granted, wait}` — an int and (as Lua returns it) a string.
fn decode(raw: &ScriptValue) -> Result<(bool, f64), String> {
    let ScriptValue::Array(items) = raw else {
        return Err(format!(
            "unexpected reply from the rate-limit script: {raw:?}"
        ));
    };
    let [granted, wait] = items.as_slice() else {
        return Err(format!(
            "unexpected reply from the rate-limit script: {raw:?}"
        ));
    };
    let text = |v: &ScriptValue| match v {
        ScriptValue::Int(i) => Ok(i.to_string()),
        ScriptValue::Bytes(b) => String::from_utf8(b.clone()).map_err(|e| e.to_string()),
        other => Err(format!("unexpected value {other:?}")),
    };
    let granted: i64 = text(granted)?
        .trim()
        .parse()
        .map_err(|_| "bad granted".to_owned())?;
    let wait: f64 = text(wait)?
        .trim()
        .parse()
        .map_err(|_| "bad wait".to_owned())?;
    Ok((granted != 0, wait))
}

/// `RateLimiter`: one script, one Redis round trip per request.
pub struct RateLimiter {
    url: Secret,
    backend: Mutex<Option<Arc<dyn ScriptBackend>>>,
    clock: Arc<dyn Clock>,
}

impl std::fmt::Debug for RateLimiter {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("RateLimiter")
    }
}

impl RateLimiter {
    /// A limiter on `settings.redis_url`; connects on first use.
    #[must_use]
    pub fn new(url: Secret) -> Self {
        Self {
            url,
            backend: Mutex::new(None),
            clock: Arc::new(*system_clock()),
        }
    }

    /// A limiter over an injected backend and clock (tests).
    #[must_use]
    pub fn with_backend(backend: Arc<dyn ScriptBackend>, clock: Arc<dyn Clock>) -> Self {
        Self {
            url: Secret::default(),
            backend: Mutex::new(Some(backend)),
            clock,
        }
    }

    async fn backend(&self) -> Result<Arc<dyn ScriptBackend>, String> {
        let mut slot = self.backend.lock().await;
        if let Some(b) = slot.as_ref() {
            return Ok(Arc::clone(b));
        }
        let connected =
            tokio::time::timeout(CONNECT_TIMEOUT, RedisScriptBackend::connect(&self.url))
                .await
                .map_err(|_| "timed out connecting to Redis".to_owned())?
                .map_err(|e| e.to_string())?;
        let b: Arc<dyn ScriptBackend> = Arc::new(connected);
        *slot = Some(Arc::clone(&b));
        Ok(b)
    }

    async fn take(&self, key: String, limit: i64) -> Result<Decision, Problem> {
        #[allow(clippy::cast_precision_loss)]
        let rate = limit as f64 / SECONDS_PER_MINUTE;
        let moment = self.clock.monotonic();
        let args = [
            limit.to_string(),
            repr_float(rate),
            repr_float(moment),
            "1".to_owned(),
        ];
        let outcome = match self.backend().await {
            Ok(b) => b
                .eval(TAKE_SCRIPT, &[key], &args)
                .await
                .map_err(|e| e.to_string()),
            Err(e) => Err(e),
        };
        let raw = match outcome {
            Ok(raw) => raw,
            Err(e) => {
                // Drop a broken connection so the next request reconnects.
                *self.backend.lock().await = None;
                logging::error(
                    "baskfy_api.ratelimit",
                    "rate limiter unavailable",
                    &[("error", &e)],
                );
                return Err(Problem::new(
                    ProblemType::PipelineDegraded,
                    "Rate limiting is unavailable, so the request was refused.",
                ));
            }
        };
        let (granted, wait) = decode(&raw).map_err(|e| {
            logging::error("baskfy_api.app", "unhandled error", &[("error", &e)]);
            crate::problems::internal_error()
        })?;
        #[allow(clippy::cast_possible_truncation)]
        let retry = wait.ceil().max(1.0) as i64;
        Ok(Decision {
            allowed: granted,
            limit_per_minute: limit,
            retry_after_seconds: retry,
        })
    }

    /// `check(principal, client_ip, auth_endpoint=, webhook=)`.
    ///
    /// # Errors
    /// 503 `pipeline-degraded` when Redis cannot answer.
    pub async fn check(
        &self,
        principal: &Principal,
        client_ip: Option<&str>,
        auth_endpoint: bool,
        webhook: bool,
        settings: &Settings,
    ) -> Result<Decision, Problem> {
        let (limit, key) = if webhook {
            (
                settings.rate_limit_webhook_per_minute,
                format!("{KEY_PREFIX}webhook:{}", client_ip.unwrap_or("unknown")),
            )
        } else if auth_endpoint {
            (
                settings.rate_limit_auth_per_minute,
                auth_bucket_key(client_ip),
            )
        } else {
            (
                limit_for(principal, settings),
                bucket_key(principal, client_ip),
            )
        };
        self.take(key, limit).await
    }

    /// `check_key(prefix, limit_per_minute)`: the per-API-key bucket.
    ///
    /// # Errors
    /// As [`RateLimiter::check`].
    pub async fn check_key(
        &self,
        prefix: &str,
        limit_per_minute: i64,
    ) -> Result<Decision, Problem> {
        self.take(format!("{KEY_PREFIX}apikey:{prefix}"), limit_per_minute)
            .await
    }

    /// `enforce(...)`: [`RateLimiter::check`], then 429 `rate-limited` when refused.
    ///
    /// # Errors
    /// 429 or 503.
    pub async fn enforce(
        &self,
        principal: &Principal,
        client_ip: Option<&str>,
        path: &str,
        settings: &Settings,
    ) -> Result<Decision, Problem> {
        let decision = self
            .check(
                principal,
                client_ip,
                is_auth_path(path),
                is_webhook_path(path),
                settings,
            )
            .await?;
        if !decision.allowed {
            return Err(rate_limited(
                decision.retry_after_seconds,
                decision.limit_per_minute,
            ));
        }
        Ok(decision)
    }
}

/// `_client_ip(request)`: the left-most `X-Forwarded-For` hop, else the peer address.
#[must_use]
pub fn client_ip(
    forwarded_for: Option<&str>,
    peer: Option<std::net::SocketAddr>,
) -> Option<String> {
    if let Some(f) = forwarded_for
        && !f.is_empty()
    {
        let first = f.split(',').next().unwrap_or("").trim();
        if !first.is_empty() {
            return Some(first.to_owned());
        }
    }
    peer.map(|p| p.ip().to_string())
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn keys_and_paths_are_pythons() {
        let anon = Principal::anonymous();
        assert_eq!(
            bucket_key(&anon, Some("1.2.3.4")),
            "ratelimit:api:ip:1.2.3.4"
        );
        assert_eq!(bucket_key(&anon, None), "ratelimit:api:ip:unknown");
        let user = Principal {
            public_id: Some("abc".into()),
            kind: PrincipalKind::User,
            ..Principal::anonymous()
        };
        assert_eq!(bucket_key(&user, Some("1.2.3.4")), "ratelimit:api:user:abc");
        assert_eq!(auth_bucket_key(None), "ratelimit:auth:ip:unknown");
        assert!(is_auth_path("/api/v1/auth/google"));
        assert!(!is_auth_path("/api/v1/me"));
        assert!(is_webhook_path("/api/v1/webhooks/razorpay"));
    }

    #[test]
    fn the_forwarded_hop_wins_over_the_peer() {
        let peer: std::net::SocketAddr = "10.0.0.1:5000".parse().unwrap();
        assert_eq!(
            client_ip(Some(" 9.9.9.9 , 10.0.0.1"), Some(peer)).as_deref(),
            Some("9.9.9.9")
        );
        assert_eq!(client_ip(Some(""), Some(peer)).as_deref(), Some("10.0.0.1"));
        assert_eq!(client_ip(None, None), None);
    }

    #[test]
    fn the_reply_decodes_as_redis_sends_it() {
        let raw = ScriptValue::Array(vec![
            ScriptValue::Int(0),
            ScriptValue::Bytes(b"5.5".to_vec()),
        ]);
        assert_eq!(decode(&raw).unwrap(), (false, 5.5));
        assert!(decode(&ScriptValue::Nil).is_err());
    }
}
