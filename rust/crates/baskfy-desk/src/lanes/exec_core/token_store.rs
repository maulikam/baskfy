//! Ports `app/token_store.py`: the daily Kite access token, Fernet-encrypted at rest through
//! the screener's store (`baskfy_providers::tokens::AccessTokenStore`), so the merged system
//! keeps a Kite token exactly one way.
//!
//! **On the key, honestly.** `KITE_TOKEN_ENCRYPTION_KEY` (config, then the environment) is the
//! real protection. Without it a key is generated once and written beside the token at 0600
//! (`<token>.key`): encryption against disclosure-by-copy (backups, syncs, snapshots), not
//! against a local attacker who can read the key too. Set the key and the fallback stops.

use std::path::{Path, PathBuf};

use base64::Engine as _;
use baskfy_providers::secret::Secret;
use baskfy_providers::tokens::AccessTokenStore;
use rand::RngCore as _;

/// `_FALLBACK_KEY_SUFFIX`.
pub const FALLBACK_KEY_SUFFIX: &str = ".key";

/// `Fernet.generate_key()`: 32 random bytes, URL-safe base64.
#[must_use]
pub fn generate_key() -> String {
    let mut bytes = [0_u8; 32];
    rand::rng().fill_bytes(&mut bytes);
    base64::engine::general_purpose::URL_SAFE.encode(bytes)
}

/// Where the fallback key lives: `token_path.with_suffix(token_path.suffix + ".key")`.
#[must_use]
pub fn key_path(token_path: &Path) -> PathBuf {
    let mut name = token_path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    name.push_str(FALLBACK_KEY_SUFFIX);
    token_path.with_file_name(name)
}

/// `_bootstrap_key(token_path)`: read the key beside the token, or generate one at 0600.
///
/// # Errors
/// The key file cannot be read or written.
pub fn bootstrap_key(token_path: &Path) -> std::io::Result<String> {
    let path = key_path(token_path);
    if path.is_file() {
        return Ok(std::fs::read_to_string(&path)?.trim().to_owned());
    }
    let key = generate_key();
    if let Some(parent) = path.parent()
        && !parent.as_os_str().is_empty()
    {
        std::fs::create_dir_all(parent)?;
    }
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt as _;
        options.mode(0o600);
    }
    let mut f = options.open(&path)?;
    std::io::Write::write_all(&mut f, key.as_bytes())?;
    Ok(key)
}

/// `store_for(token_path, configured_key)`: the encrypted store for this desk's token. The
/// environment's `KITE_TOKEN_ENCRYPTION_KEY` is read at call time (a function-level read).
///
/// # Errors
/// The fallback key cannot be read or written.
pub fn store_for(token_path: &Path, configured_key: &str) -> std::io::Result<AccessTokenStore> {
    let env_key = std::env::var("KITE_TOKEN_ENCRYPTION_KEY").unwrap_or_default();
    let key = if !configured_key.is_empty() {
        configured_key.to_owned()
    } else if !env_key.is_empty() {
        env_key
    } else {
        bootstrap_key(token_path)?
    };
    Ok(AccessTokenStore::new(token_path, Secret::new(key)))
}
