//! Load the committed reference CSV — law 1 keeps this out of `baskfy_core` (AF 3.10). Ports
//! `baskfy_providers.reference_export_io`.
//!
//! The path half is ported. `reference_rows()` also needs `baskfy_core.reference_export`'s
//! `read_export` and `to_rows`, which the Rust core has not ported yet; until it does,
//! [`reference_export_bytes`] hands the file's bytes to whoever parses them (STATUS.md).

use std::path::PathBuf;

use crate::errors::ProviderError;

/// The export's file name under `decile-blueprint/tests/fixtures/`.
pub const REFERENCE_EXPORT_FILE: &str = "reference-screen-export-2026-08-18.csv";

/// `REFERENCE_EXPORT`: Python resolves `parents[4]` of its own file (the `decile-blueprint`
/// tree); Rust resolves the same file from this crate's manifest directory at build time.
#[must_use]
pub fn reference_export() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../../decile-blueprint/tests/fixtures")
        .join(REFERENCE_EXPORT_FILE)
}

/// `reference_export_path()`: the export, if it is there.
///
/// # Errors
/// Python's `FileNotFoundError` (as a foreign error naming the path) when it is not.
pub fn reference_export_path() -> Result<PathBuf, ProviderError> {
    let path = reference_export();
    if !path.is_file() {
        return Err(ProviderError::foreign(
            "FileNotFoundError",
            path.display().to_string(),
        ));
    }
    Ok(path)
}

/// The export's raw bytes (UTF-8 with BOM, docs/13 §1) — the input `read_export` parses.
///
/// # Errors
/// `FileNotFoundError` as above; `OSError` when it cannot be read.
pub fn reference_export_bytes() -> Result<Vec<u8>, ProviderError> {
    let path = reference_export_path()?;
    std::fs::read(&path).map_err(|e| ProviderError::foreign("OSError", e.to_string()))
}
