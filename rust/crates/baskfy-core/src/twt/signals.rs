//! Port of `baskfy_core.twt.signals` — Chartink's five lines, read point-in-time, and the entry
//! event inside the state (`docs/twt/04` §3, `01` §2).
//!
//! Everything is the **point-in-time** reading (DECISIONS-TW TW0.1): `w0` is today's close, the
//! partial week. **A null on either side of a comparison is a fail, never a pass.**
//!
//! A `Decimal` threshold meets a `Float64` column as a float, as polars promotes it
//! (`float(str(d))`, checked against the live Python: identical for every literal tried).

use std::collections::HashMap;

use chrono::NaiveDate;
use polars::prelude::{BooleanChunked, DataFrame, IntoColumn, IntoSeries, NamedFrom, Series};
use rust_decimal::Decimal;

use crate::error::{CoreError, CoreResult};
use crate::twt::calendar::SessionCalendar;
use crate::twt::config::{SignalState, TwtConfig};
use crate::twt::indicators::frame::{
    F, bools, cmp_nulls_first, date_of, dates, floats, ints, put, strings, take_rows,
};
use crate::twt::indicators::with_twt_indicators;

/// Ports `WEEK_POSITION`: a session's ISO week among the week buckets the data holds.
pub const WEEK_POSITION: &str = "week_pos";

/// Ports `WEEKLY_CLOSE_PREFIX`.
pub const WEEKLY_CLOSE_PREFIX: &str = "weekly_close_";

/// Ports `SIGNAL_COLUMNS`: the columns [`with_twt_columns`] adds beyond the indicators.
pub const SIGNAL_COLUMNS: [&str; 9] = [
    WEEK_POSITION,
    "month_low_back",
    "tight_range_pct",
    "tight",
    "above_month_low",
    "tight_state",
    "sessions_out_before",
    "sessions_listed",
    "entry_event",
];

/// Ports `weekly_column`: `weekly_close_0` is today's close, `weekly_close_1` the previous
/// week's last close.
#[must_use]
pub fn weekly_column(back: i64) -> String {
    format!("{WEEKLY_CLOSE_PREFIX}{back}")
}

/// A `Decimal` literal as polars promotes it against a `Float64` column.
fn as_float(d: Decimal) -> CoreResult<f64> {
    d.to_string()
        .parse::<f64>()
        .map_err(|e| CoreError::InvalidInput(format!("{d} as a float: {e}")))
}

/// `instrument_id` and `date` of every row.
type Keys = (Vec<Option<i64>>, Vec<Option<i32>>);

/// The last printed close of an `(instrument, week_pos)` bucket: `(its date, the close)`.
type LastClose = HashMap<(Option<i64>, Option<i64>), (Option<i32>, f64)>;

/// `(instrument_id, date)` keys of every row.
fn keys(frame: &DataFrame) -> CoreResult<Keys> {
    Ok((ints(frame, "instrument_id")?, dates(frame, "date")?))
}

/// Ports `weekly_closes`: `W = (w0, w1, ..., w{n-1})` aligned to every session (`04` §3.2).
///
/// `week_pos` numbers the week buckets the frame holds (sorted, a null key first); `w0` is the
/// row's close; `w{k}` is the last non-null close (by date) of the instrument's week bucket `k`
/// positions earlier, null when it printed nothing there. Rows keep the input order (Python's
/// order here is a hash join's; the golden sorts it).
///
/// # Errors
/// A missing `instrument_id`, `date`, `week_key` or `close` column.
pub fn weekly_closes(indicated: &DataFrame, config: &TwtConfig) -> CoreResult<DataFrame> {
    let (ids, days) = keys(indicated)?;
    let week_key = ints(indicated, "week_key")?;
    let close = floats(indicated, "close")?;

    // weeks = unique keys, sorted (null first), numbered from 0. A null key takes a number but
    // never joins (polars does not match nulls).
    let mut unique: Vec<Option<i64>> = week_key.clone();
    unique.sort_by(cmp_nulls_first);
    unique.dedup();
    let position: HashMap<i64, i64> = unique
        .iter()
        .enumerate()
        .filter_map(|(i, k)| Some(((*k)?, i64::try_from(i).ok()?)))
        .collect();
    let week_pos: Vec<Option<i64>> = week_key
        .iter()
        .map(|k| k.and_then(|k| position.get(&k).copied()))
        .collect();

    // last close of each (instrument, week_pos) group, by date (stable: a tie keeps the later
    // row in frame order; a null date sorts first).
    let mut last: LastClose = HashMap::new();
    for i in 0..indicated.height() {
        let Some(c) = close[i] else { continue };
        let key = (ids[i], week_pos[i]);
        match last.get(&key) {
            Some((d, _)) if cmp_nulls_first(&days[i], d).is_lt() => {}
            _ => {
                last.insert(key, (days[i], c));
            }
        }
    }

    let mut frame = indicated.clone();
    put(
        &mut frame,
        Series::new(WEEK_POSITION.into(), week_pos.clone()).into_column(),
    )?;
    put(
        &mut frame,
        Series::new(weekly_column(0).as_str().into(), close.clone()).into_column(),
    )?;
    for back in 1..config.scan.tight_weeks {
        let values: Vec<F> = (0..indicated.height())
            .map(|i| {
                let (Some(id), Some(pos)) = (ids[i], week_pos[i]) else {
                    return None;
                };
                last.get(&(Some(id), Some(pos - back))).map(|(_, c)| *c)
            })
            .collect();
        put(
            &mut frame,
            Series::new(weekly_column(back).as_str().into(), values).into_column(),
        )?;
    }
    Ok(frame)
}

/// Ports `month_low_back`: the minimum adjusted low of the calendar month
/// `scan.month_low_months_back` months before the session's own month, for the same
/// instrument; null when that month has no bar of it. Rows keep the input order.
///
/// # Errors
/// A missing `instrument_id`, `month_key` or `low` column.
pub fn month_low_back(indicated: &DataFrame, config: &TwtConfig) -> CoreResult<DataFrame> {
    let ids = ints(indicated, "instrument_id")?;
    let month = ints(indicated, "month_key")?;
    let low = floats(indicated, "low")?;
    let mut lows: HashMap<(i64, i64), f64> = HashMap::new();
    for i in 0..indicated.height() {
        let (Some(id), Some(m), Some(l)) = (ids[i], month[i], low[i]) else {
            continue;
        };
        lows.entry((id, m))
            .and_modify(|v| {
                if l < *v {
                    *v = l;
                }
            })
            .or_insert(l);
    }
    let back = config.scan.month_low_months_back;
    let values: Vec<F> = (0..indicated.height())
        .map(|i| {
            let (Some(id), Some(m)) = (ids[i], month[i]) else {
                return None;
            };
            lows.get(&(id, m.checked_sub(back)?)).copied()
        })
        .collect();
    let mut frame = indicated.clone();
    put(
        &mut frame,
        Series::new("month_low_back".into(), values).into_column(),
    )?;
    Ok(frame)
}

/// Kleene `and` over nullable booleans.
fn and3(a: Option<bool>, b: Option<bool>) -> Option<bool> {
    match (a, b) {
        (Some(false), _) | (_, Some(false)) => Some(false),
        (Some(true), Some(true)) => Some(true),
        _ => None,
    }
}

/// `a >= b` / `a > b` / `a <= b` on nullable floats (null in, null out).
fn cmp_f(a: F, b: F, op: fn(f64, f64) -> bool) -> Option<bool> {
    Some(op(a?, b?))
}

/// Ports `tight_state`: Chartink's lines 1, 2, 3 and 5 and not an ETF (`04` §3.1). Adds
/// `tight_range_pct` (`(max(W) / min(W) - 1) x 100` when every `W` is present), `tight`,
/// `above_month_low` and `tight_state`, each null read as false. Row order is kept.
///
/// # Errors
/// A missing input column.
pub fn tight_state(framed: &DataFrame, config: &TwtConfig) -> CoreResult<DataFrame> {
    let scan = &config.scan;
    let height = framed.height();
    let weeks: Vec<Vec<F>> = (0..scan.tight_weeks)
        .map(|back| floats(framed, &weekly_column(back)))
        .collect::<CoreResult<_>>()?;
    let band = as_float(scan.tight_band_pct)?;
    let multiple = as_float(scan.month_low_multiple)?;
    let min_close_raw = as_float(scan.min_close_raw)?;
    let min_vol_sma = as_float(scan.min_vol_sma)?;

    let mut range_pct: Vec<F> = Vec::with_capacity(height);
    let mut tight: Vec<bool> = Vec::with_capacity(height);
    for i in 0..height {
        let row: Vec<F> = weeks.iter().map(|w| w[i]).collect();
        // `pl.all_horizontal` of zero columns is true; max/min of nothing is null.
        let have_all = row.iter().all(Option::is_some);
        let present: Vec<f64> = row.iter().filter_map(|v| *v).collect();
        let widest = present.iter().copied().reduce(f64::max);
        let narrowest = present.iter().copied().reduce(f64::min);
        let pct = if have_all {
            match (widest, narrowest) {
                (Some(hi), Some(lo)) => Some((hi / lo - 1.0) * 100.0),
                _ => None,
            }
        } else {
            None
        };
        range_pct.push(pct);
        tight.push(and3(Some(have_all), cmp_f(pct, Some(band), |a, b| a <= b)) == Some(true));
    }

    let close = floats(framed, "close")?;
    let month_low = floats(framed, "month_low_back")?;
    let above_month_low: Vec<bool> = (0..height)
        .map(|i| {
            let threshold = month_low[i].map(|m| m * multiple);
            cmp_f(close[i], threshold, |a, b| a >= b) == Some(true)
        })
        .collect();
    let close_raw = floats(framed, "close_raw")?;
    let vol_sma = floats(framed, "vol_sma")?;
    let is_etf = bools(framed, "is_etf")?;
    let state: Vec<bool> = (0..height)
        .map(|i| {
            let parts = [
                cmp_f(close_raw[i], Some(min_close_raw), |a, b| a > b),
                cmp_f(vol_sma[i], Some(min_vol_sma), |a, b| a >= b),
                Some(tight[i]),
                Some(above_month_low[i]),
                is_etf[i].map(|e| !e),
            ];
            parts.into_iter().fold(Some(true), and3) == Some(true)
        })
        .collect();

    let mut frame = framed.clone();
    put(
        &mut frame,
        Series::new("tight_range_pct".into(), range_pct).into_column(),
    )?;
    put(&mut frame, Series::new("tight".into(), tight).into_column())?;
    put(
        &mut frame,
        Series::new("above_month_low".into(), above_month_low).into_column(),
    )?;
    put(
        &mut frame,
        Series::new("tight_state".into(), state).into_column(),
    )?;
    Ok(frame)
}

/// Ports `entry_events`: the first session of a state (`04` §3.4). The frame is sorted by
/// `(instrument_id, date)` and gains `sessions_out_before` (`Int64`: sessions immediately
/// before this one the state was false, null when never in it), `sessions_listed` (`Int64`:
/// sessions since the first printed bar, inclusive) and `entry_event` (the state, a gap of at
/// least `entry_min_sessions_out`, and that much existing history).
///
/// # Errors
/// A missing input column.
pub fn entry_events(stated: &DataFrame, config: &TwtConfig) -> CoreResult<DataFrame> {
    let gap = config.entry.entry_min_sessions_out;
    let (ids, days) = keys(stated)?;
    let mut order: Vec<usize> = (0..stated.height()).collect();
    order.sort_by(|&a, &b| {
        cmp_nulls_first(&ids[a], &ids[b]).then_with(|| cmp_nulls_first(&days[a], &days[b]))
    });
    let mut frame = take_rows(stated, &order)?;
    let ids = ints(&frame, "instrument_id")?;
    let state = bools(&frame, "tight_state")?;
    let close = floats(&frame, "close")?;
    let height = frame.height();

    let mut out_before: Vec<Option<i64>> = vec![None; height];
    let mut listed: Vec<i64> = vec![0; height];
    let mut start = 0;
    while start < height {
        let mut end = start + 1;
        while end < height && ids[end] == ids[start] {
            end += 1;
        }
        // position within the group; last_in_state = forward-filled position of a true state,
        // shifted by one.
        let mut last_true: Option<i64> = None;
        let mut printed: i64 = 0;
        let mut since_first: i64 = 0;
        for (pos, row) in (start..end).enumerate() {
            let pos = i64::try_from(pos).unwrap_or(i64::MAX);
            out_before[row] = last_true.map(|t| pos - t - 1);
            if state[row] == Some(true) {
                last_true = Some(pos);
            }
            if close[row].is_some() {
                printed += 1;
            }
            if printed > 0 {
                since_first += 1;
            }
            listed[row] = since_first;
        }
        start = end;
    }
    let event: Vec<bool> = (0..height)
        .map(|i| {
            let long_enough_out = out_before[i].is_none_or(|n| n >= gap);
            and3(
                and3(state[i], Some(long_enough_out)),
                // Python's `sessions_listed - 1 >= gap`, spelled as clippy prefers.
                Some(listed[i] > gap),
            ) == Some(true)
        })
        .collect();
    put(
        &mut frame,
        Series::new("sessions_out_before".into(), out_before).into_column(),
    )?;
    put(
        &mut frame,
        Series::new("sessions_listed".into(), listed).into_column(),
    )?;
    put(
        &mut frame,
        Series::new("entry_event".into(), event).into_column(),
    )?;
    Ok(frame)
}

/// Ports `with_twt_columns`: indicators, the weekly closes, the month-3 low, the state and the
/// entry event — the one entry point the nightly job and the backtest share.
///
/// # Errors
/// As [`with_twt_indicators`] and the steps after it.
pub fn with_twt_columns(
    bars: &DataFrame,
    calendar: &SessionCalendar,
    config: &TwtConfig,
) -> CoreResult<DataFrame> {
    let indicated = with_twt_indicators(bars, calendar, config)?;
    let framed = month_low_back(&weekly_closes(&indicated, config)?, config)?;
    entry_events(&tight_state(&framed, config)?, config)
}

/// The floor `liquidity_floor` reads: `floor_inr`, else `entry.min_turnover_inr`.
fn floor_of(config: &TwtConfig, floor_inr: Option<Decimal>) -> CoreResult<f64> {
    as_float(floor_inr.unwrap_or(config.entry.min_turnover_inr))
}

/// Ports `liquidity_floor`: `turnover_avg_20 >= the floor`, null read as false (`04` §3.5).
/// Python returns the expression; here it is evaluated over `frame`, as a `Boolean` series
/// named `turnover_avg_20`.
///
/// # Errors
/// A missing `turnover_avg_20` column.
pub fn liquidity_floor(
    frame: &DataFrame,
    config: &TwtConfig,
    floor_inr: Option<Decimal>,
) -> CoreResult<Series> {
    let floor = floor_of(config, floor_inr)?;
    let passes: Vec<bool> = floats(frame, "turnover_avg_20")?
        .into_iter()
        .map(|t| t.is_some_and(|t| t >= floor))
        .collect();
    Ok(BooleanChunked::new("turnover_avg_20".into(), passes).into_series())
}

/// Ports `signal_mask`: a boolean series named `signal` over `detected` — `entry_event` and
/// the liquidity floor, null read as false.
///
/// # Errors
/// A missing `entry_event` or `turnover_avg_20` column.
pub fn signal_mask(
    detected: &DataFrame,
    config: &TwtConfig,
    floor_inr: Option<Decimal>,
) -> CoreResult<Series> {
    let floor = liquidity_floor(detected, config, floor_inr)?;
    let passes: Vec<bool> = floor.bool()?.iter().map(|v| v == Some(true)).collect();
    let event = bools(detected, "entry_event")?;
    let mask: Vec<bool> = event
        .into_iter()
        .zip(passes)
        .map(|(e, p)| and3(e, Some(p)) == Some(true))
        .collect();
    Ok(BooleanChunked::new("signal".into(), mask).into_series())
}

/// Ports `detect_signals`: every entry event (of `as_of` only, when given) with `rank_key`
/// (the session's own turnover) and `signal_state` (`SIGNAL` at or above the floor, else
/// `SCAN_ONLY`), sorted by date ascending, `rank_key` descending, `symbol` ascending, nulls
/// last.
///
/// # Errors
/// A missing input column.
pub fn detect_signals(
    detected: &DataFrame,
    as_of: Option<NaiveDate>,
    config: &TwtConfig,
    floor_inr: Option<Decimal>,
) -> CoreResult<DataFrame> {
    let days = dates(detected, "date")?;
    let event = bools(detected, "entry_event")?;
    let mut rows: Vec<usize> = Vec::new();
    for i in 0..detected.height() {
        let on_day = match as_of {
            None => true,
            Some(d) => days[i].map(date_of).transpose()? == Some(d),
        };
        if on_day && event[i] == Some(true) {
            rows.push(i);
        }
    }
    let mut frame = take_rows(detected, &rows)?;
    let turnover = floats(&frame, "turnover_inr")?;
    let rank = frame
        .column("turnover_inr")?
        .clone()
        .with_name("rank_key".into());
    let floor = liquidity_floor(&frame, config, floor_inr)?;
    let state: Vec<&str> = floor
        .bool()?
        .iter()
        .map(|p| {
            if p == Some(true) {
                SignalState::Signal.value()
            } else {
                SignalState::ScanOnly.value()
            }
        })
        .collect();
    put(&mut frame, rank)?;
    put(
        &mut frame,
        Series::new("signal_state".into(), state).into_column(),
    )?;

    let days = dates(&frame, "date")?;
    let symbol = strings(&frame, "symbol")?;
    let mut order: Vec<usize> = (0..frame.height()).collect();
    order.sort_by(|&a, &b| {
        nulls_last(&days[a], &days[b], false)
            .then_with(|| nulls_last(&turnover[a], &turnover[b], true))
            .then_with(|| nulls_last(&symbol[a], &symbol[b], false))
    });
    take_rows(&frame, &order)
}

/// A sort key with nulls last; `descending` flips only the non-null order.
fn nulls_last<T: PartialOrd>(a: &Option<T>, b: &Option<T>, descending: bool) -> std::cmp::Ordering {
    use std::cmp::Ordering;
    match (a, b) {
        (None, None) => Ordering::Equal,
        (None, Some(_)) => Ordering::Greater,
        (Some(_), None) => Ordering::Less,
        (Some(x), Some(y)) => {
            let o = x.partial_cmp(y).unwrap_or(Ordering::Equal);
            if descending { o.reverse() } else { o }
        }
    }
}
