//! Ports `baskfy_worker.fno.f3_scan`: the evening scan for F3, the directional index credit
//! spread (`04` §11, §8; F3-4). One `fo_scan` row per underlying, in `04` §8's order of
//! states. Reads `fo_index_daily`, `op_index_minute`, `op_expiry`/`op_contract` and
//! `fo_contract_daily`. **No network, no order.**

use std::collections::BTreeSet;

use baskfy_core::fno::calendar::{PyDateTime, monthly_expiries};
use baskfy_core::fno::config::{FnoCeilings, FnoConfig, ScanState, f3_sleeve_for};
use baskfy_core::fno::directional::{
    self as d, Confirm, Direction, Levels, MinuteBar, StrikePrices,
};
use baskfy_core::fno::directional_retest::strike_step;
use baskfy_core::fno::vol::OptionType;
use baskfy_core::options::gating::Mode;
use baskfy_db::OrderedJson;
use chrono::{DateTime, NaiveDate, NaiveTime, Utc};
use rust_decimal::Decimal;
use sqlx::PgConnection;

use super::common::{dstr, fno_err, iso, opt_dstr, opt_iso, plan_state, q_dec, scan_state};
use super::index_daily::{daily_bars, index_symbol};
use super::scan::{
    Detail, Market, ScanRow, UserBook, option_value, pauses, put, py_opt, sizing_mode,
};
use super::underlying::minus;
use crate::error::WorkerError;
use crate::pyjson::object;
use crate::pyjson::s;

/// Calendar days of minute bars read for the 75-minute confirm.
const MINUTE_LOOKBACK_DAYS: u64 = 10;
const INTRADAY_NOTE: &str =
    "the intraday check (the index against the session's open) and the level are read at 09:20";

/// `minute_bars`: the index's minute bars up to the close of `trade_date`, in IST (naive).
pub(crate) async fn minute_bars(
    conn: &mut PgConnection,
    underlying: &str,
    trade_date: NaiveDate,
) -> Result<Vec<MinuteBar>, WorkerError> {
    let Some(symbol) = index_symbol(underlying) else {
        return Ok(Vec::new());
    };
    let ids: Vec<(i64,)> =
        sqlx::query_as("SELECT id FROM instrument WHERE symbol = $1 AND instrument_type = 'INDEX'")
            .bind(symbol)
            .fetch_all(&mut *conn)
            .await?;
    let ident = match ids.as_slice() {
        [] => return Ok(Vec::new()),
        [(id,)] => *id,
        _ => {
            return Err(WorkerError::python(
                "MultipleResultsFound",
                "Multiple rows were found when one or none was required",
            ));
        }
    };
    let ist = baskfy_providers::tokens::ist();
    let start = minus(trade_date, MINUTE_LOOKBACK_DAYS)
        .and_time(NaiveTime::MIN)
        .and_local_timezone(ist)
        .single();
    let end = trade_date
        .and_hms_opt(23, 59, 0)
        .and_then(|n| n.and_local_timezone(ist).single());
    let (Some(start), Some(end)) = (start, end) else {
        return Ok(Vec::new());
    };
    let rows: Vec<(DateTime<Utc>, Decimal, Decimal, Decimal, Decimal)> = sqlx::query_as(
        "SELECT ts, open, high, low, close FROM op_index_minute WHERE instrument_id = $1 \
         AND ts >= $2 AND ts <= $3 ORDER BY ts",
    )
    .bind(ident)
    .bind(start)
    .bind(end)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows
        .into_iter()
        .map(|(ts, open, high, low, close)| MinuteBar {
            ts: PyDateTime::Naive(ts.with_timezone(&ist).naive_local()),
            open,
            high,
            low,
            close,
        })
        .collect())
}

async fn settles(
    conn: &mut PgConnection,
    underlying: &str,
    expiry: NaiveDate,
    trade_date: NaiveDate,
) -> Result<StrikePrices, WorkerError> {
    let rows: Vec<(Decimal, String, Option<Decimal>)> = sqlx::query_as(
        "SELECT strike, option_type, settle FROM fo_contract_daily WHERE trade_date = $1 \
         AND symbol = $2 AND expiry = $3 AND instrument = 'OPTIDX'",
    )
    .bind(trade_date)
    .bind(underlying)
    .bind(expiry)
    .fetch_all(&mut *conn)
    .await?;
    let mut out: StrikePrices = Vec::new();
    for (strike, kind, settle) in rows {
        let Some(settle) = settle.filter(|v| *v > Decimal::ZERO) else {
            continue;
        };
        let option_type = match kind.as_str() {
            "CE" => OptionType::Ce,
            "PE" => OptionType::Pe,
            _ => continue,
        };
        // A dict: a repeated key keeps its first position and its last value.
        match out
            .iter_mut()
            .find(|((k, t), _)| *k == strike && *t == option_type)
        {
            Some(slot) => slot.1 = settle,
            None => out.push(((strike, option_type), settle)),
        }
    }
    Ok(out)
}

async fn listed_strikes(
    conn: &mut PgConnection,
    underlying: &str,
    expiry: NaiveDate,
) -> Result<Vec<Decimal>, WorkerError> {
    let rows: Vec<(Decimal,)> =
        sqlx::query_as("SELECT strike FROM op_contract WHERE underlying = $1 AND expiry = $2")
            .bind(underlying)
            .bind(expiry)
            .fetch_all(&mut *conn)
            .await?;
    Ok(sorted_unique(rows.into_iter().map(|(k,)| k)))
}

/// `sorted({...})` over decimals: equal values once (the first seen), ascending.
fn sorted_unique(values: impl Iterator<Item = Decimal>) -> Vec<Decimal> {
    let mut seen: BTreeSet<Decimal> = BTreeSet::new();
    let mut out = Vec::new();
    for v in values {
        if seen.insert(v.normalize()) {
            out.push(v);
        }
    }
    out.sort();
    out
}

fn levels_detail(lv: &Levels) -> OrderedJson {
    let pivots = |list: &[d::Pivot]| {
        let tail = &list[list.len().saturating_sub(3)..];
        OrderedJson::Array(
            tail.iter()
                .map(|p| object([("date", s(iso(p.date))), ("price", s(dstr(p.price)))]))
                .collect(),
        )
    };
    object([
        ("close", s(dstr(lv.close))),
        ("support", opt_dstr(lv.support)),
        ("resistance", opt_dstr(lv.resistance)),
        ("weekly_high", s(dstr(lv.weekly_high))),
        ("weekly_low", s(dstr(lv.weekly_low))),
        ("trend_avg", s(dstr(lv.trend_avg))),
        ("pivot_highs", pivots(&lv.pivot_highs)),
        ("pivot_lows", pivots(&lv.pivot_lows)),
    ])
}

fn confirm_detail(verdict: &Confirm) -> OrderedJson {
    object([
        ("agrees", OrderedJson::Bool(verdict.agrees)),
        (
            "bar_start",
            verdict
                .bar_start
                .map_or(OrderedJson::Null, |b| s(b.isoformat())),
        ),
        ("last_close", opt_dstr(verdict.last_close)),
        ("average", opt_dstr(verdict.average)),
        ("message", s(verdict.message.clone())),
    ])
}

/// `f"{x or 'none'}"` for an optional decimal.
fn or_none(d: Option<Decimal>) -> String {
    d.filter(|v| !v.is_zero())
        .map_or_else(|| "none".to_owned(), dstr)
}

/// `f3_row`: one underlying's F3 row, one return per state of `04` §8.
#[allow(clippy::too_many_lines)]
pub(crate) async fn f3_row(
    conn: &mut PgConnection,
    market: &Market,
    book: &UserBook,
    underlying: &str,
    config: &FnoConfig,
    ceilings: &FnoCeilings,
) -> Result<ScanRow, WorkerError> {
    let f3 = &config.f3;
    let sleeve = f3_sleeve_for(underlying).map_err(fno_err)?;
    let s1 = market.next_session;
    let mut base: Detail = vec![
        ("underlying".to_owned(), s(underlying)),
        ("next_session".to_owned(), opt_iso(s1)),
        ("sleeve".to_owned(), s(sleeve.value())),
    ];
    if let Some(position) = book
        .open_positions
        .get(sleeve.value())
        .and_then(|v| v.first())
    {
        let mut detail = base.clone();
        put(&mut detail, "position_id", OrderedJson::Int(position.id));
        put(&mut detail, "level", opt_dstr(position.stop_price));
        put(&mut detail, "legs", position.legs.clone());
        return ScanRow::new(
            underlying,
            ScanState::OpenPosition,
            vec![format!(
                "an F3 spread on {underlying} is open (plan {}); one per underlying (04 §11); an \
                 add is the monitor's call in the window",
                position.entry_plan_id
            )],
            detail,
        );
    }
    let Some(s1) = s1 else {
        return ScanRow::new(
            underlying,
            ScanState::NoData,
            vec![format!(
                "the trading_day calendar has no session after {}",
                iso(market.trade_date)
            )],
            base,
        );
    };
    let need = d::bars_needed(f3);
    let bars = daily_bars(conn, underlying, market.trade_date, need).await?;
    let have_count = i64::try_from(bars.len()).unwrap_or(i64::MAX);
    if have_count < need || bars.last().map(|b| b.date) != Some(market.trade_date) {
        let have = bars
            .last()
            .map_or_else(|| "none".to_owned(), |b| iso(b.date));
        return ScanRow::new(
            underlying,
            ScanState::NoData,
            vec![format!(
                "fo_index_daily has {} of the {need} sessions F3 needs for {underlying} up to {} \
                 (latest {have}); run fno_cli index-daily",
                bars.len(),
                iso(market.trade_date)
            )],
            base,
        );
    }
    let Some(lv) = d::levels(&bars, f3).map_err(fno_err)? else {
        return ScanRow::new(
            underlying,
            ScanState::NoData,
            vec!["the levels could not be read".to_owned()],
            base,
        );
    };
    put(&mut base, "levels", levels_detail(&lv));
    let found = pauses(book, "F3", sleeve);
    if !found.is_empty() {
        return ScanRow::new(underlying, ScanState::Paused, found, base);
    }
    let daily = d::daily_direction(&lv);
    if daily == Direction::None {
        let mut detail = base.clone();
        put(&mut detail, "direction", s(daily.value()));
        return ScanRow::new(
            underlying,
            ScanState::NoSignal,
            vec![format!(
                "the daily chart gives no direction: close {} against its {}-session average {}, \
                 support {}, resistance {} (04 §11)",
                dstr(lv.close),
                f3.trend_sessions,
                dstr(lv.trend_avg),
                or_none(lv.support),
                or_none(lv.resistance)
            )],
            detail,
        );
    }
    let minutes = minute_bars(conn, underlying, market.trade_date).await?;
    let bars75 = d::seventy_five_minute_bars(&minutes).map_err(fno_err)?;
    if i64::try_from(bars75.len()).unwrap_or(i64::MAX) < f3.confirm_bars {
        let mut detail = base.clone();
        put(&mut detail, "direction_daily", s(daily.value()));
        return ScanRow::new(
            underlying,
            ScanState::NoData,
            vec![format!(
                "{} completed 75-minute bars < the {} the confirm needs (op_index_minute for \
                 {}); the daily read is {} but unconfirmed, and an unconfirmed read is not a \
                 trade (04 §11)",
                bars75.len(),
                f3.confirm_bars,
                index_symbol(underlying).unwrap_or("None"),
                daily.value()
            )],
            detail,
        );
    }
    let (direction, verdict) = d::direction_for(&lv, &bars75, f3).map_err(fno_err)?;
    put(&mut base, "direction_daily", s(daily.value()));
    put(&mut base, "confirm", confirm_detail(&verdict));
    if direction == Direction::None {
        let mut detail = base.clone();
        put(&mut detail, "direction", s(direction.value()));
        return ScanRow::new(
            underlying,
            ScanState::NoSignal,
            vec![format!(
                "the daily read is {} but the 75-minute bar disagrees: {}",
                daily.value(),
                verdict.message
            )],
            detail,
        );
    }
    let level = d::key_level(direction, &lv);
    put(&mut base, "direction", s(direction.value()));
    put(&mut base, "level", opt_dstr(level));
    if !market.ingested() {
        return ScanRow::new(
            underlying,
            ScanState::NoData,
            vec![format!(
                "the bhavcopy for {} is not ingested ({}); the spread is not priced from a guess",
                iso(market.trade_date),
                market
                    .ingest_status
                    .as_deref()
                    .filter(|x| !x.is_empty())
                    .unwrap_or("never tried")
            )],
            base,
        );
    }
    let listed = market.expiries.get(underlying).cloned().unwrap_or_default();
    let (expiry, kind) = if underlying == "NIFTY" {
        (
            d::choose_expiry(&listed, &market.sessions, s1, f3.min_sessions_weekly),
            "weekly",
        )
    } else {
        (
            d::choose_expiry(
                &monthly_expiries(&listed),
                &market.sessions,
                s1,
                f3.min_sessions_monthly,
            ),
            "monthly",
        )
    };
    let Some(expiry) = expiry else {
        return ScanRow::new(
            underlying,
            ScanState::NoData,
            vec![format!(
                "the NFO master lists no {kind} {underlying} expiry with enough sessions after {}",
                iso(s1)
            )],
            base,
        );
    };
    put(&mut base, "expiry", s(iso(expiry)));
    put(&mut base, "expiry_kind", s(kind));
    let prices = settles(conn, underlying, expiry, market.trade_date).await?;
    let mut strikes = listed_strikes(conn, underlying, expiry).await?;
    if strikes.is_empty() {
        strikes = sorted_unique(prices.iter().map(|((k, _), _)| *k));
    }
    let Some(step) = strike_step(&strikes, lv.close).map_err(fno_err)? else {
        return ScanRow::new(
            underlying,
            ScanState::NoData,
            vec![format!(
                "no strike grid for {underlying} {} in the master or the bhavcopy",
                iso(expiry)
            )],
            base,
        );
    };
    let lot_size = market
        .expiry_lots
        .get(&(underlying.to_owned(), expiry))
        .copied();
    let proposal = d::propose_spread(
        Mode::Paper,
        direction,
        &lv,
        step,
        &prices,
        lot_size,
        book.capital.get("F3").copied().unwrap_or(Decimal::ZERO),
        f3,
        &config.common,
        ceilings,
    )
    .map_err(fno_err)?;
    let st = &proposal.strikes;
    let (long_role, short_role) = if st.option_type == OptionType::Pe {
        ("LONG_PUT", "SHORT_PUT")
    } else {
        ("LONG_CALL", "SHORT_CALL")
    };
    let legs = OrderedJson::Array(vec![
        object([
            ("entry_seq", OrderedJson::Int(1)),
            ("role", s(long_role)),
            ("strike", s(dstr(st.wing))),
            ("option_type", s(option_value(st.option_type))),
            ("expiry", s(iso(expiry))),
            ("qty_sign", OrderedJson::Int(1)),
            ("settle", s(dstr(proposal.wing_price))),
        ]),
        object([
            ("entry_seq", OrderedJson::Int(2)),
            ("role", s(short_role)),
            ("strike", s(dstr(st.short))),
            ("option_type", s(option_value(st.option_type))),
            ("expiry", s(iso(expiry))),
            ("qty_sign", OrderedJson::Int(-1)),
            ("settle", s(dstr(proposal.short_price))),
        ]),
    ]);
    let decay = proposal
        .credit
        .map(|c| d::decay_target_mark(c, f3))
        .transpose()
        .map_err(fno_err)?;
    let loss = proposal
        .credit
        .map(|c| d::loss_cut_mark(c, f3))
        .transpose()
        .map_err(fno_err)?;
    let mut detail = base.clone();
    for (k, v) in [
        ("entry_session", s(iso(s1))),
        ("strike_step", s(dstr(step))),
        (
            "lot_size",
            lot_size.map_or(OrderedJson::Null, OrderedJson::Int),
        ),
        ("short_strike", s(dstr(st.short))),
        ("wing_strike", s(dstr(st.wing))),
        ("option_type", s(option_value(st.option_type))),
        ("legs", legs),
        ("credit_points", opt_dstr(proposal.credit)),
        ("max_loss_per_unit", opt_dstr(proposal.max_loss_per_unit)),
        (
            "max_loss_per_lot_inr",
            opt_dstr(proposal.max_loss_per_lot_inr),
        ),
        ("decay_target_mark", opt_dstr(decay)),
        ("loss_cut_mark", opt_dstr(loss)),
    ] {
        put(&mut detail, k, v);
    }
    if let Some(sz) = &proposal.sizing {
        put(&mut detail, "lots", OrderedJson::Int(sz.lots));
        put(
            &mut detail,
            "sizing",
            object([
                ("lots", OrderedJson::Int(sz.lots)),
                ("sizing_mode", s(sizing_mode(sz.sizing_mode))),
                (
                    "risk_budget_inr",
                    s(py_opt(q_dec(Some(sz.risk_budget_inr), 2))),
                ),
                ("max_loss_inr", s(py_opt(q_dec(Some(sz.max_loss_inr), 2)))),
                ("message", s(sz.message.clone())),
            ]),
        );
    }
    let mut reasons = proposal.reasons.clone();
    reasons.push(INTRADAY_NOTE.to_owned());
    let state = match proposal.state {
        Some(state) => plan_state(state).to_owned(),
        None => {
            reasons.insert(
                0,
                format!(
                    "{}: sell the {} {} {}, wing {}; out at once if the index trades beyond {} \
                     (04 §11)",
                    direction.value(),
                    option_value(st.option_type),
                    dstr(st.short),
                    iso(expiry),
                    dstr(st.wing),
                    level.map_or_else(|| "None".to_owned(), dstr)
                ),
            );
            scan_state(ScanState::Candidate).to_owned()
        }
    };
    let mut row = ScanRow::with_state(underlying, state, reasons, detail)?;
    row.credit = q_dec(proposal.credit, 2);
    row.max_loss_per_lot = q_dec(proposal.max_loss_per_lot_inr, 2);
    Ok(row)
}
