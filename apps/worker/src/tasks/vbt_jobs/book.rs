//! The evening's book work (VB6/VB7): the run's own calendar, the expiry sweep, the held names'
//! bars and 21-day EMA, `manage` over the book with Qullamaggie's rule (LV9), the trail chosen
//! once, the exit queue, and the two session counts the rules read. Ports those parts of
//! `baskfy_worker.tasks.vbt_evening` (`session_calendar`, `to_working`, `sweep_expired_orders`,
//! `bars_for_book`, `managed_actions`, `_qulla_action`, `naked_positions`,
//! `blank_session_counts`, `queue_exits`, `persist_trails`, `bars_since_entry_by_position`) and
//! the note-carrying half of `baskfy_core.exits.qulla.decide`.
//!
//! **Nothing here cancels or sends anything.** A `SENT` order past its window becomes a
//! `CANCEL_LIMIT` *line* for a person to confirm; only an order no broker ever saw (`PROPOSED`,
//! `CONFIRMED`) is marked `EXPIRED` here. `sessions_worked` is **derived** from the calendar, so a
//! re-run cannot inflate it (house rule 7).

use std::collections::BTreeMap;

use baskfy_core::decimal::WideDecimal;
use baskfy_core::swing::stops::{
    ActionKind, ActionReason, DailyBar, OpenPosition as QullaOpen, TrailMa, manage as qulla_manage,
};
use baskfy_core::vbt::calendar::{SessionCalendar, drop_thin_sessions};
use baskfy_core::vbt::config::VbtConfig;
use baskfy_core::vbt::exits::{Action, Bar, ExitReason, ManageAction, OpenPosition, manage};
use baskfy_core::vbt::indicators::with_vbt_indicators;
use baskfy_core::vbt::orders::{
    CancelReason, OrderState, WorkingOrder, expire_orders, expires_after, sessions_since,
};
use baskfy_core::vbt::plan::ManagedRow;
use chrono::NaiveDate;
use polars::prelude::DataFrame;
use rust_decimal::Decimal;
use sqlx::PgConnection;

use super::sleeve::{OrderRow, PositionRow};
use crate::error::WorkerError;
use crate::steps::sleeves::for_vbt_jobs::{
    LOOKBACK_SESSIONS, MA_TRAIL, NSE_EXCHANGE_ID, OhlcBar, PARTIAL, core_err, dates, floats, ints,
    load_raw_bars, lookback_start, mean, pl_err, trail_for, trail_value, value_err,
};

/// `CALENDAR_SESSIONS`: how far back the calendar is read from the sleeve's breadth history.
const CALENDAR_SESSIONS: i64 = 60;
/// `exits.qulla.MA_FAST` / `MA_SLOW`.
const MA_FAST: usize = 10;
const MA_SLOW: usize = 20;

fn day_from_number(n: i32) -> Option<NaiveDate> {
    NaiveDate::from_num_days_from_ce_opt(n + 719_163)
}

/// `str(Decimal)`.
pub(crate) fn py_str(d: Decimal) -> String {
    WideDecimal::from(d).to_sci_string()
}

/// `Decimal(str(float))`.
fn dec_from_float(x: f64) -> Result<Decimal, WorkerError> {
    baskfy_core::py::decimal_from_float_str(x).map_err(|e| value_err(e.to_string()))
}

/// `Decimal(str(float))`, wide (Python's 28-digit context for the division that follows).
fn wide_from_float(x: f64) -> Result<WideDecimal, WorkerError> {
    baskfy_core::py::wide_from_float_str(x).map_err(|e| value_err(e.to_string()))
}

/// `session_calendar(session, user_id, as_of)`: the run's **own** calendar, read back from what
/// the detector recorded (the last 60 breadth rows); `as_of` itself is a session unless the
/// detector called it thin.
pub(crate) async fn session_calendar(
    conn: &mut PgConnection,
    user_id: i64,
    as_of: NaiveDate,
) -> Result<SessionCalendar, WorkerError> {
    let rows: Vec<(NaiveDate, bool)> = sqlx::query_as(
        "SELECT date, thin_session FROM vb_breadth_daily WHERE user_id = $1 AND date <= $2 \
         ORDER BY date DESC LIMIT $3",
    )
    .bind(user_id)
    .bind(as_of)
    .bind(CALENDAR_SESSIONS)
    .fetch_all(&mut *conn)
    .await?;
    let mut kept: Vec<NaiveDate> = rows.iter().filter(|r| !r.1).map(|r| r.0).collect();
    kept.sort_unstable();
    let mut dropped: Vec<NaiveDate> = rows.iter().filter(|r| r.1).map(|r| r.0).collect();
    dropped.sort_unstable();
    if !kept.contains(&as_of) && !dropped.contains(&as_of) {
        kept.push(as_of);
        kept.sort_unstable();
        kept.dedup();
    }
    Ok(SessionCalendar {
        sessions: kept,
        dropped,
        counts: BTreeMap::new(),
    })
}

/// `to_working`: a row as the pure engine sees it, its session count derived.
fn to_working(
    order: &OrderRow,
    calendar: &SessionCalendar,
    as_of: NaiveDate,
) -> Result<WorkingOrder, WorkerError> {
    Ok(WorkingOrder {
        instrument_id: order.instrument_id,
        signal_date: order.signal_date,
        limit_price: order.limit_price,
        stop_price: order.stop_price,
        quantity: order.quantity,
        state: order.order_state()?,
        sessions_worked: sessions_since(order.signal_date, as_of, calendar),
        filled_quantity: order.filled_quantity,
        cancel_reason: None,
    })
}

/// `sweep_expired_orders`: the `CANCEL_LIMIT` candidates (`CANCELLED` by the engine — a broker
/// holds them) with their symbols, and how many rows had their counts refreshed. `rows` is the
/// evening's own list, mutated as the ORM's identity map is (an order marked `EXPIRED` here no
/// longer holds a slot in the book built after).
pub(crate) async fn sweep_expired_orders(
    conn: &mut PgConnection,
    rows: &mut [OrderRow],
    as_of: NaiveDate,
    calendar: &SessionCalendar,
    config: &VbtConfig,
    symbols: &BTreeMap<i64, String>,
) -> Result<(Vec<(WorkingOrder, String)>, i64), WorkerError> {
    let working = rows
        .iter()
        .map(|row| to_working(row, calendar, as_of))
        .collect::<Result<Vec<_>, _>>()?;
    let (_, done) = expire_orders(
        &working,
        as_of,
        calendar,
        &config.entry,
        CancelReason::ExpirySweep,
    );
    let expired_ids: std::collections::BTreeSet<i64> =
        done.iter().map(|o| o.instrument_id).collect();
    let mut refreshed = 0_i64;
    for row in rows.iter_mut() {
        let worked = sessions_since(row.signal_date, as_of, calendar);
        let expires = expires_after(row.signal_date, calendar, &config.entry).map_err(core_err)?;
        let mut new_worked = row.sessions_worked;
        let mut new_expires = row.expires_after_session;
        if row.sessions_worked != worked || row.expires_after_session != expires {
            new_worked = worked;
            if expires.is_some() {
                new_expires = expires;
            }
            refreshed += 1;
        }
        let mut new_state = row.state.clone();
        let mut expired_now = false;
        if expired_ids.contains(&row.instrument_id)
            && (row.state == "PROPOSED" || row.state == "CONFIRMED")
        {
            new_state = "EXPIRED".to_owned();
            expired_now = true;
        }
        // The ORM writes only the attributes whose value changed, and the row's `onupdate`
        // stamp with them; an unchanged row is not written at all.
        let changed = new_worked != row.sessions_worked
            || new_expires != row.expires_after_session
            || expired_now;
        if changed {
            let worked16 = i16::try_from(new_worked)
                .map_err(|_| WorkerError::python("DataError", "smallint out of range"))?;
            if expired_now {
                sqlx::query(
                    "UPDATE vb_order SET sessions_worked = $2, expires_after_session = $3, \
                     state = 'EXPIRED', cancel_reason = 'EXPIRY_SWEEP', cancelled_on = $4, \
                     updated_at = now() WHERE id = $1",
                )
                .bind(row.id)
                .bind(worked16)
                .bind(new_expires)
                .bind(as_of)
                .execute(&mut *conn)
                .await?;
            } else {
                sqlx::query(
                    "UPDATE vb_order SET sessions_worked = $2, expires_after_session = $3, \
                     updated_at = now() WHERE id = $1",
                )
                .bind(row.id)
                .bind(worked16)
                .bind(new_expires)
                .execute(&mut *conn)
                .await?;
            }
        }
        row.sessions_worked = new_worked;
        row.expires_after_session = new_expires;
        row.state = new_state;
    }
    let cancels = done
        .into_iter()
        .filter(|o| o.state == OrderState::Cancelled)
        .map(|o| {
            let symbol = symbols
                .get(&o.instrument_id)
                .cloned()
                .unwrap_or_else(|| o.instrument_id.to_string());
            (o, symbol)
        })
        .collect();
    Ok((cancels, refreshed))
}

/// The session's bar for a held name, and (LV9) its history in exchange prices.
#[derive(Debug, Clone)]
pub(crate) struct BookBar {
    pub(crate) bar: Bar,
    pub(crate) history: Vec<OhlcBar>,
}

/// `bars_for_book`: the session's bar and 21-day EMA for each held name, its calendar built from
/// the bars (not the young sleeve's history, which would leave the EMA null), and Qullamaggie's
/// history (`Decimal(str(v)) / factor`, the raw close as the close) when that rule is on.
pub(crate) async fn bars_for_book(
    conn: &mut PgConnection,
    instrument_ids: &[i64],
    as_of: NaiveDate,
    config: &VbtConfig,
) -> Result<BTreeMap<i64, BookBar>, WorkerError> {
    if instrument_ids.is_empty() {
        return Ok(BTreeMap::new());
    }
    let start = lookback_start(conn, as_of, LOOKBACK_SESSIONS).await?;
    let universe: std::collections::BTreeSet<i64> = instrument_ids.iter().copied().collect();
    let frame = load_raw_bars(conn, start, as_of, &universe, false).await?;
    if frame.height() == 0 {
        return Ok(BTreeMap::new());
    }
    let (kept, bar_calendar) = drop_thin_sessions(&frame, config).map_err(core_err)?;
    let indicated = with_vbt_indicators(&kept, &bar_calendar, config).map_err(core_err)?;
    let histories = if config.exits.qulla_exits {
        histories(&kept)?
    } else {
        BTreeMap::new()
    };
    let ids = ints(&indicated, "instrument_id")?;
    let days = dates(&indicated, "date")?;
    let open = floats(&indicated, "open")?;
    let high = floats(&indicated, "high")?;
    let low = floats(&indicated, "low")?;
    let close = floats(&indicated, "close")?;
    let ema = floats(&indicated, "ema_exit")?;
    let price = |v: Option<f64>| v.map(dec_from_float).transpose();
    let mut out = BTreeMap::new();
    for i in 0..indicated.height() {
        if days[i].and_then(day_from_number) != Some(as_of) {
            continue;
        }
        let Some(id) = ids[i] else { continue };
        out.insert(
            id,
            BookBar {
                bar: Bar {
                    session: as_of,
                    open: price(open[i])?,
                    high: price(high[i])?,
                    low: price(low[i])?,
                    close: price(close[i])?,
                    ema_exit: price(ema[i])?,
                    history: Vec::new(),
                },
                history: histories.get(&id).cloned().unwrap_or_default(),
            },
        );
    }
    Ok(out)
}

/// The LV9 histories: per name, oldest first, every bar of `kept` with a full OHLC and a
/// positive factor.
fn histories(kept: &DataFrame) -> Result<BTreeMap<i64, Vec<OhlcBar>>, WorkerError> {
    let sorted = kept
        .sort(["instrument_id", "date"], Default::default())
        .map_err(|e| pl_err(&e))?;
    let ids = ints(&sorted, "instrument_id")?;
    let days = dates(&sorted, "date")?;
    let open = floats(&sorted, "open")?;
    let high = floats(&sorted, "high")?;
    let low = floats(&sorted, "low")?;
    let close = floats(&sorted, "close")?;
    let close_raw = floats(&sorted, "close_raw")?;
    let factor = floats(&sorted, "adj_factor")?;
    let mut out: BTreeMap<i64, Vec<OhlcBar>> = BTreeMap::new();
    for i in 0..sorted.height() {
        // `Decimal(str(row["adj_factor"] or 1))`.
        let f = match factor[i] {
            Some(v) if v != 0.0 => wide_from_float(v)?,
            _ => WideDecimal::from(1_i64),
        };
        let (Some(o), Some(h), Some(l), Some(c)) = (open[i], high[i], low[i], close[i]) else {
            continue;
        };
        if f <= WideDecimal::zero() {
            continue;
        }
        let div = |v: f64| -> Result<WideDecimal, WorkerError> {
            wide_from_float(v)?
                .div(&f)
                .map_err(|e| value_err(e.to_string()))
        };
        let (Some(id), Some(day)) = (ids[i], days[i].and_then(day_from_number)) else {
            continue;
        };
        let close_value = match close_raw[i] {
            Some(raw) => wide_from_float(raw)?,
            None => div(c)?,
        };
        out.entry(id).or_default().push(OhlcBar {
            date: day,
            open: div(o)?,
            high: div(h)?,
            low: div(l)?,
            close: close_value,
        });
    }
    Ok(out)
}

/// One `managed_actions` row: `(instrument_id, symbol, quantity_open, action, position id)`.
pub(crate) type Managed = ManagedRow;

/// Python's `f"{x:g}"` for a float.
pub(crate) fn py_g(x: f64) -> String {
    if x.is_nan() {
        return "nan".to_owned();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf" } else { "-inf" }.to_owned();
    }
    if x == 0.0 {
        return if x.is_sign_negative() { "-0" } else { "0" }.to_owned();
    }
    let exp = format!("{x:.5e}");
    let (mantissa, e) = exp.split_once('e').unwrap_or((exp.as_str(), "0"));
    let e: i32 = e.parse().unwrap_or(0);
    if !(-4..6).contains(&e) {
        let m = mantissa.trim_end_matches('0').trim_end_matches('.');
        let sign = if e < 0 { '-' } else { '+' };
        return format!("{m}e{sign}{:02}", e.abs());
    }
    let decimals = usize::try_from(5 - e).unwrap_or(0);
    let fixed = format!("{x:.decimals$}");
    if fixed.contains('.') {
        fixed.trim_end_matches('0').trim_end_matches('.').to_owned()
    } else {
        fixed
    }
}

fn wide_str(w: &WideDecimal) -> String {
    w.to_sci_string()
}

/// `baskfy_core.exits.qulla.Decision`, the fields the VBT evening reads (the note included: the
/// plan line persists it).
struct QullaDecision {
    sell_quantity: i64,
    sell_reason: &'static str,
    raise_to: Option<Decimal>,
    note: String,
}

/// `baskfy_core.exits.qulla.decide`: `None` when the name did not print on `on`.
fn qulla_decide(
    position: &PositionRow,
    symbol: &str,
    resting: Decimal,
    series: &[OhlcBar],
    on: NaiveDate,
    bars_since_entry: i64,
    config: &VbtConfig,
) -> Result<Option<QullaDecision>, WorkerError> {
    let stops = &config.exits.qulla;
    let Some(today) = series.last() else {
        return Ok(None);
    };
    if today.date != on {
        return Ok(None);
    }
    let trail = match position.trail.as_deref() {
        Some("MA10") => TrailMa::Ma10,
        Some("MA20") => TrailMa::Ma20,
        Some(other) => {
            return Err(WorkerError::python(
                "ValueError",
                format!("'{other}' is not a valid TrailMa"),
            ));
        }
        None => trail_for(series, stops)?,
    };
    let closes: Vec<WideDecimal> = series.iter().map(|b| b.close.clone()).collect();
    let ma10 = mean(&closes, MA_FAST)?;
    let ma20 = mean(&closes, MA_SLOW)?;
    let narrow = |w: &WideDecimal| w.try_to_decimal().map_err(|e| value_err(e.to_string()));
    let bar = DailyBar {
        date: on,
        open: narrow(&today.open)?,
        high: narrow(&today.high)?,
        low: narrow(&today.low)?,
        close: narrow(&today.close)?,
        ma10: ma10.as_ref().map(narrow).transpose()?,
        ma20: ma20.as_ref().map(narrow).transpose()?,
        bars_since_entry,
    };
    let actions = qulla_manage(
        &QullaOpen {
            symbol: symbol.to_owned(),
            entry_date: position.entry_date,
            entry: position.entry_avg,
            initial_stop: position.initial_stop,
            stop: resting,
            quantity: position.quantity_open,
            partial_done: position.partial_done,
            trail,
            is_ep_gap_day: false,
        },
        &bar,
        stops,
    )
    .map_err(core_err)?;
    let mut sell_quantity = 0_i64;
    let mut sell_reason = "";
    let mut raise_to: Option<Decimal> = None;
    let mut notes: Vec<String> = Vec::new();
    for action in actions {
        match action.kind {
            ActionKind::StoppedOut => {
                notes.push(format!(
                    "the bar traded through the stop {}",
                    py_str(resting)
                ));
            }
            ActionKind::SellAll => {
                sell_quantity = position.quantity_open;
                sell_reason = MA_TRAIL;
                let level = if trail == TrailMa::Ma10 { &ma10 } else { &ma20 };
                notes.push(format!(
                    "close {} below the {} {}",
                    wide_str(&today.close),
                    trail_value(trail),
                    level.as_ref().map_or_else(|| "None".to_owned(), wide_str)
                ));
            }
            ActionKind::SellPartial => {
                sell_quantity = action.quantity;
                sell_reason = PARTIAL;
                notes.push(format!(
                    "bar {bars_since_entry} after entry, green: sell {} into strength",
                    action.quantity
                ));
            }
            ActionKind::RaiseStop => {
                if let Some(level) = action.new_stop {
                    raise_to = Some(level);
                    let why = if action.reason == ActionReason::BreakevenAfterPartial {
                        "after the partial".to_owned()
                    } else {
                        format!("+{}R showing", py_g(stops.breakeven_after_r))
                    };
                    notes.push(format!("stop to breakeven {} ({why})", py_str(level)));
                }
            }
            ActionKind::Hold => {}
        }
    }
    Ok(Some(QullaDecision {
        sell_quantity,
        sell_reason,
        raise_to,
        note: notes.join("; "),
    }))
}

/// `_qulla_action`: Qullamaggie's rule for one position, as a VBT `ManageAction`.
fn qulla_action(
    row: &PositionRow,
    symbol: &str,
    bar: Option<&BookBar>,
    config: &VbtConfig,
    bars_since_entry: i64,
) -> Result<ManageAction, WorkerError> {
    let resting = match row.gtt_trigger {
        None => row.stop_price,
        // `max(stop, trigger)`: the first on a tie.
        Some(trigger) if trigger > row.stop_price => trigger,
        Some(_) => row.stop_price,
    };
    let hold = |note: String| ManageAction {
        action: Action::Hold,
        reason: None,
        price: None,
        note,
        quantity: None,
        new_stop: None,
    };
    let (series, on): (&[OhlcBar], NaiveDate) = match bar {
        Some(b) => (&b.history, b.bar.session),
        None => (&[], row.entry_date),
    };
    let Some(QullaDecision {
        sell_quantity,
        sell_reason,
        raise_to: raise,
        note,
    }) = qulla_decide(row, symbol, resting, series, on, bars_since_entry, config)?
    else {
        return Ok(hold("no bar today".to_owned()));
    };
    let raise_to = raise.filter(|r| !r.is_zero() && *r > resting);
    if sell_reason == MA_TRAIL && sell_quantity > 0 {
        return Ok(ManageAction {
            action: Action::QueueSellAtOpen,
            reason: Some(ExitReason::MaTrail),
            price: None,
            note,
            quantity: None,
            new_stop: None,
        });
    }
    if sell_reason == PARTIAL && sell_quantity > 0 {
        return Ok(ManageAction {
            action: Action::QueuePartialAtOpen,
            reason: Some(ExitReason::Partial),
            price: None,
            note,
            quantity: Some(sell_quantity),
            new_stop: raise_to,
        });
    }
    if raise_to.is_some() {
        return Ok(ManageAction {
            action: Action::RaiseStop,
            reason: None,
            price: None,
            note,
            quantity: None,
            new_stop: raise_to,
        });
    }
    Ok(hold(if note.is_empty() {
        "holding under the rule".to_owned()
    } else {
        note
    }))
}

/// `managed_actions`: `manage` over the book, one position at a time; with `qulla_exits` the
/// stop and the write-off stay `manage`'s and the working exit is Qullamaggie's.
pub(crate) fn managed_actions(
    positions: &[PositionRow],
    bars: &BTreeMap<i64, BookBar>,
    symbols: &BTreeMap<i64, String>,
    config: &VbtConfig,
    blanks: &BTreeMap<i64, i64>,
    bars_since_entry: &BTreeMap<i64, i64>,
) -> Result<Vec<Managed>, WorkerError> {
    let mut out = Vec::with_capacity(positions.len());
    for row in positions {
        let book_bar = bars.get(&row.instrument_id);
        let blank_bar = Bar {
            session: row.entry_date,
            open: None,
            high: None,
            low: None,
            close: None,
            ema_exit: None,
            history: Vec::new(),
        };
        let bar = book_bar.map_or(&blank_bar, |b| &b.bar);
        let symbol = symbols
            .get(&row.instrument_id)
            .cloned()
            .unwrap_or_else(|| row.instrument_id.to_string());
        let mut action = manage(
            &OpenPosition {
                instrument_id: row.instrument_id,
                entry_date: row.entry_date,
                entry_price: row.entry_avg,
                quantity: row.quantity_open,
                stop_price: row.stop_price,
                initial_stop: row.initial_stop,
                exit_queued_for: row.exit_queued_for,
            },
            bar,
            blanks.get(&row.instrument_id).copied().unwrap_or(0),
            &config.exits,
        );
        if config.exits.qulla_exits
            && matches!(action.action, Action::Hold | Action::QueueSellAtOpen)
        {
            action = qulla_action(
                row,
                &symbol,
                book_bar,
                config,
                bars_since_entry.get(&row.id).copied().unwrap_or(0),
            )?;
        }
        out.push(ManagedRow {
            instrument_id: row.instrument_id,
            symbol,
            quantity_open: row.quantity_open,
            action,
            position_id: Some(row.id),
        });
    }
    Ok(out)
}

/// `naked_positions`: filled quantity with no resting GTT — `(id, symbol, quantity, stop)`.
pub(crate) fn naked_positions(
    positions: &[PositionRow],
    symbols: &BTreeMap<i64, String>,
) -> Vec<(i64, String, i64, Decimal)> {
    positions
        .iter()
        .filter(|row| row.gtt_id.is_none() && row.quantity_open > 0)
        .map(|row| {
            (
                row.instrument_id,
                symbols
                    .get(&row.instrument_id)
                    .cloned()
                    .unwrap_or_else(|| row.instrument_id.to_string()),
                row.quantity_open,
                row.stop_price,
            )
        })
        .collect()
}

/// `blank_session_counts`: sessions each held name has gone without printing, on the calendar
/// (a name with no bar at all counts from its entry).
pub(crate) async fn blank_session_counts(
    conn: &mut PgConnection,
    positions: &[PositionRow],
    as_of: NaiveDate,
    calendar: &SessionCalendar,
) -> Result<BTreeMap<i64, i64>, WorkerError> {
    if positions.is_empty() {
        return Ok(BTreeMap::new());
    }
    let ids: Vec<i64> = positions.iter().map(|p| p.instrument_id).collect();
    let rows: Vec<(i64, Option<NaiveDate>)> = sqlx::query_as(
        "SELECT instrument_id, max(date) FROM ohlcv_daily \
         WHERE instrument_id = ANY($1) AND date <= $2 GROUP BY instrument_id",
    )
    .bind(&ids)
    .bind(as_of)
    .fetch_all(&mut *conn)
    .await?;
    let last: BTreeMap<i64, NaiveDate> = rows
        .into_iter()
        .filter_map(|(id, d)| d.map(|d| (id, d)))
        .collect();
    let mut out = BTreeMap::new();
    for row in positions {
        let from = last
            .get(&row.instrument_id)
            .copied()
            .unwrap_or(row.entry_date);
        out.insert(row.instrument_id, sessions_since(from, as_of, calendar));
    }
    Ok(out)
}

/// `bars_since_entry_by_position`: trading days strictly after each entry, up to `as_of`.
pub(crate) async fn bars_since_entry_by_position(
    conn: &mut PgConnection,
    positions: &[PositionRow],
    as_of: NaiveDate,
) -> Result<BTreeMap<i64, i64>, WorkerError> {
    let mut out = BTreeMap::new();
    for row in positions {
        let (n,): (i64,) = sqlx::query_as(
            "SELECT count(*) FROM trading_day WHERE exchange_id = $1 \
             AND is_trading_day IS true AND date > $2 AND date <= $3",
        )
        .bind(NSE_EXCHANGE_ID)
        .bind(row.entry_date)
        .bind(as_of)
        .fetch_one(&mut *conn)
        .await?;
        out.insert(row.id, n);
    }
    Ok(out)
}

/// `persist_trails`: LV9's trail, chosen once from the ADR the first evening a position is
/// managed, written so every later evening reads the same average.
pub(crate) async fn persist_trails(
    conn: &mut PgConnection,
    positions: &mut [PositionRow],
    bars: &BTreeMap<i64, BookBar>,
    config: &VbtConfig,
) -> Result<i64, WorkerError> {
    if !config.exits.qulla_exits {
        return Ok(0);
    }
    let mut written = 0_i64;
    for row in positions.iter_mut() {
        if row.trail.is_some() {
            continue;
        }
        let Some(bar) = bars.get(&row.instrument_id) else {
            continue;
        };
        if bar.history.is_empty() {
            continue;
        }
        let trail = trail_value(trail_for(&bar.history, &config.exits.qulla)?);
        sqlx::query("UPDATE vb_position SET trail = $2, updated_at = now() WHERE id = $1")
            .bind(row.id)
            .bind(trail)
            .execute(&mut *conn)
            .await?;
        row.trail = Some(trail.to_owned());
        written += 1;
    }
    Ok(written)
}

/// `queue_exits`: `exit_queued_for` on the positions `manage` decided to sell; a position
/// already queued keeps its date.
pub(crate) async fn queue_exits(
    conn: &mut PgConnection,
    positions: &mut [PositionRow],
    actions: &[Managed],
    next_session: Option<NaiveDate>,
) -> Result<i64, WorkerError> {
    let mut queued = 0_i64;
    for managed in actions {
        let by_position = managed
            .position_id
            .and_then(|id| positions.iter().position(|p| p.id == id));
        let index = match by_position {
            Some(i) => Some(i),
            // `by_instrument`: the dict keeps the **last** position in a name.
            None => positions
                .iter()
                .rposition(|p| p.instrument_id == managed.instrument_id),
        };
        let Some(row) = index.and_then(|i| positions.get_mut(i)) else {
            continue;
        };
        if managed.action.action == Action::QueueSellAtOpen && row.exit_queued_for.is_none() {
            let reason = managed.action.reason.unwrap_or(ExitReason::EmaExit).value();
            let changed =
                next_session.is_some() || row.exit_reason_queued.as_deref() != Some(reason);
            if changed {
                sqlx::query(
                    "UPDATE vb_position SET exit_queued_for = $2, exit_reason_queued = $3, \
                     updated_at = now() WHERE id = $1",
                )
                .bind(row.id)
                .bind(next_session)
                .bind(reason)
                .execute(&mut *conn)
                .await?;
            }
            row.exit_queued_for = next_session;
            row.exit_reason_queued = Some(reason.to_owned());
            queued += 1;
        }
    }
    Ok(queued)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn g_formats_as_python() {
        assert_eq!(py_g(1.0), "1");
        assert_eq!(py_g(1.5), "1.5");
        assert_eq!(py_g(0.5), "0.5");
        assert_eq!(py_g(2.25), "2.25");
        assert_eq!(py_g(100.0), "100");
        assert_eq!(py_g(1e7), "1e+07");
        assert_eq!(py_g(0.0001), "0.0001");
        assert_eq!(py_g(0.00001), "1e-05");
        assert_eq!(py_g(123_456.0), "123456");
        assert_eq!(py_g(1_234_567.0), "1.23457e+06");
    }

    fn d(day: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(2026, 9, day).unwrap_or_default()
    }

    fn position(id: i64, instrument_id: i64, gtt: Option<&str>) -> PositionRow {
        PositionRow {
            id,
            instrument_id,
            entry_date: d(1),
            entry_avg: Decimal::new(10_000, 2),
            quantity_open: 10,
            initial_stop: Decimal::new(8_800, 2),
            stop_price: Decimal::new(8_800, 2),
            gtt_id: gtt.map(str::to_owned),
            gtt_trigger: None,
            exit_queued_for: None,
            exit_reason_queued: None,
            partial_done: false,
            trail: None,
        }
    }

    #[test]
    fn a_position_without_a_gtt_is_naked_and_one_with_is_not() {
        // test_vbt_evening.py::test_a_naked_position_gets_an_arm_gtt_line (the selection).
        let symbols = BTreeMap::from([(7, "AAA".to_owned())]);
        let got = naked_positions(
            &[position(1, 7, None), position(2, 8, Some("g-1"))],
            &symbols,
        );
        assert_eq!(got, vec![(7, "AAA".to_owned(), 10, Decimal::new(8_800, 2))]);
    }

    #[test]
    fn a_held_name_with_no_bar_holds_and_says_so_under_both_rules() {
        // test_vbt_qulla.py::test_a_young_history_holds — no bar today: `manage` holds, and
        // Qullamaggie's rule has no series to read.
        let config = VbtConfig::default();
        let rows = [position(1, 7, Some("g"))];
        let got = managed_actions(
            &rows,
            &BTreeMap::new(),
            &BTreeMap::new(),
            &config,
            &BTreeMap::new(),
            &BTreeMap::new(),
        )
        .unwrap_or_default();
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].action.action, Action::Hold);
        assert_eq!(got[0].action.note, "no bar today");
        assert_eq!(got[0].symbol, "7");
        assert_eq!(got[0].position_id, Some(1));
    }

    #[test]
    fn five_blank_sessions_write_the_position_off_before_any_exit_rule() {
        let config = VbtConfig::default();
        let rows = [position(1, 7, Some("g"))];
        let blanks = BTreeMap::from([(7, config.exits.no_bar_tolerance_sessions)]);
        let got = managed_actions(
            &rows,
            &BTreeMap::new(),
            &BTreeMap::new(),
            &config,
            &blanks,
            &BTreeMap::new(),
        )
        .unwrap_or_default();
        assert_eq!(got[0].action.action, Action::WriteOff);
        assert_eq!(got[0].action.reason, Some(ExitReason::NoBar));
    }

    fn calendar(days: &[u32]) -> SessionCalendar {
        SessionCalendar {
            sessions: days.iter().map(|n| d(*n)).collect(),
            dropped: Vec::new(),
            counts: BTreeMap::new(),
        }
    }

    #[test]
    fn the_session_count_is_derived_from_the_calendar_so_a_holiday_consumes_none() {
        // test_vbt_evening.py::test_a_holiday_consumes_no_session.
        let cal = calendar(&[1, 2, 4, 7]);
        let order = OrderRow {
            id: 1,
            instrument_id: 7,
            signal_date: d(1),
            limit_price: Decimal::new(10_000, 2),
            stop_price: Decimal::new(8_800, 2),
            quantity: 5,
            state: "SENT".to_owned(),
            sessions_worked: 0,
            expires_after_session: None,
            filled_quantity: 0,
        };
        let working = to_working(&order, &cal, d(4)).unwrap_or_else(|_| unreachable!());
        assert_eq!(working.sessions_worked, 2);
        assert_eq!(
            expires_after(d(1), &cal, &VbtConfig::default().entry).ok(),
            Some(Some(d(7)))
        );
    }
}
