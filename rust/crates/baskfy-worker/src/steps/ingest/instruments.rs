//! Step 1 — `refresh_instruments` and the listings register folded into it. Ports
//! `baskfy_worker.tasks.instruments` (`run_refresh_instruments`, `active_instruments`, `_merge`,
//! `_batched`) and `baskfy_worker.tasks.listings` (`run_refresh_listings`, `store_listings`).
//!
//! Rows are never deleted (docs/04); the upsert keys on `(exchange_id, symbol)` (0039: `series`
//! is mutable) and fires `DO UPDATE` only when a column differs, so a re-run is a no-op
//! (house rule 7). Every proposed row consumes one `instrument.id` identity value whether it
//! inserts or not, so the rows per statement are Python's rows.

use std::collections::{BTreeMap, BTreeSet, HashSet};

use baskfy_db::OrderedJson;
use baskfy_providers::composite::CompositeProvider;
use baskfy_providers::records::{InstrumentFields, InstrumentType, ListingFields};
use chrono::NaiveDate;
use rust_decimal::Decimal;
use sqlx::PgConnection;

use super::ActiveInstrument;
use crate::calendar::NSE_EXCHANGE_ID;
use crate::error::WorkerError;
use crate::pyjson;
use crate::steps::StepOutcome;

/// PostgreSQL's wire-protocol ceiling on bind parameters in one statement.
#[cfg(test)]
pub(super) const MAX_BIND_PARAMETERS: usize = 32767;
/// One per column the instrument upsert writes.
#[cfg(test)]
pub(super) const UPSERT_COLUMNS_PER_ROW: usize = 11;
/// `UPSERT_BATCH_ROWS`: 2,048 rows x 11 columns stays under the ceiling.
pub(super) const UPSERT_BATCH_ROWS: usize = 2048;

/// `REGISTER_COLUMNS`: what the NSE register is authoritative for.
const REGISTER_COLUMNS: [&str; 5] = ["name", "series", "isin", "listed_on", "face_value"];

/// `mutable` in `run_refresh_instruments`: every column the merge may correct.
const MUTABLE_COLUMNS: [&str; 9] = [
    "name",
    "series",
    "instrument_type",
    "isin",
    "kite_token",
    "lot_size",
    "face_value",
    "listed_on",
    "is_active",
];

fn now_utc() -> chrono::DateTime<chrono::Utc> {
    use baskfy_providers::clock::Clock as _;
    baskfy_providers::clock::system_clock().now()
}

/// `_batched(rows, size)`: successive slices, at most `size` long.
///
/// # Errors
/// `ValueError` for a size below one (it would never terminate).
pub(super) fn batched<T>(
    rows: &[T],
    size: usize,
) -> Result<std::slice::Chunks<'_, T>, WorkerError> {
    if size < 1 {
        return Err(WorkerError::python(
            "ValueError",
            format!("batch size must be at least 1; got {size}"),
        ));
    }
    Ok(rows.chunks(size))
}

/// Python truthiness of an optional string (`None` and `""` are falsy).
fn truthy_str(value: Option<&String>) -> bool {
    value.is_some_and(|s| !s.is_empty())
}

/// `_merge(records, listings)`: Kite supplies identity and the token; NSE supplies series, ISIN,
/// listing date and face value. Symbols only NSE lists still enter the universe. Sorted by
/// symbol (code-point order, as Python sorts `str`).
pub(super) fn merge(
    records: Vec<InstrumentFields>,
    listings: &[ListingFields],
) -> Vec<InstrumentFields> {
    let mut by_symbol: BTreeMap<String, InstrumentFields> = BTreeMap::new();
    for record in records {
        by_symbol.insert(record.symbol.clone(), record);
    }
    for listing in listings {
        match by_symbol.get_mut(&listing.symbol) {
            None => {
                let mut fresh =
                    InstrumentFields::new(&listing.symbol, &listing.name, InstrumentType::Eq);
                fresh.series.clone_from(&listing.series);
                fresh.isin.clone_from(&listing.isin);
                fresh.listed_on = listing.listed_on;
                fresh.face_value = listing.face_value;
                by_symbol.insert(listing.symbol.clone(), fresh);
            }
            Some(existing) => {
                // `listing.x or existing.x`: Python truthiness, so "" and Decimal(0) fall back.
                if truthy_str(listing.series.as_ref()) {
                    existing.series.clone_from(&listing.series);
                }
                if truthy_str(listing.isin.as_ref()) {
                    existing.isin.clone_from(&listing.isin);
                }
                if listing.listed_on.is_some() {
                    existing.listed_on = listing.listed_on;
                }
                if listing.face_value.is_some_and(|v| !v.is_zero()) {
                    existing.face_value = listing.face_value;
                }
            }
        }
    }
    by_symbol.into_values().collect()
}

/// `run_refresh_instruments`: upsert the instrument master. Returns the rows written.
pub(super) async fn run_refresh_instruments(
    conn: &mut PgConnection,
    provider: &CompositeProvider,
    outcome: &mut StepOutcome,
    as_of: Option<NaiveDate>,
) -> Result<i64, WorkerError> {
    let records: Vec<InstrumentFields> = match provider.list_instruments().await {
        Ok(rows) => rows.into_iter().map(|r| (*r).clone()).collect(),
        Err(e) if e.is_provider_error() => {
            // Recorded, not swallowed: the operator sees which half of the merge was missing.
            outcome.note("list_instruments_error", pyjson::s(e.to_string()));
            Vec::new()
        }
        Err(e) => return Err(e.into()),
    };
    let listings: Vec<ListingFields> = match provider.listings().await {
        Ok(rows) => rows.into_iter().map(|r| (*r).clone()).collect(),
        Err(e) if e.is_provider_error() => {
            outcome.note("listings_error", pyjson::s(e.to_string()));
            Vec::new()
        }
        Err(e) => return Err(e.into()),
    };
    let from_kite = records.len();
    outcome.rows_in = to_i64(from_kite + listings.len());
    if records.is_empty() && listings.is_empty() {
        outcome.note(
            "reason",
            pyjson::s("no provider could serve instruments or listings"),
        );
        return Ok(0);
    }
    let merged = merge(records, &listings);
    for batch in batched(&merged, UPSERT_BATCH_ROWS)? {
        upsert_instruments(conn, batch).await?;
    }
    let written = to_i64(merged.len());
    outcome.rows_out = written;
    outcome.note("from_kite", pyjson::int(to_i64(from_kite)));
    outcome.note("from_listings", pyjson::int(to_i64(listings.len())));
    outcome.note("merged", pyjson::int(written));
    outcome.note(
        "as_of",
        pyjson::s(as_of.map_or_else(|| "None".to_owned(), iso)),
    );
    Ok(written)
}

fn iso(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

fn to_i64(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

fn distinct_from(columns: &[&str]) -> String {
    columns
        .iter()
        .map(|c| format!("instrument.{c} IS DISTINCT FROM excluded.{c}"))
        .collect::<Vec<_>>()
        .join(" OR ")
}

async fn upsert_instruments(
    conn: &mut PgConnection,
    batch: &[InstrumentFields],
) -> Result<(), WorkerError> {
    let mut symbols = Vec::with_capacity(batch.len());
    let mut names = Vec::with_capacity(batch.len());
    let mut series = Vec::with_capacity(batch.len());
    let mut types = Vec::with_capacity(batch.len());
    let mut isins = Vec::with_capacity(batch.len());
    let mut tokens = Vec::with_capacity(batch.len());
    let mut lots: Vec<Option<i32>> = Vec::with_capacity(batch.len());
    let mut faces: Vec<Option<Decimal>> = Vec::with_capacity(batch.len());
    let mut listed: Vec<Option<NaiveDate>> = Vec::with_capacity(batch.len());
    for r in batch {
        symbols.push(r.symbol.clone());
        names.push(r.name.clone());
        series.push(r.series.clone());
        types.push(r.instrument_type.as_str().to_owned());
        isins.push(r.isin.clone());
        tokens.push(r.kite_token);
        lots.push(match r.lot_size {
            None => None,
            Some(v) => Some(i32::try_from(v).map_err(|_| {
                WorkerError::python(
                    "DataError",
                    format!("invalid input for query argument (value out of int32 range): {v}"),
                )
            })?),
        });
        faces.push(r.face_value);
        listed.push(r.listed_on);
    }
    let set: Vec<String> = MUTABLE_COLUMNS
        .iter()
        .map(|c| format!("{c} = excluded.{c}"))
        .collect();
    let sql = format!(
        "INSERT INTO instrument (exchange_id, symbol, name, series, instrument_type, isin, \
         kite_token, lot_size, face_value, listed_on, is_active) \
         SELECT $1::smallint, u.symbol, u.name, u.series, u.instrument_type, u.isin, \
         u.kite_token, u.lot_size, u.face_value, u.listed_on, true \
         FROM UNNEST($2::varchar[], $3::varchar[], $4::varchar[], $5::varchar[], $6::varchar[], \
         $7::int8[], $8::int4[], $9::numeric[], $10::date[]) \
         AS u(symbol, name, series, instrument_type, isin, kite_token, lot_size, face_value, \
         listed_on) \
         ON CONFLICT (exchange_id, symbol) DO UPDATE SET {}, updated_at = $11 WHERE {}",
        set.join(", "),
        distinct_from(&MUTABLE_COLUMNS)
    );
    sqlx::query(&sql)
        .bind(NSE_EXCHANGE_ID)
        .bind(symbols)
        .bind(names)
        .bind(series)
        .bind(types)
        .bind(isins)
        .bind(tokens)
        .bind(lots)
        .bind(faces)
        .bind(listed)
        .bind(now_utc())
        .execute(&mut *conn)
        .await?;
    Ok(())
}

/// `active_instruments`: a cash-market name (`series` set by the NSE register), or anything
/// with history — never every contract in Kite's dump (M78). Ordered by symbol.
pub(super) async fn active_instruments(
    conn: &mut PgConnection,
) -> Result<Vec<ActiveInstrument>, WorkerError> {
    let rows: Vec<(i64, String, Option<i64>)> = sqlx::query_as(
        "SELECT instrument.id, instrument.symbol, instrument.kite_token FROM instrument \
         WHERE instrument.delisted_on IS NULL AND (instrument.series IS NOT NULL OR \
         (EXISTS (SELECT ohlcv_daily.instrument_id FROM ohlcv_daily \
         WHERE ohlcv_daily.instrument_id = instrument.id))) ORDER BY instrument.symbol",
    )
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows
        .into_iter()
        .map(|(instrument_id, symbol, kite_token)| ActiveInstrument {
            instrument_id,
            symbol,
            kite_token,
        })
        .collect())
}

/// `ListingsResult`.
#[derive(Debug, Default, PartialEq, Eq)]
pub(super) struct ListingsResult {
    pub(super) rows_in: usize,
    pub(super) rows_written: usize,
    pub(super) new_symbols: Vec<String>,
    /// Symbols offered more than once (each repeat once more); the first record was kept.
    pub(super) duplicate_symbols: Vec<String>,
}

/// `run_refresh_listings`: the main register (raising, after noting, on a provider error) plus
/// the Emerge register (recorded and carried on failure).
pub(super) async fn run_refresh_listings(
    conn: &mut PgConnection,
    provider: &CompositeProvider,
    outcome: &mut StepOutcome,
) -> Result<usize, WorkerError> {
    let mut records: Vec<ListingFields> = match provider.listings().await {
        Ok(rows) => rows.into_iter().map(|r| (*r).clone()).collect(),
        Err(e) => {
            if e.is_provider_error() {
                outcome.note("error", pyjson::s(e.to_string()));
            }
            return Err(e.into());
        }
    };
    let mut sme_error: Option<String> = None;
    let mut sme_count = 0usize;
    match provider.sme_listings().await {
        Ok(rows) => {
            sme_count = rows.len();
            records.extend(rows.into_iter().map(|r| (*r).clone()));
        }
        Err(e) if e.is_provider_error() => sme_error = Some(e.to_string()),
        Err(e) => return Err(e.into()),
    }
    let result = store_listings(conn, records).await?;
    outcome.rows_in = to_i64(result.rows_in);
    outcome.rows_out = to_i64(result.rows_written);
    let first: Vec<String> = result.new_symbols.iter().take(50).cloned().collect();
    outcome.note(
        "new_symbols",
        if first.is_empty() {
            OrderedJson::Null
        } else {
            pyjson::strings(first)
        },
    );
    outcome.note(
        "new_symbol_count",
        pyjson::int(to_i64(result.new_symbols.len())),
    );
    outcome.note(
        "duplicate_symbols",
        if result.duplicate_symbols.is_empty() {
            OrderedJson::Null
        } else {
            pyjson::strings(result.duplicate_symbols.clone())
        },
    );
    outcome.note("sme_rows", pyjson::int(to_i64(sme_count)));
    outcome.note("sme_error", pyjson::opt_s(sme_error));
    Ok(result.rows_written)
}

/// `dedupe_listings`: one record per symbol, the FIRST offered kept (the main register is
/// passed first, so a company migrating from Emerge keeps its main-board row — PARIN, 24 Sep
/// 2026), and every repeat reported.
pub(super) fn dedupe_listings(records: Vec<ListingFields>) -> (Vec<ListingFields>, Vec<String>) {
    let mut seen: HashSet<String> = HashSet::new();
    let mut unique = Vec::with_capacity(records.len());
    let mut duplicates = Vec::new();
    for record in records {
        if seen.contains(&record.symbol) {
            duplicates.push(record.symbol.clone());
            continue;
        }
        seen.insert(record.symbol.clone());
        unique.push(record);
    }
    (unique, duplicates)
}

/// `store_listings`: upsert the register onto `instrument` in one statement.
pub(super) async fn store_listings(
    conn: &mut PgConnection,
    records: Vec<ListingFields>,
) -> Result<ListingsResult, WorkerError> {
    let mut result = ListingsResult {
        rows_in: records.len(),
        ..ListingsResult::default()
    };
    if records.is_empty() {
        return Ok(result);
    }
    let (records, duplicates) = dedupe_listings(records);
    result.duplicate_symbols = duplicates;

    let symbols: Vec<String> = records.iter().map(|r| r.symbol.clone()).collect();
    let existing: Vec<(String,)> = sqlx::query_as(
        "SELECT instrument.symbol FROM instrument WHERE instrument.symbol = ANY($1)",
    )
    .bind(&symbols)
    .fetch_all(&mut *conn)
    .await?;
    let existing: HashSet<String> = existing.into_iter().map(|(s,)| s).collect();
    let offered: BTreeSet<String> = symbols.iter().cloned().collect();
    result.new_symbols = offered
        .into_iter()
        .filter(|s| !existing.contains(s))
        .collect();

    let names: Vec<String> = records.iter().map(|r| r.name.clone()).collect();
    let series: Vec<Option<String>> = records.iter().map(|r| r.series.clone()).collect();
    let isins: Vec<Option<String>> = records.iter().map(|r| r.isin.clone()).collect();
    let listed: Vec<Option<NaiveDate>> = records.iter().map(|r| r.listed_on).collect();
    let faces: Vec<Option<Decimal>> = records.iter().map(|r| r.face_value).collect();
    let set: Vec<String> = REGISTER_COLUMNS
        .iter()
        .map(|c| format!("{c} = excluded.{c}"))
        .collect();
    let sql = format!(
        "INSERT INTO instrument (exchange_id, symbol, name, series, instrument_type, isin, \
         listed_on, face_value, is_active) \
         SELECT $1::smallint, u.symbol, u.name, u.series, 'EQ', u.isin, u.listed_on, \
         u.face_value, true \
         FROM UNNEST($2::varchar[], $3::varchar[], $4::varchar[], $5::varchar[], $6::date[], \
         $7::numeric[]) AS u(symbol, name, series, isin, listed_on, face_value) \
         ON CONFLICT (exchange_id, symbol) DO UPDATE SET {}, updated_at = $8 WHERE {}",
        set.join(", "),
        distinct_from(&REGISTER_COLUMNS)
    );
    sqlx::query(&sql)
        .bind(NSE_EXCHANGE_ID)
        .bind(&symbols)
        .bind(names)
        .bind(series)
        .bind(isins)
        .bind(listed)
        .bind(faces)
        .bind(now_utc())
        .execute(&mut *conn)
        .await?;
    result.rows_written = records.len();
    Ok(result)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

    use super::*;

    fn listing(symbol: &str, series: Option<&str>) -> ListingFields {
        let mut l = ListingFields::new(symbol, &format!("{symbol} Ltd"));
        l.series = series.map(str::to_owned);
        l
    }

    #[test]
    fn the_batch_size_stays_under_postgresqls_ceiling() {
        const { assert!(UPSERT_BATCH_ROWS * UPSERT_COLUMNS_PER_ROW <= MAX_BIND_PARAMETERS) };
        assert_eq!(UPSERT_COLUMNS_PER_ROW, MUTABLE_COLUMNS.len() + 2);
    }

    #[test]
    fn batched_covers_every_row_exactly_once_and_in_order() {
        let rows: Vec<i32> = (0..5000).collect();
        let batches: Vec<&[i32]> = batched(&rows, UPSERT_BATCH_ROWS).unwrap().collect();
        assert_eq!(
            batches.iter().map(|b| b.len()).collect::<Vec<_>>(),
            [2048, 2048, 904]
        );
        let flat: Vec<i32> = batches.concat();
        assert_eq!(flat, rows);
    }

    #[test]
    fn batched_on_an_empty_list_yields_nothing() {
        let rows: Vec<i32> = Vec::new();
        assert_eq!(batched(&rows, 10).unwrap().count(), 0);
    }

    #[test]
    fn batched_refuses_a_size_that_would_never_terminate() {
        let err = batched(&[1], 0).unwrap_err();
        assert_eq!(err.python_type(), "ValueError");
        assert!(err.to_string().contains("at least 1"));
    }

    #[test]
    fn the_register_fills_in_what_kite_does_not_publish() {
        let mut kite = InstrumentFields::new("RELIANCE", "RELIANCE INDUSTRIES", InstrumentType::Eq);
        kite.kite_token = Some(738_561);
        let mut l = listing("RELIANCE", Some("EQ"));
        l.isin = Some("INE002A01018".to_owned());
        l.face_value = Some(Decimal::new(10, 0));
        let merged = merge(vec![kite], &[l]);
        assert_eq!(merged.len(), 1);
        assert_eq!(merged[0].series.as_deref(), Some("EQ"));
        assert_eq!(merged[0].isin.as_deref(), Some("INE002A01018"));
        assert_eq!(merged[0].kite_token, Some(738_561));
        assert_eq!(merged[0].name, "RELIANCE INDUSTRIES");
    }

    #[test]
    fn a_symbol_only_nse_lists_still_enters_the_universe_sorted() {
        let kite = InstrumentFields::new("ZEN", "ZEN", InstrumentType::Etf);
        let merged = merge(vec![kite], &[listing("ABC", Some("SM"))]);
        let symbols: Vec<&str> = merged.iter().map(|r| r.symbol.as_str()).collect();
        assert_eq!(symbols, ["ABC", "ZEN"]);
        assert_eq!(merged[0].instrument_type, InstrumentType::Eq);
        assert_eq!(merged[0].series.as_deref(), Some("SM"));
        assert_eq!(merged[1].instrument_type, InstrumentType::Etf);
    }

    #[test]
    fn a_falsy_register_value_keeps_kites() {
        let mut kite = InstrumentFields::new("X", "X", InstrumentType::Eq);
        kite.series = Some("BE".to_owned());
        kite.face_value = Some(Decimal::new(5, 0));
        let mut l = listing("X", Some(""));
        l.face_value = Some(Decimal::ZERO);
        let merged = merge(vec![kite], &[l]);
        assert_eq!(merged[0].series.as_deref(), Some("BE"));
        assert_eq!(merged[0].face_value, Some(Decimal::new(5, 0)));
    }

    #[test]
    fn a_symbol_in_both_registers_is_written_once_main_board_first() {
        let (unique, dups) = dedupe_listings(vec![
            listing("PARIN", Some("EQ")),
            listing("OTHER", Some("EQ")),
            listing("PARIN", Some("SM")),
            listing("PARIN", Some("ST")),
        ]);
        assert_eq!(unique.len(), 2);
        assert_eq!(unique[0].series.as_deref(), Some("EQ"));
        assert_eq!(dups, ["PARIN", "PARIN"]);
    }
}
