//! Ports `baskfy_api.fno_read` (FO5): the FO run's reads for the web app, response models in
//! pydantic field order. Everything is end of day — the scan's `trade_date`, the marks' settle,
//! the series' latest session — and nothing reaches Kite. The only arithmetic is descriptive
//! (an IV percentile, a 5-session OI change, days to a listed expiry, counts), done in
//! [`WideDecimal`] so it rounds where Python's `Decimal` rounds.

use std::collections::{BTreeMap, BTreeSet};

use baskfy_core::decimal::{Rounding, WideDecimal};
use baskfy_db::{Db, OrderedJson};
use chrono::{DateTime, Datelike, Duration, NaiveDate, Utc, Weekday};
use rust_decimal::Decimal;
use serde::Serialize;
use sqlx::postgres::PgRow;

use super::shared::{col, db_err};
use crate::problems::{Problem, internal_error};
use crate::wire::ser;

// --- quoted evidence (verbatim from `fno_read`) ----------------------------------------------

/// `TIER_2E_CAVEAT`.
pub(crate) const TIER_2E_CAVEAT: &str = "End-of-day closes, not fills. Slippage is an assumed 3 % of premium per leg per crossing (0.03 % for futures), not measured. `n` legs were modelled because they did not trade.";
/// `RESEARCH_B4_LINE`.
pub(crate) const RESEARCH_B4_LINE: &str = "B4 Index monthly iron condor, entered 15 sessions before expiry (NIFTY + BANKNIFTY): +0.033R a trade (t = 1.36, n = 100, 86 % win, max DD -1.64R)";
/// `RESEARCH_B4_LOSS_CLOSE_LINE`.
pub(crate) const RESEARCH_B4_LOSS_CLOSE_LINE: &str = "With Maulik's loss close at 1.5 x the credit (F1 as built): +0.022R (t = 1.01), win 83 %, worst -0.73R, max drawdown -1.35R. By year: -0.010, -0.004, +0.078, +0.010, +0.047";
/// `RESEARCH_B4_SLIPPAGE`.
pub(crate) const RESEARCH_B4_SLIPPAGE: &str =
    "B4 itself assumed 0.5 % of premium per leg per crossing";
/// `RESEARCH_B4_N`.
pub(crate) const RESEARCH_B4_N: i64 = 100;
/// `RESEARCH_RUN_DATE`.
pub(crate) fn research_run_date() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 9, 23).unwrap_or_default()
}
/// `RESEARCH_SAMPLE`.
pub(crate) const RESEARCH_SAMPLE: &str = "3 Jan 2022 to 22 Sep 2026";
/// `RESEARCH_B4_WHY_PAPER`.
pub(crate) const RESEARCH_B4_WHY_PAPER: [&str; 4] = [
    "N=15 was chosen after seeing N=10 and N=20; the neighbouring choices are negative",
    "t = 1.36 on 100 trades is not significant",
    "the IV ÷ RV > 1.3 slice is 24 trades found in-sample, a hypothesis, not a filter",
    "the years are thinning: ~0 in 2025 and 2026",
];
/// `RESEARCH_F2_LINE`.
pub(crate) const RESEARCH_F2_LINE: &str = "F2, the long-only chandelier, re-costed with its rolls: n = 2,334, +0.017R (t = 0.71), win 39 %. By year: -0.026, +0.455, -0.129, -0.143, -0.141";
/// `RESEARCH_F3_LINE`.
pub(crate) const RESEARCH_F3_LINE: &str = "The daily half of F3's rules, re-tested on the closing files 3 Jan 2022 to 22 Sep 2026, after costs: NIFTY n = 206, -0.021R a trade (t = -2.26, 67 % win); BANKNIFTY n = 166, -0.020R (t = -1.60, 57 % win); near zero before costs. Whatever edge the method has must come from the intraday rules, which only the paper period can test";
/// `F3_UNDERLYINGS`: `04` §8's order, with the sleeve each trades.
pub(crate) const F3_UNDERLYINGS: [(&str, &str); 2] = [("NIFTY", "F3N"), ("BANKNIFTY", "F3B")];
/// `F3_FAMILIES`.
pub(crate) const F3_FAMILIES: [&str; 2] = ["F3N", "F3B"];
/// `F1_UNDERLYINGS`, zipped with `F1_SLEEVES`.
pub(crate) const F1_UNDERLYINGS: [(&str, &str); 2] = [("NIFTY", "F1N"), ("BANKNIFTY", "F1B")];
/// `F1_SLEEVES`, `F2_SLEEVES`, `F3_SLEEVES`.
pub(crate) const F1_SLEEVES: [&str; 2] = ["F1N", "F1B"];
/// `F2_SLEEVES`.
pub(crate) const F2_SLEEVES: [&str; 1] = ["F2"];
/// `F3_SLEEVES`.
pub(crate) const F3_SLEEVES: [&str; 2] = ["F3N", "F3B"];
/// `F2_LISTED_STATES`.
pub(crate) const F2_LISTED_STATES: [&str; 2] = ["CANDIDATE", "REJECTED_SIZE"];
/// `F2_WHOLE_SLEEVE_SYMBOL`.
pub(crate) const F2_WHOLE_SLEEVE_SYMBOL: &str = "*";
/// `FUTURE`.
const FUTURE: &str = "XX";
/// `EMPTY_SCAN_OFF`.
pub(crate) const EMPTY_SCAN_OFF: &str = "scan_off";
/// `EMPTY_NEVER`.
pub(crate) const EMPTY_NEVER: &str = "never_scanned";

const F1_PAPER_CYCLES_PER_UNDERLYING: i64 = 6;
const F1_PAPER_MIN_OPENED: i64 = 4;
const F2_PAPER_SESSIONS: i64 = 60;
const F2_PAPER_MIN_CLOSED: i64 = 15;
const F2_PAPER_MIN_ROLLS: i64 = 3;
const IV_PERCENTILE_MIN_SESSIONS: i64 = 200;
const IV_PERCENTILE_WINDOW_DAYS: i64 = 365;
const OI_CHANGE_SESSIONS: i64 = 5;
const MISSING_LOOKBACK_DAYS: i64 = 90;
const JOURNAL_LIMIT: i64 = 100;
const NSE_EXCHANGE_ID: i16 = 1;

/// `LIVE_SYMBOL.get(symbol, symbol)`.
fn live_symbol(symbol: &str) -> String {
    match symbol {
        "NIFTY" => "NIFTY 50".to_owned(),
        "BANKNIFTY" => "NIFTY BANK".to_owned(),
        other => other.to_owned(),
    }
}

// --- the verdict table -----------------------------------------------------------------------

/// `VerdictRow`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct VerdictRow {
    family: &'static str,
    best_variant: &'static str,
    net_r: &'static str,
    robust: &'static str,
    verdict: &'static str,
    retest_families: &'static [&'static str],
}

const fn v(
    family: &'static str,
    best_variant: &'static str,
    net_r: &'static str,
    robust: &'static str,
    verdict: &'static str,
    retest_families: &'static [&'static str],
) -> VerdictRow {
    VerdictRow {
        family,
        best_variant,
        net_r,
        robust,
        verdict,
        retest_families,
    }
}

/// `VERDICT_TABLE`, in `RESEARCH.md`'s order, markdown stripped.
pub(crate) const VERDICT_TABLE: [VerdictRow; 12] = [
    v(
        "B4 Index monthly iron condor, entered 15 sessions before expiry (NIFTY + BANKNIFTY)",
        "short ±1 sd, wings +0.5 sd, 50 % profit take, exit before expiry",
        "+0.033 (t = 1.36, n = 100, 86 % win, max DD -1.64R)",
        "≥ 0 in 2022, 2023, 2024; ~0 in 2025 and 2026",
        "The one paper candidate (F1), with the caveats in §B4",
        &["B4", "B4_LOSS_CLOSE"],
    ),
    v(
        "A Stock-futures trend (20-day breakout with trend + NIFTY regime)",
        "3 ATR chandelier, long only",
        "+0.033 (t = 1.35, n = 2,334)",
        "No. +0.47 in 2023, negative in 2022, 2024, 2025 and 2026",
        "Not built. One year carries it (built on paper as F2 by Maulik's choice, M.1)",
        &["A0_LONG", "F2"],
    ),
    v(
        "A Stock-futures trend, both sides",
        "as above",
        "-0.023",
        "shorts lose in 4 of 5 years",
        "Not built",
        &["A0"],
    ),
    v(
        "A Open-interest \"buildup\" filters",
        "OI rising with the breakout; 1-day long/short buildup",
        "-0.05 to -0.01",
        "—",
        "OI adds nothing; the control without OI does better",
        &["A1", "A1R", "A3"],
    ),
    v(
        "A Cross-sectional momentum, futures long/short",
        "top/bottom 10 weekly",
        "-0.004",
        "—",
        "Not built",
        &["A2"],
    ),
    v(
        "A Index-futures trend",
        "NIFTY, BANKNIFTY, MIDCPNIFTY",
        "-0.03 to -0.06",
        "—",
        "Not built",
        &["A4"],
    ),
    v(
        "B1 Stock iron condors (top 30 by liquidity)",
        "N = 10, ±1 sd",
        "-0.021 at 1.5 % slip, -0.032 at 3 %",
        "negative every year",
        "Rejected: no gross edge (+0.005 to +0.027R before costs)",
        &["B1"],
    ),
    v(
        "B2 Stock credit spreads on the trend's side",
        "N = 15, 0.5 sd",
        "-0.024 to -0.042",
        "—",
        "Rejected (gross +0.006 to +0.008)",
        &["B2"],
    ),
    v(
        "B3 Exit at E-4 instead of E-1 (stock condors)",
        "—",
        "-0.042 to -0.055",
        "—",
        "Worse. The delivery-margin ramp is avoided at a cost in edge. Moot while B1 is rejected",
        &["B3"],
    ),
    v(
        "C1 Debit spreads on the breakout signal",
        "ATM / +0.5 sd, 10 sessions",
        "-0.236 (t = -20.8)",
        "—",
        "Rejected",
        &["C1"],
    ),
    v(
        "C2 Long ATM option on the breakout signal",
        "10 sessions",
        "-0.105",
        "—",
        "Rejected",
        &["C2"],
    ),
    v(
        "E Cash-futures carry",
        "front month, ≥ 5 days to expiry",
        "median basis 5.6 % a.y. before ~0.45 %/cycle costs; 0.7 % of stock-days clear 7 % net",
        "—",
        "Rejected: below the cost of the capital",
        &["E"],
    ),
];

// --- response models -------------------------------------------------------------------------

/// `FnoScanOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct FnoScanOut {
    pub(crate) sleeve: String,
    #[serde(serialize_with = "ser::date")]
    pub(crate) trade_date: NaiveDate,
    pub(crate) symbol: String,
    pub(crate) state: String,
    pub(crate) reasons: Vec<String>,
    pub(crate) detail: OrderedJson,
    #[serde(serialize_with = "ser::opt_decimal")]
    credit: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_decimal")]
    max_loss_per_lot: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_decimal")]
    cost_share: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_decimal")]
    iv: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_decimal")]
    rv20: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_decimal")]
    iv_rv: Option<Decimal>,
}

/// `FnoMarkOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct FnoMarkOut {
    #[serde(serialize_with = "ser::date")]
    trade_date: NaiveDate,
    #[serde(serialize_with = "ser::decimal")]
    mark_points: Decimal,
    #[serde(serialize_with = "ser::decimal")]
    pnl_inr: Decimal,
    #[serde(serialize_with = "super::shared::ser_opt_wide")]
    pnl_r: Option<WideDecimal>,
    #[serde(serialize_with = "ser::opt_decimal")]
    stop_price: Option<Decimal>,
    detail: OrderedJson,
}

/// `FnoPositionOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct FnoPositionOut {
    id: i64,
    sleeve: String,
    symbol: String,
    structure: String,
    entry_plan_id: String,
    legs: OrderedJson,
    lots: i64,
    lot_size: i64,
    #[serde(serialize_with = "ser::opt_decimal")]
    entry_price: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_decimal")]
    entry_credit: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_decimal")]
    max_loss_inr: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_decimal")]
    profit_take_points: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_decimal")]
    loss_close_points: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_decimal")]
    stop_price: Option<Decimal>,
    gtt_id: Option<String>,
    #[serde(serialize_with = "ser::opt_date")]
    pub(crate) hard_exit_date: Option<NaiveDate>,
    #[serde(serialize_with = "ser::opt_date")]
    next_roll_date: Option<NaiveDate>,
    #[serde(serialize_with = "ser::date")]
    opened_on: NaiveDate,
    sessions_held: Option<i64>,
    simulated: bool,
    mark: Option<FnoMarkOut>,
}

/// `FnoJournalOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct FnoJournalOut {
    position_id: i64,
    sleeve: String,
    symbol: String,
    structure: String,
    #[serde(serialize_with = "ser::date")]
    opened_on: NaiveDate,
    #[serde(serialize_with = "ser::date")]
    closed_on: NaiveDate,
    #[serde(serialize_with = "ser::decimal")]
    net_pnl_inr: Decimal,
    #[serde(serialize_with = "ser::decimal")]
    costs_inr: Decimal,
    #[serde(serialize_with = "ser::decimal")]
    r_multiple: Decimal,
    closed_reason: String,
    sessions_held: i64,
    rolls: i64,
    simulated: bool,
    sizing_mode: String,
}

/// `FnoBacktestOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct FnoBacktestOut {
    pub(crate) family: String,
    tier: String,
    caveat: String,
    #[serde(serialize_with = "ser::date")]
    sample_from: NaiveDate,
    #[serde(serialize_with = "ser::date")]
    sample_to: NaiveDate,
    n: i64,
    #[serde(serialize_with = "ser::opt_decimal")]
    net_r: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_decimal")]
    gross_r: Option<Decimal>,
    per_year: OrderedJson,
    slippage_source: String,
    #[serde(serialize_with = "ser::datetime")]
    run_at: DateTime<Utc>,
}

/// `FnoLevelOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct FnoLevelOut {
    #[serde(serialize_with = "ser::opt_decimal")]
    level: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_date")]
    close_of: Option<NaiveDate>,
    live_symbol: String,
}

/// `FnoUnderlyingOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct FnoUnderlyingOut {
    pub(crate) symbol: String,
    pub(crate) sleeve: String,
    pub(crate) level: FnoLevelOut,
    pub(crate) scan: Option<FnoScanOut>,
    #[serde(serialize_with = "ser::opt_date")]
    pub(crate) next_entry_date: Option<NaiveDate>,
}

/// `FnoTallyOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct FnoTallyOut {
    sleeve: String,
    target: String,
    closed: i64,
    opened_cycles: i64,
    rolls: i64,
    #[serde(serialize_with = "ser::opt_date")]
    first_opened: Option<NaiveDate>,
    violations: Option<i64>,
}

/// `FnoEvidenceOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct FnoEvidenceOut {
    pub(crate) tier: &'static str,
    pub(crate) line: &'static str,
    pub(crate) loss_close_line: &'static str,
    pub(crate) slippage_note: &'static str,
    pub(crate) caveat: &'static str,
    pub(crate) n: i64,
    #[serde(serialize_with = "ser::date")]
    pub(crate) run_date: NaiveDate,
    pub(crate) sample: &'static str,
    pub(crate) why_paper: Vec<&'static str>,
    pub(crate) backtests: Vec<FnoBacktestOut>,
    pub(crate) tally: Vec<FnoTallyOut>,
}

/// `FnoF2Out`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct FnoF2Out {
    pub(crate) research_line: &'static str,
    #[serde(serialize_with = "ser::opt_date")]
    pub(crate) scan_date: Option<NaiveDate>,
    pub(crate) candidates: Vec<FnoScanOut>,
    pub(crate) sleeve_row: Option<FnoScanOut>,
    pub(crate) state_counts: BTreeMap<String, i64>,
    pub(crate) open: Vec<FnoPositionOut>,
    pub(crate) closed: Vec<FnoJournalOut>,
}

/// `FnoF3Out`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct FnoF3Out {
    pub(crate) research_line: &'static str,
    pub(crate) not_tested: Vec<&'static str>,
    #[serde(serialize_with = "ser::opt_date")]
    pub(crate) scan_date: Option<NaiveDate>,
    pub(crate) underlyings: Vec<FnoUnderlyingOut>,
    pub(crate) open: Vec<FnoPositionOut>,
    pub(crate) closed: Vec<FnoJournalOut>,
    pub(crate) backtests: Vec<FnoBacktestOut>,
}

/// `FnoGateOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct FnoGateOut {
    pub(crate) group: &'static str,
    pub(crate) mode: &'static str,
}

/// `FnoOvernightOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct FnoOvernightOut {
    #[serde(serialize_with = "ser::date")]
    pub(crate) today: NaiveDate,
    #[serde(serialize_with = "ser::opt_date")]
    pub(crate) next_session: Option<NaiveDate>,
    #[serde(serialize_with = "ser::opt_date")]
    pub(crate) scan_date: Option<NaiveDate>,
    pub(crate) empty_reason: Option<&'static str>,
    pub(crate) scan_enabled: bool,
    pub(crate) monitor_enabled: bool,
    pub(crate) gates: Vec<FnoGateOut>,
    pub(crate) underlyings: Vec<FnoUnderlyingOut>,
    pub(crate) hard_exit_tomorrow: Vec<FnoPositionOut>,
    pub(crate) open_structures: Vec<FnoPositionOut>,
    pub(crate) journal: Vec<FnoJournalOut>,
    pub(crate) evidence: FnoEvidenceOut,
    pub(crate) f2: FnoF2Out,
    pub(crate) f3: FnoF3Out,
}

/// `FnoInfoRowOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct FnoInfoRowOut {
    symbol: String,
    lot_size: Option<i64>,
    #[serde(serialize_with = "ser::opt_date")]
    near_monthly: Option<NaiveDate>,
    days_to_near_monthly: Option<i64>,
    #[serde(serialize_with = "ser::opt_decimal")]
    fut_settle: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_decimal")]
    basis_ann: Option<Decimal>,
    #[serde(serialize_with = "super::shared::ser_opt_wide")]
    oi_change_5d_pct: Option<WideDecimal>,
    #[serde(serialize_with = "ser::opt_decimal")]
    iv: Option<Decimal>,
    #[serde(serialize_with = "ser::opt_decimal")]
    rv20: Option<Decimal>,
    #[serde(serialize_with = "super::shared::ser_opt_wide")]
    iv_rv: Option<WideDecimal>,
    #[serde(serialize_with = "super::shared::ser_opt_wide")]
    iv_pct_1y: Option<WideDecimal>,
    iv_sessions_1y: i64,
    in_ban: bool,
    #[serde(serialize_with = "ser::opt_decimal")]
    fut_turnover_20d: Option<Decimal>,
}

/// `FnoFamilyOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct FnoFamilyOut {
    verdict: VerdictRow,
    latest_retest: Option<FnoBacktestOut>,
}

/// `FnoSpreadOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct FnoSpreadOut {
    sessions: i64,
    #[serde(serialize_with = "ser::opt_date")]
    first: Option<NaiveDate>,
    #[serde(serialize_with = "ser::opt_date")]
    last: Option<NaiveDate>,
    symbols: i64,
}

/// `FnoIngestOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct FnoIngestOut {
    #[serde(serialize_with = "ser::opt_date")]
    latest_date: Option<NaiveDate>,
    latest_status: Option<String>,
    #[serde(serialize_with = "ser::dates")]
    missing_days: Vec<NaiveDate>,
    #[serde(serialize_with = "ser::opt_date")]
    ban_for_session: Option<NaiveDate>,
    ban_symbols: Vec<String>,
}

/// `FnoInfoOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct FnoInfoOut {
    #[serde(serialize_with = "ser::opt_date")]
    pub(crate) as_of: Option<NaiveDate>,
    pub(crate) rows: Vec<FnoInfoRowOut>,
    pub(crate) families: Vec<FnoFamilyOut>,
    #[serde(serialize_with = "ser::date")]
    pub(crate) research_run_date: NaiveDate,
    pub(crate) research_sample: &'static str,
    pub(crate) spread_sample: FnoSpreadOut,
    pub(crate) ingest: FnoIngestOut,
}

// --- helpers ---------------------------------------------------------------------------------

/// `_q(value, places)`: quantize half-up to `places` decimal places (house rule 8).
fn q(value: &WideDecimal, places: i64) -> Result<WideDecimal, Problem> {
    value
        .quantize(-places, Rounding::HalfUp)
        .map_err(|_| internal_error())
}

fn wide(d: Decimal) -> WideDecimal {
    WideDecimal::from(d)
}

/// `date.fromisoformat(str(raw))` for a truthy JSON value, `None` for a falsy one; anything
/// Python would raise on is the 500 catch-all.
pub(crate) fn iso_date_of(raw: Option<&OrderedJson>) -> Result<Option<NaiveDate>, Problem> {
    let Some(raw) = raw else { return Ok(None) };
    let text = match raw {
        OrderedJson::Null | OrderedJson::Bool(false) => return Ok(None),
        OrderedJson::Int(0) | OrderedJson::UInt(0) => return Ok(None),
        OrderedJson::Float(f) if *f == 0.0 => return Ok(None),
        OrderedJson::Str(s) if s.is_empty() => return Ok(None),
        OrderedJson::Array(a) if a.is_empty() => return Ok(None),
        OrderedJson::Object(o) if o.is_empty() => return Ok(None),
        OrderedJson::Str(s) => s.clone(),
        _ => return Err(internal_error()),
    };
    NaiveDate::parse_from_str(&text, "%Y-%m-%d")
        .ok()
        .filter(|_| text.len() == 10)
        .or_else(|| {
            NaiveDate::parse_from_str(&text, "%Y%m%d")
                .ok()
                .filter(|_| text.len() == 8)
        })
        .map(Some)
        .ok_or_else(internal_error)
}

/// `next_session_after(session, day)`: the first NSE session after `day`; the next weekday when
/// the calendar does not reach that far.
pub(crate) async fn next_session_after(db: &Db, day: NaiveDate) -> Result<NaiveDate, Problem> {
    let found: Option<NaiveDate> = sqlx::query_scalar(
        "SELECT min(date) FROM trading_day WHERE exchange_id = $1 AND date > $2 \
         AND is_trading_day IS true",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(day)
    .fetch_one(db.pool())
    .await
    .map_err(db_err)?;
    if let Some(d) = found {
        return Ok(d);
    }
    let mut candidate = day + Duration::days(1);
    while matches!(candidate.weekday(), Weekday::Sat | Weekday::Sun) {
        candidate += Duration::days(1);
    }
    Ok(candidate)
}

const SCAN_COLUMNS: &str = "sleeve::text AS sleeve, trade_date, symbol, state, reasons::text[] AS reasons, \
     detail, credit, max_loss_per_lot, cost_share, iv, rv20, iv_rv";

fn scan_out(r: &PgRow) -> Result<FnoScanOut, Problem> {
    Ok(FnoScanOut {
        sleeve: col::text(r, "sleeve")?,
        trade_date: col::date(r, "trade_date")?,
        symbol: col::text(r, "symbol")?,
        state: col::text(r, "state")?,
        reasons: col::texts(r, "reasons")?,
        detail: col::object(r, "detail")?,
        credit: col::opt_dec(r, "credit")?,
        max_loss_per_lot: col::opt_dec(r, "max_loss_per_lot")?,
        cost_share: col::opt_dec(r, "cost_share")?,
        iv: col::opt_dec(r, "iv")?,
        rv20: col::opt_dec(r, "rv20")?,
        iv_rv: col::opt_dec(r, "iv_rv")?,
    })
}

fn sleeves_param(sleeves: &[&str]) -> Vec<String> {
    sleeves.iter().map(|s| (*s).to_owned()).collect()
}

/// `latest_scan_date(session, user_id, sleeves)`.
pub(crate) async fn latest_scan_date(
    db: &Db,
    user_id: i64,
    sleeves: &[&str],
) -> Result<Option<NaiveDate>, Problem> {
    sqlx::query_scalar(
        "SELECT max(trade_date) FROM fo_scan WHERE user_id = $1 AND sleeve::text = ANY($2)",
    )
    .bind(user_id)
    .bind(sleeves_param(sleeves))
    .fetch_one(db.pool())
    .await
    .map_err(db_err)
}

/// `scan_rows(session, user_id, day, sleeves)`, ordered by the `fo_sleeve` enum, then symbol.
pub(crate) async fn scan_rows(
    db: &Db,
    user_id: i64,
    day: NaiveDate,
    sleeves: &[&str],
) -> Result<Vec<FnoScanOut>, Problem> {
    let rows = sqlx::query(&format!(
        "SELECT {SCAN_COLUMNS} FROM fo_scan WHERE user_id = $1 AND trade_date = $2 \
         AND sleeve::text = ANY($3) ORDER BY fo_scan.sleeve, fo_scan.symbol"
    ))
    .bind(user_id)
    .bind(day)
    .bind(sleeves_param(sleeves))
    .fetch_all(db.pool())
    .await
    .map_err(db_err)?;
    rows.iter().map(scan_out).collect()
}

fn backtest_out(r: &PgRow) -> Result<FnoBacktestOut, Problem> {
    Ok(FnoBacktestOut {
        family: col::text(r, "family")?,
        tier: col::text(r, "tier")?,
        caveat: col::text(r, "caveat")?,
        sample_from: col::date(r, "sample_from")?,
        sample_to: col::date(r, "sample_to")?,
        n: col::i64(r, "n")?,
        net_r: col::opt_dec(r, "net_r")?,
        gross_r: col::opt_dec(r, "gross_r")?,
        per_year: col::object(r, "per_year")?,
        slippage_source: col::text(r, "slippage_source")?,
        run_at: col::ts(r, "run_at")?,
    })
}

/// `latest_backtests(session, user_id)`: the newest run per family (ties on `run_at` broken by
/// the highest id), in family order.
pub(crate) async fn latest_backtests(
    db: &Db,
    user_id: i64,
) -> Result<Vec<FnoBacktestOut>, Problem> {
    let rows = sqlx::query(
        "SELECT b.family, b.tier, b.caveat, b.sample_from, b.sample_to, b.n::int8 AS n, b.net_r, \
         b.gross_r, b.per_year, b.slippage_source, b.run_at \
         FROM fo_backtest_run b JOIN (SELECT family, max(run_at) AS at FROM fo_backtest_run \
         WHERE user_id = $1 GROUP BY family) newest \
         ON b.family = newest.family AND b.run_at = newest.at \
         WHERE b.user_id = $1 ORDER BY b.family, b.id DESC",
    )
    .bind(user_id)
    .fetch_all(db.pool())
    .await
    .map_err(db_err)?;
    let mut seen = BTreeSet::new();
    let mut out = Vec::new();
    for r in &rows {
        let b = backtest_out(r)?;
        if seen.insert(b.family.clone()) {
            out.push(b);
        }
    }
    Ok(out)
}

/// `underlying_level(session, symbol, day)`: the index level the bhavcopy printed beside the
/// session's futures.
pub(crate) async fn underlying_level(
    db: &Db,
    symbol: &str,
    day: Option<NaiveDate>,
) -> Result<FnoLevelOut, Problem> {
    let live = live_symbol(symbol);
    let Some(day) = day else {
        return Ok(FnoLevelOut {
            level: None,
            close_of: None,
            live_symbol: live,
        });
    };
    let row = sqlx::query(
        "SELECT max(underlying) AS level FROM fo_contract_daily WHERE trade_date = $1 \
         AND symbol = $2 AND option_type = $3",
    )
    .bind(day)
    .bind(symbol)
    .bind(FUTURE)
    .fetch_one(db.pool())
    .await
    .map_err(db_err)?;
    let level = col::opt_dec(&row, "level")?;
    Ok(FnoLevelOut {
        close_of: level.map(|_| day),
        level,
        live_symbol: live,
    })
}

/// `_sessions_between(session, start, end)`: trading sessions in `[start, end]`.
async fn sessions_between(db: &Db, start: NaiveDate, end: NaiveDate) -> Result<i64, Problem> {
    sqlx::query_scalar(
        "SELECT count(*) FROM trading_day WHERE exchange_id = $1 AND date >= $2 AND date <= $3 \
         AND is_trading_day IS true",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(start)
    .bind(end)
    .fetch_one(db.pool())
    .await
    .map_err(db_err)
}

/// `open_positions(session, user_id, today=, sleeves=)`: open structures and futures, each with
/// its latest settle mark.
pub(crate) async fn open_positions(
    db: &Db,
    user_id: i64,
    today: NaiveDate,
    sleeves: &[&str],
) -> Result<Vec<FnoPositionOut>, Problem> {
    let positions = sqlx::query(
        "SELECT id, sleeve::text AS sleeve, symbol, structure, entry_plan_id, legs, \
         lots::int8 AS lots, lot_size::int8 AS lot_size, entry_price, entry_credit, max_loss_inr, \
         profit_take_points, loss_close_points, stop_price, gtt_id, hard_exit_date, \
         next_roll_date, opened_at, simulated FROM fo_position \
         WHERE user_id = $1 AND closed_at IS NULL AND sleeve::text = ANY($2) ORDER BY opened_at",
    )
    .bind(user_id)
    .bind(sleeves_param(sleeves))
    .fetch_all(db.pool())
    .await
    .map_err(db_err)?;
    if positions.is_empty() {
        return Ok(Vec::new());
    }
    let mut ids = Vec::with_capacity(positions.len());
    for p in &positions {
        ids.push(col::i64(p, "id")?);
    }
    let mark_rows = sqlx::query(
        "SELECT m.position_id, m.trade_date, m.mark_points, m.pnl_inr, m.stop_price, m.detail \
         FROM fo_mark m JOIN (SELECT position_id, max(trade_date) AS d FROM fo_mark \
         WHERE position_id = ANY($1) GROUP BY position_id) latest \
         ON m.position_id = latest.position_id AND m.trade_date = latest.d",
    )
    .bind(&ids)
    .fetch_all(db.pool())
    .await
    .map_err(db_err)?;
    let mut marks: BTreeMap<i64, &PgRow> = BTreeMap::new();
    for m in &mark_rows {
        marks.insert(col::i64(m, "position_id")?, m);
    }
    let mut out = Vec::with_capacity(positions.len());
    for p in &positions {
        let id = col::i64(p, "id")?;
        let max_loss_inr = col::opt_dec(p, "max_loss_inr")?;
        // `_risk_of`: one R is the max loss at entry, when the desk recorded a positive one.
        let risk = max_loss_inr.filter(|m| *m > Decimal::ZERO);
        let opened_on = col::ts(p, "opened_at")?.date_naive();
        let mark = match marks.get(&id) {
            None => None,
            Some(m) => {
                let pnl_inr = col::dec(m, "pnl_inr")?;
                let pnl_r = match risk {
                    None => None,
                    Some(r) => {
                        let ratio = wide(pnl_inr).div(&wide(r)).map_err(|_| internal_error())?;
                        Some(q(&ratio, 3)?)
                    }
                };
                Some(FnoMarkOut {
                    trade_date: col::date(m, "trade_date")?,
                    mark_points: col::dec(m, "mark_points")?,
                    pnl_inr,
                    pnl_r,
                    stop_price: col::opt_dec(m, "stop_price")?,
                    detail: col::object(m, "detail")?,
                })
            }
        };
        out.push(FnoPositionOut {
            id,
            sleeve: col::text(p, "sleeve")?,
            symbol: col::text(p, "symbol")?,
            structure: col::text(p, "structure")?,
            entry_plan_id: col::text(p, "entry_plan_id")?,
            legs: col::object(p, "legs")?,
            lots: col::i64(p, "lots")?,
            lot_size: col::i64(p, "lot_size")?,
            entry_price: col::opt_dec(p, "entry_price")?,
            entry_credit: col::opt_dec(p, "entry_credit")?,
            max_loss_inr,
            profit_take_points: col::opt_dec(p, "profit_take_points")?,
            loss_close_points: col::opt_dec(p, "loss_close_points")?,
            stop_price: col::opt_dec(p, "stop_price")?,
            gtt_id: col::opt_text(p, "gtt_id")?,
            hard_exit_date: col::opt_date(p, "hard_exit_date")?,
            next_roll_date: col::opt_date(p, "next_roll_date")?,
            opened_on,
            sessions_held: Some(sessions_between(db, opened_on, today).await?),
            simulated: col::boolean(p, "simulated")?,
            mark,
        });
    }
    Ok(out)
}

/// `journal(session, user_id, sleeves)`: newest first, at most 100.
pub(crate) async fn journal(
    db: &Db,
    user_id: i64,
    sleeves: &[&str],
) -> Result<Vec<FnoJournalOut>, Problem> {
    let rows = sqlx::query(
        "SELECT position_id, sleeve::text AS sleeve, symbol, structure, opened_on, closed_on, \
         net_pnl_inr, costs_inr, r_multiple, closed_reason, sessions_held::int8 AS sessions_held, \
         rolls::int8 AS rolls, simulated, sizing_mode FROM fo_journal \
         WHERE user_id = $1 AND sleeve::text = ANY($2) \
         ORDER BY closed_on DESC, position_id DESC LIMIT $3",
    )
    .bind(user_id)
    .bind(sleeves_param(sleeves))
    .bind(JOURNAL_LIMIT)
    .fetch_all(db.pool())
    .await
    .map_err(db_err)?;
    rows.iter()
        .map(|r| {
            Ok(FnoJournalOut {
                position_id: col::i64(r, "position_id")?,
                sleeve: col::text(r, "sleeve")?,
                symbol: col::text(r, "symbol")?,
                structure: col::text(r, "structure")?,
                opened_on: col::date(r, "opened_on")?,
                closed_on: col::date(r, "closed_on")?,
                net_pnl_inr: col::dec(r, "net_pnl_inr")?,
                costs_inr: col::dec(r, "costs_inr")?,
                r_multiple: col::dec(r, "r_multiple")?,
                closed_reason: col::text(r, "closed_reason")?,
                sessions_held: col::i64(r, "sessions_held")?,
                rolls: col::i64(r, "rolls")?,
                simulated: col::boolean(r, "simulated")?,
                sizing_mode: col::text(r, "sizing_mode")?,
            })
        })
        .collect()
}

/// `paper_tally(session, user_id)`: `04` §9 against the paper journal, counts only.
pub(crate) async fn paper_tally(db: &Db, user_id: i64) -> Result<Vec<FnoTallyOut>, Problem> {
    let mut out = Vec::new();
    for sleeve in ["F1N", "F1B", "F2"] {
        let r = sqlx::query(
            "SELECT count(*) AS closed, \
             count(DISTINCT date_trunc('month', opened_on)) AS cycles, \
             coalesce(sum(rolls), 0)::int8 AS rolls, min(opened_on) AS first \
             FROM fo_journal WHERE user_id = $1 AND sleeve::text = $2 AND simulated IS true",
        )
        .bind(user_id)
        .bind(sleeve)
        .fetch_one(db.pool())
        .await
        .map_err(db_err)?;
        let target = if sleeve == "F2" {
            format!(
                "{F2_PAPER_SESSIONS} consecutive sessions, ≥ {F2_PAPER_MIN_CLOSED} closed, \
                 ≥ {F2_PAPER_MIN_ROLLS} rolls"
            )
        } else {
            format!(
                "{F1_PAPER_CYCLES_PER_UNDERLYING} consecutive monthly cycles, \
                 ≥ {F1_PAPER_MIN_OPENED} opened across F1"
            )
        };
        out.push(FnoTallyOut {
            sleeve: sleeve.to_owned(),
            target,
            closed: col::i64(&r, "closed")?,
            opened_cycles: col::i64(&r, "cycles")?,
            rolls: col::i64(&r, "rolls")?,
            first_opened: col::opt_date(&r, "first")?,
            violations: None,
        });
    }
    Ok(out)
}

// --- the information page --------------------------------------------------------------------

/// `latest_series_date(session)`.
pub(crate) async fn latest_series_date(db: &Db) -> Result<Option<NaiveDate>, Problem> {
    sqlx::query_scalar("SELECT max(trade_date) FROM fo_underlying_daily")
        .fetch_one(db.pool())
        .await
        .map_err(db_err)
}

/// `_near_monthlies(session, after)`: `symbol -> (first monthly strictly after, its lot)`.
async fn near_monthlies(
    db: &Db,
    after: NaiveDate,
) -> Result<BTreeMap<String, (NaiveDate, i64)>, Problem> {
    let rows = sqlx::query(
        "SELECT underlying, expiry_date, lot_size::int8 AS lot_size, detail FROM op_expiry",
    )
    .fetch_all(db.pool())
    .await
    .map_err(db_err)?;
    let mut listed: BTreeMap<String, BTreeMap<NaiveDate, i64>> = BTreeMap::new();
    for r in &rows {
        let withdrawn = matches!(
            col::opt_json(r, "detail")?,
            Some(OrderedJson::Object(ref o)) if o.iter().any(|(k, _)| k == "withdrawn_on")
        );
        if withdrawn {
            continue;
        }
        listed
            .entry(col::text(r, "underlying")?)
            .or_default()
            .insert(col::date(r, "expiry_date")?, col::i64(r, "lot_size")?);
    }
    let mut out = BTreeMap::new();
    for (symbol, lots) in listed {
        let days: Vec<NaiveDate> = lots.keys().copied().collect();
        if let Some(first) = baskfy_core::fno::calendar::monthly_expiries(&days)
            .into_iter()
            .find(|d| *d > after)
            && let Some(lot) = lots.get(&first)
        {
            out.insert(symbol, (first, *lot));
        }
    }
    Ok(out)
}

/// `_future_lots(session, day)`: the bhavcopy's lot per symbol.
async fn future_lots(db: &Db, day: NaiveDate) -> Result<BTreeMap<String, i64>, Problem> {
    let rows = sqlx::query(
        "SELECT symbol, max(lot_size)::int8 AS lot FROM fo_contract_daily \
         WHERE trade_date = $1 AND option_type = $2 GROUP BY symbol",
    )
    .bind(day)
    .bind(FUTURE)
    .fetch_all(db.pool())
    .await
    .map_err(db_err)?;
    let mut out = BTreeMap::new();
    for r in &rows {
        if let Some(lot) = col::opt_i64(r, "lot")? {
            out.insert(col::text(r, "symbol")?, lot);
        }
    }
    Ok(out)
}

/// `_iv_percentiles(session, day)`: `symbol -> (sessions with an iv_atm in the year, of which
/// <= the day's iv)`.
async fn iv_percentiles(db: &Db, day: NaiveDate) -> Result<BTreeMap<String, (i64, i64)>, Problem> {
    let rows = sqlx::query(
        "SELECT today.symbol, count(*) AS n, count(*) FILTER (WHERE hist.iv_atm <= today.iv) AS le \
         FROM (SELECT symbol, iv_atm AS iv FROM fo_underlying_daily \
               WHERE trade_date = $1 AND iv_atm IS NOT NULL) today \
         JOIN fo_underlying_daily hist ON hist.symbol = today.symbol \
         WHERE hist.trade_date > $2 AND hist.trade_date <= $1 AND hist.iv_atm IS NOT NULL \
         GROUP BY today.symbol",
    )
    .bind(day)
    .bind(day - Duration::days(IV_PERCENTILE_WINDOW_DAYS))
    .fetch_all(db.pool())
    .await
    .map_err(db_err)?;
    let mut out = BTreeMap::new();
    for r in &rows {
        out.insert(
            col::text(r, "symbol")?,
            (col::i64(r, "n")?, col::i64(r, "le")?),
        );
    }
    Ok(out)
}

/// `_oi_five_back(session, day)`: `symbol -> oi_total` five stored sessions before `day`.
async fn oi_five_back(db: &Db, day: NaiveDate) -> Result<BTreeMap<String, i64>, Problem> {
    let rows = sqlx::query(
        "SELECT symbol, oi FROM (SELECT symbol, oi_total AS oi, row_number() OVER \
         (PARTITION BY symbol ORDER BY trade_date DESC) AS rn FROM fo_underlying_daily \
         WHERE trade_date <= $1 AND trade_date > $2) ranked WHERE rn = $3",
    )
    .bind(day)
    .bind(day - Duration::days(30))
    .bind(OI_CHANGE_SESSIONS + 1)
    .fetch_all(db.pool())
    .await
    .map_err(db_err)?;
    let mut out = BTreeMap::new();
    for r in &rows {
        if let Some(oi) = col::opt_i64(r, "oi")? {
            out.insert(col::text(r, "symbol")?, oi);
        }
    }
    Ok(out)
}

/// `info_rows(session, day)`: every F&O underlying on `day`, by futures turnover (descending),
/// then symbol.
pub(crate) async fn info_rows(db: &Db, day: NaiveDate) -> Result<Vec<FnoInfoRowOut>, Problem> {
    let series = sqlx::query(
        "SELECT symbol, level_c, basis_ann, oi_total, iv_atm, rv20, in_ban, fut_turnover_20d \
         FROM fo_underlying_daily WHERE trade_date = $1",
    )
    .bind(day)
    .fetch_all(db.pool())
    .await
    .map_err(db_err)?;
    let monthlies = near_monthlies(db, day).await?;
    let lots = future_lots(db, day).await?;
    let ivp = iv_percentiles(db, day).await?;
    let oi_back = oi_five_back(db, day).await?;
    let hundred = WideDecimal::from(100_i64);
    let mut out = Vec::with_capacity(series.len());
    for r in &series {
        let symbol = col::text(r, "symbol")?;
        let near = monthlies.get(&symbol).copied();
        let (n_iv, le) = ivp.get(&symbol).copied().unwrap_or((0, 0));
        let oi_total = col::opt_i64(r, "oi_total")?;
        let back = oi_back.get(&symbol).copied();
        let oi_change = match (oi_total, back) {
            (Some(now), Some(b)) if b != 0 => {
                let delta = WideDecimal::from(now - b).mul(&hundred);
                Some(q(
                    &delta
                        .div(&WideDecimal::from(b))
                        .map_err(|_| internal_error())?,
                    2,
                )?)
            }
            _ => None,
        };
        let iv_atm = col::opt_dec(r, "iv_atm")?;
        let rv20 = col::opt_dec(r, "rv20")?;
        let iv_rv = match (iv_atm, rv20) {
            (Some(iv), Some(rv)) if rv > Decimal::ZERO => Some(q(
                &wide(iv).div(&wide(rv)).map_err(|_| internal_error())?,
                2,
            )?),
            _ => None,
        };
        let iv_pct = if n_iv >= IV_PERCENTILE_MIN_SESSIONS {
            let ratio = WideDecimal::from(le)
                .mul(&hundred)
                .div(&WideDecimal::from(n_iv))
                .map_err(|_| internal_error())?;
            Some(q(&ratio, 1)?)
        } else {
            None
        };
        out.push(FnoInfoRowOut {
            lot_size: near.map(|n| n.1).or_else(|| lots.get(&symbol).copied()),
            near_monthly: near.map(|n| n.0),
            days_to_near_monthly: near.map(|n| (n.0 - day).num_days()),
            fut_settle: col::opt_dec(r, "level_c")?,
            basis_ann: col::opt_dec(r, "basis_ann")?,
            oi_change_5d_pct: oi_change,
            iv: iv_atm,
            rv20,
            iv_rv,
            iv_pct_1y: iv_pct,
            iv_sessions_1y: n_iv,
            in_ban: col::boolean(r, "in_ban")?,
            fut_turnover_20d: col::opt_dec(r, "fut_turnover_20d")?,
            symbol,
        });
    }
    out.sort_by(|a, b| {
        let ta = a.fut_turnover_20d.unwrap_or(Decimal::ZERO);
        let tb = b.fut_turnover_20d.unwrap_or(Decimal::ZERO);
        tb.cmp(&ta).then_with(|| a.symbol.cmp(&b.symbol))
    });
    Ok(out)
}

/// `families(backtests)`: the verdict table with the newest re-test of each row's families.
pub(crate) fn families(backtests: &[FnoBacktestOut]) -> Vec<FnoFamilyOut> {
    VERDICT_TABLE
        .iter()
        .map(|row| {
            let mut latest: Option<&FnoBacktestOut> = None;
            for code in row.retest_families {
                // `{b.family: b for b in backtests}`: the last one of a family wins.
                if let Some(b) = backtests.iter().rev().find(|b| b.family == *code)
                    && latest.is_none_or(|l| b.run_at > l.run_at)
                {
                    latest = Some(b);
                }
            }
            FnoFamilyOut {
                verdict: row.clone(),
                latest_retest: latest.cloned(),
            }
        })
        .collect()
}

/// `spread_summary(session)`.
pub(crate) async fn spread_summary(db: &Db) -> Result<FnoSpreadOut, Problem> {
    let r = sqlx::query(
        "SELECT count(DISTINCT trade_date) AS sessions, min(trade_date) AS first, \
         max(trade_date) AS last, count(DISTINCT symbol) AS symbols FROM fo_spread_sample",
    )
    .fetch_one(db.pool())
    .await
    .map_err(db_err)?;
    Ok(FnoSpreadOut {
        sessions: col::i64(&r, "sessions")?,
        first: col::opt_date(&r, "first")?,
        last: col::opt_date(&r, "last")?,
        symbols: col::i64(&r, "symbols")?,
    })
}

/// `ingest_summary(session, today)`.
pub(crate) async fn ingest_summary(db: &Db, today: NaiveDate) -> Result<FnoIngestOut, Problem> {
    let latest = sqlx::query(
        "SELECT trade_date, status FROM fo_ingest_day ORDER BY trade_date DESC LIMIT 1",
    )
    .fetch_optional(db.pool())
    .await
    .map_err(db_err)?;
    let missing: Vec<NaiveDate> = sqlx::query_scalar(
        "SELECT trade_date FROM fo_ingest_day WHERE status = 'MISSING' AND trade_date >= $1 \
         ORDER BY trade_date DESC",
    )
    .bind(today - Duration::days(MISSING_LOOKBACK_DAYS))
    .fetch_all(db.pool())
    .await
    .map_err(db_err)?;
    let ban = sqlx::query(
        "SELECT ban_for_session, ban_symbols::text[] AS ban_symbols FROM fo_ingest_day \
         WHERE ban_for_session IS NOT NULL ORDER BY trade_date DESC LIMIT 1",
    )
    .fetch_optional(db.pool())
    .await
    .map_err(db_err)?;
    let (ban_for_session, ban_symbols) = match &ban {
        None => (None, Vec::new()),
        Some(b) => {
            let mut symbols = col::texts(b, "ban_symbols")?;
            symbols.sort();
            (col::opt_date(b, "ban_for_session")?, symbols)
        }
    };
    Ok(FnoIngestOut {
        latest_date: latest
            .as_ref()
            .map(|r| col::date(r, "trade_date"))
            .transpose()?,
        latest_status: latest
            .as_ref()
            .map(|r| col::text(r, "status"))
            .transpose()?,
        missing_days: missing,
        ban_for_session,
        ban_symbols,
    })
}

// --- settings (`fno_settings`) ----------------------------------------------------------------

/// `FnoSleeveConfigOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct FnoSleeveConfigOut {
    sleeve: String,
    #[serde(serialize_with = "ser::decimal")]
    capital_inr: Decimal,
    #[serde(serialize_with = "ser::decimal")]
    risk_per_trade_pct: Decimal,
    max_lots: i64,
    max_open_positions: i64,
    paper_enabled: bool,
    #[serde(serialize_with = "ser::opt_date")]
    paused_until: Option<NaiveDate>,
    paused_reason: Option<String>,
}

/// `FnoBookOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct FnoBookOut {
    #[serde(serialize_with = "ser::decimal")]
    monthly_pause_inr: Decimal,
    #[serde(serialize_with = "ser::opt_date")]
    paused_until: Option<NaiveDate>,
    paused_reason: Option<String>,
}

/// `FnoCeilingsOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct FnoCeilingsOut {
    #[serde(serialize_with = "ser::decimal")]
    pub(crate) risk_per_trade_inr_max: Decimal,
    #[serde(serialize_with = "ser::decimal")]
    pub(crate) risk_pct_max: Decimal,
    pub(crate) max_open_positions_max: i64,
    pub(crate) max_per_underlying_max: i64,
    #[serde(serialize_with = "ser::decimal")]
    pub(crate) book_monthly_loss_inr_max: Decimal,
}

/// `FnoAuditOut` / `OptionsAuditOut` (the same six fields).
#[derive(Debug, Clone, Serialize)]
pub(crate) struct AuditOut {
    scope: String,
    key: String,
    old_value: Option<String>,
    new_value: Option<String>,
    #[serde(serialize_with = "ser::datetime")]
    changed_at: DateTime<Utc>,
    changed_by: String,
}

/// `FnoConfigOut`.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct FnoConfigOut {
    pub(crate) seeded: bool,
    pub(crate) book: Option<FnoBookOut>,
    pub(crate) sleeves: Vec<FnoSleeveConfigOut>,
    pub(crate) ceilings: FnoCeilingsOut,
    pub(crate) gates: Vec<FnoGateOut>,
    pub(crate) audit: Vec<AuditOut>,
}

/// `read_book` + `read_sleeve` for every `FoSleeveGroup`: `None` is `FnoConfigNotSeeded`.
pub(crate) async fn read_config(
    db: &Db,
    user_id: i64,
) -> Result<Option<(FnoBookOut, Vec<FnoSleeveConfigOut>)>, Problem> {
    let book = sqlx::query(
        "SELECT monthly_pause_inr, paused_until, paused_reason FROM fo_book_config \
         WHERE user_id = $1",
    )
    .bind(user_id)
    .fetch_optional(db.pool())
    .await
    .map_err(db_err)?;
    let Some(book) = book else { return Ok(None) };
    let book = FnoBookOut {
        monthly_pause_inr: col::dec(&book, "monthly_pause_inr")?,
        paused_until: col::opt_date(&book, "paused_until")?,
        paused_reason: col::opt_text(&book, "paused_reason")?,
    };
    let mut sleeves = Vec::new();
    for group in ["F1", "F2", "F3"] {
        let row = sqlx::query(
            "SELECT sleeve, capital_inr, risk_per_trade_pct, max_lots::int8 AS max_lots, \
             max_open_positions::int8 AS max_open_positions, paper_enabled, paused_until, \
             paused_reason FROM fo_sleeve_config WHERE user_id = $1 AND sleeve = $2",
        )
        .bind(user_id)
        .bind(group)
        .fetch_optional(db.pool())
        .await
        .map_err(db_err)?;
        let Some(s) = row else { return Ok(None) };
        sleeves.push(FnoSleeveConfigOut {
            sleeve: col::text(&s, "sleeve")?,
            capital_inr: col::dec(&s, "capital_inr")?,
            risk_per_trade_pct: col::dec(&s, "risk_per_trade_pct")?,
            max_lots: col::i64(&s, "max_lots")?,
            max_open_positions: col::i64(&s, "max_open_positions")?,
            paper_enabled: col::boolean(&s, "paper_enabled")?,
            paused_until: col::opt_date(&s, "paused_until")?,
            paused_reason: col::opt_text(&s, "paused_reason")?,
        });
    }
    Ok(Some((book, sleeves)))
}

/// `audit_trail(session, user_id=, limit=50)` over `table` (`fo_config_audit` or
/// `op_config_audit`): newest first.
pub(crate) async fn audit_trail(
    db: &Db,
    table: &str,
    user_id: i64,
) -> Result<Vec<AuditOut>, Problem> {
    let table = match table {
        "op_config_audit" => "op_config_audit",
        _ => "fo_config_audit",
    };
    let rows = sqlx::query(&format!(
        "SELECT scope, key, old_value, new_value, changed_at, changed_by FROM {table} \
         WHERE user_id = $1 ORDER BY changed_at DESC, id DESC LIMIT 50"
    ))
    .bind(user_id)
    .fetch_all(db.pool())
    .await
    .map_err(db_err)?;
    rows.iter()
        .map(|r| {
            Ok(AuditOut {
                scope: col::text(r, "scope")?,
                key: col::text(r, "key")?,
                old_value: col::opt_text(r, "old_value")?,
                new_value: col::opt_text(r, "new_value")?,
                changed_at: col::ts(r, "changed_at")?,
                changed_by: col::text(r, "changed_by")?,
            })
        })
        .collect()
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn the_verdict_table_is_researchs_twelve_rows_in_order() {
        assert_eq!(VERDICT_TABLE.len(), 12);
        assert!(VERDICT_TABLE[0].family.starts_with("B4 "));
        assert_eq!(VERDICT_TABLE[0].retest_families, ["B4", "B4_LOSS_CLOSE"]);
        assert_eq!(VERDICT_TABLE[11].retest_families, ["E"]);
    }

    #[test]
    fn a_detail_date_is_read_as_python_reads_it() {
        assert_eq!(iso_date_of(None).unwrap(), None);
        assert_eq!(
            iso_date_of(Some(&OrderedJson::Str(String::new()))).unwrap(),
            None
        );
        assert_eq!(
            iso_date_of(Some(&OrderedJson::Str("2026-10-30".into()))).unwrap(),
            NaiveDate::from_ymd_opt(2026, 10, 30)
        );
        assert!(iso_date_of(Some(&OrderedJson::Str("30 Oct".into()))).is_err());
    }

    #[test]
    fn families_pick_the_newest_retest_of_any_code() {
        let mk = |family: &str, secs: i64| FnoBacktestOut {
            family: family.to_owned(),
            tier: "2E".into(),
            caveat: String::new(),
            sample_from: NaiveDate::from_ymd_opt(2022, 1, 3).unwrap(),
            sample_to: NaiveDate::from_ymd_opt(2026, 9, 22).unwrap(),
            n: 1,
            net_r: None,
            gross_r: None,
            per_year: OrderedJson::Object(Vec::new()),
            slippage_source: String::new(),
            run_at: DateTime::from_timestamp(secs, 0).unwrap(),
        };
        let out = families(&[mk("B4", 10), mk("B4_LOSS_CLOSE", 20), mk("F2", 5)]);
        assert_eq!(
            out[0].latest_retest.as_ref().unwrap().family,
            "B4_LOSS_CLOSE"
        );
        assert_eq!(out[1].latest_retest.as_ref().unwrap().family, "F2");
        assert!(out[2].latest_retest.is_none());
    }
}
