//! Port of `baskfy_core.options.config`: every threshold of the options run, named once.

use chrono::{NaiveDate, NaiveTime};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};

fn hm(h: u32, m: u32) -> NaiveTime {
    NaiveTime::from_hms_opt(h, m, 0).unwrap_or_default()
}

fn d(mantissa: i64, scale: u32) -> Decimal {
    Decimal::new(mantissa, scale)
}

/// `OPTIONS_COST_RATES_REVIEWED_ON`.
pub fn options_cost_rates_reviewed_on() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 9, 22).unwrap_or_default()
}

/// The five sleeve codes (the `op_sleeve` enum).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub enum Sleeve {
    /// The monthly condor.
    #[serde(rename = "O1M")]
    O1m,
    /// The weekly condor.
    #[serde(rename = "O1W")]
    O1w,
    /// The directional long.
    #[serde(rename = "O2")]
    O2,
    /// Expiry setup A.
    #[serde(rename = "O3A")]
    O3a,
    /// Expiry setup B.
    #[serde(rename = "O3B")]
    O3b,
}

impl Sleeve {
    /// Every sleeve, in declaration order.
    pub const ALL: [Sleeve; 5] = [
        Sleeve::O1m,
        Sleeve::O1w,
        Sleeve::O2,
        Sleeve::O3a,
        Sleeve::O3b,
    ];

    /// The enum's value.
    pub fn value(self) -> &'static str {
        match self {
            Sleeve::O1m => "O1M",
            Sleeve::O1w => "O1W",
            Sleeve::O2 => "O2",
            Sleeve::O3a => "O3A",
            Sleeve::O3b => "O3B",
        }
    }
}

/// How config and flags group the sleeves: `O3A`/`O3B` are one group.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub enum SleeveGroup {
    /// `O1M`.
    #[serde(rename = "O1M")]
    O1m,
    /// `O1W`.
    #[serde(rename = "O1W")]
    O1w,
    /// `O2`.
    #[serde(rename = "O2")]
    O2,
    /// `O3`.
    #[serde(rename = "O3")]
    O3,
}

/// The config/flag group a sleeve code belongs to.
pub fn group_of(sleeve: Sleeve) -> SleeveGroup {
    match sleeve {
        Sleeve::O1m => SleeveGroup::O1m,
        Sleeve::O1w => SleeveGroup::O1w,
        Sleeve::O2 => SleeveGroup::O2,
        Sleeve::O3a | Sleeve::O3b => SleeveGroup::O3,
    }
}

/// The sleeves that share the expiry-day slot (`04` §8.6).
pub const SLOT_SLEEVES: [Sleeve; 4] = [Sleeve::O1m, Sleeve::O1w, Sleeve::O3a, Sleeve::O3b];

/// `04` §1.1 — weekly or monthly.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum ExpiryKind {
    /// `WEEKLY`.
    Weekly,
    /// `MONTHLY`.
    Monthly,
}

impl ExpiryKind {
    /// The enum's value.
    pub fn value(self) -> &'static str {
        match self {
            ExpiryKind::Weekly => "WEEKLY",
            ExpiryKind::Monthly => "MONTHLY",
        }
    }
}

/// Call or put.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub enum OptionType {
    /// `CE`.
    #[serde(rename = "CE")]
    Ce,
    /// `PE`.
    #[serde(rename = "PE")]
    Pe,
}

impl OptionType {
    /// The enum's value.
    pub fn value(self) -> &'static str {
        match self {
            OptionType::Ce => "CE",
            OptionType::Pe => "PE",
        }
    }
}

/// Buy or sell.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum Side {
    /// `BUY`.
    Buy,
    /// `SELL`.
    Sell,
}

impl Side {
    /// The enum's value.
    pub fn value(self) -> &'static str {
        match self {
            Side::Buy => "BUY",
            Side::Sell => "SELL",
        }
    }
}

/// Paper or live, fixed by `options_gates()` at 09:15.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum Mode {
    /// Simulated.
    Paper,
    /// Real orders.
    Live,
}

/// `04` §7.3 — how a sleeve was sized.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SizingMode {
    /// From a risk budget.
    Budget,
    /// One paper lot with no capital.
    PaperOneLot,
}

impl SizingMode {
    /// The enum's value.
    pub fn value(self) -> &'static str {
        match self {
            SizingMode::Budget => "BUDGET",
            SizingMode::PaperOneLot => "PAPER_ONE_LOT",
        }
    }
}

/// What Kite's `oi` counts (`04` §2.4).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum OiUnit {
    /// Units.
    Units,
    /// Lots.
    Lots,
}

/// O1's two variants.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum CondorVariant {
    /// O1-M.
    Monthly,
    /// O1-W.
    Weekly,
}

impl CondorVariant {
    /// The enum's value.
    pub fn value(self) -> &'static str {
        match self {
            CondorVariant::Monthly => "MONTHLY",
            CondorVariant::Weekly => "WEEKLY",
        }
    }
}

/// `04` §1 and the session clock every sleeve shares.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct CalendarConfig {
    /// The underlying traded.
    pub underlying: String,
    /// The underlyings v1 accepts.
    pub allowed_underlyings: Vec<String>,
    /// The session open.
    pub market_open: NaiveTime,
    /// The settlement moment of an expiring contract.
    pub market_close: NaiveTime,
}

impl Default for CalendarConfig {
    fn default() -> Self {
        Self {
            underlying: "NIFTY".to_owned(),
            allowed_underlyings: vec!["NIFTY".to_owned()],
            market_open: hm(9, 15),
            market_close: hm(15, 30),
        }
    }
}

impl CalendarConfig {
    /// Python's `__post_init__` check: the underlying must be allowed in v1.
    pub fn validate(&self) -> CoreResult<()> {
        if self.allowed_underlyings.contains(&self.underlying) {
            Ok(())
        } else {
            Err(CoreError::InvalidInput(format!(
                "underlying '{}' is not allowed in v1 (allowed: {}; 02 Track C §5)",
                self.underlying,
                self.allowed_underlyings.join(", ")
            )))
        }
    }
}

/// `04` §2 — which contracts are read, the forward, IV/greeks, liquidity, staleness.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct ChainConfig {
    /// Strikes either side of ATM the collector stores.
    pub snapshot_strikes: i64,
    /// The rate in the forward and the model.
    pub rate: f64,
    /// IV bisection floor.
    pub iv_lower: f64,
    /// IV bisection ceiling.
    pub iv_upper: f64,
    /// IV bisection tolerance.
    pub iv_tolerance: f64,
    /// Smallest premium a quote is solved for.
    pub min_premium: Decimal,
    /// Widest spread, percent of mid.
    pub max_spread_pct: Decimal,
    /// Depth levels read.
    pub depth_levels: i64,
    /// Smallest open interest, in lots.
    pub min_oi_lots: i64,
    /// What `oi` counts.
    pub oi_unit: OiUnit,
    /// A quote older than this is stale.
    pub stale_quote_seconds: i64,
    /// No index tick for this long is stale.
    pub stale_index_seconds: i64,
    /// A scan snapshot older than this is stale.
    pub stale_scan_seconds: i64,
    /// The greeks model's name.
    pub greeks_model: String,
}

impl Default for ChainConfig {
    fn default() -> Self {
        Self {
            snapshot_strikes: 15,
            rate: 0.065,
            iv_lower: 0.01,
            iv_upper: 5.0,
            iv_tolerance: 1e-6,
            min_premium: d(50, 2),
            max_spread_pct: d(30, 1),
            depth_levels: 3,
            min_oi_lots: 200,
            oi_unit: OiUnit::Units,
            stale_quote_seconds: 15,
            stale_index_seconds: 30,
            stale_scan_seconds: 120,
            greeks_model: "black76-parity-v1".to_owned(),
        }
    }
}

/// O1 (`04` §3). Two instances, never shared.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct CondorConfig {
    /// Monthly or weekly.
    pub variant: CondorVariant,
    /// Gate: largest opening gap, percent.
    pub gap_max_pct: Decimal,
    /// Gate: widest observation range, percent.
    pub range_max_pct: Decimal,
    /// Gate: highest Kaufman efficiency ratio.
    pub er_max: Decimal,
    /// Observation window start.
    pub observation_start: NaiveTime,
    /// Observation window end.
    pub observation_end: NaiveTime,
    /// Opening range end.
    pub opening_range_end: NaiveTime,
    /// Bars a complete observation window has.
    pub min_bars: i64,
    /// Short-strike `|delta|` floor.
    pub delta_min: Decimal,
    /// Short-strike `|delta|` ceiling.
    pub delta_max: Decimal,
    /// Short-strike `|delta|` target.
    pub delta_target: Decimal,
    /// Points beyond the opening range a short must sit.
    pub strike_buffer_points: Decimal,
    /// Wing width, points.
    pub wing_width_points: Decimal,
    /// Credit floor as a fraction of the width.
    pub credit_floor_frac: Decimal,
    /// Credit target as a fraction of the width.
    pub credit_target_frac: Decimal,
    /// Profit at `D <= profit_take_frac * C`.
    pub profit_take_frac: Decimal,
    /// Stop at `D >= stop_frac * C`.
    pub stop_frac: Decimal,
    /// When the plan is built.
    pub plan_time: NaiveTime,
    /// Last moment a plan may be confirmed.
    pub entry_window_end: NaiveTime,
    /// The hard exit.
    pub hard_exit_time: NaiveTime,
    /// Largest cost share of the expected gain.
    pub cost_share_max: Decimal,
    /// Risk per trade, percent of capital.
    pub risk_per_trade_pct: Decimal,
    /// Lot cap.
    pub max_lots: i64,
    /// Reserve per lot, rupees.
    pub reserve_per_lot_inr: Decimal,
    /// Sessions before tier 3.
    pub tier3_min_sessions: i64,
}

impl Default for CondorConfig {
    fn default() -> Self {
        Self {
            variant: CondorVariant::Monthly,
            gap_max_pct: d(75, 2),
            range_max_pct: d(80, 2),
            er_max: d(30, 2),
            observation_start: hm(9, 15),
            observation_end: hm(9, 59),
            opening_range_end: hm(9, 44),
            min_bars: 45,
            delta_min: d(20, 2),
            delta_max: d(25, 2),
            delta_target: d(22, 2),
            strike_buffer_points: d(0, 0),
            wing_width_points: d(150, 0),
            credit_floor_frac: d(25, 2),
            credit_target_frac: d(30, 2),
            profit_take_frac: d(50, 2),
            stop_frac: d(150, 2),
            plan_time: hm(10, 0),
            entry_window_end: hm(10, 15),
            hard_exit_time: hm(14, 30),
            cost_share_max: d(20, 2),
            risk_per_trade_pct: d(10, 1),
            max_lots: 3,
            reserve_per_lot_inr: d(1000, 0),
            tier3_min_sessions: 12,
        }
    }
}

impl CondorConfig {
    /// `OptionsConfig().condor_weekly`.
    pub fn weekly() -> Self {
        Self {
            variant: CondorVariant::Weekly,
            risk_per_trade_pct: d(5, 1),
            max_lots: 2,
            tier3_min_sessions: 20,
            ..Self::default()
        }
    }

    /// The `OptionsConfig()` default instance for a variant.
    pub fn for_variant(variant: CondorVariant) -> Self {
        match variant {
            CondorVariant::Monthly => Self::default(),
            CondorVariant::Weekly => Self::weekly(),
        }
    }
}

/// O2 (`04` §4).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct DirectionalConfig {
    /// EMA span of the trend, days.
    pub trend_ema_days: i64,
    /// Largest opening gap, percent.
    pub gap_max_pct: Decimal,
    /// Opening range start.
    pub opening_range_start: NaiveTime,
    /// Opening range end.
    pub opening_range_end: NaiveTime,
    /// Bars a complete opening range has.
    pub opening_range_bars: i64,
    /// Widest opening range, percent.
    pub or_max_pct: Decimal,
    /// Highest India VIX.
    pub vix_max: Decimal,
    /// Trigger bar width, minutes.
    pub bar_minutes: i64,
    /// Entry window start.
    pub entry_window_start: NaiveTime,
    /// Entry window end.
    pub entry_window_end: NaiveTime,
    /// Break buffer, percent.
    pub buffer_pct: Decimal,
    /// Steps in the money.
    pub itm_steps: i64,
    /// `|delta|` floor.
    pub delta_min: Decimal,
    /// `|delta|` ceiling.
    pub delta_max: Decimal,
    /// Stop as a fraction of the entry.
    pub stop_frac: Decimal,
    /// Target as a fraction of the entry.
    pub target_frac: Decimal,
    /// Minutes before the time stop.
    pub time_stop_minutes: i64,
    /// Gain the time stop needs, a fraction.
    pub time_stop_min_gain: Decimal,
    /// The hard exit.
    pub hard_exit_time: NaiveTime,
    /// Reserve per lot, rupees.
    pub reserve_per_lot_inr: Decimal,
    /// Premium cap, percent of capital.
    pub premium_cap_pct: Decimal,
    /// Largest cost share.
    pub cost_share_max: Decimal,
    /// Risk per trade, percent.
    pub risk_per_trade_pct: Decimal,
    /// Lot cap.
    pub max_lots: i64,
    /// Sessions before tier 3.
    pub tier3_min_sessions: i64,
}

impl Default for DirectionalConfig {
    fn default() -> Self {
        Self {
            trend_ema_days: 20,
            gap_max_pct: d(10, 1),
            opening_range_start: hm(9, 15),
            opening_range_end: hm(9, 29),
            opening_range_bars: 15,
            or_max_pct: d(90, 2),
            vix_max: d(22, 0),
            bar_minutes: 5,
            entry_window_start: hm(9, 30),
            entry_window_end: hm(13, 30),
            buffer_pct: d(5, 2),
            itm_steps: 1,
            delta_min: d(50, 2),
            delta_max: d(75, 2),
            stop_frac: d(30, 2),
            target_frac: d(60, 2),
            time_stop_minutes: 45,
            time_stop_min_gain: d(10, 2),
            hard_exit_time: hm(15, 0),
            reserve_per_lot_inr: d(300, 0),
            premium_cap_pct: d(10, 0),
            cost_share_max: d(15, 2),
            risk_per_trade_pct: d(5, 1),
            max_lots: 2,
            tier3_min_sessions: 60,
        }
    }
}

/// O3 (`04` §5).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct ExpirySetupsConfig {
    /// O3-A switched on.
    pub o3a_enabled: bool,
    /// O3-B switched on.
    pub o3b_enabled: bool,
    /// Spread width, points.
    pub width_points: Decimal,
    /// Largest debit as a fraction of the width.
    pub max_debit_frac: Decimal,
    /// O3-A range start.
    pub o3a_range_start: NaiveTime,
    /// O3-A range end.
    pub o3a_range_end: NaiveTime,
    /// Bars a complete O3-A range has.
    pub o3a_range_bars: i64,
    /// Widest O3-A range, percent.
    pub o3a_range_max_pct: Decimal,
    /// O3-A trigger window start.
    pub o3a_window_start: NaiveTime,
    /// O3-A trigger window end.
    pub o3a_window_end: NaiveTime,
    /// The last moment an O3-A plan can be confirmed.
    pub o3a_entry_window_end: NaiveTime,
    /// O3-A break buffer, percent.
    pub o3a_buffer_pct: Decimal,
    /// O3-A smallest efficiency ratio.
    pub o3a_er_min: Decimal,
    /// O3-B smallest gap, percent.
    pub o3b_gap_min_pct: Decimal,
    /// O3-B largest gap, percent.
    pub o3b_gap_max_pct: Decimal,
    /// O3-B hold end.
    pub o3b_hold_end: NaiveTime,
    /// O3-B plan time.
    pub o3b_plan_time: NaiveTime,
    /// O3-B window end.
    pub o3b_window_end: NaiveTime,
    /// Trigger bar width, minutes.
    pub bar_minutes: i64,
    /// Target as a fraction of the width.
    pub target_frac_of_width: Decimal,
    /// Stop as a fraction of the debit.
    pub stop_frac_of_debit: Decimal,
    /// The hard exit.
    pub hard_exit_time: NaiveTime,
    /// Reserve per lot, rupees.
    pub reserve_per_lot_inr: Decimal,
    /// Largest cost share.
    pub cost_share_max: Decimal,
    /// Risk per trade, percent.
    pub risk_per_trade_pct: Decimal,
    /// Lot cap.
    pub max_lots: i64,
    /// Sessions before tier 3.
    pub tier3_min_sessions: i64,
}

impl Default for ExpirySetupsConfig {
    fn default() -> Self {
        Self {
            o3a_enabled: true,
            o3b_enabled: true,
            width_points: d(100, 0),
            max_debit_frac: d(55, 2),
            o3a_range_start: hm(9, 15),
            o3a_range_end: hm(10, 14),
            o3a_range_bars: 60,
            o3a_range_max_pct: d(120, 2),
            o3a_window_start: hm(10, 19),
            o3a_window_end: hm(13, 0),
            o3a_entry_window_end: hm(13, 30),
            o3a_buffer_pct: d(5, 2),
            o3a_er_min: d(40, 2),
            o3b_gap_min_pct: d(50, 2),
            o3b_gap_max_pct: d(150, 2),
            o3b_hold_end: hm(9, 44),
            o3b_plan_time: hm(9, 45),
            o3b_window_end: hm(10, 0),
            bar_minutes: 5,
            target_frac_of_width: d(80, 2),
            stop_frac_of_debit: d(50, 2),
            hard_exit_time: hm(14, 45),
            reserve_per_lot_inr: d(500, 0),
            cost_share_max: d(20, 2),
            risk_per_trade_pct: d(5, 1),
            max_lots: 2,
            tier3_min_sessions: 20,
        }
    }
}

/// `04` §6 — every statutory and broker rate as a dated field.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct CostRates {
    /// Flat brokerage per executed F&O order.
    pub brokerage_per_order_inr: Decimal,
    /// STT on the sale of an option, percent of premium.
    pub stt_sell_premium_pct: Decimal,
    /// STT on an exercised option, percent of intrinsic.
    pub stt_exercise_intrinsic_pct: Decimal,
    /// NSE options transaction charge, percent of premium.
    pub exchange_txn_pct: Decimal,
    /// SEBI turnover fee, rupees per crore.
    pub sebi_per_crore_inr: Decimal,
    /// IPFT, rupees per crore.
    pub ipft_per_crore_inr: Decimal,
    /// Stamp duty on the buy side, percent.
    pub stamp_buy_pct: Decimal,
    /// GST, percent.
    pub gst_pct: Decimal,
    /// MIS auto square-off charge per order.
    pub auto_squareoff_inr: Decimal,
    /// Tier 2's synthetic half-spread, percent of premium.
    pub synthetic_half_spread_pct: Decimal,
    /// Tier 2's synthetic half-spread floor, rupees per unit.
    pub synthetic_half_spread_min_inr: Decimal,
    /// The day every rate was last checked.
    pub reviewed_on: NaiveDate,
    /// Days after `reviewed_on` before the review warning fires.
    pub review_after_days: i64,
}

impl Default for CostRates {
    fn default() -> Self {
        Self {
            brokerage_per_order_inr: d(20, 0),
            stt_sell_premium_pct: d(15, 2),
            stt_exercise_intrinsic_pct: d(15, 2),
            exchange_txn_pct: d(3553, 5),
            sebi_per_crore_inr: d(10, 0),
            ipft_per_crore_inr: d(1, 2),
            stamp_buy_pct: d(3, 3),
            gst_pct: d(18, 0),
            auto_squareoff_inr: d(50, 0),
            synthetic_half_spread_pct: d(15, 1),
            synthetic_half_spread_min_inr: d(10, 2),
            reviewed_on: options_cost_rates_reviewed_on(),
            review_after_days: 90,
        }
    }
}

/// `04` §7.5 — the first-live multiplier.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct SizingConfig {
    /// Real trades before full size.
    pub first_live_trades: i64,
    /// Risk multiplier while first-live.
    pub first_live_risk_multiplier: Decimal,
}

impl Default for SizingConfig {
    fn default() -> Self {
        Self {
            first_live_trades: 5,
            first_live_risk_multiplier: d(5, 1),
        }
    }
}

/// `04` §8 — the shared execution mechanics.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct ExecutionConfig {
    /// Ticks a first attempt improves on the quoted side.
    pub limit_improve_ticks: i64,
    /// Seconds each attempt waits for a fill.
    pub fill_wait_seconds: i64,
    /// Whether a risk-reducing close gets a third, marketable LIMIT at the touch.
    pub exit_final_marketable: bool,
    /// Ticks worse each level past the first fills at in a paper fill.
    pub latency_ticks: i64,
    /// Minutes a plan lives.
    pub plan_ttl_minutes: i64,
    /// O1/O3 lose the feed only after this time.
    pub feed_watch_from: NaiveTime,
    /// O2 loses the feed only once a position is this old.
    pub feed_grace_seconds: i64,
}

impl Default for ExecutionConfig {
    fn default() -> Self {
        Self {
            limit_improve_ticks: 1,
            fill_wait_seconds: 20,
            exit_final_marketable: true,
            latency_ticks: 1,
            plan_ttl_minutes: 30,
            feed_watch_from: hm(14, 0),
            feed_grace_seconds: 60,
        }
    }
}

/// `04` §9 — per-sleeve limits in R, the per-trade breach, the book's limits.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct RiskConfig {
    /// Daily loss limit, R.
    pub daily_loss_r: Decimal,
    /// Weekly loss limit, R.
    pub weekly_loss_r: Decimal,
    /// Monthly loss limit, R.
    pub monthly_loss_r: Decimal,
    /// A marked loss beyond `risk_budget * budget_breach_frac` closes the position.
    pub budget_breach_frac: Decimal,
    /// Book daily loss, percent.
    pub book_daily_loss_pct: Decimal,
    /// Book monthly loss, percent.
    pub book_monthly_loss_pct: Decimal,
}

impl Default for RiskConfig {
    fn default() -> Self {
        Self {
            daily_loss_r: d(2, 0),
            weekly_loss_r: d(4, 0),
            monthly_loss_r: d(8, 0),
            budget_breach_frac: d(10, 1),
            book_daily_loss_pct: d(15, 1),
            book_monthly_loss_pct: d(5, 0),
        }
    }
}

/// The whole contract.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct OptionsConfig {
    /// `04` §1.
    pub calendar: CalendarConfig,
    /// `04` §2.
    pub chain: ChainConfig,
    /// O1-M.
    pub condor_monthly: CondorConfig,
    /// O1-W.
    pub condor_weekly: CondorConfig,
    /// O2.
    pub directional: DirectionalConfig,
    /// O3.
    pub expiry_setups: ExpirySetupsConfig,
    /// `04` §6.
    pub costs: CostRates,
    /// `04` §7.5.
    pub sizing: SizingConfig,
    /// `04` §8.
    pub execution: ExecutionConfig,
    /// `04` §9.
    pub risk: RiskConfig,
}

impl Default for OptionsConfig {
    fn default() -> Self {
        Self {
            calendar: CalendarConfig::default(),
            chain: ChainConfig::default(),
            condor_monthly: CondorConfig::default(),
            condor_weekly: CondorConfig::weekly(),
            directional: DirectionalConfig::default(),
            expiry_setups: ExpirySetupsConfig::default(),
            costs: CostRates::default(),
            sizing: SizingConfig::default(),
            execution: ExecutionConfig::default(),
            risk: RiskConfig::default(),
        }
    }
}

impl OptionsConfig {
    /// Each sleeve's hard exit.
    pub fn hard_exit_time(&self, sleeve: Sleeve) -> NaiveTime {
        match group_of(sleeve) {
            SleeveGroup::O1m => self.condor_monthly.hard_exit_time,
            SleeveGroup::O1w => self.condor_weekly.hard_exit_time,
            SleeveGroup::O2 => self.directional.hard_exit_time,
            SleeveGroup::O3 => self.expiry_setups.hard_exit_time,
        }
    }

    /// Each sleeve's risk per trade, percent.
    pub fn risk_per_trade_pct(&self, sleeve: Sleeve) -> Decimal {
        match group_of(sleeve) {
            SleeveGroup::O1m => self.condor_monthly.risk_per_trade_pct,
            SleeveGroup::O1w => self.condor_weekly.risk_per_trade_pct,
            SleeveGroup::O2 => self.directional.risk_per_trade_pct,
            SleeveGroup::O3 => self.expiry_setups.risk_per_trade_pct,
        }
    }

    /// Each sleeve's lot cap.
    pub fn max_lots(&self, sleeve: Sleeve) -> i64 {
        match group_of(sleeve) {
            SleeveGroup::O1m => self.condor_monthly.max_lots,
            SleeveGroup::O1w => self.condor_weekly.max_lots,
            SleeveGroup::O2 => self.directional.max_lots,
            SleeveGroup::O3 => self.expiry_setups.max_lots,
        }
    }

    /// Each sleeve's reserve per lot.
    pub fn reserve_per_lot_inr(&self, sleeve: Sleeve) -> Decimal {
        match group_of(sleeve) {
            SleeveGroup::O1m => self.condor_monthly.reserve_per_lot_inr,
            SleeveGroup::O1w => self.condor_weekly.reserve_per_lot_inr,
            SleeveGroup::O2 => self.directional.reserve_per_lot_inr,
            SleeveGroup::O3 => self.expiry_setups.reserve_per_lot_inr,
        }
    }

    /// Each sleeve's largest cost share.
    pub fn cost_share_max(&self, sleeve: Sleeve) -> Decimal {
        match group_of(sleeve) {
            SleeveGroup::O1m => self.condor_monthly.cost_share_max,
            SleeveGroup::O1w => self.condor_weekly.cost_share_max,
            SleeveGroup::O2 => self.directional.cost_share_max,
            SleeveGroup::O3 => self.expiry_setups.cost_share_max,
        }
    }

    /// Each sleeve's tier-3 session count.
    pub fn tier3_min_sessions(&self, sleeve: Sleeve) -> i64 {
        match group_of(sleeve) {
            SleeveGroup::O1m => self.condor_monthly.tier3_min_sessions,
            SleeveGroup::O1w => self.condor_weekly.tier3_min_sessions,
            SleeveGroup::O2 => self.directional.tier3_min_sessions,
            SleeveGroup::O3 => self.expiry_setups.tier3_min_sessions,
        }
    }
}

/// `02` "Ceilings" — the system-only env bounds as the pure core receives them.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct OptionsCeilings {
    /// Largest risk per trade, rupees.
    pub risk_per_trade_inr_max: Decimal,
    /// Largest risk percent.
    pub risk_pct_max: Decimal,
    /// Largest lot count.
    pub max_lots_max: i64,
    /// Largest book daily loss.
    pub book_daily_loss_inr_max: Decimal,
    /// Largest book monthly loss.
    pub book_monthly_loss_inr_max: Decimal,
    /// The latest hard exit.
    pub hard_exit_latest: NaiveTime,
}

impl Default for OptionsCeilings {
    fn default() -> Self {
        Self {
            risk_per_trade_inr_max: d(25000, 0),
            risk_pct_max: d(10, 1),
            max_lots_max: 10,
            book_daily_loss_inr_max: d(30000, 0),
            book_monthly_loss_inr_max: d(75000, 0),
            hard_exit_latest: hm(15, 0),
        }
    }
}

/// Every sleeve whose hard exit is later than `hard_exit_latest`.
pub fn check_hard_exits(config: &OptionsConfig, ceilings: &OptionsCeilings) -> Vec<String> {
    Sleeve::ALL
        .iter()
        .filter(|s| config.hard_exit_time(**s) > ceilings.hard_exit_latest)
        .map(|s| s.value().to_owned())
        .collect()
}
