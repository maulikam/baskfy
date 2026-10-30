//! TW6 — the evening and morning plans (`docs/twt/04` §§10, 11). Ports
//! `baskfy_worker.tasks.twt_evening`: exits first, then the entries, then the session row, then
//! the digest. **Nothing here places an order**: every line is `PROPOSED` until a person (or the
//! desk's own flagged `twt-auto`, TW17 — never this worker) confirms it.
//!
//! `MORNING` is the identical body against the identical session, re-sized and not re-detected
//! (`04` §11.3). `store_plan` replaces the `(session, source)` plan, so a re-run rewrites rather
//! than duplicates (house rule 7) — with a fresh uuid4 `plan_id`, as Python's.
//!
//! **`dry_run_sessions` is not moved here, deliberately** (Python's `settle_session` docstring:
//! `02` §3 makes it information, not a gate), and neither are the desk's counters (`confirms`,
//! `fills`, `ratchets`, `exits`, `naked_at_1515`, `first_live_entries_counted`): a rebuild never
//! resets what the desk has counted.

use baskfy_api::Settings;
use baskfy_core::decimal::WideDecimal;
use baskfy_core::twt::config::{Gate, SignalState};
use baskfy_core::twt::plan::{
    Candidate, LineKind, PlanLine, SkipReason, TwtPlan, assemble, build_entries, client_id_for,
    exit_lines,
};
use baskfy_db::OrderedJson;
use baskfy_db::row::{date as row_date, decimal as row_decimal, opt_decimal};
use chrono::{DateTime, Duration, NaiveDate, Utc};
use rust_decimal::Decimal;
use sqlx::{PgConnection, Row as _};

use super::alerting::{TwtAlert, dispatch};
use super::sleeve::{
    LoadedSleeve, book_state, config_row, evening_config, load_sleeve, slot_multiplier,
};
use crate::error::WorkerError;
use crate::pyjson::{int, object, s};
use crate::steps::sleeves::for_twt_jobs::{NSE_EXCHANGE_ID, core_err};
use crate::steps::{StepOutcome, StepStatus};

/// `SOURCE_EVENING`.
pub(crate) const SOURCE_EVENING: &str = "EVENING";
/// `SOURCE_MORNING`.
pub(crate) const SOURCE_MORNING: &str = "MORNING";
/// `baskfy_core.models.twt.TW_PLAN_TTL_MINUTES`: a plan expires thirty minutes after it is built.
pub(crate) const TW_PLAN_TTL_MINUTES: i64 = 30;
/// IST's offset, minutes.
const IST_MINUTES: i64 = 330;

/// `str(Decimal)`.
pub(crate) fn py_str(d: Decimal) -> String {
    WideDecimal::from(d).to_sci_string()
}

fn iso(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

/// `SkipReason.value`.
pub(crate) fn skip_value(reason: SkipReason) -> &'static str {
    match reason {
        SkipReason::GateShut => "GATE_SHUT",
        SkipReason::AlreadyHeld => "ALREADY_HELD",
        SkipReason::BelowLiquidityFloor => "BELOW_LIQUIDITY_FLOOR",
        SkipReason::NoBar => "NO_BAR",
        SkipReason::LockedUpperCircuit => "LOCKED_UPPER_CIRCUIT",
        SkipReason::SessionCap => "SESSION_CAP",
        SkipReason::SlotsFull => "SLOTS_FULL",
        SkipReason::NoSleeveCapital => "NO_SLEEVE_CAPITAL",
        SkipReason::StopNotBelowEntry => "STOP_NOT_BELOW_ENTRY",
        SkipReason::BelowMinTradeValue => "BELOW_MIN_TRADE_VALUE",
        SkipReason::ExposureFull => "EXPOSURE_FULL",
        SkipReason::TurnoverCap => "TURNOVER_CAP",
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
    pub(crate) arms: i64,
    pub(crate) ratchets: i64,
    pub(crate) skips: i64,
    pub(crate) adjustments: i64,
    pub(crate) naked: i64,
}

impl EveningReport {
    /// `as_detail()`.
    pub(crate) fn as_detail(&self) -> OrderedJson {
        object([
            ("session", s(iso(self.session))),
            ("source", s(self.source.clone())),
            ("gate", s(self.gate)),
            ("plan_id", s(self.plan_id.clone())),
            ("equity_inr", s(py_str(self.equity_inr))),
            ("entries", int(self.entries)),
            ("arms", int(self.arms)),
            ("ratchets", int(self.ratchets)),
            ("skips", int(self.skips)),
            ("adjustments", int(self.adjustments)),
            ("naked", int(self.naked)),
        ])
    }
}

fn len_i64(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

/// `gate_for_session`: the gate of the signal's own close; no breadth row reads `SHUT`.
pub(crate) async fn gate_for_session(
    conn: &mut PgConnection,
    user_id: i64,
    as_of: NaiveDate,
) -> Result<Gate, WorkerError> {
    let value: Option<(Option<String>,)> =
        sqlx::query_as("SELECT gate FROM tw_breadth_daily WHERE user_id = $1 AND date = $2")
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

/// `candidates_for`: the session's entry events — `SCAN_ONLY` included (TW6.1) — by
/// `rank_key` descending, then symbol; both prices the signal's `entry_reference_close`.
pub(crate) async fn candidates_for(
    conn: &mut PgConnection,
    user_id: i64,
    as_of: NaiveDate,
) -> Result<Vec<Candidate>, WorkerError> {
    let rows = sqlx::query(
        "SELECT s.instrument_id, i.symbol, s.date, s.rank_key::numeric AS rank_key, \
         s.turnover_avg_20::numeric AS turnover_avg_20, s.entry_reference_close \
         FROM tw_signal_daily s JOIN instrument i ON i.id = s.instrument_id \
         WHERE s.user_id = $1 AND s.date = $2 ORDER BY s.rank_key DESC, i.symbol",
    )
    .bind(user_id)
    .bind(as_of)
    .fetch_all(&mut *conn)
    .await?;
    rows.iter()
        .map(|row| {
            // `Decimal(str(row.rank_key or 0))`: a null or zero key is `Decimal("0")`.
            let rank = opt_decimal(row, "rank_key")?.unwrap_or(Decimal::ZERO);
            let reference = opt_decimal(row, "entry_reference_close")?.ok_or_else(|| {
                WorkerError::python("InvalidOperation", "[<class 'decimal.ConversionSyntax'>]")
            })?;
            Ok(Candidate {
                instrument_id: row.try_get("instrument_id")?,
                symbol: row.try_get("symbol")?,
                signal_date: row_date(row, "date")?,
                rank_key: rank,
                turnover_avg_inr: opt_decimal(row, "turnover_avg_20")?,
                entry_price: reference,
                stop_reference_price: reference,
                entry_bar: None,
            })
        })
        .collect()
}

/// One re-adjusted position: `(position id, symbol, entry factor, as-of factor)`.
pub(crate) type Adjusted = std::collections::BTreeMap<i64, (i64, String, Decimal, Decimal)>;

/// `adjusted_positions`: open positions whose as-of `adj_factor` differs from the entry's.
async fn adjusted_positions(
    conn: &mut PgConnection,
    user_id: i64,
    as_of: NaiveDate,
) -> Result<Adjusted, WorkerError> {
    let rows = sqlx::query(
        "SELECT p.id, p.instrument_id, i.symbol, p.entry_adj_factor, o.adj_factor \
         FROM tw_position p JOIN instrument i ON i.id = p.instrument_id \
         JOIN ohlcv_daily o ON o.instrument_id = p.instrument_id AND o.date = $2 \
         WHERE p.user_id = $1 AND p.state = 'OPEN' AND p.quantity_open > 0",
    )
    .bind(user_id)
    .bind(as_of)
    .fetch_all(&mut *conn)
    .await?;
    let mut out = Adjusted::new();
    for row in &rows {
        let entry = row_decimal(row, "entry_adj_factor")?;
        let now = row_decimal(row, "adj_factor")?;
        if entry != now {
            out.insert(
                row.try_get("instrument_id")?,
                (row.try_get("id")?, row.try_get("symbol")?, entry, now),
            );
        }
    }
    Ok(out)
}

/// `annotate_adjustments`: a `RAISE_GTT_STOP` on a re-adjusted name carries the reason.
fn annotate_adjustments(lines: Vec<PlanLine>, adjusted: &Adjusted) -> Vec<PlanLine> {
    lines
        .into_iter()
        .map(|mut line| {
            if let Some((_, _, entry, now)) = adjusted.get(&line.instrument_id)
                && line.kind == LineKind::RaiseGttStop
            {
                line.note = format!(
                    "a corporate action re-adjusted this name under the hold ({} -> {}); the \
                     level was re-derived and the resting trigger is a pre-adjustment price — \
                     check it at the broker",
                    py_str(*entry),
                    py_str(*now)
                );
            }
            line
        })
        .collect()
}

/// A fresh `uuid.uuid4()` (Postgres's `gen_random_uuid()` is a version-4 uuid).
async fn uuid4(conn: &mut PgConnection) -> Result<String, WorkerError> {
    let (id,): (String,) = sqlx::query_as("SELECT gen_random_uuid()::text")
        .fetch_one(&mut *conn)
        .await?;
    Ok(id)
}

/// `store_plan`: replace this `(session, source)`'s plan; the lines and skips in plan order.
pub(crate) async fn store_plan(
    conn: &mut PgConnection,
    user_id: i64,
    as_of: NaiveDate,
    source: &str,
    plan: &TwtPlan,
    now: DateTime<Utc>,
) -> Result<String, WorkerError> {
    let existing: Vec<(i64,)> = sqlx::query_as(
        "SELECT id FROM tw_plan WHERE user_id = $1 AND session_date = $2 AND source = $3",
    )
    .bind(user_id)
    .bind(as_of)
    .bind(source)
    .fetch_all(&mut *conn)
    .await?;
    if !existing.is_empty() {
        let ids: Vec<i64> = existing.into_iter().map(|r| r.0).collect();
        sqlx::query("DELETE FROM tw_plan WHERE id = ANY($1)")
            .bind(&ids)
            .execute(&mut *conn)
            .await?;
    }
    let plan_id = uuid4(conn).await?;
    let (row_id,): (i64,) = sqlx::query_as(
        "INSERT INTO tw_plan (plan_id, user_id, session_date, source, built_at, expires_at, \
         plan_hash, gate, sleeve_equity_inr, total_new_exposure_inr) \
         VALUES (CAST($1 AS uuid), $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id",
    )
    .bind(&plan_id)
    .bind(user_id)
    .bind(as_of)
    .bind(source)
    .bind(now)
    .bind(now + Duration::minutes(TW_PLAN_TTL_MINUTES))
    .bind(plan.plan_hash())
    .bind(plan.gate.value())
    .bind(plan.sleeve_equity_inr)
    .bind(plan.total_new_exposure_inr().map_err(core_err)?)
    .fetch_one(&mut *conn)
    .await?;
    for line in &plan.lines {
        let quantity = i32::try_from(line.quantity)
            .map_err(|_| WorkerError::python("DataError", "value out of range for type integer"))?;
        sqlx::query(
            "INSERT INTO tw_plan_line (plan_id, user_id, instrument_id, kind, state, quantity, \
             stop_price, value_inr, high_since, previous_trigger, note, position_id, client_id) \
             VALUES ($1, $2, $3, $4, 'PROPOSED', $5, $6, $7, $8, $9, $10, $11, $12)",
        )
        .bind(row_id)
        .bind(user_id)
        .bind(line.instrument_id)
        .bind(line.kind.value())
        .bind(quantity)
        .bind(line.stop_price)
        .bind(line.value_inr)
        .bind(line.high_since)
        .bind(line.previous_stop)
        .bind(&line.note)
        .bind(line.position_id)
        .bind(client_id_for(&plan_id, &line.symbol, line.kind))
        .execute(&mut *conn)
        .await?;
    }
    for skip in &plan.skips {
        sqlx::query(
            "INSERT INTO tw_plan_skip (plan_id, user_id, instrument_id, reason, detail) \
             VALUES ($1, $2, $3, $4, $5)",
        )
        .bind(row_id)
        .bind(user_id)
        .bind(skip.instrument_id)
        .bind(skip_value(skip.reason))
        .bind(&skip.detail)
        .execute(&mut *conn)
        .await?;
    }
    Ok(plan_id)
}

/// `settle_session`: the `tw_session` row's mode, gate, the detector's two counts and the plan
/// id under `source.lower()`. Nothing the desk counts is touched; neither is `dry_run_sessions`.
async fn settle_session(
    conn: &mut PgConnection,
    user_id: i64,
    as_of: NaiveDate,
    gate: Gate,
    plan_id: &str,
    source: &str,
    execution_enabled: bool,
) -> Result<(), WorkerError> {
    let existing = sqlx::query(
        "SELECT mode, gate, states::bigint AS states, signals::bigint AS signals, \
         plan_ids::text AS plan_ids FROM tw_session WHERE user_id = $1 AND session_date = $2",
    )
    .bind(user_id)
    .bind(as_of)
    .fetch_optional(&mut *conn)
    .await?;
    let (states,): (i64,) =
        sqlx::query_as("SELECT count(*) FROM tw_state_daily WHERE user_id = $1 AND date = $2")
            .bind(user_id)
            .bind(as_of)
            .fetch_one(&mut *conn)
            .await?;
    let (signals,): (i64,) = sqlx::query_as(
        "SELECT count(*) FROM tw_signal_daily WHERE user_id = $1 AND date = $2 AND state = $3",
    )
    .bind(user_id)
    .bind(as_of)
    .bind(SignalState::Signal.value())
    .fetch_one(&mut *conn)
    .await?;
    let mode = if execution_enabled { "LIVE" } else { "DRY_RUN" };
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
    crate::pyjson::update(&mut ids, &source.to_lowercase(), s(plan_id));
    let plan_ids = OrderedJson::Object(ids)
        .to_python_json()
        .map_err(|e| WorkerError::python("ValueError", e.to_string()))?;
    let states32 = i32::try_from(states).unwrap_or(i32::MAX);
    let signals32 = i32::try_from(signals).unwrap_or(i32::MAX);
    match existing {
        None => {
            sqlx::query(
                "INSERT INTO tw_session (user_id, session_date, mode, gate, states, signals, \
                 plan_ids) VALUES ($1, $2, $3, $4, $5, $6, CAST($7 AS JSONB))",
            )
            .bind(user_id)
            .bind(as_of)
            .bind(mode)
            .bind(gate.value())
            .bind(states32)
            .bind(signals32)
            .bind(plan_ids)
            .execute(&mut *conn)
            .await?;
        }
        Some(_) => {
            // The ORM writes the row (and its `onupdate` stamp) whenever an attribute changed;
            // the plan id always does, so every rebuild updates it.
            sqlx::query(
                "UPDATE tw_session SET mode = $3, gate = $4, states = $5, signals = $6, \
                 plan_ids = CAST($7 AS JSONB), updated_at = now() \
                 WHERE user_id = $1 AND session_date = $2",
            )
            .bind(user_id)
            .bind(as_of)
            .bind(mode)
            .bind(gate.value())
            .bind(states32)
            .bind(signals32)
            .bind(plan_ids)
            .execute(&mut *conn)
            .await?;
        }
    }
    Ok(())
}

/// `_raise_adjustment_alert`: `TWT_ADJUSTMENT_RESET`, whether or not a raise was emitted.
async fn raise_adjustment_alert(
    adjusted: &Adjusted,
    as_of: NaiveDate,
    settings: &Settings,
    raised_at: DateTime<Utc>,
) {
    if adjusted.is_empty() {
        return;
    }
    let positions = adjusted
        .iter()
        .map(|(instrument_id, (position_id, symbol, entry, now))| {
            object([
                ("id", int(*position_id)),
                ("instrument_id", int(*instrument_id)),
                ("symbol", s(symbol.clone())),
                ("entry_adj_factor", s(py_str(*entry))),
                ("adj_factor", s(py_str(*now))),
            ])
        })
        .collect();
    let alert = TwtAlert {
        name: "TWT_ADJUSTMENT_RESET",
        severity: "critical",
        summary: format!(
            "{} three-weeks-tight position(s) were re-adjusted under the hold on {}; a resting \
             GTT may be quoting a pre-adjustment price.",
            adjusted.len(),
            iso(as_of)
        ),
        labels: vec![("trade_date".to_owned(), iso(as_of))],
        detail: object([("positions", OrderedJson::Array(positions))]),
    };
    dispatch(&alert, settings, raised_at).await;
}

/// `_raise_evening_alert`: the `TWT_EVENING` digest. Never raises.
async fn raise_evening_alert(
    report: &EveningReport,
    plan: &TwtPlan,
    settings: &Settings,
    raised_at: DateTime<Utc>,
) {
    let entries_word = if report.entries == 1 { "y" } else { "ies" };
    let OrderedJson::Object(mut detail) = report.as_detail() else {
        return;
    };
    let lines = plan
        .lines
        .iter()
        .map(|line| {
            object([
                ("kind", s(line.kind.value())),
                ("symbol", s(line.symbol.clone())),
                ("quantity", int(line.quantity)),
                (
                    "stop_price",
                    line.stop_price.map_or(OrderedJson::Null, |p| s(py_str(p))),
                ),
                ("value_inr", s(py_str(line.value_inr))),
                ("note", s(line.note.clone())),
            ])
        })
        .collect();
    let skipped = plan
        .skips
        .iter()
        .map(|skip| {
            object([
                ("symbol", s(skip.symbol.clone())),
                ("reason", s(skip_value(skip.reason))),
                ("detail", s(skip.detail.clone())),
            ])
        })
        .collect();
    crate::pyjson::update(&mut detail, "lines", OrderedJson::Array(lines));
    crate::pyjson::update(&mut detail, "skipped", OrderedJson::Array(skipped));
    let alert = TwtAlert {
        name: "TWT_EVENING",
        severity: "warning",
        summary: format!(
            "TWT {} plan for {}: gate {}, {} entr{entries_word}, {} arm(s), {} ratchet(s), {} \
             skip(s).",
            report.source.to_lowercase(),
            iso(report.session),
            report.gate,
            report.entries,
            report.arms,
            report.ratchets,
            report.skips
        ),
        labels: vec![
            ("trade_date".to_owned(), iso(report.session)),
            ("source".to_owned(), report.source.clone()),
        ],
        detail: OrderedJson::Object(detail),
    };
    dispatch(&alert, settings, raised_at).await;
}

fn count(lines: &[PlanLine], kind: LineKind) -> i64 {
    len_i64(lines.iter().filter(|l| l.kind == kind).count())
}

/// The inputs the evening reads from its caller.
pub(crate) struct EveningInputs<'a> {
    pub(crate) trade_date: NaiveDate,
    pub(crate) user_id: i64,
    pub(crate) execution_enabled: bool,
    pub(crate) source: &'a str,
    /// `now` (`datetime.now(tz=UTC)` on the task clock).
    pub(crate) now: DateTime<Utc>,
    pub(crate) settings: &'a Settings,
}

/// `run_twt_evening(...)`: the plan and the session row, or `None` (with the reason noted on
/// `outcome`) when the detector has not spoken about the session.
pub(crate) async fn run_twt_evening(
    conn: &mut PgConnection,
    outcome: &mut StepOutcome,
    inputs: &EveningInputs<'_>,
) -> Result<Option<EveningReport>, WorkerError> {
    let trade_date = inputs.trade_date;
    let user_id = inputs.user_id;
    let spoken: Option<(NaiveDate,)> =
        sqlx::query_as("SELECT date FROM tw_breadth_daily WHERE user_id = $1 AND date = $2")
            .bind(user_id)
            .bind(trade_date)
            .fetch_optional(&mut *conn)
            .await?;
    if spoken.is_none() {
        outcome.status = StepStatus::Skipped;
        outcome.note(
            "skipped_reason",
            s(format!(
                "{} has no tw_breadth_daily row: the detector has not spoken about this \
                 session, and a plan built without its gate is a guess",
                iso(trade_date)
            )),
        );
        return Ok(None);
    }
    let config = evening_config(conn, user_id).await?;
    let settings_row = config_row(conn, user_id).await?;
    let gate = gate_for_session(conn, user_id, trade_date).await?;
    let sleeve: LoadedSleeve = load_sleeve(conn, user_id, trade_date).await?;
    let equity = sleeve.quantized_equity()?;
    // LV10.2: the cap counts entries sent on the day the plan executes.
    let entries_on = if inputs.source == SOURCE_EVENING {
        next_trading_day(conn, trade_date).await?
    } else {
        Some((inputs.now + Duration::minutes(IST_MINUTES)).date_naive())
    };
    let book = book_state(conn, user_id, trade_date, Some(trade_date), entries_on).await?;
    let multiplier = slot_multiplier(conn, user_id, inputs.execution_enabled, &config).await?;
    let candidates = candidates_for(conn, user_id, trade_date).await?;
    let (entries, skips) = build_entries(
        &candidates,
        gate,
        equity,
        &book,
        &config,
        multiplier,
        settings_row.as_ref().map(|r| r.max_open_positions),
        None,
    )
    .map_err(core_err)?;
    let adjusted = adjusted_positions(conn, user_id, trade_date).await?;
    let exits = annotate_adjustments(exit_lines(&book, trade_date), &adjusted);
    let plan = assemble(
        trade_date,
        gate,
        equity,
        &exits,
        &entries,
        &skips,
        inputs.source,
    );
    let plan_id = store_plan(conn, user_id, trade_date, inputs.source, &plan, inputs.now).await?;
    settle_session(
        conn,
        user_id,
        trade_date,
        gate,
        &plan_id,
        inputs.source,
        inputs.execution_enabled,
    )
    .await?;
    let report = EveningReport {
        session: trade_date,
        source: inputs.source.to_owned(),
        gate: gate.value(),
        plan_id,
        equity_inr: equity,
        entries: count(&plan.lines, LineKind::BuyAtOpen),
        arms: count(&plan.lines, LineKind::ArmGtt),
        ratchets: count(&plan.lines, LineKind::RaiseGttStop),
        skips: len_i64(plan.skips.len()),
        adjustments: len_i64(adjusted.len()),
        naked: len_i64(book.positions_naked_of_gtt.len()),
    };
    raise_adjustment_alert(&adjusted, trade_date, inputs.settings, inputs.now).await;
    raise_evening_alert(&report, &plan, inputs.settings, inputs.now).await;
    outcome.rows_out = len_i64(plan.lines.len());
    outcome.note("mark_fallbacks", int(sleeve.stale_marks()));
    if let OrderedJson::Object(entries) = report.as_detail() {
        for (k, v) in entries {
            outcome.note(&k, v);
        }
    }
    Ok(Some(report))
}

/// `last_detected_session`: the newest **tradeable** (not thin) breadth row on or before.
pub(crate) async fn last_detected_session(
    conn: &mut PgConnection,
    user_id: i64,
    on_or_before: NaiveDate,
) -> Result<Option<NaiveDate>, WorkerError> {
    let row: Option<(NaiveDate,)> = sqlx::query_as(
        "SELECT date FROM tw_breadth_daily WHERE user_id = $1 AND date <= $2 \
         AND thin_session IS false ORDER BY date DESC LIMIT 1",
    )
    .bind(user_id)
    .bind(on_or_before)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(row.map(|r| r.0))
}

/// `next_trading_day`: the session a `BUY_AT_OPEN` would fill in.
async fn next_trading_day(
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

#[cfg(test)]
mod tests {
    #![allow(clippy::panic)]
    use super::*;

    fn line(kind: LineKind, instrument_id: i64, symbol: &str) -> PlanLine {
        PlanLine {
            kind,
            instrument_id,
            symbol: symbol.to_owned(),
            quantity: 10,
            entry_price: None,
            stop_price: Some(Decimal::new(9_000, 2)),
            previous_stop: Some(Decimal::new(8_000, 2)),
            high_since: None,
            value_inr: Decimal::ZERO,
            cap: None,
            note: "the stop moves on 2026-10-30".to_owned(),
            position_id: Some(1),
        }
    }

    #[test]
    fn only_a_raise_on_a_re_adjusted_name_carries_the_reason() {
        // test_twt_evening.py: `04` §7.3 step 3 — the raise carries it; an ARM_GTT does not.
        let mut adjusted = Adjusted::new();
        adjusted.insert(
            7,
            (1, "ACME".to_owned(), Decimal::new(1, 0), Decimal::new(5, 1)),
        );
        let out = annotate_adjustments(
            vec![
                line(LineKind::RaiseGttStop, 7, "ACME"),
                line(LineKind::ArmGtt, 7, "ACME"),
                line(LineKind::RaiseGttStop, 8, "BETA"),
            ],
            &adjusted,
        );
        assert_eq!(
            out[0].note,
            "a corporate action re-adjusted this name under the hold (1 -> 0.5); the level was \
             re-derived and the resting trigger is a pre-adjustment price — check it at the broker"
        );
        assert_eq!(out[1].note, "the stop moves on 2026-10-30");
        assert_eq!(out[2].note, "the stop moves on 2026-10-30");
    }

    #[test]
    fn the_report_keeps_pythons_keys_and_equity_as_str() {
        let report = EveningReport {
            session: NaiveDate::from_ymd_opt(2026, 10, 29).unwrap_or_default(),
            source: SOURCE_EVENING.to_owned(),
            gate: "SHUT",
            plan_id: "p".to_owned(),
            equity_inr: Decimal::new(251_234_560, 2),
            entries: 0,
            arms: 1,
            ratchets: 0,
            skips: 2,
            adjustments: 0,
            naked: 1,
        };
        let OrderedJson::Object(entries) = report.as_detail() else {
            panic!("an object");
        };
        let keys: Vec<&str> = entries.iter().map(|(k, _)| k.as_str()).collect();
        assert_eq!(
            keys,
            [
                "session",
                "source",
                "gate",
                "plan_id",
                "equity_inr",
                "entries",
                "arms",
                "ratchets",
                "skips",
                "adjustments",
                "naked"
            ]
        );
        assert_eq!(entries[4].1, s("2512345.60"));
    }

    #[test]
    fn every_skip_reason_has_its_schema_value() {
        // ck_tw_plan_skip_reason_known's twelve values.
        let all = [
            SkipReason::GateShut,
            SkipReason::AlreadyHeld,
            SkipReason::BelowLiquidityFloor,
            SkipReason::NoBar,
            SkipReason::LockedUpperCircuit,
            SkipReason::SessionCap,
            SkipReason::SlotsFull,
            SkipReason::NoSleeveCapital,
            SkipReason::StopNotBelowEntry,
            SkipReason::BelowMinTradeValue,
            SkipReason::ExposureFull,
            SkipReason::TurnoverCap,
        ];
        for reason in all {
            let serde = serde_json::to_string(&reason).unwrap_or_default();
            assert_eq!(serde, format!("\"{}\"", skip_value(reason)));
        }
    }
}
