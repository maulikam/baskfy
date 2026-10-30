//! The worker's error, carrying the Python class a step's failure is reported under.
//!
//! Python records a failed step as `{"type": type(exc).__name__, "message": str(exc), ...}` and
//! the post-publish steps as `skipped_reason = f"{type(exc).__name__}: {exc}"`. Those strings
//! are graded, so every error here answers [`WorkerError::python_type`] with the class Python
//! would have raised, and its `Display` is Python's `str(exc)`.

use baskfy_db::DbError;
use baskfy_providers::ProviderError;

/// Anything a step, the orchestrator or the task around them can fail with.
#[derive(Debug, thiserror::Error)]
pub enum WorkerError {
    /// A step body (or another piece) that its wave-5 lane has not ported yet. Raised by the
    /// declared stubs in `steps/*.rs`; never reached once a lane lands.
    #[error("{what} is not ported to Rust yet")]
    NotPorted {
        /// What is missing (a step name, `catch_up`, ...).
        what: &'static str,
    },
    /// A database failure (Python: an `sqlalchemy.exc.DBAPIError` subclass).
    #[error("{0}")]
    Db(#[from] DbError),
    /// A provider failure, with Python's class (`ProviderError::python_type`).
    #[error("{0}")]
    Provider(#[from] ProviderError),
    /// `LookupError` (`adopt_run` for an id that is not there).
    #[error("{0}")]
    Lookup(String),
    /// A setting that does not parse (pydantic's `ValidationError`).
    #[error("{0}")]
    Settings(String),
    /// Any other Python exception a ported step reproduces: its class name and `str(exc)`.
    #[error("{message}")]
    Python {
        /// `type(exc).__name__`.
        type_name: String,
        /// `str(exc)`.
        message: String,
    },
}

impl WorkerError {
    /// A Python exception by class name and message (`ValueError`, `KeyError`, ...).
    #[must_use]
    pub fn python(type_name: impl Into<String>, message: impl Into<String>) -> Self {
        Self::Python {
            type_name: type_name.into(),
            message: message.into(),
        }
    }

    /// `type(exc).__name__` for this error.
    #[must_use]
    pub fn python_type(&self) -> String {
        match self {
            Self::NotPorted { .. } => "NotPorted".to_owned(),
            Self::Db(e) => db_python_type(e).to_owned(),
            Self::Provider(e) => e.python_type().to_owned(),
            Self::Lookup(_) => "LookupError".to_owned(),
            Self::Settings(_) => "ValidationError".to_owned(),
            Self::Python { type_name, .. } => type_name.clone(),
        }
    }
}

impl From<sqlx::Error> for WorkerError {
    fn from(e: sqlx::Error) -> Self {
        Self::Db(DbError::from(e))
    }
}

/// The SQLAlchemy class asyncpg's error surfaces as, by SQLSTATE class: `IntegrityError` for
/// class 23, `DataError` for 22, `ProgrammingError` for 42, `OperationalError` otherwise —
/// SQLAlchemy's mapping of asyncpg's exceptions. A lane whose step can fail on the database
/// graded by its message checks this mapping against Python before relying on it.
fn db_python_type(e: &DbError) -> &'static str {
    match e.sqlstate().map(|s| s.get(..2).unwrap_or("")) {
        Some("23") => "IntegrityError",
        Some("22") => "DataError",
        Some("42") => "ProgrammingError",
        Some("40") => "OperationalError",
        Some(_) => "DBAPIError",
        None => "OperationalError",
    }
}
