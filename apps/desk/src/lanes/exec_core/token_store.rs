//! Ports `app/token_store.py`: the daily Kite access token, Fernet-encrypted at rest through
//! the screener's store (`baskfy_providers::tokens::AccessTokenStore`), so the merged system
//! keeps a Kite token exactly one way.
//!
//! **On the key, honestly.** `KITE_TOKEN_ENCRYPTION_KEY` (config, then the environment) is the
//! real protection. Without it a key is generated once and written beside the token at 0600
//! (`<token>.key`): encryption against disclosure-by-copy (backups, syncs, snapshots), not
//! against a local attacker who can read the key too. Set the key and the fallback stops.
//!
//! **Where the key comes from** (wave D2): `os.getenv("KITE_TOKEN_ENCRYPTION_KEY")` after
//! `app.config`'s `load_dotenv()` — the process environment, then the desk's `.env` (which never
//! overrides a variable the process already has, even an empty one). The binary hands its
//! `--env-file` to [`install_dotenv`] at boot; without that a key set only in `.env` was invisible
//! here, the store fell back to a generated key, and the token Python wrote read as logged out.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use base64::Engine as _;
use baskfy_providers::secret::Secret;
use baskfy_providers::tokens::AccessTokenStore;
use rand::RngCore as _;

/// The environment variable (a function-level read, `settings::FUNCTION_LEVEL`).
pub const KEY_VAR: &str = "KITE_TOKEN_ENCRYPTION_KEY";

/// The `.env` value of [`KEY_VAR`], installed once at boot.
static DOTENV_KEY: OnceLock<Secret> = OnceLock::new();

/// `load_dotenv()` for this module's one variable: remember the `.env` file's
/// `KITE_TOKEN_ENCRYPTION_KEY` (if it has one) so [`store_for`] sees it under the process
/// environment. Only the first call counts (one `.env` per process, as Python's import).
pub fn install_dotenv(dotenv: &BTreeMap<String, String>) {
    if let Some(value) = dotenv.get(KEY_VAR) {
        let _ = DOTENV_KEY.set(Secret::new(value.clone()));
    }
}

/// `os.getenv(KEY_VAR, "")` after `load_dotenv()`: a variable the process has — even an empty
/// one — wins; otherwise the `.env` value; otherwise `""`.
#[must_use]
pub fn resolve_env_key(process: Option<String>, dotenv: Option<&str>) -> String {
    match process {
        Some(value) => value,
        None => dotenv.unwrap_or_default().to_owned(),
    }
}

fn env_key() -> String {
    let process = std::env::var_os(KEY_VAR).map(|v| v.to_string_lossy().into_owned());
    resolve_env_key(process, DOTENV_KEY.get().map(Secret::expose))
}

/// Python's `OSError` for an I/O failure on `path`: the errno's subclass and
/// `[Errno N] <strerror>: '<path>'`.
#[must_use]
pub fn os_error(e: &std::io::Error, path: &Path) -> (String, String) {
    let Some(errno) = e.raw_os_error() else {
        return ("OSError".to_owned(), e.to_string());
    };
    let python_type = match errno {
        1 | 13 => "PermissionError",
        2 => "FileNotFoundError",
        17 => "FileExistsError",
        20 => "NotADirectoryError",
        21 => "IsADirectoryError",
        _ => "OSError",
    };
    let text = e.to_string();
    let strerror = text
        .strip_suffix(&format!(" (os error {errno})"))
        .unwrap_or(&text);
    (
        python_type.to_owned(),
        format!(
            "[Errno {errno}] {strerror}: {}",
            baskfy_providers::py::repr_str(&path.display().to_string())
        ),
    )
}

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
    warn(&format!(
        "KITE_TOKEN_ENCRYPTION_KEY is not set, so a key was generated at {}. The token is now \
         encrypted at rest against being copied, but the key sits beside it — anything that can \
         read one can read the other. Set KITE_TOKEN_ENCRYPTION_KEY in .env for real separation.",
        path.display()
    ));
    Ok(key)
}

/// `logging.getLogger("token_store").warning`, on stderr (it names the key's path, never the key).
#[allow(clippy::print_stderr)]
fn warn(message: &str) {
    eprintln!("WARNING:token_store:{message}");
}

/// `store_for(token_path, configured_key)`: the encrypted store for this desk's token. The
/// environment's `KITE_TOKEN_ENCRYPTION_KEY` (then `.env`'s) is read at call time (a
/// function-level read).
///
/// # Errors
/// The fallback key cannot be read or written.
pub fn store_for(token_path: &Path, configured_key: &str) -> std::io::Result<AccessTokenStore> {
    let env_key = env_key();
    let key = if !configured_key.is_empty() {
        configured_key.to_owned()
    } else if !env_key.is_empty() {
        env_key
    } else {
        bootstrap_key(token_path)?
    };
    Ok(AccessTokenStore::new(token_path, Secret::new(key)))
}
