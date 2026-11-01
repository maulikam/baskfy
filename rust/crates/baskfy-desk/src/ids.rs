//! `uuid.uuid4()` as a seam (DECISIONS-RS R8.4).
//!
//! The weekly plan's id is `uuid.uuid4().hex[:12]` (`baskfy_core.basket.build_plan`), the
//! gateway's fallback client id `uuid.uuid4().hex[:10]`, and the sleeves mint a few more. In
//! production they are random, so two correct desks never agree on them. Under `desk_diff.py`
//! both desks draw from [`HarnessIds`] instead — `tools/parity/desk_harness.py` patches
//! Python's `uuid.uuid4` with the same sequence — so a plan id, and every byte that depends on
//! it, is comparable. The sequence is `SHA-256("baskfy-desk-harness:<seed>:<n>")[:16]` with the
//! RFC 4122 version-4 and variant bits set, `n` counting from 0 per process.

use std::sync::atomic::{AtomicU64, Ordering};

use sha2::{Digest, Sha256};

/// Where the desk gets its uuid4s.
pub trait IdSource: Send + Sync {
    /// `uuid.uuid4().bytes`.
    fn uuid4_bytes(&self) -> [u8; 16];

    /// `uuid.uuid4().hex`: 32 lowercase hex digits.
    fn uuid4_hex(&self) -> String {
        hex(&self.uuid4_bytes())
    }
}

fn hex(bytes: &[u8]) -> String {
    use std::fmt::Write as _;
    bytes
        .iter()
        .fold(String::with_capacity(bytes.len() * 2), |mut s, b| {
            let _ = write!(s, "{b:02x}");
            s
        })
}

fn set_v4_bits(mut b: [u8; 16]) -> [u8; 16] {
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    b
}

/// The production source: 122 random bits, as `os.urandom(16)` gives Python.
#[derive(Debug, Default, Clone, Copy)]
pub struct RandomIds;

impl IdSource for RandomIds {
    fn uuid4_bytes(&self) -> [u8; 16] {
        set_v4_bits(rand::random::<[u8; 16]>())
    }
}

/// The harness's deterministic sequence (see the module docs).
#[derive(Debug)]
pub struct HarnessIds {
    seed: String,
    next: AtomicU64,
}

impl HarnessIds {
    /// A sequence keyed by `seed` (the harness passes the same seed to both desks).
    #[must_use]
    pub fn new(seed: impl Into<String>) -> Self {
        Self {
            seed: seed.into(),
            next: AtomicU64::new(0),
        }
    }

    /// The `n`th id of `seed`'s sequence, without consuming one.
    #[must_use]
    pub fn nth(seed: &str, n: u64) -> [u8; 16] {
        let digest = Sha256::digest(format!("baskfy-desk-harness:{seed}:{n}").as_bytes());
        let mut b = [0u8; 16];
        b.copy_from_slice(&digest[..16]);
        set_v4_bits(b)
    }
}

impl IdSource for HarnessIds {
    fn uuid4_bytes(&self) -> [u8; 16] {
        let n = self.next.fetch_add(1, Ordering::SeqCst);
        Self::nth(&self.seed, n)
    }
}
