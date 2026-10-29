//! What every handler shares: `app.state` in Python.

use std::sync::Arc;

use baskfy_db::Db;

use crate::ratelimit::RateLimiter;
use crate::settings::Settings;

/// `app.state`: the settings the app was built with, the pool, and the limiter.
#[derive(Debug, Clone)]
pub struct AppState {
    /// `app.state.settings`.
    pub settings: Arc<Settings>,
    /// `app.state.engine` / `session_factory` — read-only (baskfy-db).
    pub db: Db,
    /// `app.state.rate_limiter`: `None` when `BASKFY_RATE_LIMIT_ENABLED` is false.
    pub limiter: Option<Arc<RateLimiter>>,
}

impl AppState {
    /// The lifespan's wiring: one lazy pool, one limiter when enabled.
    ///
    /// # Errors
    /// An unusable database URL or pool size.
    pub fn from_settings(settings: Settings) -> Result<Self, baskfy_db::DbError> {
        let db = Db::connect_lazy(&settings.db_settings())?;
        let limiter = settings
            .rate_limit_enabled
            .then(|| Arc::new(RateLimiter::new(settings.redis_url.clone())));
        Ok(Self {
            settings: Arc::new(settings),
            db,
            limiter,
        })
    }
}
