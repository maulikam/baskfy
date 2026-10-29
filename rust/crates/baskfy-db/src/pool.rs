//! The process's one pool. Ports `baskfy_api.db.create_engine`.
//!
//! Python's engine, field for field (`create_async_engine(..., pool_pre_ping=True,
//! pool_size=db_pool_size, max_overflow=db_max_overflow, pool_timeout=db_pool_timeout_seconds,
//! pool_recycle=db_pool_recycle_seconds, connect_args={"statement_cache_size": ...})`):
//!
//! | SQLAlchemy / asyncpg | here |
//! |---|---|
//! | `pool_size + max_overflow` connections at peak | `max_connections` |
//! | `pool_timeout` — seconds to wait for a free connection | `acquire_timeout` |
//! | `pool_recycle` — retire a connection older than this | `max_lifetime` |
//! | `pool_pre_ping=True` | `test_before_acquire(true)` |
//! | asyncpg `statement_cache_size` | `statement_cache_capacity` |
//! | the engine connects lazily (startup needs no database) | `connect_lazy_with` |
//!
//! **Statement timeout: none, as in Python.** `db.py` sets neither `statement_timeout` nor
//! asyncpg's `command_timeout`, so a statement runs until the server's own setting stops it. The
//! port matches that rather than inventing a bound the Python service does not have; adding one
//! would change which requests fail ([`STATEMENT_TIMEOUT`]).
//!
//! Two differences, both deliberate (DECISIONS-RS R5.2): every connection is **read-only**
//! ([`READ_ONLY`]), and an idle connection is never closed early (SQLAlchemy's `QueuePool` keeps
//! up to `pool_size` idle connections indefinitely; sqlx's default would close them after ten
//! minutes).

use std::str::FromStr;
use std::time::Duration;

use baskfy_providers::secret::Secret;
use sqlx::postgres::{PgConnectOptions, PgPool, PgPoolOptions};
use sqlx::{Postgres, Transaction};

use crate::error::DbError;

/// Every connection this crate opens refuses writes. A constant, not a setting: turning it off is
/// a code change with its own DECISIONS-RS entry, made one table at a time.
pub const READ_ONLY: bool = true;

/// `statement_timeout` sent at connect. `None`: Python sets none (see the module docs).
pub const STATEMENT_TIMEOUT: Option<Duration> = None;

/// What a Rust process reports as `application_name`, so an operator can tell it from Python's
/// connections in `pg_stat_activity`.
pub const APPLICATION_NAME: &str = "baskfy-rs";

/// The `BASKFY_DB_*` settings `create_engine` reads, plus the URL.
#[derive(Debug, Clone, PartialEq)]
pub struct DbSettings {
    /// `BASKFY_DATABASE_URL`, SQLAlchemy spelling allowed (`postgresql+asyncpg://...`).
    pub url: Secret,
    /// `db_pool_size` (> 0).
    pub pool_size: u32,
    /// `db_max_overflow` (>= 0).
    pub max_overflow: u32,
    /// `db_pool_timeout_seconds` (> 0).
    pub pool_timeout_seconds: f64,
    /// `db_pool_recycle_seconds` (> 0).
    pub pool_recycle_seconds: u64,
    /// `db_statement_cache_size` (>= 0; 0 disables the cache, the pgbouncer setting).
    pub statement_cache_size: usize,
}

impl DbSettings {
    /// Python's defaults over `url`.
    #[must_use]
    pub fn with_url(url: Secret) -> Self {
        Self {
            url,
            pool_size: 10,
            max_overflow: 10,
            pool_timeout_seconds: 5.0,
            pool_recycle_seconds: 1800,
            statement_cache_size: 100,
        }
    }
}

/// `postgresql+asyncpg://...` → `postgresql://...`: SQLAlchemy names the driver in the scheme;
/// libpq-style URLs (what sqlx parses) do not. Any `+driver` suffix is dropped.
#[must_use]
pub fn normalise_url(url: &str) -> String {
    match url.split_once("://") {
        Some((scheme, rest)) => {
            let base = scheme.split('+').next().unwrap_or(scheme);
            format!("{base}://{rest}")
        }
        None => url.to_owned(),
    }
}

/// The connect options for `settings`: read-only, the statement cache, the application name.
///
/// # Errors
/// [`DbError::Config`] when the URL does not parse (the URL itself is never in the message).
pub fn connect_options(settings: &DbSettings) -> Result<PgConnectOptions, DbError> {
    let url = normalise_url(settings.url.expose());
    let options = PgConnectOptions::from_str(&url).map_err(|_| {
        DbError::Config(
            "BASKFY_DATABASE_URL does not parse (not shown: it may carry a password)".to_owned(),
        )
    })?;
    let mut startup: Vec<(&str, String)> = Vec::new();
    if READ_ONLY {
        startup.push(("default_transaction_read_only", "on".to_owned()));
    }
    if let Some(timeout) = STATEMENT_TIMEOUT {
        startup.push(("statement_timeout", timeout.as_millis().to_string()));
    }
    Ok(options
        .application_name(APPLICATION_NAME)
        .statement_cache_capacity(settings.statement_cache_size)
        .options(startup))
}

/// The pool, connecting lazily. Ports `create_engine` + `session_factory`.
#[derive(Debug, Clone)]
pub struct Db {
    pool: PgPool,
}

impl Db {
    /// Builds the pool without connecting: like SQLAlchemy's engine, the first query opens the
    /// first connection, so a process starts (and answers `/health`) with the database down.
    ///
    /// # Errors
    /// [`DbError::Config`] for an unusable URL or pool size.
    pub fn connect_lazy(settings: &DbSettings) -> Result<Self, DbError> {
        if settings.pool_size == 0 {
            return Err(DbError::Config(
                "db_pool_size must be greater than 0".to_owned(),
            ));
        }
        if !(settings.pool_timeout_seconds.is_finite() && settings.pool_timeout_seconds > 0.0) {
            return Err(DbError::Config(
                "db_pool_timeout_seconds must be greater than 0".to_owned(),
            ));
        }
        let options = connect_options(settings)?;
        let pool = PgPoolOptions::new()
            .max_connections(settings.pool_size.saturating_add(settings.max_overflow))
            .min_connections(0)
            .acquire_timeout(Duration::from_secs_f64(settings.pool_timeout_seconds))
            .max_lifetime(Some(Duration::from_secs(settings.pool_recycle_seconds)))
            .idle_timeout(None)
            .test_before_acquire(true)
            .connect_lazy_with(options);
        Ok(Self { pool })
    }

    /// The sqlx pool, for queries.
    #[must_use]
    pub fn pool(&self) -> &PgPool {
        &self.pool
    }

    /// One transaction on one connection — Python's request-scoped session, for a handler whose
    /// reads must share a connection. Read-only like every other.
    ///
    /// # Errors
    /// [`DbError::Unavailable`] when no connection can be had.
    pub async fn begin(&self) -> Result<Transaction<'static, Postgres>, DbError> {
        Ok(self.pool.begin().await?)
    }

    /// `SELECT 1`.
    ///
    /// # Errors
    /// The failure, when the database cannot answer.
    pub async fn ping(&self) -> Result<(), DbError> {
        sqlx::query("SELECT 1").execute(&self.pool).await?;
        Ok(())
    }

    /// Closes every connection (`engine.dispose()`).
    pub async fn close(&self) {
        self.pool.close().await;
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn the_sqlalchemy_driver_suffix_is_dropped() {
        assert_eq!(
            normalise_url("postgresql+asyncpg://u:p@h:1/d"),
            "postgresql://u:p@h:1/d"
        );
        assert_eq!(normalise_url("postgres://u@h/d"), "postgres://u@h/d");
        assert_eq!(normalise_url("not a url"), "not a url");
    }

    #[test]
    fn a_bad_url_never_reaches_the_message() {
        let settings = DbSettings::with_url(Secret::new("postgresql://u:hunter2@[bad/d"));
        let message = connect_options(&settings).unwrap_err().to_string();
        assert!(!message.contains("hunter2"), "{message}");
    }

    #[test]
    fn pythons_defaults() {
        let s = DbSettings::with_url(Secret::new("postgresql://h/d"));
        assert_eq!((s.pool_size, s.max_overflow), (10, 10));
        assert!((s.pool_timeout_seconds - 5.0).abs() < f64::EPSILON);
        assert_eq!(
            (s.pool_recycle_seconds, s.statement_cache_size),
            (1800, 100)
        );
    }

    #[tokio::test]
    async fn the_pool_does_not_connect_until_asked() {
        // Port 9 (discard) on loopback: nothing listens, and nothing is dialled at build time.
        let s = DbSettings::with_url(Secret::new("postgresql://u:p@127.0.0.1:9/d"));
        let db = Db::connect_lazy(&s).unwrap();
        assert_eq!(db.pool().size(), 0);
    }
}
