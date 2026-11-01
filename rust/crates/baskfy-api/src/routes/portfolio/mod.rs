//! Shared, private machinery for the api-portfolio lane: the routers `portfolios`,
//! `portfolio_overview`, `trades`, `brokers`, `billing` and `track_b`.
//!
//! Everything here is crate-private. The pure modules (`graph`, `ledger`, `cash`, `nav`,
//! `recon`, `suggest`, `costs`, `tradebook`) are line-for-line ports of the `baskfy_core`
//! modules of the same purpose, which have no Rust home in `baskfy-core` yet; they live beside
//! the routes that are their only callers until the integrator moves them (DECISIONS-RS,
//! api-portfolio P1). Arithmetic that is `Decimal` in Python is [`pydec::Dec`] (the overview) or
//! `baskfy_core::decimal::WideDecimal` (the simple routes), so every intermediate rounds where
//! Python's context rounds. `golden` holds Python's answers for random inputs to the solver and
//! the NAV arithmetic.

pub(crate) mod cash;
pub(crate) mod costs;
pub(crate) mod db;
#[cfg(test)]
mod golden;
pub(crate) mod graph;
pub(crate) mod ledger;
pub(crate) mod load;
pub(crate) mod model;
pub(crate) mod multipart;
pub(crate) mod nav;
pub(crate) mod num;
pub(crate) mod params;
pub(crate) mod portfolio_csv;
pub(crate) mod portfolios_writes;
pub(crate) mod pycsv;
pub(crate) mod pydec;
pub(crate) mod pyfmt;
pub(crate) mod rebalance;
pub(crate) mod recon;
pub(crate) mod request;
pub(crate) mod suggest;
pub(crate) mod symbols;
pub(crate) mod tenant;
pub(crate) mod tradebook;
pub(crate) mod tradebook_csv;

/// A Python `ValueError`/`KeyError`/`TypeError` escaping a handler: logged, answered with the
/// 500 catch-all (no handler in `app.py` catches them).
pub(crate) fn fail(message: &str) -> crate::problems::Problem {
    crate::logging::error("baskfy_api.app", "unhandled error", &[("error", message)]);
    crate::problems::internal_error()
}
