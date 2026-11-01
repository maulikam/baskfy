//! `FnoMonitor` (`app.fno_monitor`): raises plans, watches positions, marks the book. It holds
//! no confirm and sends no entry — an entry needs `execute_entry(..., confirm=true)`, the desk's
//! route; the one pass that sends anything ([`super::super::execute::run_pending`]) sends EXIT
//! and ROLL plans only, written under the entry's confirm (or, for an F3 spread, by the monitor
//! only when M.5's flag says so).

use std::collections::BTreeMap;
use std::sync::Arc;

use chrono::{DateTime, Duration, FixedOffset, NaiveDate, NaiveTime};
use rust_decimal::Decimal;

use baskfy_core::fno::calendar::{
    PyDateTime, hard_exit_session, plan_expires_at, roll_session, sessions_before,
};
use baskfy_core::fno::condor::{
    CondorStrikes, ENTRY_SEQUENCE, EXIT_SEQUENCE, LegQuote, LegRole, RoleMap, loss_close_level,
    option_type_of, profit_take_level, sign_of,
};
use baskfy_core::fno::config::{FnoCeilings, FnoConfig, FnoError, FoSleeve, PlanState};
use baskfy_core::fno::directional::{
    self as d3, Direction, F3ExitReason, OpenSpread, SpreadStrikes,
};
use baskfy_core::fno::exits::{ExitDecision, ExitReason, f1_exit, f2_time_exit_session};
use baskfy_core::fno::monitor::{f1_close_cost, f2_action, reprice_condor, reprice_future};
use baskfy_core::fno::sizing::{MarginVerdict, SizingMode, margin_check};
use baskfy_core::fno::vol::OptionType;
use baskfy_core::options::costs::CostRates;
use baskfy_core::options::gating::Mode;

use super::super::execute::{
    Alert, ExecError, FUTURE_ROLE, FUTURE_TYPE, FoCtx, FoLedger, FoOutcome, FoQuoteSource, kind,
    paper_book, run_pending, state, structure,
};
use super::super::store::{FoStore, PositionRow, Val, money};
use super::seams::{
    Contract, IndexQuote, IndexSource, Instruments, MarginLeg, MarginSource, Market, ScanRow,
    f3_index_name, scan_candidates, sleeve_capital,
};
use super::{
    ExitByMonitor, entry_plan_id, exit_awaits_click, exit_status, reason_value, session_close,
    session_open,
};
use crate::gateway::{Gateway, TenantIds};
use crate::lanes::options::py;
use crate::wire::{PyDict, PyValue, dict};

/// `F1_SLEEVES`.
pub const F1_SLEEVES: [&str; 2] = ["F1N", "F1B"];
/// `F3_SLEEVES`.
pub const F3_SLEEVES: [&str; 2] = ["F3N", "F3B"];

/// The sleeve's FO gateway (`gateway_for(sleeve)`); `None` fails closed.
pub type GatewayFor = dyn Fn(FoSleeve) -> Option<Arc<dyn Gateway>> + Send + Sync;
/// `mode_of(sleeve)` (`fno_execute.fno_mode`).
pub type ModeOf = dyn Fn(FoSleeve) -> Mode + Send + Sync;

/// The tick's report (`TickReport`).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct TickReport {
    /// Entry plans lapsed.
    pub lapsed: Vec<String>,
    /// Entry (and add) plans raised, refusals included.
    pub raised: Vec<String>,
    /// EXIT/ROLL plans raised.
    pub actions: Vec<String>,
    /// What `run_pending` sent.
    pub done: Vec<FoOutcome>,
}

/// The night's report (`NightReport`).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct NightReport {
    /// The day's bhavcopy has landed.
    pub landed: bool,
    /// Marks written.
    pub marked: i64,
    /// F2 trails moved (`"<symbol>:<old>-><new>:<status>"`).
    pub trailed: Vec<String>,
    /// Sessions that could not be marked (`"<position>@<day>"`).
    pub missing: Vec<String>,
    /// `04` §7's pauses in force after the night, `<mode>:<code>:<scope>`.
    pub pauses: Vec<String>,
}

/// The engine and every seam a replay needs (`FnoMonitor(store=, gateway_for=, ...)`); the
/// store is handed to each call.
pub struct FnoMonitor {
    /// `gateway_for`.
    pub gateway_for: Arc<GatewayFor>,
    /// `quotes`.
    pub quotes: Arc<dyn FoQuoteSource>,
    /// `instruments`.
    pub instruments: Arc<dyn Instruments>,
    /// `margins`.
    pub margins: Arc<dyn MarginSource>,
    /// `market`.
    pub market: Arc<dyn Market>,
    /// `config`.
    pub config: FnoConfig,
    /// `ceilings`.
    pub ceilings: FnoCeilings,
    /// `mode_of`.
    pub mode_of: Arc<ModeOf>,
    /// `alert`.
    pub alert: Arc<Alert>,
    /// `rates`.
    pub rates: CostRates,
    /// `index_quotes` (F3's index reader).
    pub index_quotes: Arc<dyn IndexSource>,
    /// `exit_by_monitor` (M.5's flag; the default says no).
    pub exit_by_monitor: Arc<ExitByMonitor>,
    /// `fno_ledger` at a close (FO10).
    pub ledger: Arc<dyn FoLedger>,
    /// The console's tenant, for the GTT calls (`_tenancy()`).
    pub tenant: Option<TenantIds>,
}

pub(super) fn fe(e: FnoError) -> ExecError {
    ExecError::Other(format!("{}: {}", e.python_type(), e.message()))
}

/// `PlanState.value`.
#[must_use]
pub const fn plan_state(s: PlanState) -> &'static str {
    match s {
        PlanState::Issued => "ISSUED",
        PlanState::Confirmed => "CONFIRMED",
        PlanState::Filling => "FILLING",
        PlanState::Open => "OPEN",
        PlanState::Exiting => "EXITING",
        PlanState::Closed => "CLOSED",
        PlanState::Lapsed => "LAPSED",
        PlanState::RejectedStructure => "REJECTED_STRUCTURE",
        PlanState::RejectedLiquidity => "REJECTED_LIQUIDITY",
        PlanState::RejectedSize => "REJECTED_SIZE",
        PlanState::RejectedMargin => "REJECTED_MARGIN",
        PlanState::RejectedCost => "REJECTED_COST",
        PlanState::NoSleeveCapital => "NO_SLEEVE_CAPITAL",
        PlanState::RejectedPaused => "REJECTED_PAUSED",
        PlanState::AbandonedPartial => "ABANDONED_PARTIAL",
    }
}

const fn sizing_mode(m: SizingMode) -> &'static str {
    match m {
        SizingMode::Budget => "BUDGET",
        SizingMode::PaperOneLot => "PAPER_ONE_LOT",
    }
}

const fn option_type_value(k: OptionType) -> &'static str {
    match k {
        OptionType::Ce => "CE",
        OptionType::Pe => "PE",
    }
}

fn ds(d: Decimal) -> PyValue {
    PyValue::Str(py::dstr(d))
}

fn ods(d: Option<Decimal>) -> PyValue {
    d.map_or(PyValue::None, ds)
}

fn odate(d: Option<NaiveDate>) -> PyValue {
    d.map_or(PyValue::None, PyValue::Date)
}

fn olots(n: Option<i64>) -> PyValue {
    n.map_or(PyValue::None, PyValue::Int)
}

/// `a or b` for a `Decimal`: a zero is falsy.
pub(super) fn or_dec(a: Option<Decimal>, b: Decimal) -> Decimal {
    match a {
        Some(x) if !x.is_zero() => x,
        _ => b,
    }
}

pub(super) fn get_text(m: &PyDict, key: &str) -> String {
    m.get(key)
        .map_or_else(|| "None".to_owned(), PyValue::py_str)
}

/// `Decimal(str(value))`, raising as Python does on junk.
fn strict_dec(v: Option<&PyValue>) -> Result<Decimal, ExecError> {
    let text = v.map_or_else(|| "None".to_owned(), PyValue::py_str);
    py::dec_from_str(&text).ok_or_else(|| {
        ExecError::Other("InvalidOperation: [<class 'decimal.ConversionSyntax'>]".to_owned())
    })
}

pub(super) fn role_of(text: &str) -> Result<LegRole, ExecError> {
    LegRole::ALL
        .into_iter()
        .find(|r| r.value() == text)
        .ok_or_else(|| ExecError::Other(format!("ValueError: '{text}' is not a valid LegRole")))
}

fn direction_of(text: &str) -> Result<Direction, ExecError> {
    match text {
        "UP" => Ok(Direction::Up),
        "DOWN" => Ok(Direction::Down),
        "NONE" => Ok(Direction::None),
        other => Err(ExecError::Other(format!(
            "ValueError: '{other}' is not a valid Direction"
        ))),
    }
}

/// `D3.Direction(str(x.get("direction") or "NONE"))`.
fn direction_in(m: &PyDict) -> Result<Direction, ExecError> {
    match m.get("direction") {
        Some(v) if v.truthy() => direction_of(&v.py_str()),
        _ => Ok(Direction::None),
    }
}

fn sleeve_of(text: &str) -> Result<FoSleeve, ExecError> {
    super::super::store::sleeve_of(text)
        .ok_or_else(|| ExecError::Other(format!("ValueError: '{text}' is not a valid FoSleeve")))
}

pub(super) fn ist_day(now: DateTime<FixedOffset>) -> NaiveDate {
    now.with_timezone(&py::ist()).date_naive()
}

fn ist_time(now: DateTime<FixedOffset>) -> NaiveTime {
    now.with_timezone(&py::ist()).time()
}

/// `now.astimezone(IST).replace(tzinfo=None)`.
fn naive_ist(now: DateTime<FixedOffset>) -> PyDateTime {
    PyDateTime::Naive(now.with_timezone(&py::ist()).naive_local())
}

fn expires(
    now: DateTime<FixedOffset>,
    config: &FnoConfig,
) -> Result<DateTime<FixedOffset>, ExecError> {
    match plan_expires_at(PyDateTime::Aware(now), &config.f1, &config.common).map_err(fe)? {
        PyDateTime::Aware(a) => Ok(a),
        PyDateTime::Naive(n) => py::ist()
            .from_local_datetime(&n)
            .single()
            .ok_or_else(|| ExecError::Other("ValueError: no such local time".to_owned())),
    }
}

use chrono::TimeZone;

/// A `fo_plan` row being built (`_head(...)` then `head.update(...)`).
struct Head(Vec<(&'static str, Val)>);

impl Head {
    fn set(&mut self, key: &'static str, value: Val) {
        if let Some(slot) = self.0.iter_mut().find(|(k, _)| *k == key) {
            slot.1 = value;
        } else {
            self.0.push((key, value));
        }
    }
}

/// `_head(...)`: one `fo_plan` row.
#[allow(clippy::too_many_arguments)]
fn head(
    plan_id: &str,
    sleeve: FoSleeve,
    symbol: &str,
    day: NaiveDate,
    structure: &str,
    issued: DateTime<FixedOffset>,
    expires: DateTime<FixedOffset>,
    lots: i64,
    lot_size: i64,
    status: &str,
    reason: Option<String>,
    detail: PyDict,
    plan_kind: &str,
) -> Head {
    Head(vec![
        ("plan_id", Val::Str(plan_id.to_owned())),
        ("sleeve", Val::Str(sleeve.value().to_owned())),
        ("symbol", Val::Str(symbol.to_owned())),
        ("trade_date", Val::Date(day)),
        ("structure", Val::Str(structure.to_owned())),
        ("kind", Val::Str(plan_kind.to_owned())),
        ("sizing_mode", Val::Str("BUDGET".to_owned())),
        ("issued_at", Val::At(issued)),
        ("entry_window_end", Val::At(expires)),
        ("expires_at", Val::At(expires)),
        ("lots", Val::Int(lots)),
        ("lot_size", Val::Int(lot_size)),
        ("status", Val::Str(status.to_owned())),
        ("reason", reason.map_or(Val::None, Val::Str)),
        ("detail", Val::Json(PyValue::Map(detail))),
    ])
}

type Leg = Vec<(&'static str, Val)>;

/// `_strikes(detail)`: the condor's four strikes from a position's carry, or `None`.
pub(super) fn strikes_of(detail: &PyDict) -> Option<CondorStrikes> {
    let Some(PyValue::Map(raw)) = detail.get("strikes") else {
        return None;
    };
    let get = |k: &str| py::dec_from_str(&raw.get(k)?.py_str());
    Some(CondorStrikes {
        long_call: get("LONG_CALL")?,
        short_call: get("SHORT_CALL")?,
        short_put: get("SHORT_PUT")?,
        long_put: get("LONG_PUT")?,
    })
}

impl FnoMonitor {
    /// A monitor over these seams with the rest at Python's defaults: the default config and
    /// ceilings, `mode_of`/`tenant` from `settings`, `NoIndex`, the monitor never sending an F3
    /// exit itself (`_no_monitor_exit`), the desk's ledger, alerts logged.
    #[must_use]
    pub fn new(
        settings: &crate::settings::Settings,
        gateway_for: Arc<GatewayFor>,
        quotes: Arc<dyn FoQuoteSource>,
        instruments: Arc<dyn Instruments>,
        margins: Arc<dyn MarginSource>,
        market: Arc<dyn Market>,
    ) -> Self {
        let s = settings.clone();
        Self {
            gateway_for,
            quotes,
            instruments,
            margins,
            market,
            config: FnoConfig::default(),
            ceilings: FnoCeilings::default(),
            mode_of: Arc::new(move |sleeve| crate::lanes::fno::gates::mode_of(&s, sleeve)),
            alert: Arc::new(|m: &str| log_error(m)),
            rates: CostRates::default(),
            index_quotes: Arc::new(super::seams::NoIndex),
            exit_by_monitor: Arc::new(super::no_monitor_exit),
            ledger: Arc::new(crate::lanes::fno::ledger::DeskLedger),
            tenant: super::super::execute::tenancy(settings).ok(),
        }
    }

    fn alert(&self, message: &str) {
        (self.alert)(message);
    }

    fn gateway(&self, sleeve: FoSleeve) -> Result<Arc<dyn Gateway>, ExecError> {
        (self.gateway_for)(sleeve).ok_or_else(|| {
            ExecError::Other("RuntimeError: the FO gateway could not be built".to_owned())
        })
    }

    async fn books(
        &self,
        symbols: &[String],
    ) -> Result<BTreeMap<String, super::super::store::FoQuote>, ExecError> {
        self.quotes.quotes(symbols).await
    }

    // the tick ---------------------------------------------------------------------------------

    /// `tick(now)`: lapse, raise at 09:20-10:30, watch and send inside the session.
    ///
    /// # Errors
    /// The database, a seam, or the arithmetic (Python's exception text).
    pub async fn tick(
        &self,
        store: &mut FoStore<'_>,
        now: DateTime<FixedOffset>,
    ) -> Result<TickReport, ExecError> {
        let mut report = TickReport {
            lapsed: store.lapse_expired(now).await?,
            ..TickReport::default()
        };
        let t = ist_time(now);
        let f1 = &self.config.f1;
        if f1.plan_time <= t && t < f1.entry_window_end {
            report.raised = self.raise_plans(store, now).await?;
        }
        if session_open() <= t && t < session_close() {
            report.actions = self.watch(store, now).await?;
            let now_fn = move || now;
            let book = |filled: &[baskfy_core::fno::covered::OptionPosition]| paper_book(filled);
            let ctx = FoCtx {
                quotes: &*self.quotes,
                now: &now_fn,
                broker_book: &book,
                alert: &*self.alert,
                ledger: &*self.ledger,
                tenant: self.tenant,
            };
            report.done = run_pending(store, &*self.gateway_for, &ctx).await?;
        }
        Ok(report)
    }

    // raising plans ----------------------------------------------------------------------------

    /// `raise_plans(now)`: the day's entry plans (F1, F3, F3's add, F2), each sleeve from its
    /// scan; a paused sleeve's plan is written `REJECTED_PAUSED`.
    ///
    /// # Errors
    /// As [`FnoMonitor::tick`].
    pub async fn raise_plans(
        &self,
        store: &mut FoStore<'_>,
        now: DateTime<FixedOffset>,
    ) -> Result<Vec<String>, ExecError> {
        let day = ist_day(now);
        let mut raised = Vec::new();
        let open_positions = store.open_positions().await?;
        if i64::try_from(open_positions.len()).unwrap_or(i64::MAX)
            >= self.config.book.max_open_positions
        {
            return Ok(raised);
        }
        let iso = day.format("%Y-%m-%d").to_string();
        for row in scan_candidates(store, &F1_SLEEVES, day, "CANDIDATE").await? {
            if get_text(&row.detail, "entry_session") != iso {
                continue;
            }
            if open_positions.iter().any(|p| p.symbol == row.symbol) {
                continue; // one F1 structure per underlying; the next waits for the exit
            }
            if let Some(paused) = self.paused(store, &row, now).await? {
                raised.push(paused);
                continue;
            }
            if let Some(id) = self.raise_f1(store, &row, now).await? {
                raised.push(id);
            }
        }
        for row in scan_candidates(store, &F3_SLEEVES, day, "CANDIDATE").await? {
            if get_text(&row.detail, "entry_session") != iso {
                continue;
            }
            if open_positions
                .iter()
                .any(|p| p.symbol == row.symbol && F3_SLEEVES.contains(&p.sleeve.value()))
            {
                continue; // one F3 spread per underlying (04 §11)
            }
            if let Some(paused) = self.paused(store, &row, now).await? {
                raised.push(paused);
                continue;
            }
            if let Some(id) = self.raise_f3(store, &row, now).await? {
                raised.push(id);
            }
        }
        for position in &open_positions {
            if position.structure == structure::CREDIT_SPREAD
                && let Some(id) = self.raise_f3_add(store, position, now).await?
            {
                raised.push(id);
            }
        }
        let sessions = self.market.sessions().await?;
        let previous = if sessions.contains(&day) {
            sessions_before(&sessions, day, 1).map_err(fe)?
        } else {
            None
        };
        let mut held: Vec<String> = Vec::new();
        for p in &open_positions {
            if p.sleeve == FoSleeve::F2 && !held.contains(&p.symbol) {
                held.push(p.symbol.clone());
            }
        }
        for row in scan_candidates(store, &["F2"], day, "CANDIDATE").await? {
            if previous.is_none() || Some(row.trade_date) != previous || held.contains(&row.symbol)
            {
                continue; // F2 trades the previous session's signal only
            }
            if i64::try_from(held.len()).unwrap_or(i64::MAX) >= self.config.f2.max_open {
                break;
            }
            if let Some(paused) = self.paused(store, &row, now).await? {
                raised.push(paused);
                continue;
            }
            if let Some(id) = self.raise_f2(store, &row, now, &sessions).await? {
                raised.push(id);
                held.push(row.symbol.clone());
            }
        }
        Ok(raised)
    }

    /// `_paused(row, now)`: `04` §7 in the mode this sleeve trades now (FO10.1).
    async fn paused(
        &self,
        store: &mut FoStore<'_>,
        row: &ScanRow,
        now: DateTime<FixedOffset>,
    ) -> Result<Option<String>, ExecError> {
        let sleeve = sleeve_of(&row.sleeve)?;
        let day = ist_day(now);
        let plan_id = entry_plan_id(sleeve, day, &row.symbol, store.user_id);
        if store.plan(&plan_id).await?.is_some() {
            return Ok(None); // raised (or refused) already this session
        }
        let reasons = crate::lanes::fno::ledger::entry_block(
            store,
            sleeve,
            day,
            (self.mode_of)(sleeve) == Mode::Paper,
            &self.config,
            &self.ceilings,
        )
        .await?;
        if reasons.is_empty() {
            return Ok(None);
        }
        let structure_value = if sleeve == FoSleeve::F2 {
            structure::FUTURE
        } else if F3_SLEEVES.contains(&sleeve.value()) {
            structure::CREDIT_SPREAD
        } else {
            structure::IRON_CONDOR
        };
        let exp = expires(now, &self.config)?;
        let detail = dict([
            ("scan_id", PyValue::Int(row.id)),
            ("scan_date", PyValue::Date(row.trade_date)),
            (
                "reasons",
                PyValue::List(reasons.iter().cloned().map(PyValue::Str).collect()),
            ),
            ("violations", PyValue::List(Vec::new())),
        ]);
        let h = head(
            &plan_id,
            sleeve,
            &row.symbol,
            day,
            structure_value,
            now,
            exp,
            1,
            py::py_int(row.detail.get("lot_size"), 1),
            plan_state(PlanState::RejectedPaused),
            Some(reasons.join("; ")),
            detail,
            kind::ENTRY,
        );
        Ok(if store.insert_plan(&h.0, &[]).await? {
            Some(plan_id)
        } else {
            None
        })
    }

    /// `_margin(legs)`: `(state or None, message, required)`.
    async fn margin(
        &self,
        legs: &[MarginLeg],
    ) -> Result<(Option<PlanState>, String, Option<Decimal>), ExecError> {
        let Some(quote) = self.margins.margins(legs).await? else {
            return Ok((
                Some(PlanState::RejectedMargin),
                "the broker's basket margin could not be read; a plan must be shown to fit \
                 under it (04 §3)"
                    .to_owned(),
                None,
            ));
        };
        let check = margin_check(quote.required_inr, quote.free_inr);
        if check.verdict == MarginVerdict::RejectedMargin {
            return Ok((
                Some(PlanState::RejectedMargin),
                check.message,
                Some(quote.required_inr),
            ));
        }
        Ok((None, check.message, Some(quote.required_inr)))
    }

    #[allow(clippy::too_many_lines)]
    async fn raise_f1(
        &self,
        store: &mut FoStore<'_>,
        row: &ScanRow,
        now: DateTime<FixedOffset>,
    ) -> Result<Option<String>, ExecError> {
        let sleeve = sleeve_of(&row.sleeve)?;
        let day = ist_day(now);
        let plan_id = entry_plan_id(sleeve, day, &row.symbol, store.user_id);
        if store.plan(&plan_id).await?.is_some() {
            return Ok(None);
        }
        let d = &row.detail;
        let legs_raw: Vec<PyDict> = match d.get("legs") {
            Some(PyValue::List(items)) => items
                .iter()
                .filter_map(|x| match x {
                    PyValue::Map(m) => Some(m.clone()),
                    _ => None,
                })
                .collect(),
            _ => Vec::new(),
        };
        let mut by_role: Vec<(String, PyDict)> = Vec::new();
        for x in legs_raw {
            let r = get_text(&x, "role");
            if let Some(slot) = by_role.iter_mut().find(|(k, _)| *k == r) {
                slot.1 = x;
            } else {
                by_role.push((r, x));
            }
        }
        let leg_of = |role: LegRole| {
            by_role
                .iter()
                .find(|(k, _)| k == role.value())
                .map(|(_, v)| v)
        };
        let expiry = py::opt_day_py(d.get("expiry"));
        let mut reasons: Vec<String> = Vec::new();
        let mut contracts: BTreeMap<LegRole, Contract> = BTreeMap::new();
        let mut strikes_map: Vec<(LegRole, Decimal)> = Vec::new();
        for role in LegRole::ALL {
            let leg = leg_of(role);
            let leg_expiry = match leg {
                Some(l) => py::opt_day_py(l.get("expiry")),
                None => expiry,
            };
            let (Some(leg), Some(leg_expiry)) = (leg, leg_expiry) else {
                reasons.push(format!("{}: the scan names no leg", role.value()));
                continue;
            };
            let strike = strict_dec(leg.get("strike"))?;
            strikes_map.push((role, strike));
            let found = self
                .instruments
                .option(
                    &row.symbol,
                    leg_expiry,
                    strike,
                    &get_text(leg, "option_type"),
                )
                .await?;
            match found {
                None => reasons.push(format!(
                    "{} {}: not in the NFO master",
                    role.value(),
                    py::dstr(strike)
                )),
                Some(c) => {
                    contracts.insert(role, c);
                }
            }
        }
        let issued = now;
        let exp = expires(issued, &self.config)?;
        let strikes_json = PyValue::Map(dict(strikes_map.iter().map(|(r, s)| (r.value(), ds(*s)))));
        let mut base = dict([
            ("scan_id", PyValue::Int(row.id)),
            ("scan_date", PyValue::Date(row.trade_date)),
            ("strikes", strikes_json),
            ("iv", d.get("iv").cloned().unwrap_or(PyValue::None)),
            ("iv_rv", d.get("iv_rv").cloned().unwrap_or(PyValue::None)),
            ("violations", PyValue::List(Vec::new())),
        ]);
        let lot_from_scan = py::py_int(d.get("lot_size"), 1);
        let strs = |v: &[String]| PyValue::List(v.iter().cloned().map(PyValue::Str).collect());
        let (Some(expiry), true) = (expiry, contracts.len() == LegRole::ALL.len()) else {
            let mut detail = base.clone();
            detail.insert("reasons", strs(&reasons));
            let h = head(
                &plan_id,
                sleeve,
                &row.symbol,
                day,
                structure::IRON_CONDOR,
                issued,
                exp,
                1,
                lot_from_scan,
                plan_state(PlanState::RejectedStructure),
                Some(reasons.join("; ")),
                detail,
                kind::ENTRY,
            );
            store.insert_plan(&h.0, &[]).await?;
            return Ok(Some(plan_id));
        };
        let sm = |r: LegRole| {
            strikes_map
                .iter()
                .rev()
                .find(|(k, _)| *k == r)
                .map_or(Decimal::ZERO, |(_, v)| *v)
        };
        let strikes = CondorStrikes {
            long_call: sm(LegRole::LongCall),
            short_call: sm(LegRole::ShortCall),
            short_put: sm(LegRole::ShortPut),
            long_put: sm(LegRole::LongPut),
        };
        let contract = |r: LegRole| {
            contracts
                .get(&r)
                .ok_or_else(|| ExecError::Other(format!("KeyError: <LegRole.{}>", r.value())))
        };
        let lot = contract(LegRole::ShortCall)?.lot_size;
        let symbols: Vec<String> = LegRole::ALL
            .iter()
            .filter_map(|r| contracts.get(r).map(|c| c.tradingsymbol.clone()))
            .collect();
        let books = self.books(&symbols).await?;
        let mut leg_quotes: BTreeMap<LegRole, LegQuote> = BTreeMap::new();
        for (role, c) in &contracts {
            if let Some(q) = books.get(&c.tradingsymbol) {
                leg_quotes.insert(
                    *role,
                    LegQuote {
                        bid: q.bid,
                        ask: q.ask,
                        oi_lots: q.oi.unwrap_or(0).div_euclid(lot.max(1)),
                    },
                );
            }
        }
        let capital = sleeve_capital(store, "F1").await?;
        let rep = reprice_condor(
            &strikes,
            &leg_quotes,
            lot,
            capital,
            (self.mode_of)(sleeve),
            &self.config,
            &self.rates,
            &self.ceilings,
        )
        .map_err(fe)?;
        let mut state_now = rep.state;
        let mut reasons = rep.reasons.clone();
        let lots = rep
            .sizing
            .as_ref()
            .map_or(0, |s| if s.lots > 0 { s.lots } else { 0 });
        let quantity = lots.max(1) * lot;
        let mut margin_required = None;
        if state_now.is_none() {
            let mut margin_legs = Vec::new();
            for r in ENTRY_SEQUENCE {
                margin_legs.push(MarginLeg {
                    tradingsymbol: contract(r)?.tradingsymbol.clone(),
                    side: if r.value().starts_with("LONG") {
                        "BUY"
                    } else {
                        "SELL"
                    }
                    .to_owned(),
                    quantity,
                    price: rep.mids.get(&r).copied().ok_or_else(|| {
                        ExecError::Other(format!("KeyError: <LegRole.{}>", r.value()))
                    })?,
                });
            }
            let (s, message, required) = self.margin(&margin_legs).await?;
            state_now = s;
            margin_required = required;
            reasons.push(message);
        }
        let hard_exit = match py::opt_day_py(d.get("hard_exit_date")) {
            Some(h) => Some(h),
            None => {
                let sessions = self.market.sessions().await?;
                hard_exit_session(
                    &sessions,
                    expiry,
                    self.config.common.hard_exit_before_expiry,
                )
                .map_err(fe)?
            }
        };
        let credit = rep.credit;
        let mids_json =
            |m: &RoleMap| PyValue::Map(dict(m.iter().map(|(r, v)| (r.value(), ds(*v)))));
        let width = strikes.max_width().map_err(fe)?;
        base.insert("reasons", strs(&reasons));
        base.insert("mids", mids_json(&rep.mids));
        base.insert("lots_sized", PyValue::Int(lots));
        base.insert("width_points", ds(width));
        base.insert(
            "sizing",
            rep.sizing.as_ref().map_or(PyValue::None, |s| {
                PyValue::Map(dict([
                    ("lots", PyValue::Int(s.lots)),
                    ("message", PyValue::Str(s.message.clone())),
                    ("lots_at_ceiling", olots(s.lots_at_ceiling)),
                ]))
            }),
        );
        base.insert(
            "cost",
            rep.cost.as_ref().map_or(PyValue::None, |c| {
                PyValue::Map(dict([
                    ("round_trip_inr", ds(c.round_trip_inr)),
                    ("cost_share_pct", ods(c.cost_share_pct)),
                ]))
            }),
        );
        base.insert("margin_required_inr", ods(margin_required));
        if let Some(c) = credit
            && c > Decimal::ZERO
        {
            base.insert(
                "profit_take_points",
                ds(money(profit_take_level(c, &self.config.f1).map_err(fe)?)),
            );
            base.insert(
                "loss_close_points",
                ds(money(
                    loss_close_level(c, &strikes, &self.config.f1).map_err(fe)?,
                )),
            );
        }
        let status = state_now.map_or(state::ISSUED, plan_state);
        let reason = state_now.map(|_| reasons.join("; "));
        let mut h = head(
            &plan_id,
            sleeve,
            &row.symbol,
            day,
            structure::IRON_CONDOR,
            issued,
            exp,
            lots.max(1),
            lot,
            status,
            reason,
            base,
            kind::ENTRY,
        );
        let sz = rep.sizing.as_ref();
        h.set("hard_exit_date", Val::date(hard_exit));
        h.set("credit_points", Val::dec(credit.map(money)));
        h.set("width_points", Val::Dec(width));
        h.set(
            "risk_per_lot_inr",
            Val::dec(sz.map(|s| money(s.risk_per_lot_inr))),
        );
        h.set(
            "risk_budget_inr",
            Val::dec(sz.map(|s| money(s.risk_budget_inr))),
        );
        h.set("max_loss_inr", Val::dec(sz.map(|s| money(s.max_loss_inr))));
        h.set(
            "expected_cost_inr",
            Val::dec(rep.cost.as_ref().map(|c| c.round_trip_inr)),
        );
        h.set(
            "cost_share",
            Val::dec(rep.cost.as_ref().and_then(|c| c.cost_share_pct)),
        );
        h.set("margin_required_inr", Val::dec(margin_required));
        h.set(
            "sizing_mode",
            Val::Str(
                sz.map_or("BUDGET", |s| sizing_mode(s.sizing_mode))
                    .to_owned(),
            ),
        );
        let mut legs: Vec<Leg> = Vec::new();
        for (seq, role) in ENTRY_SEQUENCE.into_iter().enumerate() {
            let c = contract(role)?;
            let q = books.get(&c.tradingsymbol);
            let option_type =
                leg_of(role).map_or_else(|| "None".to_owned(), |l| get_text(l, "option_type"));
            legs.push(vec![
                ("entry_seq", Val::Int(i64::try_from(seq).unwrap_or(0) + 1)),
                ("role", Val::Str(role.value().to_owned())),
                ("tradingsymbol", Val::Str(c.tradingsymbol.clone())),
                ("instrument_token", Val::Int(c.instrument_token)),
                ("expiry", Val::Date(c.expiry)),
                ("strike", Val::Dec(strikes.strike(role))),
                ("option_type", Val::Str(option_type)),
                (
                    "side",
                    Val::Str(
                        if role.value().starts_with("LONG") {
                            "BUY"
                        } else {
                            "SELL"
                        }
                        .to_owned(),
                    ),
                ),
                ("quantity", Val::Int(quantity)),
                ("reference_price", Val::dec(rep.mids.get(&role).copied())),
                ("bid", Val::dec(q.and_then(|q| q.bid))),
                ("ask", Val::dec(q.and_then(|q| q.ask))),
            ]);
        }
        store.insert_plan(&h.0, &legs).await?;
        Ok(Some(plan_id))
    }

    // F3 — the directional index credit spread (04 §11; DECISIONS-FO M.5) ---------------------

    /// `_f3_index(symbol)`: a missing level is a refusal by name, never a crash.
    async fn f3_index(&self, symbol: &str) -> Option<IndexQuote> {
        match self.index_quotes.index(symbol).await {
            Ok(q) => q,
            Err(e) => {
                log_warning(&format!("F3 {symbol}: the index could not be read: {e}"));
                None
            }
        }
    }

    #[allow(clippy::too_many_lines)]
    async fn raise_f3(
        &self,
        store: &mut FoStore<'_>,
        row: &ScanRow,
        now: DateTime<FixedOffset>,
    ) -> Result<Option<String>, ExecError> {
        let sleeve = sleeve_of(&row.sleeve)?;
        let f3 = &self.config.f3;
        let day = ist_day(now);
        let plan_id = entry_plan_id(sleeve, day, &row.symbol, store.user_id);
        if store.plan(&plan_id).await?.is_some() {
            return Ok(None);
        }
        let d = &row.detail;
        let issued = now;
        let exp = expires(issued, &self.config)?;
        let expiry = py::opt_day_py(d.get("expiry"));
        let level = py::dec_py(d.get("level"));
        let direction = direction_in(d)?;
        let lot_from_scan = py::py_int(d.get("lot_size"), 1);
        let mut base = dict([
            ("scan_id", PyValue::Int(row.id)),
            ("scan_date", PyValue::Date(row.trade_date)),
            ("direction", PyValue::Str(direction.value().to_owned())),
            ("level", ods(level)),
            ("violations", PyValue::List(Vec::new())),
            (
                "strikes",
                PyValue::Map(dict([
                    (
                        "SHORT",
                        d.get("short_strike").cloned().unwrap_or(PyValue::None),
                    ),
                    (
                        "WING",
                        d.get("wing_strike").cloned().unwrap_or(PyValue::None),
                    ),
                ])),
            ),
        ]);
        let strs = |v: &[String]| PyValue::List(v.iter().cloned().map(PyValue::Str).collect());
        macro_rules! refuse {
            ($state:expr, $reasons:expr) => {{
                let reasons: Vec<String> = $reasons;
                let mut detail = base.clone();
                detail.insert("reasons", strs(&reasons));
                let h = head(
                    &plan_id,
                    sleeve,
                    &row.symbol,
                    day,
                    structure::CREDIT_SPREAD,
                    issued,
                    exp,
                    1,
                    lot_from_scan,
                    plan_state($state),
                    Some(reasons.join("; ")),
                    detail,
                    kind::ENTRY,
                );
                store.insert_plan(&h.0, &[]).await?;
                return Ok(Some(plan_id));
            }};
        }
        let (Some(expiry), Some(level), false) = (expiry, level, direction == Direction::None)
        else {
            refuse!(
                PlanState::RejectedStructure,
                vec!["the scan row lacks its expiry, level or direction".to_owned()]
            );
        };
        let mut by_role: Vec<(String, PyDict)> = Vec::new();
        if let Some(PyValue::List(items)) = d.get("legs") {
            for x in items {
                if let PyValue::Map(m) = x {
                    let r = get_text(m, "role");
                    if let Some(slot) = by_role.iter_mut().find(|(k, _)| *k == r) {
                        slot.1 = m.clone();
                    } else {
                        by_role.push((r, m.clone()));
                    }
                }
            }
        }
        let roles = if direction == Direction::Up {
            [LegRole::LongPut, LegRole::ShortPut]
        } else {
            [LegRole::LongCall, LegRole::ShortCall]
        };
        let mut contracts: BTreeMap<LegRole, Contract> = BTreeMap::new();
        let mut strikes: BTreeMap<LegRole, Decimal> = BTreeMap::new();
        let mut reasons: Vec<String> = Vec::new();
        for role in roles {
            let Some(leg) = by_role
                .iter()
                .find(|(k, _)| k == role.value())
                .map(|(_, v)| v)
            else {
                reasons.push(format!("{}: the scan names no leg", role.value()));
                continue;
            };
            let strike = strict_dec(leg.get("strike"))?;
            strikes.insert(role, strike);
            let found = self
                .instruments
                .option(
                    &row.symbol,
                    expiry,
                    strike,
                    option_type_value(option_type_of(role)),
                )
                .await?;
            match found {
                None => reasons.push(format!(
                    "{} {}: not in the NFO master",
                    role.value(),
                    py::dstr(strike)
                )),
                Some(c) => {
                    contracts.insert(role, c);
                }
            }
        }
        if !reasons.is_empty() {
            refuse!(PlanState::RejectedStructure, reasons);
        }
        let Some(index) = self.f3_index(&row.symbol).await else {
            refuse!(
                PlanState::RejectedStructure,
                vec![format!(
                    "no live quote for {}: the intraday check and the level cannot be read (04 §11)",
                    f3_index_name(&row.symbol).unwrap_or(&row.symbol)
                )]
            );
        };
        base.insert("index_last", ds(index.last));
        base.insert("index_open", ods(index.open));
        if d3::level_broken(direction, level, index.last, f3).map_err(fe)? {
            refuse!(
                PlanState::RejectedStructure,
                vec![format!(
                    "the level {} is already broken: the index prints {}; no entry into a broken \
                     level (04 §11)",
                    py::dstr(level),
                    py::dstr(index.last)
                )]
            );
        }
        let aligned = index
            .open
            .is_some_and(|o| d3::intraday_aligned(direction, index.last, o));
        if !aligned {
            refuse!(
                PlanState::RejectedStructure,
                vec![format!(
                    "the intraday check fails: {} needs the index {} the session's open {}, and \
                     it prints {} (04 §11)",
                    direction.value(),
                    if direction == Direction::Up {
                        "above"
                    } else {
                        "below"
                    },
                    index.open.map_or_else(|| "None".to_owned(), py::dstr),
                    py::dstr(index.last)
                )]
            );
        }
        let [long_role, short_role] = roles;
        let symbols: Vec<String> = roles
            .iter()
            .filter_map(|r| contracts.get(r).map(|c| c.tradingsymbol.clone()))
            .collect();
        let books = self.books(&symbols).await?;
        let mut mids: BTreeMap<LegRole, Decimal> = BTreeMap::new();
        for role in roles {
            let Some(c) = contracts.get(&role) else {
                continue;
            };
            match books
                .get(&c.tradingsymbol)
                .and_then(super::super::store::FoQuote::mid)
            {
                None => reasons.push(format!(
                    "{} {}: no two-sided quote",
                    role.value(),
                    c.tradingsymbol
                )),
                Some(m) => {
                    mids.insert(role, m);
                }
            }
        }
        if !reasons.is_empty() {
            refuse!(PlanState::RejectedLiquidity, reasons);
        }
        let key = |r: LegRole| ExecError::Other(format!("KeyError: <LegRole.{}>", r.value()));
        let lot = contracts
            .get(&short_role)
            .ok_or_else(|| key(short_role))?
            .lot_size;
        let short_strike = *strikes.get(&short_role).ok_or_else(|| key(short_role))?;
        let wing_strike = *strikes.get(&long_role).ok_or_else(|| key(long_role))?;
        let spread = SpreadStrikes {
            option_type: option_type_of(short_role),
            short: short_strike,
            wing: wing_strike,
        };
        let short_mid = *mids.get(&short_role).ok_or_else(|| key(short_role))?;
        let wing_mid = *mids.get(&long_role).ok_or_else(|| key(long_role))?;
        let credit = d3::credit_per_unit(short_mid, wing_mid).map_err(fe)?;
        if short_mid < f3.short_premium_min_inr || credit <= Decimal::ZERO {
            refuse!(
                PlanState::RejectedCost,
                vec![format!(
                    "short mid {} against wing mid {}: credit {}, below ₹{} or not positive",
                    py::dstr(short_mid),
                    py::dstr(wing_mid),
                    py::dstr(credit),
                    py::dstr(f3.short_premium_min_inr)
                )]
            );
        }
        let loss_unit = d3::max_loss_per_unit(&spread, credit).map_err(fe)?;
        let capital = sleeve_capital(store, "F3").await?;
        let sizing = d3::size_entry(
            (self.mode_of)(sleeve),
            capital,
            loss_unit,
            Some(lot),
            f3,
            &self.config.common,
            &self.ceilings,
        )
        .map_err(fe)?;
        let mut state_now = sizing.state;
        let mut reasons = vec![sizing.message.clone()];
        let lots = if sizing.lots > 0 { sizing.lots } else { 0 };
        let quantity = lots.max(1) * lot;
        let mut margin_required = None;
        if state_now.is_none() {
            let mut margin_legs = Vec::new();
            for r in roles {
                margin_legs.push(MarginLeg {
                    tradingsymbol: contracts
                        .get(&r)
                        .ok_or_else(|| key(r))?
                        .tradingsymbol
                        .clone(),
                    side: if sign_of(r) > 0 { "BUY" } else { "SELL" }.to_owned(),
                    quantity,
                    price: *mids.get(&r).ok_or_else(|| key(r))?,
                });
            }
            let (s, message, required) = self.margin(&margin_legs).await?;
            state_now = s;
            margin_required = required;
            reasons.push(message);
        }
        let decay = d3::decay_target_mark(credit, f3).map_err(fe)?;
        let cut = d3::loss_cut_mark(credit, f3).map_err(fe)?;
        let width = spread.width().map_err(fe)?;
        let mut detail = base.clone();
        detail.insert("reasons", strs(&reasons));
        detail.insert(
            "mids",
            PyValue::Map(dict(mids.iter().map(|(r, m)| (r.value(), ds(*m))))),
        );
        detail.insert("lots_sized", PyValue::Int(lots));
        detail.insert("width_points", ds(width));
        detail.insert("expiry", PyValue::Date(expiry));
        detail.insert("decay_target_mark", ds(money(decay)));
        detail.insert("loss_cut_mark", ds(money(cut)));
        detail.insert(
            "sizing",
            PyValue::Map(dict([
                ("lots", PyValue::Int(sizing.lots)),
                ("message", PyValue::Str(sizing.message.clone())),
                ("lots_at_ceiling", olots(sizing.lots_at_ceiling)),
            ])),
        );
        detail.insert("margin_required_inr", ods(margin_required));
        detail.insert(
            "rule",
            PyValue::Str(format!(
                "out at once if {} trades beyond {}; the cut at a mark of {}; the target at {}; \
                 flat at {} on {}",
                row.symbol,
                py::dstr(level),
                py::dstr(money(cut)),
                py::dstr(money(decay)),
                self.config.common.hard_exit_time.format("%H:%M"),
                expiry.format("%Y-%m-%d")
            )),
        );
        let status = state_now.map_or(state::ISSUED, plan_state);
        let reason = state_now.map(|_| reasons.join("; "));
        let mut h = head(
            &plan_id,
            sleeve,
            &row.symbol,
            day,
            structure::CREDIT_SPREAD,
            issued,
            exp,
            lots.max(1),
            lot,
            status,
            reason,
            detail,
            kind::ENTRY,
        );
        h.set("hard_exit_date", Val::Date(expiry));
        h.set("credit_points", Val::Dec(money(credit)));
        h.set("width_points", Val::Dec(width));
        h.set("risk_per_lot_inr", Val::Dec(money(sizing.risk_per_lot_inr)));
        h.set("risk_budget_inr", Val::Dec(money(sizing.risk_budget_inr)));
        h.set("max_loss_inr", Val::Dec(money(sizing.max_loss_inr)));
        h.set("margin_required_inr", Val::dec(margin_required));
        h.set(
            "sizing_mode",
            Val::Str(sizing_mode(sizing.sizing_mode).to_owned()),
        );
        let mut legs: Vec<Leg> = Vec::new();
        for (seq, role) in roles.into_iter().enumerate() {
            let c = contracts.get(&role).ok_or_else(|| key(role))?;
            let q = books.get(&c.tradingsymbol);
            legs.push(vec![
                ("entry_seq", Val::Int(i64::try_from(seq).unwrap_or(0) + 1)),
                ("role", Val::Str(role.value().to_owned())),
                ("tradingsymbol", Val::Str(c.tradingsymbol.clone())),
                ("instrument_token", Val::Int(c.instrument_token)),
                ("expiry", Val::Date(c.expiry)),
                ("strike", Val::dec(strikes.get(&role).copied())),
                (
                    "option_type",
                    Val::Str(option_type_value(option_type_of(role)).to_owned()),
                ),
                (
                    "side",
                    Val::Str(if sign_of(role) > 0 { "BUY" } else { "SELL" }.to_owned()),
                ),
                ("quantity", Val::Int(quantity)),
                ("reference_price", Val::dec(mids.get(&role).copied())),
                ("bid", Val::dec(q.and_then(|q| q.bid))),
                ("ask", Val::dec(q.and_then(|q| q.ask))),
            ]);
        }
        store.insert_plan(&h.0, &legs).await?;
        Ok(Some(plan_id))
    }

    /// `_f3_open(position)`: the pure core's view of an open F3 position.
    pub(super) fn f3_open(position: &PositionRow) -> Result<Option<OpenSpread>, ExecError> {
        let carry = position.carry();
        let legs = position.leg_list();
        let short = legs
            .iter()
            .find(|x| get_text(x, "role").starts_with("SHORT"));
        let wing = legs
            .iter()
            .find(|x| get_text(x, "role").starts_with("LONG"));
        let expiry = short.and_then(|s| py::opt_day_py(s.get("expiry")));
        let (Some(short), Some(wing), Some(expiry), Some(level), Some(credit), Some(max_loss)) = (
            short,
            wing,
            expiry,
            position.stop_price,
            position.entry_credit,
            position.max_loss_inr,
        ) else {
            return Ok(None);
        };
        let kind_ = OptionType::parse(&get_text(short, "option_type")).map_err(fe)?;
        let strikes = SpreadStrikes {
            option_type: kind_,
            short: py::dec_py(short.get("strike")).unwrap_or(Decimal::ZERO),
            wing: py::dec_py(wing.get("strike")).unwrap_or(Decimal::ZERO),
        };
        let per_lot =
            baskfy_core::fno::config::py::div(max_loss, Decimal::from(position.lots.max(1)))
                .map_err(fe)?;
        Ok(Some(OpenSpread {
            direction: direction_in(&carry)?,
            level,
            strikes,
            expiry,
            entry_session: ist_day(position.opened_at),
            entry_credit: credit,
            lots: position.lots,
            lot_size: position.lot_size,
            max_loss_per_lot_inr: per_lot,
        }))
    }

    /// `_f3_mark(position)`: the spread's live mark per unit, the short's mid less the wing's.
    async fn f3_mark(&self, position: &PositionRow) -> Result<Option<Decimal>, ExecError> {
        let mut symbols: Vec<(String, String)> = Vec::new();
        for x in position.leg_list() {
            let r = get_text(&x, "role");
            let s = get_text(&x, "tradingsymbol");
            if let Some(slot) = symbols.iter_mut().find(|(k, _)| *k == r) {
                slot.1 = s;
            } else {
                symbols.push((r, s));
            }
        }
        let wanted: Vec<String> = symbols.iter().map(|(_, s)| s.clone()).collect();
        let books = self.books(&wanted).await?;
        let pick = |prefix: &str| {
            symbols
                .iter()
                .find(|(r, _)| r.starts_with(prefix))
                .and_then(|(_, s)| books.get(s))
        };
        let short = pick("SHORT");
        let wing = pick("LONG");
        let Some(short_mid) = short.and_then(super::super::store::FoQuote::mid) else {
            return Ok(None);
        };
        let wing_mid = wing
            .and_then(super::super::store::FoQuote::mid)
            .unwrap_or(Decimal::ZERO);
        Ok(Some(short_mid - wing_mid))
    }

    async fn f3_decision(
        &self,
        position: &PositionRow,
        now: DateTime<FixedOffset>,
    ) -> Result<Option<ExitDecision>, ExecError> {
        let Some(spread) = Self::f3_open(position)? else {
            self.alert(&format!(
                "F3 position {} lacks its level, credit or legs; not watched",
                position.id
            ));
            return Ok(None);
        };
        let index = self.f3_index(&position.symbol).await;
        let mark = self.f3_mark(position).await?;
        let due = d3::f3_exit(
            naive_ist(now),
            &spread,
            index.map(|i| i.last),
            mark,
            &self.config.f3,
            &self.config.common,
        )
        .map_err(fe)?;
        Ok(due.map(|e| ExitDecision {
            reason: match e.reason {
                F3ExitReason::LevelBreak => ExitReason::LevelBreak,
                F3ExitReason::LossCut => ExitReason::LossCut,
                F3ExitReason::DecayTarget => ExitReason::DecayTarget,
                F3ExitReason::HardExit => ExitReason::HardExit,
            },
            message: e.message,
        }))
    }

    /// `_raise_f3_add(position, now)`: `04` §11's add, raised for the click (never sent here).
    async fn raise_f3_add(
        &self,
        store: &mut FoStore<'_>,
        position: &PositionRow,
        now: DateTime<FixedOffset>,
    ) -> Result<Option<String>, ExecError> {
        let spread = Self::f3_open(position)?;
        let Some(spread) = spread else {
            return Ok(None);
        };
        if position.carry().get("added").is_some_and(PyValue::truthy) {
            return Ok(None);
        }
        let day = ist_day(now);
        let add_id = format!("{}-A1", position.entry_plan_id);
        if store.plan(&add_id).await?.is_some() {
            return Ok(None);
        }
        let rows = scan_candidates(store, &[position.sleeve.value()], day, "OPEN_POSITION").await?;
        let row = rows.iter().find(|r| r.symbol == position.symbol);
        let direction_now = match row {
            Some(r) => direction_in(&r.detail)?,
            None => Direction::None,
        };
        let index = self.f3_index(&position.symbol).await;
        let intact = match index {
            Some(i) => !d3::level_broken(spread.direction, spread.level, i.last, &self.config.f3)
                .map_err(fe)?,
            None => false,
        };
        let mark = self.f3_mark(position).await?;
        let capital = sleeve_capital(store, "F3").await?;
        let decision = d3::add_decision(
            &spread,
            day,
            direction_now,
            intact,
            mark,
            capital,
            &self.config.f3,
            &self.config.common,
        )
        .map_err(fe)?;
        if !decision.allowed() {
            return Ok(None);
        }
        let Some(entry) = store.plan(&position.entry_plan_id).await? else {
            return Ok(None);
        };
        let quantity = decision.lots * position.lot_size;
        let exp = expires(now, &self.config)?;
        let detail = dict([
            ("position_id", PyValue::Int(position.id)),
            ("reason", PyValue::Str(decision.message.clone())),
            (
                "direction",
                PyValue::Str(spread.direction.value().to_owned()),
            ),
            ("level", ds(spread.level)),
            ("under_confirm_of", PyValue::None),
            ("violations", PyValue::List(Vec::new())),
        ]);
        let mut h = head(
            &add_id,
            position.sleeve,
            &position.symbol,
            day,
            structure::CREDIT_SPREAD,
            now,
            exp,
            decision.lots,
            position.lot_size,
            state::ISSUED,
            None,
            detail,
            kind::ADD,
        );
        h.set("parent_plan_id", Val::Str(entry.plan_id.clone()));
        h.set("hard_exit_date", Val::date(position.hard_exit_date));
        h.set(
            "width_points",
            Val::Dec(spread.strikes.width().map_err(fe)?),
        );
        let mut sorted = position.leg_list();
        sorted.sort_by_key(|x| usize::from(!get_text(x, "role").starts_with("LONG")));
        let mut legs: Vec<Leg> = Vec::new();
        for (seq, leg) in sorted.iter().enumerate() {
            let role = get_text(leg, "role");
            legs.push(vec![
                ("entry_seq", Val::Int(i64::try_from(seq).unwrap_or(0) + 1)),
                ("role", Val::Str(role.clone())),
                ("tradingsymbol", Val::Str(get_text(leg, "tradingsymbol"))),
                (
                    "instrument_token",
                    Val::Int(py::py_int(leg.get("instrument_token"), 0)),
                ),
                ("expiry", Val::date(py::opt_day_py(leg.get("expiry")))),
                ("strike", Val::dec(py::dec_py(leg.get("strike")))),
                ("option_type", Val::Str(get_text(leg, "option_type"))),
                (
                    "side",
                    Val::Str(
                        if role.starts_with("LONG") {
                            "BUY"
                        } else {
                            "SELL"
                        }
                        .to_owned(),
                    ),
                ),
                ("quantity", Val::Int(quantity)),
            ]);
        }
        store.insert_plan(&h.0, &legs).await?;
        Ok(Some(add_id))
    }

    #[allow(clippy::too_many_lines)]
    async fn raise_f2(
        &self,
        store: &mut FoStore<'_>,
        row: &ScanRow,
        now: DateTime<FixedOffset>,
        sessions: &[NaiveDate],
    ) -> Result<Option<String>, ExecError> {
        let day = ist_day(now);
        let plan_id = entry_plan_id(FoSleeve::F2, day, &row.symbol, store.user_id);
        if store.plan(&plan_id).await?.is_some() {
            return Ok(None);
        }
        let d = &row.detail;
        let expiry = py::opt_day_py(d.get("contract_expiry"));
        let contract = match expiry {
            Some(e) => self.instruments.future(&row.symbol, e).await?,
            None => None,
        };
        let issued = now;
        let exp = expires(issued, &self.config)?;
        let mut base = dict([
            ("scan_id", PyValue::Int(row.id)),
            ("scan_date", PyValue::Date(row.trade_date)),
            ("violations", PyValue::List(Vec::new())),
            ("atr14", d.get("atr14").cloned().unwrap_or(PyValue::None)),
            (
                "industry",
                d.get("industry").cloned().unwrap_or(PyValue::None),
            ),
        ]);
        let (Some(contract), Some(expiry)) = (contract, expiry) else {
            let reason = format!(
                "{}: no future for {} in the NFO master",
                row.symbol,
                get_text(d, "contract_expiry")
            );
            base.insert("reasons", PyValue::List(vec![PyValue::Str(reason.clone())]));
            let h = head(
                &plan_id,
                FoSleeve::F2,
                &row.symbol,
                day,
                structure::FUTURE,
                issued,
                exp,
                1,
                py::py_int(d.get("lot_size"), 1),
                plan_state(PlanState::RejectedStructure),
                Some(reason),
                base,
                kind::ENTRY,
            );
            store.insert_plan(&h.0, &[]).await?;
            return Ok(Some(plan_id));
        };
        let books = self
            .books(std::slice::from_ref(&contract.tradingsymbol))
            .await?;
        let book = books.get(&contract.tradingsymbol);
        let ann_vol = row.rv20.map_or(0.0, crate::lanes::options::execute::to_f64);
        let capital = sleeve_capital(store, "F2").await?;
        let rep = reprice_future(
            book.and_then(|b| b.bid),
            book.and_then(|b| b.ask),
            or_dec(py::dec_py(d.get("atr14")), Decimal::ZERO),
            ann_vol,
            contract.lot_size,
            capital,
            (self.mode_of)(FoSleeve::F2),
            &self.config,
            &self.ceilings,
        )
        .map_err(fe)?;
        let mut state_now = rep.state;
        let mut reasons = rep.reasons.clone();
        let lots = rep
            .sizing
            .as_ref()
            .map_or(0, |s| if s.lots > 0 { s.lots } else { 0 });
        let quantity = lots.max(1) * contract.lot_size;
        let mut margin_required = None;
        if state_now.is_none()
            && let Some(entry) = rep.entry
        {
            let (s, message, required) = self
                .margin(&[MarginLeg {
                    tradingsymbol: contract.tradingsymbol.clone(),
                    side: "BUY".to_owned(),
                    quantity,
                    price: entry,
                }])
                .await?;
            state_now = s;
            margin_required = required;
            reasons.push(message);
        }
        let time_exit = f2_time_exit_session(sessions, day, &self.config.f2).map_err(fe)?;
        let roll = roll_session(sessions, expiry, self.config.f2.roll_before_expiry).map_err(fe)?;
        base.insert(
            "reasons",
            PyValue::List(reasons.iter().cloned().map(PyValue::Str).collect()),
        );
        base.insert("ann_vol", PyValue::Float(ann_vol));
        base.insert("stop", ods(rep.stop));
        base.insert("gtt_trigger", ods(rep.trigger));
        base.insert("entry_reference", ods(rep.entry));
        base.insert("lots_sized", PyValue::Int(lots));
        base.insert(
            "lots_at_ceiling",
            olots(rep.sizing.as_ref().and_then(|s| s.lots_at_ceiling)),
        );
        base.insert("time_exit_date", odate(time_exit));
        base.insert("roll_date", odate(roll));
        base.insert("margin_required_inr", ods(margin_required));
        let status = state_now.map_or(state::ISSUED, plan_state);
        let reason = state_now.map(|_| reasons.join("; "));
        let mut h = head(
            &plan_id,
            FoSleeve::F2,
            &row.symbol,
            day,
            structure::FUTURE,
            issued,
            exp,
            lots.max(1),
            contract.lot_size,
            status,
            reason,
            base,
            kind::ENTRY,
        );
        let sz = rep.sizing.as_ref();
        h.set("debit_points", Val::dec(rep.entry));
        h.set(
            "risk_per_lot_inr",
            Val::dec(sz.map(|s| money(s.risk_per_lot_inr))),
        );
        h.set(
            "risk_budget_inr",
            Val::dec(sz.map(|s| money(s.risk_budget_inr))),
        );
        h.set("max_loss_inr", Val::dec(sz.map(|s| money(s.max_loss_inr))));
        let cost = match rep.cost.as_ref() {
            Some(c) => Some(c.total().map_err(fe)?),
            None => None,
        };
        h.set("expected_cost_inr", Val::dec(cost));
        h.set("margin_required_inr", Val::dec(margin_required));
        h.set(
            "sizing_mode",
            Val::Str(
                sz.map_or("BUDGET", |s| sizing_mode(s.sizing_mode))
                    .to_owned(),
            ),
        );
        let leg: Leg = vec![
            ("entry_seq", Val::Int(1)),
            ("role", Val::Str(FUTURE_ROLE.to_owned())),
            ("tradingsymbol", Val::Str(contract.tradingsymbol.clone())),
            ("instrument_token", Val::Int(contract.instrument_token)),
            ("expiry", Val::Date(contract.expiry)),
            ("strike", Val::None),
            ("option_type", Val::Str(FUTURE_TYPE.to_owned())),
            ("side", Val::Str("BUY".to_owned())),
            ("quantity", Val::Int(quantity)),
            (
                "reference_price",
                Val::dec(book.and_then(super::super::store::FoQuote::mid)),
            ),
            ("bid", Val::dec(book.and_then(|b| b.bid))),
            ("ask", Val::dec(book.and_then(|b| b.ask))),
        ];
        store.insert_plan(&h.0, &[leg]).await?;
        Ok(Some(plan_id))
    }

    // watching positions -----------------------------------------------------------------------

    /// `watch(now)`: every open position without a pending action, checked on live marks;
    /// the EXIT or ROLL plans raised.
    ///
    /// # Errors
    /// As [`FnoMonitor::tick`].
    pub async fn watch(
        &self,
        store: &mut FoStore<'_>,
        now: DateTime<FixedOffset>,
    ) -> Result<Vec<String>, ExecError> {
        let busy: Vec<String> = store
            .pending_actions()
            .await?
            .iter()
            .map(|p| get_text(&p.detail, "position_id"))
            .collect();
        let mut raised = Vec::new();
        for position in store.open_positions().await? {
            if busy.contains(&position.id.to_string()) {
                continue;
            }
            let decision = if position.structure == structure::IRON_CONDOR {
                self.f1_decision(&position, now).await?
            } else if position.structure == structure::CREDIT_SPREAD {
                self.f3_decision(&position, now).await?
            } else {
                self.f2_decision(&position, now).await?
            };
            let Some(decision) = decision else { continue };
            let id = if decision.reason == ExitReason::Roll {
                self.raise_roll(store, &position, now).await?
            } else {
                self.raise_exit(store, &position, &decision, now).await?
            };
            if let Some(id) = id {
                raised.push(id);
            }
        }
        Ok(raised)
    }

    async fn f1_decision(
        &self,
        position: &PositionRow,
        now: DateTime<FixedOffset>,
    ) -> Result<Option<ExitDecision>, ExecError> {
        let carry = position.carry();
        let (Some(strikes), Some(hard), Some(credit)) = (
            strikes_of(&carry),
            position.hard_exit_date,
            position.entry_credit,
        ) else {
            self.alert(&format!(
                "F1 position {} lacks its strikes or dates; not watched",
                position.id
            ));
            return Ok(None);
        };
        let mut symbols: Vec<(String, String)> = Vec::new();
        for x in position.leg_list() {
            let r = get_text(&x, "role");
            let s = get_text(&x, "tradingsymbol");
            if let Some(slot) = symbols.iter_mut().find(|(k, _)| *k == r) {
                slot.1 = s;
            } else {
                symbols.push((r, s));
            }
        }
        let wanted: Vec<String> = symbols.iter().map(|(_, s)| s.clone()).collect();
        let books = self.books(&wanted).await?;
        let mut marks = RoleMap::new();
        for role in LegRole::ALL {
            let sym = symbols
                .iter()
                .find(|(r, _)| r == role.value())
                .map_or("", |(_, s)| s.as_str());
            if let Some(m) = books.get(sym).and_then(super::super::store::FoQuote::mid) {
                marks.insert(role, m);
            }
        }
        let close_cost = f1_close_cost(&strikes, &marks).map_err(fe)?;
        f1_exit(
            naive_ist(now),
            hard,
            close_cost,
            credit,
            &strikes,
            &self.config.f1,
            &self.config.common,
        )
        .map_err(fe)
    }

    async fn f2_decision(
        &self,
        position: &PositionRow,
        now: DateTime<FixedOffset>,
    ) -> Result<Option<ExitDecision>, ExecError> {
        let legs = position.leg_list();
        let Some(first) = legs.first() else {
            return Ok(None);
        };
        let symbol = get_text(first, "tradingsymbol");
        let books = self.books(std::slice::from_ref(&symbol)).await?;
        let price = books.get(&symbol).and_then(|q| match q.last {
            Some(l) if !l.is_zero() => Some(l),
            _ => q.mid(),
        });
        let carry = position.carry();
        f2_action(
            naive_ist(now),
            price,
            position.stop_price.unwrap_or(Decimal::ZERO),
            py::opt_day_py(carry.get("time_exit_date")),
            position.next_roll_date,
            position.gtt_id.is_none(),
            &self.config.common,
        )
        .map_err(fe)
    }

    /// `_raise_exit(position, decision, now)`: the EXIT plan `<entry>-X`, legs in exit order
    /// (shorts first), `CONFIRMED` under the entry's confirm — or, for an F3 spread with M.5's
    /// flag off, `ISSUED` for the click. `None` when it exists already or nothing is held.
    ///
    /// # Errors
    /// The database.
    #[allow(clippy::too_many_lines)]
    pub async fn raise_exit(
        &self,
        store: &mut FoStore<'_>,
        position: &PositionRow,
        decision: &ExitDecision,
        now: DateTime<FixedOffset>,
    ) -> Result<Option<String>, ExecError> {
        let Some(entry) = store.plan(&position.entry_plan_id).await? else {
            self.alert(&format!(
                "FO position {}: entry plan {} is gone",
                position.id, position.entry_plan_id
            ));
            return Ok(None);
        };
        let exit_id = format!("{}-X", position.entry_plan_id);
        if store.plan(&exit_id).await?.is_some() {
            return Ok(None);
        }
        let net = store.net_by_symbol(position.id).await?;
        let order: Vec<&str> = EXIT_SEQUENCE
            .iter()
            .map(|r| r.value())
            .chain(std::iter::once(FUTURE_ROLE))
            .collect();
        // `{tradingsymbol: leg}` (last wins, first position kept), then sorted by role order.
        let mut by_symbol: Vec<(String, PyDict)> = Vec::new();
        for leg in position.leg_list() {
            let sym = get_text(&leg, "tradingsymbol");
            if let Some(slot) = by_symbol.iter_mut().find(|(s, _)| *s == sym) {
                slot.1 = leg;
            } else {
                by_symbol.push((sym, leg));
            }
        }
        let mut sorted: Vec<PyDict> = Vec::new();
        for (_, leg) in by_symbol {
            let role = get_text(&leg, "role");
            if !order.contains(&role.as_str()) {
                return Err(ExecError::Other(format!(
                    "ValueError: '{role}' is not in list"
                )));
            }
            sorted.push(leg);
        }
        sorted.sort_by_key(|leg| {
            let role = get_text(leg, "role");
            order.iter().position(|r| *r == role).unwrap_or(usize::MAX)
        });
        let mut legs: Vec<Leg> = Vec::new();
        for leg in &sorted {
            let sym = get_text(leg, "tradingsymbol");
            let units = net.iter().find(|(s, _)| *s == sym).map_or(0, |(_, n)| *n);
            if units == 0 {
                continue;
            }
            legs.push(vec![
                (
                    "entry_seq",
                    Val::Int(i64::try_from(legs.len()).unwrap_or(0) + 1),
                ),
                ("role", Val::Str(get_text(leg, "role"))),
                ("tradingsymbol", Val::Str(sym)),
                (
                    "instrument_token",
                    Val::Int(py::py_int(leg.get("instrument_token"), 0)),
                ),
                ("expiry", Val::date(py::opt_day_py(leg.get("expiry")))),
                ("strike", Val::dec(py::dec_py(leg.get("strike")))),
                ("option_type", Val::Str(get_text(leg, "option_type"))),
                (
                    "side",
                    Val::Str(if units > 0 { "SELL" } else { "BUY" }.to_owned()),
                ),
                ("quantity", Val::Int(units.abs())),
            ]);
        }
        if legs.is_empty() {
            self.alert(&format!(
                "FO position {} is open with no units; nothing to close",
                position.id
            ));
            return Ok(None);
        }
        let day = ist_day(now);
        let close = py::ist_at(day, session_close())
            .ok_or_else(|| ExecError::Other("ValueError: no 15:30 IST".to_owned()))?;
        let a_minute = now + Duration::minutes(1);
        let exp = if a_minute > close { a_minute } else { close };
        let by_click =
            exit_awaits_click(&position.structure, (self.exit_by_monitor)(position.sleeve));
        let code = reason_value(decision.reason);
        let detail = dict([
            ("code", PyValue::Str(code.to_owned())),
            ("reason", PyValue::Str(decision.message.clone())),
            ("position_id", PyValue::Int(position.id)),
            ("under_confirm_of", PyValue::Str(entry.plan_id.clone())),
            ("awaits_click", PyValue::Bool(by_click)),
        ]);
        let mut h = head(
            &exit_id,
            position.sleeve,
            &position.symbol,
            day,
            &position.structure,
            now,
            exp,
            position.lots,
            position.lot_size,
            exit_status(by_click),
            Some(decision.message.clone()),
            detail,
            kind::EXIT,
        );
        h.set("parent_plan_id", Val::Str(entry.plan_id.clone()));
        h.set(
            "confirmed_at",
            if by_click {
                Val::None
            } else {
                entry.confirmed_at.map_or(Val::None, Val::At)
            },
        );
        h.set("hard_exit_date", Val::date(position.hard_exit_date));
        h.set("entry_window_end", Val::None);
        store.insert_plan(&h.0, &legs).await?;
        if by_click {
            self.alert(&format!(
                "F3 {}: {code} — {}; the exit {exit_id} waits for the click on /fno (the monitor \
                 may not send it)",
                position.symbol, decision.message
            ));
        }
        if decision.reason == ExitReason::LateExit {
            store
                .add_violation(&entry.plan_id, "LATE_EXIT", &decision.message, now)
                .await?;
            self.alert(&format!(
                "FO {}: LATE_EXIT — {}",
                entry.plan_id, decision.message
            ));
        }
        Ok(Some(exit_id))
    }

    /// `_raise_roll(position, now)`: F2's `E - 1` roll under the entry's confirm, or the exit
    /// when there is no next month to roll into.
    async fn raise_roll(
        &self,
        store: &mut FoStore<'_>,
        position: &PositionRow,
        now: DateTime<FixedOffset>,
    ) -> Result<Option<String>, ExecError> {
        let entry = store.plan(&position.entry_plan_id).await?;
        let legs_now = position.leg_list();
        let (Some(entry), Some(held)) = (entry, legs_now.first()) else {
            return Ok(None);
        };
        let held_expiry = py::opt_day_py(held.get("expiry"));
        let nxt = match held_expiry {
            Some(e) => self.instruments.next_future(&position.symbol, e).await?,
            None => None,
        };
        let Some(nxt) = nxt else {
            // nothing to roll into: flat by E-1 all the same
            let decision = ExitDecision {
                reason: ExitReason::HardExit,
                message: "no next-month future to roll into; flat by E-1 (02 §2.2)".to_owned(),
            };
            return self.raise_exit(store, position, &decision, now).await;
        };
        let rolls = py::py_int(position.carry().get("rolls"), 0);
        let roll_id = format!("{}-R{}", position.entry_plan_id, rolls + 1);
        if store.plan(&roll_id).await?.is_some() {
            return Ok(None);
        }
        let held_symbol = get_text(held, "tradingsymbol");
        let units = store
            .net_by_symbol(position.id)
            .await?
            .iter()
            .find(|(s, _)| *s == held_symbol)
            .map_or(0, |(_, n)| *n);
        if units <= 0 {
            return Ok(None);
        }
        let day = ist_day(now);
        let close = py::ist_at(day, session_close())
            .ok_or_else(|| ExecError::Other("ValueError: no 15:30 IST".to_owned()))?;
        let a_minute = now + Duration::minutes(1);
        let exp = if a_minute > close { a_minute } else { close };
        let sessions = self.market.sessions().await?;
        let next_roll =
            roll_session(&sessions, nxt.expiry, self.config.f2.roll_before_expiry).map_err(fe)?;
        let detail = dict([
            ("code", PyValue::Str("ROLL".to_owned())),
            ("position_id", PyValue::Int(position.id)),
            ("under_confirm_of", PyValue::Str(entry.plan_id.clone())),
            (
                "rolled_from",
                held.get("tradingsymbol").cloned().unwrap_or(PyValue::None),
            ),
            ("next_roll_date", odate(next_roll)),
        ]);
        let mut h = head(
            &roll_id,
            position.sleeve,
            &position.symbol,
            day,
            structure::FUTURE,
            now,
            exp,
            position.lots,
            nxt.lot_size,
            state::ISSUED,
            Some("15:00 on E-1: the held month sold, the next bought, stop carried".to_owned()),
            detail,
            kind::ROLL,
        );
        h.set("parent_plan_id", Val::Str(entry.plan_id.clone()));
        h.set(
            "confirmed_at",
            entry.confirmed_at.map_or(Val::None, Val::At),
        );
        h.set("entry_window_end", Val::None);
        let legs: Vec<Leg> = vec![
            vec![
                ("entry_seq", Val::Int(1)),
                ("role", Val::Str(FUTURE_ROLE.to_owned())),
                ("tradingsymbol", Val::Str(held_symbol)),
                (
                    "instrument_token",
                    Val::Int(py::py_int(held.get("instrument_token"), 0)),
                ),
                ("expiry", Val::date(held_expiry)),
                ("strike", Val::None),
                ("option_type", Val::Str(FUTURE_TYPE.to_owned())),
                ("side", Val::Str("SELL".to_owned())),
                ("quantity", Val::Int(units)),
            ],
            vec![
                ("entry_seq", Val::Int(2)),
                ("role", Val::Str(FUTURE_ROLE.to_owned())),
                ("tradingsymbol", Val::Str(nxt.tradingsymbol.clone())),
                ("instrument_token", Val::Int(nxt.instrument_token)),
                ("expiry", Val::Date(nxt.expiry)),
                ("strike", Val::None),
                ("option_type", Val::Str(FUTURE_TYPE.to_owned())),
                ("side", Val::Str("BUY".to_owned())),
                ("quantity", Val::Int(units)),
            ],
        ];
        store.insert_plan(&h.0, &legs).await?;
        Ok(Some(roll_id))
    }

    pub(super) fn gateway_or_err(&self, sleeve: FoSleeve) -> Result<Arc<dyn Gateway>, ExecError> {
        self.gateway(sleeve)
    }
}

/// `log.error` on `desk.fno_monitor` (the default alert).
#[allow(clippy::print_stderr)]
pub fn log_error(message: &str) {
    eprintln!("ERROR:desk.fno_monitor:{message}");
}

#[allow(clippy::print_stderr)]
fn log_warning(message: &str) {
    eprintln!("WARNING:desk.fno_monitor:{message}");
}
