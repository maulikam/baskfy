//! The pure pieces of `baskfy_core.options` the desk's executors call that `baskfy-core` has
//! not ported yet: `executor` (`run_entry`, `run_exit`), the sequencing reducers and attempts of
//! `execution` (`entry_sequence`, `advance_entry`, `next_exit_leg`, `next_attempt`), the
//! expiry-day slot (§8.6), `risk` (§9), `ledger.pnl_points`, the structures' enums and
//! `OptionsConfig.hard_exit_time`.
//!
//! Pure (law 1): no I/O, no clock. Kept beside the desk lane rather than in `baskfy-core`
//! because this wave's lanes may not edit another crate's modules (DECISIONS-RS R8.5); moving
//! them under `baskfy_core::options` is a mechanical follow-up (R8.F1).

use std::collections::BTreeMap;

use chrono::{Datelike, Duration, NaiveDate, NaiveTime};
use rust_decimal::Decimal;

pub use baskfy_core::options::costs::Side;
pub use baskfy_core::options::execution::{ExecutionConfig, LegRole, Level, never_naked};
pub use baskfy_core::options::gating::{Mode, Sleeve, SleeveGroup, group_of};

use super::py;

/// An executor contract violation (Python's `AssertionError`/`ValueError`).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{kind}: {message}")]
pub struct CoreViolation {
    /// `AssertionError` / `ValueError`.
    pub kind: &'static str,
    /// The message.
    pub message: String,
}

fn value_error(message: impl Into<String>) -> CoreViolation {
    CoreViolation {
        kind: "ValueError",
        message: message.into(),
    }
}

/// `LegRole.value`.
#[must_use]
pub const fn role_str(role: LegRole) -> &'static str {
    match role {
        LegRole::LongCall => "LONG_CALL",
        LegRole::LongPut => "LONG_PUT",
        LegRole::ShortCall => "SHORT_CALL",
        LegRole::ShortPut => "SHORT_PUT",
    }
}

/// `LegRole(text)`.
#[must_use]
pub fn role_of(text: &str) -> Option<LegRole> {
    match text {
        "LONG_CALL" => Some(LegRole::LongCall),
        "LONG_PUT" => Some(LegRole::LongPut),
        "SHORT_CALL" => Some(LegRole::ShortCall),
        "SHORT_PUT" => Some(LegRole::ShortPut),
        _ => None,
    }
}

/// `Side.value`.
#[must_use]
pub const fn side_str(side: Side) -> &'static str {
    match side {
        Side::Buy => "BUY",
        Side::Sell => "SELL",
    }
}

/// `Side(text)`.
#[must_use]
pub fn side_of(text: &str) -> Option<Side> {
    match text {
        "BUY" => Some(Side::Buy),
        "SELL" => Some(Side::Sell),
        _ => None,
    }
}

/// `Sleeve.value`.
#[must_use]
pub const fn sleeve_str(sleeve: Sleeve) -> &'static str {
    match sleeve {
        Sleeve::O1m => "O1M",
        Sleeve::O1w => "O1W",
        Sleeve::O2 => "O2",
        Sleeve::O3a => "O3A",
        Sleeve::O3b => "O3B",
    }
}

/// `Sleeve(text)`.
#[must_use]
pub fn sleeve_of(text: &str) -> Option<Sleeve> {
    match text {
        "O1M" => Some(Sleeve::O1m),
        "O1W" => Some(Sleeve::O1w),
        "O2" => Some(Sleeve::O2),
        "O3A" => Some(Sleeve::O3a),
        "O3B" => Some(Sleeve::O3b),
        _ => None,
    }
}

/// `SleeveGroup.value`.
#[must_use]
pub const fn group_str(group: SleeveGroup) -> &'static str {
    match group {
        SleeveGroup::O1m => "O1M",
        SleeveGroup::O1w => "O1W",
        SleeveGroup::O2 => "O2",
        SleeveGroup::O3 => "O3",
    }
}

/// Every sleeve, in declaration order (`for sleeve in Sleeve`).
pub const SLEEVES: [Sleeve; 5] = [
    Sleeve::O1m,
    Sleeve::O1w,
    Sleeve::O2,
    Sleeve::O3a,
    Sleeve::O3b,
];

/// `Mode.value`.
#[must_use]
pub const fn mode_str(mode: Mode) -> &'static str {
    match mode {
        Mode::Paper => "PAPER",
        Mode::Live => "LIVE",
    }
}

/// `baskfy_core.options.structures.Structure`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Structure {
    /// `IRON_CONDOR`.
    IronCondor,
    /// `LONG_OPTION`.
    LongOption,
    /// `DEBIT_SPREAD`.
    DebitSpread,
}

impl Structure {
    /// `Structure(text)`.
    #[must_use]
    pub fn parse(text: &str) -> Option<Self> {
        match text {
            "IRON_CONDOR" => Some(Self::IronCondor),
            "LONG_OPTION" => Some(Self::LongOption),
            "DEBIT_SPREAD" => Some(Self::DebitSpread),
            _ => None,
        }
    }

    /// `.value`.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::IronCondor => "IRON_CONDOR",
            Self::LongOption => "LONG_OPTION",
            Self::DebitSpread => "DEBIT_SPREAD",
        }
    }
}

/// `baskfy_core.options.structures.Direction`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Direction {
    /// `UP`.
    Up,
    /// `DOWN`.
    Down,
}

impl Direction {
    /// `Direction(text)`.
    #[must_use]
    pub fn parse(text: &str) -> Option<Self> {
        match text {
            "UP" => Some(Self::Up),
            "DOWN" => Some(Self::Down),
            _ => None,
        }
    }
}

/// `OptionsConfig().hard_exit_time(sleeve)`: O1 14:30, O2 15:00, O3 14:45 (`04` §3.2, §4.5,
/// §5.3).
#[must_use]
pub fn hard_exit_time(sleeve: Sleeve) -> NaiveTime {
    let hm = |h, m| NaiveTime::from_hms_opt(h, m, 0).unwrap_or(NaiveTime::MIN);
    match group_of(sleeve) {
        SleeveGroup::O1m | SleeveGroup::O1w => hm(14, 30),
        SleeveGroup::O2 => hm(15, 0),
        SleeveGroup::O3 => hm(14, 45),
    }
}

/// `ledger.pnl_points`: a condor's credit less the cost to close it; a long's or a spread's
/// close value less what was paid.
#[must_use]
pub fn pnl_points(structure: Structure, entry_points: Decimal, value_points: Decimal) -> Decimal {
    if structure == Structure::IronCondor {
        entry_points - value_points
    } else {
        value_points - entry_points
    }
}

// --- sequencing (`04` §8.3) ----------------------------------------------------------------------

fn entry_rank(role: LegRole) -> u8 {
    match role {
        LegRole::LongPut => 0,
        LegRole::LongCall => 1,
        LegRole::ShortPut => 2,
        LegRole::ShortCall => 3,
    }
}

fn exit_rank(role: LegRole) -> u8 {
    match role {
        LegRole::ShortCall => 0,
        LegRole::ShortPut => 1,
        LegRole::LongCall => 2,
        LegRole::LongPut => 3,
    }
}

/// `entry_sequence(roles)`: every long before any short; a short with no protecting long is
/// refused.
///
/// # Errors
/// A repeated role, or a naked short (`ValueError`).
pub fn entry_sequence(roles: &[LegRole]) -> Result<Vec<LegRole>, CoreViolation> {
    let mut seen = roles.to_vec();
    seen.sort_unstable();
    seen.dedup();
    if seen.len() != roles.len() {
        return Err(value_error("a structure has at most one leg per role"));
    }
    for role in roles {
        if let Some(p) = role.protector()
            && !roles.contains(&p)
        {
            return Err(value_error(format!(
                "{} has no {}: a naked short is Track C",
                role_str(*role),
                role_str(p)
            )));
        }
    }
    let mut out = roles.to_vec();
    out.sort_by_key(|r| entry_rank(*r));
    Ok(out)
}

/// `exit_sequence(roles)`: every short before any long.
#[must_use]
pub fn exit_sequence(roles: impl IntoIterator<Item = LegRole>) -> Vec<LegRole> {
    let mut out: Vec<LegRole> = roles.into_iter().collect();
    out.sort_unstable();
    out.dedup();
    out.sort_by_key(|r| exit_rank(*r));
    out
}

/// What the entry reducer says next.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EntryAction {
    /// Send this leg.
    Send(LegRole),
    /// Every leg filled in full.
    Open,
    /// A leg fell short: close what filled, in exit order.
    Abandon(Vec<(LegRole, i64)>),
}

/// `advance_entry(sequence, quantity, outcomes)`.
///
/// # Errors
/// More outcomes than legs, or an outcome outside `0..=quantity`.
pub fn advance_entry(
    sequence: &[LegRole],
    quantity: i64,
    outcomes: &[i64],
) -> Result<EntryAction, CoreViolation> {
    if outcomes.len() > sequence.len() {
        return Err(value_error("more outcomes than legs"));
    }
    for (role, filled) in sequence.iter().zip(outcomes) {
        if !(0..=quantity).contains(filled) {
            return Err(value_error(format!(
                "{} filled {filled} of {quantity}",
                role_str(*role)
            )));
        }
    }
    if outcomes.iter().any(|f| *f < quantity) {
        let filled: BTreeMap<LegRole, i64> = sequence
            .iter()
            .zip(outcomes)
            .filter(|(_, f)| **f > 0)
            .map(|(r, f)| (*r, *f))
            .collect();
        let close = exit_sequence(filled.keys().copied())
            .into_iter()
            .map(|r| (r, filled.get(&r).copied().unwrap_or(0)))
            .collect();
        return Ok(EntryAction::Abandon(close));
    }
    if outcomes.len() == sequence.len() {
        return Ok(EntryAction::Open);
    }
    sequence
        .get(outcomes.len())
        .map(|r| EntryAction::Send(*r))
        .ok_or_else(|| value_error("an entry step with no leg"))
}

/// An ordered `dict[LegRole, int]` (Python's insertion order matters for the views).
pub type Position = Vec<(LegRole, i64)>;

fn pos_get(p: &Position, role: LegRole) -> i64 {
    p.iter().find(|(r, _)| *r == role).map_or(0, |(_, q)| *q)
}

fn pos_add(p: &mut Position, role: LegRole, delta: i64) {
    if let Some(slot) = p.iter_mut().find(|(r, _)| *r == role) {
        slot.1 += delta;
    } else {
        p.push((role, delta));
    }
}

fn pos_map(p: &Position) -> BTreeMap<LegRole, i64> {
    p.iter().copied().collect()
}

/// `next_exit_leg(open_quantity)`: the first leg in exit order still open; a long is never
/// offered while its short is open.
#[must_use]
pub fn next_exit_leg(open: &Position) -> Option<LegRole> {
    for role in exit_sequence(open.iter().map(|(r, _)| *r)) {
        if pos_get(open, role) <= 0 {
            continue;
        }
        let protected_open = [LegRole::ShortCall, LegRole::ShortPut]
            .iter()
            .any(|s| s.protector() == Some(role) && pos_get(open, *s) > 0);
        if protected_open {
            continue;
        }
        return Some(role);
    }
    None
}

/// One LIMIT attempt (`execution.Attempt`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Attempt {
    /// 1, 2 or 3.
    pub number: i64,
    /// The side.
    pub side: Side,
    /// The limit.
    pub price: Decimal,
    /// The third, risk-reducing close at the touch.
    pub marketable: bool,
}

/// `next_attempt(...)` (§8.2).
///
/// # Errors
/// `number < 1`.
pub fn next_attempt(
    number: i64,
    side: Side,
    bid: Decimal,
    ask: Decimal,
    tick: Decimal,
    closing_reduces_risk: bool,
    config: &ExecutionConfig,
) -> Result<Option<Attempt>, CoreViolation> {
    if number < 1 {
        return Err(value_error("attempts count from 1"));
    }
    let direction = if side == Side::Buy {
        Decimal::ONE
    } else {
        Decimal::NEGATIVE_ONE
    };
    let touch = if side == Side::Buy { ask } else { bid };
    // `max(a, b)` returns `a` unless `b > a`.
    let pymax = |a: Decimal, b: Decimal| if b > a { b } else { a };
    if number <= 2 {
        let ticks = Decimal::from(config.limit_improve_ticks + (number - 1));
        let price = touch + direction * tick * ticks;
        return Ok(Some(Attempt {
            number,
            side,
            price: pymax(price, tick),
            marketable: false,
        }));
    }
    if number == 3 && closing_reduces_risk && config.exit_final_marketable {
        return Ok(Some(Attempt {
            number,
            side,
            price: pymax(touch, tick),
            marketable: true,
        }));
    }
    Ok(None)
}

// --- the executor (`executor.py`) ---------------------------------------------------------------

/// `executor.MAX_ATTEMPTS`.
pub const MAX_ATTEMPTS: i64 = 3;

/// `executor.Outcome`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    /// Every leg filled.
    Open,
    /// A leg fell short; what filled was closed.
    AbandonedEntry,
    /// Everything closed.
    Flat,
    /// An exit left a leg open; the next pass retries.
    PartialExit,
}

impl Outcome {
    /// `.value`.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Open => "OPEN",
            Self::AbandonedEntry => "ABANDONED_ENTRY",
            Self::Flat => "FLAT",
            Self::PartialExit => "PARTIAL_EXIT",
        }
    }
}

/// A leg's quote at the moment of an attempt.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Book {
    /// Bid.
    pub bid: Decimal,
    /// Ask.
    pub ask: Decimal,
}

/// What one attempt filled.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Fill {
    /// Units.
    pub quantity: i64,
    /// Average price.
    pub avg_price: Option<Decimal>,
}

/// One step of the procedure.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Event {
    /// The leg.
    pub role: LegRole,
    /// The side.
    pub side: Side,
    /// Attempt number.
    pub attempt: i64,
    /// Limit.
    pub price: Decimal,
    /// Requested.
    pub requested: i64,
    /// Filled.
    pub filled: i64,
    /// A close.
    pub closing: bool,
    /// Marketable.
    pub marketable: bool,
}

/// `executor.Result`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExecResult {
    /// The outcome.
    pub outcome: Outcome,
    /// Signed open quantity per role, in insertion order.
    pub position: Position,
    /// Fills per role, in insertion order.
    pub fills: Vec<(LegRole, Vec<Fill>)>,
    /// Every step.
    pub events: Vec<Event>,
}

impl ExecResult {
    fn new(outcome: Outcome, position: Position) -> Self {
        Self {
            outcome,
            position,
            fills: Vec::new(),
            events: Vec::new(),
        }
    }

    /// `result.avg_price(role)`.
    #[must_use]
    pub fn avg_price(&self, role: LegRole) -> Option<Decimal> {
        let fills = self.fills.iter().find(|(r, _)| *r == role)?;
        let done: Vec<&Fill> = fills
            .1
            .iter()
            .filter(|f| f.quantity != 0 && f.avg_price.is_some())
            .collect();
        let total: i64 = done.iter().map(|f| f.quantity).sum();
        if total == 0 {
            return None;
        }
        let value = done.iter().fold(Decimal::ZERO, |acc, f| {
            acc + f.avg_price.unwrap_or(Decimal::ZERO) * Decimal::from(f.quantity)
        });
        py::div(value, Decimal::from(total)).ok()
    }

    fn push_fill(&mut self, role: LegRole, fill: Fill) {
        if let Some(slot) = self.fills.iter_mut().find(|(r, _)| *r == role) {
            slot.1.push(fill);
        } else {
            self.fills.push((role, vec![fill]));
        }
    }
}

/// Where orders go: `quote` reads the book; `send` places one attempt and waits it out.
#[async_trait::async_trait]
pub trait Venue: Send {
    /// The leg's book now.
    ///
    /// # Errors
    /// No quote (the desk answers `Refused`).
    async fn quote(&mut self, role: LegRole) -> Result<Book, VenueError>;

    /// Send one attempt; what filled.
    ///
    /// # Errors
    /// A refusal that aborts the procedure (no quote, a gateway exception).
    async fn send(
        &mut self,
        role: LegRole,
        attempt: &Attempt,
        quantity: i64,
        closing: bool,
    ) -> Result<Fill, VenueError>;
}

/// Why the procedure stopped (a venue's exception, or a contract violation).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum VenueError {
    /// The desk's `Refused(status, code, message)`.
    #[error("Refused {status} {code}: {message}")]
    Refused {
        /// HTTP status.
        status: u16,
        /// Code.
        code: String,
        /// Message.
        message: String,
    },
    /// Anything else (an exception that propagates to a 500).
    #[error("{0}")]
    Other(String),
}

impl From<CoreViolation> for VenueError {
    fn from(v: CoreViolation) -> Self {
        Self::Other(v.to_string())
    }
}

fn reduces_risk(role: LegRole, sleeve: Sleeve) -> bool {
    !role.is_long() || matches!(sleeve, Sleeve::O2)
}

#[allow(clippy::too_many_arguments)]
async fn send_leg(
    venue: &mut dyn Venue,
    role: LegRole,
    quantity: i64,
    closing: bool,
    sleeve: Sleeve,
    tick: Decimal,
    config: &ExecutionConfig,
    result: &mut ExecResult,
) -> Result<i64, VenueError> {
    let side = if closing {
        role.exit_side()
    } else {
        role.entry_side()
    };
    let reduces = closing && reduces_risk(role, sleeve);
    let mut remaining = quantity;
    for number in 1..=MAX_ATTEMPTS {
        let book = venue.quote(role).await?;
        let Some(attempt) = next_attempt(number, side, book.bid, book.ask, tick, reduces, config)?
        else {
            break;
        };
        let mut worst = result.position.clone();
        pos_add(
            &mut worst,
            role,
            if closing { -remaining } else { remaining },
        );
        if !never_naked(&pos_map(&worst)) {
            return Err(VenueError::Other(format!(
                "AssertionError: {} would leave the book short more than long",
                role_str(role)
            )));
        }
        let fill = venue.send(role, &attempt, remaining, closing).await?;
        if !(0..=remaining).contains(&fill.quantity) {
            return Err(VenueError::Other(format!(
                "ValueError: {}: filled {} of {remaining}",
                role_str(role),
                fill.quantity
            )));
        }
        pos_add(
            &mut result.position,
            role,
            if closing {
                -fill.quantity
            } else {
                fill.quantity
            },
        );
        result.push_fill(role, fill);
        result.events.push(Event {
            role,
            side,
            attempt: number,
            price: attempt.price,
            requested: remaining,
            filled: fill.quantity,
            closing,
            marketable: attempt.marketable,
        });
        remaining -= fill.quantity;
        if remaining == 0 {
            break;
        }
    }
    Ok(quantity - remaining)
}

/// `run_entry(venue, roles, quantity, sleeve=, tick=, config=)`.
///
/// # Errors
/// The venue's refusal, or a contract violation.
pub async fn run_entry(
    venue: &mut dyn Venue,
    roles: &[LegRole],
    quantity: i64,
    sleeve: Sleeve,
    tick: Decimal,
    config: &ExecutionConfig,
) -> Result<ExecResult, VenueError> {
    let sequence = entry_sequence(roles)?;
    let mut result = ExecResult::new(Outcome::Open, Vec::new());
    let mut outcomes: Vec<i64> = Vec::new();
    loop {
        match advance_entry(&sequence, quantity, &outcomes)? {
            EntryAction::Open => return Ok(result),
            EntryAction::Abandon(_) => {
                let position = result.position.clone();
                let mut closed =
                    run_exit_into(venue, position, sleeve, tick, config, result).await?;
                closed.outcome = Outcome::AbandonedEntry;
                return Ok(closed);
            }
            EntryAction::Send(role) => {
                let filled = send_leg(
                    venue,
                    role,
                    quantity,
                    false,
                    sleeve,
                    tick,
                    config,
                    &mut result,
                )
                .await?;
                outcomes.push(filled);
            }
        }
    }
}

/// `run_exit(venue, position, sleeve=, tick=, config=)`.
///
/// # Errors
/// The venue's refusal, or a contract violation.
pub async fn run_exit(
    venue: &mut dyn Venue,
    position: Position,
    sleeve: Sleeve,
    tick: Decimal,
    config: &ExecutionConfig,
) -> Result<ExecResult, VenueError> {
    let out = ExecResult::new(Outcome::Flat, position.clone());
    run_exit_into(venue, position, sleeve, tick, config, out).await
}

async fn run_exit_into(
    venue: &mut dyn Venue,
    position: Position,
    sleeve: Sleeve,
    tick: Decimal,
    config: &ExecutionConfig,
    mut out: ExecResult,
) -> Result<ExecResult, VenueError> {
    out.position = position;
    let mut tried: Vec<LegRole> = Vec::new();
    loop {
        let Some(role) = next_exit_leg(&out.position) else {
            out.outcome = Outcome::Flat;
            return Ok(out);
        };
        if tried.contains(&role) {
            out.outcome = Outcome::PartialExit;
            return Ok(out);
        }
        tried.push(role);
        let qty = pos_get(&out.position, role);
        send_leg(venue, role, qty, true, sleeve, tick, config, &mut out).await?;
    }
}

// --- the expiry-day slot (§8.6) -----------------------------------------------------------------

/// `slot_after_confirm(sleeve, holder)`: O1/O3 share one slot per expiry day; O2 is outside it.
///
/// # Errors
/// The slot is held by another sleeve (`ValueError("the expiry-day slot is held by O1M")`).
pub fn slot_after_confirm(
    sleeve: Sleeve,
    holder: Option<Sleeve>,
) -> Result<Option<Sleeve>, String> {
    if sleeve == Sleeve::O2 {
        return Ok(holder);
    }
    match holder {
        None => Ok(Some(sleeve)),
        Some(h) if h == sleeve => Ok(Some(sleeve)),
        Some(h) => Err(format!("the expiry-day slot is held by {}", sleeve_str(h))),
    }
}

// --- the risk ledger (`risk.py`, `04` §9) -------------------------------------------------------

/// `risk.PauseReason`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PauseReason {
    /// `DAILY_R`.
    DailyR,
    /// `WEEKLY_R`.
    WeeklyR,
    /// `MONTHLY_R`.
    MonthlyR,
    /// `BOOK_DAILY_INR`.
    BookDailyInr,
    /// `BOOK_MONTHLY_INR`.
    BookMonthlyInr,
}

impl PauseReason {
    /// `.value`.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::DailyR => "DAILY_R",
            Self::WeeklyR => "WEEKLY_R",
            Self::MonthlyR => "MONTHLY_R",
            Self::BookDailyInr => "BOOK_DAILY_INR",
            Self::BookMonthlyInr => "BOOK_MONTHLY_INR",
        }
    }
}

/// `risk.RealisedTrade`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RealisedTrade {
    /// The sleeve.
    pub sleeve: Sleeve,
    /// The day.
    pub trade_date: NaiveDate,
    /// Net ₹.
    pub net_pnl_inr: Decimal,
    /// R.
    pub r_multiple: Decimal,
}

/// `risk.Pause`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Pause {
    /// Inclusive.
    pub paused_until: NaiveDate,
    /// Every reason.
    pub reasons: Vec<PauseReason>,
    /// Close the open positions now.
    pub close_now: bool,
}

/// `RiskConfig` defaults (`04` §9).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RiskConfig {
    /// 2 R.
    pub daily_loss_r: Decimal,
    /// 4 R.
    pub weekly_loss_r: Decimal,
    /// 8 R.
    pub monthly_loss_r: Decimal,
    /// 1.5 %.
    pub book_daily_loss_pct: Decimal,
    /// 5 %.
    pub book_monthly_loss_pct: Decimal,
}

impl Default for RiskConfig {
    fn default() -> Self {
        Self {
            daily_loss_r: Decimal::from(2),
            weekly_loss_r: Decimal::from(4),
            monthly_loss_r: Decimal::from(8),
            book_daily_loss_pct: Decimal::new(15, 1),
            book_monthly_loss_pct: Decimal::from(5),
        }
    }
}

/// `OptionsCeilings` defaults (the two the ledger reads).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BookCeilings {
    /// ₹30,000.
    pub book_daily_loss_inr_max: Decimal,
    /// ₹1,00,000.
    pub book_monthly_loss_inr_max: Decimal,
}

impl Default for BookCeilings {
    fn default() -> Self {
        Self {
            book_daily_loss_inr_max: Decimal::from(30_000),
            book_monthly_loss_inr_max: Decimal::from(100_000),
        }
    }
}

fn week_bounds(day: NaiveDate) -> (NaiveDate, NaiveDate) {
    let monday = day - Duration::days(i64::from(day.weekday().number_from_monday()) - 1);
    (monday, monday + Duration::days(6))
}

/// `last_trading_day_of_week(day, trading_days)`.
#[must_use]
pub fn last_trading_day_of_week(day: NaiveDate, trading_days: &[NaiveDate]) -> NaiveDate {
    let (_, sunday) = week_bounds(day);
    trading_days
        .iter()
        .filter(|d| day <= **d && **d <= sunday)
        .max()
        .copied()
        .unwrap_or(day)
}

/// `month_end(day)`.
#[must_use]
pub fn month_end(day: NaiveDate) -> NaiveDate {
    let (y, m) = if day.month() == 12 {
        (day.year() + 1, 1)
    } else {
        (day.year(), day.month() + 1)
    };
    NaiveDate::from_ymd_opt(y, m, 1).map_or(day, |first| first - Duration::days(1))
}

/// `evaluate_sleeve(...)` (§9.1).
///
/// # Errors
/// Rows of another sleeve, or `r_today_inr <= 0` (`ValueError`).
pub fn evaluate_sleeve(
    sleeve: Sleeve,
    today: NaiveDate,
    history: &[RealisedTrade],
    today_marked_inr: Decimal,
    r_today_inr: Decimal,
    trading_days: &[NaiveDate],
    config: &RiskConfig,
) -> Result<Option<Pause>, String> {
    if history.iter().any(|r| r.sleeve != sleeve) {
        return Err("evaluate_sleeve reads one sleeve's rows; never pool sleeves".to_owned());
    }
    if r_today_inr <= Decimal::ZERO {
        return Err("R must be positive".to_owned());
    }
    let mut found: Vec<(PauseReason, NaiveDate)> = Vec::new();
    let realised_today: Decimal = history
        .iter()
        .filter(|r| r.trade_date == today)
        .map(|r| r.net_pnl_inr)
        .sum();
    if py::div(realised_today + today_marked_inr, r_today_inr)? <= -config.daily_loss_r {
        found.push((PauseReason::DailyR, today));
    }
    let (monday, sunday) = week_bounds(today);
    let week_r: Decimal = history
        .iter()
        .filter(|r| monday <= r.trade_date && r.trade_date <= sunday)
        .map(|r| r.r_multiple)
        .sum();
    if week_r <= -config.weekly_loss_r {
        found.push((
            PauseReason::WeeklyR,
            last_trading_day_of_week(today, trading_days),
        ));
    }
    let month_r: Decimal = history
        .iter()
        .filter(|r| {
            r.trade_date.year() == today.year()
                && r.trade_date.month() == today.month()
                && r.trade_date <= today
        })
        .map(|r| r.r_multiple)
        .sum();
    if month_r <= -config.monthly_loss_r {
        found.push((PauseReason::MonthlyR, month_end(today)));
    }
    Ok(pause_of(&found))
}

fn pause_of(found: &[(PauseReason, NaiveDate)]) -> Option<Pause> {
    let until = found.iter().map(|(_, d)| *d).max()?;
    Some(Pause {
        paused_until: until,
        reasons: found.iter().map(|(r, _)| *r).collect(),
        close_now: found
            .iter()
            .any(|(r, _)| matches!(r, PauseReason::DailyR | PauseReason::BookDailyInr)),
    })
}

/// `risk.BookLimits`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BookLimits {
    /// ₹ a day.
    pub daily_loss_inr: Decimal,
    /// ₹ a month.
    pub monthly_pause_inr: Decimal,
}

/// `book_limits(...)` (§9.3): `None` until some sleeve has capital.
#[must_use]
pub fn book_limits(
    sleeve_capitals_inr: &[Decimal],
    daily_loss_limit_inr: Decimal,
    monthly_pause_inr: Decimal,
    config: &RiskConfig,
    ceilings: &BookCeilings,
) -> Option<BookLimits> {
    let total: Decimal = sleeve_capitals_inr.iter().copied().sum();
    if total <= Decimal::ZERO {
        return None;
    }
    let hundred = Decimal::from(100);
    let daily = if daily_loss_limit_inr > Decimal::ZERO {
        daily_loss_limit_inr
    } else {
        py::div(total * config.book_daily_loss_pct, hundred).unwrap_or(Decimal::ZERO)
    };
    let monthly = if monthly_pause_inr > Decimal::ZERO {
        monthly_pause_inr
    } else {
        py::div(total * config.book_monthly_loss_pct, hundred).unwrap_or(Decimal::ZERO)
    };
    // `min(a, b)` returns `a` unless `b < a`.
    let pymin = |a: Decimal, b: Decimal| if b < a { b } else { a };
    Some(BookLimits {
        daily_loss_inr: pymin(daily, ceilings.book_daily_loss_inr_max),
        monthly_pause_inr: pymin(monthly, ceilings.book_monthly_loss_inr_max),
    })
}

/// `evaluate_book(...)` (§9.3).
#[must_use]
pub fn evaluate_book(
    today: NaiveDate,
    today_net_inr: Decimal,
    month_realised_inr: Decimal,
    limits: Option<&BookLimits>,
) -> Option<Pause> {
    let limits = limits?;
    let mut found = Vec::new();
    if today_net_inr <= -limits.daily_loss_inr {
        found.push((PauseReason::BookDailyInr, today));
    }
    if month_realised_inr <= -limits.monthly_pause_inr {
        found.push((PauseReason::BookMonthlyInr, month_end(today)));
    }
    pause_of(&found)
}
