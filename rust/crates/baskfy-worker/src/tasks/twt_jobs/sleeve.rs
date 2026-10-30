//! The sleeve's own money, its book and its half-size countdown, as the plan builders read them.
//!
//! Ports the reading half of `baskfy_api.twt_sleeve` (`open_positions`, `marks`, `realised_pnl`,
//! `sleeve_capital`, `load_sleeve`, `config_for`, `entries_already_this_session`, `book_state`,
//! `slot_multiplier`) and `baskfy_api.twt_settings.read_config`. The arithmetic is
//! `baskfy_core::twt::{sleeve, sizing, plan}`; this module reads rows and nothing else.
//!
//! It never writes: `count_first_live_entry` (the spending half of the countdown) belongs to a
//! fill, and a fill is the desk's — nothing in the worker reaches it (law 2).

use std::collections::{BTreeMap, BTreeSet, HashMap};

use baskfy_core::twt::config::TwtConfig;
use baskfy_core::twt::plan::{BookState, NakedPosition, RatchetDue, SellDue};
use baskfy_core::twt::sizing::first_live_multiplier;
use baskfy_core::twt::sleeve::{MarkSource, OpenPositionValue, SleeveValue, sleeve_value};
use baskfy_db::row::{date as row_date, decimal as row_decimal, opt_date, opt_decimal};
use chrono::NaiveDate;
use rust_decimal::Decimal;
use sqlx::{PgConnection, Row as _};

use crate::error::WorkerError;
use crate::steps::sleeves::for_twt_jobs::core_err;

/// `ENTERED_ORDER_STATES`: a buy that has been, or is being, sent.
const ENTERED_ORDER_STATES: [&str; 4] = ["CONFIRMED", "SENT", "PARTIAL", "FILLED"];

/// The `tw_config` columns the plan reads (`TwConfig`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ConfigRow {
    pub(crate) sleeve_capital_inr: Decimal,
    pub(crate) max_open_positions: i64,
    pub(crate) max_position_pct: Decimal,
    pub(crate) stop_pct: Decimal,
    pub(crate) trail_pct: Decimal,
    pub(crate) first_live_entries_left: i64,
}

/// `read_config` (and `twt_evening.config_row`): the user's row, or `None` when unseeded
/// (`TwtConfigNotSeeded`, which every caller here turns into its default).
pub(crate) async fn config_row(
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<Option<ConfigRow>, WorkerError> {
    let row = sqlx::query(
        "SELECT sleeve_capital_inr, max_open_positions::bigint AS max_open_positions, \
         max_position_pct, stop_pct, trail_pct, \
         first_live_entries_left::bigint AS first_live_entries_left \
         FROM tw_config WHERE user_id = $1",
    )
    .bind(user_id)
    .fetch_optional(&mut *conn)
    .await?;
    let Some(row) = row else {
        return Ok(None);
    };
    Ok(Some(ConfigRow {
        sleeve_capital_inr: row_decimal(&row, "sleeve_capital_inr")?,
        max_open_positions: row.try_get("max_open_positions")?,
        max_position_pct: row_decimal(&row, "max_position_pct")?,
        stop_pct: row_decimal(&row, "stop_pct")?,
        trail_pct: row_decimal(&row, "trail_pct")?,
        first_live_entries_left: row.try_get("first_live_entries_left")?,
    }))
}

/// `config_for(row)`: the defaults with the person's position cap and two stops written in.
pub(crate) fn config_for(row: &ConfigRow) -> TwtConfig {
    let mut config = TwtConfig::default();
    config.sizing.max_position_pct = row.max_position_pct;
    config.exits.stop_pct = row.stop_pct;
    config.exits.trail_pct = row.trail_pct;
    config
}

/// `twt_evening.load_twt_config`: `config_for(read_config(...))`, or the defaults when unseeded.
pub(crate) async fn evening_config(
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<TwtConfig, WorkerError> {
    Ok(config_row(conn, user_id)
        .await?
        .map_or_else(TwtConfig::default, |row| config_for(&row)))
}

/// One open `tw_position`, as `load_sleeve` reads it.
struct OpenRow {
    instrument_id: i64,
    quantity_open: i64,
    entry_avg: Decimal,
}

/// `open_positions`: the sleeve's book, ordered by `(instrument_id, id)`.
async fn open_positions(
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<Vec<OpenRow>, WorkerError> {
    let rows = sqlx::query(
        "SELECT instrument_id, quantity_open::bigint AS quantity_open, entry_avg \
         FROM tw_position WHERE user_id = $1 AND state = 'OPEN' AND quantity_open > 0 \
         ORDER BY instrument_id, id",
    )
    .bind(user_id)
    .fetch_all(&mut *conn)
    .await?;
    rows.iter()
        .map(|row| {
            Ok(OpenRow {
                instrument_id: row.try_get("instrument_id")?,
                quantity_open: row.try_get("quantity_open")?,
                entry_avg: row_decimal(row, "entry_avg")?,
            })
        })
        .collect()
}

/// `marks`: the newest published close on or before `as_of`, per instrument, with its session.
async fn marks(
    conn: &mut PgConnection,
    ids: &[i64],
    as_of: NaiveDate,
) -> Result<HashMap<i64, (Decimal, NaiveDate)>, WorkerError> {
    if ids.is_empty() {
        return Ok(HashMap::new());
    }
    let rows = sqlx::query(
        "SELECT o.instrument_id, o.date, o.close FROM ohlcv_daily o JOIN ( \
             SELECT instrument_id, max(date) AS date FROM ohlcv_daily \
             WHERE instrument_id = ANY($1) AND date <= $2 GROUP BY instrument_id) latest \
         ON o.instrument_id = latest.instrument_id AND o.date = latest.date",
    )
    .bind(ids)
    .bind(as_of)
    .fetch_all(&mut *conn)
    .await?;
    let mut out = HashMap::new();
    for row in &rows {
        out.insert(
            row.try_get::<i64, _>("instrument_id")?,
            (row_decimal(row, "close")?, row_date(row, "date")?),
        );
    }
    Ok(out)
}

/// `realised_pnl(user_id, as_of)`: Σ `pnl_inr` over `CLOSED` positions closed on or before.
async fn realised_pnl(
    conn: &mut PgConnection,
    user_id: i64,
    as_of: NaiveDate,
) -> Result<Decimal, WorkerError> {
    let row = sqlx::query(
        "SELECT coalesce(sum(pnl_inr), 0) AS total FROM tw_position \
         WHERE user_id = $1 AND state = 'CLOSED' AND closed_on <= $2",
    )
    .bind(user_id)
    .bind(as_of)
    .fetch_one(&mut *conn)
    .await?;
    Ok(row_decimal(&row, "total")?)
}

/// `LoadedSleeve`: the sleeve's money as of a session.
#[derive(Debug, Clone)]
pub(crate) struct LoadedSleeve {
    pub(crate) value: SleeveValue,
    pub(crate) positions: Vec<OpenPositionValue>,
}

impl LoadedSleeve {
    /// `len(stale_marks)`: positions whose mark is not this session's close.
    pub(crate) fn stale_marks(&self) -> i64 {
        i64::try_from(self.positions.iter().filter(|p| p.is_stale_mark()).count())
            .unwrap_or(i64::MAX)
    }

    /// `sleeve.value.quantize().equity_inr`.
    pub(crate) fn quantized_equity(&self) -> Result<Decimal, WorkerError> {
        self.value
            .quantize()
            .and_then(|v| v.equity_inr())
            .map_err(core_err)
    }
}

/// `load_sleeve(session, user_id, as_of)` (`04` §9.1): a missing print falls back to the last
/// known close, a name never printed since the fill to its entry; neither is dropped.
pub(crate) async fn load_sleeve(
    conn: &mut PgConnection,
    user_id: i64,
    as_of: NaiveDate,
) -> Result<LoadedSleeve, WorkerError> {
    let positions = open_positions(conn, user_id).await?;
    let ids: Vec<i64> = positions.iter().map(|p| p.instrument_id).collect();
    let prices = marks(conn, &ids, as_of).await?;
    let valued: Vec<OpenPositionValue> = positions
        .iter()
        .map(|row| {
            let (source, price) = match prices.get(&row.instrument_id) {
                None => (MarkSource::Entry, row.entry_avg),
                Some((close, on)) if *on == as_of => (MarkSource::SessionClose, *close),
                Some((close, _)) => (MarkSource::LastKnownClose, *close),
            };
            OpenPositionValue {
                instrument_id: row.instrument_id,
                quantity_open: row.quantity_open,
                entry_avg: row.entry_avg,
                mark: price,
                mark_source: source,
            }
        })
        .collect();
    let capital = config_row(conn, user_id)
        .await?
        .map_or(Decimal::ZERO, |row| row.sleeve_capital_inr);
    let realised = realised_pnl(conn, user_id, as_of).await?;
    let value = sleeve_value(capital, realised, &valued).map_err(core_err)?;
    Ok(LoadedSleeve {
        value,
        positions: valued,
    })
}

/// `entries_already_this_session(user_id, on)`: buys sent on `on` (IST), whatever plan.
async fn entries_already_this_session(
    conn: &mut PgConnection,
    user_id: i64,
    on: Option<NaiveDate>,
) -> Result<i64, WorkerError> {
    let Some(on) = on else {
        return Ok(0);
    };
    let (count,): (i64,) = sqlx::query_as(
        "SELECT count(*) FROM tw_order WHERE user_id = $1 \
         AND date(timezone('Asia/Kolkata', created_at)) = $2 AND side = 'BUY' \
         AND state = ANY($3)",
    )
    .bind(user_id)
    .bind(on)
    .bind(&ENTERED_ORDER_STATES[..])
    .fetch_one(&mut *conn)
    .await?;
    Ok(count)
}

/// `book_state(session, user_id=, as_of=, signal_date=, entries_on=)`: what `build_entries` and
/// `exit_lines` read. Every position it reports is a `tw_position` row.
pub(crate) async fn book_state(
    conn: &mut PgConnection,
    user_id: i64,
    as_of: NaiveDate,
    signal_date: Option<NaiveDate>,
    entries_on: Option<NaiveDate>,
) -> Result<BookState, WorkerError> {
    let sleeve = load_sleeve(conn, user_id, as_of).await?;
    let rows = sqlx::query(
        "SELECT p.id, p.instrument_id, i.symbol, p.quantity_open::bigint AS quantity_open, \
         p.stop_price, p.gtt_id, p.gtt_trigger, p.next_trigger, p.next_trigger_for, \
         p.high_since, p.trail, p.partial_queued_for, \
         p.partial_quantity::bigint AS partial_quantity, p.exit_queued_for, \
         p.exit_reason_queued \
         FROM tw_position p JOIN instrument i ON i.id = p.instrument_id \
         WHERE p.user_id = $1 AND p.state = 'OPEN' AND p.quantity_open > 0 \
         ORDER BY i.symbol",
    )
    .bind(user_id)
    .fetch_all(&mut *conn)
    .await?;
    let mut naked = Vec::new();
    let mut ratchets = Vec::new();
    let mut sells = Vec::new();
    for row in &rows {
        let id: i64 = row.try_get("id")?;
        let instrument_id: i64 = row.try_get("instrument_id")?;
        let symbol: String = row.try_get("symbol")?;
        let quantity: i64 = row.try_get("quantity_open")?;
        let trail: Option<String> = row.try_get("trail")?;
        let trail_word = trail
            .as_deref()
            .filter(|t| !t.is_empty())
            .unwrap_or("trail")
            .to_owned();
        let exit_for = opt_date(row, "exit_queued_for")?;
        let exit_reason: Option<String> = row.try_get("exit_reason_queued")?;
        let partial_for = opt_date(row, "partial_queued_for")?;
        let partial_quantity: Option<i64> = row.try_get("partial_quantity")?;
        match (exit_for, exit_reason.as_deref().filter(|r| !r.is_empty())) {
            (Some(decided_for), Some(reason)) => sells.push(SellDue {
                instrument_id,
                symbol: symbol.clone(),
                quantity,
                reason: reason.to_owned(),
                decided_for,
                note: format!("close below the {trail_word}: sell the remainder"),
                position_id: Some(id),
            }),
            _ => {
                if let (Some(decided_for), Some(q)) =
                    (partial_for, partial_quantity.filter(|q| *q != 0))
                {
                    sells.push(SellDue {
                        instrument_id,
                        symbol: symbol.clone(),
                        quantity: q,
                        reason: "PARTIAL".to_owned(),
                        decided_for,
                        note: "a third into strength; the stop moves to breakeven".to_owned(),
                        position_id: Some(id),
                    });
                }
            }
        }
        let gtt_id: Option<String> = row.try_get("gtt_id")?;
        let gtt_trigger = opt_decimal(row, "gtt_trigger")?;
        let Some(gtt_trigger) = gtt_trigger.filter(|_| gtt_id.is_some()) else {
            naked.push(NakedPosition {
                instrument_id,
                symbol,
                quantity,
                stop_price: row_decimal(row, "stop_price")?,
                position_id: Some(id),
            });
            continue;
        };
        let (Some(next_trigger), Some(next_trigger_for)) = (
            opt_decimal(row, "next_trigger")?,
            opt_date(row, "next_trigger_for")?,
        ) else {
            continue;
        };
        ratchets.push(RatchetDue {
            instrument_id,
            symbol,
            quantity,
            gtt_trigger,
            next_trigger,
            next_trigger_for,
            high_since: row_decimal(row, "high_since")?,
            note: if trail.is_some() {
                format!("stop to breakeven ({trail_word} rule)")
            } else {
                String::new()
            },
            position_id: Some(id),
        });
    }
    let mut counts: BTreeMap<i64, i64> = BTreeMap::new();
    for p in &sleeve.positions {
        *counts.entry(p.instrument_id).or_insert(0) += 1;
    }
    let on = entries_on.or(signal_date).or(Some(as_of));
    Ok(BookState {
        open_instrument_ids: sleeve
            .positions
            .iter()
            .map(|p| p.instrument_id)
            .collect::<BTreeSet<i64>>(),
        open_exposure_inr: sleeve.value.open_exposure_inr(),
        cash_available_inr: sleeve.value.cash_available_inr().map_err(core_err)?,
        entries_already_this_session: entries_already_this_session(conn, user_id, on).await?,
        positions_naked_of_gtt: naked,
        ratchets_due: ratchets,
        sells_due: sells,
        open_entry_counts: counts,
    })
}

/// `slot_multiplier(user_id=, execution_enabled=, config=)`: half while the first live entries
/// last **and** execution is on; one otherwise (and for an unseeded sleeve).
pub(crate) async fn slot_multiplier(
    conn: &mut PgConnection,
    user_id: i64,
    execution_enabled: bool,
    config: &TwtConfig,
) -> Result<Decimal, WorkerError> {
    Ok(match config_row(conn, user_id).await? {
        None => Decimal::ONE,
        Some(row) => first_live_multiplier(
            row.first_live_entries_left,
            execution_enabled,
            &config.sizing,
        ),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row() -> ConfigRow {
        ConfigRow {
            sleeve_capital_inr: Decimal::new(250_000_000, 2),
            max_open_positions: 10,
            max_position_pct: Decimal::new(1000, 2),
            stop_pct: Decimal::new(1800, 2),
            trail_pct: Decimal::new(2200, 2),
            first_live_entries_left: 4,
        }
    }

    #[test]
    fn config_for_writes_the_three_engine_fields_and_nothing_else() {
        let config = config_for(&row());
        let base = TwtConfig::default();
        assert_eq!(config.sizing.max_position_pct, Decimal::new(1000, 2));
        assert_eq!(config.exits.stop_pct, Decimal::new(1800, 2));
        assert_eq!(config.exits.trail_pct, Decimal::new(2200, 2));
        // The capital becomes equity, and the slot count is handed to build_entries separately.
        assert_eq!(config.sizing.max_slots, base.sizing.max_slots);
        assert_eq!(config.scan, base.scan);
    }

    #[test]
    fn a_dry_run_plan_is_full_size_and_a_live_one_half_while_entries_last() {
        // test_twt_sleeve_db.py: the multiplier reads the countdown only with execution on.
        let config = TwtConfig::default();
        assert_eq!(
            first_live_multiplier(4, false, &config.sizing),
            Decimal::ONE
        );
        assert_eq!(
            first_live_multiplier(4, true, &config.sizing),
            config.sizing.risk_multiplier_first_live
        );
        assert_eq!(first_live_multiplier(0, true, &config.sizing), Decimal::ONE);
    }
}
