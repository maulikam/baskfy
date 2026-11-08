//! Step 4 — `apply_adjustments` and `reprocess_instrument`. Ports
//! `baskfy_worker.tasks.adjustments` (the database half; the maths is `adjust.rs`).
//!
//! The rebuild rule (docs/09): a new corporate action means the instrument's **whole** adjusted
//! history is recomputed from its raw prints — idempotent by construction, because the inputs
//! (`*_raw`, `corporate_action`) are never written here.
//!
//! **House rule 5.** `reprocess_instrument(.., as_of = None)` — the nightly's call — applies
//! every action known now: `ohlcv_daily.close` is an "as of today" series, deliberately. A
//! point-in-time reader passes `as_of` and gets only actions with `ex_date <= as_of`.
//!
//! # Reproducing the frame Python builds
//!
//! `_load_raw_bars` builds a polars frame from a list of dicts (`strict=False`). Its decimal
//! columns are therefore `Decimal(38, s)` with `s` the largest scale among the **first 100**
//! rows (polars' `infer_schema_length`), and every later value is rounded half-even onto that
//! scale. A row without `open_raw` is recovered as `open / adj_factor` in Python's decimal
//! context (28 digits), so `s` can be 20-odd. [`frame_column`] reproduces both rules; the
//! numerics are read as text so each value keeps the exact scale asyncpg hands Python.

use std::collections::BTreeSet;
use std::str::FromStr;

use baskfy_core::decimal::{Rounding, WideDecimal};
use chrono::NaiveDate;
use sqlx::PgConnection;

use super::adjust::{self, ActionInput, AdjustedBar, RawBar, Scales};
use crate::error::WorkerError;
use crate::pyjson;
use crate::steps::StepOutcome;

/// `UPSERT_CHUNK`.
const UPSERT_CHUNK: usize = 2000;
/// `KITE_ADJUSTED_SOURCE`: vendor-adjusted deep history (M29), never fed to `adjust_bars`.
const KITE_ADJUSTED_SOURCE: &str = "kite_adjusted";
/// polars' `N_INFER_DEFAULT`: how many rows a list-of-dicts frame infers its dtypes from.
const POLARS_INFER_ROWS: usize = 100;
/// `PRICE_EXPONENT` / `FACTOR_EXPONENT`: `numeric(18,4)` and `numeric(18,10)`.
const PRICE_EXP: i64 = -4;
const FACTOR_EXP: i64 = -10;

/// What reprocessing one instrument did (`InstrumentAdjustment`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InstrumentAdjustment {
    /// The instrument.
    pub instrument_id: i64,
    /// Adjusted bars rewritten plus deep (`kite_adjusted`) bars rescaled.
    pub bars_rewritten: i64,
    /// Actions that changed a factor.
    pub actions_applied: i64,
    /// `"{type}@{ex_date}: {detail}"` for every action recognised but not quantified.
    pub unquantified: Vec<String>,
    /// Deep bars rescaled across the seam (AF 3.1).
    pub deep_rescaled: i64,
}

fn to_i64(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

fn iso(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

fn parse(text: &str) -> Result<WideDecimal, WorkerError> {
    WideDecimal::from_str(text).map_err(|e| WorkerError::python("InvalidOperation", e.0))
}

fn parse_opt(text: Option<String>) -> Result<Option<WideDecimal>, WorkerError> {
    text.as_deref().map(parse).transpose()
}

/// A polars `Decimal(38, s)` column built from Python decimals with `strict=False`: `s` is the
/// largest `max(0, -exponent)` among the first 100 rows' values; every value is put on `s`
/// half-even. A value that needs more than 38 digits at `s` becomes **null**, as `strict=False`
/// makes it (measured against polars 1.43.2; the null then reaches `ohlcv_daily`'s `NOT NULL`
/// column and fails the write, on both sides). Returns the column and `s`.
///
/// # Errors
/// `PanicException` when `s` exceeds 38: polars 1.43.2 panics building such a column
/// (`index out of bounds` in `polars-compute`). Residual: Python's `PanicException` is a
/// `BaseException`, so it escapes `record_step` unrecorded, where this is recorded as the step's
/// failure. Reaching it needs a stored price below 1e-10 after `open / adj_factor` (an
/// `adj_factor` above a million), which no real history has.
pub(super) fn frame_column(
    values: Vec<Option<WideDecimal>>,
) -> Result<(Vec<Option<WideDecimal>>, i64), WorkerError> {
    let scale = values
        .iter()
        .take(POLARS_INFER_ROWS)
        .flatten()
        .map(adjust::scale_of)
        .max()
        .unwrap_or(0);
    if scale > 38 {
        return Err(WorkerError::python(
            "PanicException",
            format!("index out of bounds: the len is 39 but the index is {scale}"),
        ));
    }
    let mut out = Vec::with_capacity(values.len());
    for value in values {
        out.push(value.and_then(|v| {
            let at = v.rescale_exact(-scale, Rounding::HalfEven);
            (at.coefficient_len() <= adjust::POLARS_MAX_PRECISION).then_some(at)
        }));
    }
    Ok((out, scale))
}

/// `_unadjust`: a stored adjusted price divided back by its factor (identity at factor 1 or 0).
fn unadjust(
    value: &WideDecimal,
    adj_factor: Option<&WideDecimal>,
) -> Result<WideDecimal, WorkerError> {
    match adj_factor {
        None => Ok(value.clone()),
        Some(f) if f.is_zero() => Ok(value.clone()),
        Some(f) => value
            .div(f)
            .map_err(|e| WorkerError::python("InvalidOperation", e.0)),
    }
}

/// `_price`: `Decimal(str(v)).quantize(Decimal("0.0001"), ROUND_HALF_UP)`.
fn price(value: &WideDecimal) -> Result<WideDecimal, WorkerError> {
    value
        .quantize(PRICE_EXP, Rounding::HalfUp)
        .map_err(|e| WorkerError::python("InvalidOperation", e.0))
}

/// `_factor`: ten places, half up.
fn factor(value: &WideDecimal) -> Result<WideDecimal, WorkerError> {
    value
        .quantize(FACTOR_EXP, Rounding::HalfUp)
        .map_err(|e| WorkerError::python("InvalidOperation", e.0))
}

/// `_issue_price(raw)`: a rights issue's subscription price, when the payload carries one under
/// `issue_price`, `subscription_price` or `premium` (the first key holding an int, float or
/// str decides; one that does not parse is `None`).
pub(super) fn issue_price(raw: &serde_json::Value) -> Option<WideDecimal> {
    let serde_json::Value::Object(map) = raw else {
        return None;
    };
    for key in ["issue_price", "subscription_price", "premium"] {
        let text = match map.get(key) {
            // `isinstance(True, int)`: `Decimal("True")` is InvalidOperation -> None.
            Some(serde_json::Value::Bool(_)) => return None,
            Some(serde_json::Value::Number(n)) => {
                if let Some(i) = n.as_i64() {
                    i.to_string()
                } else if let Some(u) = n.as_u64() {
                    u.to_string()
                } else {
                    baskfy_core::py::float_repr(n.as_f64().unwrap_or(f64::NAN))
                }
            }
            Some(serde_json::Value::String(s)) => s.clone(),
            _ => continue,
        };
        return WideDecimal::from_str(&text.replace('_', "")).ok();
    }
    None
}

/// `_load_actions`: every action for the instrument (bounded to `ex_date <= as_of` when given),
/// by ex-date.
async fn load_actions(
    conn: &mut PgConnection,
    instrument_id: i64,
    as_of: Option<NaiveDate>,
) -> Result<Vec<ActionInput>, WorkerError> {
    type Row = (
        i64,
        i64,
        String,
        NaiveDate,
        Option<String>,
        Option<String>,
        Option<String>,
        String,
    );
    let base = "SELECT corporate_action.id, corporate_action.instrument_id, \
                corporate_action.action_type, corporate_action.ex_date, \
                corporate_action.ratio_from::text, corporate_action.ratio_to::text, \
                corporate_action.amount::text, corporate_action.raw::text \
                FROM corporate_action WHERE corporate_action.instrument_id = $1";
    let rows: Vec<Row> = match as_of {
        None => {
            sqlx::query_as(&format!("{base} ORDER BY corporate_action.ex_date"))
                .bind(instrument_id)
                .fetch_all(&mut *conn)
                .await?
        }
        Some(day) => {
            sqlx::query_as(&format!(
                "{base} AND corporate_action.ex_date <= $2 ORDER BY corporate_action.ex_date"
            ))
            .bind(instrument_id)
            .bind(day)
            .fetch_all(&mut *conn)
            .await?
        }
    };
    let mut out = Vec::with_capacity(rows.len());
    for (_, _, action_type, ex_date, ratio_from, ratio_to, amount, raw) in rows {
        let raw: serde_json::Value = serde_json::from_str(&raw)
            .map_err(|e| WorkerError::python("JSONDecodeError", e.to_string()))?;
        out.push(ActionInput {
            action_type,
            ex_date,
            ratio_from: parse_opt(ratio_from)?,
            ratio_to: parse_opt(ratio_to)?,
            amount: parse_opt(amount)?,
            issue_price: issue_price(&raw),
        });
    }
    Ok(out)
}

/// `_load_raw_bars`: the unadjusted history (stored `*_raw` where present, else
/// `price / adj_factor`), `kite_adjusted` rows excluded, as Python's frame holds it.
async fn load_raw_bars(
    conn: &mut PgConnection,
    instrument_id: i64,
) -> Result<(Vec<RawBar>, Scales), WorkerError> {
    type Row = (
        NaiveDate,
        String,
        String,
        String,
        String,
        i64,
        String,
        Option<String>,
        Option<String>,
        Option<String>,
    );
    let rows: Vec<Row> = sqlx::query_as(
        "SELECT ohlcv_daily.date, ohlcv_daily.open::text, ohlcv_daily.high::text, \
         ohlcv_daily.low::text, ohlcv_daily.close_raw::text, ohlcv_daily.volume_raw, \
         ohlcv_daily.adj_factor::text, ohlcv_daily.open_raw::text, ohlcv_daily.high_raw::text, \
         ohlcv_daily.low_raw::text FROM ohlcv_daily \
         WHERE ohlcv_daily.instrument_id = $1 AND ohlcv_daily.source != $2 \
         ORDER BY ohlcv_daily.date",
    )
    .bind(instrument_id)
    .bind(KITE_ADJUSTED_SOURCE)
    .fetch_all(&mut *conn)
    .await?;
    let mut dates = Vec::with_capacity(rows.len());
    let mut opens = Vec::with_capacity(rows.len());
    let mut highs = Vec::with_capacity(rows.len());
    let mut lows = Vec::with_capacity(rows.len());
    let mut closes = Vec::with_capacity(rows.len());
    let mut volumes = Vec::with_capacity(rows.len());
    for (date, open, high, low, close_raw, volume_raw, adj, open_raw, high_raw, low_raw) in rows {
        let adj = parse(&adj)?;
        let pick = |raw: Option<String>, adjusted: &str| -> Result<WideDecimal, WorkerError> {
            match raw {
                Some(r) => parse(&r),
                None => unadjust(&parse(adjusted)?, Some(&adj)),
            }
        };
        dates.push(date);
        opens.push(Some(pick(open_raw, &open)?));
        highs.push(Some(pick(high_raw, &high)?));
        lows.push(Some(pick(low_raw, &low)?));
        closes.push(Some(parse(&close_raw)?));
        volumes.push(Some(volume_raw));
    }
    let (opens, open) = frame_column(opens)?;
    let (highs, high) = frame_column(highs)?;
    let (lows, low) = frame_column(lows)?;
    let (closes, close) = frame_column(closes)?;
    let bars = dates
        .into_iter()
        .zip(opens)
        .zip(highs)
        .zip(lows)
        .zip(closes)
        .zip(volumes)
        .map(|(((((date, o), h), l), c), v)| RawBar {
            date,
            open_raw: o,
            high_raw: h,
            low_raw: l,
            close_raw: c,
            volume_raw: v,
        })
        .collect();
    Ok((
        bars,
        Scales {
            open,
            high,
            low,
            close,
        },
    ))
}

/// One row of the rewrite, as `_price`/`_factor` leave it.
struct Rewritten {
    date: NaiveDate,
    open: Option<String>,
    high: Option<String>,
    low: Option<String>,
    close: Option<String>,
    volume: i64,
    adj_factor: WideDecimal,
}

fn rewritten(bar: &AdjustedBar) -> Result<Rewritten, WorkerError> {
    let p = |v: &Option<WideDecimal>| -> Result<Option<String>, WorkerError> {
        v.as_ref()
            .map(|x| price(x).map(|q| q.to_string()))
            .transpose()
    };
    // `int(row["volume"])`: truncation toward zero.
    let volume = match &bar.volume {
        None => 0,
        Some(v) => v
            .rescale_exact(0, Rounding::Down)
            .to_string()
            .parse::<i64>()
            .map_err(|e| WorkerError::python("OverflowError", e.to_string()))?,
    };
    Ok(Rewritten {
        date: bar.date,
        open: p(&bar.open)?,
        high: p(&bar.high)?,
        low: p(&bar.low)?,
        close: p(&bar.close)?,
        volume,
        adj_factor: factor(&bar.adj_factor)?,
    })
}

async fn write_adjusted(
    conn: &mut PgConnection,
    instrument_id: i64,
    rows: &[Rewritten],
) -> Result<i64, WorkerError> {
    let mut written = 0;
    for chunk in rows.chunks(UPSERT_CHUNK) {
        // The conflict target rows already exist; the raw columns ride along only so the
        // statement is a legal INSERT ... ON CONFLICT (Python's own shape).
        sqlx::query(
            "INSERT INTO ohlcv_daily (instrument_id, date, open, high, low, close, volume, \
             adj_factor, close_raw, volume_raw, source) \
             SELECT $1::int8, u.date, u.open::numeric, u.high::numeric, u.low::numeric, \
             u.close::numeric, u.volume, u.adj_factor::numeric, u.close::numeric, u.volume, 'nse' \
             FROM UNNEST($2::date[], $3::text[], $4::text[], $5::text[], $6::text[], $7::int8[], \
             $8::text[]) AS u(date, open, high, low, close, volume, adj_factor) \
             ON CONFLICT (instrument_id, date) DO UPDATE SET open = excluded.open, \
             high = excluded.high, low = excluded.low, close = excluded.close, \
             volume = excluded.volume, adj_factor = excluded.adj_factor",
        )
        .bind(instrument_id)
        .bind(chunk.iter().map(|r| r.date).collect::<Vec<_>>())
        .bind(chunk.iter().map(|r| r.open.clone()).collect::<Vec<_>>())
        .bind(chunk.iter().map(|r| r.high.clone()).collect::<Vec<_>>())
        .bind(chunk.iter().map(|r| r.low.clone()).collect::<Vec<_>>())
        .bind(chunk.iter().map(|r| r.close.clone()).collect::<Vec<_>>())
        .bind(chunk.iter().map(|r| r.volume).collect::<Vec<_>>())
        .bind(
            chunk
                .iter()
                .map(|r| r.adj_factor.to_string())
                .collect::<Vec<_>>(),
        )
        .execute(&mut *conn)
        .await?;
        written += to_i64(chunk.len());
    }
    Ok(written)
}

/// `_price(x) or fallback`: a zero result keeps the stored value (`Decimal("0.0000")` is falsy).
fn price_or(value: &WideDecimal, fallback: &WideDecimal) -> Result<WideDecimal, WorkerError> {
    let p = price(value)?;
    Ok(if p.is_zero() { fallback.clone() } else { p })
}

/// `_rescale_deep_segment` (AF 3.1): multiply the `kite_adjusted` OHLC (and `close_raw`) by
/// `seam_factor / adj_factor` so the M29 splice stays continuous after a later split.
async fn rescale_deep_segment(
    conn: &mut PgConnection,
    instrument_id: i64,
    seam_factor: &WideDecimal,
) -> Result<i64, WorkerError> {
    type Row = (
        NaiveDate,
        String,
        String,
        String,
        String,
        String,
        Option<String>,
    );
    let rows: Vec<Row> = sqlx::query_as(
        "SELECT ohlcv_daily.date, ohlcv_daily.open::text, ohlcv_daily.high::text, \
         ohlcv_daily.low::text, ohlcv_daily.close::text, ohlcv_daily.close_raw::text, \
         ohlcv_daily.adj_factor::text FROM ohlcv_daily \
         WHERE ohlcv_daily.instrument_id = $1 AND ohlcv_daily.source = $2",
    )
    .bind(instrument_id)
    .bind(KITE_ADJUSTED_SOURCE)
    .fetch_all(&mut *conn)
    .await?;
    if rows.is_empty() {
        return Ok(0);
    }
    let target = factor(seam_factor)?;
    let mut updated = 0;
    for (date, open, high, low, close, close_raw, adj) in rows {
        let was = match parse_opt(adj)? {
            Some(f) if !f.is_zero() => f,
            _ => WideDecimal::from(1i64),
        };
        if was == target {
            continue;
        }
        let scale = target
            .div(&was)
            .map_err(|e| WorkerError::python("InvalidOperation", e.0))?;
        let (open, high, low, close, close_raw) = (
            parse(&open)?,
            parse(&high)?,
            parse(&low)?,
            parse(&close)?,
            parse(&close_raw)?,
        );
        let new_open = price_or(&open.mul(&scale), &open)?;
        let new_high = price_or(&high.mul(&scale), &high)?;
        let new_low = price_or(&low.mul(&scale), &low)?;
        let new_close = price_or(&close.mul(&scale), &close)?;
        let new_close_raw = price_or(&close_raw.mul(&scale), &close_raw)?;
        // One UPDATE per row by primary key, as the ORM's flush issues them.
        sqlx::query(
            "UPDATE ohlcv_daily SET open = $1::numeric, high = $2::numeric, low = $3::numeric, \
             close = $4::numeric, close_raw = $5::numeric, adj_factor = $6::numeric \
             WHERE ohlcv_daily.instrument_id = $7 AND ohlcv_daily.date = $8",
        )
        .bind(new_open.to_string())
        .bind(new_high.to_string())
        .bind(new_low.to_string())
        .bind(new_close.to_string())
        .bind(new_close_raw.to_string())
        .bind(target.to_string())
        .bind(instrument_id)
        .bind(date)
        .execute(&mut *conn)
        .await?;
        updated += 1;
    }
    Ok(updated)
}

/// `reprocess_instrument(session, instrument_id, as_of=...)`.
pub(super) async fn reprocess_instrument(
    conn: &mut PgConnection,
    instrument_id: i64,
    as_of: Option<NaiveDate>,
) -> Result<InstrumentAdjustment, WorkerError> {
    let actions = load_actions(conn, instrument_id, as_of).await?;
    let (bars, scales) = load_raw_bars(conn, instrument_id).await?;
    if bars.is_empty() {
        let deep_only = rescale_deep_segment(conn, instrument_id, &WideDecimal::from(1i64)).await?;
        return Ok(InstrumentAdjustment {
            instrument_id,
            bars_rewritten: deep_only,
            actions_applied: 0,
            unquantified: Vec::new(),
            deep_rescaled: deep_only,
        });
    }
    let result = adjust::adjust_bars(&bars, scales, &actions)?;
    let rows = result
        .bars
        .iter()
        .map(rewritten)
        .collect::<Result<Vec<_>, _>>()?;
    let written = write_adjusted(conn, instrument_id, &rows).await?;

    // The earliest adjustable bar carries every later action: the factor the deep segment
    // must share for the splice to stay continuous.
    let seam = rows
        .iter()
        .min_by_key(|r| r.date)
        .map_or_else(|| WideDecimal::from(1i64), |r| r.adj_factor.clone());
    let deep_rescaled = rescale_deep_segment(conn, instrument_id, &seam).await?;

    let unquantified = result
        .unquantified_actions()
        .map(|a| {
            format!(
                "{}@{}: {}",
                a.action.action_type,
                iso(a.action.ex_date),
                a.detail
            )
        })
        .collect();
    Ok(InstrumentAdjustment {
        instrument_id,
        bars_rewritten: written + deep_rescaled,
        actions_applied: to_i64(result.effective_actions().count()),
        unquantified,
        deep_rescaled,
    })
}

/// `run_apply_adjustments(session, step, instrument_ids)`: "only instruments with new actions".
pub(super) async fn run_apply_adjustments(
    conn: &mut PgConnection,
    outcome: &mut StepOutcome,
    instrument_ids: &BTreeSet<i64>,
) -> Result<i64, WorkerError> {
    outcome.rows_in = to_i64(instrument_ids.len());
    if instrument_ids.is_empty() {
        outcome.note(
            "reason",
            pyjson::s("no instrument had a new corporate action"),
        );
        return Ok(0);
    }
    // 17 Sep 2026: the rebuild rewrites compressed history; TimescaleDB's per-transaction
    // decompression cap (100,000 tuples) failed the chain for five sessions. Lifted for this
    // transaction only (SET LOCAL), as migrations 0027 and 0044 do.
    sqlx::query("SET LOCAL timescaledb.max_tuples_decompressed_per_dml_transaction = 0")
        .execute(&mut *conn)
        .await?;
    let mut rewritten_total = 0;
    let mut applied = 0;
    let mut unquantified: Vec<String> = Vec::new();
    for &instrument_id in instrument_ids {
        // House rule 5: no `as_of` — tonight's series carries every action known now.
        let result = reprocess_instrument(conn, instrument_id, None).await?;
        rewritten_total += result.bars_rewritten;
        applied += result.actions_applied;
        unquantified.extend(
            result
                .unquantified
                .iter()
                .map(|u| format!("instrument {instrument_id}: {u}")),
        );
    }
    outcome.rows_out = rewritten_total;
    outcome.note("instruments", pyjson::int(to_i64(instrument_ids.len())));
    outcome.note("actions_applied", pyjson::int(applied));
    outcome.note(
        "unquantified_actions",
        if unquantified.is_empty() {
            baskfy_db::OrderedJson::Null
        } else {
            pyjson::strings(unquantified)
        },
    );
    Ok(rewritten_total)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

    use super::*;

    fn d(s: &str) -> WideDecimal {
        s.parse().unwrap()
    }

    #[test]
    fn the_frame_infers_its_scale_from_the_first_hundred_rows() {
        // Measured against polars 1.43.2: later values are rounded half-even onto the scale.
        let mut values: Vec<Option<WideDecimal>> = vec![Some(d("1.0000")); 100];
        for v in [
            "2.00004999",
            "2.00005",
            "2.00015",
            "2.000150001",
            "-2.00005",
        ] {
            values.push(Some(d(v)));
        }
        let (col, scale) = frame_column(values).unwrap();
        assert_eq!(scale, 4);
        let tail: Vec<String> = col[100..]
            .iter()
            .map(|v| v.as_ref().unwrap().to_string())
            .collect();
        assert_eq!(tail, ["2.0000", "2.0000", "2.0002", "2.0002", "-2.0000"]);

        let mut values: Vec<Option<WideDecimal>> = vec![Some(d("1.0000")); 99];
        values.push(Some(d("2.00004999")));
        values.push(Some(d("2.00005")));
        let (col, scale) = frame_column(values).unwrap();
        assert_eq!(scale, 8);
        assert_eq!(col[100].as_ref().unwrap().to_string(), "2.00005000");

        // An exact quotient like `Decimal("1E+2")` is scale 0, not negative.
        let (_, scale) = frame_column(vec![Some(d("1E+2"))]).unwrap();
        assert_eq!(scale, 0);
        let (col, scale) = frame_column(vec![None, Some(d("2.50"))]).unwrap();
        assert_eq!((scale, col[0].is_none()), (2, true));

        // `strict=False`: a value needing more than 38 digits at the inferred scale is null,
        // not an error (polars 1.43.2: `[1.000..01 (scale 35), 108159362.54.., 558.2000]` ->
        // `[.., None, 558.20000000000000000000000000000000000]`).
        let (col, scale) = frame_column(vec![
            Some(d("1.00000000000000000000000000000000001")),
            Some(d("108159362.5409392533149026541")),
            Some(d("558.2000")),
        ])
        .unwrap();
        assert_eq!(scale, 35);
        assert!(col[1].is_none());
        assert_eq!(
            col[2].as_ref().unwrap().to_string(),
            "558.20000000000000000000000000000000000"
        );
        // A scale past 38 is a polars panic, and the step fails with it.
        let err = frame_column(vec![Some(d("1E-40"))]).unwrap_err();
        assert_eq!(err.python_type(), "PanicException");
    }

    #[test]
    fn unadjust_divides_in_pythons_context() {
        assert_eq!(
            unadjust(&d("98.5000"), Some(&d("0.9860465070")))
                .unwrap()
                .to_string(),
            "99.89386839336980684624036704"
        );
        assert_eq!(
            unadjust(&d("100.0000"), Some(&d("1.0000000000")))
                .unwrap()
                .to_sci_string(),
            "1E+2"
        );
        assert_eq!(unadjust(&d("5.0000"), Some(&d("0E-10"))).unwrap(), d("5"));
    }

    #[test]
    fn prices_round_half_up_to_four_places_and_a_zero_keeps_the_old_value() {
        assert_eq!(price(&d("1.00005")).unwrap().to_string(), "1.0001");
        assert_eq!(price(&d("1.00015")).unwrap().to_string(), "1.0002");
        assert_eq!(
            price_or(&d("0.00004"), &d("0.0003")).unwrap().to_string(),
            "0.0003"
        );
    }

    #[test]
    fn the_issue_price_is_read_from_the_payload_when_present() {
        let raw = serde_json::json!({"subject": "Rights 1:5", "issue_price": "45.5"});
        assert_eq!(issue_price(&raw), Some(d("45.5")));
        let raw = serde_json::json!({"premium": 12.5});
        assert_eq!(issue_price(&raw), Some(d("12.5")));
        let raw = serde_json::json!({"issue_price": null, "premium": 3});
        assert_eq!(issue_price(&raw), Some(d("3")));
        let raw = serde_json::json!({"issue_price": "n/a", "premium": 3});
        assert_eq!(issue_price(&raw), None);
        assert_eq!(issue_price(&serde_json::json!({"subject": "x"})), None);
        assert_eq!(issue_price(&serde_json::json!([1])), None);
    }
}
