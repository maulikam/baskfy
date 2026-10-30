//! VB6 / VB7 — the evening and morning plans for the volume-breakout sleeve. Ports
//! `baskfy_worker.tasks.vbt_evening.run_vbt_evening` and what it calls that is not the book's
//! (`gate_for_session`, `candidates_for`, `entries_confirmed_today`, `store_plan`,
//! `settle_session`, `next_trading_day`, `last_detected_session`) and
//! `baskfy_api.vbt_settings.record_system_change`.
//!
//! Cancels first, then exits, then the naked stops, then the entries, then the session row.
//! **Nothing here places an order**: every line is `PROPOSED` until a person confirms it on the
//! desk (`02` Track C §3, DECISIONS-VB PACK.2) — there is no flag that changes that, and this
//! worker reads none. `store_plan` replaces the `(session, source)` plan, so a re-run rewrites
//! rather than duplicates (house rule 7).
//!
//! **The two counters** (`02` §3.1's twenty-session gate, `04` §5.4's countdown) move exactly as
//! Python's `settle_session` moves them: once per session (`counted_for_dry_run_gate`,
//! `first_live_counted`), the DRY_RUN count only when the session was rehearsed (a confirm, or no
//! executable line), each move audited in `vb_config_audit` by `vbt-evening`.

use std::collections::{BTreeMap, BTreeSet};

use baskfy_core::vbt::config::{Gate, SignalState};
use baskfy_core::vbt::plan::{
    BookState, Candidate, LineKind, PlanLine, VbtPlan, assemble, build_entries, exit_lines,
};
use baskfy_core::vbt::sizing::first_live_multiplier;
use baskfy_db::OrderedJson;
use baskfy_db::row::{date as row_date, opt_decimal};
use chrono::{DateTime, Duration, NaiveDate, Utc};
use rust_decimal::Decimal;
use serde::Serialize;
use sqlx::{PgConnection, Row as _};

use super::book::{
    bars_for_book, bars_since_entry_by_position, blank_session_counts, managed_actions,
    naked_positions, persist_trails, py_str, queue_exits, session_calendar, sweep_expired_orders,
};
use super::sleeve::{LIVE_ORDER_STATES, live_orders, load_sleeve, open_positions, symbols_for};
use crate::error::WorkerError;
use crate::pyjson::{int, s};
use crate::steps::sleeves::for_vbt_jobs::{NSE_EXCHANGE_ID, core_err, load_vbt_config};
use crate::steps::{StepOutcome, StepStatus};

/// `SOURCE_EVENING`.
pub(crate) const SOURCE_EVENING: &str = "EVENING";
/// `SOURCE_MORNING`.
pub(crate) const SOURCE_MORNING: &str = "MORNING";
/// `baskfy_core.models.vbt.VB_PLAN_TTL_MINUTES`.
pub(crate) const VB_PLAN_TTL_MINUTES: i64 = 30;
/// IST's offset, minutes.
pub(crate) const IST_MINUTES: i64 = 330;

pub(crate) fn iso(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

/// An enum's Python `.value` through its serde name.
pub(crate) fn enum_value<T: Serialize>(value: &T) -> Result<String, WorkerError> {
    match serde_json::to_value(value) {
        Ok(serde_json::Value::String(text)) => Ok(text),
        Ok(other) => Err(WorkerError::python(
            "ValueError",
            format!("not an enum value: {other}"),
        )),
        Err(e) => Err(WorkerError::python("ValueError", e.to_string())),
    }
}

/// `EveningReport`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct EveningReport {
    pub(crate) session: NaiveDate,
    pub(crate) source: String,
    pub(crate) gate: &'static str,
    pub(crate) plan_id: String,
    pub(crate) equity_inr: Decimal,
    pub(crate) entries: i64,
    pub(crate) sells: i64,
    pub(crate) cancels: i64,
    pub(crate) arms: i64,
    pub(crate) skips: i64,
    pub(crate) counted_dry_run: bool,
    pub(crate) counted_first_live: bool,
}

impl EveningReport {
    /// `as_detail()`, in its key order; the equity as `str(Decimal)`.
    pub(crate) fn as_detail(&self) -> Vec<(String, OrderedJson)> {
        vec![
            ("session".to_owned(), s(iso(self.session))),
            ("source".to_owned(), s(self.source.clone())),
            ("gate".to_owned(), s(self.gate)),
            ("plan_id".to_owned(), s(self.plan_id.clone())),
            ("equity_inr".to_owned(), s(py_str(self.equity_inr))),
            ("entries".to_owned(), int(self.entries)),
            ("sells".to_owned(), int(self.sells)),
            ("cancels".to_owned(), int(self.cancels)),
            ("arms".to_owned(), int(self.arms)),
            ("skips".to_owned(), int(self.skips)),
            (
                "counted_dry_run".to_owned(),
                OrderedJson::Bool(self.counted_dry_run),
            ),
            (
                "counted_first_live".to_owned(),
                OrderedJson::Bool(self.counted_first_live),
            ),
        ]
    }
}

fn len_i64(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

/// `gate_for_session`: the gate of the signal's own close; no row (or an empty one) is `SHUT`.
pub(crate) async fn gate_for_session(
    conn: &mut PgConnection,
    user_id: i64,
    as_of: NaiveDate,
) -> Result<Gate, WorkerError> {
    let value: Option<(Option<String>,)> =
        sqlx::query_as("SELECT gate FROM vb_breadth_daily WHERE user_id = $1 AND date = $2")
            .bind(user_id)
            .bind(as_of)
            .fetch_optional(&mut *conn)
            .await?;
    match value.and_then(|v| v.0).filter(|v| !v.is_empty()).as_deref() {
        None => Ok(Gate::Shut),
        Some("OPEN") => Ok(Gate::Open),
        Some("SHUT") => Ok(Gate::Shut),
        Some(other) => Err(WorkerError::python(
            "ValueError",
            format!("'{other}' is not a valid Gate"),
        )),
    }
}

/// `candidates_for`: the session's `SIGNAL` rows (never `SCAN_ONLY`), by `rank_key` descending,
/// each limited at its own stored `limit_price`.
pub(crate) async fn candidates_for(
    conn: &mut PgConnection,
    user_id: i64,
    as_of: NaiveDate,
) -> Result<Vec<Candidate>, WorkerError> {
    let rows = sqlx::query(
        "SELECT s.instrument_id, i.symbol, s.date, s.limit_price, \
         s.turnover_avg_20::numeric AS turnover_avg_20, s.rank_key, s.locked_upper_circuit \
         FROM vb_signal_daily s JOIN instrument i ON i.id = s.instrument_id \
         WHERE s.user_id = $1 AND s.date = $2 AND s.state = $3 ORDER BY s.rank_key DESC",
    )
    .bind(user_id)
    .bind(as_of)
    .bind(SignalState::Signal.value())
    .fetch_all(&mut *conn)
    .await?;
    rows.iter()
        .map(|row| {
            let limit = opt_decimal(row, "limit_price")?.ok_or_else(|| {
                WorkerError::python("InvalidOperation", "[<class 'decimal.ConversionSyntax'>]")
            })?;
            let rank: Option<i64> = row.try_get("rank_key")?;
            let locked: Option<bool> = row.try_get("locked_upper_circuit")?;
            Ok(Candidate {
                instrument_id: row.try_get("instrument_id")?,
                symbol: row.try_get("symbol")?,
                signal_date: row_date(row, "date")?,
                close_raw: limit,
                turnover_avg_inr: opt_decimal(row, "turnover_avg_20")?,
                rank_key: rank.unwrap_or(0),
                locked_upper_circuit: locked.unwrap_or(false),
            })
        })
        .collect()
}

/// `entries_confirmed_today(on)`: orders created on the execution day (IST) that a person has
/// confirmed or the desk has sent — any plan's (LV10.2). `None` is zero.
pub(crate) async fn entries_confirmed_today(
    conn: &mut PgConnection,
    user_id: i64,
    on: Option<NaiveDate>,
) -> Result<i64, WorkerError> {
    let Some(on) = on else {
        return Ok(0);
    };
    let (n,): (i64,) = sqlx::query_as(
        "SELECT count(*) FROM vb_order WHERE user_id = $1 \
         AND date(timezone('Asia/Kolkata', created_at)) = $2 AND state = ANY($3)",
    )
    .bind(user_id)
    .bind(on)
    .bind(&["CONFIRMED", "SENT", "FILLED"][..])
    .fetch_one(&mut *conn)
    .await?;
    Ok(n)
}

/// The `vb_config` row's counters, as `select(VbConfig)` reads them.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct ConfigRow {
    pub(crate) max_open_positions: i64,
    pub(crate) first_live_sessions_left: i64,
    pub(crate) dry_run_sessions: i64,
}

/// `select(VbConfig).where(user_id == ...)`.
pub(crate) async fn config_row(
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<Option<ConfigRow>, WorkerError> {
    let row: Option<(i64, i64, i64)> = sqlx::query_as(
        "SELECT max_open_positions::bigint, first_live_sessions_left::bigint, \
         dry_run_sessions::bigint FROM vb_config WHERE user_id = $1",
    )
    .bind(user_id)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(row.map(|r| ConfigRow {
        max_open_positions: r.0,
        first_live_sessions_left: r.1,
        dry_run_sessions: r.2,
    }))
}

async fn uuid4(conn: &mut PgConnection) -> Result<String, WorkerError> {
    let (id,): (String,) = sqlx::query_as("SELECT gen_random_uuid()::text")
        .fetch_one(&mut *conn)
        .await?;
    Ok(id)
}

/// `store_plan`: replace this `(session, source)`'s plan (a fresh uuid4 `plan_id`); its lines
/// `PROPOSED`, `client_id = "{plan_id}:{symbol}:{kind}"`, and its skips.
pub(crate) async fn store_plan(
    conn: &mut PgConnection,
    user_id: i64,
    as_of: NaiveDate,
    source: &str,
    plan: &VbtPlan,
    now: DateTime<Utc>,
) -> Result<String, WorkerError> {
    let existing: Vec<(i64,)> = sqlx::query_as(
        "SELECT id FROM vb_plan WHERE user_id = $1 AND session_date = $2 AND source = $3",
    )
    .bind(user_id)
    .bind(as_of)
    .bind(source)
    .fetch_all(&mut *conn)
    .await?;
    if !existing.is_empty() {
        let ids: Vec<i64> = existing.into_iter().map(|r| r.0).collect();
        sqlx::query("DELETE FROM vb_plan WHERE id = ANY($1)")
            .bind(&ids)
            .execute(&mut *conn)
            .await?;
    }
    let plan_id = uuid4(conn).await?;
    let (row_id,): (i64,) = sqlx::query_as(
        "INSERT INTO vb_plan (plan_id, user_id, session_date, source, built_at, expires_at, \
         plan_hash, gate, sleeve_equity_inr, total_new_exposure_inr) \
         VALUES (CAST($1 AS uuid), $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id",
    )
    .bind(&plan_id)
    .bind(user_id)
    .bind(as_of)
    .bind(source)
    .bind(now)
    .bind(now + Duration::minutes(VB_PLAN_TTL_MINUTES))
    .bind(plan.plan_hash())
    .bind(plan.gate.value())
    .bind(plan.sleeve_equity_inr)
    .bind(plan.total_new_exposure_inr().map_err(core_err)?)
    .fetch_one(&mut *conn)
    .await?;
    for line in &plan.lines {
        let quantity = i32::try_from(line.quantity)
            .map_err(|_| WorkerError::python("DataError", "value out of range for type integer"))?;
        let cap = line.cap.as_ref().map(enum_value).transpose()?;
        let reason = line.reason.map(|r| r.value());
        sqlx::query(
            "INSERT INTO vb_plan_line (plan_id, user_id, instrument_id, kind, state, quantity, \
             limit_price, stop_price, value_inr, size_cap, reason, note, position_id, client_id) \
             VALUES ($1, $2, $3, $4, 'PROPOSED', $5, $6, $7, $8, $9, $10, $11, $12, $13)",
        )
        .bind(row_id)
        .bind(user_id)
        .bind(line.instrument_id)
        .bind(line.kind.value())
        .bind(quantity)
        .bind(line.limit_price)
        .bind(line.stop_price)
        .bind(line.value_inr)
        .bind(cap)
        .bind(reason)
        .bind(&line.note)
        .bind(line.position_id)
        .bind(format!("{plan_id}:{}:{}", line.symbol, line.kind.value()))
        .execute(&mut *conn)
        .await?;
    }
    for skip in &plan.skips {
        sqlx::query(
            "INSERT INTO vb_plan_skip (plan_id, user_id, instrument_id, reason, detail) \
             VALUES ($1, $2, $3, $4, $5)",
        )
        .bind(row_id)
        .bind(user_id)
        .bind(skip.instrument_id)
        .bind(enum_value(&skip.reason)?)
        .bind(&skip.detail)
        .execute(&mut *conn)
        .await?;
    }
    Ok(plan_id)
}

/// `vbt_settings.record_system_change`: one job-owned field (`dry_run_sessions`,
/// `first_live_sessions_left`) with its audit row; nothing when the value is already there.
async fn record_system_change(
    conn: &mut PgConnection,
    user_id: i64,
    field: &str,
    value: i64,
    now: DateTime<Utc>,
    note: &str,
) -> Result<(), WorkerError> {
    const CHANGED_BY: &str = "vbt-evening";
    let column = match field {
        "dry_run_sessions" => "dry_run_sessions",
        "first_live_sessions_left" => "first_live_sessions_left",
        other => {
            return Err(WorkerError::python(
                "ValueError",
                format!(
                    "'{other}' is not a system-owned field; the editable ones go through \
                     apply_patch (dry_run_sessions, first_live_sessions_left)"
                ),
            ));
        }
    };
    let before: Option<(i64,)> = sqlx::query_as(&format!(
        "SELECT {column}::bigint FROM vb_config WHERE user_id = $1"
    ))
    .bind(user_id)
    .fetch_optional(&mut *conn)
    .await?;
    let Some((before,)) = before else {
        return Err(WorkerError::python(
            "VbtConfigNotSeeded",
            format!("no vb_config row for user {user_id}; run `make seed` (or `baskfy-seed vbt`)"),
        ));
    };
    if before == value {
        return Ok(());
    }
    sqlx::query(&format!(
        "UPDATE vb_config SET {column} = $2, updated_by = $3, updated_at = now() \
         WHERE user_id = $1"
    ))
    .bind(user_id)
    .bind(value)
    .bind(CHANGED_BY)
    .execute(&mut *conn)
    .await?;
    sqlx::query(
        "INSERT INTO vb_config_audit (user_id, key, old_value, new_value, changed_at, \
         changed_by, note) VALUES ($1, $2, $3, $4, $5, $6, $7)",
    )
    .bind(user_id)
    .bind(field)
    .bind(before.to_string())
    .bind(value.to_string())
    .bind(now)
    .bind(CHANGED_BY)
    .bind(note)
    .execute(&mut *conn)
    .await?;
    Ok(())
}

/// `settle_session`: the `vb_session` row and the two counters it moves. Returns
/// `(counted_dry_run, counted_first_live)`.
///
/// Python's own merge is kept: the plan id goes under `"evening"` whichever source built it.
#[allow(clippy::too_many_arguments)] // one keyword per input the row records, as Python's
async fn settle_session(
    conn: &mut PgConnection,
    user_id: i64,
    as_of: NaiveDate,
    gate: Gate,
    signals: i64,
    plan_id: &str,
    executable_lines: i64,
    execution_enabled: bool,
    now: DateTime<Utc>,
) -> Result<(bool, bool), WorkerError> {
    let existing = sqlx::query(
        "SELECT plan_ids::text AS plan_ids, confirms::bigint AS confirms, \
         counted_for_dry_run_gate, first_live_counted FROM vb_session \
         WHERE user_id = $1 AND session_date = $2",
    )
    .bind(user_id)
    .bind(as_of)
    .fetch_optional(&mut *conn)
    .await?;
    let mode = if execution_enabled { "LIVE" } else { "DRY_RUN" };
    let signals32 = i32::try_from(signals)
        .map_err(|_| WorkerError::python("DataError", "value out of range for type integer"))?;
    let (confirms, counted_dry, counted_live) = match &existing {
        Some(row) => (
            row.try_get::<i64, _>("confirms")?,
            row.try_get::<bool, _>("counted_for_dry_run_gate")?,
            row.try_get::<bool, _>("first_live_counted")?,
        ),
        None => (0, false, false),
    };
    let old_ids = match &existing {
        Some(row) => row
            .try_get::<Option<String>, _>("plan_ids")?
            .map(|text| OrderedJson::parse(&text))
            .transpose()
            .map_err(|e| WorkerError::python("ValueError", e.to_string()))?,
        None => None,
    };
    let mut ids = match old_ids {
        Some(OrderedJson::Object(entries)) => entries,
        _ => Vec::new(),
    };
    crate::pyjson::update(&mut ids, "evening", s(plan_id));
    let plan_ids = OrderedJson::Object(ids)
        .to_python_json()
        .map_err(|e| WorkerError::python("ValueError", e.to_string()))?;
    if existing.is_none() {
        sqlx::query(
            "INSERT INTO vb_session (user_id, session_date, mode, gate, signals, plan_ids, \
             notes) VALUES ($1, $2, $3, $4, $5, CAST($6 AS JSONB), NULL)",
        )
        .bind(user_id)
        .bind(as_of)
        .bind(mode)
        .bind(gate.value())
        .bind(signals32)
        .bind(&plan_ids)
        .execute(&mut *conn)
        .await?;
    } else {
        // The plan id always changes, so the ORM always writes the row and its stamp.
        sqlx::query(
            "UPDATE vb_session SET mode = $3, gate = $4, signals = $5, \
             plan_ids = CAST($6 AS JSONB), notes = NULL, updated_at = now() \
             WHERE user_id = $1 AND session_date = $2",
        )
        .bind(user_id)
        .bind(as_of)
        .bind(mode)
        .bind(gate.value())
        .bind(signals32)
        .bind(&plan_ids)
        .execute(&mut *conn)
        .await?;
    }

    let Some(cfg) = config_row(conn, user_id).await? else {
        return Ok((false, false));
    };
    let mut counted_dry_run = false;
    let mut counted_first_live = false;
    if mode == "DRY_RUN" && !counted_dry {
        let rehearsed = confirms > 0 || executable_lines == 0;
        if rehearsed {
            sqlx::query(
                "UPDATE vb_session SET counted_for_dry_run_gate = true, updated_at = now() \
                 WHERE user_id = $1 AND session_date = $2",
            )
            .bind(user_id)
            .bind(as_of)
            .execute(&mut *conn)
            .await?;
            record_system_change(
                conn,
                user_id,
                "dry_run_sessions",
                cfg.dry_run_sessions + 1,
                now,
                &format!("{} closed in DRY_RUN", iso(as_of)),
            )
            .await?;
            counted_dry_run = true;
        }
    }
    if mode == "LIVE" && !counted_live && cfg.first_live_sessions_left > 0 {
        sqlx::query(
            "UPDATE vb_session SET first_live_counted = true, updated_at = now() \
             WHERE user_id = $1 AND session_date = $2",
        )
        .bind(user_id)
        .bind(as_of)
        .execute(&mut *conn)
        .await?;
        record_system_change(
            conn,
            user_id,
            "first_live_sessions_left",
            cfg.first_live_sessions_left - 1,
            now,
            &format!("{} closed LIVE", iso(as_of)),
        )
        .await?;
        counted_first_live = true;
    }
    Ok((counted_dry_run, counted_first_live))
}

/// `next_trading_day(after)`: the exchange's next session (the sleeve's own calendar only knows
/// sessions that have happened).
pub(crate) async fn next_trading_day(
    conn: &mut PgConnection,
    after: NaiveDate,
) -> Result<Option<NaiveDate>, WorkerError> {
    let row: Option<(NaiveDate,)> = sqlx::query_as(
        "SELECT date FROM trading_day WHERE exchange_id = $1 AND is_trading_day IS true \
         AND date > $2 ORDER BY date LIMIT 1",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(after)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(row.map(|r| r.0))
}

/// `last_detected_session(on_or_before)`: the newest **tradeable** breadth row's date.
pub(crate) async fn last_detected_session(
    conn: &mut PgConnection,
    user_id: i64,
    on_or_before: NaiveDate,
) -> Result<Option<NaiveDate>, WorkerError> {
    let row: Option<(NaiveDate,)> = sqlx::query_as(
        "SELECT date FROM vb_breadth_daily WHERE user_id = $1 AND date <= $2 \
         AND thin_session IS false ORDER BY date DESC LIMIT 1",
    )
    .bind(user_id)
    .bind(on_or_before)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(row.map(|r| r.0))
}

fn count(lines: &[PlanLine], kind: LineKind) -> i64 {
    len_i64(lines.iter().filter(|l| l.kind == kind).count())
}

/// What one evening (or morning) depends on.
pub(crate) struct EveningInputs<'a> {
    pub(crate) trade_date: NaiveDate,
    pub(crate) user_id: i64,
    pub(crate) execution_enabled: bool,
    pub(crate) source: &'a str,
    pub(crate) now: DateTime<Utc>,
}

/// The book's slot-holding orders after the sweep (`state in LIVE_STATES`).
fn working_ids(orders: &[super::sleeve::OrderRow]) -> BTreeSet<i64> {
    orders
        .iter()
        .filter(|o| LIVE_ORDER_STATES.contains(&o.state.as_str()))
        .map(|o| o.instrument_id)
        .collect()
}

/// `run_vbt_evening`: build the session's plan and settle its row; `None` (the outcome says why)
/// when the date is not a session this sleeve traded.
pub(crate) async fn run_vbt_evening(
    conn: &mut PgConnection,
    outcome: &mut StepOutcome,
    inputs: &EveningInputs<'_>,
) -> Result<Option<EveningReport>, WorkerError> {
    let EveningInputs {
        trade_date,
        user_id,
        execution_enabled,
        source,
        now: stamp,
    } = *inputs;
    let config = load_vbt_config(conn, user_id).await?;
    let calendar = session_calendar(conn, user_id, trade_date).await?;
    if !calendar.sessions.contains(&trade_date) {
        outcome.status = StepStatus::Skipped;
        outcome.note(
            "skipped_reason",
            s(format!(
                "{} is not a session this sleeve traded",
                iso(trade_date)
            )),
        );
        return Ok(None);
    }

    let mut positions = open_positions(conn, user_id).await?;
    let mut orders = live_orders(conn, user_id).await?;
    let ids: BTreeSet<i64> = positions
        .iter()
        .map(|p| p.instrument_id)
        .chain(orders.iter().map(|o| o.instrument_id))
        .collect();
    let symbols = symbols_for(conn, &ids.into_iter().collect::<Vec<_>>()).await?;
    let (cancels, refreshed) =
        sweep_expired_orders(conn, &mut orders, trade_date, &calendar, &config, &symbols).await?;
    let held: Vec<i64> = positions.iter().map(|p| p.instrument_id).collect();
    let bars = bars_for_book(conn, &held, trade_date, &config).await?;
    let blanks = blank_session_counts(conn, &positions, trade_date, &calendar).await?;
    let since_entry = bars_since_entry_by_position(conn, &positions, trade_date).await?;
    let actions = managed_actions(&positions, &bars, &symbols, &config, &blanks, &since_entry)?;
    persist_trails(conn, &mut positions, &bars, &config).await?;

    let sleeve = load_sleeve(conn, user_id, trade_date)
        .await?
        .quantize()
        .map_err(core_err)?;
    let gate = gate_for_session(conn, user_id, trade_date).await?;
    let candidates = candidates_for(conn, user_id, trade_date).await?;
    let cfg = config_row(conn, user_id).await?;
    let multiplier = first_live_multiplier(
        cfg.map_or(0, |c| c.first_live_sessions_left),
        execution_enabled,
        &config.sizing,
    )
    .map_err(core_err)?;
    // LV10.2: the day the plan executes — the next session for the EVENING plan, today (IST) for
    // the MORNING rebuild.
    let executes_on = if source == SOURCE_EVENING {
        match calendar.advance(trade_date, 1).map_err(core_err)? {
            Some(day) => Some(day),
            None => next_trading_day(conn, trade_date).await?,
        }
    } else {
        Some((stamp + Duration::minutes(IST_MINUTES)).date_naive())
    };
    let mut open_entry_counts: BTreeMap<i64, i64> = BTreeMap::new();
    for p in &positions {
        *open_entry_counts.entry(p.instrument_id).or_insert(0) += 1;
    }
    let book = BookState {
        open_instrument_ids: positions.iter().map(|p| p.instrument_id).collect(),
        working_instrument_ids: working_ids(&orders),
        open_exposure_inr: sleeve.open_exposure_inr().map_err(core_err)?,
        cash_available_inr: sleeve.cash_available_inr().map_err(core_err)?,
        entries_already_this_session: entries_confirmed_today(conn, user_id, executes_on).await?,
        positions_naked_of_gtt: naked_positions(&positions, &symbols),
        open_entry_counts,
    };
    let equity = sleeve.equity_inr().map_err(core_err)?;
    let (entries, skips) = build_entries(
        &candidates,
        gate,
        equity,
        &book,
        &config,
        multiplier,
        cfg.map(|c| c.max_open_positions),
        None,
    )
    .map_err(core_err)?;
    let exits = exit_lines(&actions, &cancels, &book);
    let plan = assemble(trade_date, gate, equity, &exits, &entries, &skips, source);
    let plan_id = store_plan(conn, user_id, trade_date, source, &plan, stamp).await?;
    let next_session = match calendar.advance(trade_date, 1).map_err(core_err)? {
        Some(day) => Some(day),
        None => next_trading_day(conn, trade_date).await?,
    };
    queue_exits(conn, &mut positions, &actions, next_session).await?;

    let executable = len_i64(
        plan.lines
            .iter()
            .filter(|l| l.kind != LineKind::CancelLimit)
            .count(),
    );
    let (counted_dry, counted_live) = settle_session(
        conn,
        user_id,
        trade_date,
        gate,
        len_i64(candidates.len()),
        &plan_id,
        executable,
        execution_enabled,
        stamp,
    )
    .await?;
    let report = EveningReport {
        session: trade_date,
        source: source.to_owned(),
        gate: gate.value(),
        plan_id,
        equity_inr: equity,
        entries: count(&plan.lines, LineKind::PlaceLimit),
        sells: count(&plan.lines, LineKind::SellAtOpen),
        cancels: count(&plan.lines, LineKind::CancelLimit),
        arms: count(&plan.lines, LineKind::ArmGtt),
        skips: len_i64(plan.skips.len()),
        counted_dry_run: counted_dry,
        counted_first_live: counted_live,
    };
    outcome.rows_out = len_i64(plan.lines.len());
    outcome.note("orders_refreshed", int(refreshed));
    for (k, v) in report.as_detail() {
        outcome.note(&k, v);
    }
    Ok(Some(report))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::panic)]
    use super::*;
    use baskfy_core::vbt::plan::SkipReason;
    use baskfy_core::vbt::sizing::SizeCap;

    #[test]
    fn the_report_keeps_pythons_keys_and_equity_as_str() {
        let report = EveningReport {
            session: NaiveDate::from_ymd_opt(2026, 10, 29).unwrap_or_default(),
            source: SOURCE_EVENING.to_owned(),
            gate: "SHUT",
            plan_id: "p".to_owned(),
            equity_inr: Decimal::new(250_000_000, 2),
            entries: 0,
            sells: 1,
            cancels: 2,
            arms: 3,
            skips: 4,
            counted_dry_run: true,
            counted_first_live: false,
        };
        let detail = report.as_detail();
        let keys: Vec<&str> = detail.iter().map(|(k, _)| k.as_str()).collect();
        assert_eq!(
            keys,
            [
                "session",
                "source",
                "gate",
                "plan_id",
                "equity_inr",
                "entries",
                "sells",
                "cancels",
                "arms",
                "skips",
                "counted_dry_run",
                "counted_first_live"
            ]
        );
        assert_eq!(detail[4].1, s("2500000.00"));
        assert_eq!(detail[0].1, s("2026-10-29"));
    }

    #[test]
    fn the_enum_values_are_the_schemas() {
        assert_eq!(
            enum_value(&SkipReason::GateShut).unwrap_or_default(),
            "GATE_SHUT"
        );
        assert_eq!(
            enum_value(&SkipReason::ExposureFull).unwrap_or_default(),
            "EXPOSURE_FULL"
        );
        assert_eq!(
            enum_value(&SizeCap::PositionPct).unwrap_or_default(),
            "POSITION_PCT"
        );
    }

    #[test]
    fn a_working_order_holds_a_slot_and_an_expired_one_does_not() {
        // test_vbt_evening.py::test_a_working_order_holds_a_slot (the book's half).
        let row = |state: &str, id: i64| super::super::sleeve::OrderRow {
            id,
            instrument_id: id,
            signal_date: NaiveDate::from_ymd_opt(2026, 9, 1).unwrap_or_default(),
            limit_price: Decimal::ONE,
            stop_price: Decimal::ONE,
            quantity: 1,
            state: state.to_owned(),
            sessions_worked: 0,
            expires_after_session: None,
            filled_quantity: 0,
        };
        let ids = working_ids(&[row("SENT", 1), row("EXPIRED", 2), row("PARTIAL", 3)]);
        assert_eq!(ids.into_iter().collect::<Vec<_>>(), vec![1, 3]);
    }
}
