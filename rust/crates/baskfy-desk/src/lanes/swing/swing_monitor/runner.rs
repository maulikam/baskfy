//! Ports the runner half of `app/swing_monitor.py` (SW6, SW10, SW11, SW25, SW26, SW30, LV4):
//! the flag ([`build_monitor`] builds nothing with `BASKFY_SWING_MONITOR_ENABLED` off), the
//! signal store ([`PgSignalStore`]: an `sw_signal` row and a one-line `sw_plan(SIGNAL)` per
//! trigger, sized by the same `build_entries` the evening uses, re-read per trigger), the
//! notice to the daily focus, SW25's auto-execute queue ([`drain_auto_execute`]: drained every
//! pass, **sent only when all three flags say so**), the quote fallback's cap
//! ([`QuoteFallback`]), the late-login wait ([`wait_for_session`]), the chores at their own
//! hours inside the watch ([`Chores`]) and the `monitor_ran` mark.
//!
//! **No order path lives here.** The store records lines; the strategy raises signals; the
//! only thing that can confirm a line is [`drain_auto_execute`] handing it to an
//! [`AutoConfirm`] — which the desk wires to the same `execute_line` a click reaches, gateway
//! and guards included — and only when `auto_execute_enabled()` is true.

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
    PLAN_TTL_MINUTES, SwingPlan, assemble, dec_of_float, gate_str, kind_str, py_dec, skip_str,
    trail_str,
};
use crate::lanes::swing::store::SCHEMA;
use crate::lanes::swing::values::{col, int, raw_dec, text};
use crate::settings::Settings;
use crate::storage::{Conn, DbValue, Param, StorageError};
use crate::wire::PyValue;
use baskfy_providers::records::PyDateTime;

/// `SESSION_WAIT_UNTIL` (LV4): a login after 15:20 is tomorrow's.
#[must_use]
pub fn session_wait_until() -> NaiveTime {
    NaiveTime::from_hms_opt(15, 20, 0).unwrap_or_default()
}

/// `SESSION_POLL_SECONDS`.
pub const SESSION_POLL_SECONDS: f64 = 30.0;

/// `load_config`: `SwingConfig::default()` with the person's three liquidity floors and three
/// sizing knobs.
///
/// # Errors
/// The driver's.
pub async fn load_config(
    conn: &mut Conn,
    user_id: i64,
    schema: &str,
) -> Result<SwingConfig, StorageError> {
    let table = if schema.is_empty() {
        "sw_config".to_owned()
    } else {
        format!("{schema}.sw_config")
    };
    let row = conn
        .execute(
            &format!(
                "SELECT adr_min_pct, turnover_min_inr, price_min, risk_per_trade_pct, \
                 max_position_pct, max_open_positions FROM {table} WHERE user_id = ?"
            ),
            &[Param::Int(user_id)],
        )
        .await?
        .rows
        .into_iter()
        .next();
    let mut config = SwingConfig::default();
    let Some(r) = row else { return Ok(config) };
    let f = |name: &str| match col(&r, name) {
        Some(DbValue::Float(x)) => *x,
        #[allow(clippy::cast_precision_loss)]
        Some(DbValue::Int(i)) => *i as f64,
        Some(DbValue::Text(s)) => s.trim().parse().unwrap_or(0.0),
        _ => 0.0,
    };
    config.liquidity = baskfy_core::swing::setups::LiquidityConfig::default();
    config.liquidity.adr_min_pct = f("adr_min_pct");
    config.liquidity.turnover_min_inr = f("turnover_min_inr");
    config.liquidity.price_min = f("price_min");
    config.sizing = baskfy_core::swing::sizing::SizingConfig::default();
    config.sizing.risk_per_trade_pct = f("risk_per_trade_pct");
    config.sizing.max_position_pct = f("max_position_pct");
    config.sizing.max_open_positions = int(col(&r, "max_open_positions"));
    Ok(config)
}

fn setup_of(value: &str) -> Option<Setup> {
    match value {
        "FLAG" => Some(Setup::Flag),
        "EP" => Some(Setup::Ep),
        "PARABOLIC_SHORT" => Some(Setup::ParabolicShort),
        _ => None,
    }
}

/// `load_watchlist`: every `WATCHING` row with a Kite token. A FLAG's pivot is its trigger; an
/// EP's is the gap itself. A name with no token is skipped with a warning.
///
/// # Errors
/// The driver's.
pub async fn load_watchlist(
    conn: &mut Conn,
    user_id: i64,
    circuits: &BTreeMap<String, Decimal>,
) -> Result<Vec<WatchedName>, StorageError> {
    let rows = conn
        .execute(
            &format!(
                "SELECT w.id, w.instrument_id, i.symbol, i.kite_token, w.setup, w.trigger, w.focus \
                 FROM {SCHEMA}.sw_watch w JOIN {SCHEMA}.instrument i ON i.id = w.instrument_id \
                 WHERE w.user_id = ? AND w.state = 'WATCHING' ORDER BY i.symbol"
            ),
            &[Param::Int(user_id)],
        )
        .await?
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
        let Some(setup) = setup_of(&text(col(&r, "setup"))) else {
            continue;
        };
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

fn uuid_text(bytes: [u8; 16]) -> String {
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
            log_line!(
                "INFO swing_monitor gate {} -> {} · {}",
                gate_str(b),
                gate_str(context.gate),
                gate_line(&context)
            );
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
        let plan_pk = plan_row.rows.first().map_or(0, |r| int(col(r, "id")));
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
            line_id = row.rows.first().map(|r| int(col(r, "id")));
        }
        Ok(Planned {
            line_id,
            plan,
            expires_at,
            plan_id: Some(plan_id),
        })
    }

    fn notify(&mut self, signal: &Signal, raised_at: DateTime<FixedOffset>, planned: &Planned) {
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
            n.notify(&notice);
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
        self.notify(signal, raised, &planned);
        Ok(())
    }
}

/// What confirms a line for auto-execute: the desk wires this to `execute_line` with the swing
/// gateway, the price the tick carried and `confirm="true"` — exactly what a click reaches.
#[async_trait]
pub trait AutoConfirm: Send + Sync {
    /// Confirm one line; the outcome's status, or the error's words.
    async fn confirm(
        &self,
        plan_id: &str,
        line_id: i64,
        last_price: Decimal,
    ) -> Result<String, String>;
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
            Ok(status) => {
                log_line!("WARNING swing_monitor AUTO-EXECUTE {symbol}: {status}");
                done.push((symbol, status));
            }
            Err(e) => {
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

/// `build_monitor`: the strategy, or `None` with the flag off — nothing constructed.
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
        return Ok(None);
    }
    SwingBreakout::new(watchlist, day, window_minutes, config).map(Some)
}

/// The Kite read `QuoteFallback` makes (`Kite.quote_raw`).
#[async_trait]
pub trait QuoteReader: Send + Sync {
    /// Kite's `quote` for pre-keyed instruments.
    async fn quote_raw(&self, keys: &[String]) -> Result<PyValue, String>;
}

/// `QuoteFallback` (B10): Kite `/quote` for the watchlist, at most once every `min_interval`
/// seconds — the cap is the object's, not the loop's.
pub struct QuoteFallback {
    /// token → symbol.
    pub symbols_by_token: BTreeMap<i64, String>,
    /// The cap, seconds.
    pub min_interval: f64,
    last_call: Option<f64>,
    /// Calls actually made.
    pub calls: i64,
}

fn num(v: Option<&PyValue>) -> Option<Decimal> {
    match v {
        Some(PyValue::Float(f)) => Some(dec_of_float(*f)),
        Some(PyValue::Int(i)) => Some(Decimal::from(*i)),
        _ => None,
    }
}

/// `_tick_time`'s reading of a polled stamp: a datetime as naive exchange-local, an ISO string
/// parsed, else `None` (the caller falls through).
fn stamp_of(v: Option<&PyValue>) -> Option<NaiveDateTime> {
    match v {
        Some(PyValue::DateTime(PyDateTime::Naive(t))) => Some(*t),
        Some(PyValue::DateTime(PyDateTime::Aware(t))) => Some(t.naive_local()),
        Some(PyValue::Str(s)) => NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S")
            .or_else(|_| NaiveDateTime::parse_from_str(s, "%Y-%m-%dT%H:%M:%S"))
            .ok()
            .or_else(|| {
                DateTime::parse_from_rfc3339(s)
                    .ok()
                    .map(|t| t.naive_local())
            }),
        _ => None,
    }
}

impl QuoteFallback {
    /// Over the watched names, capped at `min_interval` (default `quote_poll_min_seconds`).
    #[must_use]
    pub fn new(symbols_by_token: BTreeMap<i64, String>, min_interval: Option<f64>) -> Self {
        Self {
            symbols_by_token,
            min_interval: min_interval
                .unwrap_or(SwingConfig::default().opening_range.quote_poll_min_seconds),
            last_call: None,
            calls: 0,
        }
    }

    /// `due()`.
    #[must_use]
    pub fn due(&self, monotonic: f64) -> bool {
        self.last_call
            .is_none_or(|last| monotonic - last >= self.min_interval)
    }

    /// `poll(now)`: the watchlist's quotes as ticks the strategy cannot tell from pushed ones,
    /// or `None` when a call is not yet allowed. A failed chunk is a missed poll, not a halt.
    pub async fn poll(
        &mut self,
        reader: &dyn QuoteReader,
        now: NaiveDateTime,
        monotonic: f64,
    ) -> Option<Vec<Tick>> {
        if !self.due(monotonic) || self.symbols_by_token.is_empty() {
            return None;
        }
        self.last_call = Some(monotonic);
        self.calls += 1;
        let mut ticks = Vec::new();
        let tokens: Vec<i64> = self.symbols_by_token.keys().copied().collect();
        for chunk in tokens.chunks(500) {
            let keys: Vec<String> = chunk
                .iter()
                .filter_map(|t| self.symbols_by_token.get(t))
                .map(|s| format!("NSE:{s}"))
                .collect();
            let data = match reader.quote_raw(&keys).await {
                Ok(PyValue::Map(data)) => data,
                Ok(_) => continue,
                Err(e) => {
                    log_line!("WARNING swing_monitor quote fallback failed: {e}");
                    continue;
                }
            };
            for (_, row) in data.iter() {
                let PyValue::Map(row) = row else {
                    continue;
                };
                let Some(token) = (match row.get("instrument_token") {
                    Some(PyValue::Int(t)) => Some(*t),
                    _ => None,
                }) else {
                    continue;
                };
                let Some(price) = num(row.get("last_price")) else {
                    continue;
                };
                let low = match row.get("ohlc") {
                    Some(PyValue::Map(o)) => num(o.get("low")),
                    _ => None,
                };
                let at = stamp_of(row.get("timestamp"))
                    .or_else(|| stamp_of(row.get("last_trade_time")))
                    .unwrap_or(now);
                ticks.push(Tick {
                    instrument_token: token,
                    last_price: Some(price),
                    day_low: low,
                    at,
                });
            }
        }
        Some(ticks)
    }
}

/// `wait_for_session` (LV4): poll for a Kite session until `until` (IST); true the moment one
/// exists.
pub async fn wait_for_session<A, N, S, F>(
    mut authed: A,
    mut now: N,
    mut sleep: S,
    until: NaiveTime,
    poll_seconds: f64,
) -> bool
where
    A: FnMut() -> Result<bool, String>,
    N: FnMut() -> DateTime<FixedOffset>,
    S: FnMut(f64) -> F,
    F: std::future::Future<Output = ()>,
{
    loop {
        if matches!(authed(), Ok(true)) {
            return true;
        }
        let moment = now().with_timezone(&ist());
        if moment.time() >= until {
            return false;
        }
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

/// `monitor_metrics_port`: `DESK_MONITOR_METRICS_PORT`, else `METRICS_PORT + 1`, else none.
#[must_use]
pub fn monitor_metrics_port(own: Option<&str>, metrics_port: i64) -> Option<i64> {
    if let Some(v) = own.map(str::trim).filter(|v| !v.is_empty()) {
        return v.parse().ok();
    }
    if metrics_port != 0 {
        Some(metrics_port + 1)
    } else {
        None
    }
}

/// `Decimal(str(x))` for a polled price.
#[must_use]
pub fn tick_price(x: f64) -> Decimal {
    dec_of_float(x)
}

/// The store as the loop sees it: a [`SignalStore`] that may hold SW25's queue. A store with
/// no queue (the replay harness's) answers empty, and the monitor still watches the tape.
pub trait MonitorStore: SignalStore {
    /// Take every `(signal, planned)` the store recorded for auto-execute since the last pass.
    fn take_pending(&mut self) -> Vec<(Signal, Planned)> {
        Vec::new()
    }
}

impl MonitorStore for PgSignalStore {
    fn take_pending(&mut self) -> Vec<(Signal, Planned)> {
        std::mem::take(&mut self.pending_confirms)
    }
}

/// The ticker's bus as the loop reads it: one queue per subscribed token.
pub trait TickBus: Send {
    /// `bus.subscribe(token)`.
    fn subscribe(&mut self, token: i64);
    /// Everything queued for `token` now (`while not q.empty(): q.get_nowait()`).
    fn drain(&mut self, token: i64) -> Vec<Tick>;
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
    /// `heartbeat(detail)`.
    async fn heartbeat(&mut self, detail: &str) -> Result<(), String> {
        let _ = detail;
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
) -> usize {
    let mut chores = Chores::default();
    let mut queues: Vec<i64> = strategy.tokens();
    for token in &queues {
        bus.subscribe(*token);
    }
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
                handle(strategy, store, candles, hooks, &tick, &mut first_tick_seen).await;
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
            if let Err(e) = hooks.heartbeat(&detail).await {
                log_line!("ERROR swing_monitor could not write the monitor heartbeat: {e}");
            }
        }
        if hooks.has_reload() && hooks.monotonic() - last_reload_at >= timings.reload_every_seconds
        {
            last_reload_at = hooks.monotonic();
            reload_watchlist(strategy, bus, hooks, &mut queues, quotes.as_mut()).await;
        }
        if drained {
            last_tick_at = hooks.monotonic();
            continue;
        }
        if let Some(q) = quotes.as_mut()
            && hooks.monotonic() - last_tick_at >= quiet_for
        {
            let mono = hooks.monotonic();
            let polled = q.fallback.poll(q.reader, moment, mono).await;
            for tick in polled.unwrap_or_default() {
                handle(strategy, store, candles, hooks, &tick, &mut first_tick_seen).await;
            }
            let mut pending = store.take_pending();
            drain_auto_execute(&mut pending, settings, executor).await;
        }
        hooks.sleep(timings.poll_seconds).await;
    }
    strategy.signals.len()
}

async fn handle(
    strategy: &mut SwingBreakout,
    store: &mut dyn MonitorStore,
    candles: &dyn CandleSource,
    hooks: &mut dyn WatchHooks,
    tick: &Tick,
    first_tick_seen: &mut bool,
) {
    strategy.on_tick(tick, store, candles).await;
    if !*first_tick_seen {
        *first_tick_seen = true;
        if let Err(e) = hooks.on_first_tick().await {
            log_line!("ERROR swing_monitor could not record the monitor's start: {e}");
        }
    }
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
    let gone: Vec<i64> = strategy
        .watchlist
        .keys()
        .copied()
        .filter(|t| !by_token.iter().any(|(f, _)| f == t))
        .collect();
    for w in &added {
        if strategy.add_watch(w.clone()) {
            bus.subscribe(w.token);
            if !queues.contains(&w.token) {
                queues.push(w.token);
            }
            if let Some(q) = quotes.as_deref_mut() {
                q.fallback
                    .symbols_by_token
                    .insert(w.token, w.symbol.clone());
            }
        }
    }
    for token in &gone {
        if strategy.drop_watch(*token) {
            queues.retain(|t| t != token);
            if let Some(q) = quotes.as_deref_mut() {
                q.fallback.symbols_by_token.remove(token);
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

    /// The beat's text; re-reads the store's context first when due.
    ///
    /// # Errors
    /// The store's re-read.
    pub async fn line(
        &mut self,
        store: &mut PgSignalStore,
        detail: &str,
        monotonic: f64,
    ) -> Result<String, StorageError> {
        if self
            .last
            .is_none_or(|at| monotonic - at >= self.every_seconds)
        {
            self.last = Some(monotonic);
            store.read_context().await?;
        }
        Ok(match &store.context {
            Some(c) => format!("{detail} · {}", gate_line(c)),
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
