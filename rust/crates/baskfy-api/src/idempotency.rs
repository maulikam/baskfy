//! `Idempotency-Key` support — ports `baskfy_api.idempotency` (docs/07 §Conventions: "honoured on
//! all POSTs that create resources"), ready for the wave-4c routes that create (`POST /screens`,
//! `POST /screens/{id}/duplicate`). No route ported so far creates a resource with it: the
//! session routes do not take the header in Python either.
//!
//! The record is `idempotency:api:<scope>:<key[:255]>` in Redis, holding the `public_id` the first
//! attempt created. [`reserve`] claims the key atomically (`SET … NX`, M45.6) with [`PENDING`] for
//! [`RESERVATION_TTL_SECONDS`]; [`remember`] replaces it with the real value for a day;
//! [`release`] gives it back (compare-and-delete) when the work did not happen. Redis being
//! unavailable degrades to **no** idempotency, never a refused write — every failure is logged
//! and treated as "reserved" / "nothing stored", as Python does.
//!
//! One deliberate difference, recorded (DECISIONS-RS R6.8): Python's `reserve` is `SET NX` then a
//! separate `GET`; here both run in one script, so the rare "it expired between the two" case
//! answers as the key's state at that instant instead of reserving. The outcomes a caller can
//! observe are the same three.

use baskfy_providers::ratelimit::ScriptValue;
use baskfy_providers::secret::Secret;

use crate::cache;
use crate::logging;

/// `KEY_PREFIX`.
pub const KEY_PREFIX: &str = "idempotency:api:";
/// `TTL_SECONDS`: a day.
pub const TTL_SECONDS: u64 = 24 * 60 * 60;
/// `HEADER`.
pub const HEADER: &str = "Idempotency-Key";
/// `MAX_KEY_LENGTH`.
pub const MAX_KEY_LENGTH: usize = 255;
/// `PENDING`: what a reservation holds until the real value replaces it.
pub const PENDING: &str = "-pending-";
/// `RESERVATION_TTL_SECONDS`.
pub const RESERVATION_TTL_SECONDS: u64 = 120;

/// `SET NX EX` and, when the key was taken, what it holds.
const RESERVE_SCRIPT: &str = "if redis.call('set', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) then \
     return {1} end return {0, redis.call('get', KEYS[1])}";
/// `_RELEASE_SCRIPT`, verbatim.
const RELEASE_SCRIPT: &str = "\nif redis.call('get', KEYS[1]) == ARGV[1] then\n    return redis.call('del', KEYS[1])\nend\nreturn 0\n";
/// `SET key value EX ttl`.
const REMEMBER_SCRIPT: &str = "return redis.call('set', KEYS[1], ARGV[1], 'EX', ARGV[2])";
/// `GET key`.
const REPLAY_SCRIPT: &str = "return redis.call('get', KEYS[1])";

/// `Outcome` / `Reservation`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Reservation {
    /// `RESERVED`: this caller owns the key and should do the work.
    Reserved,
    /// `REPLAY`: a previous request with this key finished; what it made.
    Replay(String),
    /// `IN_FLIGHT`: another request holds the key right now.
    InFlight,
}

/// `_redis_key(scope, key[:MAX_KEY_LENGTH])`. The header arrives as Latin-1 text (Starlette) and
/// redis-py encodes the key as UTF-8; the slice counts characters, as Python's does.
#[must_use]
pub fn redis_key(scope: &str, key: &str) -> String {
    let cut: String = key.chars().take(MAX_KEY_LENGTH).collect();
    format!("{KEY_PREFIX}{scope}:{cut}")
}

fn text(v: &ScriptValue) -> Option<String> {
    match v {
        ScriptValue::Bytes(b) => Some(String::from_utf8_lossy(b).into_owned()),
        ScriptValue::Int(i) => Some(i.to_string()),
        _ => None,
    }
}

/// `reserve(client, scope, key)`.
pub async fn reserve(url: &Secret, scope: &str, key: Option<&str>) -> Reservation {
    let Some(key) = key.filter(|k| !k.is_empty()) else {
        return Reservation::Reserved;
    };
    let reply = cache::eval(
        url,
        RESERVE_SCRIPT,
        &[redis_key(scope, key)],
        &[PENDING.to_owned(), RESERVATION_TTL_SECONDS.to_string()],
    )
    .await;
    match reply {
        Ok(ScriptValue::Array(items)) => match items.as_slice() {
            [ScriptValue::Int(1)] => Reservation::Reserved,
            [ScriptValue::Int(0), stored] => match text(stored) {
                None => Reservation::Reserved,
                Some(v) if v == PENDING => Reservation::InFlight,
                Some(v) => Reservation::Replay(v),
            },
            [ScriptValue::Int(0)] => Reservation::Reserved,
            _ => Reservation::Reserved,
        },
        Ok(_) => Reservation::Reserved,
        Err(e) => {
            logging::warning(
                "baskfy_api.idempotency",
                "idempotency reservation failed",
                &[("error", &e)],
            );
            Reservation::Reserved
        }
    }
}

/// `release(client, scope, key)`: give back a reservation this caller did not complete.
pub async fn release(url: &Secret, scope: &str, key: Option<&str>) {
    let Some(key) = key.filter(|k| !k.is_empty()) else {
        return;
    };
    if let Err(e) = cache::eval(
        url,
        RELEASE_SCRIPT,
        &[redis_key(scope, key)],
        &[PENDING.to_owned()],
    )
    .await
    {
        logging::warning(
            "baskfy_api.idempotency",
            "idempotency release failed",
            &[("error", &e)],
        );
    }
}

/// `remember(client, scope, key, value)`.
pub async fn remember(url: &Secret, scope: &str, key: Option<&str>, value: &str) {
    let Some(key) = key.filter(|k| !k.is_empty()) else {
        return;
    };
    if let Err(e) = cache::eval(
        url,
        REMEMBER_SCRIPT,
        &[redis_key(scope, key)],
        &[value.to_owned(), TTL_SECONDS.to_string()],
    )
    .await
    {
        logging::warning(
            "baskfy_api.idempotency",
            "idempotency record failed",
            &[("error", &e)],
        );
    }
}

/// `replay(client, scope, key)`: what a previous request with this key created, if any.
pub async fn replay(url: &Secret, scope: &str, key: Option<&str>) -> Option<String> {
    let key = key.filter(|k| !k.is_empty())?;
    match cache::eval(url, REPLAY_SCRIPT, &[redis_key(scope, key)], &[]).await {
        Ok(v) => text(&v),
        Err(e) => {
            logging::warning(
                "baskfy_api.idempotency",
                "idempotency lookup failed",
                &[("error", &e)],
            );
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_key_is_scoped_and_cut_at_255_characters() {
        assert_eq!(redis_key("user:abc", "k1"), "idempotency:api:user:abc:k1");
        let long = "é".repeat(300);
        let key = redis_key("s", &long);
        assert_eq!(key.chars().count(), KEY_PREFIX.len() + 2 + MAX_KEY_LENGTH);
    }
}
