//! What every handler shares: `app.state` in Python.

use std::sync::Arc;

use baskfy_db::{Db, WriteDb};

use crate::ratelimit::RateLimiter;
use crate::settings::Settings;

/// The read-write pool, held where only [`crate::session`] reads it (DECISIONS-RS R6.2).
#[derive(Debug, Clone, Default)]
pub struct WriteAccess(Option<WriteDb>);

impl WriteAccess {
    /// No write pool: every write route answers the 500 a missing session gives (tests that need
    /// no database build their state with this).
    #[must_use]
    pub fn none() -> Self {
        Self(None)
    }

    /// A write pool over `settings`, connecting lazily.
    ///
    /// # Errors
    /// An unusable database URL or pool size.
    pub fn from_settings(settings: &Settings) -> Result<Self, baskfy_db::DbError> {
        Ok(Self(Some(WriteDb::connect_lazy(&settings.db_settings())?)))
    }
}

/// `app.state`: the settings the app was built with, the pools, and the limiter.
#[derive(Debug, Clone)]
pub struct AppState {
    /// `app.state.settings`.
    pub settings: Arc<Settings>,
    /// `app.state.engine` / `session_factory` for every read — read-only (baskfy-db).
    pub db: Db,
    /// `app.state.rate_limiter`: `None` when `BASKFY_RATE_LIMIT_ENABLED` is false.
    pub limiter: Option<Arc<RateLimiter>>,
    /// The write pool. Opaque on purpose: handlers reach it only through
    /// [`crate::session::WritePool`], which the write-method layer alone hands out.
    pub write: WriteAccess,
}

impl AppState {
    /// The lifespan's wiring: one lazy read-only pool, one lazy write pool, one limiter when
    /// enabled.
    ///
    /// # Errors
    /// An unusable database URL or pool size.
    pub fn from_settings(settings: Settings) -> Result<Self, baskfy_db::DbError> {
        let db = Db::connect_lazy(&settings.db_settings())?;
        let write = WriteAccess::from_settings(&settings)?;
        // Python builds the limiter only when `_open_cache` returned a client
        // (`RateLimiter(cache, settings) if cache is not None and ...`). With no client there is
        // no limiter and requests go unmetered, after one error line (R6.40).
        let cache_opens = redis_py_accepts(settings.redis_url.expose());
        if settings.rate_limit_enabled && !cache_opens {
            crate::logging::error(
                "baskfy_api.app",
                "rate limiting is enabled but Redis is unavailable; requests will be refused",
                &[],
            );
        }
        let limiter = (settings.rate_limit_enabled && cache_opens)
            .then(|| Arc::new(RateLimiter::new(settings.redis_url.clone())));
        Ok(Self {
            settings: Arc::new(settings),
            db,
            limiter,
            write,
        })
    }

    /// The write pool, for [`crate::session::write_access`] only.
    pub(crate) fn write_db(&self) -> Option<&WriteDb> {
        self.write.0.as_ref()
    }

    /// Drops the write pool (tests of the no-pool path).
    #[cfg(test)]
    pub(crate) fn clear_write_db(&mut self) {
        self.write = WriteAccess::none();
    }
}

/// Whether `redis.Redis.from_url(url)` returns a client rather than raising `ValueError`:
/// redis-py's `parse_url` first requires one of three literal, case-sensitive prefixes. Its later
/// `ValueError`s (a non-numeric port or db path, a bad query argument) are not reproduced (R6.40).
fn redis_py_accepts(url: &str) -> bool {
    ["redis://", "rediss://", "unix://"]
        .iter()
        .any(|prefix| url.starts_with(prefix))
}

#[cfg(test)]
mod redis_url_tests {
    use super::redis_py_accepts;

    /// Expected answers probed from redis-py's `Redis.from_url` in the API venv.
    #[test]
    fn the_cache_opens_exactly_where_redis_py_accepts_the_url() {
        for (url, opens) in [
            ("", false),
            ("REDIS://h:1/0", false),
            ("redis:/x", false),
            (" redis://h", false),
            ("http://h", false),
            ("redis+x://h", false),
            ("redis", false),
            ("unix:///tmp/s", true),
            ("rediss://h", true),
            ("redis://127.0.0.1:56379/0", true),
        ] {
            assert_eq!(redis_py_accepts(url), opens, "{url:?}");
        }
    }
}
