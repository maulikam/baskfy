//! `app/session_supervisor.py` — a Kite login is the event, whatever the hour (LV4).
//!
//! One process (`session-supervisor`), awake 08:30–15:50 IST on weekdays, ticking every ten
//! seconds: it watches the token blob; on a new token that authenticates it beats `login`, seeds
//! the account's holdings into the shared risk ledger (LV3) and runs one reconcile pass at once
//! (LV2); in session it reconciles every tick and, since LV8 (DECISIONS-TW TW19), asks
//! `twt_auto.drain_now` once a minute to confirm TWT's LIVE plan. It writes `lv_heartbeat` rows
//! (`supervisor`, `reconciler`, `twt-auto`).
//!
//! **What it never does:** build a plan, place anything of its own, start a process, or write the
//! token. The drain is the flag-gated `twt::auto::drain_now` (the three TWT flags are checked
//! inside it — this module adds no flag, non-negotiable 1). Every write the reconciler causes goes
//! through the sleeves' handlers and the gateway.
//!
//! Everything time- and broker-dependent is injected ([`Deps`], [`WallClock`]), so the
//! supervisor is a table of tests (`tests` below, ported from `tests/test_session_supervisor.py`).
//!
//! **Not wired yet (loud):** `reconcile.run_pass_from_desk` — the swing/twt/vbt `SleeveHooks`
//! belong to the sleeve lanes. [`DeskDeps`] takes the pass as a [`ReconcilePass`]; until one
//! exists the `session-supervisor` service refuses to start (lane `session`'s `run_service`)
//! rather than beat a reconciler that does not exist. DECISIONS-RS R8 (session lane).

use std::path::PathBuf;
use std::sync::Arc;

use chrono::{DateTime, Datelike as _, Duration, FixedOffset, NaiveTime, TimeZone as _};

use crate::app::AppState;
use crate::clock::ist;
use crate::storage::{Db, Param};
use crate::wire::{PyDict, PyValue};

/// Supervisor's day starts (before the pre-open nudges).
pub const WAKE_AT: (u32, u32) = (8, 30);
/// …and ends (after the last GTT sweep).
pub const SLEEP_AT: (u32, u32) = (15, 50);
/// NSE's cash session opens — the reconciler runs inside it.
pub const MARKET_OPEN: (u32, u32) = (9, 15);
/// NSE's cash session closes.
pub const MARKET_CLOSE: (u32, u32) = (15, 30);
/// One tick (six order-book reads a minute).
pub const TICK_SECONDS: f64 = 10.0;
/// LV8: how often the supervisor asks TWT's auto-execute to drain a LIVE plan.
pub const DRAIN_EVERY_SECONDS: f64 = 60.0;

/// No Kite session.
pub const STATE_WAITING: &str = "waiting_for_login";
/// In session with a token.
pub const STATE_SESSION: &str = "session";
/// Token present, session closed.
pub const STATE_IDLE: &str = "idle";
/// The login event.
pub const STATE_LOGIN: &str = "login";

/// `calendar.FRIDAY`, as `weekday()` counts (Monday 0).
const FRIDAY: u32 = 4;

fn hm((h, m): (u32, u32)) -> NaiveTime {
    NaiveTime::from_hms_opt(h, m, 0).unwrap_or_default()
}

/// The desk's log (stderr). Every line is composed here from states, counts and error words —
/// never a token.
#[allow(clippy::print_stderr)]
fn log(line: &str) {
    eprintln!("session_supervisor: {line}");
}

/// An exception a dependency raised: Python's `type(exc).__name__` and `str(exc)`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DepError {
    /// `type(exc).__name__`.
    pub kind: String,
    /// `str(exc)`.
    pub message: String,
}

impl DepError {
    /// A `RuntimeError(message)`.
    #[must_use]
    pub fn runtime(message: impl Into<String>) -> Self {
        Self {
            kind: "RuntimeError".to_owned(),
            message: message.into(),
        }
    }
}

impl std::fmt::Display for DepError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.kind, self.message)
    }
}

/// What one reconcile pass reported (`run.line()`, `run.ok`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PassOutcome {
    /// `run.ok` (`True` when the run has no such attribute).
    pub ok: bool,
    /// `run.line()` (or `str(run)`).
    pub line: String,
}

/// What one TWT drain reported (`report.ran`, `.reason`, `.attempts`).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct DrainOutcome {
    /// `ran`.
    pub ran: bool,
    /// `reason`.
    pub reason: String,
    /// How many lines went through `execute_line`.
    pub attempts: usize,
}

/// Everything the supervisor touches, named so a test can hand it fakes (Python's `Deps`).
#[async_trait::async_trait]
pub trait Deps: Send + Sync {
    /// `kite.is_authed()`.
    async fn authed(&self) -> Result<bool, DepError>;
    /// The token blob's mtime, or `None` when it is gone.
    fn token_mtime(&self) -> Result<Option<f64>, DepError>;
    /// `kite.holdings()`.
    async fn holdings(&self) -> Result<Vec<PyDict>, DepError>;
    /// `risk.seed_positions({symbol: notional})`, in first-seen order.
    async fn seed_positions(&self, values: &[(String, f64)]) -> Result<(), DepError>;
    /// One reconcile pass.
    async fn reconcile(&self, now: DateTime<FixedOffset>) -> Result<PassOutcome, DepError>;
    /// One heartbeat upsert.
    async fn beat(
        &self,
        process: &str,
        state: &str,
        detail: &str,
        now: DateTime<FixedOffset>,
    ) -> Result<(), DepError>;
    /// Whether a TWT drain is wired (`drain_twt is not None`).
    fn has_drain(&self) -> bool {
        false
    }
    /// LV8: `twt_auto.drain_now(now)` — refuses inside itself unless all three TWT flags are set.
    async fn drain_twt(&self, now: DateTime<FixedOffset>) -> Result<DrainOutcome, DepError> {
        let _ = now;
        Ok(DrainOutcome::default())
    }
}

/// `Supervisor`: the state machine. One [`Supervisor::tick`] is one look at the world.
pub struct Supervisor {
    deps: Arc<dyn Deps>,
    /// Seconds between reconcile passes in session.
    pub reconcile_every: f64,
    /// Seconds between TWT drains in session.
    pub drain_every: f64,
    seen_mtime: Option<f64>,
    authed: bool,
    last_reconcile: Option<DateTime<FixedOffset>>,
    last_drain: Option<DateTime<FixedOffset>>,
    /// Logins seen.
    pub logins: u64,
    /// Reconcile passes started.
    pub passes: u64,
    /// Drains that returned.
    pub drains: u64,
}

impl std::fmt::Debug for Supervisor {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Supervisor")
            .field("logins", &self.logins)
            .field("passes", &self.passes)
            .field("drains", &self.drains)
            .finish_non_exhaustive()
    }
}

fn seconds_between(later: DateTime<FixedOffset>, earlier: DateTime<FixedOffset>) -> f64 {
    let d = later.signed_duration_since(earlier);
    #[allow(clippy::cast_precision_loss)]
    let secs = d.num_seconds() as f64;
    let nanos = d
        .checked_sub(&Duration::seconds(d.num_seconds()))
        .and_then(|r| r.num_nanoseconds())
        .unwrap_or(0);
    #[allow(clippy::cast_precision_loss)]
    let frac = nanos as f64 / 1e9;
    secs + frac
}

/// Python's `str[:n]`: the first `n` code points.
fn head(text: &str, n: usize) -> String {
    text.chars().take(n).collect()
}

/// `float(x or 0)` over a holdings cell.
fn float_or_zero(v: Option<&PyValue>) -> Result<f64, DepError> {
    match v {
        None | Some(PyValue::None) => Ok(0.0),
        Some(PyValue::Bool(b)) => Ok(if *b { 1.0 } else { 0.0 }),
        #[allow(clippy::cast_precision_loss)]
        Some(PyValue::Int(i)) => Ok(*i as f64),
        Some(PyValue::Float(f)) => Ok(*f),
        Some(PyValue::Str(s)) if s.is_empty() => Ok(0.0),
        Some(PyValue::Str(s)) => crate::settings::py_float(s).ok_or_else(|| DepError {
            kind: "ValueError".to_owned(),
            message: format!(
                "could not convert string to float: {}",
                baskfy_providers::py::repr_str(s)
            ),
        }),
        Some(_) => Err(DepError {
            kind: "TypeError".to_owned(),
            message: "float() argument must be a string or a real number".to_owned(),
        }),
    }
}

/// `str(row.get("symbol") or "")`.
fn symbol_of(row: &PyDict) -> String {
    match row.get("symbol") {
        None | Some(PyValue::None) => String::new(),
        Some(PyValue::Str(s)) => s.clone(),
        Some(PyValue::Bool(false)) => String::new(),
        Some(PyValue::Int(0)) => String::new(),
        Some(PyValue::Int(i)) => i.to_string(),
        Some(PyValue::Bool(true)) => "True".to_owned(),
        // A zero float and an empty container are falsy: `x or ""` is "".
        Some(PyValue::Float(f)) if *f == 0.0 => String::new(),
        Some(PyValue::Float(f)) => baskfy_core::py::float_repr(*f),
        Some(PyValue::List(l)) if l.is_empty() => String::new(),
        Some(PyValue::Map(m)) if m.is_empty() => String::new(),
        Some(other) => format!("{other:?}"),
    }
}

/// The login seed: `{symbol: quantity * last_price}` for positive quantities and prices, a later
/// duplicate replacing the value where the first one stood (dict semantics).
///
/// # Errors
/// A cell `float()` refuses (the whole seed fails, as in Python).
pub fn seed_values(rows: &[PyDict]) -> Result<Vec<(String, f64)>, DepError> {
    let mut values: Vec<(String, f64)> = Vec::new();
    for row in rows {
        let symbol = symbol_of(row);
        let qty = float_or_zero(row.get("quantity"))?;
        let price = float_or_zero(row.get("last_price"))?;
        if !symbol.is_empty() && qty > 0.0 && price > 0.0 {
            let value = qty * price;
            if let Some(slot) = values.iter_mut().find(|(s, _)| *s == symbol) {
                slot.1 = value;
            } else {
                values.push((symbol, value));
            }
        }
    }
    Ok(values)
}

impl Supervisor {
    /// `Supervisor(deps)` with the default cadences.
    #[must_use]
    pub fn new(deps: Arc<dyn Deps>) -> Self {
        Self::with_cadence(deps, TICK_SECONDS, DRAIN_EVERY_SECONDS)
    }

    /// `Supervisor(deps, reconcile_every=, drain_every=)`.
    #[must_use]
    pub fn with_cadence(deps: Arc<dyn Deps>, reconcile_every: f64, drain_every: f64) -> Self {
        Self {
            deps,
            reconcile_every,
            drain_every,
            seen_mtime: None,
            authed: false,
            last_reconcile: None,
            last_drain: None,
            logins: 0,
            passes: 0,
            drains: 0,
        }
    }

    /// One look at the world. Returns the supervisor's state after it.
    pub async fn tick(&mut self, now: DateTime<FixedOffset>) -> &'static str {
        let local = now.with_timezone(&ist());
        let mtime = match self.deps.token_mtime() {
            Ok(m) => m,
            Err(e) => {
                log(&format!("token_mtime failed: {}", e.message));
                None
            }
        };
        let changed = mtime.is_some() && mtime != self.seen_mtime;
        if changed || !self.authed {
            self.authed = match self.deps.authed().await {
                Ok(a) => a,
                Err(e) => {
                    log(&format!("authed failed: {}", e.message));
                    false
                }
            };
            if changed {
                self.seen_mtime = mtime;
                if self.authed {
                    self.on_login(now).await;
                }
            }
        }
        if !self.authed {
            self.beat(
                "supervisor",
                STATE_WAITING,
                "no Kite session; the token is absent or expired",
                now,
            )
            .await;
            return STATE_WAITING;
        }
        let t = local.time();
        if hm(MARKET_OPEN) <= t && t < hm(MARKET_CLOSE) {
            if self.due(now) {
                self.reconcile(now).await;
            }
            if self.drain_due(now) {
                self.drain(now).await;
            }
            self.beat(
                "supervisor",
                STATE_SESSION,
                "session open; reconciling every tick",
                now,
            )
            .await;
            return STATE_SESSION;
        }
        self.beat(
            "supervisor",
            STATE_IDLE,
            "session closed; token present",
            now,
        )
        .await;
        STATE_IDLE
    }

    fn drain_due(&self, now: DateTime<FixedOffset>) -> bool {
        if !self.deps.has_drain() {
            return false;
        }
        self.last_drain
            .is_none_or(|last| seconds_between(now, last) >= self.drain_every)
    }

    async fn drain(&mut self, now: DateTime<FixedOffset>) {
        self.last_drain = Some(now);
        let report = match self.deps.drain_twt(now).await {
            Ok(r) => r,
            Err(e) => {
                log(&format!("TWT drain raised: {}", e.kind));
                self.beat(
                    "twt-auto",
                    "error",
                    &format!("drain raised {}", e.kind),
                    now,
                )
                .await;
                return;
            }
        };
        self.drains += 1;
        if report.ran {
            self.beat(
                "twt-auto",
                "ran",
                &format!("{} line(s) sent through execute_line", report.attempts),
                now,
            )
            .await;
        } else {
            let reason = head(&report.reason, 200);
            let detail = if reason.is_empty() {
                "nothing to drain".to_owned()
            } else {
                reason
            };
            self.beat("twt-auto", "idle", &detail, now).await;
        }
    }

    async fn on_login(&mut self, now: DateTime<FixedOffset>) {
        self.logins += 1;
        log(&format!(
            "a Kite session arrived at {} IST",
            now.with_timezone(&ist()).format("%H:%M:%S")
        ));
        self.beat(
            "supervisor",
            STATE_LOGIN,
            "token arrived; seeding exposure and reconciling",
            now,
        )
        .await;
        let seeded = match self.deps.holdings().await {
            Ok(rows) => match seed_values(&rows) {
                Ok(values) => self
                    .deps
                    .seed_positions(&values)
                    .await
                    .map(|()| values.len()),
                Err(e) => Err(e),
            },
            Err(e) => Err(e),
        };
        match seeded {
            Ok(n) => log(&format!("seeded {n} holding(s) into the risk ledger")),
            Err(e) => log(&format!(
                "could not seed holdings into the risk ledger: {}",
                e.message
            )),
        }
        self.reconcile(now).await;
    }

    fn due(&self, now: DateTime<FixedOffset>) -> bool {
        self.last_reconcile
            .is_none_or(|last| seconds_between(now, last) >= self.reconcile_every)
    }

    async fn reconcile(&mut self, now: DateTime<FixedOffset>) {
        self.last_reconcile = Some(now);
        self.passes += 1;
        match self.deps.reconcile(now).await {
            Ok(run) => {
                let state = if run.ok { "ok" } else { "errors" };
                self.beat("reconciler", state, &run.line, now).await;
            }
            Err(e) => {
                log("reconcile pass failed");
                self.beat("reconciler", "error", &e.to_string(), now).await;
            }
        }
    }

    async fn beat(&self, process: &str, state: &str, detail: &str, now: DateTime<FixedOffset>) {
        if let Err(e) = self.deps.beat(process, state, detail, now).await {
            log(&format!(
                "could not write the {process} heartbeat: {}",
                e.message
            ));
        }
    }
}

// --- the clock ----------------------------------------------------------------------------------

/// `next_wake(now)`: `now` inside a weekday's 08:30–15:50; else the next weekday's 08:30 IST.
#[must_use]
pub fn next_wake(now: DateTime<FixedOffset>) -> DateTime<FixedOffset> {
    let local = now.with_timezone(&ist());
    let t = local.time();
    let weekday = local.weekday().num_days_from_monday();
    if weekday <= FRIDAY && hm(WAKE_AT) <= t && t < hm(SLEEP_AT) {
        return now;
    }
    let mut day = local.date_naive();
    if t >= hm(SLEEP_AT) || weekday > FRIDAY {
        day = day.succ_opt().unwrap_or(day);
    }
    while day.weekday().num_days_from_monday() > FRIDAY {
        day = day.succ_opt().unwrap_or(day);
    }
    ist()
        .from_local_datetime(&day.and_time(hm(WAKE_AT)))
        .single()
        .unwrap_or(now)
}

/// `run_forever`'s `now_fn` and `sleep_fn`.
#[async_trait::async_trait]
pub trait WallClock: Send + Sync {
    /// `datetime.now(tz=IST)`.
    fn now(&self) -> DateTime<FixedOffset>;
    /// `time.sleep(seconds)`.
    async fn sleep(&self, seconds: f64);
}

/// `run_forever(supervisor, limit=)`: tick every [`TICK_SECONDS`] inside the day; sleep to the
/// next wake outside it. Returns the ticks taken (only with a `limit`).
pub async fn run_forever(
    supervisor: &mut Supervisor,
    clock: &dyn WallClock,
    limit: Option<u64>,
) -> u64 {
    let mut ticks = 0;
    while limit.is_none_or(|l| ticks < l) {
        let current = clock.now();
        let wake = next_wake(current);
        if wake > current {
            let wait = seconds_between(wake, current);
            log(&format!(
                "supervisor: asleep until {} IST ({wait:.0}s)",
                wake.with_timezone(&ist())
            ));
            clock.sleep(wait).await;
            continue;
        }
        let _state = supervisor.tick(current).await;
        ticks += 1;
        clock.sleep(TICK_SECONDS).await;
    }
    ticks
}

// --- the desk's wiring -----------------------------------------------------------------------

/// `PgHeartbeats`: `lv_heartbeat` upserts through the desk's sqlite-shaped connection — one row
/// a process.
#[derive(Debug, Clone)]
pub struct PgHeartbeats {
    db: Db,
    /// `user_id`.
    pub user_id: i64,
    /// `schema` (`"public"` on Postgres, `""` on SQLite).
    pub schema: String,
}

impl PgHeartbeats {
    /// `PgHeartbeats(connect, user_id=, schema=)`.
    #[must_use]
    pub fn new(db: Db, user_id: i64, schema: &str) -> Self {
        Self {
            db,
            user_id,
            schema: schema.to_owned(),
        }
    }

    /// `t(table)`: schema-qualified when a schema is set.
    #[must_use]
    pub fn t(&self, table: &str) -> String {
        if self.schema.is_empty() {
            table.to_owned()
        } else {
            format!("{}.{table}", self.schema)
        }
    }

    /// `beat(process, state, detail, at)`: one upsert, the detail cut at 2,000 characters.
    ///
    /// # Errors
    /// The database.
    pub async fn beat(
        &self,
        process: &str,
        state: &str,
        detail: &str,
        at: DateTime<FixedOffset>,
    ) -> Result<(), DepError> {
        let err = |e: crate::storage::StorageError| DepError {
            kind: "DatabaseError".to_owned(),
            message: e.to_string(),
        };
        let mut conn = self.db.connect().await.map_err(err)?;
        let sql = format!(
            "INSERT INTO {} (user_id, process, state, detail, at) VALUES (?, ?, ?, ?, ?) \
             ON CONFLICT (user_id, process) DO UPDATE SET state = EXCLUDED.state, \
             detail = EXCLUDED.detail, at = EXCLUDED.at",
            self.t("lv_heartbeat")
        );
        conn.execute(
            &sql,
            &[
                Param::Int(self.user_id),
                Param::Text(process.to_owned()),
                Param::Text(state.to_owned()),
                Param::Text(head(detail, 2000)),
                Param::Aware(at),
            ],
        )
        .await
        .map_err(err)?;
        Ok(())
    }
}

/// `reconcile.run_pass_from_desk(now)`: the sleeves' hooks over the desk's stores and gateways,
/// one pass. Implemented by whichever lane wires the swing/twt/vbt `SleeveHooks`.
#[async_trait::async_trait]
pub trait ReconcilePass: Send + Sync {
    /// One pass at `now`.
    async fn run(&self, now: DateTime<FixedOffset>) -> Result<PassOutcome, DepError>;
}

/// `from_desk()`'s `Deps`: the desk's Kite, its gateway's risk manager, a reconcile pass, the
/// heartbeat table, and TWT's drain.
pub struct DeskDeps {
    state: AppState,
    kite: Arc<crate::lanes::exec_core::kite_client::DeskKite>,
    token_path: PathBuf,
    beats: PgHeartbeats,
    pass: Arc<dyn ReconcilePass>,
}

impl std::fmt::Debug for DeskDeps {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DeskDeps")
            .field("beats", &self.beats)
            .finish_non_exhaustive()
    }
}

impl DeskDeps {
    /// The real wiring; `None` when the app's gateway is not the desk's (no Kite wrapper, no
    /// risk manager to seed).
    #[must_use]
    pub fn from_desk(state: &AppState, pass: Arc<dyn ReconcilePass>) -> Option<Self> {
        let gw = state
            .gateway
            .as_any()?
            .downcast_ref::<crate::lanes::exec_core::desk::DeskGateway>()?;
        let settings = state.settings_snapshot();
        let schema = if state.db.config().is_postgres() {
            "public"
        } else {
            ""
        };
        Some(Self {
            state: state.clone(),
            kite: gw.kite(),
            token_path: PathBuf::from(&settings.token_file),
            beats: PgHeartbeats::new(state.db.clone(), settings.sole_user_id, schema),
            pass,
        })
    }
}

#[async_trait::async_trait]
impl Deps for DeskDeps {
    async fn authed(&self) -> Result<bool, DepError> {
        Ok(self.kite.is_authed().await)
    }

    fn token_mtime(&self) -> Result<Option<f64>, DepError> {
        // `Kite._blob_mtime(store)`: the blob beside the configured token file.
        let store =
            crate::lanes::exec_core::token_store::store_for(&self.token_path, "").map_err(|e| {
                DepError {
                    kind: "OSError".to_owned(),
                    message: e.to_string(),
                }
            })?;
        Ok(std::fs::metadata(store.path())
            .and_then(|m| m.modified())
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_secs_f64()))
    }

    async fn holdings(&self) -> Result<Vec<PyDict>, DepError> {
        self.kite.holdings().await.map_err(|e| DepError {
            kind: e.python_type.clone(),
            message: e.message,
        })
    }

    async fn seed_positions(&self, values: &[(String, f64)]) -> Result<(), DepError> {
        let gw = self
            .state
            .gateway
            .as_any()
            .and_then(|a| a.downcast_ref::<crate::lanes::exec_core::desk::DeskGateway>())
            .ok_or_else(|| DepError::runtime("the desk's gateway is not wired"))?;
        let risk = gw
            .risk()
            .await
            .map_err(|e| DepError::runtime(e.to_string()))?;
        risk.seed_positions(values)
            .await
            .map_err(|e| DepError::runtime(e.to_string()))
    }

    async fn reconcile(&self, now: DateTime<FixedOffset>) -> Result<PassOutcome, DepError> {
        self.pass.run(now).await
    }

    async fn beat(
        &self,
        process: &str,
        state: &str,
        detail: &str,
        now: DateTime<FixedOffset>,
    ) -> Result<(), DepError> {
        self.beats.beat(process, state, detail, now).await
    }

    fn has_drain(&self) -> bool {
        true
    }

    async fn drain_twt(&self, now: DateTime<FixedOffset>) -> Result<DrainOutcome, DepError> {
        let report = crate::lanes::twt::auto::drain_now(&self.state, Some(now))
            .await
            .map_err(DepError::runtime)?;
        Ok(DrainOutcome {
            ran: report.ran,
            reason: report.reason,
            attempts: report.attempts.len(),
        })
    }
}

/// The real wall clock.
#[derive(Clone)]
pub struct DeskWallClock(pub Arc<dyn crate::clock::Clock>);

impl std::fmt::Debug for DeskWallClock {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("DeskWallClock")
    }
}

#[async_trait::async_trait]
impl WallClock for DeskWallClock {
    fn now(&self) -> DateTime<FixedOffset> {
        self.0.now_ist()
    }

    async fn sleep(&self, seconds: f64) {
        if seconds > 0.0 && seconds.is_finite() {
            tokio::time::sleep(std::time::Duration::from_secs_f64(seconds)).await;
        }
    }
}

#[cfg(test)]
#[path = "supervisor_tests.rs"]
mod tests;
