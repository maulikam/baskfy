//! Port of `baskfy_core.fno.ledger` (`decile-blueprint/packages/core/src/baskfy_core/fno/ledger.py`).
//!
//! The FO ledger and `04` §7's pauses, pure (`docs/fno/06` FO10).
//!
//! **Never pooled.** Every figure is keyed by `simulated`: a paper line and a live line are two
//! lines, a paper book and a live book are two books, and a pause is evaluated over one mode's
//! rows only (FO10.1). A live loss never pauses paper and a paper loss never pauses live.
//!
//! The pauses ([`evaluate_pauses`]), `04` §7, exactly:
//!
//! * **F1, per underlying**: the last 3 closed trades on that underlying were each ≤ -0.6R;
//!   it stands until lifted (only trades closed after the lift count); an `ABANDONED_PARTIAL`
//!   neither counts nor breaks the run;
//! * **F2**: the month's closed F2 trades total ≤ -6R → pause to the end of that month;
//! * **Book**: the month's realised FO result ≤ -`monthly_pause_inr` → the book pauses to month
//!   end; `monthly_pause_inr = 0` means the ₹75,000 ceiling applies, above it is held to it.
//!
//! A pause stops new entries only; nothing here closes anything.
//!
//! The three pause predicates belong to `fno.exits` (another lane's port); they are carried
//! here as private copies of `f1_paused`, `f2_month_paused` and `book_paused` (R2.3's rule)
//! until `fno::exits` lands, and `options.journal.max_drawdown` likewise.
//!
//! Goldens: `L1/fno/ledger.<function>.case_NNN.json`.

use std::collections::BTreeMap;

use chrono::{Datelike, NaiveDate, NaiveDateTime};
use rust_decimal::Decimal;
use serde::{Deserialize, Deserializer, Serialize, Serializer, ser::SerializeStruct};

use super::config::py::{add, sub};
use super::config::{
    F1Config, F2Config, FnoCeilings, FnoConfig, FnoError, FnoResult, FoSleeve, FoSleeveGroup,
    group_of, py,
};
use super::journal::CloseKind;
use crate::backtest::helpers::WideDecimal;

/// F1's two sleeves, in the order the pauses are evaluated.
///
/// Ports `baskfy_core.fno.ledger.F1_SLEEVES`.
pub const F1_SLEEVES: [FoSleeve; 2] = [FoSleeve::F1N, FoSleeve::F1B];

/// A `Decimal` input that keeps the sign of a zero (`"-0.00"`), as Python's `Decimal(str)`.
fn signed_decimal<'de, D: Deserializer<'de>>(d: D) -> Result<Decimal, D::Error> {
    let text = String::deserialize(d)?;
    parse_signed(&text).map_err(serde::de::Error::custom)
}

fn signed_decimal_opt<'de, D: Deserializer<'de>>(d: D) -> Result<Option<Decimal>, D::Error> {
    Option::<String>::deserialize(d)?
        .map(|t| parse_signed(&t).map_err(serde::de::Error::custom))
        .transpose()
}

fn parse_signed(text: &str) -> Result<Decimal, String> {
    let mut v: Decimal = text
        .trim()
        .parse()
        .map_err(|e| format!("invalid decimal {text:?}: {e}"))?;
    if v.is_zero() && text.trim_start().starts_with('-') {
        v.set_sign_negative(true);
    }
    Ok(v)
}

/// One `fo_journal` row, as the ledger and the pauses read it.
///
/// Ports `baskfy_core.fno.ledger.ClosedTrade`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ClosedTrade {
    /// The position.
    pub position_id: i64,
    /// The sleeve.
    pub sleeve: FoSleeve,
    /// The underlying.
    pub symbol: String,
    /// Paper (`true`) or live.
    pub simulated: bool,
    /// The close's session.
    pub closed_on: NaiveDate,
    /// The close's moment (naive IST), when known.
    pub closed_at: Option<NaiveDateTime>,
    /// Net P&L, ₹.
    #[serde(deserialize_with = "signed_decimal")]
    pub net_pnl_inr: Decimal,
    /// Net P&L in R.
    #[serde(deserialize_with = "signed_decimal")]
    pub r_multiple: Decimal,
    /// The close's reason (a `CloseKind` value, or the desk's own text).
    pub closed_reason: String,
    /// F2's rolls.
    #[serde(default)]
    pub rolls: i64,
}

/// An open position's latest `fo_mark`: its nightly P&L and its planned max loss.
///
/// Ports `baskfy_core.fno.ledger.OpenMark`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OpenMark {
    /// The position.
    pub position_id: i64,
    /// The sleeve.
    pub sleeve: FoSleeve,
    /// The underlying.
    pub symbol: String,
    /// Paper or live.
    pub simulated: bool,
    /// The mark's session.
    pub trade_date: Option<NaiveDate>,
    /// The marked P&L, ₹.
    #[serde(deserialize_with = "signed_decimal")]
    pub pnl_inr: Decimal,
    /// The planned max loss, ₹.
    #[serde(default, deserialize_with = "signed_decimal_opt")]
    pub r_inr: Option<Decimal>,
}

/// One ledger line's key.
///
/// Ports `baskfy_core.fno.ledger.LineKey`.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub struct LineKey {
    /// The sleeve.
    pub sleeve: FoSleeve,
    /// The underlying.
    pub symbol: String,
    /// Paper or live.
    pub simulated: bool,
}

/// Realised and open figures for one pool.
///
/// Ports `baskfy_core.fno.ledger.LedgerFigures`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LedgerFigures {
    /// Closed trades up to `as_of`.
    pub closed: i64,
    /// Of which with a positive net.
    pub wins: i64,
    /// Realised ₹.
    pub realised_inr: Decimal,
    /// Realised R.
    pub realised_r: Decimal,
    /// Closed this month.
    pub mtd_closed: i64,
    /// Realised ₹ this month.
    pub mtd_realised_inr: Decimal,
    /// Realised R this month.
    pub mtd_realised_r: Decimal,
    /// Open positions.
    pub open_positions: i64,
    /// Their latest marked P&L, ₹.
    pub open_marked_inr: Decimal,
    /// The realised series' drawdown, ₹ (peak from zero).
    pub max_drawdown_inr: Decimal,
    /// The realised series' drawdown, R.
    pub max_drawdown_r: Decimal,
}

impl LedgerFigures {
    /// Realised plus the open positions' marked P&L.
    ///
    /// Ports `baskfy_core.fno.ledger.LedgerFigures.total_inr`.
    ///
    /// # Errors
    /// Decimal overflow.
    pub fn total_inr(&self) -> FnoResult<Decimal> {
        add(self.realised_inr, self.open_marked_inr)
    }

    /// `wins / closed` in Python's default context (kept exact: `1/11` prints at scale 29),
    /// `None` with nothing closed.
    ///
    /// Ports `baskfy_core.fno.ledger.LedgerFigures.win_rate`.
    ///
    /// # Errors
    /// Never in practice (`closed > 0`).
    pub fn win_rate(&self) -> FnoResult<Option<WideDecimal>> {
        if self.closed == 0 {
            return Ok(None);
        }
        WideDecimal::from(self.wins)
            .div(&WideDecimal::from(self.closed))
            .map(Some)
            .map_err(|e| FnoError::Decimal(e.to_string()))
    }
}

impl Serialize for LedgerFigures {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let total = self.total_inr().map_err(serde::ser::Error::custom)?;
        let win_rate = self.win_rate().map_err(serde::ser::Error::custom)?;
        let mut s = serializer.serialize_struct("LedgerFigures", 13)?;
        s.serialize_field("closed", &self.closed)?;
        s.serialize_field("wins", &self.wins)?;
        s.serialize_field("realised_inr", &self.realised_inr)?;
        s.serialize_field("realised_r", &self.realised_r)?;
        s.serialize_field("mtd_closed", &self.mtd_closed)?;
        s.serialize_field("mtd_realised_inr", &self.mtd_realised_inr)?;
        s.serialize_field("mtd_realised_r", &self.mtd_realised_r)?;
        s.serialize_field("open_positions", &self.open_positions)?;
        s.serialize_field("open_marked_inr", &self.open_marked_inr)?;
        s.serialize_field("max_drawdown_inr", &self.max_drawdown_inr)?;
        s.serialize_field("max_drawdown_r", &self.max_drawdown_r)?;
        s.serialize_field("total_inr", &total)?;
        s.serialize_field("win_rate", &win_rate)?;
        s.end()
    }
}

/// The ledger: one line per `(sleeve, underlying, simulated)` and one book per `simulated`.
///
/// Ports `baskfy_core.fno.ledger.Ledger`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Ledger {
    /// The session it describes.
    pub as_of: NaiveDate,
    /// One per `(sleeve, underlying, simulated)` present, ordered by `(simulated, sleeve,
    /// symbol)`.
    pub lines: Vec<(LineKey, LedgerFigures)>,
    /// One per `simulated` present (paper and live, never one number), live first.
    pub books: Vec<(bool, LedgerFigures)>,
}

impl Ledger {
    /// One line's figures.
    ///
    /// Ports `baskfy_core.fno.ledger.Ledger.line`.
    #[must_use]
    pub fn line(&self, sleeve: FoSleeve, symbol: &str, simulated: bool) -> Option<&LedgerFigures> {
        self.lines
            .iter()
            .find(|(k, _)| k.sleeve == sleeve && k.symbol == symbol && k.simulated == simulated)
            .map(|(_, f)| f)
    }

    /// One book's figures.
    ///
    /// Ports `baskfy_core.fno.ledger.Ledger.book`.
    #[must_use]
    pub fn book(&self, simulated: bool) -> Option<&LedgerFigures> {
        self.books
            .iter()
            .find(|(m, _)| *m == simulated)
            .map(|(_, f)| f)
    }
}

/// The first of `day`'s month.
///
/// Ports `baskfy_core.fno.ledger.month_start`.
#[must_use]
pub fn month_start(day: NaiveDate) -> NaiveDate {
    day.with_day(1).unwrap_or(day)
}

/// The last of `day`'s month.
///
/// Ports `baskfy_core.fno.ledger.month_end`.
#[must_use]
pub fn month_end(day: NaiveDate) -> NaiveDate {
    let (y, m) = if day.month() == 12 {
        (day.year() + 1, 1)
    } else {
        (day.year(), day.month() + 1)
    };
    NaiveDate::from_ymd_opt(y, m, 1)
        .and_then(|first| first.pred_opt())
        .unwrap_or(day)
}

fn in_month(day: NaiveDate, as_of: NaiveDate) -> bool {
    month_start(as_of) <= day && day <= as_of
}

/// Sorted by `(closed_on, closed_at.timestamp() if closed_at else 0.0, position_id)`, stably.
/// A naive `closed_at`'s timestamp is monotonic in the datetime, and every real one is after
/// the epoch, so a missing `closed_at` (0.0) sorts first within its day.
fn ordered<'a>(trades: impl IntoIterator<Item = &'a ClosedTrade>) -> Vec<&'a ClosedTrade> {
    let mut out: Vec<&ClosedTrade> = trades.into_iter().collect();
    out.sort_by(|a, b| {
        (a.closed_on, a.closed_at, a.position_id).cmp(&(b.closed_on, b.closed_at, b.position_id))
    });
    out
}

fn sum(values: impl IntoIterator<Item = Decimal>) -> FnoResult<Decimal> {
    values.into_iter().try_fold(Decimal::ZERO, add)
}

/// The largest peak-to-trough fall of the running sum, as a positive number (0 if none); the
/// peak starts at 0.
///
/// Ports `baskfy_core.options.journal.max_drawdown` (no Rust home yet; R2.3).
fn max_drawdown(values: impl IntoIterator<Item = Decimal>) -> FnoResult<Decimal> {
    let (mut peak, mut running, mut worst) = (Decimal::ZERO, Decimal::ZERO, Decimal::ZERO);
    for value in values {
        running = add(running, value)?;
        peak = py::max(peak, running);
        worst = py::max(worst, sub(peak, running)?);
    }
    Ok(worst)
}

fn count(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

/// One pool's figures.
///
/// Ports `baskfy_core.fno.ledger.figures`.
///
/// # Errors
/// `PooledRows` when the rows hold both modes.
pub fn figures(
    trades: &[ClosedTrade],
    marks: &[OpenMark],
    as_of: NaiveDate,
) -> FnoResult<LedgerFigures> {
    let mut modes: Vec<bool> = trades
        .iter()
        .map(|t| t.simulated)
        .chain(marks.iter().map(|m| m.simulated))
        .collect();
    modes.sort_unstable();
    modes.dedup();
    if modes.len() > 1 {
        return Err(FnoError::PooledRows(
            "a ledger figure is one mode's: paper and live are never pooled".to_owned(),
        ));
    }
    let done: Vec<&ClosedTrade> = ordered(trades)
        .into_iter()
        .filter(|t| t.closed_on <= as_of)
        .collect();
    let month: Vec<&ClosedTrade> = done
        .iter()
        .copied()
        .filter(|t| in_month(t.closed_on, as_of))
        .collect();
    Ok(LedgerFigures {
        closed: count(done.len()),
        wins: count(
            done.iter()
                .filter(|t| t.net_pnl_inr > Decimal::ZERO)
                .count(),
        ),
        realised_inr: sum(done.iter().map(|t| t.net_pnl_inr))?,
        realised_r: sum(done.iter().map(|t| t.r_multiple))?,
        mtd_closed: count(month.len()),
        mtd_realised_inr: sum(month.iter().map(|t| t.net_pnl_inr))?,
        mtd_realised_r: sum(month.iter().map(|t| t.r_multiple))?,
        open_positions: count(marks.len()),
        open_marked_inr: sum(marks.iter().map(|m| m.pnl_inr))?,
        max_drawdown_inr: max_drawdown(done.iter().map(|t| t.net_pnl_inr))?,
        max_drawdown_r: max_drawdown(done.iter().map(|t| t.r_multiple))?,
    })
}

/// Per `(sleeve, underlying, simulated)` and per book per `simulated`.
///
/// Ports `baskfy_core.fno.ledger.build_ledger`.
///
/// # Errors
/// Decimal overflow.
pub fn build_ledger(
    trades: &[ClosedTrade],
    marks: &[OpenMark],
    as_of: NaiveDate,
) -> FnoResult<Ledger> {
    type Pool = (Vec<ClosedTrade>, Vec<OpenMark>);
    // Keyed by Python's sort key: (simulated, sleeve.value, symbol).
    let mut by_line: BTreeMap<(bool, &'static str, String), (LineKey, Pool)> = BTreeMap::new();
    let mut by_book: BTreeMap<bool, Pool> = BTreeMap::new();
    for t in trades {
        let key = LineKey {
            sleeve: t.sleeve,
            symbol: t.symbol.clone(),
            simulated: t.simulated,
        };
        by_line
            .entry((t.simulated, t.sleeve.value(), t.symbol.clone()))
            .or_insert_with(|| (key, (Vec::new(), Vec::new())))
            .1
            .0
            .push(t.clone());
        by_book.entry(t.simulated).or_default().0.push(t.clone());
    }
    for m in marks {
        let key = LineKey {
            sleeve: m.sleeve,
            symbol: m.symbol.clone(),
            simulated: m.simulated,
        };
        by_line
            .entry((m.simulated, m.sleeve.value(), m.symbol.clone()))
            .or_insert_with(|| (key, (Vec::new(), Vec::new())))
            .1
            .1
            .push(m.clone());
        by_book.entry(m.simulated).or_default().1.push(m.clone());
    }
    let mut lines = Vec::with_capacity(by_line.len());
    for (_, (key, (t, m))) in by_line {
        let figs = figures(&t, &m, as_of)?;
        lines.push((key, figs));
    }
    let mut books = Vec::with_capacity(by_book.len());
    for (mode, (t, m)) in by_book {
        books.push((mode, figures(&t, &m, as_of)?));
    }
    Ok(Ledger {
        as_of,
        lines,
        books,
    })
}

// --- the pauses ---------------------------------------------------------------------------

/// `fo_sleeve_config` / `fo_book_config.paused_reason` codes (≤ 32 characters).
///
/// Ports `baskfy_core.fno.ledger.PauseCode`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum PauseCode {
    /// F1's loss run.
    F1LossRun,
    /// F2's month in R.
    F2MonthR,
    /// The book's month in ₹.
    BookMonthInr,
}

impl PauseCode {
    /// The enum's value.
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::F1LossRun => "F1_LOSS_RUN",
            Self::F2MonthR => "F2_MONTH_R",
            Self::BookMonthInr => "BOOK_MONTH_INR",
        }
    }
}

/// One pause. `scope` is `F1N`/`F1B` (per underlying), `F2` or `BOOK`. `paused_until` is
/// inclusive; `None` stands until lifted (F1, FO10.2).
///
/// Ports `baskfy_core.fno.ledger.FoPause`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FoPause {
    /// `F1N`, `F1B`, `F2` or `BOOK`.
    pub scope: String,
    /// The code.
    pub code: PauseCode,
    /// The mode it binds.
    pub simulated: bool,
    /// Inclusive end; `None` until lifted.
    pub paused_until: Option<NaiveDate>,
    /// The pause in words.
    pub message: String,
}

impl FoPause {
    /// The `paused_reason` column's value: the mode, then the code (FO10.1).
    ///
    /// Ports `baskfy_core.fno.ledger.FoPause.reason`.
    #[must_use]
    pub fn reason(&self) -> String {
        format!("{}:{}", mode_tag(self.simulated), self.code.value())
    }

    /// Whether this pause stops a new `sleeve` entry.
    ///
    /// Ports `baskfy_core.fno.ledger.FoPause.covers`.
    #[must_use]
    pub fn covers(&self, sleeve: FoSleeve) -> bool {
        if self.scope == "BOOK" {
            return true;
        }
        if self.scope == FoSleeveGroup::F2.value() {
            return sleeve == FoSleeve::F2;
        }
        self.scope == sleeve.value()
    }
}

impl Serialize for FoPause {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut s = serializer.serialize_struct("FoPause", 6)?;
        s.serialize_field("scope", &self.scope)?;
        s.serialize_field("code", &self.code)?;
        s.serialize_field("simulated", &self.simulated)?;
        s.serialize_field("paused_until", &self.paused_until)?;
        s.serialize_field("message", &self.message)?;
        s.serialize_field("reason", &self.reason())?;
        s.end()
    }
}

/// `"PAPER"` or `"LIVE"`.
///
/// Ports `baskfy_core.fno.ledger.mode_tag`.
#[must_use]
pub fn mode_tag(simulated: bool) -> &'static str {
    if simulated { "PAPER" } else { "LIVE" }
}

/// `fo_book_config.monthly_pause_inr` as the rule applies it (FO10.5): 0 — the seed, "not
/// set" — is the ceiling; anything above the ceiling is held to it.
///
/// Ports `baskfy_core.fno.ledger.book_pause_limit`.
#[must_use]
pub fn book_pause_limit(monthly_pause_inr: Decimal, ceilings: &FnoCeilings) -> Decimal {
    let ceiling = ceilings.book_monthly_loss_inr_max;
    if monthly_pause_inr <= Decimal::ZERO {
        return ceiling;
    }
    py::min(monthly_pause_inr, ceiling)
}

/// `fno.exits.f1_paused`: the last `pause_consecutive` closed F1 trades were each ≤
/// `pause_loss_r`.
fn f1_paused(closed_r: &[Decimal], f1: &F1Config) -> bool {
    let n = usize::try_from(f1.pause_consecutive).unwrap_or(0);
    let tail = &closed_r[closed_r.len().saturating_sub(n)..];
    tail.len() == n && tail.iter().all(|r| *r <= f1.pause_loss_r)
}

/// `fno.exits.f2_month_paused`: the month's closed F2 trades sum to ≤ `month_pause_r`.
fn f2_month_paused(month_closed_r: &[Decimal], f2: &F2Config) -> FnoResult<bool> {
    Ok(sum(month_closed_r.iter().copied())? <= f2.month_pause_r)
}

/// `fno.exits.book_paused`: the month's realised FO loss has reached `monthly_pause_inr`.
fn book_paused(month_realised_inr: Decimal, monthly_pause_inr: Decimal) -> bool {
    month_realised_inr <= -monthly_pause_inr
}

/// `04` §7 over one mode's closed trades up to `as_of`. Rows of the other mode are not read at
/// all. `lifted_after` maps an F1 sleeve to the moment its pause was last lifted.
///
/// Ports `baskfy_core.fno.ledger.evaluate_pauses`.
///
/// # Errors
/// Decimal overflow.
pub fn evaluate_pauses(
    trades: &[ClosedTrade],
    simulated: bool,
    as_of: NaiveDate,
    monthly_pause_inr: Decimal,
    lifted_after: Option<&BTreeMap<FoSleeve, NaiveDateTime>>,
    config: &FnoConfig,
    ceilings: &FnoCeilings,
) -> FnoResult<Vec<FoPause>> {
    let mine: Vec<&ClosedTrade> = ordered(trades)
        .into_iter()
        .filter(|t| t.simulated == simulated && t.closed_on <= as_of)
        .collect();
    let mut found = Vec::new();
    let f1 = &config.f1;
    for sleeve in F1_SLEEVES {
        let lifted = lifted_after.and_then(|m| m.get(&sleeve)).copied();
        let history: Vec<Decimal> = mine
            .iter()
            .filter(|t| {
                t.sleeve == sleeve
                    && t.closed_reason != CloseKind::AbandonedPartial.value()
                    && lifted.is_none_or(|l| t.closed_at.is_some_and(|at| at > l))
            })
            .map(|t| t.r_multiple)
            .collect();
        if f1_paused(&history, f1) {
            let n = usize::try_from(f1.pause_consecutive).unwrap_or(0);
            let tail = history[history.len().saturating_sub(n)..]
                .iter()
                .map(|r| format!("{}R", py::str(*r)))
                .collect::<Vec<_>>()
                .join(", ");
            found.push(FoPause {
                scope: sleeve.value().to_owned(),
                code: PauseCode::F1LossRun,
                simulated,
                paused_until: None,
                message: format!(
                    "the last {} closed {} trades were each ≤ {}R ({tail}); new entries pause \
                     until lifted (04 §7)",
                    f1.pause_consecutive,
                    sleeve.value(),
                    py::str(f1.pause_loss_r)
                ),
            });
        }
    }
    let month: Vec<&ClosedTrade> = mine
        .iter()
        .copied()
        .filter(|t| in_month(t.closed_on, as_of))
        .collect();
    let f2_month: Vec<Decimal> = month
        .iter()
        .filter(|t| group_of(t.sleeve) == FoSleeveGroup::F2)
        .map(|t| t.r_multiple)
        .collect();
    if !f2_month.is_empty() && f2_month_paused(&f2_month, &config.f2)? {
        found.push(FoPause {
            scope: FoSleeveGroup::F2.value().to_owned(),
            code: PauseCode::F2MonthR,
            simulated,
            paused_until: Some(month_end(as_of)),
            message: format!(
                "the month's closed F2 trades total {}R ≤ {}R; new entries pause for the rest \
                 of the month (04 §7)",
                py::str(sum(f2_month.iter().copied())?),
                py::str(config.f2.month_pause_r)
            ),
        });
    }
    let limit = book_pause_limit(monthly_pause_inr, ceilings);
    let realised = sum(month.iter().map(|t| t.net_pnl_inr))?;
    if !month.is_empty() && book_paused(realised, limit) {
        found.push(FoPause {
            scope: "BOOK".to_owned(),
            code: PauseCode::BookMonthInr,
            simulated,
            paused_until: Some(month_end(as_of)),
            message: format!(
                "the month's realised FO result ₹{} has reached the ₹{} book pause; new \
                 entries pause for the rest of the month (04 §7)",
                py::str(realised),
                py::str(limit)
            ),
        });
    }
    Ok(found)
}

/// The pauses that stop a new `sleeve` entry.
///
/// Ports `baskfy_core.fno.ledger.pauses_for`.
#[must_use]
pub fn pauses_for(pauses: &[FoPause], sleeve: FoSleeve) -> Vec<FoPause> {
    pauses
        .iter()
        .filter(|p| p.covers(sleeve))
        .cloned()
        .collect()
}

/// Whether a stored `paused_until`/`paused_reason` stops an entry of this mode on `day`. A
/// ledger-written reason carries its mode (`PAPER:…` / `LIVE:…`) and binds that mode only; a
/// reason without one is a hand-set pause and binds both (FO10.1).
///
/// Ports `baskfy_core.fno.ledger.column_pause_applies`.
#[must_use]
pub fn column_pause_applies(
    paused_until: Option<NaiveDate>,
    paused_reason: Option<&str>,
    simulated: bool,
    day: NaiveDate,
) -> bool {
    let Some(until) = paused_until else {
        return false;
    };
    if day > until {
        return false;
    }
    let reason = paused_reason.unwrap_or("");
    match reason.split_once(':') {
        Some((tag, _)) if tag == "PAPER" || tag == "LIVE" => tag == mode_tag(simulated),
        _ => true,
    }
}
