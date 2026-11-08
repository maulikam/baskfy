//! Lane **exec_kite**: `KITE_TOKEN_ENCRYPTION_KEY` set only in `.env` reaches the token store.
//!
//! Python's `app.config` runs `load_dotenv()`, so `token_store.store_for`'s
//! `os.getenv("KITE_TOKEN_ENCRYPTION_KEY")` sees a key written only in `.env`. Wave D fed the
//! binary's `--env-file` to `Settings` alone, so the Rust store fell back to a generated key and
//! read Python's token as logged out. `main.rs` now hands the file to
//! `token_store::install_dotenv`; this test does what `main.rs` does. Its own binary, because the
//! installed key is process-wide and would change the fallback tests in `tests/exec_kite.rs`.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::BTreeMap;

use baskfy_desk::lanes::exec_core::exec_kite::token_store;
use baskfy_desk::settings::dotenv_lookup;
use baskfy_providers::secret::Secret;
use baskfy_providers::tokens::AccessTokenStore;

#[test]
fn a_key_set_only_in_dotenv_encrypts_the_token_and_no_fallback_key_is_written() {
    if std::env::var_os(token_store::KEY_VAR).is_some() {
        // The process environment wins over `.env` (load_dotenv never overrides); this test is
        // about the `.env`-only case, so it has nothing to show in an environment that sets it.
        return;
    }
    let key = token_store::generate_key();
    let dotenv: BTreeMap<String, String> = dotenv_lookup(&format!(
        "KITE_API_KEY=k\nKITE_TOKEN_ENCRYPTION_KEY={key}\n"
    ));
    token_store::install_dotenv(&dotenv);

    let dir = tempfile::tempdir().unwrap();
    let token = dir.path().join(".kite_token.json");
    let store = token_store::store_for(&token, "").unwrap();
    store.save("tok-python-wrote", None).unwrap();
    assert!(
        !token_store::key_path(&token).exists(),
        "the .env key was ignored and a fallback key was generated"
    );
    // The token Python wrote with that key reads back with it — the desk is not logged out.
    let python_side = AccessTokenStore::new(&token, Secret::new(key));
    assert_eq!(
        python_side.load().unwrap().value.expose(),
        "tok-python-wrote"
    );
    // A configured key still wins over the environment's.
    let other = token_store::generate_key();
    let configured = token_store::store_for(&token, &other).unwrap();
    assert!(
        configured.load().is_err(),
        "a different key must not decrypt"
    );
}
