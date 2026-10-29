//! Baskfy's market-data adapters — the Rust port of `baskfy_providers`
//! (`decile-blueprint/packages/providers/src/baskfy_providers`).
//!
//! Python's package docstring, restated: call sites depend on [`ports::Capability`] and the port
//! traits, never on a vendor — "adding a paid vendor later is a new adapter, not a rewrite".
//!
//! | Python | Rust | state |
//! |---|---|---|
//! | `errors` | [`errors`] | ported |
//! | `records` | [`records`] | ported |
//! | `ports` | [`ports`] | ported (async traits, DECISIONS-RS R4.1) |
//! | `settings` | [`settings`] | ported |
//! | `ratelimit` | [`ratelimit`] | ported (Python's Lua, byte for byte) |
//! | `retry` | [`retry`] | ported |
//! | `circuit` | [`circuit`] | ported |
//! | `tokens` | [`tokens`] | ported (Fernet-compatible with Python) |
//! | `publication` | [`publication`] | ported |
//! | `reference_export_io` | [`reference_export_io`] | path half ported; rows wait on core |
//! | `kite` | [`kite`] | ported (with kiteconnect's read-only REST half, DECISIONS-RS R4.10) |
//! | `nse` | [`nse`] | ported (DECISIONS-RS R4.11) |
//! | `archive` | [`archive`] | ported (hand-signed SigV4, DECISIONS-RS R4.14) |
//! | `composite` | [`composite`] | ported |
//! | `fixtures` | [`fixtures`] | ported; Parquet files wait on DECISIONS-RS R4.12 |
//! | `factory` | [`factory`] | ported (DECISIONS-RS R4.13) |
//! | `fixture_builder`, `cli` | — | not started |
//!
//! Four modules have no Python twin: [`clock`] (the injected `time.monotonic`/`time.sleep`/
//! `random` seams as traits), [`net`] (the network chokepoint and the test-suite block that
//! `network_guard.py` gives the Python suite), [`secret`] (credentials that never print) and
//! [`py`] (Python's float and string formatting where messages are graded).
//!
//! **Law 2 holds here:** no type in this crate can place an order. The broker ledger is read
//! only ([`ports::HoldingsProvider`]); the only path to an order is `baskfy-execution`.

pub mod circuit;
pub mod clock;
pub mod errors;
pub mod net;
pub mod ports;
pub mod publication;
pub mod py;
pub mod ratelimit;
pub mod records;
pub mod reference_export_io;
pub mod retry;
pub mod secret;
pub mod settings;
pub mod tokens;

pub mod archive;
pub mod composite;
pub mod factory;
pub mod fixtures;
pub mod kite;
pub mod nse;

pub use errors::{ErrorKind, ProviderError};
