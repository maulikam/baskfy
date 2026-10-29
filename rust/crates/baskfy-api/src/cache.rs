//! `app.state.cache`: the process's one Redis client, as the routers use it (`GET`, and the
//! idempotency scripts). Held once (wave 4c's foundation; the derivatives lane had the first
//! copy, R5.19). The API crate has no `redis` dependency of its own: commands go through
//! `baskfy_providers::ratelimit::RedisScriptBackend` as one-line Lua scripts.
//!
//! Python's `Redis.from_url` connects lazily and every command may raise `RedisError`, which every
//! caller treats as "nothing stored" / "do the work anyway". Here a failed connect is not cached:
//! the next call tries again, as redis-py's pool does.

use std::sync::Arc;

use baskfy_providers::ratelimit::{RedisScriptBackend, ScriptBackend, ScriptValue};
use baskfy_providers::secret::Secret;
use tokio::sync::Mutex;

use crate::logging;

/// `redis.call('GET', key)`.
const GET_SCRIPT: &str = "return redis.call('GET', KEYS[1])";

/// The client and the URL it was opened for (one per process in a deployment; a test binary may
/// ask for two).
static CACHE: Mutex<Option<(String, Arc<RedisScriptBackend>)>> = Mutex::const_new(None);

async fn client(url: &Secret) -> Option<Arc<RedisScriptBackend>> {
    let mut slot = CACHE.lock().await;
    if let Some((opened_for, client)) = slot.as_ref()
        && opened_for == url.expose()
    {
        return Some(Arc::clone(client));
    }
    match tokio::time::timeout(
        std::time::Duration::from_secs(5),
        RedisScriptBackend::connect(url),
    )
    .await
    {
        Ok(Ok(client)) => {
            let client = Arc::new(client);
            *slot = Some((url.expose().to_owned(), Arc::clone(&client)));
            Some(client)
        }
        Ok(Err(e)) => {
            logging::warning(
                "baskfy_api.app",
                "redis unavailable",
                &[("error", &e.to_string())],
            );
            None
        }
        Err(_) => None,
    }
}

/// Runs one script: the reply, or the failure's text (`RedisError` in Python).
///
/// # Errors
/// No client could be had, or Redis refused the script.
pub async fn eval(
    url: &Secret,
    script: &'static str,
    keys: &[String],
    args: &[String],
) -> Result<ScriptValue, String> {
    let Some(client) = client(url).await else {
        return Err("redis unavailable".to_owned());
    };
    client
        .eval(script, keys, args)
        .await
        .map_err(|e| e.to_string())
}

/// `await cache.get(key)` as the routers use it: the stored bytes, or `None` when the key is
/// absent **or** Redis failed (every caller treats a `RedisError` as "nothing stored").
pub async fn cache_get(url: &Secret, key: &str) -> Option<Vec<u8>> {
    match eval(url, GET_SCRIPT, &[key.to_owned()], &[]).await {
        Ok(ScriptValue::Bytes(b)) => Some(b),
        Ok(_) => None,
        Err(e) => {
            logging::warning("baskfy_api.app", "redis read failed", &[("error", &e)]);
            None
        }
    }
}
