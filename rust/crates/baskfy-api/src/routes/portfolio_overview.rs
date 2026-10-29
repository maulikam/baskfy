//! ``/portfolio/*`` — the read API behind ``PORTFOLIO_REDESIGN.md`` §6 and §7. Ports the GET
//! routes of `baskfy_api.routers.portfolio_overview` (Python declares
//! `APIRouter(prefix="/portfolio", tags=["portfolio"])`).
//!
//! Ported: `GET /overview`, `GET /holdings`, `GET /activity`, `GET /reconciliation`,
//! `GET /suggestions`, `GET /{portfolio_id}`, `GET /{portfolio_id}/nav`. **Not ported** (they
//! write; Python keeps serving them): `POST `, `POST /{portfolio_id}/holdings`,
//! `POST /reconciliation/{item_id}/resolve` — their paths are registered with no method, so a
//! GET there is the `405` Starlette gives it.
//!
//! **There is no execute route and there will not be one** (§9, law 2): nothing here names a
//! side, a product, a venue, an order type or a broker session. Marks are the close (`close_raw`);
//! Python's live overlay needs a Kite session, and with none (the bed) it is empty there too
//! (DECISIONS-RS, api-portfolio P3). The arithmetic is `baskfy_core`'s, ported beside this router in
//! `routes/portfolio/` and run in Python's `Decimal` context (`pydec`).

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use axum::Router;
use axum::extract::{Extension, Path, RawQuery, State};
use axum::response::Response;
use axum::routing::{get, post};
use chrono::{DateTime, Duration, NaiveDate, Utc};
use serde::Serialize;

use super::portfolios::portfolio::cash::{CashFlowKind, portfolio_cash, portfolio_xirr};
use super::portfolios::portfolio::costs::{ESTIMATE_CAVEAT, TradeFlow, estimate_portfolio_costs};
use super::portfolios::portfolio::db;
use super::portfolios::portfolio::ledger::{
    Holding, LedgerPortfolio, MetricKind, PortfolioKind, PortfolioSource, ReturnFigure,
    consolidated_value, cost_basis, holding_value, model_figure, money, portfolio_values,
    unallocated_holdings,
};
use super::portfolios::portfolio::load::{
    BrokerRow, InstrumentRow, Ledger, NavRow, Position, load_ledger, model_return_values,
    nav_by_portfolio, nav_rows, today_ist,
};
use super::portfolios::portfolio::nav::{
    NavPoint, benchmark_comparison, daily_pnl, drawdown_series, max_drawdown, quantize_return_even,
    since_grouped_figure, twr_figure,
};
use super::portfolios::portfolio::params::{self, IntBounds};
use super::portfolios::portfolio::pydec::Dec;
use super::portfolios::portfolio::recon::{
    AttentionInputs, FreezeReport, InboxEntry, ReconciliationState, attention_items, freeze_report,
};
use super::portfolios::portfolio::suggest::{
    SubscribedBasket, SuggestionBasis, SuggestionInputs, suggest_groupings,
};
use super::portfolios::portfolio::tradebook::{
    TradeFill, fills_by_instrument, history_for, xirr_since_first_purchase,
};
use super::portfolios::portfolio::{fail, pyfmt};
mod writes;

use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, not_found};
use crate::query::{QueryParams, Validation};
use sqlx::PgConnection;

use crate::state::AppState;
use crate::wire;

/// `ORGANIZE_CTA`.
pub const ORGANIZE_CTA: &str = "Organize into portfolios";
/// `MONITORING_NOTE`.
pub const MONITORING_NOTE: &str =
    "Monitoring view — overlaps with other portfolios, excluded from totals.";
const STATUS_PENDING: &str = "Pending reconciliation";
const STATUS_REBALANCE: &str = "Rebalance due";
const STATUS_SYNCED: &str = "Synced";
const WEEK_LABEL: &str = "Change since last week's close";
const MONTH_LABEL: &str = "Change over the last 30 days";
const TODAY_LABEL: &str = "Change since the previous close";
const TOTAL_PNL_LABEL: &str = "Total P&L since purchase";
const ACTIVITY_DEFAULT: i128 = 50;
const ACTIVITY_MAX: i128 = 200;
const NO_SECTORS: &str =
    "We do not have a sector for any of these holdings yet, so we cannot group them by sector.";
const NO_PURCHASE_DATES: &str = "We do not know when these holdings were bought, so we cannot \
group them by purchase year. Importing a CAS statement fills in the buy dates.";
const NO_BASKETS: &str = "You are not following any published model yet, so there is nothing for \
these holdings to overlap with.";
const NOTHING_TO_SORT: &str =
    "Everything you hold is already in a portfolio, so there is nothing here to sort.";
const NO_INPUTS: &str = "We have none of the three things a suggestion needs — no sectors, no \
purchase dates and no models you follow — so there is nothing we can propose yet. Sorting by hand \
still works.";
const EXECUTION_NOTE: &str =
    "Baskfy never places an order. A rebalance produces a plan you take to your broker.";

fn ser_dec<S: serde::Serializer>(v: &Dec, s: S) -> Result<S::Ok, S::Error> {
    s.serialize_str(&v.to_sci_string())
}

fn ser_opt_dec<S: serde::Serializer>(v: &Option<Dec>, s: S) -> Result<S::Ok, S::Error> {
    match v {
        Some(v) => ser_dec(v, s),
        None => s.serialize_none(),
    }
}

/// `NavRange`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum NavRange {
    /// `1M`.
    #[serde(rename = "1M")]
    M1,
    /// `3M`.
    #[serde(rename = "3M")]
    M3,
    /// `1Y`.
    #[serde(rename = "1Y")]
    Y1,
    /// `3Y`.
    #[serde(rename = "3Y")]
    Y3,
    /// `ALL`.
    #[serde(rename = "ALL")]
    All,
}

impl NavRange {
    fn days(self) -> Option<i64> {
        match self {
            Self::M1 => Some(31),
            Self::M3 => Some(92),
            Self::Y1 => Some(366),
            Self::Y3 => Some(1096),
            Self::All => None,
        }
    }

    fn parse(raw: &str) -> Option<Self> {
        match raw {
            "1M" => Some(Self::M1),
            "3M" => Some(Self::M3),
            "1Y" => Some(Self::Y1),
            "3Y" => Some(Self::Y3),
            "ALL" => Some(Self::All),
            _ => None,
        }
    }
}

/// The `range` query parameter (`NavRange`, default `1Y`).
fn range_param(v: &mut Validation, q: &QueryParams) -> NavRange {
    match q.get("range") {
        None => NavRange::Y1,
        Some(raw) => NavRange::parse(raw).unwrap_or_else(|| {
            v.error(
                "range",
                "Input should be '1M', '3M', '1Y', '3Y' or 'ALL'",
                "enum",
            );
            NavRange::Y1
        }),
    }
}

/// `ReturnFigureOut`.
#[derive(Debug, Clone, Serialize)]
pub struct ReturnFigureOut {
    kind: MetricKindOut,
    label: String,
    #[serde(serialize_with = "wire::ser::date")]
    since: NaiveDate,
    #[serde(serialize_with = "ser_opt_dec")]
    value: Option<Dec>,
    is_model: bool,
    unavailable_reason: Option<String>,
}

/// `MetricKind` on the wire.
#[derive(Debug, Clone, Copy, Serialize)]
#[serde(transparent)]
struct MetricKindOut(MetricKind);

fn figure_out(f: &ReturnFigure) -> ReturnFigureOut {
    ReturnFigureOut {
        kind: MetricKindOut(f.kind),
        label: f.label(),
        since: f.since,
        value: f.value.clone(),
        is_model: f.is_model,
        unavailable_reason: f.unavailable_reason.clone(),
    }
}

/// `LabelledRateOut`.
#[derive(Debug, Clone, Serialize)]
pub struct LabelledRateOut {
    label: String,
    #[serde(serialize_with = "wire::ser::opt_date")]
    since: Option<NaiveDate>,
    #[serde(serialize_with = "ser_opt_dec")]
    value: Option<Dec>,
    unavailable_reason: Option<String>,
}

fn rate(
    label: &str,
    since: Option<NaiveDate>,
    value: Option<Dec>,
    reason: Option<&str>,
) -> LabelledRateOut {
    LabelledRateOut {
        label: label.to_owned(),
        since,
        value,
        unavailable_reason: reason.map(str::to_owned),
    }
}

/// `MoneyMoveOut`.
#[derive(Debug, Clone, Serialize)]
pub struct MoneyMoveOut {
    #[serde(serialize_with = "ser_opt_dec")]
    amount: Option<Dec>,
    #[serde(serialize_with = "ser_opt_dec")]
    pct: Option<Dec>,
    label: String,
    #[serde(serialize_with = "wire::ser::opt_date")]
    since: Option<NaiveDate>,
    unavailable_reason: Option<String>,
}

fn money_move(
    amount: Option<Dec>,
    pct: Option<Dec>,
    label: &str,
    since: Option<NaiveDate>,
    reason: Option<&str>,
) -> MoneyMoveOut {
    MoneyMoveOut {
        amount,
        pct,
        label: label.to_owned(),
        since,
        unavailable_reason: reason.map(str::to_owned),
    }
}

/// `BrokerRefOut`.
#[derive(Debug, Clone, Serialize)]
pub struct BrokerRefOut {
    broker_account_id: i64,
    broker_id: String,
    label: String,
}

fn broker_ref(a: &BrokerRow) -> BrokerRefOut {
    BrokerRefOut {
        broker_account_id: a.id,
        broker_id: a.broker_id.clone(),
        label: a.label.clone(),
    }
}

fn unknown_broker(id: i64) -> BrokerRefOut {
    BrokerRefOut {
        broker_account_id: id,
        broker_id: "unknown".to_owned(),
        label: "unknown".to_owned(),
    }
}

/// `PortfolioRefOut`.
#[derive(Debug, Clone, Serialize)]
pub struct PortfolioRefOut {
    portfolio_id: i64,
    name: String,
    kind: PortfolioKindOut,
    source: PortfolioSourceOut,
}

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(transparent)]
struct PortfolioKindOut(PortfolioKind);

#[derive(Debug, Clone, Copy, Serialize)]
#[serde(transparent)]
struct PortfolioSourceOut(PortfolioSource);

fn portfolio_ref(p: &LedgerPortfolio) -> PortfolioRefOut {
    PortfolioRefOut {
        portfolio_id: p.portfolio_id,
        name: p.name.clone(),
        kind: PortfolioKindOut(p.kind),
        source: PortfolioSourceOut(p.source),
    }
}

/// `InstrumentRefOut`.
#[derive(Debug, Clone, Serialize)]
pub struct InstrumentRefOut {
    instrument_id: i64,
    symbol: String,
    name: String,
}

fn instrument_ref(i: &InstrumentRow) -> InstrumentRefOut {
    InstrumentRefOut {
        instrument_id: i.id,
        symbol: i.symbol.clone(),
        name: i.name.clone(),
    }
}

/// `BrokerCashOut`.
#[derive(Debug, Clone, Serialize)]
struct BrokerCashOut {
    broker: BrokerRefOut,
    #[serde(serialize_with = "ser_dec")]
    balance: Dec,
    #[serde(serialize_with = "wire::ser::date")]
    as_of: NaiveDate,
}

/// `AttentionOut`.
#[derive(Debug, Clone, Serialize)]
struct AttentionOut {
    kind: &'static str,
    message: String,
    count: i64,
    subject_ids: Vec<i64>,
    #[serde(serialize_with = "wire::ser::opt_date")]
    since: Option<NaiveDate>,
}

/// `PortfolioRowOut`.
#[derive(Debug, Clone, Serialize)]
struct PortfolioRowOut {
    portfolio_id: i64,
    name: String,
    kind: PortfolioKindOut,
    source: PortfolioSourceOut,
    source_badge: String,
    publisher: Option<String>,
    #[serde(serialize_with = "wire::ser::date")]
    started_on: NaiveDate,
    #[serde(serialize_with = "ser_dec")]
    value: Dec,
    #[serde(serialize_with = "ser_dec")]
    cash: Dec,
    counts_toward_total: bool,
    excluded_note: Option<&'static str>,
    todays_pnl: MoneyMoveOut,
    week_pnl: Option<MoneyMoveOut>,
    month_pnl: Option<MoneyMoveOut>,
    headline_return: ReturnFigureOut,
    model_return: Option<ReturnFigureOut>,
    brokers: Vec<BrokerRefOut>,
    broker_count: usize,
    holdings_count: usize,
    status: &'static str,
    pending_reconciliation: bool,
    benchmark_name: Option<String>,
}

/// `UnallocatedHoldingOut`.
#[derive(Debug, Clone, Serialize)]
struct UnallocatedHoldingOut {
    instrument: InstrumentRefOut,
    #[serde(serialize_with = "ser_dec")]
    quantity: Dec,
    #[serde(serialize_with = "ser_dec")]
    value: Dec,
    brokers: Vec<BrokerRefOut>,
    monitoring_views: Vec<PortfolioRefOut>,
    pending_reconciliation: bool,
}

/// `UnallocatedOut`.
#[derive(Debug, Clone, Serialize)]
struct UnallocatedOut {
    #[serde(serialize_with = "ser_dec")]
    cash: Dec,
    cash_by_broker: Vec<BrokerCashOut>,
    holdings: Vec<UnallocatedHoldingOut>,
    #[serde(serialize_with = "ser_dec")]
    holdings_value: Dec,
    holdings_count: usize,
    #[serde(serialize_with = "ser_dec")]
    total_value: Dec,
    pending_reconciliation: bool,
    cta: &'static str,
}

/// `SyncStatusOut`.
#[derive(Debug, Clone, Serialize)]
struct SyncStatusOut {
    broker: BrokerRefOut,
    #[serde(serialize_with = "wire::ser::opt_date")]
    synced_on: Option<NaiveDate>,
    label: String,
}

/// `HeroSecondaryOut`.
#[derive(Debug, Clone, Serialize)]
struct HeroSecondaryOut {
    #[serde(serialize_with = "ser_dec")]
    cash: Dec,
    #[serde(serialize_with = "ser_dec")]
    dividends: Dec,
    broker_count: usize,
    unrealised_pnl: MoneyMoveOut,
    realised_pnl: MoneyMoveOut,
}

/// `HeroOut`.
#[derive(Debug, Clone, Serialize)]
struct HeroOut {
    #[serde(serialize_with = "ser_dec")]
    current_value: Dec,
    todays_pnl: MoneyMoveOut,
    week_pnl: Option<MoneyMoveOut>,
    month_pnl: Option<MoneyMoveOut>,
    total_pnl: MoneyMoveOut,
    xirr: LabelledRateOut,
    twr: LabelledRateOut,
    #[serde(serialize_with = "ser_opt_dec")]
    invested: Option<Dec>,
    invested_unavailable_reason: Option<&'static str>,
    secondary: HeroSecondaryOut,
    pending_reconciliation: bool,
    holdings_without_cost_basis: usize,
}

/// `NavPointOut`.
#[derive(Debug, Clone, Serialize)]
struct NavPointOut {
    #[serde(serialize_with = "wire::ser::date")]
    on: NaiveDate,
    #[serde(serialize_with = "ser_dec")]
    value: Dec,
    #[serde(serialize_with = "ser_dec")]
    cash: Dec,
    #[serde(serialize_with = "ser_dec")]
    net_flow: Dec,
    pending_reconciliation: bool,
}

/// `DayPnlOut`.
#[derive(Debug, Clone, Serialize)]
struct DayPnlOut {
    #[serde(serialize_with = "wire::ser::date")]
    on: NaiveDate,
    #[serde(serialize_with = "ser_dec")]
    amount: Dec,
    #[serde(serialize_with = "ser_opt_dec")]
    pct: Option<Dec>,
}

/// `DrawdownPointOut`.
#[derive(Debug, Clone, Serialize)]
struct DrawdownPointOut {
    #[serde(serialize_with = "wire::ser::date")]
    on: NaiveDate,
    #[serde(serialize_with = "ser_dec")]
    index: Dec,
    #[serde(serialize_with = "ser_dec")]
    peak: Dec,
    #[serde(serialize_with = "ser_dec")]
    drawdown: Dec,
}

/// `MaxDrawdownOut`.
#[derive(Debug, Clone, Serialize)]
struct MaxDrawdownOut {
    #[serde(serialize_with = "wire::ser::date")]
    peak_on: NaiveDate,
    #[serde(serialize_with = "wire::ser::date")]
    trough_on: NaiveDate,
    #[serde(serialize_with = "ser_dec")]
    drawdown: Dec,
}

/// `BenchmarkOut`.
#[derive(Debug, Clone, Serialize)]
struct BenchmarkOut {
    name: String,
    portfolio: ReturnFigureOut,
    benchmark: ReturnFigureOut,
    #[serde(serialize_with = "ser_opt_dec")]
    difference: Option<Dec>,
    points: Vec<NavPointOut>,
}

/// `NavSeriesOut`.
#[derive(Debug, Clone, Serialize)]
struct NavSeriesOut {
    portfolio_id: Option<i64>,
    range: NavRange,
    #[serde(serialize_with = "wire::ser::opt_date")]
    from_on: Option<NaiveDate>,
    #[serde(serialize_with = "wire::ser::opt_date")]
    to_on: Option<NaiveDate>,
    points: Vec<NavPointOut>,
    daily_pnl: Vec<DayPnlOut>,
    drawdown: Vec<DrawdownPointOut>,
    max_drawdown: Option<MaxDrawdownOut>,
    total_return: LabelledRateOut,
    benchmark: Option<BenchmarkOut>,
    pending_reconciliation: bool,
}

/// `EstimatedCostsOut`.
#[derive(Debug, Clone, Serialize)]
struct EstimatedCostsOut {
    #[serde(serialize_with = "ser_dec")]
    stt: Dec,
    #[serde(serialize_with = "ser_dec")]
    exchange: Dec,
    #[serde(serialize_with = "ser_dec")]
    sebi: Dec,
    #[serde(serialize_with = "ser_dec")]
    stamp: Dec,
    #[serde(serialize_with = "ser_dec")]
    gst: Dec,
    #[serde(serialize_with = "ser_dec")]
    dp: Dec,
    #[serde(serialize_with = "ser_dec")]
    brokerage: Dec,
    #[serde(serialize_with = "ser_dec")]
    total: Dec,
    #[serde(serialize_with = "ser_dec")]
    turnover: Dec,
    trades: usize,
    sell_scrip_days: usize,
    #[serde(serialize_with = "ser_opt_dec")]
    bps_of_turnover: Option<Dec>,
    caveat: &'static str,
}

/// `OverviewOut`.
#[derive(Debug, Clone, Serialize)]
struct OverviewOut {
    #[serde(serialize_with = "wire::ser::opt_date")]
    prices_as_of: Option<NaiveDate>,
    prices_label: String,
    #[serde(serialize_with = "wire::ser::opt_date")]
    holdings_synced_on: Option<NaiveDate>,
    holdings_synced_label: String,
    sync_status: Vec<SyncStatusOut>,
    sync_summary: String,
    live_overlay: bool,
    hero: HeroOut,
    chart: NavSeriesOut,
    attention: Vec<AttentionOut>,
    portfolios: Vec<PortfolioRowOut>,
    monitoring_views: Vec<PortfolioRowOut>,
    unallocated: UnallocatedOut,
    estimated_costs: Option<EstimatedCostsOut>,
    open_reconciliation_count: usize,
    monitoring_excluded_note: &'static str,
}

/// `HoldingSliceOut`.
#[derive(Debug, Clone, Serialize)]
struct HoldingSliceOut {
    portfolio: PortfolioRefOut,
    #[serde(serialize_with = "ser_dec")]
    quantity: Dec,
}

/// `HoldingBrokerLineOut`.
#[derive(Debug, Clone, Serialize)]
struct HoldingBrokerLineOut {
    broker: BrokerRefOut,
    #[serde(serialize_with = "ser_dec")]
    quantity: Dec,
    #[serde(serialize_with = "ser_dec")]
    unallocated_quantity: Dec,
    allocations: Vec<HoldingSliceOut>,
    #[serde(serialize_with = "ser_opt_dec")]
    price: Option<Dec>,
    #[serde(serialize_with = "ser_opt_dec")]
    value: Option<Dec>,
    #[serde(serialize_with = "ser_opt_dec")]
    avg_price: Option<Dec>,
    #[serde(serialize_with = "ser_opt_dec")]
    cost_basis: Option<Dec>,
    allocation: Option<PortfolioRefOut>,
    monitoring_views: Vec<PortfolioRefOut>,
    #[serde(serialize_with = "wire::ser::opt_date")]
    first_bought_on: Option<NaiveDate>,
    history_source: String,
    pending_reconciliation: bool,
}

/// `AggregatedHoldingOut`.
#[derive(Debug, Clone, Serialize)]
struct AggregatedHoldingOut {
    instrument: InstrumentRefOut,
    #[serde(serialize_with = "ser_dec")]
    quantity: Dec,
    #[serde(serialize_with = "ser_opt_dec")]
    price: Option<Dec>,
    #[serde(serialize_with = "wire::ser::opt_date")]
    price_as_of: Option<NaiveDate>,
    #[serde(serialize_with = "ser_opt_dec")]
    value: Option<Dec>,
    allocation: Option<PortfolioRefOut>,
    allocated: bool,
    split_across_portfolios: bool,
    monitoring_views: Vec<PortfolioRefOut>,
    brokers: Vec<HoldingBrokerLineOut>,
    pending_reconciliation: bool,
    #[serde(serialize_with = "ser_opt_dec")]
    invested: Option<Dec>,
    #[serde(serialize_with = "ser_opt_dec")]
    avg_price: Option<Dec>,
    total_pnl: Option<MoneyMoveOut>,
    todays_pnl: Option<MoneyMoveOut>,
    week_pnl: Option<MoneyMoveOut>,
    month_pnl: Option<MoneyMoveOut>,
}

/// `HoldingsOut`.
#[derive(Debug, Clone, Serialize)]
struct HoldingsOut {
    #[serde(serialize_with = "wire::ser::opt_date")]
    prices_as_of: Option<NaiveDate>,
    prices_label: String,
    #[serde(serialize_with = "wire::ser::opt_date")]
    holdings_synced_on: Option<NaiveDate>,
    holdings_synced_label: String,
    rows: Vec<AggregatedHoldingOut>,
    #[serde(serialize_with = "ser_dec")]
    total_value: Dec,
    unallocated_count: usize,
    unpriced_instrument_ids: Vec<i64>,
    live_overlay: bool,
}

/// `DetailHoldingOut`.
#[derive(Debug, Clone, Serialize)]
struct DetailHoldingOut {
    instrument: InstrumentRefOut,
    broker: BrokerRefOut,
    #[serde(serialize_with = "ser_dec")]
    quantity: Dec,
    #[serde(serialize_with = "ser_opt_dec")]
    avg_price: Option<Dec>,
    #[serde(serialize_with = "ser_opt_dec")]
    price: Option<Dec>,
    #[serde(serialize_with = "ser_opt_dec")]
    value: Option<Dec>,
    #[serde(serialize_with = "ser_opt_dec")]
    weight: Option<Dec>,
    #[serde(serialize_with = "ser_opt_dec")]
    todays_contribution: Option<Dec>,
    #[serde(serialize_with = "ser_opt_dec")]
    total_contribution: Option<Dec>,
    #[serde(serialize_with = "wire::ser::opt_date")]
    first_bought_on: Option<NaiveDate>,
    history_source: String,
    pending_reconciliation: bool,
}

/// `SourcePanelOut`.
#[derive(Debug, Clone, Serialize)]
struct SourcePanelOut {
    source: PortfolioSourceOut,
    headline: String,
    publisher: Option<String>,
    basket_slug: Option<String>,
    basket_name: Option<String>,
    screen_public_id: Option<String>,
    screen_name: Option<String>,
    brokers: Vec<BrokerRefOut>,
    #[serde(serialize_with = "wire::ser::opt_date")]
    grouped_on: Option<NaiveDate>,
    execution_note: &'static str,
}

/// `PortfolioSummaryOut` (this router's).
#[derive(Debug, Clone, Serialize)]
struct PortfolioSummaryOut {
    portfolio_id: i64,
    name: String,
    kind: PortfolioKindOut,
    source: PortfolioSourceOut,
    source_badge: String,
    publisher: Option<String>,
    #[serde(serialize_with = "wire::ser::date")]
    started_on: NaiveDate,
    #[serde(serialize_with = "ser_dec")]
    value: Dec,
    #[serde(serialize_with = "ser_dec")]
    cash: Dec,
    #[serde(serialize_with = "ser_opt_dec")]
    invested: Option<Dec>,
    invested_unavailable_reason: Option<&'static str>,
    todays_pnl: MoneyMoveOut,
    week_pnl: Option<MoneyMoveOut>,
    month_pnl: Option<MoneyMoveOut>,
    total_pnl: MoneyMoveOut,
    headline_return: ReturnFigureOut,
    model_return: Option<ReturnFigureOut>,
    xirr: LabelledRateOut,
    benchmark: Option<BenchmarkOut>,
    counts_toward_total: bool,
    excluded_note: Option<&'static str>,
    status: &'static str,
    pending_reconciliation: bool,
    #[serde(serialize_with = "wire::ser::opt_date")]
    holdings_synced_on: Option<NaiveDate>,
    #[serde(serialize_with = "wire::ser::opt_date")]
    prices_as_of: Option<NaiveDate>,
}

/// `PortfolioDetailOut` (this router's).
#[derive(Debug, Clone, Serialize)]
struct PortfolioDetailOut {
    summary: PortfolioSummaryOut,
    holdings: Vec<DetailHoldingOut>,
    source_panel: SourcePanelOut,
    brokers: Vec<BrokerRefOut>,
}

/// `ActivityItemOut`.
#[derive(Debug, Clone, Serialize)]
struct ActivityItemOut {
    kind: &'static str,
    #[serde(serialize_with = "wire::ser::date")]
    on: NaiveDate,
    description: String,
    portfolio: Option<PortfolioRefOut>,
    instrument: Option<InstrumentRefOut>,
    broker: Option<BrokerRefOut>,
    #[serde(serialize_with = "ser_opt_dec")]
    quantity: Option<Dec>,
    #[serde(serialize_with = "ser_opt_dec")]
    amount: Option<Dec>,
    is_pnl_event: bool,
    reconciliation_item_id: Option<i64>,
    reconciliation_state: Option<ReconciliationState>,
}

/// `ActivityOut`.
#[derive(Debug, Clone, Serialize)]
struct ActivityOut {
    items: Vec<ActivityItemOut>,
    total: usize,
}

/// `ReconciliationItemOut`.
#[derive(Debug, Clone, Serialize)]
struct ReconciliationItemOut {
    item_id: i64,
    state: ReconciliationState,
    reason: &'static str,
    question: &'static str,
    instrument: InstrumentRefOut,
    broker: BrokerRefOut,
    #[serde(serialize_with = "ser_dec")]
    quantity: Dec,
    #[serde(serialize_with = "wire::ser::date")]
    detected_on: NaiveDate,
    suggested_portfolio: Option<PortfolioRefOut>,
    resolved_portfolio: Option<PortfolioRefOut>,
    #[serde(serialize_with = "wire::ser::opt_datetime")]
    resolved_at: Option<DateTime<Utc>>,
    freezes: bool,
}

/// `ReconciliationInboxOut`.
#[derive(Debug, Clone, Serialize)]
struct ReconciliationInboxOut {
    items: Vec<ReconciliationItemOut>,
    open_count: usize,
    pending_portfolio_ids: Vec<i64>,
    unallocated_pending: bool,
    consolidated_pending: bool,
    pending_status_label: &'static str,
}

/// `HoldingKeyOut`.
#[derive(Debug, Clone, Serialize)]
struct HoldingKeyOut {
    instrument_id: i64,
    broker_account_id: i64,
}

/// `GroupingSuggestionOut`.
#[derive(Debug, Clone, Serialize)]
struct GroupingSuggestionOut {
    basis: SuggestionBasis,
    proposed_name: String,
    keys: Vec<HoldingKeyOut>,
    #[serde(serialize_with = "ser_dec")]
    value: Dec,
    rationale: String,
    suggested_kind: PortfolioKindOut,
    basket_id: Option<i64>,
    #[serde(serialize_with = "ser_opt_dec")]
    basket_coverage: Option<Dec>,
    missing_instrument_ids: Vec<i64>,
}

/// `SuggestionBasisStatusOut`.
#[derive(Debug, Clone, Serialize)]
struct SuggestionBasisStatusOut {
    basis: SuggestionBasis,
    available: bool,
    considered: usize,
    unavailable_reason: Option<&'static str>,
}

/// `SuggestionsOut`.
#[derive(Debug, Clone, Serialize)]
struct SuggestionsOut {
    suggestions: Vec<GroupingSuggestionOut>,
    sectors: baskfy_db::OrderedJson,
    unallocated_count: usize,
    #[serde(serialize_with = "ser_dec")]
    unallocated_value: Dec,
    bases: Vec<SuggestionBasisStatusOut>,
    unavailable_reason: Option<&'static str>,
    unpriced_instrument_ids: Vec<i64>,
    cta: &'static str,
}

// ---------------------------------------------------------------------------------------------
// Helpers (the module's private functions, one for one)
// ---------------------------------------------------------------------------------------------

fn dfail(e: String) -> Problem {
    fail(&e)
}

fn label_for_prices(as_of: Option<NaiveDate>, live_overlay: bool) -> String {
    match as_of {
        None if live_overlay => "Live marks".to_owned(),
        None => "No closing prices yet".to_owned(),
        Some(d) if live_overlay => format!("Prices: live, over close of {}", wire::py_date(d)),
        Some(d) => format!("Prices: close of {}", wire::py_date(d)),
    }
}

fn label_for_sync(on: Option<NaiveDate>) -> String {
    match on {
        None => "Holdings not synced yet".to_owned(),
        Some(d) => format!("Holdings synced: {}", wire::py_date(d)),
    }
}

fn synced_on_by_account(ledger: &Ledger) -> HashMap<i64, NaiveDate> {
    let ist = super::portfolios::portfolio::load::ist();
    let mut dated = HashMap::new();
    for (id, account) in &ledger.brokers {
        if let Some(t) = account.holdings_synced_at {
            dated.insert(*id, t.with_timezone(&ist).date_naive());
        }
    }
    for row in &ledger.broker_cash {
        let current = dated
            .get(&row.broker_account_id)
            .copied()
            .unwrap_or(row.as_of);
        dated.insert(row.broker_account_id, current.max(row.as_of));
    }
    dated
}

fn latest_sync(ledger: &Ledger) -> Option<NaiveDate> {
    synced_on_by_account(ledger).values().max().copied()
}

fn sync_status(ledger: &Ledger) -> Vec<SyncStatusOut> {
    let dated = synced_on_by_account(ledger);
    ledger
        .brokers
        .iter()
        .map(|(id, account)| SyncStatusOut {
            broker: broker_ref(account),
            synced_on: dated.get(id).copied(),
            label: label_for_sync(dated.get(id).copied()),
        })
        .collect()
}

fn sync_summary(statuses: &[SyncStatusOut]) -> String {
    if statuses.is_empty() {
        return "No broker connected".to_owned();
    }
    let never: Vec<&SyncStatusOut> = statuses.iter().filter(|s| s.synced_on.is_none()).collect();
    if never.len() == statuses.len() {
        return "Holdings not synced yet".to_owned();
    }
    if !never.is_empty() {
        let names: Vec<&str> = never.iter().map(|s| s.broker.label.as_str()).collect();
        let names = names.join(", ");
        return if never.len() == 1 {
            format!("{names} has never synced")
        } else {
            format!("Never synced: {names}")
        };
    }
    label_for_sync(statuses.iter().filter_map(|s| s.synced_on).max())
}

fn source_badge(portfolio: &LedgerPortfolio, publisher: Option<&String>) -> String {
    match portfolio.source {
        PortfolioSource::Subscribed => format!(
            "Subscribed model by {}",
            publisher.map_or("an unnamed publisher", String::as_str)
        ),
        PortfolioSource::MyScreen => "My screen".to_owned(),
        PortfolioSource::MyStrategy => "My strategy".to_owned(),
        PortfolioSource::HoldingGroup => "Holding group".to_owned(),
    }
}

fn nav_point(row: &NavRow) -> Result<NavPoint, Problem> {
    NavPoint::new(
        row.date,
        row.market_value.add(&row.cash),
        row.net_flow.clone(),
    )
    .map_err(dfail)
}

fn real_nav_points(rows: &[NavRow]) -> Result<Vec<NavPoint>, Problem> {
    let one = Dec::int(1);
    let mut out = Vec::new();
    for row in rows {
        if row.market_value.add(&row.cash) > one {
            out.push(nav_point(row)?);
        }
    }
    Ok(out)
}

fn real_points(points: &[NavPoint]) -> Vec<NavPoint> {
    let one = Dec::int(1);
    points.iter().filter(|p| p.value > one).cloned().collect()
}

/// `_chain_linked`: TWR from `daily_pnl`'s own moves.
fn chain_linked(points: &[NavPoint]) -> Result<Option<Dec>, Problem> {
    let moves = daily_pnl(points).map_err(dfail)?;
    if moves.is_empty() {
        return Ok(None);
    }
    let one = Dec::int(1);
    let mut product = Dec::int(1);
    for m in &moves {
        let pct = m.pct.clone().unwrap_or_else(|| Dec::lit("0"));
        product = product.mul(&one.add(&pct));
    }
    Ok(Some(quantize_return_even(&product.sub(&one))))
}

fn todays_move(ledger: &Ledger, positions: &[&Position], label: &str) -> MoneyMoveOut {
    let mut mv = Dec::lit("0");
    let mut base = Dec::lit("0");
    let mut measured = false;
    for p in positions {
        let id = p.key.instrument_id;
        let (Some(latest), Some(previous)) = (
            ledger.prices.latest.get(&id),
            ledger.prices.previous.get(&id),
        ) else {
            continue;
        };
        measured = true;
        let q = &p.holding.quantity;
        mv = mv.add(&q.mul(&latest.sub(previous)));
        base = base.add(&q.mul(previous));
    }
    if !measured {
        return money_move(
            None,
            None,
            label,
            None,
            Some("No previous close to compare against yet"),
        );
    }
    let amount = money(&mv);
    let pct = if base.is_zero() {
        None
    } else {
        amount.div(&base).map(|r| quantize_return_even(&r))
    };
    money_move(Some(amount), pct, label, None, None)
}

fn move_since(
    ledger: &Ledger,
    positions: &[&Position],
    base_map: &HashMap<i64, Dec>,
    since: Option<NaiveDate>,
    label: &str,
    reason: &str,
) -> MoneyMoveOut {
    let mut mv = Dec::lit("0");
    let mut total_base = Dec::lit("0");
    let mut measured = false;
    for p in positions {
        let id = p.key.instrument_id;
        let (Some(latest), Some(start)) = (ledger.prices.latest.get(&id), base_map.get(&id)) else {
            continue;
        };
        measured = true;
        let q = &p.holding.quantity;
        mv = mv.add(&q.mul(&latest.sub(start)));
        total_base = total_base.add(&q.mul(start));
    }
    if !measured {
        return money_move(None, None, label, since, Some(reason));
    }
    let amount = money(&mv);
    let pct = if total_base.is_zero() {
        None
    } else {
        amount.div(&total_base).map(|r| quantize_return_even(&r))
    };
    money_move(Some(amount), pct, label, since, None)
}

fn week_move(ledger: &Ledger, positions: &[&Position]) -> MoneyMoveOut {
    move_since(
        ledger,
        positions,
        &ledger.prices.week_base,
        ledger.prices.week_since,
        WEEK_LABEL,
        "No close from last week to compare against yet",
    )
}

fn month_move(ledger: &Ledger, positions: &[&Position]) -> MoneyMoveOut {
    move_since(
        ledger,
        positions,
        &ledger.prices.month_base,
        ledger.prices.month_since,
        MONTH_LABEL,
        "No close from 30 days ago to compare against yet",
    )
}

fn holding_total_pnl(invested: Option<&Dec>, value: Option<&Dec>) -> MoneyMoveOut {
    let Some(invested) = invested else {
        return money_move(
            None,
            None,
            TOTAL_PNL_LABEL,
            None,
            Some("No purchase price on record for this holding"),
        );
    };
    let Some(value) = value else {
        return money_move(None, None, TOTAL_PNL_LABEL, None, Some("No price yet"));
    };
    let diff = value.sub(invested);
    let pct = if invested.is_zero() {
        None
    } else {
        diff.div(invested).map(|r| quantize_return_even(&r))
    };
    money_move(Some(money(&diff)), pct, TOTAL_PNL_LABEL, None, None)
}

/// `_cost_and_value`: (invested, value of the positions that know their cost, how many do not).
fn cost_and_value(
    ledger: &Ledger,
    positions: &[&Position],
) -> Result<(Option<Dec>, Option<Dec>, usize), Problem> {
    let mut invested = Dec::lit("0");
    let mut valued = Dec::lit("0");
    let mut unknown = 0;
    let mut known = false;
    for p in positions {
        let basis = cost_basis(&p.holding);
        let value = ledger.value_of(&p.holding)?;
        let (Some(basis), Some(value)) = (basis, value) else {
            unknown += 1;
            continue;
        };
        known = true;
        invested = invested.add(&basis);
        valued = valued.add(&value);
    }
    if !known {
        return Ok((None, None, unknown));
    }
    Ok((Some(money(&invested)), Some(money(&valued)), unknown))
}

fn total_pnl_out(
    invested: Option<&Dec>,
    invested_value: Option<&Dec>,
    reason: &str,
) -> MoneyMoveOut {
    match (invested, invested_value) {
        (Some(invested), Some(value)) => {
            let diff = value.sub(invested);
            let pct = if invested.is_zero() {
                None
            } else {
                diff.div(invested).map(|r| quantize_return_even(&r))
            };
            money_move(Some(money(&diff)), pct, TOTAL_PNL_LABEL, None, None)
        }
        _ => money_move(None, None, TOTAL_PNL_LABEL, None, Some(reason)),
    }
}

fn status_for(pending: bool, rebalance_due: bool) -> &'static str {
    if pending {
        STATUS_PENDING
    } else if rebalance_due {
        STATUS_REBALANCE
    } else {
        STATUS_SYNCED
    }
}

fn headline_for(portfolio: &LedgerPortfolio, points: &[NavPoint]) -> Result<ReturnFigure, Problem> {
    let real = real_points(points);
    if portfolio.source == PortfolioSource::HoldingGroup {
        since_grouped_figure(portfolio, &real).map_err(dfail)
    } else {
        twr_figure(portfolio, &real).map_err(dfail)
    }
}

fn model_for(
    portfolio: &LedgerPortfolio,
    value: Option<Dec>,
) -> Result<Option<ReturnFigureOut>, Problem> {
    if portfolio.source != PortfolioSource::Subscribed {
        return Ok(None);
    }
    Ok(Some(figure_out(
        &model_figure(portfolio, value).map_err(dfail)?,
    )))
}

fn windowed(rows: &[NavRow], window: NavRange) -> Vec<NavRow> {
    let (Some(days), Some(last)) = (window.days(), rows.last()) else {
        return rows.to_vec();
    };
    let cutoff = last.date - Duration::days(days);
    rows.iter().filter(|r| r.date >= cutoff).cloned().collect()
}

async fn benchmark(
    conn: &mut PgConnection,
    index: Option<(i64, &String)>,
    figure: &ReturnFigure,
    rows: &[NavRow],
) -> Result<Option<BenchmarkOut>, Problem> {
    let (Some((index_id, index_name)), Some(first), Some(last), Some(value)) =
        (index, rows.first(), rows.last(), figure.value.as_ref())
    else {
        return Ok(None);
    };
    let levels = sqlx::query(
        "SELECT date, level FROM index_snapshot_daily WHERE index_id = $1 AND date >= $2 \
         AND date <= $3 AND level IS NOT NULL ORDER BY date",
    )
    .bind(i16::try_from(index_id).unwrap_or(i16::MAX))
    .bind(first.date)
    .bind(last.date)
    .fetch_all(&mut *conn)
    .await
    .map_err(db::fail)?;
    if levels.len() < 2 {
        return Ok(None);
    }
    let mut points = Vec::new();
    for row in &levels {
        if let Some(level) = db::opt_pdec(row, "level")? {
            points
                .push(NavPoint::new(db::date(row, "date")?, level, Dec::lit("0")).map_err(dfail)?);
        }
    }
    if points.first().is_some_and(|p| p.on < figure.since) {
        points.retain(|p| p.on >= figure.since);
    }
    if points.len() < 2 {
        return Ok(None);
    }
    let plain = ReturnFigure::new(figure.kind, figure.since, Some(value.clone()), false, None)
        .map_err(dfail)?;
    let comparison = benchmark_comparison(&plain, &points, index_name).map_err(dfail)?;
    Ok(Some(BenchmarkOut {
        name: comparison.name,
        portfolio: figure_out(&comparison.portfolio),
        benchmark: figure_out(&comparison.benchmark),
        difference: comparison.difference,
        points: points
            .iter()
            .map(|p| NavPointOut {
                on: p.on,
                value: p.value.clone(),
                cash: Dec::lit("0"),
                net_flow: Dec::lit("0"),
                pending_reconciliation: false,
            })
            .collect(),
    }))
}

struct SeriesMeta {
    portfolio_id: Option<i64>,
    window: NavRange,
    label: String,
    since: Option<NaiveDate>,
    benchmark: Option<BenchmarkOut>,
}

fn series_out(rows: &[NavRow], meta: SeriesMeta) -> Result<NavSeriesOut, Problem> {
    let real = real_nav_points(rows)?;
    let drawdown = if real.len() >= 2 {
        drawdown_series(&real).map_err(dfail)?
    } else {
        Vec::new()
    };
    let worst = if real.len() >= 2 {
        max_drawdown(&real).map_err(dfail)?
    } else {
        None
    };
    let total = chain_linked(&real)?;
    let moves = daily_pnl(&real).map_err(dfail)?;
    Ok(NavSeriesOut {
        portfolio_id: meta.portfolio_id,
        range: meta.window,
        from_on: rows.first().map(|r| r.date),
        to_on: rows.last().map(|r| r.date),
        points: rows
            .iter()
            .map(|r| NavPointOut {
                on: r.date,
                value: r.market_value.add(&r.cash),
                cash: r.cash.clone(),
                net_flow: r.net_flow.clone(),
                pending_reconciliation: r.pending_reconciliation,
            })
            .collect(),
        daily_pnl: moves
            .into_iter()
            .map(|m| DayPnlOut {
                on: m.on,
                amount: m.amount,
                pct: m.pct,
            })
            .collect(),
        drawdown: drawdown
            .into_iter()
            .map(|d| DrawdownPointOut {
                on: d.on,
                index: d.index,
                peak: d.peak,
                drawdown: d.drawdown,
            })
            .collect(),
        max_drawdown: worst.map(|w| MaxDrawdownOut {
            peak_on: w.peak_on,
            trough_on: w.trough_on,
            drawdown: w.drawdown,
        }),
        total_return: LabelledRateOut {
            label: meta.label,
            since: real.first().map(|p| p.on).or(meta.since),
            unavailable_reason: total
                .is_none()
                .then(|| "Not enough end-of-day valuations yet".to_owned()),
            value: total,
        },
        benchmark: meta.benchmark,
        pending_reconciliation: rows.iter().any(|r| r.pending_reconciliation),
    })
}

/// `_owned_portfolio`: the caller's portfolio row (its benchmark), or `No portfolio with id '7'.`
async fn owned_portfolio(
    conn: &mut PgConnection,
    portfolio_id: params::PyInt,
    user_id: i64,
) -> Result<Option<i64>, Problem> {
    let id = params::bigint(portfolio_id)?;
    let row = sqlx::query(
        "SELECT benchmark_index_id::bigint AS benchmark_index_id FROM portfolio \
         WHERE id = $1 AND user_id = $2",
    )
    .bind(id)
    .bind(user_id)
    .fetch_optional(&mut *conn)
    .await
    .map_err(db::fail)?
    .ok_or_else(|| not_found("portfolio", &portfolio_id.to_string()))?;
    db::get(&row, "benchmark_index_id")
}

fn members_of<'a>(ledger: &'a Ledger, portfolio: &LedgerPortfolio) -> Vec<&'a Position> {
    let id = portfolio.portfolio_id;
    ledger
        .positions
        .iter()
        .filter(|p| match portfolio.kind {
            PortfolioKind::Capital => p.has_slice(id),
            PortfolioKind::Monitoring => p.monitoring_portfolio_ids.contains(&id),
        })
        .collect()
}

fn monitoring_value(ledger: &Ledger, members: &[&Position]) -> Result<Dec, Problem> {
    let mut total = Dec::lit("0");
    for p in members {
        if let Some(v) = ledger.value_of(&p.holding)? {
            total = total.add(&v);
        }
    }
    Ok(money(&total))
}

fn benchmark_name(ledger: &Ledger, portfolio_id: i64) -> Option<String> {
    let id = ledger.rows.get(&portfolio_id)?.benchmark_index_id?;
    ledger.benchmarks.get(&id).cloned()
}

fn priced<'a>(ledger: &Ledger, positions: &[&'a Position]) -> Vec<&'a Position> {
    positions
        .iter()
        .copied()
        .filter(|p| !ledger.prices.is_unpriced(p.key.instrument_id))
        .collect()
}

/// `_valued_on`.
fn valued_on(ledger: &Ledger, last_event: NaiveDate) -> NaiveDate {
    match ledger.prices.as_of {
        Some(p) if p >= last_event => p,
        _ => last_event,
    }
}

/// `_trade_xirr`: a money-weighted return from recorded trades, when no cash was ever assigned.
fn trade_xirr(ledger: &Ledger) -> Result<Option<LabelledRateOut>, Problem> {
    if ledger.trades.is_empty() {
        return Ok(None);
    }
    let mut held: HashMap<i64, Dec> = HashMap::new();
    let mut value: HashMap<i64, Option<Dec>> = HashMap::new();
    let mut held_order: Vec<i64> = Vec::new();
    for p in &ledger.positions {
        let id = p.key.instrument_id;
        if !held.contains_key(&id) {
            held_order.push(id);
        }
        let h = held.get(&id).cloned().unwrap_or_else(|| Dec::lit("0"));
        held.insert(id, h.add(&p.holding.quantity));
        let priced_value = ledger.value_of(&p.holding)?;
        let previous = value
            .get(&id)
            .cloned()
            .unwrap_or_else(|| Some(Dec::lit("0")));
        let next = match (priced_value, previous) {
            (Some(v), Some(prev)) => Some(prev.add(&v)),
            _ => None,
        };
        value.insert(id, next);
    }
    let zero = Dec::lit("0");
    let mut fills: Vec<TradeFill> = Vec::new();
    let mut closing = Dec::lit("0");
    let mut covered = 0usize;
    for (id, trades) in &ledger.trades {
        let quantity = held.get(id).cloned().unwrap_or_else(|| zero.clone());
        if !history_for(trades, &quantity).map_err(dfail)?.reconciles {
            continue;
        }
        if quantity > zero {
            let Some(Some(worth)) = value.get(id) else {
                continue;
            };
            closing = closing.add(worth);
            covered += 1;
        }
        fills.extend(trades.iter().cloned());
    }
    let held_count = held.values().filter(|q| **q > zero).count();
    let label =
        format!("XIRR since your first recorded purchase ({covered} of {held_count} holdings)");
    if fills.is_empty() {
        return Ok(Some(rate(
            &label,
            None,
            None,
            Some(
                "Your recorded trades do not add up to any holding yet — a tradebook that starts \
                 after a position was opened, or a bonus or split, leaves it out",
            ),
        )));
    }
    let since = fills.iter().map(|f| f.trade_date).min().unwrap_or_default();
    let as_of = fills
        .iter()
        .map(|f| f.trade_date)
        .chain(std::iter::once(ledger.prices.as_of.unwrap_or(since)))
        .max()
        .unwrap_or(since);
    let value = xirr_since_first_purchase(&fills, &closing, as_of).map_err(dfail)?;
    let reason = value
        .is_none()
        .then_some("These trades do not solve to a rate yet");
    Ok(Some(rate(&label, Some(since), value, reason)))
}

/// `_consolidated_xirr`.
fn consolidated_xirr(
    ledger: &Ledger,
    closing_value: &Dec,
    from_trades: Option<LabelledRateOut>,
) -> Result<LabelledRateOut, Problem> {
    let label = "XIRR since your first cash assignment";
    let events: Vec<_> = ledger
        .flows
        .iter()
        .filter(|f| f.kind.is_xirr_event())
        .collect();
    if events.is_empty() {
        let from_trades = match from_trades {
            Some(t) => Some(t),
            None => trade_xirr(ledger)?,
        };
        if let Some(t) = from_trades {
            return Ok(t);
        }
        return Ok(rate(
            label,
            None,
            None,
            Some(
                "Assign cash to a portfolio, or import your Zerodha tradebook, to start measuring \
                 XIRR",
            ),
        ));
    }
    let since = events
        .iter()
        .map(|f| f.occurred_on)
        .min()
        .unwrap_or_default();
    let last_event = events
        .iter()
        .map(|f| f.occurred_on)
        .max()
        .unwrap_or_default();
    let as_of = valued_on(ledger, last_event);
    let synthetic = -1;
    let combined: Vec<_> = events
        .iter()
        .map(|f| {
            let mut c = (*f).clone();
            c.portfolio_id = Some(synthetic);
            c
        })
        .collect();
    let value = portfolio_xirr(&combined, synthetic, as_of, closing_value).map_err(dfail)?;
    let reason = value
        .is_none()
        .then_some("Not enough cash-flow history to solve a rate yet");
    Ok(rate(label, Some(since), value, reason))
}

fn consolidated_twr(rows: &[NavRow]) -> Result<LabelledRateOut, Problem> {
    let points = real_nav_points(rows)?;
    let value = chain_linked(&points)?;
    let reason = value
        .is_none()
        .then_some("Not enough end-of-day valuations yet");
    Ok(rate(
        "Time-weighted return since your first valuation",
        points.first().map(|p| p.on),
        value,
        reason,
    ))
}

fn unallocated_out(
    ledger: &Ledger,
    cash: &Dec,
    freeze: &FreezeReport,
) -> Result<UnallocatedOut, Problem> {
    let loose_keys: HashSet<_> = unallocated_holdings(&ledger.holdings(), &ledger.allocations())
        .map_err(dfail)?
        .into_iter()
        .map(|h| h.key)
        .collect();
    let mut by_instrument: BTreeMap<i64, Vec<&Position>> = BTreeMap::new();
    for p in &ledger.positions {
        if loose_keys.contains(&p.key) {
            by_instrument
                .entry(p.key.instrument_id)
                .or_default()
                .push(p);
        }
    }
    let mut rows = Vec::new();
    let mut total = Dec::lit("0");
    for (id, group) in &by_instrument {
        let Some(instrument) = ledger.instruments.get(id) else {
            continue;
        };
        let quantity = group
            .iter()
            .fold(Dec::lit("0"), |acc, p| acc.add(&p.holding.quantity));
        let mut value = Dec::lit("0");
        for p in group {
            if let Some(v) = ledger.value_of(&p.holding)? {
                value = value.add(&v);
            }
        }
        total = total.add(&value);
        rows.push(UnallocatedHoldingOut {
            instrument: instrument_ref(instrument),
            quantity,
            value: money(&value),
            brokers: group
                .iter()
                .filter_map(|p| ledger.brokers.get(&p.key.broker_account_id).map(broker_ref))
                .collect(),
            monitoring_views: group
                .iter()
                .flat_map(|p| p.monitoring_portfolio_ids.iter())
                .filter_map(|v| ledger.portfolios.get(v).map(portfolio_ref))
                .collect(),
            pending_reconciliation: group.iter().any(|p| freeze.is_frozen(&p.key)),
        });
    }
    let holdings_value = money(&total);
    Ok(UnallocatedOut {
        cash: cash.clone(),
        cash_by_broker: ledger
            .broker_cash
            .iter()
            .filter_map(|row| {
                ledger
                    .brokers
                    .get(&row.broker_account_id)
                    .map(|a| BrokerCashOut {
                        broker: broker_ref(a),
                        balance: row.balance.clone(),
                        as_of: row.as_of,
                    })
            })
            .collect(),
        holdings_count: rows.len(),
        holdings: rows,
        total_value: money(&cash.add(&holdings_value)),
        holdings_value,
        pending_reconciliation: freeze.unallocated_pending,
        cta: ORGANIZE_CTA,
    })
}

fn estimated_costs_out(ledger: &Ledger) -> Option<EstimatedCostsOut> {
    let flows: Vec<TradeFlow> = ledger
        .flow_rows
        .iter()
        .map(|r| TradeFlow {
            on: r.occurred_on,
            kind: r.kind.value().to_owned(),
            value: r.amount.clone(),
            instrument_id: r.instrument_id,
        })
        .collect();
    let costs = estimate_portfolio_costs(&flows)?;
    Some(EstimatedCostsOut {
        bps_of_turnover: costs.bps_of_turnover(),
        stt: costs.stt,
        exchange: costs.exchange,
        sebi: costs.sebi,
        stamp: costs.stamp,
        gst: costs.gst,
        dp: costs.dp,
        brokerage: costs.brokerage,
        total: costs.total,
        turnover: costs.turnover,
        trades: costs.trades,
        sell_scrip_days: costs.sell_scrip_days,
        caveat: ESTIMATE_CAVEAT,
    })
}

fn refs_of_brokers(ledger: &Ledger, members: &[&Position]) -> Vec<BrokerRefOut> {
    members
        .iter()
        .map(|p| p.key.broker_account_id)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .filter_map(|id| ledger.brokers.get(&id).map(broker_ref))
        .collect()
}

// ---------------------------------------------------------------------------------------------
// GET /portfolio/overview
// ---------------------------------------------------------------------------------------------

/// `GET /portfolio/overview?range=1Y` — §6 in one response.
///
/// # Errors
/// 401 anonymous; 400 for an unknown `range`; 500 for a database failure or a domain refusal
/// (a damaged ledger), as Python's catch-all.
pub async fn portfolio_overview(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let window = range_param(&mut v, &q);
    v.finish()?;
    let user_id = principal.require_user()?;
    let mut conn = db::acquire(&state).await?;
    let mut ledger = load_ledger(&mut conn, user_id).await?;
    let mut trade_figure = None;
    if !ledger.flows.iter().any(|f| f.kind.is_xirr_event()) {
        ledger.trades = fills_by_instrument(&mut conn, user_id).await?;
        trade_figure = trade_xirr(&ledger)?;
    }
    let series = nav_by_portfolio(&mut conn, user_id).await?;
    let model_values = model_return_values(&mut conn, &ledger).await?;

    let holdings = ledger.holdings();
    let allocations = ledger.allocations();
    let prices = ledger.priced();
    let priced_holdings: Vec<Holding> = holdings
        .iter()
        .filter(|h| !ledger.prices.is_unpriced(h.key.instrument_id))
        .cloned()
        .collect();
    let all_positions: Vec<&Position> = ledger.positions.iter().collect();
    let priced_positions = priced(&ledger, &all_positions);
    let priced_allocations: Vec<_> = allocations
        .iter()
        .filter(|a| !ledger.prices.is_unpriced(a.key.instrument_id))
        .cloned()
        .collect();

    let unallocated_cash = money(
        &ledger
            .broker_cash
            .iter()
            .fold(Dec::lit("0"), |acc, r| acc.add(&r.balance)),
    );
    let mut portfolio_cash_by_id: HashMap<i64, Dec> = HashMap::new();
    for (id, p) in &ledger.portfolios {
        if p.kind == PortfolioKind::Capital {
            portfolio_cash_by_id.insert(*id, portfolio_cash(&ledger.flows, *id));
        }
    }
    let total_cash = money(
        &unallocated_cash.add(
            &portfolio_cash_by_id
                .values()
                .fold(Dec::lit("0"), |acc, c| acc.add(c)),
        ),
    );
    let current_value = consolidated_value(
        &priced_holdings,
        &priced_allocations,
        &ledger.portfolios,
        &prices,
        &total_cash,
    )
    .map_err(dfail)?;
    let values = portfolio_values(
        &priced_holdings,
        &priced_allocations,
        &ledger.portfolios,
        &prices,
    )
    .map_err(dfail)?;
    let freeze = freeze_report(&ledger.entries, &ledger.ledger_positions()?);
    let (invested, invested_value, without_basis) = cost_and_value(&ledger, &all_positions)?;

    let dividends = money(
        &ledger
            .flows
            .iter()
            .filter(|f| f.kind == CashFlowKind::Dividend)
            .fold(Dec::lit("0"), |acc, f| acc.add(&f.amount)),
    );
    let unrealised = match (&invested, &invested_value) {
        (Some(i), Some(v)) => {
            money_move(Some(money(&v.sub(i))), None, "Unrealised P&L", None, None)
        }
        _ => money_move(
            None,
            None,
            "Unrealised P&L",
            None,
            Some("No purchase prices on record yet"),
        ),
    };
    let hero = HeroOut {
        todays_pnl: todays_move(&ledger, &priced_positions, TODAY_LABEL),
        week_pnl: Some(week_move(&ledger, &priced_positions)),
        month_pnl: Some(month_move(&ledger, &priced_positions)),
        total_pnl: total_pnl_out(
            invested.as_ref(),
            invested_value.as_ref(),
            "Import your CAS to see returns from your purchase dates. Until then we know what \
             these shares are worth, not what you paid for them",
        ),
        xirr: consolidated_xirr(&ledger, &current_value, trade_figure)?,
        twr: consolidated_twr(series.get(&None).map_or(&[][..], Vec::as_slice))?,
        invested_unavailable_reason: invested
            .is_none()
            .then_some("No purchase prices on record yet"),
        invested,
        secondary: HeroSecondaryOut {
            cash: total_cash.clone(),
            dividends,
            broker_count: ledger.brokers.len(),
            unrealised_pnl: unrealised,
            realised_pnl: money_move(
                None,
                None,
                "Realised P&L",
                None,
                Some(
                    "Realised P&L needs the purchase price of shares you no longer hold. Import \
                     your CAS to unlock it",
                ),
            ),
        },
        pending_reconciliation: freeze.consolidated_pending(),
        holdings_without_cost_basis: without_basis,
        current_value,
    };

    let mut capital_rows = Vec::new();
    let mut monitoring_rows = Vec::new();
    for (id, portfolio) in &ledger.portfolios {
        if ledger.pile_ids.contains(id) {
            continue;
        }
        let members = members_of(&ledger, portfolio);
        let priced_members = priced(&ledger, &members);
        let points: Vec<NavPoint> = series
            .get(&Some(*id))
            .map_or(&[][..], Vec::as_slice)
            .iter()
            .map(nav_point)
            .collect::<Result<_, _>>()?;
        let capital = portfolio.kind == PortfolioKind::Capital;
        let row = PortfolioRowOut {
            portfolio_id: *id,
            name: portfolio.name.clone(),
            kind: PortfolioKindOut(portfolio.kind),
            source: PortfolioSourceOut(portfolio.source),
            source_badge: source_badge(portfolio, ledger.publishers.get(id)),
            publisher: ledger.publishers.get(id).cloned(),
            started_on: portfolio.started_on,
            value: if capital {
                values
                    .get(&Some(*id))
                    .cloned()
                    .unwrap_or_else(|| Dec::lit("0"))
            } else {
                monitoring_value(&ledger, &members)?
            },
            cash: portfolio_cash_by_id
                .get(id)
                .cloned()
                .unwrap_or_else(|| Dec::lit("0")),
            counts_toward_total: capital,
            excluded_note: (!capital).then_some(MONITORING_NOTE),
            todays_pnl: todays_move(&ledger, &priced_members, TODAY_LABEL),
            week_pnl: Some(week_move(&ledger, &priced_members)),
            month_pnl: Some(month_move(&ledger, &priced_members)),
            headline_return: figure_out(&headline_for(portfolio, &points)?),
            model_return: model_for(portfolio, model_values.get(id).cloned().flatten())?,
            brokers: refs_of_brokers(&ledger, &members),
            broker_count: members
                .iter()
                .map(|p| p.key.broker_account_id)
                .collect::<HashSet<_>>()
                .len(),
            holdings_count: members.len(),
            status: status_for(
                freeze.is_pending(*id),
                ledger.rebalance_due_portfolio_ids.contains(id),
            ),
            pending_reconciliation: freeze.is_pending(*id),
            benchmark_name: benchmark_name(&ledger, *id),
        };
        if capital {
            capital_rows.push(row);
        } else {
            monitoring_rows.push(row);
        }
    }

    let consolidated_rows = windowed(series.get(&None).map_or(&[][..], Vec::as_slice), window);
    let chart = series_out(
        &consolidated_rows,
        SeriesMeta {
            portfolio_id: None,
            window,
            label: "Consolidated time-weighted return".to_owned(),
            since: None,
            benchmark: None,
        },
    )?;
    let ribbon = attention_items(&AttentionInputs {
        as_of: today_ist(),
        holdings: &holdings,
        allocations: &allocations,
        entries: &ledger.entries,
        rebalance_due_portfolio_ids: ledger.rebalance_due_portfolio_ids.iter().copied().collect(),
        prices_as_of: ledger.prices.as_of,
        check_prices: !ledger.positions.is_empty(),
    })
    .map_err(dfail)?;
    let synced_on = latest_sync(&ledger);
    let statuses = sync_status(&ledger);
    let out = OverviewOut {
        prices_as_of: ledger.prices.as_of,
        prices_label: label_for_prices(ledger.prices.as_of, ledger.prices.live_overlay),
        holdings_synced_on: synced_on,
        holdings_synced_label: label_for_sync(synced_on),
        sync_summary: sync_summary(&statuses),
        sync_status: statuses,
        live_overlay: ledger.prices.live_overlay,
        hero,
        chart,
        attention: ribbon
            .iter()
            .map(|item| AttentionOut {
                kind: item.kind.value(),
                message: item.message(),
                count: item.count,
                subject_ids: item.subject_ids.clone(),
                since: item.since,
            })
            .collect(),
        portfolios: capital_rows,
        monitoring_views: monitoring_rows,
        unallocated: unallocated_out(&ledger, &unallocated_cash, &freeze)?,
        estimated_costs: estimated_costs_out(&ledger),
        open_reconciliation_count: ledger.entries.iter().filter(|e| e.freezes()).count(),
        monitoring_excluded_note: MONITORING_NOTE,
    };
    Ok(wire::json(&out))
}

// ---------------------------------------------------------------------------------------------
// GET /portfolio/holdings
// ---------------------------------------------------------------------------------------------

fn broker_line(
    ledger: &Ledger,
    position: &Position,
    freeze: &FreezeReport,
) -> Result<HoldingBrokerLineOut, Problem> {
    let account = ledger.brokers.get(&position.key.broker_account_id);
    let value = ledger.value_of(&position.holding)?;
    let allocation = position
        .sole_capital_portfolio_id()
        .and_then(|id| ledger.portfolios.get(&id))
        .map(portfolio_ref);
    Ok(HoldingBrokerLineOut {
        broker: account.map_or_else(
            || unknown_broker(position.key.broker_account_id),
            broker_ref,
        ),
        quantity: position.holding.quantity.clone(),
        unallocated_quantity: position.unallocated_quantity(),
        allocations: position
            .capital_slices
            .iter()
            .filter_map(|(pid, q)| {
                ledger.portfolios.get(pid).map(|p| HoldingSliceOut {
                    portfolio: portfolio_ref(p),
                    quantity: q.clone(),
                })
            })
            .collect(),
        price: ledger
            .prices
            .latest
            .get(&position.key.instrument_id)
            .cloned(),
        value,
        avg_price: position.holding.avg_price.clone(),
        cost_basis: cost_basis(&position.holding),
        allocation,
        monitoring_views: position
            .monitoring_portfolio_ids
            .iter()
            .filter_map(|v| ledger.portfolios.get(v).map(portfolio_ref))
            .collect(),
        first_bought_on: position.first_bought_on,
        history_source: position.history_source.clone(),
        pending_reconciliation: freeze.is_frozen(&position.key),
    })
}

/// `GET /portfolio/holdings` — §2's flat broker-level truth, aggregated per §6.7.
///
/// # Errors
/// 401 anonymous; 500 for a database failure or a domain refusal.
pub async fn portfolio_holdings(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    let user_id = require_authenticated(&principal)?.require_user()?;
    let mut conn = db::acquire(&state).await?;
    let ledger = load_ledger(&mut conn, user_id).await?;
    let freeze = freeze_report(&ledger.entries, &ledger.ledger_positions()?);
    let mut by_instrument: BTreeMap<i64, Vec<&Position>> = BTreeMap::new();
    for p in &ledger.positions {
        by_instrument
            .entry(p.key.instrument_id)
            .or_default()
            .push(p);
    }
    let zero = Dec::lit("0");
    let mut rows = Vec::new();
    let mut total = Dec::lit("0");
    let mut unallocated_count = 0;
    for (id, group) in &by_instrument {
        let Some(instrument) = ledger.instruments.get(id) else {
            continue;
        };
        let mut lines = Vec::with_capacity(group.len());
        for p in group {
            lines.push(broker_line(&ledger, p, &freeze)?);
        }
        let quantity = group
            .iter()
            .fold(Dec::lit("0"), |acc, p| acc.add(&p.holding.quantity));
        let mut value: Option<Dec> = None;
        for p in group {
            if let Some(v) = ledger.value_of(&p.holding)? {
                let base = match value {
                    Some(existing) if !existing.is_zero() => existing,
                    _ => Dec::lit("0"),
                };
                value = Some(base.add(&v));
            }
        }
        if let Some(v) = &value {
            total = total.add(v);
        }
        // allocated_ids: every slice of every leg, plus Unallocated (None) for a remainder.
        let mut allocated_ids: BTreeSet<Option<i64>> = BTreeSet::new();
        for p in group {
            allocated_ids.extend(p.capital_slices.iter().map(|(pid, _)| Some(*pid)));
            if p.unallocated_quantity() > zero || p.capital_slices.is_empty() {
                allocated_ids.insert(None);
            }
        }
        let only_unallocated = allocated_ids.len() == 1 && allocated_ids.contains(&None);
        let allocation_id = if allocated_ids.len() == 1 && !only_unallocated {
            allocated_ids.iter().next().copied().flatten()
        } else {
            None
        };
        if only_unallocated {
            unallocated_count += 1;
        }
        let bases: Vec<Option<Dec>> = group.iter().map(|p| cost_basis(&p.holding)).collect();
        let invested = if bases.iter().any(Option::is_none) {
            None
        } else {
            Some(
                bases
                    .iter()
                    .flatten()
                    .fold(Dec::lit("0"), |acc, b| acc.add(b)),
            )
        };
        let avg_price = match &invested {
            Some(i) if quantity > zero => {
                Some(money(&i.div(&quantity).ok_or_else(|| fail("div"))?))
            }
            _ => None,
        };
        rows.push(AggregatedHoldingOut {
            instrument: instrument_ref(instrument),
            price: ledger.prices.latest.get(id).cloned(),
            price_as_of: ledger.prices.dated.get(id).copied(),
            value: value.as_ref().map(money),
            allocation: allocation_id
                .and_then(|a| ledger.portfolios.get(&a))
                .map(portfolio_ref),
            allocated: !only_unallocated,
            split_across_portfolios: allocated_ids.len() > 1,
            monitoring_views: group
                .iter()
                .flat_map(|p| p.monitoring_portfolio_ids.iter())
                .filter_map(|v| ledger.portfolios.get(v).map(portfolio_ref))
                .collect(),
            brokers: lines,
            pending_reconciliation: group.iter().any(|p| freeze.is_frozen(&p.key)),
            invested: invested.as_ref().map(money),
            avg_price,
            total_pnl: Some(holding_total_pnl(invested.as_ref(), value.as_ref())),
            todays_pnl: Some(todays_move(&ledger, group, TODAY_LABEL)),
            week_pnl: Some(week_move(&ledger, group)),
            month_pnl: Some(month_move(&ledger, group)),
            quantity,
        });
    }
    let synced_on = latest_sync(&ledger);
    Ok(wire::json(&HoldingsOut {
        prices_as_of: ledger.prices.as_of,
        prices_label: label_for_prices(ledger.prices.as_of, ledger.prices.live_overlay),
        holdings_synced_on: synced_on,
        holdings_synced_label: label_for_sync(synced_on),
        rows,
        total_value: money(&total),
        unallocated_count,
        unpriced_instrument_ids: ledger.prices.unpriced.clone(),
        live_overlay: ledger.prices.live_overlay,
    }))
}

// ---------------------------------------------------------------------------------------------
// GET /portfolio/activity
// ---------------------------------------------------------------------------------------------

fn flow_activity(
    ledger: &Ledger,
    row: &super::portfolios::portfolio::load::FlowRow,
) -> ActivityItemOut {
    let portfolio = row.portfolio_id.and_then(|p| ledger.portfolios.get(&p));
    let instrument = row.instrument_id.and_then(|i| ledger.instruments.get(&i));
    let account = ledger.brokers.get(&row.broker_account_id);
    let name = portfolio.map_or("Unallocated", |p| p.name.as_str());
    let symbol = instrument.map_or("", |i| i.symbol.as_str());
    let qty = row
        .quantity
        .as_ref()
        .map_or_else(|| "None".to_owned(), Dec::to_sci_string);
    let amount = row.amount.to_sci_string();
    let description = match row.kind {
        CashFlowKind::Buy => format!("Bought {qty} {symbol} in {name}"),
        CashFlowKind::Sell => format!("Sold {qty} {symbol} from {name}"),
        CashFlowKind::Dividend => format!("Dividend from {symbol} in {name}"),
        CashFlowKind::Assign => format!("Assigned {amount} to {name}"),
        CashFlowKind::Release => format!("Released {amount} from {name} back to Unallocated"),
        CashFlowKind::ExternalDeposit => format!("Deposit of {amount} into Unallocated cash"),
        CashFlowKind::ExternalWithdrawal => {
            format!("Withdrawal of {amount} from Unallocated cash")
        }
    };
    ActivityItemOut {
        kind: row.kind.value(),
        on: row.occurred_on,
        description: match &row.note {
            Some(n) if !n.is_empty() => n.clone(),
            _ => description,
        },
        portfolio: portfolio.map(portfolio_ref),
        instrument: instrument.map(instrument_ref),
        broker: account.map(broker_ref),
        quantity: row.quantity.clone(),
        amount: Some(row.amount.clone()),
        is_pnl_event: true,
        reconciliation_item_id: None,
        reconciliation_state: None,
    }
}

fn reconciliation_activity(ledger: &Ledger, entry: &InboxEntry) -> ActivityItemOut {
    let instrument = ledger.instruments.get(&entry.key().instrument_id);
    let account = ledger.brokers.get(&entry.key().broker_account_id);
    let resolved = entry
        .resolved_portfolio_id
        .and_then(|p| ledger.portfolios.get(&p));
    let description = if entry.state == ReconciliationState::Open {
        entry.item.reason.question().to_owned()
    } else if let Some(r) = resolved {
        format!("Reconciled to {}", r.name)
    } else {
        "Reconciliation item dismissed — nothing to attribute".to_owned()
    };
    ActivityItemOut {
        kind: "RECONCILIATION",
        on: entry.resolved_on.unwrap_or(entry.detected_on),
        description,
        portfolio: resolved.map(portfolio_ref),
        instrument: instrument.map(instrument_ref),
        broker: account.map(broker_ref),
        quantity: Some(entry.item.quantity.clone()),
        amount: None,
        is_pnl_event: false,
        reconciliation_item_id: Some(entry.item_id),
        reconciliation_state: Some(entry.state),
    }
}

async fn corporate_actions(
    conn: &mut PgConnection,
    ledger: &Ledger,
    portfolio_id: Option<i64>,
) -> Result<Vec<ActivityItemOut>, Problem> {
    let ids: Vec<i64> = ledger
        .positions
        .iter()
        .filter(|p| match portfolio_id {
            None => true,
            Some(id) => p.has_slice(id) || p.monitoring_portfolio_ids.contains(&id),
        })
        .map(|p| p.key.instrument_id)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    if ids.is_empty() {
        return Ok(Vec::new());
    }
    let earliest = ledger.portfolios.values().map(|p| p.started_on).min();
    let rows = sqlx::query(
        "SELECT instrument_id, action_type, ex_date, amount FROM corporate_action \
         WHERE instrument_id = ANY($1) AND ($2::date IS NULL OR ex_date >= $2) \
         ORDER BY ex_date DESC",
    )
    .bind(&ids)
    .bind(earliest)
    .fetch_all(&mut *conn)
    .await
    .map_err(db::fail)?;
    let mut items = Vec::new();
    for row in &rows {
        let id: i64 = db::get(row, "instrument_id")?;
        let Some(instrument) = ledger.instruments.get(&id) else {
            continue;
        };
        let action: String = db::get(row, "action_type")?;
        items.push(ActivityItemOut {
            kind: "CORPORATE_ACTION",
            on: db::date(row, "ex_date")?,
            description: format!("{} on {}", pyfmt::py_title(&action), instrument.symbol),
            portfolio: None,
            instrument: Some(instrument_ref(instrument)),
            broker: None,
            quantity: None,
            amount: db::opt_pdec(row, "amount")?,
            is_pnl_event: false,
            reconciliation_item_id: None,
            reconciliation_state: None,
        });
    }
    Ok(items)
}

/// `GET /portfolio/activity?portfolio_id=&limit=50` — §7's merged feed, newest first.
///
/// # Errors
/// 401 anonymous; 400 for a bad `portfolio_id` (>= 1) or `limit` (1..=200); 404 for a
/// portfolio that is not the caller's; 500 as Python's catch-all.
pub async fn portfolio_activity(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let portfolio_id = match q.get("portfolio_id") {
        None => None,
        Some(raw) => params::int_value(
            &mut v,
            "portfolio_id",
            raw,
            IntBounds {
                ge: Some(1),
                le: None,
                gt: None,
            },
        ),
    };
    let limit = params::query_int(
        &mut v,
        &q,
        "limit",
        ACTIVITY_DEFAULT,
        IntBounds {
            ge: Some(1),
            le: Some(ACTIVITY_MAX),
            gt: None,
        },
    )
    .unwrap_or(ACTIVITY_DEFAULT);
    v.finish()?;
    let user_id = principal.require_user()?;
    let mut conn = db::acquire(&state).await?;
    let portfolio_id = match portfolio_id {
        Some(pid) => {
            owned_portfolio(&mut conn, pid, user_id).await?;
            Some(params::bigint(pid)?)
        }
        None => None,
    };
    let ledger = load_ledger(&mut conn, user_id).await?;
    let mut items = Vec::new();
    for row in &ledger.flow_rows {
        if let Some(pid) = portfolio_id
            && row.portfolio_id != Some(pid)
        {
            continue;
        }
        items.push(flow_activity(&ledger, row));
    }
    for entry in &ledger.entries {
        if let Some(pid) = portfolio_id
            && entry.resolved_portfolio_id != Some(pid)
            && entry.item.suggested_portfolio_id != Some(pid)
        {
            continue;
        }
        items.push(reconciliation_activity(&ledger, entry));
    }
    items.extend(corporate_actions(&mut conn, &ledger, portfolio_id).await?);
    items.sort_by(|a, b| (b.on, b.kind).cmp(&(a.on, a.kind)));
    let total = items.len();
    items.truncate(usize::try_from(limit).unwrap_or(usize::MAX));
    Ok(wire::json(&ActivityOut { items, total }))
}

// ---------------------------------------------------------------------------------------------
// GET /portfolio/reconciliation
// ---------------------------------------------------------------------------------------------

fn item_out(ledger: &Ledger, entry: &InboxEntry) -> ReconciliationItemOut {
    let key = entry.key();
    ReconciliationItemOut {
        item_id: entry.item_id,
        state: entry.state,
        reason: entry.item.reason.value(),
        question: entry.item.reason.question(),
        instrument: ledger.instruments.get(&key.instrument_id).map_or_else(
            || InstrumentRefOut {
                instrument_id: key.instrument_id,
                symbol: String::new(),
                name: "Unknown instrument".to_owned(),
            },
            instrument_ref,
        ),
        broker: ledger
            .brokers
            .get(&key.broker_account_id)
            .map_or_else(|| unknown_broker(key.broker_account_id), broker_ref),
        quantity: entry.item.quantity.clone(),
        detected_on: entry.detected_on,
        suggested_portfolio: entry
            .item
            .suggested_portfolio_id
            .and_then(|p| ledger.portfolios.get(&p))
            .map(portfolio_ref),
        resolved_portfolio: entry
            .resolved_portfolio_id
            .and_then(|p| ledger.portfolios.get(&p))
            .map(portfolio_ref),
        resolved_at: ledger
            .item_resolved_at
            .get(&entry.item_id)
            .copied()
            .flatten(),
        freezes: entry.freezes(),
    }
}

/// `GET /portfolio/reconciliation?state=` — §4.3's inbox (open questions by default).
///
/// # Errors
/// 401 anonymous; 400 for an unknown `state`; 500 as Python's catch-all.
pub async fn portfolio_reconciliation(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let wanted_state = match q.get("state") {
        None => None,
        Some(raw) => match ReconciliationState::parse(raw) {
            Ok(s) => Some(s),
            Err(_) => {
                v.error(
                    "state",
                    "Input should be 'OPEN', 'RESOLVED' or 'DISMISSED'",
                    "enum",
                );
                None
            }
        },
    };
    v.finish()?;
    let user_id = principal.require_user()?;
    let mut conn = db::acquire(&state).await?;
    let ledger = load_ledger(&mut conn, user_id).await?;
    let freeze = freeze_report(&ledger.entries, &ledger.ledger_positions()?);
    let items = ledger
        .entries
        .iter()
        .filter(|e| match wanted_state {
            None => e.freezes(),
            Some(s) => e.state == s,
        })
        .map(|e| item_out(&ledger, e))
        .collect();
    Ok(wire::json(&ReconciliationInboxOut {
        items,
        open_count: ledger.entries.iter().filter(|e| e.freezes()).count(),
        pending_portfolio_ids: freeze.pending_portfolio_ids.iter().copied().collect(),
        unallocated_pending: freeze.unallocated_pending,
        consolidated_pending: freeze.consolidated_pending(),
        pending_status_label: STATUS_PENDING,
    }))
}

// ---------------------------------------------------------------------------------------------
// GET /portfolio/suggestions
// ---------------------------------------------------------------------------------------------

async fn sector_map(
    conn: &mut PgConnection,
    ids: &[i64],
) -> Result<BTreeMap<i64, String>, Problem> {
    if ids.is_empty() {
        return Ok(BTreeMap::new());
    }
    let rows = sqlx::query(
        "SELECT DISTINCT ON (m.instrument_id) m.instrument_id, d.name FROM index_member_daily m \
         JOIN index_def d ON d.id = m.index_id WHERE m.instrument_id = ANY($1) \
         AND d.is_universe IS false ORDER BY m.instrument_id, m.date DESC, d.id",
    )
    .bind(ids)
    .fetch_all(&mut *conn)
    .await
    .map_err(db::fail)?;
    let mut out = BTreeMap::new();
    for row in &rows {
        out.insert(db::get(row, "instrument_id")?, db::get(row, "name")?);
    }
    Ok(out)
}

async fn subscribed_baskets(
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<Vec<SubscribedBasket>, Problem> {
    let invested: Vec<i64> = sqlx::query_scalar(
        "SELECT basket_id FROM cb_investment WHERE user_id = $1 AND status = 'ACTIVE'",
    )
    .bind(user_id)
    .fetch_all(&mut *conn)
    .await
    .map_err(db::fail)?;
    let filed: Vec<Option<i64>> = sqlx::query_scalar(
        "SELECT s.basket_id FROM portfolio_sleeve s JOIN portfolio p ON p.id = s.portfolio_id \
         WHERE p.user_id = $1 AND p.source = 'SUBSCRIBED' AND s.basket_id IS NOT NULL",
    )
    .bind(user_id)
    .fetch_all(&mut *conn)
    .await
    .map_err(db::fail)?;
    let ids: Vec<i64> = invested
        .into_iter()
        .chain(filed.into_iter().flatten())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    if ids.is_empty() {
        return Ok(Vec::new());
    }
    let rows = sqlx::query("SELECT id, name FROM cb_basket WHERE id = ANY($1)")
        .bind(&ids)
        .fetch_all(&mut *conn)
        .await
        .map_err(db::fail)?;
    let mut names = HashMap::new();
    for row in &rows {
        names.insert(db::get::<i64>(row, "id")?, db::get::<String>(row, "name")?);
    }
    let rows = sqlx::query(
        "SELECT anon_1.basket_id, c.instrument_id FROM (SELECT id AS version_id, basket_id, \
         row_number() OVER (PARTITION BY basket_id ORDER BY version_no DESC) AS recency \
         FROM cb_basket_version WHERE basket_id = ANY($1)) AS anon_1 \
         JOIN cb_constituent c ON c.version_id = anon_1.version_id WHERE anon_1.recency = 1",
    )
    .bind(&ids)
    .fetch_all(&mut *conn)
    .await
    .map_err(db::fail)?;
    let mut members: HashMap<i64, BTreeSet<i64>> = HashMap::new();
    for row in &rows {
        members
            .entry(db::get(row, "basket_id")?)
            .or_default()
            .insert(db::get(row, "instrument_id")?);
    }
    let mut found = Vec::new();
    for id in ids {
        let (Some(name), Some(constituents)) = (names.get(&id), members.remove(&id)) else {
            continue;
        };
        if constituents.is_empty() {
            continue;
        }
        found.push(SubscribedBasket {
            basket_id: id,
            name: name.clone(),
            constituent_instrument_ids: constituents,
        });
    }
    Ok(found)
}

/// `GET /portfolio/suggestions` — §6.6's first-run helper.
///
/// # Errors
/// 401 anonymous; 500 as Python's catch-all.
pub async fn portfolio_suggestions(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    let user_id = require_authenticated(&principal)?.require_user()?;
    let mut conn = db::acquire(&state).await?;
    let ledger = load_ledger(&mut conn, user_id).await?;
    let today = today_ist();
    let unallocated =
        unallocated_holdings(&ledger.holdings(), &ledger.allocations()).map_err(dfail)?;
    let unpriced: Vec<i64> = unallocated
        .iter()
        .map(|h| h.key.instrument_id)
        .filter(|id| ledger.prices.is_unpriced(*id))
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let priced_pile: Vec<Holding> = unallocated
        .iter()
        .filter(|h| !ledger.prices.is_unpriced(h.key.instrument_id))
        .cloned()
        .collect();
    let prices = ledger.priced();
    let mut unallocated_value = Dec::lit("0");
    for h in &priced_pile {
        unallocated_value = unallocated_value.add(&holding_value(h, &prices).map_err(dfail)?);
    }
    let unallocated_value = money(&unallocated_value);
    let ids: Vec<i64> = ledger
        .positions
        .iter()
        .map(|p| p.key.instrument_id)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let sectors = sector_map(&mut conn, &ids).await?;
    let mut first_bought = Vec::new();
    for p in &ledger.positions {
        if let Some(on) = p.first_bought_on
            && on <= today
        {
            match first_bought.iter_mut().find(|(k, _)| *k == p.key) {
                Some(slot) => *slot = (p.key, on),
                None => first_bought.push((p.key, on)),
            }
        }
    }
    let baskets = subscribed_baskets(&mut conn, user_id).await?;
    let pile: HashSet<_> = priced_pile.iter().map(|h| h.key).collect();
    let with_sector = priced_pile
        .iter()
        .filter(|h| sectors.contains_key(&h.key.instrument_id))
        .count();
    let with_dates = first_bought
        .iter()
        .filter(|(k, _)| pile.contains(k))
        .count();
    let n_baskets = baskets.len();
    let inputs = SuggestionInputs {
        sectors: sectors.iter().map(|(k, v)| (*k, v.clone())).collect(),
        first_bought: first_bought
            .iter()
            .filter(|(k, _)| pile.contains(k))
            .copied()
            .collect(),
        as_of: today,
        baskets,
    };
    let suggestions = suggest_groupings(&priced_pile, &prices, &inputs).map_err(dfail)?;
    let bases = vec![
        SuggestionBasisStatusOut {
            basis: SuggestionBasis::BasketOverlap,
            available: n_baskets > 0,
            considered: n_baskets,
            unavailable_reason: (n_baskets == 0).then_some(NO_BASKETS),
        },
        SuggestionBasisStatusOut {
            basis: SuggestionBasis::Sector,
            available: with_sector > 0,
            considered: with_sector,
            unavailable_reason: (with_sector == 0).then_some(NO_SECTORS),
        },
        SuggestionBasisStatusOut {
            basis: SuggestionBasis::PurchaseEra,
            available: with_dates > 0,
            considered: with_dates,
            unavailable_reason: (with_dates == 0).then_some(NO_PURCHASE_DATES),
        },
    ];
    let reason = if unallocated.is_empty() {
        Some(NOTHING_TO_SORT)
    } else if bases.iter().any(|b| b.available) {
        None
    } else {
        Some(NO_INPUTS)
    };
    Ok(wire::json(&SuggestionsOut {
        suggestions: suggestions
            .into_iter()
            .map(|s| GroupingSuggestionOut {
                basis: s.basis,
                proposed_name: s.proposed_name,
                keys: s
                    .keys
                    .iter()
                    .map(|k| HoldingKeyOut {
                        instrument_id: k.instrument_id,
                        broker_account_id: k.broker_account_id,
                    })
                    .collect(),
                value: s.value,
                rationale: s.rationale,
                suggested_kind: PortfolioKindOut(PortfolioKind::Capital),
                basket_id: s.basket_id,
                basket_coverage: s.basket_coverage,
                missing_instrument_ids: s.missing_instrument_ids,
            })
            .collect(),
        sectors: baskfy_db::OrderedJson::Object(
            sectors
                .iter()
                .map(|(k, v)| (k.to_string(), baskfy_db::OrderedJson::Str(v.clone())))
                .collect(),
        ),
        unallocated_count: unallocated.len(),
        unallocated_value,
        bases,
        unavailable_reason: reason,
        unpriced_instrument_ids: unpriced,
        cta: ORGANIZE_CTA,
    }))
}

// ---------------------------------------------------------------------------------------------
// GET /portfolio/{portfolio_id} and /{portfolio_id}/nav
// ---------------------------------------------------------------------------------------------

fn path_portfolio_id(v: &mut Validation, raw: &str) -> Option<params::PyInt> {
    params::int_value(
        v,
        "portfolio_id",
        raw,
        IntBounds {
            ge: Some(1),
            le: None,
            gt: None,
        },
    )
}

fn portfolio_xirr_out(
    ledger: &Ledger,
    portfolio: &LedgerPortfolio,
    closing_value: &Dec,
) -> Result<LabelledRateOut, Problem> {
    let label = "XIRR since your first cash assignment";
    if portfolio.kind == PortfolioKind::Monitoring {
        return Ok(rate(
            label,
            None,
            None,
            Some("A monitoring view holds no cash of its own, so it has no money-weighted return"),
        ));
    }
    let events: Vec<_> = ledger
        .flows
        .iter()
        .filter(|f| f.portfolio_id == Some(portfolio.portfolio_id) && f.kind.is_xirr_event())
        .collect();
    let (Some(since), Some(last)) = (
        events.iter().map(|f| f.occurred_on).min(),
        events.iter().map(|f| f.occurred_on).max(),
    ) else {
        return Ok(rate(
            label,
            None,
            None,
            Some("Assign cash to this portfolio to start measuring XIRR"),
        ));
    };
    let value = portfolio_xirr(
        &ledger.flows,
        portfolio.portfolio_id,
        valued_on(ledger, last),
        closing_value,
    )
    .map_err(dfail)?;
    let reason = value
        .is_none()
        .then_some("Not enough cash-flow history to solve a rate yet");
    Ok(rate(label, Some(since), value, reason))
}

fn source_panel(
    ledger: &Ledger,
    portfolio: &LedgerPortfolio,
    brokers: Vec<BrokerRefOut>,
) -> SourcePanelOut {
    let id = portfolio.portfolio_id;
    let basket = ledger.baskets.get(&id);
    let screen = ledger.screens.get(&id);
    let publisher = ledger.publishers.get(&id);
    let headline = match portfolio.source {
        PortfolioSource::Subscribed => format!(
            "Subscribed model by {}",
            publisher.map_or("an unnamed publisher", String::as_str)
        ),
        PortfolioSource::MyScreen => screen.map_or_else(
            || "Built from your own screening rules".to_owned(),
            |s| {
                format!(
                    "Built from your screen {}",
                    baskfy_providers::py::repr_str(&s.name)
                )
            },
        ),
        PortfolioSource::MyStrategy => "Driven by your saved strategy rules".to_owned(),
        PortfolioSource::HoldingGroup => format!(
            "Holdings you grouped on {}",
            wire::py_date(portfolio.started_on)
        ),
    };
    SourcePanelOut {
        source: PortfolioSourceOut(portfolio.source),
        headline,
        publisher: publisher.cloned(),
        basket_slug: basket.map(|b| b.slug.clone()),
        basket_name: basket.map(|b| b.name.clone()),
        screen_public_id: screen.map(|s| s.public_id.clone()),
        screen_name: screen.map(|s| s.name.clone()),
        brokers,
        grouped_on: (portfolio.source == PortfolioSource::HoldingGroup)
            .then_some(portfolio.started_on),
        execution_note: EXECUTION_NOTE,
    }
}

/// `GET /portfolio/{portfolio_id}` — §7: summary, holdings with weight and contribution, source.
///
/// # Errors
/// 401 anonymous; 400 for an id below 1 or not an integer; 404 when not the caller's; 500 as
/// Python's catch-all.
pub async fn portfolio_detail(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(raw_id): Path<String>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let mut v = Validation::new();
    let pid = path_portfolio_id(&mut v, &raw_id);
    v.finish()?;
    let pid = pid.unwrap_or_default();
    let user_id = principal.require_user()?;
    let mut conn = db::acquire(&state).await?;
    let benchmark_index_id = owned_portfolio(&mut conn, pid, user_id).await?;
    let pid = params::bigint(pid)?;
    Ok(wire::json(
        &detail_on(&mut conn, user_id, pid, benchmark_index_id).await?,
    ))
}

/// `portfolio_detail`'s body on a given connection (a write route's transaction answers with it).
async fn detail_on(
    conn: &mut PgConnection,
    user_id: i64,
    pid: i64,
    benchmark_index_id: Option<i64>,
) -> Result<PortfolioDetailOut, Problem> {
    let ledger = load_ledger(conn, user_id).await?;
    let portfolio = ledger
        .portfolios
        .get(&pid)
        .ok_or_else(|| fail(&format!("KeyError: {pid}")))?
        .clone();
    let members = members_of(&ledger, &portfolio);
    let freeze = freeze_report(&ledger.entries, &ledger.ledger_positions()?);
    let model_values = model_return_values(conn, &ledger).await?;
    let rows = nav_rows(conn, user_id, Some(pid)).await?;
    let points: Vec<NavPoint> = rows.iter().map(nav_point).collect::<Result<_, _>>()?;
    let priced_members = priced(&ledger, &members);
    let mut value = Dec::lit("0");
    for p in &priced_members {
        value = value.add(
            &ledger
                .value_of(&p.holding)?
                .unwrap_or_else(|| Dec::lit("0")),
        );
    }
    let value = money(&value);
    let capital = portfolio.kind == PortfolioKind::Capital;
    let cash = if capital {
        portfolio_cash(&ledger.flows, pid)
    } else {
        Dec::lit("0")
    };
    let (invested, invested_value, _) = cost_and_value(&ledger, &members)?;
    let headline = headline_for(&portfolio, &points)?;

    let mut holdings = Vec::new();
    for p in &members {
        let (Some(instrument), Some(account)) = (
            ledger.instruments.get(&p.key.instrument_id),
            ledger.brokers.get(&p.key.broker_account_id),
        ) else {
            continue;
        };
        let hv = ledger.value_of(&p.holding)?;
        let basis = cost_basis(&p.holding);
        let latest = ledger.prices.latest.get(&p.key.instrument_id);
        let previous = ledger.prices.previous.get(&p.key.instrument_id);
        holdings.push(DetailHoldingOut {
            instrument: instrument_ref(instrument),
            broker: broker_ref(account),
            quantity: p.holding.quantity.clone(),
            avg_price: p.holding.avg_price.clone(),
            price: latest.cloned(),
            weight: match &hv {
                Some(h) if !value.is_zero() => h.div(&value).map(|r| quantize_return_even(&r)),
                _ => None,
            },
            todays_contribution: match (latest, previous) {
                (Some(l), Some(pr)) => Some(money(&p.holding.quantity.mul(&l.sub(pr)))),
                _ => None,
            },
            total_contribution: match (&basis, &hv) {
                (Some(b), Some(h)) => Some(money(&h.sub(b))),
                _ => None,
            },
            value: hv,
            first_bought_on: p.first_bought_on,
            history_source: p.history_source.clone(),
            pending_reconciliation: freeze.is_frozen(&p.key),
        });
    }
    let index = benchmark_index_id.and_then(|id| ledger.benchmarks.get(&id).map(|name| (id, name)));
    let brokers = refs_of_brokers(&ledger, &members);
    let summary = PortfolioSummaryOut {
        portfolio_id: pid,
        name: portfolio.name.clone(),
        kind: PortfolioKindOut(portfolio.kind),
        source: PortfolioSourceOut(portfolio.source),
        source_badge: source_badge(&portfolio, ledger.publishers.get(&pid)),
        publisher: ledger.publishers.get(&pid).cloned(),
        started_on: portfolio.started_on,
        invested_unavailable_reason: invested
            .is_none()
            .then_some("No purchase prices on record yet"),
        todays_pnl: todays_move(&ledger, &priced_members, TODAY_LABEL),
        week_pnl: Some(week_move(&ledger, &priced_members)),
        month_pnl: Some(month_move(&ledger, &priced_members)),
        total_pnl: total_pnl_out(
            invested.as_ref(),
            invested_value.as_ref(),
            "Import your CAS to see returns from your purchase dates",
        ),
        invested,
        headline_return: figure_out(&headline),
        model_return: model_for(&portfolio, model_values.get(&pid).cloned().flatten())?,
        xirr: portfolio_xirr_out(&ledger, &portfolio, &money(&value.add(&cash)))?,
        benchmark: benchmark(conn, index, &headline, &rows).await?,
        counts_toward_total: capital,
        excluded_note: (!capital).then_some(MONITORING_NOTE),
        status: status_for(
            freeze.is_pending(pid),
            ledger.rebalance_due_portfolio_ids.contains(&pid),
        ),
        pending_reconciliation: freeze.is_pending(pid),
        holdings_synced_on: latest_sync(&ledger),
        prices_as_of: ledger.prices.as_of,
        value,
        cash,
    };
    Ok(PortfolioDetailOut {
        summary,
        holdings,
        source_panel: source_panel(&ledger, &portfolio, brokers.clone()),
        brokers,
    })
}

/// `GET /portfolio/{portfolio_id}/nav?range=1Y` — one portfolio's stored EOD series.
///
/// # Errors
/// 401 anonymous; 400 for a bad id or `range`; 404 when not the caller's; 500 as Python's
/// catch-all (including a series that starts before the portfolio did).
pub async fn portfolio_nav(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(raw_id): Path<String>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let pid = path_portfolio_id(&mut v, &raw_id);
    let window = range_param(&mut v, &q);
    v.finish()?;
    let pid = pid.unwrap_or_default();
    let user_id = principal.require_user()?;
    let mut conn = db::acquire(&state).await?;
    let benchmark_index_id = owned_portfolio(&mut conn, pid, user_id).await?;
    let pid = params::bigint(pid)?;
    let ledger = load_ledger(&mut conn, user_id).await?;
    let portfolio = ledger
        .portfolios
        .get(&pid)
        .ok_or_else(|| fail(&format!("KeyError: {pid}")))?
        .clone();
    let all_rows = nav_rows(&mut conn, user_id, Some(pid)).await?;
    let rows = windowed(&all_rows, window);
    let points: Vec<NavPoint> = all_rows.iter().map(nav_point).collect::<Result<_, _>>()?;
    let headline = headline_for(&portfolio, &points)?;
    let index = benchmark_index_id.and_then(|id| ledger.benchmarks.get(&id).map(|name| (id, name)));
    let bench = benchmark(&mut conn, index, &headline, &rows).await?;
    let out = series_out(
        &rows,
        SeriesMeta {
            portfolio_id: Some(pid),
            window,
            label: headline.label(),
            since: Some(portfolio.started_on),
            benchmark: bench,
        },
    )?;
    Ok(wire::json(&out))
}

/// The GET routes of `baskfy_api.routers.portfolio_overview`, relative to `/api/v1`, plus the
/// write routes' paths with no method (Starlette answers a GET there with 405).
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/portfolio", post(writes::new_portfolio))
        .route("/portfolio/overview", get(portfolio_overview))
        .route("/portfolio/holdings", get(portfolio_holdings))
        .route("/portfolio/activity", get(portfolio_activity))
        .route("/portfolio/reconciliation", get(portfolio_reconciliation))
        .route(
            "/portfolio/reconciliation/{item_id}/resolve",
            post(writes::resolve_reconciliation_item),
        )
        .route("/portfolio/suggestions", get(portfolio_suggestions))
        .route("/portfolio/{portfolio_id}", get(portfolio_detail))
        .route(
            "/portfolio/{portfolio_id}/holdings",
            post(writes::add_holdings),
        )
        .route("/portfolio/{portfolio_id}/nav", get(portfolio_nav))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ranges_and_labels() {
        assert_eq!(NavRange::parse("1Y"), Some(NavRange::Y1));
        assert_eq!(NavRange::parse("1y"), None);
        assert_eq!(label_for_prices(None, false), "No closing prices yet");
        assert_eq!(label_for_sync(None), "Holdings not synced yet");
        assert_eq!(status_for(true, true), STATUS_PENDING);
        assert_eq!(status_for(false, true), STATUS_REBALANCE);
        // §9: no forbidden word in any sentence this module emits.
        for s in [
            ORGANIZE_CTA,
            MONITORING_NOTE,
            NO_SECTORS,
            NO_PURCHASE_DATES,
            NO_BASKETS,
            NOTHING_TO_SORT,
            NO_INPUTS,
            EXECUTION_NOTE,
        ] {
            let lower = s.to_lowercase();
            for word in ["managed", "advisory", "pms"] {
                assert!(!lower.contains(word), "{s}");
            }
        }
    }
}
