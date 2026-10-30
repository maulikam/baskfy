//! Ports `app/breadth_source.py` (M14 §1): which 20-DMA breadth number the weekly plan's cash
//! bands see — the pipeline's (`market_health_daily.pct_above_20dma` over `DESK_UNIVERSE_SLUG`)
//! when its membership is the scan's, symbol for symbol, else the scan's own, and always why.
//!
//! The decision ([`breadth_for_plan`]) is pure over a [`PipelineBreadth`] reader, so it is
//! tested without a database; [`PgPipeline`] is the reader over `SCREENER_DATABASE_URL` with
//! Python's two statements. Neither branch guesses: a different universe, a missing row or an
//! unreachable pipeline each fall back to the scan's figure and say so on the plan.

use std::collections::BTreeSet;

use async_trait::async_trait;
use chrono::NaiveDate;

use crate::wire::{PyDict, PyValue};

/// `_SQL`.
pub const BREADTH_SQL: &str = "select m.pct_above_20dma, m.constituent_count \
     from market_health_daily m join index_def d on d.id = m.index_id \
     where d.slug = $1 and m.date = $2";

/// `_MEMBERS_SQL`.
pub const MEMBERS_SQL: &str = "select i.symbol from index_member_daily m \
     join instrument i on i.id = m.instrument_id \
     join index_def d on d.id = m.index_id \
     where d.slug = $1 and m.date = $2";

/// A pipeline read that raised: the exception's class name, as `exc.__class__.__name__`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PipelineError(pub String);

/// `pipeline_breadth(on, slug)`: the value (or `None`) and the membership it was measured over.
#[async_trait]
pub trait PipelineBreadth: Send + Sync {
    /// One date's breadth and members.
    async fn read(
        &self,
        on: NaiveDate,
        slug: &str,
    ) -> Result<(Option<f64>, BTreeSet<String>), PipelineError>;
}

/// The reader over the screener's Postgres (`psycopg.connect(SCREENER_DSN)`).
#[derive(Debug, Clone)]
pub struct PgPipeline {
    /// `SCREENER_DATABASE_URL`.
    pub dsn: String,
}

/// `exc.__class__.__name__` for an error raised by a statement, as psycopg names it: the
/// server's SQLSTATE picks the `psycopg.errors` class (the common ones are named here; any other
/// server error falls back to the base `DatabaseError`), and a transport failure mid-statement
/// is psycopg's `OperationalError`.
fn class_of(e: &sqlx::Error) -> PipelineError {
    let name = match e {
        sqlx::Error::Database(db) => match db.code().as_deref() {
            Some("42P01") => "UndefinedTable",
            Some("42703") => "UndefinedColumn",
            Some("42883") => "UndefinedFunction",
            Some("42501") => "InsufficientPrivilege",
            Some("57014") => "QueryCanceled",
            Some("57P01") => "AdminShutdown",
            _ => "DatabaseError",
        },
        _ => "OperationalError",
    };
    PipelineError(name.to_owned())
}

/// `psycopg.connect(...)` failing: psycopg raises `OperationalError` for every connection it
/// cannot make, including one the server refused (a bad password, a missing database), so a
/// connect error is never classed by its SQLSTATE.
fn connect_class(_e: &sqlx::Error) -> PipelineError {
    PipelineError("OperationalError".to_owned())
}

#[async_trait]
impl PipelineBreadth for PgPipeline {
    async fn read(
        &self,
        on: NaiveDate,
        slug: &str,
    ) -> Result<(Option<f64>, BTreeSet<String>), PipelineError> {
        use sqlx::Connection;
        use sqlx::Row;
        let mut conn = sqlx::PgConnection::connect(&self.dsn)
            .await
            .map_err(|e| connect_class(&e))?;
        let row = sqlx::query(BREADTH_SQL)
            .bind(slug)
            .bind(on)
            .fetch_optional(&mut conn)
            .await
            .map_err(|e| class_of(&e))?;
        let members: Vec<sqlx::postgres::PgRow> = sqlx::query(MEMBERS_SQL)
            .bind(slug)
            .bind(on)
            .fetch_all(&mut conn)
            .await
            .map_err(|e| class_of(&e))?;
        let members: BTreeSet<String> = members
            .iter()
            .filter_map(|r| r.try_get::<String, _>(0).ok())
            .collect();
        let value = row.and_then(|r| {
            r.try_get::<Option<rust_decimal::Decimal>, _>(0)
                .ok()
                .flatten()
                .and_then(|d| d.to_string().parse::<f64>().ok())
                .or_else(|| r.try_get::<Option<f64>, _>(0).ok().flatten())
        });
        Ok((value, members))
    }
}

/// `breadth_for_plan(scan_breadth, symbols, on)`: decide which number the cash bands see, and
/// record why. Keys in Python's order: `value`, `source`, `scan_value`, `pipeline_value`,
/// `universe`, `reason`.
pub async fn breadth_for_plan(
    scan_breadth: f64,
    symbols: &BTreeSet<String>,
    on: NaiveDate,
    universe: &str,
    reader: &dyn PipelineBreadth,
) -> PyDict {
    let mut value = scan_breadth;
    let mut source = "scan";
    let pipeline_value;
    let reason;
    match reader.read(on, universe).await {
        Err(PipelineError(class)) => {
            let mut out = PyDict::new();
            out.insert("value", PyValue::Float(scan_breadth));
            out.insert("source", PyValue::Str("scan".to_owned()));
            out.insert("scan_value", PyValue::Float(scan_breadth));
            out.insert("pipeline_value", PyValue::None);
            out.insert("universe", PyValue::Str(universe.to_owned()));
            out.insert(
                "reason",
                PyValue::Str(format!(
                    "pipeline unreachable ({class}); used the scan's own"
                )),
            );
            return out;
        }
        Ok((pv, members)) => {
            pipeline_value = pv;
            if pv.is_none() {
                reason = format!("no market_health_daily row for {on}; used the scan's own");
            } else if &members != symbols {
                let only_scan = symbols.difference(&members).count();
                let only_index = members.difference(symbols).count();
                reason = format!(
                    "scan universe differs from {universe} on {on} ({only_scan} only in the \
                     scan, {only_index} only in the index); used the scan's own"
                );
            } else {
                value = pv.unwrap_or(scan_breadth);
                source = "pipeline";
                reason = format!("same {} symbols as {universe} on {on}", members.len());
            }
        }
    }
    let mut out = PyDict::new();
    out.insert("value", PyValue::Float(value));
    out.insert("source", PyValue::Str(source.to_owned()));
    out.insert("scan_value", PyValue::Float(scan_breadth));
    out.insert(
        "pipeline_value",
        pipeline_value.map_or(PyValue::None, PyValue::Float),
    );
    out.insert("universe", PyValue::Str(universe.to_owned()));
    out.insert("reason", PyValue::Str(reason));
    out
}
