//! The one error this crate returns. No Python twin: SQLAlchemy raises `SQLAlchemyError`
//! subclasses, and the API's catch-all turns every one of them into a 500 `internal-error`.

/// A database failure. The message never carries the connection URL (it holds a password).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum DbError {
    /// The settings could not be turned into connect options (the URL is not shown).
    #[error("database configuration error: {0}")]
    Config(String),
    /// The server refused or failed a statement. `sqlstate` is Postgres's five-character code.
    #[error("database error{}: {message}", .sqlstate.as_deref().map(|s| format!(" [{s}]")).unwrap_or_default())]
    Query {
        /// Postgres's SQLSTATE, when the server sent one (`25006` = read-only transaction).
        sqlstate: Option<String>,
        /// The server's or driver's message.
        message: String,
    },
    /// No connection could be had: refused, timed out waiting for the pool, or closed.
    #[error("database unavailable: {0}")]
    Unavailable(String),
    /// A column did not decode into the requested type.
    #[error("column {column:?} did not decode: {message}")]
    Decode {
        /// The column name.
        column: String,
        /// Why.
        message: String,
    },
}

impl DbError {
    /// Postgres's SQLSTATE, when the failure came from the server.
    #[must_use]
    pub fn sqlstate(&self) -> Option<&str> {
        match self {
            Self::Query { sqlstate, .. } => sqlstate.as_deref(),
            _ => None,
        }
    }
}

impl From<sqlx::Error> for DbError {
    fn from(e: sqlx::Error) -> Self {
        match e {
            sqlx::Error::Database(db) => Self::Query {
                sqlstate: db.code().map(|c| c.into_owned()),
                message: db.message().to_owned(),
            },
            sqlx::Error::PoolTimedOut => {
                Self::Unavailable("timed out waiting for a pooled connection".to_owned())
            }
            sqlx::Error::PoolClosed => Self::Unavailable("the pool is closed".to_owned()),
            sqlx::Error::Io(io) => Self::Unavailable(io.to_string()),
            sqlx::Error::Tls(tls) => Self::Unavailable(format!("tls: {tls}")),
            sqlx::Error::Configuration(c) => Self::Config(c.to_string()),
            sqlx::Error::ColumnDecode { index, source } => Self::Decode {
                column: index,
                message: source.to_string(),
            },
            sqlx::Error::ColumnNotFound(column) => Self::Decode {
                column,
                message: "no such column in the row".to_owned(),
            },
            other => Self::Query {
                sqlstate: None,
                message: other.to_string(),
            },
        }
    }
}
