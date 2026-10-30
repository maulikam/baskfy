//! `baskfy_api.curated_metrics_service.compute_all_metrics` — the EOD `cb_metrics` job (SC2).
//!
//! Same shape as Python: the basket ids and the published count, then per chunk of
//! [`METRICS_BASKET_CHUNK`] baskets four reads in one transaction (committed before the
//! arithmetic), then one transaction per written basket. Every read is Python's SQL text, so a
//! query without `ORDER BY` (the constituents) returns its rows in the same order — the order a
//! weight map is summed in, which moves the last digit of a basket's daily return.

use std::collections::{HashMap, HashSet};

use baskfy_db::OrderedJson;
use baskfy_db::row::{date, decimal, opt_date};
use chrono::{DateTime, Duration, NaiveDate, Utc};
use sqlx::Row;
use sqlx::postgres::PgRow;

use super::math::{
    self, BasketVersion, CatalogVolatility, DIVIDENDS_INCLUDED, Dec, History, MIN_BASKET_VOL_DAYS,
    MONTHS_1M, MONTHS_1Y, MONTHS_3Y, MONTHS_5Y, MONTHS_6M, MathError, RETURN_CONVENTION,
    RETURN_CONVENTION_NOTE, Weights,
};
use crate::error::WorkerError;
use crate::pyjson::{int, object, opt_s, s};
use crate::tasks::TaskContext;

/// `METRICS_BASKET_CHUNK`.
const METRICS_BASKET_CHUNK: usize = 50;

/// `CLOSE_RAW_LOOKBACK_DAYS`.
const CLOSE_RAW_LOOKBACK_DAYS: i64 = 30;

fn math_err(e: MathError) -> WorkerError {
    WorkerError::python(e.type_name, e.message)
}

/// `($first::BIGINT, ...)` for `count` placeholders starting at `$first`.
fn in_list(first: usize, count: usize) -> String {
    let items: Vec<String> = (first..first + count)
        .map(|i| format!("${i}::BIGINT"))
        .collect();
    format!("({})", items.join(", "))
}

#[derive(Debug, Clone)]
struct BasketRef {
    id: i64,
    visibility: String,
    launched_at: Option<NaiveDate>,
}

#[derive(Debug, Clone, Copy)]
struct VersionRow {
    id: i64,
    effective_date: NaiveDate,
}

/// `(instrument_id, weight, symbol)`.
type ConstituentRow = (i64, Dec, String);

#[derive(Debug, Default)]
struct ChunkInputs {
    versions: HashMap<i64, Vec<VersionRow>>,
    constituents: HashMap<i64, Vec<ConstituentRow>>,
    history: History,
    close_raw: HashMap<i64, Dec>,
}

async fn basket_refs(
    conn: &mut sqlx::PgConnection,
    ids: &[i64],
) -> Result<Vec<BasketRef>, WorkerError> {
    let sql = format!(
        "SELECT cb_basket.id, cb_basket.visibility, cb_basket.launched_at \nFROM cb_basket \n\
         WHERE cb_basket.id IN {} ORDER BY cb_basket.id",
        in_list(1, ids.len())
    );
    let mut q = sqlx::query(&sql);
    for id in ids {
        q = q.bind(*id);
    }
    let rows = q.fetch_all(&mut *conn).await?;
    rows.iter()
        .map(|r| {
            Ok(BasketRef {
                id: r.try_get::<i64, _>("id")?,
                visibility: r.try_get::<String, _>("visibility")?,
                launched_at: opt_date(r, "launched_at")?,
            })
        })
        .collect()
}

async fn versions_by_basket(
    conn: &mut sqlx::PgConnection,
    ids: &[i64],
    as_of: NaiveDate,
) -> Result<HashMap<i64, Vec<VersionRow>>, WorkerError> {
    let mut out: HashMap<i64, Vec<VersionRow>> = HashMap::new();
    if ids.is_empty() {
        return Ok(out);
    }
    let sql = format!(
        "SELECT cb_basket_version.basket_id, cb_basket_version.id, cb_basket_version.effective_date \n\
         FROM cb_basket_version \nWHERE cb_basket_version.basket_id IN {} AND \
         cb_basket_version.effective_date <= $1::DATE ORDER BY cb_basket_version.basket_id, \
         cb_basket_version.effective_date, cb_basket_version.version_no",
        in_list(2, ids.len())
    );
    let mut q = sqlx::query(&sql).bind(as_of);
    for id in ids {
        q = q.bind(*id);
    }
    for r in q.fetch_all(&mut *conn).await? {
        let basket_id: i64 = r.try_get("basket_id")?;
        out.entry(basket_id).or_default().push(VersionRow {
            id: r.try_get("id")?,
            effective_date: date(&r, "effective_date")?,
        });
    }
    Ok(out)
}

async fn constituents_by_version(
    conn: &mut sqlx::PgConnection,
    version_ids: &[i64],
) -> Result<HashMap<i64, Vec<ConstituentRow>>, WorkerError> {
    let mut out: HashMap<i64, Vec<ConstituentRow>> = HashMap::new();
    if version_ids.is_empty() {
        return Ok(out);
    }
    let sql = format!(
        "SELECT cb_constituent.version_id, cb_constituent.instrument_id, cb_constituent.weight, \
         instrument.symbol \nFROM cb_constituent JOIN instrument ON instrument.id = \
         cb_constituent.instrument_id \nWHERE cb_constituent.version_id IN {}",
        in_list(1, version_ids.len())
    );
    let mut q = sqlx::query(&sql);
    for id in version_ids {
        q = q.bind(*id);
    }
    for r in q.fetch_all(&mut *conn).await? {
        let version_id: i64 = r.try_get("version_id")?;
        out.entry(version_id).or_default().push((
            r.try_get("instrument_id")?,
            Dec::from(decimal(&r, "weight")?),
            r.try_get("symbol")?,
        ));
    }
    Ok(out)
}

async fn close_raw_map(
    conn: &mut sqlx::PgConnection,
    instrument_ids: &[i64],
    as_of: NaiveDate,
) -> Result<HashMap<i64, Dec>, WorkerError> {
    let mut out = HashMap::new();
    if instrument_ids.is_empty() {
        return Ok(out);
    }
    let floor = as_of - Duration::days(CLOSE_RAW_LOOKBACK_DAYS);
    let sql = format!(
        "SELECT DISTINCT ON (ohlcv_daily.instrument_id) ohlcv_daily.instrument_id, \
         ohlcv_daily.close_raw \nFROM ohlcv_daily \nWHERE ohlcv_daily.instrument_id IN {} AND \
         ohlcv_daily.date >= $1::DATE AND ohlcv_daily.date <= $2::DATE ORDER BY \
         ohlcv_daily.instrument_id, ohlcv_daily.date DESC",
        in_list(3, instrument_ids.len())
    );
    let mut q = sqlx::query(&sql).bind(floor).bind(as_of);
    for id in instrument_ids {
        q = q.bind(*id);
    }
    for r in q.fetch_all(&mut *conn).await? {
        out.insert(
            r.try_get::<i64, _>("instrument_id")?,
            Dec::from(decimal(&r, "close_raw")?),
        );
    }
    Ok(out)
}

async fn price_history(
    conn: &mut sqlx::PgConnection,
    instrument_ids: &[i64],
    start: NaiveDate,
    as_of: NaiveDate,
) -> Result<History, WorkerError> {
    let mut out = History::new();
    if instrument_ids.is_empty() {
        return Ok(out);
    }
    let sql = format!(
        "SELECT ohlcv_daily.date, instrument.symbol, ohlcv_daily.close \nFROM ohlcv_daily JOIN \
         instrument ON instrument.id = ohlcv_daily.instrument_id \nWHERE \
         ohlcv_daily.instrument_id IN {} AND ohlcv_daily.date >= $1::DATE AND ohlcv_daily.date \
         <= $2::DATE ORDER BY ohlcv_daily.date",
        in_list(3, instrument_ids.len())
    );
    let mut q = sqlx::query(&sql).bind(start).bind(as_of);
    for id in instrument_ids {
        q = q.bind(*id);
    }
    let rows: Vec<PgRow> = q.fetch_all(&mut *conn).await?;
    for r in &rows {
        out.entry(date(r, "date")?)
            .or_default()
            .insert(r.try_get("symbol")?, Dec::from(decimal(r, "close")?));
    }
    Ok(out)
}

async fn load_chunk(
    conn: &mut sqlx::PgConnection,
    refs: &[BasketRef],
    as_of: NaiveDate,
) -> Result<ChunkInputs, WorkerError> {
    let ids: Vec<i64> = refs.iter().map(|r| r.id).collect();
    let versions = versions_by_basket(conn, &ids, as_of).await?;
    // Python iterates `versions.values()` in insertion order — the query's basket order.
    let mut basket_order: Vec<i64> = versions.keys().copied().collect();
    basket_order.sort_unstable();
    let version_ids: Vec<i64> = basket_order
        .iter()
        .flat_map(|b| versions.get(b).into_iter().flatten().map(|v| v.id))
        .collect();
    let constituents = constituents_by_version(conn, &version_ids).await?;
    let mut instrument_ids: Vec<i64> = constituents
        .values()
        .flatten()
        .map(|(iid, _, _)| *iid)
        .collect::<HashSet<_>>()
        .into_iter()
        .collect();
    instrument_ids.sort_unstable();
    let starts: Vec<NaiveDate> = versions
        .values()
        .filter_map(|rows| rows.first().map(|v| v.effective_date))
        .collect();
    let history = match starts.iter().min() {
        Some(start) => price_history(conn, &instrument_ids, *start, as_of).await?,
        None => History::new(),
    };
    let mut latest_ids: Vec<i64> = versions
        .values()
        .filter_map(|rows| rows.last())
        .flat_map(|v| constituents.get(&v.id).into_iter().flatten().map(|c| c.0))
        .collect::<HashSet<_>>()
        .into_iter()
        .collect();
    latest_ids.sort_unstable();
    let close_raw = close_raw_map(conn, &latest_ids, as_of).await?;
    Ok(ChunkInputs {
        versions,
        constituents,
        history,
        close_raw,
    })
}

/// `_weights_by_symbol`: a dict comprehension (a repeated symbol keeps its first position and
/// takes the last weight).
fn weights_by_symbol(rows: &[ConstituentRow]) -> Weights {
    let mut out: Weights = Vec::with_capacity(rows.len());
    for (_, weight, symbol) in rows {
        if let Some(slot) = out.iter_mut().find(|(s, _)| s == symbol) {
            slot.1 = weight.clone();
        } else {
            out.push((symbol.clone(), weight.clone()));
        }
    }
    out
}

/// `_min_amount_for`: NULL when any constituent is unpriced.
fn min_amount_for(
    rows: &[ConstituentRow],
    prices: &HashMap<i64, Dec>,
) -> Result<Option<Dec>, MathError> {
    if rows.is_empty() {
        return Ok(None);
    }
    let mut ordered = Vec::with_capacity(rows.len());
    for (iid, _, _) in rows {
        match prices.get(iid) {
            Some(p) => ordered.push(p.clone()),
            None => return Ok(None),
        }
    }
    let weights: Vec<Dec> = rows.iter().map(|(_, w, _)| w.clone()).collect();
    Ok(Some(math::money(&math::min_amount(&ordered, &weights)?)?))
}

/// `_slice_history`.
fn slice_history(history: &History, symbols: &HashSet<String>, start: NaiveDate) -> History {
    let mut out = History::new();
    for (day, closes) in history.range(start..) {
        let row: HashMap<String, Dec> = closes
            .iter()
            .filter(|(sym, _)| symbols.contains(*sym))
            .map(|(sym, c)| (sym.clone(), c.clone()))
            .collect();
        if !row.is_empty() {
            out.insert(*day, row);
        }
    }
    out
}

#[derive(Debug, Clone)]
struct MetricValues {
    basket_id: i64,
    visibility: String,
    min_amount: Option<Dec>,
    volatility: Option<CatalogVolatility>,
    ret_1m: Option<Dec>,
    ret_6m: Option<Dec>,
    ret_1y: Option<Dec>,
    cagr_3y: Option<Dec>,
    cagr_5y: Option<Dec>,
    since_inception_pct: Option<Dec>,
    months_available: Option<i64>,
    status: &'static str,
}

/// `_metric_values(basket, inputs, as_of)`.
fn metric_values(
    basket: &BasketRef,
    inputs: &ChunkInputs,
    as_of: NaiveDate,
) -> Result<MetricValues, MathError> {
    let empty_rows: Vec<ConstituentRow> = Vec::new();
    let mut values = MetricValues {
        basket_id: basket.id,
        visibility: basket.visibility.clone(),
        min_amount: None,
        volatility: None,
        ret_1m: None,
        ret_6m: None,
        ret_1y: None,
        cagr_3y: None,
        cagr_5y: None,
        since_inception_pct: None,
        months_available: None,
        status: "no_version",
    };
    let Some(versions) = inputs.versions.get(&basket.id).filter(|v| !v.is_empty()) else {
        return Ok(values);
    };
    let (Some(first), Some(last)) = (versions.first(), versions.last()) else {
        return Ok(values);
    };
    let constituents = &inputs.constituents;
    let latest = constituents.get(&last.id).unwrap_or(&empty_rows);
    let history_versions: Vec<BasketVersion> = versions
        .iter()
        .filter_map(|v| {
            constituents
                .get(&v.id)
                .filter(|rows| !rows.is_empty())
                .map(|rows| BasketVersion {
                    effective_date: v.effective_date,
                    weights: weights_by_symbol(rows),
                })
        })
        .collect();
    let symbols: HashSet<String> = versions
        .iter()
        .flat_map(|v| constituents.get(&v.id).into_iter().flatten())
        .map(|(_, _, sym)| sym.clone())
        .collect();
    let history = slice_history(&inputs.history, &symbols, first.effective_date);
    let nav = math::version_aware_nav(&history, &history_versions)?;
    let latest_weights = weights_by_symbol(latest);
    let returns = nav.returns_only();
    let constituent_vols = if returns.len() < MIN_BASKET_VOL_DAYS {
        let mut names: Vec<String> = latest_weights.iter().map(|(s, _)| s.clone()).collect();
        names.sort();
        Some(math::constituent_volatilities(&history, &names)?)
    } else {
        None
    };
    values.volatility =
        math::catalog_volatility(&returns, Some(&latest_weights), constituent_vols.as_ref())?;
    let anchor = math::since_inception_anchor(&nav.points, basket.launched_at);
    values.min_amount = min_amount_for(latest, &inputs.close_raw)?;
    values.ret_1m = math::window_return(&nav.points, MONTHS_1M)?;
    values.ret_6m = math::window_return(&nav.points, MONTHS_6M)?;
    values.ret_1y = math::window_return(&nav.points, MONTHS_1Y)?;
    values.cagr_3y = math::window_cagr(&nav.points, MONTHS_3Y)?;
    values.cagr_5y = math::window_cagr(&nav.points, MONTHS_5Y)?;
    values.since_inception_pct = math::since_inception_return(&nav.points, basket.launched_at)?;
    values.months_available = anchor.map(|a| math::whole_months_between(a, as_of));
    values.status = "ok";
    Ok(values)
}

fn opt_text(d: Option<&Dec>) -> Option<String> {
    d.map(Dec::to_sci_string)
}

/// `upsert_metrics_row` + the payload `_write_metric_values` returns.
async fn write_metric_values(
    conn: &mut sqlx::PgConnection,
    values: &MetricValues,
    as_of: NaiveDate,
    published_count: i64,
    peer_vols: &[Dec],
    computed_at: DateTime<Utc>,
) -> Result<OrderedJson, WorkerError> {
    let bucket = match &values.volatility {
        Some(v) => Some(
            math::volatility_bucket_for_catalog(&v.value, published_count, Some(peer_vols))
                .map_err(math_err)?,
        ),
        None => None,
    };
    let basis = values.volatility.as_ref().map(|v| v.basis);
    sqlx::query(
        "INSERT INTO cb_metrics (basket_id, as_of_date, min_amount, volatility_bucket, \
         volatility_value, volatility_basis, months_available, return_convention, \
         dividends_included, ret_1m, ret_6m, ret_1y, cagr_3y, cagr_5y, since_inception_pct, \
         computed_at) VALUES ($1::BIGINT, $2::DATE, $3::NUMERIC(20, 2), $4::VARCHAR, \
         $5::NUMERIC(18, 10), $6::VARCHAR, $7::INTEGER, $8::VARCHAR, $9, $10::NUMERIC(14, 2), \
         $11::NUMERIC(14, 2), $12::NUMERIC(14, 2), $13::NUMERIC(14, 2), $14::NUMERIC(14, 2), \
         $15::NUMERIC(14, 2), $16::TIMESTAMP WITH TIME ZONE) ON CONFLICT (basket_id, as_of_date) \
         DO UPDATE SET min_amount = excluded.min_amount, volatility_bucket = \
         excluded.volatility_bucket, volatility_value = excluded.volatility_value, \
         volatility_basis = excluded.volatility_basis, months_available = \
         excluded.months_available, return_convention = excluded.return_convention, \
         dividends_included = excluded.dividends_included, ret_1m = excluded.ret_1m, ret_6m = \
         excluded.ret_6m, ret_1y = excluded.ret_1y, cagr_3y = excluded.cagr_3y, cagr_5y = \
         excluded.cagr_5y, since_inception_pct = excluded.since_inception_pct, computed_at = \
         excluded.computed_at",
    )
    .bind(values.basket_id)
    .bind(as_of)
    .bind(opt_text(values.min_amount.as_ref()))
    .bind(bucket)
    .bind(opt_text(values.volatility.as_ref().map(|v| &v.value)))
    .bind(basis)
    .bind(values.months_available.and_then(|m| i32::try_from(m).ok()))
    .bind(RETURN_CONVENTION)
    .bind(DIVIDENDS_INCLUDED)
    .bind(opt_text(values.ret_1m.as_ref()))
    .bind(opt_text(values.ret_6m.as_ref()))
    .bind(opt_text(values.ret_1y.as_ref()))
    .bind(opt_text(values.cagr_3y.as_ref()))
    .bind(opt_text(values.cagr_5y.as_ref()))
    .bind(opt_text(values.since_inception_pct.as_ref()))
    .bind(computed_at)
    .execute(&mut *conn)
    .await?;
    let mut entries = vec![
        ("basket_id", int(values.basket_id)),
        ("as_of", s(iso(as_of))),
        ("status", s(values.status)),
        ("min_amount", opt_s(opt_text(values.min_amount.as_ref()))),
        ("volatility_bucket", opt_s(bucket)),
        ("volatility_basis", opt_s(basis)),
        (
            "months_available",
            values.months_available.map_or(OrderedJson::Null, int),
        ),
    ];
    entries.extend(return_convention_fields());
    Ok(object(entries))
}

fn iso(day: NaiveDate) -> String {
    day.format("%Y-%m-%d").to_string()
}

/// `return_convention_fields()`.
fn return_convention_fields() -> Vec<(&'static str, OrderedJson)> {
    vec![
        ("return_convention", s(RETURN_CONVENTION)),
        ("dividends_included", OrderedJson::Bool(DIVIDENDS_INCLUDED)),
        ("return_convention_note", s(RETURN_CONVENTION_NOTE)),
    ]
}

/// `compute_all_metrics(session, as_of)`: every non-archived basket's row for `as_of`.
///
/// # Errors
/// A database failure, or the arithmetic's `ValueError` (a non-positive price). Baskets
/// written before the failure stay committed, as in Python.
pub async fn compute_all_metrics(
    ctx: &TaskContext,
    as_of: NaiveDate,
) -> Result<OrderedJson, WorkerError> {
    let now = ctx.now_utc();
    let computed_at = now - Duration::nanoseconds(i64::from(now.timestamp_subsec_nanos() % 1_000));
    let mut tx = ctx.db.begin().await?;
    let basket_ids: Vec<i64> = sqlx::query_scalar(
        "SELECT cb_basket.id \nFROM cb_basket \nWHERE cb_basket.archived_at IS NULL ORDER BY \
         cb_basket.id",
    )
    .fetch_all(&mut *tx)
    .await?;
    let published_count: i64 = sqlx::query_scalar(
        "SELECT count(*) AS count_1 \nFROM cb_basket \nWHERE cb_basket.archived_at IS NULL AND \
         cb_basket.visibility = $1::VARCHAR",
    )
    .bind("PUBLISHED")
    .fetch_one(&mut *tx)
    .await?;

    let mut computed: Vec<MetricValues> = Vec::with_capacity(basket_ids.len());
    // The first chunk reads in the transaction that read the ids, as Python's session does.
    let mut open = Some(tx);
    for chunk in basket_ids.chunks(METRICS_BASKET_CHUNK) {
        let mut tx = match open.take() {
            Some(tx) => tx,
            None => ctx.db.begin().await?,
        };
        let refs = basket_refs(&mut tx, chunk).await?;
        let inputs = load_chunk(&mut tx, &refs, as_of).await?;
        tx.commit().await?;
        for r in &refs {
            computed.push(metric_values(r, &inputs, as_of).map_err(math_err)?);
        }
    }
    if let Some(tx) = open {
        tx.commit().await?;
    }

    let peer_vols: Vec<Dec> = computed
        .iter()
        .filter(|v| v.visibility == "PUBLISHED")
        .filter_map(|v| v.volatility.as_ref().map(|c| c.value.clone()))
        .collect();
    let mut results = Vec::with_capacity(computed.len());
    for values in &computed {
        let mut tx = ctx.db.begin().await?;
        results.push(
            write_metric_values(
                &mut tx,
                values,
                as_of,
                published_count,
                &peer_vols,
                computed_at,
            )
            .await?,
        );
        tx.commit().await?;
    }
    let mut entries = vec![
        ("as_of", s(iso(as_of))),
        (
            "baskets",
            int(i64::try_from(results.len()).unwrap_or(i64::MAX)),
        ),
        ("results", OrderedJson::Array(results)),
    ];
    entries.extend(return_convention_fields());
    Ok(object(entries))
}
