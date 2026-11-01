//! Ports the runner half of `app/swing_monitor.py` (SW6, SW10, SW11, SW25, SW26, SW30, LV4):
//! the flag ([`build_monitor`] builds nothing with `BASKFY_SWING_MONITOR_ENABLED` off), the
//! signal store ([`PgSignalStore`]: an `sw_signal` row and a one-line `sw_plan(SIGNAL)` per
//! trigger, sized by the same `build_entries` the evening uses, re-read per trigger), the
//! notice to the daily focus, SW25's auto-execute queue ([`drain_auto_execute`]: drained every
//! pass, **sent only when all three flags say so**), the quote fallback's cap
//! ([`QuoteFallback`]), the late-login wait ([`wait_for_session`]), the chores at their own
//! hours inside the watch ([`Chores`]), the gated heartbeat ([`GatedHeartbeat`]) and the
//! `monitor_ran` mark.
//!
//! **No order path lives here.** The store records lines; the strategy raises signals; the
//! only thing that can confirm a line is [`drain_auto_execute`] handing it to an
//! [`AutoConfirm`] — which the desk wires to the same `execute_line` a click reaches, gateway
//! and guards included (`service.rs`) — and only when `auto_execute_enabled()` is true.
//!
//! Ticks are Python's dicts ([`PyDict`]) up to the strategy's door, read there by [`tick_of`]
//! exactly as `SwingBreakout.on_tick` reads them (`int(token or 0)`, `Decimal(str(price))`,
//! `_tick_time`), so a polled quote and a pushed tick cannot be told apart and a malformed one
//! fails where Python's fails.

use std::collections::BTreeMap;
use std::sync::Arc;

use async_trait::async_trait;
use baskfy_core::swing::opening_range::TriggerState;
use baskfy_core::swing::plan::WatchItem;
use baskfy_core::swing::setups::{Setup, SwingConfig};
use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime, TimeZone};
use rust_decimal::Decimal;

use crate::clock::ist;
use crate::ids::IdSource;
use crate::lanes::swing::breakout::{
    CandleSource, Signal, SignalStore, SwingBreakout, Tick, WatchedName, trigger_state_str,
};
use crate::lanes::swing::context::{
    SignalContext, auto_execute_enabled, entries_now, gate_line, live_execution, load_context,
};
use crate::lanes::swing::live_gate::LiveLevel;
use crate::lanes::swing::notify::{Notifier, SignalNotice, notice_json};
use crate::lanes::swing::rules::{
    PLAN_TTL_MINUTES, SwingPlan, assemble, dec, dec_of_float, gate_str, kind_str, py_dec, skip_str,
    trail_str,
};
use crate::lanes::swing::store::SCHEMA;
use crate::lanes::swing::values::{col, int, raw_dec, text};
use crate::settings::Settings;
use crate::storage::{Conn, DbValue, Param, StorageError};
use crate::wire::{PyDict, PyValue};
use baskfy_providers::records::PyDateTime;

/// `SESSION_WAIT_UNTIL` (LV4): a login after 15:20 is tomorrow's.
#[must_use]
pub fn session_wait_until() -> NaiveTime {
    NaiveTime::from_hms_opt(15, 20, 0).unwrap_or_default()
}

/// `SESSION_POLL_SECONDS`.
pub const SESSION_POLL_SECONDS: f64 = 30.0;

/// `TypeError: 'NoneType' object is not subscriptable` — what Python raises when a
/// `RETURNING id` row is missing and the code indexes it.
const NONE_SUBSCRIPT: &str = "TypeError: 'NoneType' object is not subscriptable";

fn table(schema: &str, name: &str) -> String {
    if schema.is_empty() {
        name.to_owned()
    } else {
        format!("{schema}.{name}")
    }
}

/// `float(row[col])` for the driver's value. A string Python cannot read is its `ValueError`.
fn float_col(r: &crate::storage::Row, name: &str) -> Result<f64, String> {
    match col(r, name) {
        Some(DbValue::Float(x)) => Ok(*x),
        #[allow(clippy::cast_precision_loss)]
        Some(DbValue::Int(i)) => Ok(*i as f64),
        Some(DbValue::Bool(b)) => Ok(if *b { 1.0 } else { 0.0 }),
        Some(DbValue::Text(s)) => crate::settings::py_float(s)
            .ok_or_else(|| format!("ValueError: could not convert string to float: '{s}'")),
        Some(DbValue::Null) | None => Err(
            "TypeError: float() argument must be a string or a real number, not 'NoneType'"
                .to_owned(),
        ),
        Some(other) => Err(format!("TypeError: float() argument: {other:?}")),
    }
}

/// `load_config`: `SwingConfig::default()` with the person's three liquidity floors and three
/// sizing knobs.
///
/// # Errors
/// The driver's, or a column Python's `float()`/`int()` refuses.
pub async fn load_config(
    conn: &mut Conn,
    user_id: i64,
    schema: &str,
) -> Result<SwingConfig, String> {
    let row = conn
        .execute(
            &format!(
                "SELECT adr_min_pct, turnover_min_inr, price_min, risk_per_trade_pct, \
                 max_position_pct, max_open_positions FROM {} WHERE user_id = ?",
                table(schema, "sw_config")
            ),
            &[Param::Int(user_id)],
        )
        .await
        .map_err(|e| e.to_string())?
        .rows
        .into_iter()
        .next();
    let mut config = SwingConfig::default();
    let Some(r) = row else { return Ok(config) };
    config.liquidity = baskfy_core::swing::setups::LiquidityConfig::default();
    config.liquidity.adr_min_pct = float_col(&r, "adr_min_pct")?;
    config.liquidity.turnover_min_inr = float_col(&r, "turnover_min_inr")?;
    config.liquidity.price_min = float_col(&r, "price_min")?;
    config.sizing = baskfy_core::swing::sizing::SizingConfig::default();
    config.sizing.risk_per_trade_pct = float_col(&r, "risk_per_trade_pct")?;
    config.sizing.max_position_pct = float_col(&r, "max_position_pct")?;
    config.sizing.max_open_positions = int(col(&r, "max_open_positions"));
    Ok(config)
}

/// `Setup(value)`: Python's enum lookup, which raises for anything it does not name.
///
/// # Errors
/// `ValueError: '<x>' is not a valid Setup`.
pub fn setup_of(value: &str) -> Result<Setup, String> {
    match value {
        "FLAG" => Ok(Setup::Flag),
        "EP" => Ok(Setup::Ep),
        "PARABOLIC_SHORT" => Ok(Setup::ParabolicShort),
        other => Err(format!(
            "ValueError: {} is not a valid Setup",
            baskfy_providers::py::repr_str(other)
        )),
    }
}

/// `load_watchlist`: every `WATCHING` row with a Kite token. A FLAG's pivot is its trigger; an
/// EP's is the gap itself. A name with no token is skipped with a warning; a setup that is not
/// FLAG or EP is skipped silently; a setup Python's enum does not know raises, as Python's does.
///
/// # Errors
/// The driver's, or an unknown setup.
pub async fn load_watchlist(
    conn: &mut Conn,
    user_id: i64,
    circuits: &BTreeMap<String, Decimal>,
) -> Result<Vec<WatchedName>, String> {
    let rows = conn
        .execute(
            &format!(
                "SELECT w.id, w.instrument_id, i.symbol, i.kite_token, w.setup, w.trigger, w.focus \
                 FROM {SCHEMA}.sw_watch w JOIN {SCHEMA}.instrument i ON i.id = w.instrument_id \
                 WHERE w.user_id = ? AND w.state = 'WATCHING' ORDER BY i.symbol"
            ),
            &[Param::Int(user_id)],
        )
        .await
        .map_err(|e| e.to_string())?
        .rows;
    let mut names = Vec::new();
    for r in rows {
        let symbol = text(col(&r, "symbol"));
        let Some(token) = col(&r, "kite_token").filter(|v| !v.is_null()) else {
            log_line!(
                "WARNING swing_monitor {symbol} is watched but has no Kite token; not subscribed"
            );
            continue;
        };
        let setup = setup_of(&text(col(&r, "setup")))?;
        if setup != Setup::Flag && setup != Setup::Ep {
            continue;
        }
        let trigger = col(&r, "trigger").and_then(raw_dec);
        names.push(WatchedName {
            watch_id: int(col(&r, "id")),
            instrument_id: int(col(&r, "instrument_id")),
            symbol: symbol.clone(),
            token: int(Some(token)),
            setup,
            pivot_high: if setup == Setup::Flag { trigger } else { None },
            upper_circuit: circuits.get(&symbol).copied(),
            focus: crate::lanes::swing::values::boolean(col(&r, "focus")),
        });
    }
    Ok(names)
}

/// `Planned`: what `_plan_for` wrote.
#[derive(Debug, Clone, PartialEq)]
pub struct Planned {
    /// The line's id, or `None` for a skip.
    pub line_id: Option<i64>,
    /// The plan.
    pub plan: SwingPlan,
    /// Its expiry.
    pub expires_at: DateTime<FixedOffset>,
    /// `sw_plan.plan_id`.
    pub plan_id: Option<String>,
}

/// `PgSignalStore`: `sw_signal` + a one-line `sw_plan(SIGNAL)` for a trigger; a signal row alone
/// otherwise. It records lines auto-execute may confirm (`pending_confirms`) and never reaches a
/// broker itself.
pub struct PgSignalStore {
    /// The monitor's connection (`public.*`).
    pub conn: Conn,
    /// The user.
    pub user_id: i64,
    /// The session's day.
    pub day: NaiveDate,
    /// The person's config.
    pub config: SwingConfig,
    /// SW30's live level.
    pub live_level: Option<Arc<dyn LiveLevel>>,
    /// The reading last taken (the log's; never what a plan is sized against).
    pub context: Option<SignalContext>,
    /// Lines written this morning.
    pub lines_written: Vec<i64>,
    /// SW25: lines auto-execute may confirm, drained by the runner.
    pub pending_confirms: Vec<(Signal, Planned)>,
    /// A2: where a daily-focus trigger is told.
    pub notifier: Option<Notifier>,
    /// Every notice composed.
    pub notices: Vec<SignalNotice>,
    /// The settings (the gate's index, the notice's mode, A9's live flag).
    pub settings: Settings,
    /// `uuid4`.
    pub ids: Arc<dyn IdSource>,
    /// The live gate's "now".
    pub now: Arc<dyn Fn() -> DateTime<FixedOffset> + Send + Sync>,
}

/// `str(uuid.UUID(bytes=...))`.
#[must_use]
pub fn uuid_text(bytes: [u8; 16]) -> String {
    let h: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
    format!(
        "{}-{}-{}-{}-{}",
        &h[0..8],
        &h[8..12],
        &h[12..16],
        &h[16..20],
        &h[20..32]
    )
}

/// `log_gate_change` (SW30): one line when the session's gate moves, naming the averages that
/// moved it.
#[must_use]
pub fn gate_change_line(
    before: baskfy_core::swing::market::MarketGate,
    c: &SignalContext,
) -> String {
    match &c.live {
        None => format!(
            "INFO swing_monitor gate {} -> {} (the last close's)",
            gate_str(before),
            gate_str(c.gate)
        ),
        Some(live) => format!(
            "INFO swing_monitor gate {} -> {} on the live {}: {}, 10-day {} {} 20-day {}; rung {}, \
             entries {}",
            gate_str(before),
            gate_str(c.gate),
            live.index_slug,
            baskfy_providers::py::fixed(live.reading.close, 2),
            baskfy_providers::py::fixed(live.reading.ma_fast, 2),
            if live.reading.long_bias() { ">" } else { "<=" },
            baskfy_providers::py::fixed(live.reading.ma_slow, 2),
            c.tier.level,
            if c.tier.new_entries_allowed {
                "allowed"
            } else {
                "refused"
            }
        ),
    }
}

impl PgSignalStore {
    fn dec_param(&self, d: Option<Decimal>) -> Param {
        match d {
            None => Param::Null,
            Some(v) if self.conn.is_postgres() => Param::Decimal(v),
            Some(v) => Param::Text(py_dec(v)),
        }
    }

    /// `read_context`: the book as it is *now* (A5).
    ///
    /// # Errors
    /// The driver's.
    pub async fn read_context(&mut self) -> Result<SignalContext, StorageError> {
        let before = self.context.as_ref().map(|c| c.gate);
        let now = (self.now)();
        let live = self.live_level.clone();
        let context = load_context(
            &mut self.conn,
            self.user_id,
            self.day,
            SCHEMA,
            live.as_deref(),
            Some(now),
            &self.settings.swing_index_slug.clone(),
        )
        .await?;
        if let Some(b) = before
            && b != context.gate
        {
            log_line!("{}", gate_change_line(b, &context));
        }
        self.context = Some(context.clone());
        Ok(context)
    }

    /// `release_reservation` (A7): this name's PENDING_RANGE line on today's plan → SKIPPED.
    ///
    /// # Errors
    /// The driver's.
    pub async fn release_reservation(
        &mut self,
        instrument_id: i64,
        raised_at: &DateTime<FixedOffset>,
    ) -> Result<i64, StorageError> {
        let rows = self
            .conn
            .execute(
                &format!(
                    "UPDATE {SCHEMA}.sw_plan_line SET state = 'SKIPPED', \
                     note = COALESCE(note, '') || ?, updated_at = CURRENT_TIMESTAMP \
                     WHERE user_id = ? AND kind = 'PENDING_RANGE' AND state = 'PROPOSED' \
                     AND instrument_id = ? AND plan_id IN \
                     (SELECT id FROM {SCHEMA}.sw_plan WHERE user_id = ? AND as_of = ?) RETURNING id"
                ),
                &[
                    Param::Text(format!(
                        "; slot released at {} — the range broke, see the SIGNAL plan",
                        raised_at.format("%H:%M")
                    )),
                    Param::Int(self.user_id),
                    Param::Int(instrument_id),
                    Param::Int(self.user_id),
                    Param::Date(self.day),
                ],
            )
            .await?
            .rows;
        Ok(i64::try_from(rows.len()).unwrap_or(0))
    }

    #[allow(clippy::too_many_lines)]
    async fn plan_for(
        &mut self,
        signal: &Signal,
        raised_at: DateTime<FixedOffset>,
    ) -> Result<Planned, String> {
        let verdict = &signal.verdict;
        self.release_reservation(signal.watch.instrument_id, &raised_at)
            .await
            .map_err(|e| e.to_string())?;
        let context = self.read_context().await.map_err(|e| e.to_string())?;
        let (adr, turnover, score) = context
            .detected
            .get(&signal.watch.symbol)
            .cloned()
            .unwrap_or((Decimal::ZERO, None, Decimal::ZERO));
        let item = WatchItem {
            symbol: signal.watch.symbol.clone(),
            setup: signal.watch.setup,
            trigger: verdict.entry.unwrap_or_default(),
            stop_ref: Some(verdict.stop.unwrap_or_default()),
            adr_pct: adr,
            avg_turnover_inr: turnover,
            score,
            locked_upper_circuit: false,
        };
        let (entries, skipped) = entries_now(
            &item,
            &context,
            &self.config,
            self.day,
            live_execution(&self.settings),
        )
        .map_err(|e| e.to_string())?;
        let plan = assemble(
            self.day,
            context.gate,
            context.tier.clone(),
            entries,
            Vec::new(),
            skipped,
        );
        let plan_id = uuid_text(self.ids.uuid4_bytes());
        let expires_at = raised_at + chrono::Duration::minutes(PLAN_TTL_MINUTES);
        let plan_row = self
            .conn
            .execute(
                &format!(
                    "INSERT INTO {SCHEMA}.sw_plan (plan_id, user_id, as_of, source, built_at, \
                     expires_at, plan_hash, gate, exposure_level, total_risk_inr, \
                     total_new_exposure_inr) VALUES (?, ?, ?, 'SIGNAL', ?, ?, ?, ?, ?, ?, ?) RETURNING id"
                ),
                &[
                    Param::Text(plan_id.clone()),
                    Param::Int(self.user_id),
                    Param::Date(self.day),
                    Param::Aware(raised_at),
                    Param::Aware(expires_at),
                    Param::Text(plan.plan_hash()),
                    Param::Text(gate_str(plan.gate).to_owned()),
                    Param::Int(plan.tier.level),
                    self.dec_param(Some(plan.total_risk_inr)),
                    self.dec_param(Some(plan.total_new_exposure_inr)),
                ],
            )
            .await
            .map_err(|e| e.to_string())?;
        // `int(plan_row["id"])`: a missing row is Python's TypeError, never a plan pk of 0.
        let plan_pk = plan_row
            .rows
            .first()
            .map(|r| int(col(r, "id")))
            .ok_or_else(|| NONE_SUBSCRIPT.to_owned())?;
        for skip in &plan.skipped {
            self.conn
                .execute(
                    &format!(
                        "INSERT INTO {SCHEMA}.sw_plan_skip (plan_id, user_id, instrument_id, symbol, \
                         reason, detail) VALUES (?, ?, ?, ?, ?, ?)"
                    ),
                    &[
                        Param::Int(plan_pk),
                        Param::Int(self.user_id),
                        Param::Int(signal.watch.instrument_id),
                        Param::Text(skip.symbol.clone()),
                        Param::Text(skip_str(skip.reason)),
                        if skip.detail.is_empty() {
                            Param::Null
                        } else {
                            Param::Text(skip.detail.clone())
                        },
                    ],
                )
                .await
                .map_err(|e| e.to_string())?;
        }
        let mut line_id = None;
        for line in plan.lines.clone() {
            let row = self
                .conn
                .execute(
                    &format!(
                        "INSERT INTO {SCHEMA}.sw_plan_line (plan_id, user_id, kind, instrument_id, \
                         setup, quantity, trigger, stop, risk_inr, position_value, trail, note, state, \
                         client_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PROPOSED', ?) \
                         RETURNING id"
                    ),
                    &[
                        Param::Int(plan_pk),
                        Param::Int(self.user_id),
                        Param::Text(kind_str(line.kind).to_owned()),
                        Param::Int(signal.watch.instrument_id),
                        line.setup
                            .map_or(Param::Null, |s| Param::Text(s.as_str().to_owned())),
                        Param::Int(line.quantity),
                        self.dec_param(line.trigger),
                        self.dec_param(line.stop),
                        self.dec_param(Some(line.risk_inr)),
                        self.dec_param(Some(line.position_value)),
                        line.trail
                            .map_or(Param::Null, |t| Param::Text(trail_str(t).to_owned())),
                        Param::Text(line.note.clone()),
                        Param::Text(format!("{plan_id}:{}:{}", line.symbol, kind_str(line.kind))),
                    ],
                )
                .await
                .map_err(|e| e.to_string())?;
            line_id = Some(
                row.rows
                    .first()
                    .map(|r| int(col(r, "id")))
                    .ok_or_else(|| NONE_SUBSCRIPT.to_owned())?,
            );
        }
        if let Some(first) = plan.lines.first() {
            log_line!(
                "INFO swing_monitor SIGNAL plan {plan_id}: BUY {} x{} @ {} stop {} (expires {})",
                signal.watch.symbol,
                first.quantity,
                first.trigger.map_or_else(|| "None".to_owned(), py_dec),
                first.stop.map_or_else(|| "None".to_owned(), py_dec),
                expires_at.format("%H:%M:%S")
            );
        } else {
            log_line!(
                "INFO swing_monitor SIGNAL plan {plan_id}: {} triggered but skipped — {}",
                signal.watch.symbol,
                plan.skipped
                    .iter()
                    .map(|s| format!("{} {}", skip_str(s.reason), s.detail)
                        .trim()
                        .to_owned())
                    .collect::<Vec<_>>()
                    .join("; ")
            );
        }
        Ok(Planned {
            line_id,
            plan,
            expires_at,
            plan_id: Some(plan_id),
        })
    }

    async fn notify(
        &mut self,
        signal: &Signal,
        raised_at: DateTime<FixedOffset>,
        planned: &Planned,
    ) {
        if !signal.watch.focus {
            return;
        }
        let line = planned.plan.lines.first();
        let skipped = if line.is_some() {
            None
        } else {
            let text = planned
                .plan
                .skipped
                .iter()
                .map(|s| {
                    format!("{} {}", skip_str(s.reason), s.detail)
                        .trim()
                        .to_owned()
                })
                .collect::<Vec<_>>()
                .join("; ");
            Some(if text.is_empty() {
                "no line".to_owned()
            } else {
                text
            })
        };
        let notice = SignalNotice {
            symbol: signal.watch.symbol.clone(),
            setup: signal.watch.setup.as_str().to_owned(),
            at: raised_at,
            entry: line.map_or(signal.verdict.entry, |l| l.trigger),
            stop: line.map_or(signal.verdict.stop, |l| l.stop),
            quantity: line.map(|l| l.quantity),
            risk_inr: line.map(|l| l.risk_inr),
            plan_expires_at: line.map(|_| planned.expires_at),
            skipped,
            mode: if live_execution(&self.settings) {
                "LIVE".to_owned()
            } else {
                "DRY_RUN".to_owned()
            },
        };
        self.notices.push(notice.clone());
        log_line!("INFO swing_monitor swing notice {}", notice_json(&notice));
        if let Some(n) = &mut self.notifier {
            n.notify(&notice).await;
        }
    }
}

/// `raised_at = signal.at.replace(tzinfo=IST)`.
fn raised_at(at: NaiveDateTime) -> DateTime<FixedOffset> {
    ist()
        .from_local_datetime(&at)
        .single()
        .unwrap_or_else(|| ist().from_utc_datetime(&at))
}

#[async_trait]
impl SignalStore for PgSignalStore {
    async fn raise_signal(&mut self, signal: &Signal) -> Result<(), String> {
        let verdict = &signal.verdict;
        let raised = raised_at(signal.at);
        let row = self
            .conn
            .execute(
                &format!(
                    "INSERT INTO {SCHEMA}.sw_signal (user_id, watch_id, instrument_id, setup, \
                     session_date, raised_at, state, or_window_minutes, range_high, range_low, \
                     low_of_day, last_price, entry, stop) \
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id"
                ),
                &[
                    Param::Int(self.user_id),
                    Param::Int(signal.watch.watch_id),
                    Param::Int(signal.watch.instrument_id),
                    Param::Text(signal.watch.setup.as_str().to_owned()),
                    Param::Date(self.day),
                    Param::Aware(raised),
                    Param::Text(trigger_state_str(verdict.state).to_owned()),
                    Param::Int(signal.window_minutes),
                    self.dec_param(verdict.range_high),
                    self.dec_param(verdict.range_low),
                    self.dec_param(Some(signal.low_of_day)),
                    self.dec_param(Some(signal.last_price)),
                    self.dec_param(verdict.entry),
                    self.dec_param(verdict.stop),
                ],
            )
            .await
            .map_err(|e| e.to_string())?;
        let signal_id = row.rows.first().map(|r| int(col(r, "id")));
        crate::lanes::session::telemetry::count(
            "swing_signals",
            &[("state", trigger_state_str(verdict.state))],
        );
        if verdict.state != TriggerState::Triggered
            || verdict.entry.is_none()
            || verdict.stop.is_none()
        {
            return Ok(());
        }
        let planned = self.plan_for(signal, raised).await?;
        if let (Some(line_id), Some(signal_id)) = (planned.line_id, signal_id) {
            self.conn
                .execute(
                    &format!("UPDATE {SCHEMA}.sw_signal SET plan_line_id = ? WHERE id = ?"),
                    &[Param::Int(line_id), Param::Int(signal_id)],
                )
                .await
                .map_err(|e| e.to_string())?;
            self.lines_written.push(line_id);
        }
        if planned.line_id.is_some() && planned.plan_id.is_some() {
            self.pending_confirms
                .push((signal.clone(), planned.clone()));
        }
        self.notify(signal, raised, &planned).await;
        Ok(())
    }
}

/// What a confirm answered: `ExecOutcome.status` and its `reason`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Confirmed {
    /// `outcome.status`.
    pub status: String,
    /// `outcome.reason` (empty for none).
    pub reason: String,
}

/// What confirms a line for auto-execute: the desk wires this to `execute_line` with the swing
/// gateway, the price the tick carried and `confirm="true"` — exactly what a click reaches.
#[async_trait]
pub trait AutoConfirm: Send + Sync {
    /// Confirm one line; the outcome, or the error as `"<PythonType>: <words>"`.
    async fn confirm(
        &self,
        plan_id: &str,
        line_id: i64,
        last_price: Decimal,
    ) -> Result<Confirmed, String>;
}

/// `drain_auto_execute` (SW25): confirm every line the store just wrote **only when all three
/// flags say so**. The queue is drained whether or not the flag is on, so a line is never
/// confirmed late across a restart. A failure is logged and the watch goes on.
pub async fn drain_auto_execute(
    pending: &mut Vec<(Signal, Planned)>,
    settings: &Settings,
    executor: &dyn AutoConfirm,
) -> Vec<(String, String)> {
    let queued = std::mem::take(pending);
    if queued.is_empty() || !auto_execute_enabled(settings) {
        return Vec::new();
    }
    let mut done = Vec::new();
    for (signal, planned) in queued {
        let symbol = signal.watch.symbol.clone();
        let (Some(plan_id), Some(line_id)) = (planned.plan_id.as_deref(), planned.line_id) else {
            continue;
        };
        match executor.confirm(plan_id, line_id, signal.last_price).await {
            Ok(outcome) => {
                crate::lanes::session::telemetry::count(
                    "swing_auto_execute",
                    &[("outcome", &outcome.status)],
                );
                log_line!(
                    "WARNING swing_monitor AUTO-EXECUTE {symbol}: {}{}",
                    outcome.status,
                    if outcome.reason.is_empty() {
                        String::new()
                    } else {
                        format!(" — {}", outcome.reason)
                    }
                );
                done.push((symbol, outcome.status));
            }
            Err(e) => {
                crate::lanes::session::telemetry::count(
                    "swing_auto_execute",
                    &[("outcome", "error")],
                );
                log_line!(
                    "ERROR swing_monitor AUTO-EXECUTE {symbol}: {e} — the line is written and can \
                     be confirmed by hand"
                );
                done.push((symbol, "ERROR".to_owned()));
            }
        }
    }
    done
}

/// `build_monitor`: the strategy, or `None` with the flag off — nothing constructed. It takes
/// no gateway at all: a process that holds no gateway cannot be talked into using one.
///
/// # Errors
/// An unknown window.
pub fn build_monitor(
    enabled: bool,
    watchlist: Vec<WatchedName>,
    day: NaiveDate,
    config: SwingConfig,
    window_minutes: Option<i64>,
) -> Result<Option<SwingBreakout>, crate::lanes::swing::rules::RuleError> {
    if !enabled {
        log_line!(
            "INFO swing_monitor BASKFY_SWING_MONITOR_ENABLED is false; the opening-range monitor \
             is not built"
        );
        return Ok(None);
    }
    SwingBreakout::new(watchlist, day, window_minutes, config).map(Some)
}

// =============================================================================================
// Python's readings of a tick dict
// =============================================================================================

/// `int(value)` for a Python value: an int as is, a float truncated, a bool as 0/1, a string as
/// Python parses it. Anything else is Python's `ValueError`/`TypeError`.
///
/// # Errors
/// What Python's `int()` raises.
pub fn py_int_value(value: &PyValue) -> Result<i64, String> {
    match value {
        PyValue::Int(i) => Ok(*i),
        PyValue::Bool(b) => Ok(i64::from(*b)),
        PyValue::Float(f) if f.is_finite() =>
        {
            #[allow(clippy::cast_possible_truncation)]
            Ok(f.trunc() as i64)
        }
        PyValue::Float(f) if f.is_nan() => {
            Err("ValueError: cannot convert float NaN to integer".to_owned())
        }
        PyValue::Float(_) => {
            Err("OverflowError: cannot convert float infinity to integer".to_owned())
        }
        PyValue::Str(s) => crate::settings::py_int(s).ok_or_else(|| {
            format!(
                "ValueError: invalid literal for int() with base 10: {}",
                baskfy_providers::py::repr_str(s)
            )
        }),
        other => Err(format!(
            "TypeError: int() argument must be a string, a bytes-like object or a real number, \
             not '{}'",
            other.type_name()
        )),
    }
}

/// `_decimal(value)`: `Decimal(str(value))`, `None` for `None` or what `Decimal` refuses.
/// (A NaN or an infinity, which `Decimal` accepts and the strategy's comparisons would then
/// raise on, is `None` here — DECISIONS-RS R8.SM4.)
#[must_use]
pub fn py_decimal_value(value: Option<&PyValue>) -> Option<Decimal> {
    match value? {
        PyValue::None | PyValue::Bool(_) => None,
        PyValue::Int(i) => Some(Decimal::from(*i)),
        PyValue::Float(f) if f.is_finite() => Some(dec_of_float(*f)),
        PyValue::Str(s) => dec(s.trim()),
        _ => None,
    }
}

/// `datetime.fromisoformat(text)` as the strategy reads a string stamp, with the zone dropped
/// (`.replace(tzinfo=None)`).
#[must_use]
pub fn fromisoformat_naive(text: &str) -> Option<NaiveDateTime> {
    crate::lanes::twt::common::py_fromisoformat_naive(text)
}

/// `_tick_time(tick, day)`: the first of `exchange_timestamp`, `last_trade_time`, `timestamp`
/// that is a datetime (its zone dropped) or an ISO string; else `wall` (Python's
/// `datetime.combine(day, datetime.now().time())` — the caller hands today's wall clock).
#[must_use]
pub fn tick_time(tick: &PyDict, wall: NaiveDateTime) -> NaiveDateTime {
    for key in ["exchange_timestamp", "last_trade_time", "timestamp"] {
        match tick.get(key) {
            Some(PyValue::DateTime(PyDateTime::Naive(t))) => return *t,
            Some(PyValue::DateTime(PyDateTime::Aware(t))) => return t.naive_local(),
            Some(PyValue::Str(s)) => {
                if let Some(t) = fromisoformat_naive(s) {
                    return t;
                }
            }
            _ => {}
        }
    }
    wall
}

/// The tick dict as `SwingBreakout.on_tick` reads it: `int(tick.get("instrument_token") or 0)`,
/// `_decimal(last_price)`, `_decimal((ohlc or {}).get("low"))`, `_tick_time`.
///
/// # Errors
/// A token Python's `int()` refuses.
pub fn tick_of(tick: &PyDict, wall: NaiveDateTime) -> Result<Tick, String> {
    let none = PyValue::None;
    let zero = PyValue::Int(0);
    let token = py_int_value(tick.get("instrument_token").unwrap_or(&none).or(&zero))?;
    let low = match tick.get("ohlc").unwrap_or(&none) {
        PyValue::Map(o) => py_decimal_value(o.get("low")),
        other if other.truthy() => {
            return Err(format!(
                "AttributeError: '{}' object has no attribute 'get'",
                other.type_name()
            ));
        }
        _ => None,
    };
    Ok(Tick {
        instrument_token: token,
        last_price: py_decimal_value(tick.get("last_price")),
        day_low: low,
        at: tick_time(tick, wall),
    })
}

// =============================================================================================
// B10: the quote fallback
// =============================================================================================

/// The Kite read `QuoteFallback` makes (`Kite.quote_raw`).
#[async_trait]
pub trait QuoteReader: Send + Sync {
    /// Kite's `quote` for pre-keyed instruments.
    async fn quote_raw(&self, keys: &[String]) -> Result<PyValue, String>;
}

/// `QuoteFallback` (B10): Kite `/quote` for the watchlist, at most once every `min_interval`
/// seconds — the cap is the object's, not the loop's.
pub struct QuoteFallback {
    /// token → symbol, in insertion order (`dict`).
    pub symbols_by_token: Vec<(i64, String)>,
    /// The cap, seconds.
    pub min_interval: f64,
    last_call: Option<f64>,
    /// Calls actually made.
    pub calls: i64,
}

impl QuoteFallback {
    /// Over the watched names, capped at `min_interval` (default `quote_poll_min_seconds`).
    #[must_use]
    pub fn new(symbols_by_token: BTreeMap<i64, String>, min_interval: Option<f64>) -> Self {
        Self::ordered(symbols_by_token.into_iter().collect(), min_interval)
    }

    /// As [`QuoteFallback::new`] with the dict's insertion order given.
    #[must_use]
    pub fn ordered(symbols_by_token: Vec<(i64, String)>, min_interval: Option<f64>) -> Self {
        let mut deduped: Vec<(i64, String)> = Vec::new();
        for (t, s) in symbols_by_token {
            if let Some(slot) = deduped.iter_mut().find(|(k, _)| *k == t) {
                slot.1 = s;
            } else {
                deduped.push((t, s));
            }
        }
        Self {
            symbols_by_token: deduped,
            min_interval: min_interval
                .unwrap_or(SwingConfig::default().opening_range.quote_poll_min_seconds),
            last_call: None,
            calls: 0,
        }
    }

    /// `symbols_by_token[token] = symbol`.
    pub fn insert(&mut self, token: i64, symbol: String) {
        if let Some(slot) = self.symbols_by_token.iter_mut().find(|(t, _)| *t == token) {
            slot.1 = symbol;
        } else {
            self.symbols_by_token.push((token, symbol));
        }
    }

    /// `symbols_by_token.pop(token, None)`.
    pub fn remove(&mut self, token: i64) {
        self.symbols_by_token.retain(|(t, _)| *t != token);
    }

    /// `due()`.
    #[must_use]
    pub fn due(&self, monotonic: f64) -> bool {
        self.last_call
            .is_none_or(|last| monotonic - last >= self.min_interval)
    }

    /// `poll(now)`: the watchlist's quotes as ticks in the ticker's own shape
    /// (`instrument_token`, `last_price`, `ohlc`, `exchange_timestamp`), or `None` when a call is
    /// not yet allowed. A failed chunk is a missed poll, not a halt.
    ///
    /// # Errors
    /// A row whose `instrument_token` Python's `int()` refuses (it raises out of `poll`).
    pub async fn poll(
        &mut self,
        reader: &dyn QuoteReader,
        now: NaiveDateTime,
        monotonic: f64,
    ) -> Result<Option<Vec<PyDict>>, String> {
        if !self.due(monotonic) || self.symbols_by_token.is_empty() {
            return Ok(None);
        }
        self.last_call = Some(monotonic);
        self.calls += 1;
        let mut ticks = Vec::new();
        let keys: Vec<String> = self
            .symbols_by_token
            .iter()
            .map(|(_, s)| format!("NSE:{s}"))
            .collect();
        for chunk in keys.chunks(500) {
            let data = match reader.quote_raw(chunk).await {
                Ok(data) => data,
                Err(e) => {
                    log_line!("WARNING swing_monitor quote fallback failed: {e}");
                    crate::lanes::session::telemetry::count(
                        "swing_quote_polls",
                        &[("outcome", "failed")],
                    );
                    continue;
                }
            };
            crate::lanes::session::telemetry::count("swing_quote_polls", &[("outcome", "ok")]);
            let PyValue::Map(data) = data else {
                // `data.values()` on anything but a dict is Python's AttributeError.
                return Err(format!(
                    "AttributeError: '{}' object has no attribute 'values'",
                    data.type_name()
                ));
            };
            let none = PyValue::None;
            for (_, row) in data.iter() {
                let PyValue::Map(row) = row else {
                    return Err(format!(
                        "AttributeError: '{}' object has no attribute 'get'",
                        row.type_name()
                    ));
                };
                let token = row.get("instrument_token").unwrap_or(&none);
                let price = row.get("last_price").unwrap_or(&none);
                if matches!(token, PyValue::None) || matches!(price, PyValue::None) {
                    continue;
                }
                let token = py_int_value(token)?;
                let empty = PyValue::Map(PyDict::default());
                let now_value = PyValue::DateTime(PyDateTime::Naive(now));
                let stamp = row
                    .get("timestamp")
                    .unwrap_or(&none)
                    .or(row.get("last_trade_time").unwrap_or(&none))
                    .or(&now_value)
                    .clone();
                let mut tick = PyDict::default();
                tick.insert("instrument_token", PyValue::Int(token));
                tick.insert("last_price", price.clone());
                tick.insert("ohlc", row.get("ohlc").unwrap_or(&none).or(&empty).clone());
                tick.insert("exchange_timestamp", stamp);
                ticks.push(tick);
            }
        }
        Ok(Some(ticks))
    }
}

/// `wait_for_session` (LV4): poll for a Kite session until `until` (IST); true the moment one
/// exists. A check that fails is "not yet".
pub async fn wait_for_session<A, FA, N, S, F>(
    mut authed: A,
    mut now: N,
    mut sleep: S,
    until: NaiveTime,
    poll_seconds: f64,
) -> bool
where
    A: FnMut() -> FA,
    FA: std::future::Future<Output = Result<bool, String>>,
    N: FnMut() -> DateTime<FixedOffset>,
    S: FnMut(f64) -> F,
    F: std::future::Future<Output = ()>,
{
    loop {
        match authed().await {
            Ok(true) => return true,
            Ok(false) => {}
            Err(e) => log_line!("WARNING swing_monitor Kite session check failed: {e}"),
        }
        let moment = now().with_timezone(&ist());
        if moment.time() >= until {
            return false;
        }
        log_line!(
            "INFO swing_monitor no Kite session yet; the monitor waits (until {} IST)",
            until.format("%H:%M:%S")
        );
        sleep(poll_seconds).await;
    }
}

/// `_chores` (SW26): the cutoff at `pending_cutoff_at` and the GTT sweep at `gtt_sweep_at`,
/// each once, from inside the watch.
#[derive(Debug, Default, Clone)]
pub struct Chores {
    done: std::collections::BTreeSet<String>,
}

impl Chores {
    /// The chores due at `moment` that have not fired yet, in order (cutoff before sweep);
    /// each is marked done as it is returned.
    pub fn due(&mut self, moment: NaiveDateTime) -> Vec<&'static str> {
        let window = SwingConfig::default().opening_range;
        let mut out = Vec::new();
        for (name, (hour, minute)) in [
            ("cutoff", window.pending_cutoff_at),
            ("gtt_sweep", window.gtt_sweep_at),
        ] {
            let at = NaiveTime::from_hms_opt(
                u32::try_from(hour).unwrap_or(0),
                u32::try_from(minute).unwrap_or(0),
                0,
            )
            .unwrap_or_default();
            if self.done.contains(name) || moment.time() < at {
                continue;
            }
            self.done.insert(name.to_owned());
            out.push(name);
        }
        out
    }
}

/// `record_monitor_ran`: `sw_session.monitor_ran` for today (the mode from the execution flag
/// alone, as Python writes it).
///
/// # Errors
/// The driver's.
pub async fn record_monitor_ran(
    conn: &mut Conn,
    user_id: i64,
    day: NaiveDate,
    signals: i64,
    settings: &Settings,
) -> Result<(), StorageError> {
    conn.execute(
        &format!(
            "INSERT INTO {SCHEMA}.sw_session (user_id, session_date, mode, monitor_ran, signals, \
             notes) VALUES (?, ?, ?, true, ?, 'swing-monitor') \
             ON CONFLICT (user_id, session_date) DO UPDATE SET monitor_ran = true, \
             signals = EXCLUDED.signals"
        ),
        &[
            Param::Int(user_id),
            Param::Date(day),
            Param::Text(
                if settings.swing_execution_enabled {
                    "LIVE"
                } else {
                    "DRY_RUN"
                }
                .to_owned(),
            ),
            Param::Int(signals),
        ],
    )
    .await?;
    Ok(())
}

/// `heartbeat_writer`: the `swing_monitor` row of `lv_heartbeat`.
///
/// # Errors
/// The driver's.
pub async fn write_heartbeat(
    conn: &mut Conn,
    user_id: i64,
    detail: &str,
    at: DateTime<FixedOffset>,
) -> Result<(), StorageError> {
    let detail: String = detail.chars().take(2000).collect();
    conn.execute(
        &format!(
            "INSERT INTO {SCHEMA}.lv_heartbeat (user_id, process, state, detail, at) \
             VALUES (?, 'swing_monitor', 'watching', ?, ?) ON CONFLICT (user_id, process) \
             DO UPDATE SET state = EXCLUDED.state, detail = EXCLUDED.detail, at = EXCLUDED.at"
        ),
        &[Param::Int(user_id), Param::Text(detail), Param::Aware(at)],
    )
    .await?;
    Ok(())
}

/// `monitor_metrics_port`: `DESK_MONITOR_METRICS_PORT` (stripped), else `METRICS_PORT + 1`,
/// else none (metrics off).
///
/// # Errors
/// Python's `ValueError` for a value `int()` refuses — `main` dies before anything else runs.
pub fn monitor_metrics_port(own: Option<&str>, metrics_port: &str) -> Result<Option<i64>, String> {
    let invalid = |v: &str| {
        format!(
            "ValueError: invalid literal for int() with base 10: {}",
            baskfy_providers::py::repr_str(v)
        )
    };
    if let Some(v) = own.map(str::trim).filter(|v| !v.is_empty()) {
        return crate::settings::py_int(v)
            .map(Some)
            .ok_or_else(|| invalid(v));
    }
    if metrics_port.is_empty() {
        return Ok(None);
    }
    crate::settings::py_int(metrics_port)
        .map(|p| Some(p + 1))
        .ok_or_else(|| invalid(metrics_port))
}

/// `Decimal(str(x))` for a polled price.
#[must_use]
pub fn tick_price(x: f64) -> Decimal {
    dec_of_float(x)
}

/// The store as the loop sees it: a [`SignalStore`] that may hold SW25's queue and SW30's
/// gate. A store with neither (the replay harness's) answers empty, and the monitor still
/// watches the tape.
#[async_trait]
pub trait MonitorStore: SignalStore {
    /// Take every `(signal, planned)` the store recorded for auto-execute since the last pass.
    fn take_pending(&mut self) -> Vec<(Signal, Planned)> {
        Vec::new()
    }

    /// `store.read_context()` — SW30's gate re-read for the heartbeat.
    ///
    /// # Errors
    /// The store's.
    async fn reread_gate(&mut self) -> Result<(), String> {
        Ok(())
    }

    /// `gate_line(store.context)`, when the store holds a context.
    fn gate_clause(&self) -> Option<String> {
        None
    }
}

#[async_trait]
impl MonitorStore for PgSignalStore {
    fn take_pending(&mut self) -> Vec<(Signal, Planned)> {
        std::mem::take(&mut self.pending_confirms)
    }

    async fn reread_gate(&mut self) -> Result<(), String> {
        self.read_context()
            .await
            .map(|_| ())
            .map_err(|e| e.to_string())
    }

    fn gate_clause(&self) -> Option<String> {
        self.context.as_ref().map(gate_line)
    }
}

/// The ticker's bus as the loop reads it: one queue per subscribed token, Python's dicts.
pub trait TickBus: Send {
    /// `bus.subscribe(token)`.
    fn subscribe(&mut self, token: i64);
    /// Everything queued for `token` now (`while not q.empty(): q.get_nowait()`).
    fn drain(&mut self, token: i64) -> Vec<PyDict>;
}

/// The loop's seams (`run_until_close`'s keyword collaborators): the clocks, the sleep, the
/// chores, the first-tick mark, the reload, the ticker's (un)subscribe and the heartbeat. Every
/// one but the clocks may fail; a failure is logged and the watch goes on.
#[async_trait]
pub trait WatchHooks: Send {
    /// `now()`: naive IST.
    fn now(&mut self) -> NaiveDateTime;
    /// `clock()`: monotonic seconds.
    fn monotonic(&mut self) -> f64;
    /// `asyncio.sleep(poll_seconds)`.
    async fn sleep(&mut self, seconds: f64);
    /// `run_chore(name, day)` (SW26).
    async fn run_chore(&mut self, name: &str, day: NaiveDate) -> Result<(), String>;
    /// `on_first_tick()` (SW11.2).
    async fn on_first_tick(&mut self) -> Result<(), String> {
        Ok(())
    }
    /// Whether a `reload` is wired (LV4).
    fn has_reload(&self) -> bool {
        false
    }
    /// `reload()`: the current watchlist.
    async fn reload(&mut self) -> Result<Vec<WatchedName>, String> {
        Err("no reload".to_owned())
    }
    /// `subscribe(tokens)` on the ticker.
    fn subscribe(&mut self, tokens: &[i64]) -> Result<(), String> {
        let _ = tokens;
        Ok(())
    }
    /// `unsubscribe(tokens)` on the ticker.
    fn unsubscribe(&mut self, tokens: &[i64]) -> Result<(), String> {
        let _ = tokens;
        Ok(())
    }
    /// Whether a heartbeat is wired.
    fn has_heartbeat(&self) -> bool {
        false
    }
    /// `heartbeat(detail)`; the store is handed in so SW30's gated beat can re-read its gate.
    async fn heartbeat(
        &mut self,
        store: &mut dyn MonitorStore,
        detail: &str,
    ) -> Result<(), String> {
        let _ = (store, detail);
        Ok(())
    }
}

/// `run_until_close`'s tunables, Python's defaults in [`LoopTimings::default`].
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct LoopTimings {
    /// `poll_seconds`.
    pub poll_seconds: f64,
    /// `reload_every_seconds`.
    pub reload_every_seconds: f64,
    /// `heartbeat_every_seconds`.
    pub heartbeat_every_seconds: f64,
}

impl Default for LoopTimings {
    fn default() -> Self {
        Self {
            poll_seconds: 1.0,
            reload_every_seconds: 60.0,
            heartbeat_every_seconds: 15.0,
        }
    }
}

/// The quote fallback with its reader, as the loop holds it.
pub struct Quotes<'a> {
    /// The capped fallback.
    pub fallback: QuoteFallback,
    /// Kite's `quote`, read-only.
    pub reader: &'a dyn QuoteReader,
}

/// `run_until_close`: consume the bus until `monitor_close`; the number of signals raised.
///
/// Per pass, in Python's order: stop past `monitor_close`; drain every queue through the
/// strategy; SW25's queue ([`drain_auto_execute`] — sent only when all three flags say so);
/// the chores at their hours ([`Chores`]); the heartbeat; the reload when due; then, if
/// nothing arrived and the ticker has been quiet for `quote_poll_min_seconds`, the B10 fallback
/// (itself capped), and the sleep.
///
/// # Errors
/// What Python's loop lets escape: a tick whose token `int()` refuses (pushed or polled), or a
/// quote answer that is not a dict of dicts.
#[allow(clippy::too_many_arguments)]
pub async fn run_until_close(
    strategy: &mut SwingBreakout,
    store: &mut dyn MonitorStore,
    candles: &dyn CandleSource,
    bus: &mut dyn TickBus,
    hooks: &mut dyn WatchHooks,
    mut quotes: Option<Quotes<'_>>,
    settings: &Settings,
    executor: &dyn AutoConfirm,
    timings: LoopTimings,
) -> Result<usize, String> {
    let mut chores = Chores::default();
    let mut queues: Vec<i64> = strategy.tokens();
    for token in &queues {
        bus.subscribe(*token);
    }
    crate::lanes::session::telemetry::gauge("swing_monitor_up", 1.0, &[]);
    let result = watch(
        strategy,
        store,
        candles,
        bus,
        hooks,
        &mut quotes,
        settings,
        executor,
        timings,
        &mut chores,
        &mut queues,
    )
    .await;
    crate::lanes::session::telemetry::gauge("swing_monitor_up", 0.0, &[]);
    result
}

#[allow(clippy::too_many_arguments)]
async fn watch(
    strategy: &mut SwingBreakout,
    store: &mut dyn MonitorStore,
    candles: &dyn CandleSource,
    bus: &mut dyn TickBus,
    hooks: &mut dyn WatchHooks,
    quotes: &mut Option<Quotes<'_>>,
    settings: &Settings,
    executor: &dyn AutoConfirm,
    timings: LoopTimings,
    chores: &mut Chores,
    queues: &mut Vec<i64>,
) -> Result<usize, String> {
    let quiet_for = SwingConfig::default().opening_range.quote_poll_min_seconds;
    let mut last_tick_at = hooks.monotonic();
    let mut last_reload_at = hooks.monotonic();
    let mut last_beat_at: Option<f64> = None;
    let mut first_tick_seen = false;
    loop {
        let moment = hooks.now();
        if strategy.session_over(moment) {
            log_line!(
                "INFO swing_monitor monitor close reached; {} signals raised",
                strategy.signals.len()
            );
            break;
        }
        let mut drained = false;
        for token in queues.clone() {
            for tick in bus.drain(token) {
                handle(
                    strategy,
                    store,
                    candles,
                    hooks,
                    &tick,
                    moment,
                    &mut first_tick_seen,
                )
                .await?;
                drained = true;
            }
        }
        let mut pending = store.take_pending();
        drain_auto_execute(&mut pending, settings, executor).await;
        for name in chores.due(moment) {
            if let Err(e) = hooks.run_chore(name, moment.date()).await {
                log_line!(
                    "ERROR swing_monitor clock: the {name} chore failed inside the watch: {e}"
                );
            }
        }
        if hooks.has_heartbeat()
            && last_beat_at
                .is_none_or(|at| hooks.monotonic() - at >= timings.heartbeat_every_seconds)
        {
            last_beat_at = Some(hooks.monotonic());
            let detail = format!(
                "watching {} name(s), {} signal(s) raised, at {}",
                strategy.watchlist.len(),
                strategy.signals.len(),
                moment.format("%H:%M:%S")
            );
            if let Err(e) = hooks.heartbeat(store, &detail).await {
                log_line!("ERROR swing_monitor could not write the monitor heartbeat: {e}");
            }
        }
        if hooks.has_reload() && hooks.monotonic() - last_reload_at >= timings.reload_every_seconds
        {
            last_reload_at = hooks.monotonic();
            reload_watchlist(strategy, bus, hooks, queues, quotes.as_mut()).await;
        }
        if drained {
            last_tick_at = hooks.monotonic();
            continue;
        }
        if let Some(q) = quotes.as_mut()
            && hooks.monotonic() - last_tick_at >= quiet_for
        {
            let mono = hooks.monotonic();
            let polled = q.fallback.poll(q.reader, moment, mono).await?;
            for tick in polled.unwrap_or_default() {
                handle(
                    strategy,
                    store,
                    candles,
                    hooks,
                    &tick,
                    moment,
                    &mut first_tick_seen,
                )
                .await?;
            }
            let mut pending = store.take_pending();
            drain_auto_execute(&mut pending, settings, executor).await;
        }
        hooks.sleep(timings.poll_seconds).await;
    }
    Ok(strategy.signals.len())
}

async fn handle(
    strategy: &mut SwingBreakout,
    store: &mut dyn MonitorStore,
    candles: &dyn CandleSource,
    hooks: &mut dyn WatchHooks,
    tick: &PyDict,
    wall: NaiveDateTime,
    first_tick_seen: &mut bool,
) -> Result<(), String> {
    let read = tick_of(tick, wall)?;
    strategy.on_tick(&read, store, candles).await;
    if !*first_tick_seen {
        *first_tick_seen = true;
        if let Err(e) = hooks.on_first_tick().await {
            log_line!("ERROR swing_monitor could not record the monitor's start: {e}");
        }
    }
    Ok(())
}

/// `_reload_watchlist` (LV4): diff the list against what is watched; add, subscribe, drop,
/// unsubscribe. A reload that fails keeps the list as it was.
async fn reload_watchlist(
    strategy: &mut SwingBreakout,
    bus: &mut dyn TickBus,
    hooks: &mut dyn WatchHooks,
    queues: &mut Vec<i64>,
    mut quotes: Option<&mut Quotes<'_>>,
) {
    let fresh = match hooks.reload().await {
        Ok(list) => list,
        Err(e) => {
            log_line!(
                "WARNING swing_monitor watchlist reload failed; keeping the current list: {e}"
            );
            return;
        }
    };
    // `{w.token: w for w in reload()}`: the last row for a token wins, first-seen order kept.
    let mut by_token: Vec<(i64, WatchedName)> = Vec::new();
    for w in fresh {
        if let Some(slot) = by_token.iter_mut().find(|(t, _)| *t == w.token) {
            slot.1 = w;
        } else {
            by_token.push((w.token, w));
        }
    }
    let added: Vec<WatchedName> = by_token
        .iter()
        .filter(|(t, _)| !strategy.watchlist.contains_key(t))
        .map(|(_, w)| w.clone())
        .collect();
    // `list(strategy.watchlist)`: the dict's insertion order, which `tokens()` keeps.
    let gone: Vec<i64> = strategy
        .tokens()
        .into_iter()
        .filter(|t| !by_token.iter().any(|(f, _)| f == t))
        .collect();
    for w in &added {
        if strategy.add_watch(w.clone()) {
            bus.subscribe(w.token);
            if !queues.contains(&w.token) {
                queues.push(w.token);
            }
            if let Some(q) = quotes.as_deref_mut() {
                q.fallback.insert(w.token, w.symbol.clone());
            }
        }
    }
    for token in &gone {
        if strategy.drop_watch(*token) {
            queues.retain(|t| t != token);
            if let Some(q) = quotes.as_deref_mut() {
                q.fallback.remove(*token);
            }
        }
    }
    if !added.is_empty() {
        let tokens: Vec<i64> = added.iter().map(|w| w.token).collect();
        if let Err(e) = hooks.subscribe(&tokens) {
            log_line!(
                "WARNING swing_monitor could not subscribe {} new name(s) on the ticker: {e}",
                added.len()
            );
        }
    }
    if !gone.is_empty()
        && let Err(e) = hooks.unsubscribe(&gone)
    {
        log_line!(
            "WARNING swing_monitor could not unsubscribe {} name(s) on the ticker: {e}",
            gone.len()
        );
    }
    if !added.is_empty() || !gone.is_empty() {
        let plus: Vec<&str> = added.iter().map(|w| w.symbol.as_str()).collect();
        let minus: Vec<String> = gone.iter().map(ToString::to_string).collect();
        log_line!(
            "INFO swing_monitor watchlist reloaded: +{} -{} ({} watched)",
            if plus.is_empty() {
                "none".to_owned()
            } else {
                plus.join(", ")
            },
            if minus.is_empty() {
                "none".to_owned()
            } else {
                minus.join(", ")
            },
            strategy.watchlist.len()
        );
    }
}

/// `gated_heartbeat` (SW30): at most once every `every_seconds` the gate is re-read, and every
/// beat names the gate in force and from which data.
#[derive(Debug, Clone)]
pub struct GatedHeartbeat {
    /// `every_seconds`.
    pub every_seconds: f64,
    last: Option<f64>,
}

impl GatedHeartbeat {
    /// Re-read at most every `every_seconds` (Python's default 60).
    #[must_use]
    pub const fn new(every_seconds: f64) -> Self {
        Self {
            every_seconds,
            last: None,
        }
    }

    /// The beat's text, `"{detail} · {gate_line(store.context)}"`; re-reads the store's
    /// context first when due.
    ///
    /// # Errors
    /// The store's re-read (Python's `wrapped` raises it into `_beat`, which logs it and writes
    /// no beat).
    pub async fn line(
        &mut self,
        store: &mut dyn MonitorStore,
        detail: &str,
        monotonic: f64,
    ) -> Result<String, String> {
        if self
            .last
            .is_none_or(|at| monotonic - at >= self.every_seconds)
        {
            self.last = Some(monotonic);
            store.reread_gate().await?;
        }
        Ok(match store.gate_clause() {
            Some(clause) => format!("{detail} · {clause}"),
            None => detail.to_owned(),
        })
    }

    /// Whether the next beat re-reads.
    #[must_use]
    pub fn due(&self, monotonic: f64) -> bool {
        self.last
            .is_none_or(|at| monotonic - at >= self.every_seconds)
    }
}
