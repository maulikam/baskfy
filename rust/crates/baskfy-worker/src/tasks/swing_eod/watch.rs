//! The watchlist as the evening uses it — ports `baskfy_api.swing_watch` (`auto_watch`,
//! `expire_stale`, `refresh_focus`, `_sessions_ahead`, `list_watch`'s rows).
//!
//! The ORM's unit of work is reproduced where a table shows it: a row is updated only when a
//! value changes, and then with `updated_at = now()` (the `onupdate`); rows are inserted in the
//! order Python adds them, so the sequence hands out the same ids.

use std::collections::BTreeSet;

use baskfy_core::swing::setups::SwingConfig;
use chrono::NaiveDate;
use rust_decimal::Decimal;
use sqlx::{PgConnection, Row as _};

use crate::error::WorkerError;
use crate::steps::sleeves::for_swing_eod::NSE_EXCHANGE_ID;

/// `WATCHING`.
pub(super) const WATCHING: &str = "WATCHING";
/// `EXPIRED`.
const EXPIRED: &str = "EXPIRED";
/// `SOURCE_DETECTOR`.
const SOURCE_DETECTOR: &str = "DETECTOR";
/// `SOURCE_MANUAL`.
const SOURCE_MANUAL: &str = "MANUAL";
/// `Setup.EP.value`, `Setup.FLAG.value`.
const EP: &str = "EP";
const FLAG: &str = "FLAG";
/// `CandidateStatus.SETTING_UP.value`.
const SETTING_UP: &str = "SETTING_UP";

/// `AutoWatchResult` (the counters the evening reads).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct AutoWatchResult {
    pub(crate) added: i64,
    pub(crate) expired: i64,
}

fn count(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

/// `_sessions_ahead(start, count)`: the date `count` sessions after `start`, or `None` when the
/// calendar ends first.
async fn sessions_ahead(
    conn: &mut PgConnection,
    start: NaiveDate,
    n: i64,
) -> Result<Option<NaiveDate>, WorkerError> {
    let rows: Vec<(NaiveDate,)> = sqlx::query_as(
        "SELECT trading_day.date FROM trading_day WHERE trading_day.exchange_id = $1 \
         AND trading_day.is_trading_day IS true AND trading_day.date > $2 \
         ORDER BY trading_day.date LIMIT $3",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(start)
    .bind(n)
    .fetch_all(&mut *conn)
    .await?;
    Ok(if count(rows.len()) == n {
        rows.last().map(|r| r.0)
    } else {
        None
    })
}

fn valid_bars(setup: &str, config: &SwingConfig) -> i64 {
    if setup == EP {
        config.ep.valid_bars
    } else {
        config.watch.flag_valid_bars
    }
}

/// `expire_stale`: give a MANUAL row with no expiry its clock, then retire every WATCHING row
/// whose `expires_on` has passed. Returns how many expired.
async fn expire_stale(
    conn: &mut PgConnection,
    user_id: i64,
    on: NaiveDate,
    config: &SwingConfig,
) -> Result<i64, WorkerError> {
    let unbounded = sqlx::query(
        "SELECT id, added_on, reconfirmed_on FROM sw_watch WHERE user_id = $1 AND state = $2 \
         AND source = $3 AND expires_on IS NULL",
    )
    .bind(user_id)
    .bind(WATCHING)
    .bind(SOURCE_MANUAL)
    .fetch_all(&mut *conn)
    .await?;
    for row in &unbounded {
        let id: i64 = row.try_get("id")?;
        let added: NaiveDate = row.try_get("added_on")?;
        let reconfirmed: Option<NaiveDate> = row.try_get("reconfirmed_on")?;
        let since = reconfirmed.unwrap_or(added);
        if let Some(expires) = sessions_ahead(conn, since, config.watch.manual_valid_bars).await? {
            sqlx::query("UPDATE sw_watch SET expires_on = $1, updated_at = now() WHERE id = $2")
                .bind(expires)
                .bind(id)
                .execute(&mut *conn)
                .await?;
        }
    }
    let stale: Vec<(i64,)> = sqlx::query_as(
        "SELECT id FROM sw_watch WHERE user_id = $1 AND state = $2 \
         AND expires_on IS NOT NULL AND expires_on < $3",
    )
    .bind(user_id)
    .bind(WATCHING)
    .bind(on)
    .fetch_all(&mut *conn)
    .await?;
    for (id,) in &stale {
        sqlx::query("UPDATE sw_watch SET state = $1, updated_at = now() WHERE id = $2")
            .bind(EXPIRED)
            .bind(id)
            .execute(&mut *conn)
            .await?;
    }
    Ok(count(stale.len()))
}

/// One `sw_setup_daily` candidate.
struct Candidate {
    instrument_id: i64,
    date: NaiveDate,
    setup: String,
    score: Option<Decimal>,
    trigger: Option<Decimal>,
    stop_ref: Option<Decimal>,
    adr_pct: Option<Decimal>,
}

/// `auto_watch`: expire the stale rows, then put every EP and the top `auto_watch_top_n` flags
/// (by score, ties to the lower instrument id) on the list unless already watching. Idempotent.
pub(crate) async fn auto_watch(
    conn: &mut PgConnection,
    user_id: i64,
    on: NaiveDate,
    config: &SwingConfig,
) -> Result<AutoWatchResult, WorkerError> {
    let expired = expire_stale(conn, user_id, on, config).await?;
    let min_score = baskfy_core::py::decimal_from_float_str(config.watch.auto_watch_min_score)
        .map_err(|e| WorkerError::python("InvalidOperation", e.to_string()))?;
    // No ORDER BY, as Python's: the EPs keep the order the database returns them in.
    let rows = sqlx::query(
        "SELECT sw_setup_daily.instrument_id, sw_setup_daily.date, sw_setup_daily.setup, \
         sw_setup_daily.score, sw_setup_daily.trigger, sw_setup_daily.stop_ref, \
         sw_setup_daily.adr_pct FROM sw_setup_daily WHERE sw_setup_daily.user_id = $1 \
         AND sw_setup_daily.date = $2 AND sw_setup_daily.setup IN ($3, $4) \
         AND (sw_setup_daily.setup = $5 OR sw_setup_daily.status = $6 \
         AND sw_setup_daily.score >= $7)",
    )
    .bind(user_id)
    .bind(on)
    .bind(FLAG)
    .bind(EP)
    .bind(EP)
    .bind(SETTING_UP)
    .bind(min_score)
    .fetch_all(&mut *conn)
    .await?;
    let mut candidates = Vec::with_capacity(rows.len());
    for row in &rows {
        candidates.push(Candidate {
            instrument_id: row.try_get("instrument_id")?,
            date: row.try_get("date")?,
            setup: row.try_get("setup")?,
            score: baskfy_db::row::opt_decimal(row, "score")?,
            trigger: baskfy_db::row::opt_decimal(row, "trigger")?,
            stop_ref: baskfy_db::row::opt_decimal(row, "stop_ref")?,
            adr_pct: baskfy_db::row::opt_decimal(row, "adr_pct")?,
        });
    }
    // `_top_flags`: every EP, then the flags by (-score, instrument_id). `Decimal(None)` would
    // raise in Python; a candidate flag always has a score (the query requires one).
    let mut flags: Vec<&Candidate> = candidates.iter().filter(|c| c.setup != EP).collect();
    flags.sort_by(|a, b| {
        b.score
            .unwrap_or_default()
            .cmp(&a.score.unwrap_or_default())
            .then(a.instrument_id.cmp(&b.instrument_id))
    });
    let top = usize::try_from(config.watch.auto_watch_top_n.max(0)).unwrap_or(0);
    let mut chosen: Vec<&Candidate> = candidates.iter().filter(|c| c.setup == EP).collect();
    chosen.extend(flags.into_iter().take(top));

    let existing_rows: Vec<(i64, String)> = sqlx::query_as(
        "SELECT instrument_id, setup FROM sw_watch WHERE user_id = $1 AND state = $2",
    )
    .bind(user_id)
    .bind(WATCHING)
    .fetch_all(&mut *conn)
    .await?;
    let mut existing: BTreeSet<(i64, String)> = existing_rows.into_iter().collect();
    let mut added = 0;
    for c in chosen {
        let key = (c.instrument_id, c.setup.clone());
        if existing.contains(&key) {
            continue;
        }
        let expires = sessions_ahead(conn, on, valid_bars(&c.setup, config)).await?;
        sqlx::query(
            "INSERT INTO sw_watch (user_id, instrument_id, setup, source, added_on, expires_on, \
             trigger, stop_ref, setup_daily_date, state, score, adr_pct) \
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
        )
        .bind(user_id)
        .bind(c.instrument_id)
        .bind(&c.setup)
        .bind(SOURCE_DETECTOR)
        .bind(on)
        .bind(expires)
        .bind(c.trigger)
        .bind(c.stop_ref)
        .bind(c.date)
        .bind(WATCHING)
        .bind(c.score)
        .bind(c.adr_pct)
        .execute(&mut *conn)
        .await?;
        existing.insert(key);
        added += 1;
    }
    refresh_focus(conn, user_id, config).await?;
    Ok(AutoWatchResult { added, expired })
}

/// `refresh_focus`: every WATCHING EP and the top `focus_top_n` flags by score (no score ranks
/// at zero; ties to the lower id) carry `focus`; the rest do not. A row is updated only when its
/// flag changes. Returns the count.
pub(crate) async fn refresh_focus(
    conn: &mut PgConnection,
    user_id: i64,
    config: &SwingConfig,
) -> Result<i64, WorkerError> {
    let rows = sqlx::query(
        "SELECT id, setup, score, focus FROM sw_watch WHERE user_id = $1 AND state = $2",
    )
    .bind(user_id)
    .bind(WATCHING)
    .fetch_all(&mut *conn)
    .await?;
    let mut all = Vec::with_capacity(rows.len());
    for row in &rows {
        let id: i64 = row.try_get("id")?;
        let setup: String = row.try_get("setup")?;
        let score = baskfy_db::row::opt_decimal(row, "score")?.unwrap_or_default();
        let focus: bool = row.try_get("focus")?;
        all.push((id, setup, score, focus));
    }
    let mut flags: Vec<&(i64, String, Decimal, bool)> = all.iter().filter(|r| r.1 != EP).collect();
    flags.sort_by(|a, b| b.2.cmp(&a.2).then(a.0.cmp(&b.0)));
    let top = usize::try_from(config.watch.focus_top_n.max(0)).unwrap_or(0);
    let mut chosen: BTreeSet<i64> = all.iter().filter(|r| r.1 == EP).map(|r| r.0).collect();
    chosen.extend(flags.iter().take(top).map(|r| r.0));
    for (id, _, _, focus) in &all {
        let wanted = chosen.contains(id);
        if *focus != wanted {
            sqlx::query("UPDATE sw_watch SET focus = $1, updated_at = now() WHERE id = $2")
                .bind(wanted)
                .bind(id)
                .execute(&mut *conn)
                .await?;
        }
    }
    Ok(count(chosen.len()))
}

/// One `list_watch` row (the fields the evening reads).
#[derive(Debug, Clone)]
pub(super) struct WatchRow {
    pub(super) instrument_id: i64,
    pub(super) symbol: String,
    pub(super) setup: String,
    pub(super) trigger: Option<Decimal>,
    pub(super) stop_ref: Option<Decimal>,
    pub(super) catalyst: Option<String>,
    pub(super) score: Option<Decimal>,
    pub(super) adr_pct: Option<Decimal>,
}

/// `list_watch(state=WATCHING)`: the list, newest first (`added_on DESC, symbol`). The latest
/// close and the catalyst feed `list_watch` also joins are not read by the evening.
pub(super) async fn list_watch(
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<Vec<WatchRow>, WorkerError> {
    let rows = sqlx::query(
        "SELECT sw_watch.instrument_id, instrument.symbol, sw_watch.setup, sw_watch.trigger, \
         sw_watch.stop_ref, sw_watch.catalyst, sw_watch.score, sw_watch.adr_pct \
         FROM sw_watch JOIN instrument ON instrument.id = sw_watch.instrument_id \
         WHERE sw_watch.user_id = $1 AND sw_watch.state = $2 \
         ORDER BY sw_watch.added_on DESC, instrument.symbol",
    )
    .bind(user_id)
    .bind(WATCHING)
    .fetch_all(&mut *conn)
    .await?;
    let mut out = Vec::with_capacity(rows.len());
    for row in &rows {
        out.push(WatchRow {
            instrument_id: row.try_get("instrument_id")?,
            symbol: row.try_get("symbol")?,
            setup: row.try_get("setup")?,
            trigger: baskfy_db::row::opt_decimal(row, "trigger")?,
            stop_ref: baskfy_db::row::opt_decimal(row, "stop_ref")?,
            catalyst: row.try_get("catalyst")?,
            score: baskfy_db::row::opt_decimal(row, "score")?,
            adr_pct: baskfy_db::row::opt_decimal(row, "adr_pct")?,
        });
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_ep_stays_its_own_valid_bars_and_a_flag_the_watch_window() {
        let config = SwingConfig::default();
        assert_eq!(valid_bars(EP, &config), config.ep.valid_bars);
        assert_eq!(valid_bars(FLAG, &config), 10);
        assert_eq!(valid_bars("PARABOLIC_SHORT", &config), 10);
    }
}
