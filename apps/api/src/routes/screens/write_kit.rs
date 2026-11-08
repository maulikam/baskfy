//! What every write route of the writes-screens lane does before and around its transaction:
//! read the body as FastAPI does (before any dependency runs — a malformed body is a 400 even for
//! an anonymous caller, `fastapi.routing.get_request_handler`), take the write pool, stamp
//! Python's clock, and bind a `Decimal` exactly as asyncpg sends it.

use axum::extract::{Extension, Request};
use axum::http::header;
use baskfy_core::decimal::WideDecimal;
use chrono::{DateTime, Timelike, Utc};

use crate::body::{Received, receive};
use crate::problems::{Problem, framework, internal_error};
use crate::session::WritePool;

/// The largest body read (Starlette reads it whole; this bounds memory far above any model here).
const MAX_BODY_BYTES: usize = 1 << 20;

/// `await request.body()` and FastAPI's JSON rule: the [`Received`] body, or the parse 400s.
///
/// # Errors
/// `json_invalid` (400), Starlette's parse 400, or the 500 for a lone surrogate.
pub async fn read(request: Request) -> Result<Received, Problem> {
    let content_type = request
        .headers()
        .get(header::CONTENT_TYPE)
        .map(|v| String::from_utf8_lossy(v.as_bytes()).into_owned());
    let bytes = axum::body::to_bytes(request.into_body(), MAX_BODY_BYTES)
        .await
        .map_err(|_| framework(400, "There was an error parsing the body"))?;
    receive(content_type.as_deref(), &bytes)
}

/// The write pool, or the 500 a state built without one gives.
///
/// # Errors
/// The 500 catch-all.
pub fn pool(pool: Option<Extension<WritePool>>) -> Result<WritePool, Problem> {
    pool.map(|Extension(p)| p).ok_or_else(internal_error)
}

/// `dt.datetime.now(tz=dt.UTC)`: microsecond precision, as Python's clock and the column hold it.
#[must_use]
pub fn now_utc() -> DateTime<Utc> {
    let t = crate::entitlements::utc_now();
    t.with_nanosecond(t.nanosecond() / 1000 * 1000).unwrap_or(t)
}

/// A database failure inside a write: logged, the 500 catch-all.
#[must_use]
pub fn db(e: sqlx::Error) -> Problem {
    crate::db_failure(&e.into())
}

/// A `Decimal` as asyncpg hands it to Postgres: its exact value, which the column then rounds
/// (`numeric(20,2)` rounds half away from zero). Bind with `($n::text)::numeric`.
#[must_use]
pub fn numeric_text(d: &WideDecimal) -> String {
    d.to_sci_string()
}

/// SQLAlchemy's `insertmanyvalues_page_size`: the ORM flushes rows of one table as multi-row
/// `INSERT ... VALUES (...), (...)` statements of at most this many rows.
pub const INSERT_PAGE_ROWS: usize = 1000;

/// The `VALUES` list of a multi-row INSERT: `rows` tuples, each rendered by `row` from the
/// placeholder numbers of its `per_row` parameters. Numbering starts at `$2` (`$1` is a value
/// every row shares, bound first). Only placeholders are rendered: the statement stays
/// parameterised.
#[must_use]
pub fn values_rows(rows: usize, per_row: usize, row: impl Fn(&[usize]) -> String) -> String {
    (0..rows)
        .map(|i| {
            let first = 2 + i * per_row;
            let numbers: Vec<usize> = (first..first + per_row).collect();
            row(&numbers)
        })
        .collect::<Vec<_>>()
        .join(", ")
}

/// A `numeric` read back, as Python's `str(Decimal)` prints it.
#[must_use]
pub fn dec_str(d: rust_decimal::Decimal) -> String {
    crate::wire::py_decimal(d)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::panic)]

    /// The writes-screens lane's write paths reach no broker: no order, GTT or execution name in
    /// any of its source files (law 2; plans and SIPs are records, never orders).
    #[test]
    fn the_lanes_writes_name_no_order_path() {
        for (name, source) in [
            ("screens/writes", include_str!("writes.rs")),
            ("screens/body_model", include_str!("body_model.rs")),
            ("watchlist/writes", include_str!("../watchlist/writes.rs")),
            ("managers/writes", include_str!("../managers/writes.rs")),
            (
                "curated_create/writes",
                include_str!("../curated_create/writes.rs"),
            ),
            (
                "curated_engage/writes",
                include_str!("../curated_engage/writes.rs"),
            ),
            (
                "curated_investments/writes",
                include_str!("../curated_investments/writes.rs"),
            ),
            (
                "curated_sip/writes",
                include_str!("../curated_sip/writes.rs"),
            ),
            (
                "curated_drift/writes",
                include_str!("../curated_drift/writes.rs"),
            ),
            (
                "curated_plans/core",
                include_str!("../curated_plans/core.rs"),
            ),
            (
                "curated_plans/writes",
                include_str!("../curated_plans/writes.rs"),
            ),
            ("curated_customize", include_str!("../curated_customize.rs")),
        ] {
            let code = source.split("#[cfg(test)]").next().unwrap_or(source);
            for order in [
                "baskfy_execution",
                "place_order",
                "modify_order",
                "cancel_order",
                "place_gtt",
                "kite::",
                "OrderGateway",
            ] {
                assert!(!code.contains(order), "{name} names {order}");
            }
        }
    }
}
