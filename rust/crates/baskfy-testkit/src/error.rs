//! The one error type of the testkit.

use std::path::PathBuf;

use thiserror::Error;

/// Everything that can go wrong loading or decoding a golden.
#[derive(Debug, Error)]
pub enum TestkitError {
    /// The golden file could not be read.
    #[error("cannot read golden {path}: {source}")]
    Io {
        /// File that failed.
        path: PathBuf,
        /// Underlying error.
        source: std::io::Error,
    },
    /// The golden file is not valid JSON or lacks a required field.
    #[error("malformed golden {path}: {detail}")]
    Malformed {
        /// File that failed.
        path: PathBuf,
        /// What was wrong.
        detail: String,
    },
    /// `cases_for` matched nothing — almost always a typo in the function name.
    #[error("no golden has fn == {0:?}")]
    NoCases(String),
    /// An input key the test asked for is absent.
    #[error("golden {case}: no input named {key:?}")]
    MissingInput {
        /// Case name.
        case: String,
        /// Key asked for.
        key: String,
    },
    /// A value had the wrong JSON shape or could not be parsed as the requested type.
    #[error("decode error at {at}: {detail}")]
    Decode {
        /// Where (a JSON path or a column name).
        at: String,
        /// What was wrong.
        detail: String,
    },
    /// A polars dtype name the testkit does not decode yet.
    #[error("unsupported dtype {0:?} (add it to baskfy-testkit/src/frame.rs)")]
    UnsupportedDtype(String),
    /// The tolerance object has a key golden.py does not define.
    #[error("unsupported tolerance key {0:?}; only \"float\" is defined (golden.py)")]
    UnsupportedTolerance(String),
    /// Encoding an actual output failed.
    #[error("encode error: {0}")]
    Encode(String),
    /// A polars operation failed.
    #[error(transparent)]
    Polars(#[from] polars::error::PolarsError),
}

impl TestkitError {
    pub(crate) fn decode(at: impl Into<String>, detail: impl Into<String>) -> Self {
        Self::Decode {
            at: at.into(),
            detail: detail.into(),
        }
    }
}
