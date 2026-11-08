//! `baskfy_api.security`'s two primitives the session routes use: an opaque token and its digest.
//! (Argon2id, OTP codes and password policy are not ported: no route that uses them exists any
//! more — Google sign-in replaced them, M46.)

use base64::Engine;
use sha2::{Digest, Sha256};

/// `OPAQUE_TOKEN_BYTES`: 256 bits.
pub const OPAQUE_TOKEN_BYTES: usize = 32;

/// `new_opaque_token()`: `secrets.token_urlsafe(32)` — 32 random bytes, base64url, no padding
/// (43 characters).
#[must_use]
pub fn new_opaque_token() -> String {
    let bytes: [u8; OPAQUE_TOKEN_BYTES] = rand::random();
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes)
}

/// `digest(value)`: SHA-256 of the UTF-8 bytes, lower-case hex — how a token is stored.
#[must_use]
pub fn digest(value: &str) -> String {
    Sha256::digest(value.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// `secrets.token_hex(n)`.
#[must_use]
pub fn token_hex(n: usize) -> String {
    (0..n)
        .map(|_| format!("{:02x}", rand::random::<u8>()))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_digest_is_pythons() {
        // hashlib.sha256("rs-parity-refresh-owner-0001".encode()).hexdigest()
        assert_eq!(
            digest("abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn an_opaque_token_is_43_url_safe_characters() {
        let t = new_opaque_token();
        assert_eq!(t.len(), 43);
        assert!(
            t.bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
        );
        assert_eq!(token_hex(16).len(), 32);
    }
}
