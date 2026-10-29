//! Baskfy's Postgres access, ported from `baskfy_api.db` (and, later, the SQLAlchemy reads in
//! `services/`).
//!
//! What this crate is, and is not (DECISIONS-RS R5.1, R5.2):
//!
//! * **One pool per process**, built from the same `BASKFY_DB_*` settings as Python's
//!   `create_engine` — the same size, overflow, checkout timeout, recycle age, pre-ping and
//!   prepared-statement cache ([`pool`]).
//! * **Read-only, every connection.** `default_transaction_read_only=on` travels in the startup
//!   packet, so no statement on any connection this crate opens can write — a strangler port must
//!   never mutate a database Python still owns before its writes are graded one table at a time.
//!   `tests/read_only.rs` proves a write is refused (SQLSTATE `25006`).
//! * **Never migrates.** Alembic (Python) owns the schema; there is no `migrate` feature and no
//!   DDL anywhere in this crate.
//! * **Typed row helpers** for the four column types whose Python decoding is easy to get wrong:
//!   `numeric` → [`rust_decimal::Decimal`] with its scale kept, `timestamptz` → UTC, `date`, and
//!   `jsonb` → [`OrderedJson`] with the key order Python's `json.loads` would see ([`row`]).

pub mod error;
pub mod json;
pub mod pool;
pub mod row;

pub use error::DbError;
pub use json::OrderedJson;
pub use pool::{Db, DbSettings};
