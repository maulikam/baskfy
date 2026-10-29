//! Port of `baskfy_core.vbt.config` (`decile-blueprint/packages/core/src/baskfy_core/vbt/config.py`).
//!
//! Every threshold of VBT-1, named once (`docs/vbt/04-business-rules.md`). Nothing downstream
//! compares to a literal: a detector, a task, a router and a page each read a field of one of
//! these structs, so a recalibration is an edit here and a diff in one place.
//!
//! Units, stated once: every `*_pct` is a **percent** (`6.5` means 6.5%); every `*_bars` /
//! `*_sessions` counts trading sessions on the run's own calendar ([`super::calendar`]); every
//! `*_inr` is rupees. Floats stay `f64` where Python declares `float`; the one rupee threshold
//! Python declares `Decimal` (`SizingConfig::min_trade_value_inr`) is a `Decimal` here.
//!
//! Every struct deserialises from the golden encoding of its dataclass (field names are Python's)
//! and a missing key takes Python's default.
//!
//! Goldens: `config.DataConfig.min_samples`, `config.VbtConfig.bars_required`,
//! `config.filter_legend`.

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::swing::stops::StopConfig;

/// Ports `baskfy_core.vbt.config.Gate` — the breadth gate. Two values, not three
/// (DECISIONS-VB VB0.4).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Gate {
    /// Breadth above the threshold: entries may be planned.
    #[serde(rename = "OPEN")]
    Open,
    /// At or below the threshold: no new entries.
    #[serde(rename = "SHUT")]
    Shut,
}

impl Gate {
    /// The enum's value, as Python's `Gate.X.value`.
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::Open => "OPEN",
            Self::Shut => "SHUT",
        }
    }
}

/// Ports `baskfy_core.vbt.config.SignalState` — what a row of `vb_signal_daily` is.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum SignalState {
    /// The scan hit and every trend filter passed.
    #[serde(rename = "SIGNAL")]
    Signal,
    /// The scan hit and at least one trend filter failed (stored on purpose, PACK.6).
    #[serde(rename = "SCAN_ONLY")]
    ScanOnly,
}

impl SignalState {
    /// The enum's value, as Python's `SignalState.X.value`.
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::Signal => "SIGNAL",
            Self::ScanOnly => "SCAN_ONLY",
        }
    }
}

/// Ports `baskfy_core.vbt.config.TrendFilter` — the six filters of `docs/vbt/04` §3.2, by the
/// letters STRATEGY §3 gives them, in declaration order.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub enum TrendFilter {
    /// A — `close > SMA(close, dma_bars)`.
    #[serde(rename = "A")]
    AAbove200dma,
    /// B — `close >` the highest high of the prior `breakout_high_bars` sessions.
    #[serde(rename = "B")]
    BTwentyDayHigh,
    /// C — `ret_20_pct < max_ret_20_pct`.
    #[serde(rename = "C")]
    CNotExtended,
    /// D — `close_position >= min_close_position`.
    #[serde(rename = "D")]
    DStrongClose,
    /// E — `change_pct <= max_change_pct`.
    #[serde(rename = "E")]
    EChangeCeiling,
    /// F — `turnover_avg >= min_turnover_avg_inr`.
    #[serde(rename = "F")]
    FTurnoverFloor,
}

impl TrendFilter {
    /// Every filter, in Python's declaration (and iteration) order.
    pub const ALL: [Self; 6] = [
        Self::AAbove200dma,
        Self::BTwentyDayHigh,
        Self::CNotExtended,
        Self::DStrongClose,
        Self::EChangeCeiling,
        Self::FTurnoverFloor,
    ];

    /// The letter, as Python's `TrendFilter.X.value`.
    #[must_use]
    pub fn value(self) -> &'static str {
        match self {
            Self::AAbove200dma => "A",
            Self::BTwentyDayHigh => "B",
            Self::CNotExtended => "C",
            Self::DStrongClose => "D",
            Self::EChangeCeiling => "E",
            Self::FTurnoverFloor => "F",
        }
    }
}

/// Ports `baskfy_core.vbt.config.DataConfig` — the universe, the calendar and the missing-bar
/// tolerance (`docs/vbt/04` §1, §2).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct DataConfig {
    /// NSE cash equities.
    pub instrument_type: String,
    /// BE/BZ stay in; SME is out.
    pub series_allowed: Vec<String>,
    /// A null series is a delisted name with no current listing row, and is kept.
    pub keep_null_series: bool,
    /// The `etf` index universe is the authority for ETFs.
    pub etf_universe_slug: String,
    /// A name pattern that catches an ETF missing from the universe.
    pub etf_name_pattern: String,
    /// A symbol pattern that catches an ETF missing from the universe.
    pub etf_symbol_pattern: String,
    /// The thin-session rule: a session below this share of the centred rolling median of the
    /// traded-name count is dropped.
    pub thin_session_min_share: f64,
    /// The centred median's window, in sessions.
    pub thin_session_window_bars: i64,
    /// The centred median's `min_samples`.
    pub thin_session_min_periods: i64,
    /// The missing-bar tolerance: a window over n sessions is valid with
    /// `max(2, round(n x this))` bars.
    pub rolling_min_share: f64,
}

impl Default for DataConfig {
    fn default() -> Self {
        Self {
            instrument_type: "EQ".to_owned(),
            series_allowed: vec!["EQ".to_owned(), "BE".to_owned(), "BZ".to_owned()],
            keep_null_series: true,
            etf_universe_slug: "etf".to_owned(),
            etf_name_pattern: r"\bETF\b".to_owned(),
            etf_symbol_pattern: "(BEES|ETF|IETF)$".to_owned(),
            thin_session_min_share: 0.25,
            thin_session_window_bars: 41,
            thin_session_min_periods: 5,
            rolling_min_share: 0.90,
        }
    }
}

impl DataConfig {
    /// Ports `DataConfig.min_samples`: how many bars a `window_bars` window needs before it has
    /// a value — `max(2, round(window_bars * rolling_min_share))`, Python's half-even `round`.
    ///
    /// # Errors
    /// [`CoreError::InvalidInput`] when the product is not finite (Python's `round` raises
    /// `ValueError` on NaN and `OverflowError` on an infinity) or does not fit an `i64`.
    pub fn min_samples(&self, window_bars: i64) -> CoreResult<i64> {
        #[allow(clippy::cast_precision_loss)] // Python's int * float converts the same way
        let product = window_bars as f64 * self.rolling_min_share;
        let rounded = py_round_to_int(product)?;
        Ok(rounded.max(2))
    }
}

/// Python's `round(x)` on a float (half-even, to an `int`).
fn py_round_to_int(x: f64) -> CoreResult<i64> {
    if x.is_nan() {
        return Err(CoreError::InvalidInput(
            "cannot convert float NaN to integer".to_owned(),
        ));
    }
    if x.is_infinite() {
        return Err(CoreError::InvalidInput(
            "cannot convert float infinity to integer".to_owned(),
        ));
    }
    let r = x.round_ties_even();
    // i64::MIN as f64 is exact (-2^63); i64::MAX as f64 rounds up to 2^63, which is excluded.
    #[allow(clippy::cast_precision_loss)]
    let (lo, hi) = (i64::MIN as f64, i64::MAX as f64);
    if r < lo || r >= hi {
        return Err(CoreError::InvalidInput(format!(
            "round({x}) does not fit a 64-bit integer"
        )));
    }
    #[allow(clippy::cast_possible_truncation)] // range checked above; r is integral
    let out = r as i64;
    Ok(out)
}

/// Ports `baskfy_core.vbt.config.ScanConfig` — Chartink's five lines (`docs/vbt/04` §3.1).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct ScanConfig {
    /// 1. `volume > SMA(volume, vol_sma_bars) x vol_mult` (strict).
    pub vol_mult: f64,
    /// The volume SMA's window; it includes the signal day.
    pub vol_sma_bars: i64,
    /// 2. `close_raw > this` (strict) — an exchange price, never the adjusted close.
    pub min_close_raw_inr: f64,
    /// 3. `change_pct >= this`.
    pub min_change_pct: f64,
    /// 4. `vol_sma >= this`.
    pub min_vol_sma: f64,
    /// 5. `volume > this` (strict).
    pub min_volume: f64,
}

impl Default for ScanConfig {
    fn default() -> Self {
        Self {
            vol_mult: 3.0,
            vol_sma_bars: 50,
            min_close_raw_inr: 30.0,
            min_change_pct: 6.5,
            min_vol_sma: 25_000.0,
            min_volume: 50_000.0,
        }
    }
}

/// Ports `baskfy_core.vbt.config.TrendConfig` — the six filters (`docs/vbt/04` §3.2).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct TrendConfig {
    /// A. `close > SMA(close, dma_bars)`.
    pub dma_bars: i64,
    /// B. `close >` the highest high of the prior `breakout_high_bars` sessions, today excluded.
    pub breakout_high_bars: i64,
    /// C. the return window, in sessions.
    pub ret_bars: i64,
    /// C. `ret_20_pct < this` (strict).
    pub max_ret_20_pct: f64,
    /// D. `(close - low) / (high - low) >= this`.
    pub min_close_position: f64,
    /// E. `change_pct <= this`.
    pub max_change_pct: f64,
    /// F. the turnover average's window.
    pub turnover_bars: i64,
    /// F. `mean(close_raw x volume) >= this` — Rs 2 crore.
    pub min_turnover_avg_inr: f64,
}

impl Default for TrendConfig {
    fn default() -> Self {
        Self {
            dma_bars: 200,
            breakout_high_bars: 20,
            ret_bars: 20,
            max_ret_20_pct: 25.0,
            min_close_position: 0.6,
            max_change_pct: 15.0,
            turnover_bars: 20,
            min_turnover_avg_inr: 2e7,
        }
    }
}

/// Ports `baskfy_core.vbt.config.FilterExplanation` — one trend filter in the words a page can
/// print, beside the rule it actually applies.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct FilterExplanation {
    /// The letter (`"A"` … `"F"`).
    pub code: String,
    /// The plain-words label.
    pub label: String,
    /// The rule as the engine applies it, thresholds read from the config.
    pub rule: String,
}

/// Python's `f"{x:g}"`: six significant digits, trailing zeros stripped, scientific notation
/// when the decimal exponent is below -4 or at least 6.
#[must_use]
pub fn py_format_g(x: f64) -> String {
    if x.is_nan() {
        return "nan".to_owned();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf" } else { "-inf" }.to_owned();
    }
    if x == 0.0 {
        return if x.is_sign_negative() { "-0" } else { "0" }.to_owned();
    }
    // Round once to six significant digits; the exponent is the rounded value's.
    let sci = format!("{x:.5e}");
    let Some((mantissa, exp)) = sci.split_once('e') else {
        return sci;
    };
    let Ok(exp) = exp.parse::<i64>() else {
        return sci;
    };
    let negative = mantissa.starts_with('-');
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    let sign = if negative { "-" } else { "" };
    if (-4..6).contains(&exp) {
        // Fixed notation with exactly the six rounded digits, then strip.
        let body = if exp < 0 {
            let zeros = "0".repeat(usize::try_from(-exp - 1).unwrap_or(0));
            format!("0.{zeros}{digits}")
        } else {
            let cut = usize::try_from(exp + 1).unwrap_or(0).min(digits.len());
            format!("{}.{}", &digits[..cut], &digits[cut..])
        };
        let trimmed = body.trim_end_matches('0').trim_end_matches('.');
        return format!("{sign}{trimmed}");
    }
    let (head, rest) = digits.split_at(1.min(digits.len()));
    let rest = rest.trim_end_matches('0');
    let body = if rest.is_empty() {
        head.to_owned()
    } else {
        format!("{head}.{rest}")
    };
    let esign = if exp < 0 { "-" } else { "+" };
    format!("{sign}{body}e{esign}{:02}", exp.unsigned_abs())
}

fn crore(rupees: f64) -> String {
    let crore = rupees / 1e7;
    format!("Rs {} crore", py_format_g(crore))
}

/// Ports `baskfy_core.vbt.config.filter_legend`: A, B, C … spelled out, each threshold read from
/// the config that rejects the row. `None` reads the default `TrendConfig`.
#[must_use]
pub fn filter_legend(config: Option<&TrendConfig>) -> Vec<FilterExplanation> {
    let default = TrendConfig::default();
    let cfg = config.unwrap_or(&default);
    let item = |f: TrendFilter, label: String, rule: String| FilterExplanation {
        code: f.value().to_owned(),
        label,
        rule,
    };
    vec![
        item(
            TrendFilter::AAbove200dma,
            format!("Above its {}-day average", cfg.dma_bars),
            format!("close > SMA(close, {})", cfg.dma_bars),
        ),
        item(
            TrendFilter::BTwentyDayHigh,
            format!("New {}-day high", cfg.breakout_high_bars),
            format!(
                "close > the highest high of the prior {} sessions (today excluded)",
                cfg.breakout_high_bars
            ),
        ),
        item(
            TrendFilter::CNotExtended,
            "Not already extended".to_owned(),
            format!(
                "{}-day return < {}%",
                cfg.ret_bars,
                py_format_g(cfg.max_ret_20_pct)
            ),
        ),
        item(
            TrendFilter::DStrongClose,
            "Closed strong".to_owned(),
            format!(
                "(close - low) / (high - low) >= {}",
                py_format_g(cfg.min_close_position)
            ),
        ),
        item(
            TrendFilter::EChangeCeiling,
            "Not a spike".to_owned(),
            format!("the day's change <= {}%", py_format_g(cfg.max_change_pct)),
        ),
        item(
            TrendFilter::FTurnoverFloor,
            "Liquid enough to exit".to_owned(),
            format!(
                "{}-day average turnover >= {}",
                cfg.turnover_bars,
                crore(cfg.min_turnover_avg_inr)
            ),
        ),
    ]
}

/// Ports `baskfy_core.vbt.config.BreadthConfig` — the regime gate (`docs/vbt/04` §4).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct BreadthConfig {
    /// The average each name is measured against.
    pub dma_bars: i64,
    /// OPEN above this (strict `>`), SHUT at or below it.
    pub min_pct_above_dma: f64,
}

impl Default for BreadthConfig {
    fn default() -> Self {
        Self {
            dma_bars: 200,
            min_pct_above_dma: 40.0,
        }
    }
}

/// Ports `baskfy_core.vbt.config.EntryConfig` — the pullback entry (`docs/vbt/04` §7).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct EntryConfig {
    /// The limit is the signal bar's close.
    pub limit_at: String,
    /// The order works this many sessions after the signal (the parameter with a cliff).
    pub valid_sessions: i64,
    /// Require the low to trade this far through the limit before a fill is assumed.
    pub fill_through_pct: f64,
}

impl Default for EntryConfig {
    fn default() -> Self {
        Self {
            limit_at: "SIGNAL_CLOSE".to_owned(),
            valid_sessions: 3,
            fill_through_pct: 0.0,
        }
    }
}

/// Ports `baskfy_core.vbt.config.SizingConfig` — ten equal slots, and every cap named
/// (`docs/vbt/04` §5).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct SizingConfig {
    /// Ten slots, 10% of equity each.
    pub max_slots: i64,
    /// LV10: a fresh qualifying setup in a held name is a new entry.
    pub pyramiding: bool,
    /// At most this many open entries in one name.
    pub max_entries_per_name: i64,
    /// The per-position ceiling, percent of equity.
    pub max_position_pct: f64,
    /// Lines in this plan plus the session's confirmed or sent orders.
    pub max_new_entries_per_session: i64,
    /// Never more than this share of the name's 20-day average turnover.
    pub max_position_vs_turnover: f64,
    /// Below this the brokerage dominates the edge (house rule 9: a `Decimal`).
    pub min_trade_value_inr: Decimal,
    /// The first live sessions plan at this share of a slot.
    pub risk_multiplier_first_live: f64,
    /// How many first live sessions.
    pub first_live_sessions: i64,
}

impl Default for SizingConfig {
    fn default() -> Self {
        Self {
            max_slots: 10,
            pyramiding: true,
            max_entries_per_name: 2,
            max_position_pct: 12.5,
            max_new_entries_per_session: 3,
            max_position_vs_turnover: 0.01,
            min_trade_value_inr: Decimal::from(10_000),
            risk_multiplier_first_live: 0.5,
            first_live_sessions: 5,
        }
    }
}

/// Ports `baskfy_core.vbt.config.ExitConfig` — the stop and the exit (`docs/vbt/04` §6).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct ExitConfig {
    /// The disaster stop, percent below the fill.
    pub stop_pct: f64,
    /// The GTT's resting limit as a fraction of its trigger.
    pub gtt_limit_fraction: f64,
    /// The band's lower bound handed to the gateway.
    pub gtt_band_min_pct: f64,
    /// The band's upper bound handed to the gateway.
    pub gtt_band_max_pct: f64,
    /// The working exit: a close below this EMA sells at the next open.
    pub trail_ema_bars: i64,
    /// A held name with this many blank sessions is written off at its last close.
    pub no_bar_tolerance_sessions: i64,
    /// LV9: the working exit is Qullamaggie's (the swing book's).
    pub qulla_exits: bool,
    /// The swing `StopConfig` the Qullamaggie exit reads.
    pub qulla: StopConfig,
}

impl Default for ExitConfig {
    fn default() -> Self {
        Self {
            stop_pct: 12.0,
            gtt_limit_fraction: 0.97,
            gtt_band_min_pct: 0.005,
            gtt_band_max_pct: 0.15,
            trail_ema_bars: 21,
            no_bar_tolerance_sessions: 5,
            qulla_exits: true,
            qulla: StopConfig::default(),
        }
    }
}

/// Ports `baskfy_core.vbt.config.CostConfig` — what a round trip costs the backtest.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct CostConfig {
    /// 25 bps a side. Used by the backtest only.
    pub cost_pct_per_side: f64,
}

impl Default for CostConfig {
    fn default() -> Self {
        Self {
            cost_pct_per_side: 0.25,
        }
    }
}

/// Ports `baskfy_core.vbt.config.VbtConfig` — everything the sleeve can be recalibrated on.
#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct VbtConfig {
    /// The universe, the calendar, the missing-bar tolerance.
    pub data: DataConfig,
    /// Chartink's five lines.
    pub scan: ScanConfig,
    /// The six trend filters.
    pub trend: TrendConfig,
    /// The regime gate.
    pub breadth: BreadthConfig,
    /// The pullback entry.
    pub entry: EntryConfig,
    /// Slots and caps.
    pub sizing: SizingConfig,
    /// Stop and exit.
    pub exits: ExitConfig,
    /// Backtest costs.
    pub costs: CostConfig,
}

impl VbtConfig {
    /// Ports the `VbtConfig.bars_required` property: sessions of history a full detection
    /// needs (the longest window, +1).
    #[must_use]
    pub fn bars_required(&self) -> i64 {
        let longest = [
            self.trend.dma_bars,
            self.breadth.dma_bars,
            self.scan.vol_sma_bars,
            self.exits.trail_ema_bars,
            self.trend.breakout_high_bars.saturating_add(1),
            self.trend.ret_bars.saturating_add(1),
            self.trend.turnover_bars,
        ]
        .into_iter()
        .max()
        .unwrap_or(0);
        longest.saturating_add(1)
    }
}

/// Ports `baskfy_core.vbt.config.TICK_INR` — the exchange's tick for NSE cash equities.
pub const TICK_INR: &str = "0.05";

/// Ports `baskfy_core.vbt.config.DRY_RUN_SESSIONS_REQUIRED` — zero since 11 Sep 2026 (the paper
/// gate was withdrawn by Maulik, DECISIONS-VB VB11.4).
pub const DRY_RUN_SESSIONS_REQUIRED: i64 = 0;
