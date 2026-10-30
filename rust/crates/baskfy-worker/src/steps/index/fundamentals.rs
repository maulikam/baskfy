//! Equity fundamentals — folded into `refresh_index_snapshots` (T9.1). Ports
//! `baskfy_worker.tasks.fundamentals`.
//!
//! Upserts `fundamental_daily` for one as-of date from `provider.equity_fundamentals`. A night
//! where NSE does not quote is still publishable — NULL is the honest state.
//!
//! Market cap is issued shares × that day's `close_raw` / ₹1 crore when a bar exists, else the
//! quote's last price; the quoted P/E is re-priced onto the day's print (house rule 5). Both are
//! Python `Decimal` arithmetic in the default context, rounded half-up at write ([`WideDecimal`]).

use std::collections::{HashMap, HashSet};

use baskfy_core::decimal::{Rounding, WideDecimal};
use baskfy_db::OrderedJson;
use baskfy_providers::composite::CompositeProvider;
use baskfy_providers::records::EquityFundamental;
use chrono::NaiveDate;
use rust_decimal::Decimal;
use rust_decimal::prelude::ToPrimitive;
use sqlx::PgConnection;

use crate::error::WorkerError;
use crate::pyjson;
use crate::steps::StepOutcome;

/// `_CRORE = Decimal("10000000")`.
const CRORE: i64 = 10_000_000;

/// `FundamentalsResult`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct FundamentalsResult {
    pub(crate) rows_in: i64,
    pub(crate) rows_written: i64,
    pub(crate) unmatched: i64,
}

/// `fundamentals_scope(session, on)`: `(symbol, series)` for every instrument that traded on
/// `on`, one entry per symbol (the first series in the database's order). The day's own universe,
/// not the listing register: at NSE's 1 req/s that is forty minutes instead of three hours.
pub(crate) async fn fundamentals_scope(
    conn: &mut PgConnection,
    on: NaiveDate,
) -> Result<Vec<(String, Option<String>)>, WorkerError> {
    let rows: Vec<(String, Option<String>)> = sqlx::query_as(
        "SELECT instrument.symbol, instrument.series FROM instrument \
         JOIN ohlcv_daily ON ohlcv_daily.instrument_id = instrument.id \
         WHERE ohlcv_daily.date = $1 ORDER BY instrument.symbol, instrument.series",
    )
    .bind(on)
    .fetch_all(&mut *conn)
    .await?;
    let mut seen: HashSet<String> = HashSet::new();
    let mut scope = Vec::new();
    for (symbol, series) in rows {
        if seen.insert(symbol.clone()) {
            scope.push((symbol, series));
        }
    }
    Ok(scope)
}

/// `run_fetch_fundamentals(session, provider, outcome, on, scope)`.
///
/// A provider error is noted (`error=str(exc)`) and raised; the caller folds it into step 6.
pub(crate) async fn run_fetch_fundamentals(
    conn: &mut PgConnection,
    provider: &CompositeProvider,
    outcome: &mut StepOutcome,
    on: NaiveDate,
    scope: &[(String, Option<String>)],
) -> Result<i64, WorkerError> {
    let symbols: Vec<String> = scope.iter().map(|(s, _)| s.clone()).collect();
    // `{symbol: series for symbol, series in scope if series}` — a falsy series is no hint.
    let series_by_symbol: HashMap<String, String> = scope
        .iter()
        .filter_map(|(s, series)| {
            series
                .as_ref()
                .filter(|v| !v.is_empty())
                .map(|v| (s.clone(), v.clone()))
        })
        .collect();
    let records = match provider
        .equity_fundamentals(on, &symbols, Some(&series_by_symbol))
        .await
    {
        Ok(records) => records,
        Err(exc) => {
            if exc.is_provider_error() {
                outcome.note("error", pyjson::s(exc.to_string()));
            }
            return Err(WorkerError::Provider(exc));
        }
    };

    let result = store_fundamentals(conn, on, &records).await?;
    outcome.rows_in = result.rows_in;
    outcome.rows_out = result.rows_written;
    outcome.note("date", pyjson::s(on.format("%Y-%m-%d").to_string()));
    outcome.note(
        "unmatched",
        if result.unmatched == 0 {
            OrderedJson::Null
        } else {
            pyjson::int(result.unmatched)
        },
    );
    Ok(result.rows_written)
}

/// One row bound for `fundamental_daily`.
#[derive(Debug, Clone, PartialEq, Eq)]
struct FundamentalRow {
    instrument_id: i64,
    marketcap_cr: Option<i64>,
    pe: Option<Decimal>,
    pb: Option<Decimal>,
    div_yield: Option<Decimal>,
    shares_outstanding: Option<i64>,
}

/// `store_fundamentals(session, on, records)`.
pub(crate) async fn store_fundamentals(
    conn: &mut PgConnection,
    on: NaiveDate,
    records: &[EquityFundamental],
) -> Result<FundamentalsResult, WorkerError> {
    let mut result = FundamentalsResult {
        rows_in: i64::try_from(records.len()).unwrap_or(i64::MAX),
        ..FundamentalsResult::default()
    };
    if records.is_empty() {
        return Ok(result);
    }

    let wanted: Vec<String> = records
        .iter()
        .map(|r| r.symbol.to_uppercase())
        .collect::<HashSet<String>>()
        .into_iter()
        .collect();
    let mapped: HashMap<String, i64> = sqlx::query_as::<_, (String, i64)>(
        "SELECT instrument.symbol, instrument.id FROM instrument \
         WHERE instrument.symbol = ANY($1::varchar[])",
    )
    .bind(&wanted)
    .fetch_all(&mut *conn)
    .await?
    .into_iter()
    .map(|(symbol, id)| (symbol.to_uppercase(), id))
    .collect();
    let mapped_ids: Vec<i64> = mapped.values().copied().collect();
    let mut closes: HashMap<i64, Option<WideDecimal>> = HashMap::new();
    for (instrument_id, close_raw) in sqlx::query_as::<_, (i64, Option<String>)>(
        "SELECT ohlcv_daily.instrument_id, ohlcv_daily.close_raw::text FROM ohlcv_daily \
         WHERE ohlcv_daily.date = $1 AND ohlcv_daily.instrument_id = ANY($2::bigint[])",
    )
    .bind(on)
    .bind(&mapped_ids)
    .fetch_all(&mut *conn)
    .await?
    {
        let close = close_raw
            .map(|c| c.parse::<WideDecimal>())
            .transpose()
            .map_err(decimal_error)?;
        closes.insert(instrument_id, close);
    }

    let mut values: Vec<FundamentalRow> = Vec::new();
    for record in records {
        let Some(instrument_id) = mapped.get(&record.symbol.to_uppercase()).copied() else {
            result.unmatched += 1;
            continue;
        };
        let close = closes.get(&instrument_id).cloned().flatten();
        let last_price = record.last_price.map(WideDecimal::from);
        let marketcap_cr = match marketcap_cr(
            record.shares_outstanding,
            close.as_ref(),
            last_price.as_ref(),
        )? {
            Some(cap) => Some(cap),
            None => record.marketcap_cr,
        };
        values.push(FundamentalRow {
            instrument_id,
            marketcap_cr,
            pe: pe_on(record.pe, close.as_ref(), last_price.as_ref())?,
            pb: record.pb,
            div_yield: record.div_yield,
            shares_outstanding: record.shares_outstanding,
        });
    }

    if values.is_empty() {
        return Ok(result);
    }

    sqlx::query(
        "INSERT INTO fundamental_daily \
         (instrument_id, date, marketcap_cr, pe, pb, div_yield, shares_outstanding) \
         SELECT * FROM UNNEST($1::bigint[], $2::date[], $3::bigint[], $4::numeric[], \
         $5::numeric[], $6::numeric[], $7::bigint[]) \
         ON CONFLICT (instrument_id, date) DO UPDATE SET marketcap_cr = excluded.marketcap_cr, \
         pe = excluded.pe, pb = excluded.pb, div_yield = excluded.div_yield, \
         shares_outstanding = excluded.shares_outstanding",
    )
    .bind(values.iter().map(|v| v.instrument_id).collect::<Vec<_>>())
    .bind(vec![on; values.len()])
    .bind(values.iter().map(|v| v.marketcap_cr).collect::<Vec<_>>())
    .bind(values.iter().map(|v| v.pe).collect::<Vec<_>>())
    .bind(values.iter().map(|v| v.pb).collect::<Vec<_>>())
    .bind(values.iter().map(|v| v.div_yield).collect::<Vec<_>>())
    .bind(
        values
            .iter()
            .map(|v| v.shares_outstanding)
            .collect::<Vec<_>>(),
    )
    .execute(&mut *conn)
    .await?;
    result.rows_written = i64::try_from(values.len()).unwrap_or(i64::MAX);
    Ok(result)
}

fn decimal_error(e: baskfy_core::decimal::DecimalError) -> WorkerError {
    WorkerError::python("InvalidOperation", e.to_string())
}

/// `_pe_on(quoted_pe, close_raw, last_price)`: re-price the quoted P/E onto `on`'s exchange
/// print (house rule 5) — `(quoted_pe * close_raw / last_price).quantize(0.0001, HALF_UP)`.
/// A missing or non-positive input leaves the quoted ratio alone; an absent ratio stays absent.
fn pe_on(
    quoted_pe: Option<Decimal>,
    close_raw: Option<&WideDecimal>,
    last_price: Option<&WideDecimal>,
) -> Result<Option<Decimal>, WorkerError> {
    let Some(pe) = quoted_pe else {
        return Ok(None);
    };
    if pe <= Decimal::ZERO {
        return Ok(Some(pe));
    }
    let zero = WideDecimal::zero();
    let (Some(close), Some(last)) = (close_raw, last_price) else {
        return Ok(Some(pe));
    };
    if *close <= zero || *last <= zero {
        return Ok(Some(pe));
    }
    let repriced = WideDecimal::from(pe)
        .mul(close)
        .div(last)
        .map_err(decimal_error)?
        .quantize(-4, Rounding::HalfUp)
        .map_err(decimal_error)?;
    Ok(Some(repriced.try_to_decimal().map_err(decimal_error)?))
}

/// `_marketcap_cr(shares, close_raw, last_price)`: prefer the exchange print, fall back to the
/// quote's last price; `int((shares * price / 1e7).quantize(1, HALF_UP))`.
fn marketcap_cr(
    shares: Option<i64>,
    close_raw: Option<&WideDecimal>,
    last_price: Option<&WideDecimal>,
) -> Result<Option<i64>, WorkerError> {
    let zero = WideDecimal::zero();
    let price = match close_raw {
        Some(c) if *c > zero => Some(c),
        _ => last_price,
    };
    let (Some(shares), Some(price)) = (shares, price) else {
        return Ok(None);
    };
    if *price <= zero {
        return Ok(None);
    }
    let crores = WideDecimal::from(shares)
        .mul(price)
        .div(&WideDecimal::from(CRORE))
        .map_err(decimal_error)?
        .quantize(0, Rounding::HalfUp)
        .map_err(decimal_error)?;
    let as_int = crores
        .try_to_decimal()
        .map_err(decimal_error)?
        .to_i64()
        .ok_or_else(|| {
            WorkerError::python(
                "DBAPIError",
                format!("invalid input for query argument: {crores} (value out of int64 range)"),
            )
        })?;
    Ok(Some(as_int))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn w(s: &str) -> WideDecimal {
        s.parse().unwrap()
    }

    fn dec(s: &str) -> Decimal {
        s.parse().unwrap()
    }

    // test_fundamentals.TestPeVintage.
    #[test]
    fn the_ratio_is_repriced_onto_the_target_dates_close() {
        // eps = 200/20 = 10; on `on` the print was 150, so the P/E that day was 15.
        let pe = pe_on(Some(dec("20")), Some(&w("150")), Some(&w("200"))).unwrap();
        assert_eq!(pe.unwrap().to_string(), "15.0000");
    }

    #[test]
    fn a_same_day_fetch_changes_nothing() {
        let pe = pe_on(Some(dec("20")), Some(&w("150")), Some(&w("150"))).unwrap();
        assert_eq!(pe.unwrap().to_string(), "20.0000");
    }

    #[test]
    fn a_missing_close_leaves_the_quoted_ratio_alone() {
        let pe = pe_on(Some(dec("20")), None, Some(&w("200"))).unwrap();
        assert_eq!(pe.unwrap().to_string(), "20");
    }

    #[test]
    fn an_absent_ratio_stays_absent() {
        assert_eq!(pe_on(None, Some(&w("150")), Some(&w("200"))).unwrap(), None);
    }

    #[test]
    fn a_non_positive_ratio_or_price_is_left_as_quoted() {
        assert_eq!(
            pe_on(Some(dec("-3.5")), Some(&w("150")), Some(&w("200")))
                .unwrap()
                .unwrap()
                .to_string(),
            "-3.5"
        );
        assert_eq!(
            pe_on(Some(dec("12.30")), Some(&w("0")), Some(&w("200")))
                .unwrap()
                .unwrap()
                .to_string(),
            "12.30"
        );
    }

    #[test]
    fn the_stored_pe_is_rounded_half_up_after_pythons_division() {
        // test_fundamentals.TestStoreFundamentals: 24.50 * 1850.25 / 9999 -> 4.5336.
        let pe = pe_on(Some(dec("24.50")), Some(&w("1850.2500")), Some(&w("9999"))).unwrap();
        assert_eq!(pe.unwrap().to_string(), "4.5336");
        // A tie at the fourth place rounds up: 1.00005 * 1 / 1.
        let pe = pe_on(Some(dec("1.00005")), Some(&w("1")), Some(&w("1"))).unwrap();
        assert_eq!(pe.unwrap().to_string(), "1.0001");
    }

    // test_fundamentals.TestMarketcapMath.
    #[test]
    fn close_raw_beats_the_quote_last_price() {
        assert_eq!(
            marketcap_cr(Some(4_148_506_959), Some(&w("1850.25")), Some(&w("9999"))).unwrap(),
            Some(767_578)
        );
    }

    #[test]
    fn a_missing_price_is_none_not_zero() {
        assert_eq!(marketcap_cr(Some(1_000_000), None, None).unwrap(), None);
        assert_eq!(marketcap_cr(None, Some(&w("10")), None).unwrap(), None);
    }

    #[test]
    fn a_non_positive_close_falls_back_to_the_last_price() {
        // 1e7 shares at 15 -> 15 crore; at 0.5 -> 0.5 crore, half up to 1.
        assert_eq!(
            marketcap_cr(Some(10_000_000), Some(&w("0")), Some(&w("15"))).unwrap(),
            Some(15)
        );
        assert_eq!(
            marketcap_cr(Some(10_000_000), None, Some(&w("0.5"))).unwrap(),
            Some(1)
        );
        assert_eq!(
            marketcap_cr(Some(10_000_000), Some(&w("-1")), Some(&w("-1"))).unwrap(),
            None
        );
    }
}
