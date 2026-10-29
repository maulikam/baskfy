//! The crate-wide error type. Python raises `ValueError` on bad input; Rust returns
//! [`CoreError`]. No function in this crate panics on input (house rule 3).

use thiserror::Error;

/// Errors a pure core function can return.
#[derive(Debug, Error)]
pub enum CoreError {
    /// Input violates the function's contract (Python's `ValueError`).
    #[error("invalid input: {0}")]
    InvalidInput(String),
    /// A required column is missing from an input frame.
    #[error("missing column: {0}")]
    MissingColumn(String),
    /// A decimal operation overflowed or divided by zero.
    #[error("decimal arithmetic: {0}")]
    Arithmetic(String),
    /// An error raised by polars while evaluating a frame.
    #[error(transparent)]
    Polars(#[from] polars::error::PolarsError),
}

/// `Result` with [`CoreError`].
pub type CoreResult<T> = Result<T, CoreError>;
