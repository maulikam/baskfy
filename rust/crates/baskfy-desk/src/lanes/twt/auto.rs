//! `app/twt_auto.py` — TWT auto-execute: the sleeve confirms its own MORNING plan at the open,
//! and (LV8, TW19) a LIVE plan during the session; since LV9 (TW20) its kinds include
//! `SELL_AT_OPEN`.
//!
//! **Whose decision.** Maulik, in session, 21 Sep 2026: *"TWT has no auto-execute flag - this
//! should be implemented auto execute"* — the **second** named exception to non-negotiable 1
//! (DECISIONS-TW TW17; widened by Maulik in TW19 and TW20). The flag is
//! `BASKFY_TWT_AUTO_EXECUTE`; it **defaults false**, it exists only in the desk, and flipping it
//! is Maulik's hand. An agent may not default it true, widen what it does, or add another.
//!
//! **It removes the person pressing Confirm and nothing else.** Every line is sent by the same
//! [`execute::execute_line`] the button reaches, with `confirm="true"` and the plan's own id —
//! so the 30-minute expiry, the `PROPOSED` check, the session lock and re-size, the three-entry
//! cap, half size for the first ten live entries, a halt, `client_id` idempotency, the guards,
//! risk, rate limits, journal and the same-request GTT all still apply. It **never builds a
//! plan**: a missing, stale, EVENING or expired plan is refused and nothing is sent. Nothing
//! happens unless all THREE flags say so — `DRY_RUN=false`, `BASKFY_TWT_EXECUTION_ENABLED=true`
//! and `BASKFY_TWT_AUTO_EXECUTE=true` — read at call time, checked in [`run_once`] **and** in
//! [`drain_plan`] so no caller reaches the order path around it.

use std::sync::Arc;

use async_trait::async_trait;
use chrono::{DateTime, Datelike, FixedOffset, NaiveDate, NaiveTime};
use rust_decimal::Decimal;

use super::common::{Rec, py_isoformat, to_ist};
use super::execute::{self, ExecError, ExecOutcome, ExecResult};
use super::store::{StoreError, StoreResult, TwtStore};
use crate::app::AppState;
use crate::gateway::Gateway;
use crate::lanes::session::telemetry::count;
use crate::settings::Settings;
use crate::storage::StorageError;

/// `log.warning(...)` / `log.error(...)` under the `twt.auto` logger: stderr, the desk process's
/// log. Every line is composed here from a plan id, symbols, kinds, statuses and refusal words —
/// never a token or a price the operator did not already see on the page.
#[allow(clippy::print_stderr)]
fn emit(line: &str) {
    eprintln!("twt.auto {line}");
}

/// What `run_once`/`drain_now` raised: `type(exc).__name__` and `str(exc)`.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{python_type}: {message}")]
pub struct DrainError {
    /// The Python exception class.
    pub python_type: String,
    /// Its message.
    pub message: String,
}

fn class_prefix(text: &str) -> Option<(&str, &str)> {
    text.split_once(": ").filter(|(kind, _)| {
        !kind.is_empty()
            && kind.chars().all(|c| c.is_ascii_alphanumeric() || c == '_')
            && (kind.ends_with("Error") || kind.ends_with("Exception") || kind.ends_with("Refused"))
    })
}

impl DrainError {
    /// `kind(message)`.
    #[must_use]
    pub fn new(python_type: &str, message: impl Into<String>) -> Self {
        Self {
            python_type: python_type.to_owned(),
            message: message.into(),
        }
    }

    /// A seam's `"<Class>: message"` text; `default` when it names no class.
    #[must_use]
    pub fn from_text(text: &str, default: &str) -> Self {
        class_prefix(text).map_or_else(
            || Self::new(default, text),
            |(kind, message)| Self::new(kind, message),
        )
    }

    /// The DB-API class the driver's error would have been.
    #[must_use]
    pub fn from_storage(e: &StorageError) -> Self {
        let text = e.to_string();
        match e {
            StorageError::Untranslatable(m) => Self::new("ValueError", m.clone()),
            StorageError::BadParameter(m) if m.contains("NUL") => Self::new("DataError", m.clone()),
            StorageError::BadParameter(m) => Self::new("ProgrammingError", m.clone()),
            StorageError::NoLastRowId => Self::new("NotImplementedError", text),
            StorageError::Driver(sqlx::Error::Database(db)) => {
                use sqlx::error::ErrorKind;
                let kind = match db.kind() {
                    ErrorKind::UniqueViolation
                    | ErrorKind::ForeignKeyViolation
                    | ErrorKind::NotNullViolation
                    | ErrorKind::CheckViolation => "IntegrityError",
                    _ => {
                        let code = db.code().map(|c| c.into_owned()).unwrap_or_default();
                        if code.len() == 5 {
                            match &code[..2] {
                                "08" | "40" | "53" | "55" | "57" | "58" => "OperationalError",
                                "22" => "DataError",
                                "42" => "ProgrammingError",
                                "0A" => "NotSupportedError",
                                "XX" => "InternalError",
                                _ => "DatabaseError",
                            }
                        } else {
                            "OperationalError"
                        }
                    }
                };
                Self::new(kind, db.message().to_owned())
            }
            StorageError::Driver(
                sqlx::Error::Io(_)
                | sqlx::Error::Tls(_)
                | sqlx::Error::PoolTimedOut
                | sqlx::Error::PoolClosed
                | sqlx::Error::Protocol(_)
                | sqlx::Error::WorkerCrashed,
            ) => Self::new("OperationalError", text),
            StorageError::Driver(sqlx::Error::Configuration(_)) => {
                Self::new("ProgrammingError", text)
            }
            StorageError::Driver(_) => Self::new("DatabaseError", text),
            StorageError::Other(m) => Self::from_text(m, "RuntimeError"),
        }
    }

    /// The class a store call raised.
    #[must_use]
    pub fn from_store(e: &StoreError) -> Self {
        match e {
            StoreError::Storage(s) => Self::from_storage(s),
            StoreError::Scan(r) => Self::new("ScanRefused", r.reason.clone()),
            StoreError::Bad(m) => Self::from_text(m, "ValueError"),
            StoreError::SessionBusy => Self::new("HTTPException", e.to_string()),
        }
    }
}

impl From<StoreError> for DrainError {
    fn from(e: StoreError) -> Self {
        Self::from_store(&e)
    }
}

/// `MARKET_OPEN`: 09:15 IST.
#[must_use]
pub fn market_open() -> NaiveTime {
    NaiveTime::from_hms_opt(9, 15, 0).unwrap_or_default()
}

/// `MARKET_CLOSE`: 15:30 IST.
#[must_use]
pub fn market_close() -> NaiveTime {
    NaiveTime::from_hms_opt(15, 30, 0).unwrap_or_default()
}

/// `MORNING`.
pub const MORNING: &str = "MORNING";
/// `LIVE`.
pub const LIVE: &str = "LIVE";
/// `AUTO_SOURCES`: the plan sources this runner acts on — never `EVENING`.
pub const AUTO_SOURCES: [&str; 2] = [MORNING, LIVE];

/// `DRAIN_EVERY_SECONDS`.
pub const DRAIN_EVERY_SECONDS: u64 = 60;
/// `SESSION_WAIT_SECONDS`.
pub const SESSION_WAIT_SECONDS: f64 = 600.0;
/// `SESSION_POLL_SECONDS`.
pub const SESSION_POLL_SECONDS: f64 = 30.0;

/// `AUTO_KINDS`: exactly `twt_execute.EXECUTABLE_KINDS`.
pub const AUTO_KINDS: [&str; 4] = ["SELL_AT_OPEN", "ARM_GTT", "RAISE_GTT_STOP", "BUY_AT_OPEN"];

/// `live_execution()`: the TWT flag on AND the desk not in `DRY_RUN`.
#[must_use]
pub const fn live_execution(s: &Settings) -> bool {
    s.twt_execution_enabled && !s.dry_run
}

/// `auto_execute_enabled()`: all THREE flags, never fewer.
#[must_use]
pub const fn auto_execute_enabled(s: &Settings) -> bool {
    live_execution(s) && s.twt_auto_execute
}

/// What one run came to (`RunReport`).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RunReport {
    /// `ran`.
    pub ran: bool,
    /// `reason`.
    pub reason: String,
    /// `plan_id`.
    pub plan_id: Option<String>,
    /// `(symbol, kind, status)` per attempt.
    pub attempts: Vec<(String, String, String)>,
}

impl RunReport {
    /// `line()`.
    #[must_use]
    pub fn line(&self) -> String {
        if !self.ran {
            return format!("TWT AUTO: nothing sent \u{2014} {}", self.reason);
        }
        let mut counts: Vec<(String, usize)> = Vec::new();
        for (_, _, status) in &self.attempts {
            if let Some(slot) = counts.iter_mut().find(|(s, _)| s == status) {
                slot.1 += 1;
            } else {
                counts.push((status.clone(), 1));
            }
        }
        counts.sort();
        let tally = if counts.is_empty() {
            "no executable lines".to_owned()
        } else {
            counts
                .iter()
                .map(|(k, v)| format!("{k} {v}"))
                .collect::<Vec<_>>()
                .join(", ")
        };
        let detail = self
            .attempts
            .iter()
            .map(|(s, k, st)| format!("{s} {k} {st}"))
            .collect::<Vec<_>>()
            .join("; ");
        let plan = self.plan_id.clone().unwrap_or_else(|| "None".to_owned());
        let mut out = format!(
            "TWT AUTO: plan {plan}: {} line(s) \u{2014} {tally}",
            self.attempts.len()
        );
        if !detail.is_empty() {
            out.push_str(&format!(" [{detail}]"));
        }
        out
    }
}

/// `morning_plan(store, now)`: today's MORNING or LIVE plan, or `None` and the reason. Never a
/// rebuild.
///
/// # Errors
/// The store.
pub async fn morning_plan(
    store: &mut dyn TwtStore,
    now: DateTime<FixedOffset>,
) -> StoreResult<(Option<Rec>, String)> {
    let today = to_ist(&now).date_naive();
    let Some(plan) = store.todays_plan(today).await? else {
        return Ok((
            None,
            "NO_PLAN: no TWT plan exists \u{2014} did twt-morning run at 09:05, or a live scan?"
                .to_owned(),
        ));
    };
    let plan_id = plan.s("plan_id");
    let source = plan.s("source");
    if !AUTO_SOURCES.contains(&source.as_str()) {
        return Ok((
            None,
            format!(
                "NOT_MORNING: the newest plan {plan_id} is {source}, and only the 09:05 MORNING \
                 rebuild or a LIVE scan's plan is sized against today's book"
            ),
        ));
    }
    let built = plan.stamp("built_at");
    if built.is_none_or(|b| to_ist(&b).date_naive() != today) {
        let shown = built.map_or_else(|| "None".to_owned(), |b| py_isoformat(&b, ' '));
        return Ok((
            None,
            format!(
                "STALE_PLAN: plan {plan_id} was not built today ({shown}); this runner never \
                 builds a plan of its own"
            ),
        ));
    }
    if let Some(expires) = plan.stamp("expires_at")
        && now >= expires
    {
        return Ok((
            None,
            format!(
                "EXPIRED: plan {plan_id} expired at {}; nothing is sent against an expired plan, \
                 by a person or by this",
                py_isoformat(&expires, 'T')
            ),
        ));
    }
    Ok((Some(plan), String::new()))
}

/// `executable_lines(store, plan)`: the plan's `PROPOSED` executable lines, in the plan's order.
///
/// # Errors
/// The store.
pub async fn executable_lines(store: &mut dyn TwtStore, plan: &Rec) -> StoreResult<Vec<Rec>> {
    Ok(store
        .lines_for(plan.int("id"))
        .await?
        .into_iter()
        .filter(|l| AUTO_KINDS.contains(&l.s("kind").as_str()) && l.s("state") == "PROPOSED")
        .collect())
}

/// `session_closed(store, day)`: true only when the calendar says `day` is **not** a session;
/// unknown (no row, an unreadable calendar) is open.
pub async fn session_closed(store: &mut dyn TwtStore, day: NaiveDate) -> bool {
    matches!(store.is_trading_day(day).await, Ok(Some(false)))
}

/// Everything `run_once` reaches, as seams (Python's keyword arguments).
#[async_trait]
pub trait Collaborators: Send + Sync {
    /// The settings, read at call time.
    fn settings(&self) -> Settings;
    /// `now()`.
    fn now(&self) -> DateTime<FixedOffset>;
    /// `open_store()`.
    async fn open_store(&self) -> Result<Box<dyn TwtStore>, String>;
    /// `gateway()`: this sleeve's gateway (`twt_desk.twt_gateway`, which builds the desk's shared
    /// risk manager first).
    async fn gateway(&self) -> Arc<dyn Gateway>;
    /// `price_for(symbol)`.
    async fn price_for(&self, symbol: &str) -> Option<Decimal>;
    /// `authed()`: a Kite session exists.
    async fn authed(&self) -> bool;
    /// `sleep(seconds)`.
    async fn sleep(&self, seconds: f64);
    /// `execute(store, gateway, plan_id=..., line_id=..., confirm="true", now=..., last_price=...)`.
    #[allow(clippy::too_many_arguments)]
    async fn execute(
        &self,
        store: &mut dyn TwtStore,
        gateway: &dyn Gateway,
        settings: &Settings,
        plan_id: &str,
        line_id: i64,
        now: DateTime<FixedOffset>,
        last_price: Option<Decimal>,
    ) -> ExecResult<ExecOutcome> {
        execute::execute_line(
            store, gateway, settings, plan_id, line_id, "true", now, last_price,
        )
        .await
    }
}

/// `drain_plan(store, gateway, plan, now=..., price_for=..., execute=...)`: each `PROPOSED` line
/// through `execute_line`, once, in order. Does nothing unless [`auto_execute_enabled`] **at
/// call time**. Fails soft per line.
///
/// # Errors
/// The store (reading the lines).
pub async fn drain_plan(
    c: &dyn Collaborators,
    store: &mut dyn TwtStore,
    gateway: &dyn Gateway,
    plan: &Rec,
) -> StoreResult<Vec<(String, String, String)>> {
    let settings = c.settings();
    if !auto_execute_enabled(&settings) {
        return Ok(Vec::new());
    }
    let mut done = Vec::new();
    for line in executable_lines(store, plan).await? {
        let (symbol, kind) = (line.s("symbol"), line.s("kind"));
        let now = c.now();
        let price = c.price_for(&symbol).await;
        let status = match c
            .execute(
                store,
                gateway,
                &settings,
                &plan.s("plan_id"),
                line.int("id"),
                now,
                price,
            )
            .await
        {
            Ok(outcome) => {
                let reason = if outcome.reason.is_empty() {
                    String::new()
                } else {
                    format!(" \u{2014} {}", outcome.reason)
                };
                emit(&format!(
                    "TWT AUTO {symbol} {kind}: {}{reason}",
                    outcome.status
                ));
                outcome.status
            }
            Err(ExecError::Http(h)) => {
                let status = format!("REFUSED_{}", h.status);
                emit(&format!(
                    "TWT AUTO {symbol} {kind}: {status} \u{2014} {}",
                    h.detail
                ));
                status
            }
            Err(e) if e.is_untouchable() => {
                emit(&format!(
                    "TWT AUTO {symbol} {kind}: BLOCKED by a guard \u{2014} {e}"
                ));
                "BLOCKED".to_owned()
            }
            Err(_) => {
                emit(&format!(
                    "TWT AUTO {symbol} {kind}: ERROR \u{2014} the line can still be confirmed by hand"
                ));
                "ERROR".to_owned()
            }
        };
        count("twt_auto_execute", &[("outcome", &status)]);
        done.push((symbol, kind, status));
    }
    Ok(done)
}

async fn wait_for_session(c: &dyn Collaborators, wait_seconds: f64, poll_seconds: f64) -> bool {
    let mut waited = 0.0;
    loop {
        if c.authed().await {
            return true;
        }
        if waited >= wait_seconds {
            return false;
        }
        c.sleep(poll_seconds).await;
        waited += poll_seconds;
    }
}

fn weekday_abbrev(d: NaiveDate) -> &'static str {
    match d.weekday() {
        chrono::Weekday::Mon => "Mon",
        chrono::Weekday::Tue => "Tue",
        chrono::Weekday::Wed => "Wed",
        chrono::Weekday::Thu => "Thu",
        chrono::Weekday::Fri => "Fri",
        chrono::Weekday::Sat => "Sat",
        chrono::Weekday::Sun => "Sun",
    }
}

/// `run_once(...)`: one pass. Every refusal before the drain is a [`RunReport`] with a reason.
///
/// # Errors
/// The store failing to open or read (Python raises out of `run_once`).
pub async fn run_once(
    c: &dyn Collaborators,
    wait_seconds: f64,
    poll_seconds: f64,
) -> Result<RunReport, DrainError> {
    let mut report = RunReport::default();
    let refuse = |mut r: RunReport, reason: String, outcome: &str| {
        count("twt_auto_runs", &[("outcome", outcome)]);
        r.reason = reason;
        Ok(r)
    };
    if !auto_execute_enabled(&c.settings()) {
        return refuse(
            report,
            "auto-execute is off (needs DRY_RUN=false, BASKFY_TWT_EXECUTION_ENABLED=true and \
             BASKFY_TWT_AUTO_EXECUTE=true)"
                .to_owned(),
            "off",
        );
    }
    let stamp = to_ist(&c.now());
    if stamp.weekday().num_days_from_monday() > 4 {
        return refuse(
            report,
            format!("{} is not a weekday", weekday_abbrev(stamp.date_naive())),
            "not_a_session",
        );
    }
    if stamp.time() < market_open() || stamp.time() >= market_close() {
        return refuse(
            report,
            format!(
                "{} IST is outside the session; the entry is at market inside it (04 \u{a7}5.1, \
                 TW19)",
                stamp.format("%H:%M")
            ),
            "outside_session",
        );
    }
    if !wait_for_session(c, wait_seconds, poll_seconds).await {
        emit(&format!(
            "TWT AUTO: no Kite session after {wait_seconds:.0}s; nothing sent"
        ));
        return refuse(
            report,
            format!("no Kite session after {wait_seconds:.0}s"),
            "no_session",
        );
    }
    let mut store = c
        .open_store()
        .await
        .map_err(|e| DrainError::from_text(&e, "RuntimeError"))?;
    if session_closed(store.as_mut(), stamp.date_naive()).await {
        return refuse(
            report,
            format!(
                "{} is not a trading session",
                stamp.date_naive().format("%Y-%m-%d")
            ),
            "not_a_session",
        );
    }
    let (plan, why) = morning_plan(store.as_mut(), c.now()).await?;
    let Some(plan) = plan else {
        emit(&format!("TWT AUTO: {why}"));
        return refuse(report, why, "no_plan");
    };
    report.plan_id = Some(plan.s("plan_id"));
    let gateway = c.gateway().await;
    report.attempts = drain_plan(c, store.as_mut(), gateway.as_ref(), &plan).await?;
    report.ran = true;
    count("twt_auto_runs", &[("outcome", "ran")]);
    Ok(report)
}

/// The desk's own collaborators: its store, this sleeve's gateway, `last_price`, the Kite
/// session check, and the desk clock (or a fixed `now` for [`drain_now`]).
pub struct DeskCollaborators {
    state: AppState,
    now: Option<DateTime<FixedOffset>>,
}

impl std::fmt::Debug for DeskCollaborators {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DeskCollaborators")
            .field("now", &self.now)
            .finish_non_exhaustive()
    }
}

impl DeskCollaborators {
    /// Over the desk's state.
    #[must_use]
    pub const fn new(state: AppState, now: Option<DateTime<FixedOffset>>) -> Self {
        Self { state, now }
    }
}

#[async_trait]
impl Collaborators for DeskCollaborators {
    fn settings(&self) -> Settings {
        self.state.settings_snapshot()
    }

    fn now(&self) -> DateTime<FixedOffset> {
        self.now.unwrap_or_else(|| super::now_ist(&self.state))
    }

    async fn open_store(&self) -> Result<Box<dyn TwtStore>, String> {
        super::open_store(&self.state)
            .await
            .map(|s| Box::new(s) as Box<dyn TwtStore>)
            .map_err(|e| DrainError::from_storage(&e).to_string())
    }

    async fn gateway(&self) -> Arc<dyn Gateway> {
        // `twt_desk.twt_gateway()` calls `_main.gateway()` first, which builds the shared risk
        // manager (on Postgres: the day's `risk_ledger` row) — `twt_gateway_ready` is that.
        super::twt_gateway_ready(&self.state).await
    }

    async fn price_for(&self, symbol: &str) -> Option<Decimal> {
        super::common::last_price(&self.state, symbol).await
    }

    async fn authed(&self) -> bool {
        // `_main.kite().is_authed()`: the desk's wrapper re-reads a rotated token
        // (`refresh_token_if_changed`) and takes a `general` rate-limit slot before `profile`.
        // The bare client would keep answering with the token it started with, so a morning
        // login would never be seen by a long-lived `twt-auto`.
        if let Some(desk) = self
            .state
            .gateway
            .as_any()
            .and_then(|a| a.downcast_ref::<crate::lanes::exec_core::desk::DeskGateway>())
        {
            return desk.kite().is_authed().await;
        }
        self.state.kite.profile().await.is_ok()
    }

    async fn sleep(&self, seconds: f64) {
        if seconds > 0.0 {
            tokio::time::sleep(std::time::Duration::from_secs_f64(seconds)).await;
        }
    }
}

/// `drain_now(now)`: LV8's one pass for the session supervisor — no wait for a Kite session (the
/// supervisor only calls it when it has seen one), no sleep.
///
/// # Errors
/// The store.
pub async fn drain_now(
    state: &AppState,
    now: Option<DateTime<FixedOffset>>,
) -> Result<RunReport, DrainError> {
    let c = DeskCollaborators::new(state.clone(), now);
    run_once(&c, 0.0, 0.0).await
}

/// `python -m app.twt_auto`: one morning's run; exits 0 when the flag is off.
///
/// # Errors
/// The store.
pub async fn main(state: &AppState) -> Result<(), String> {
    if !auto_execute_enabled(&state.settings_snapshot()) {
        return Ok(());
    }
    let c = DeskCollaborators::new(state.clone(), None);
    let report = run_once(&c, SESSION_WAIT_SECONDS, SESSION_POLL_SECONDS)
        .await
        .map_err(|e| e.to_string())?;
    emit(&report.line());
    Ok(())
}
