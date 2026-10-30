//! Ports `baskfy_worker.fno.underlying`: the seam for `fo_underlying_daily` (`docs/fno/03` §2).
//! Reads a lookback window of `fo_contract_daily`, hands it to
//! [`baskfy_core::fno::underlying::derive_underlying`] (pure) and upserts that session's rows —
//! **never touching `in_ban`**, which the ban list writes.

use baskfy_core::fno::config::SeriesConfig;
use baskfy_core::fno::series::cols;
use baskfy_core::fno::underlying::{DERIVED_COLUMNS, derive_underlying};
use chrono::{Days, NaiveDate};
use polars::prelude::{
    Column, DataFrame, Int128Chunked, IntoColumn, IntoSeries, NamedFrom, NewChunkedArray,
    PlSmallStr, Series,
};
use rust_decimal::Decimal;
use sqlx::PgConnection;

use super::common::fno_err;
use crate::calendar::NSE_EXCHANGE_ID;
use crate::error::WorkerError;

/// Calendar days of history the derivation reads.
pub(crate) const LOOKBACK_DAYS: u64 = 120;
/// Calendar days of future calendar `iv_atm` needs.
pub(crate) const CALENDAR_AHEAD_DAYS: u64 = 100;

/// `trading_days`: NSE sessions in `[start, end]`, oldest first.
pub(crate) async fn trading_days(
    conn: &mut PgConnection,
    start: NaiveDate,
    end: NaiveDate,
) -> Result<Vec<NaiveDate>, WorkerError> {
    let rows: Vec<(NaiveDate,)> = sqlx::query_as(
        "SELECT date FROM trading_day WHERE exchange_id = $1 AND is_trading_day IS true \
         AND date >= $2 AND date <= $3 ORDER BY date",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(start)
    .bind(end)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows.into_iter().map(|(d,)| d).collect())
}

/// `trade_date - days`, saturating.
pub(crate) fn minus(day: NaiveDate, days: u64) -> NaiveDate {
    day.checked_sub_days(Days::new(days)).unwrap_or(day)
}

/// `trade_date + days`, saturating.
pub(crate) fn plus(day: NaiveDate, days: u64) -> NaiveDate {
    day.checked_add_days(Days::new(days)).unwrap_or(day)
}

/// A `Decimal(38, 2)` column, as polars infers one from `numeric(18,2)` values.
pub(crate) fn decimal_column(
    name: &str,
    values: &[Option<Decimal>],
) -> Result<Column, WorkerError> {
    let mantissas = values.iter().map(|v| {
        v.map(|d| {
            let mut x = d;
            x.rescale(2);
            x.mantissa()
        })
    });
    let series = Int128Chunked::from_iter_options(PlSmallStr::from(name), mantissas)
        .into_decimal(38, 2)
        .map_err(|e| WorkerError::python("ComputeError", e.to_string()))?
        .into_series();
    Ok(series.into_column())
}

struct WindowRow {
    trade_date: NaiveDate,
    instrument: String,
    symbol: String,
    expiry: NaiveDate,
    strike: Decimal,
    option_type: String,
    prices: [Option<Decimal>; 6],
    ints: [Option<i64>; 3],
    turnover: Option<Decimal>,
    lot_size: Option<i32>,
    source_key: Option<String>,
}

fn window_row(r: &sqlx::postgres::PgRow) -> Result<WindowRow, sqlx::Error> {
    use sqlx::Row as _;
    Ok(WindowRow {
        trade_date: r.try_get(0)?,
        instrument: r.try_get(1)?,
        symbol: r.try_get(2)?,
        expiry: r.try_get(3)?,
        strike: r.try_get(4)?,
        option_type: r.try_get(5)?,
        prices: [
            r.try_get(6)?,
            r.try_get(7)?,
            r.try_get(8)?,
            r.try_get(9)?,
            r.try_get(10)?,
            r.try_get(11)?,
        ],
        ints: [r.try_get(12)?, r.try_get(13)?, r.try_get(14)?],
        turnover: r.try_get(15)?,
        lot_size: r.try_get(16)?,
        source_key: r.try_get(17)?,
    })
}

/// `load_window`: `fo_contract_daily` for `(trade_date - 120, trade_date]`, as a frame with the
/// table's column names.
pub(crate) async fn load_window(
    conn: &mut PgConnection,
    trade_date: NaiveDate,
) -> Result<DataFrame, WorkerError> {
    let start = minus(trade_date, LOOKBACK_DAYS);
    let raw: Vec<sqlx::postgres::PgRow> = sqlx::query(
        "SELECT trade_date, instrument, symbol, expiry, strike, option_type, open, high, low, \
         close, settle, underlying, open_interest, oi_change, volume, turnover, lot_size, \
         source_key FROM fo_contract_daily WHERE trade_date > $1 AND trade_date <= $2",
    )
    .bind(start)
    .bind(trade_date)
    .fetch_all(&mut *conn)
    .await?;
    let rows = raw.iter().map(window_row).collect::<Result<Vec<_>, _>>()?;
    let n = rows.len();
    let dates = |f: fn(&WindowRow) -> NaiveDate| -> Result<Column, WorkerError> {
        cols::date_column("", &rows.iter().map(|r| Some(f(r))).collect::<Vec<_>>()).map_err(fno_err)
    };
    let strs = |name: &str, f: fn(&WindowRow) -> Option<String>| -> Column {
        Series::new(name.into(), rows.iter().map(f).collect::<Vec<_>>()).into_column()
    };
    let decs = |name: &str, f: fn(&WindowRow) -> Option<Decimal>| {
        decimal_column(name, &rows.iter().map(f).collect::<Vec<_>>())
    };
    let ints = |name: &str, f: fn(&WindowRow) -> Option<i64>| -> Column {
        Series::new(name.into(), rows.iter().map(f).collect::<Vec<_>>()).into_column()
    };
    let columns = vec![
        dates(|r| r.trade_date)?.with_name("trade_date".into()),
        strs("instrument", |r| Some(r.instrument.clone())),
        strs("symbol", |r| Some(r.symbol.clone())),
        dates(|r| r.expiry)?.with_name("expiry".into()),
        decs("strike", |r| Some(r.strike))?,
        strs("option_type", |r| Some(r.option_type.clone())),
        decs("open", |r| r.prices[0])?,
        decs("high", |r| r.prices[1])?,
        decs("low", |r| r.prices[2])?,
        decs("close", |r| r.prices[3])?,
        decs("settle", |r| r.prices[4])?,
        decs("underlying", |r| r.prices[5])?,
        ints("open_interest", |r| r.ints[0]),
        ints("oi_change", |r| r.ints[1]),
        ints("volume", |r| r.ints[2]),
        decs("turnover", |r| r.turnover)?,
        ints("lot_size", |r| r.lot_size.map(i64::from)),
        strs("source_key", |r| r.source_key.clone()),
    ];
    DataFrame::new(n, columns).map_err(|e| WorkerError::python("ShapeError", e.to_string()))
}

fn float_text(f: Option<f64>) -> Option<String> {
    f.filter(|v| v.is_finite()).map(baskfy_core::py::float_repr)
}

/// `derive_underlying_daily` bound to `derive_for_night`'s calendar: derive and upsert
/// `fo_underlying_daily` for `trade_date`; returns rows written. Idempotent; `in_ban` is never
/// in the SET list.
pub(crate) async fn derive_for_night(
    conn: &mut PgConnection,
    trade_date: NaiveDate,
) -> Result<i64, WorkerError> {
    let sessions = trading_days(
        conn,
        minus(trade_date, LOOKBACK_DAYS),
        plus(trade_date, CALENDAR_AHEAD_DAYS),
    )
    .await?;
    let window = load_window(conn, trade_date).await?;
    if window.height() == 0 {
        return Ok(0);
    }
    let derived = derive_underlying(&window, trade_date, &sessions, &SeriesConfig::default())
        .map_err(fno_err)?;
    if derived.height() == 0 {
        return Ok(0);
    }
    let _ = DERIVED_COLUMNS;
    let symbol = cols::strings(&derived, "symbol").map_err(fno_err)?;
    let held = cols::dates(&derived, "held_expiry").map_err(fno_err)?;
    let floats = |name: &str| -> Result<Vec<Option<String>>, WorkerError> {
        Ok(cols::floats(&derived, name)
            .map_err(fno_err)?
            .into_iter()
            .map(float_text)
            .collect())
    };
    let oi = cols::ints(&derived, "oi_total").map_err(fno_err)?;
    let ca = cols::bools(&derived, "ca_flag").map_err(fno_err)?;
    sqlx::query(
        "INSERT INTO fo_underlying_daily (trade_date, symbol, held_expiry, level_o, level_h, \
         level_l, level_c, ret, atr14, oi_total, fut_turnover_20d, iv_atm, rv20, basis_ann, \
         ca_flag) \
         SELECT $1, t.* FROM UNNEST($2::varchar[], $3::date[], $4::numeric[], $5::numeric[], \
         $6::numeric[], $7::numeric[], $8::numeric[], $9::numeric[], $10::int8[], \
         $11::numeric[], $12::numeric[], $13::numeric[], $14::numeric[], $15::bool[]) AS t \
         ON CONFLICT (trade_date, symbol) DO UPDATE SET held_expiry = excluded.held_expiry, \
         level_o = excluded.level_o, level_h = excluded.level_h, level_l = excluded.level_l, \
         level_c = excluded.level_c, ret = excluded.ret, atr14 = excluded.atr14, \
         oi_total = excluded.oi_total, fut_turnover_20d = excluded.fut_turnover_20d, \
         iv_atm = excluded.iv_atm, rv20 = excluded.rv20, basis_ann = excluded.basis_ann, \
         ca_flag = excluded.ca_flag, updated_at = now()",
    )
    .bind(trade_date)
    .bind(&symbol)
    .bind(&held)
    .bind(floats("level_o")?)
    .bind(floats("level_h")?)
    .bind(floats("level_l")?)
    .bind(floats("level_c")?)
    .bind(floats("ret")?)
    .bind(floats("atr14")?)
    .bind(&oi)
    .bind(floats("fut_turnover_20d")?)
    .bind(floats("iv_atm")?)
    .bind(floats("rv20")?)
    .bind(floats("basis_ann")?)
    .bind(&ca)
    .execute(&mut *conn)
    .await?;
    Ok(i64::try_from(symbol.len()).unwrap_or(i64::MAX))
}

/// The column types of a frame this module builds (for tests).
#[cfg(test)]
pub(crate) fn dtypes(frame: &DataFrame) -> Vec<polars::prelude::DataType> {
    frame.dtypes()
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn decimal_columns_are_38_2() {
        let col = decimal_column("x", &[Some("1.5".parse().unwrap()), None]).unwrap();
        let df = DataFrame::new(2, vec![col]).unwrap();
        assert_eq!(format!("{:?}", dtypes(&df)), "[Decimal(38, 2)]");
        let as_float = cols::floats(&df, "x").unwrap();
        assert_eq!(as_float, vec![Some(1.5), None]);
    }
}
