//! What a SIGNAL plan — and a confirm — is sized against (`app/swing_monitor.py`:
//! `SignalContext`, `load_context`, `read_live_gate`, `entries_now`, `live_execution`,
//! `auto_execute_enabled`, `gate_line`). Re-read per trigger and per confirm (A5), so the 09:45
//! trigger sees what was confirmed at 09:31, and the per-session cap counts today's entries.

use std::collections::BTreeMap;

use baskfy_core::swing::market::{ExposureTier, MarketGate};
use baskfy_core::swing::plan::{
    PlanLine, SkipReason, Skipped, SwingAccount, WatchItem, build_entries, first_live_multiplier,
};
use baskfy_core::swing::setups::SwingConfig;
use chrono::{DateTime, FixedOffset, NaiveDate};
use rust_decimal::Decimal;

use super::live_gate::{LiveGate, LiveLevel, in_session, live_gate, published_levels};
use super::rules::{
    BreadthSnapshot, INDEX_HISTORY_SESSIONS, dec_of_float, gate_from, gate_str, money, py_dec,
    skip_str,
};
use super::values::{boolean, col, int, raw_dec, text};
use crate::settings::Settings;
use crate::storage::{Conn, DbValue, Param, StorageError};

/// `ENTRY_TAKEN_STATES`: the session has taken (or is taking) the entry.
pub const ENTRY_TAKEN_STATES: [&str; 3] = ["CONFIRMED", "SENT", "FILLED"];

/// `PendingLine`: a BUY confirmed today that is not (yet) a position.
#[derive(Debug, Clone, PartialEq)]
pub struct PendingLine {
    /// `line_id`.
    pub line_id: i64,
    /// `symbol`.
    pub symbol: String,
    /// `quantity`.
    pub quantity: i64,
    /// `value_inr`.
    pub value_inr: Decimal,
    /// `state`.
    pub state: String,
}

/// `SignalContext`.
#[derive(Debug, Clone, PartialEq)]
pub struct SignalContext {
    /// The gate.
    pub gate: MarketGate,
    /// The rung.
    pub tier: ExposureTier,
    /// The book at cost plus every pending line.
    pub account: SwingAccount,
    /// symbol → (adr_pct, avg_turnover_inr, score).
    pub detected: BTreeMap<String, (Decimal, Option<Decimal>, Decimal)>,
    /// BUY lines the session has already confirmed, sent or filled.
    pub entries_today: i64,
    /// The pending lines.
    pub pending: Vec<PendingLine>,
    /// A7: symbols whose PENDING_RANGE line is still PROPOSED today.
    pub reserved: Vec<String>,
    /// A9: `first_live_sessions_left`.
    pub first_live_sessions_left: i64,
    /// SW30: the live re-read, when it took.
    pub live: Option<LiveGate>,
}

fn red_tier() -> ExposureTier {
    ExposureTier {
        level: 0,
        max_open_positions: 0,
        max_exposure_pct: 0.0,
        new_entries_allowed: false,
        drawdown_locked: false,
    }
}

fn t(schema: &str, table: &str) -> String {
    if schema.is_empty() {
        table.to_owned()
    } else {
        format!("{schema}.{table}")
    }
}

fn float_of(value: Option<&DbValue>) -> f64 {
    match value {
        Some(DbValue::Float(f)) => *f,
        #[allow(clippy::cast_precision_loss)]
        Some(DbValue::Int(i)) => *i as f64,
        Some(DbValue::Text(s)) => s.trim().parse().unwrap_or(0.0),
        _ => 0.0,
    }
}

fn dec_or_zero(value: Option<&DbValue>) -> Decimal {
    value.and_then(raw_dec).unwrap_or(Decimal::ZERO)
}

/// `load_context`.
///
/// # Errors
/// The driver's.
#[allow(clippy::too_many_lines)]
pub async fn load_context(
    conn: &mut Conn,
    user_id: i64,
    day: NaiveDate,
    schema: &str,
    live_level: Option<&dyn LiveLevel>,
    now: Option<DateTime<FixedOffset>>,
    index_slug: &str,
) -> Result<SignalContext, StorageError> {
    let day_iso = day.format("%Y-%m-%d").to_string();
    let market = conn
        .execute(
            &format!(
                "SELECT gate, exposure_level, max_open_positions, max_exposure_pct, \
                 new_entries_allowed, drawdown_locked, constituent_count, pct_up_strong_1m, \
                 pct_new_52w_high, pct_above_ma_slow FROM {} \
                 WHERE user_id = ? AND date < ? ORDER BY date DESC LIMIT 1",
                t(schema, "sw_market_daily")
            ),
            &[Param::Int(user_id), Param::Text(day_iso.clone())],
        )
        .await?
        .rows
        .into_iter()
        .next();
    let (mut gate, mut tier) = match &market {
        None => (MarketGate::Red, red_tier()),
        Some(m) => (
            gate_from(&text(col(m, "gate"))).unwrap_or(MarketGate::Red),
            ExposureTier {
                level: int(col(m, "exposure_level")),
                max_open_positions: int(col(m, "max_open_positions")),
                max_exposure_pct: float_of(col(m, "max_exposure_pct")),
                new_entries_allowed: boolean(col(m, "new_entries_allowed")),
                drawdown_locked: boolean(col(m, "drawdown_locked")),
            },
        ),
    };
    let mut live = None;
    if let (Some(m), Some(level)) = (&market, live_level) {
        live = read_live_gate(conn, m, &tier, day, schema, level, now, index_slug).await?;
        if let Some(l) = &live {
            gate = l.gate;
            tier = l.tier.clone();
        }
    }
    let capital_row = conn
        .execute(
            &format!(
                "SELECT sleeve_capital_inr, first_live_sessions_left FROM {} WHERE user_id = ?",
                t(schema, "sw_config")
            ),
            &[Param::Int(user_id)],
        )
        .await?
        .rows
        .into_iter()
        .next();
    let capital = capital_row
        .as_ref()
        .map_or(Decimal::ZERO, |r| dec_or_zero(col(r, "sleeve_capital_inr")));
    let first_live_left = capital_row
        .as_ref()
        .map_or(0, |r| int(col(r, "first_live_sessions_left")));
    let held = conn
        .execute(
            &format!(
                "SELECT i.symbol, p.entry_avg, p.quantity_open FROM {} p \
                 JOIN {} i ON i.id = p.instrument_id \
                 WHERE p.user_id = ? AND p.state IN ('OPEN', 'PARTIAL') AND p.quantity_open > 0",
                t(schema, "sw_position"),
                t(schema, "instrument")
            ),
            &[Param::Int(user_id)],
        )
        .await?
        .rows;
    let mut exposure = held.iter().fold(Decimal::ZERO, |acc, r| {
        acc + dec_or_zero(col(r, "entry_avg")) * Decimal::from(int(col(r, "quantity_open")))
    });
    let mut open_symbols: std::collections::BTreeSet<String> =
        held.iter().map(|r| text(col(r, "symbol"))).collect();
    let mut params = vec![Param::Int(user_id), Param::Text(day_iso.clone())];
    params.extend(
        ENTRY_TAKEN_STATES
            .iter()
            .map(|s| Param::Text((*s).to_owned())),
    );
    let taken = conn
        .execute(
            &format!(
                "SELECT l.id, i.symbol, l.quantity, l.trigger, l.state, l.position_id, \
                 (SELECT q.quantity_entered FROM {pos} q WHERE q.id = l.position_id) AS entered \
                 FROM {line} l JOIN {plan} p ON p.id = l.plan_id \
                 JOIN {ins} i ON i.id = l.instrument_id \
                 WHERE l.user_id = ? AND p.as_of = ? AND l.kind = 'BUY_ON_TRIGGER' \
                 AND l.state IN (?, ?, ?) ORDER BY l.id",
                pos = t(schema, "sw_position"),
                line = t(schema, "sw_plan_line"),
                plan = t(schema, "sw_plan"),
                ins = t(schema, "instrument"),
            ),
            &params,
        )
        .await?
        .rows;
    let mut pending = Vec::new();
    for r in &taken {
        let symbol = text(col(r, "symbol"));
        let position_id = col(r, "position_id").filter(|v| !v.is_null());
        if position_id.is_some() || open_symbols.contains(&symbol) {
            let entered = col(r, "entered").filter(|v| !v.is_null());
            if text(col(r, "state")) == "SENT"
                && let Some(entered) = entered
            {
                let remainder = int(col(r, "quantity")) - int(Some(entered));
                if remainder > 0 {
                    exposure += dec_or_zero(col(r, "trigger")) * Decimal::from(remainder);
                }
            }
            continue;
        }
        let quantity = int(col(r, "quantity"));
        let value = dec_or_zero(col(r, "trigger")) * Decimal::from(quantity);
        pending.push(PendingLine {
            line_id: int(col(r, "id")),
            symbol: symbol.clone(),
            quantity,
            value_inr: value,
            state: text(col(r, "state")),
        });
        exposure += value;
        open_symbols.insert(symbol);
    }
    let reserved: Vec<String> = conn
        .execute(
            &format!(
                "SELECT i.symbol FROM {} l JOIN {} p ON p.id = l.plan_id \
                 JOIN {} i ON i.id = l.instrument_id \
                 WHERE l.user_id = ? AND p.as_of = ? AND l.kind = 'PENDING_RANGE' \
                 AND l.state = 'PROPOSED' ORDER BY l.id",
                t(schema, "sw_plan_line"),
                t(schema, "sw_plan"),
                t(schema, "instrument")
            ),
            &[Param::Int(user_id), Param::Text(day_iso.clone())],
        )
        .await?
        .rows
        .iter()
        .map(|r| text(col(r, "symbol")))
        .collect();
    let account = SwingAccount {
        equity: capital,
        cash_available: (capital - exposure).max(Decimal::ZERO),
        open_symbols,
        open_exposure_inr: exposure,
        open_entry_counts: BTreeMap::new(),
    };
    let stats = conn
        .execute(
            &format!(
                "SELECT i.symbol, s.adr_pct, s.turnover_avg, s.score \
                 FROM {sd} s JOIN {ins} i ON i.id = s.instrument_id \
                 WHERE s.user_id = ? AND s.date < ? AND s.date = (\
                 SELECT max(x.date) FROM {sd} x \
                 WHERE x.user_id = s.user_id AND x.instrument_id = s.instrument_id AND x.date < ?) \
                 ORDER BY s.instrument_id, s.score DESC",
                sd = t(schema, "sw_setup_daily"),
                ins = t(schema, "instrument"),
            ),
            &[
                Param::Int(user_id),
                Param::Text(day_iso.clone()),
                Param::Text(day_iso.clone()),
            ],
        )
        .await?
        .rows;
    let mut detected = BTreeMap::new();
    for r in &stats {
        let symbol = text(col(r, "symbol"));
        if detected.contains_key(&symbol) {
            continue;
        }
        let turnover = col(r, "turnover_avg")
            .filter(|v| !v.is_null())
            .map(|v| Decimal::from(int(Some(v))));
        detected.insert(
            symbol,
            (
                dec_or_zero(col(r, "adr_pct")),
                turnover,
                dec_or_zero(col(r, "score")),
            ),
        );
    }
    for r in conn
        .execute(
            &format!(
                "SELECT i.symbol, w.adr_pct, w.score FROM {} w \
                 JOIN {} i ON i.id = w.instrument_id \
                 WHERE w.user_id = ? AND w.state = 'WATCHING' ORDER BY w.id",
                t(schema, "sw_watch"),
                t(schema, "instrument")
            ),
            &[Param::Int(user_id)],
        )
        .await?
        .rows
    {
        let symbol = text(col(&r, "symbol"));
        let adr = col(&r, "adr_pct").filter(|v| !v.is_null());
        let Some(adr) = adr else { continue };
        if detected.contains_key(&symbol) {
            continue;
        }
        let adr = dec_or_zero(Some(adr));
        detected.insert(symbol, (adr, None, dec_or_zero(col(&r, "score"))));
    }
    Ok(SignalContext {
        gate,
        tier,
        account,
        detected,
        entries_today: i64::try_from(taken.len()).unwrap_or(i64::MAX),
        pending,
        reserved,
        first_live_sessions_left: first_live_left,
        live,
    })
}

/// `read_live_gate` (SW30): `None` outside the session, without breadth, without a level, or
/// with too few published closes. The quote is only asked for while the session is open.
///
/// # Errors
/// The driver's.
#[allow(clippy::too_many_arguments)]
pub async fn read_live_gate(
    conn: &mut Conn,
    market: &crate::storage::Row,
    close_tier: &ExposureTier,
    day: NaiveDate,
    schema: &str,
    live_level: &dyn LiveLevel,
    now: Option<DateTime<FixedOffset>>,
    index_slug: &str,
) -> Result<Option<LiveGate>, StorageError> {
    let Some(moment) = now else { return Ok(None) };
    let moment = moment.with_timezone(&crate::clock::ist());
    if moment.date_naive() != day || !in_session(&moment) {
        return Ok(None);
    }
    if col(market, "pct_up_strong_1m").is_none_or(DbValue::is_null) {
        return Ok(None);
    }
    let Some(level) = live_level.level().await else {
        return Ok(None);
    };
    let config = SwingConfig::default().market;
    let closes =
        published_levels(conn, index_slug, day, INDEX_HISTORY_SESSIONS - 1, schema).await?;
    let breadth = BreadthSnapshot {
        constituent_count: int(col(market, "constituent_count")),
        pct_up_strong_1m: float_of(col(market, "pct_up_strong_1m")),
        pct_new_52w_high: float_of(col(market, "pct_new_52w_high")),
        pct_above_ma_slow: float_of(col(market, "pct_above_ma_slow")),
    };
    Ok(live_gate(
        &breadth, close_tier, &closes, level, index_slug, &config,
    ))
}

/// `live_execution()`: a confirm would place a real order — the swing flag on AND the desk not
/// in DRY_RUN.
#[must_use]
pub const fn live_execution(settings: &Settings) -> bool {
    settings.swing_execution_enabled && !settings.dry_run
}

/// `auto_execute_enabled()` (SW25): may the monitor confirm its own triggers? All THREE flags —
/// `DRY_RUN=false`, `BASKFY_SWING_EXECUTION_ENABLED=true`, `BASKFY_SWING_AUTO_EXECUTE=true` —
/// never fewer. Each defaults to the safe side; flipping them is Maulik's hand.
#[must_use]
pub const fn auto_execute_enabled(settings: &Settings) -> bool {
    live_execution(settings) && settings.swing_auto_execute
}

/// `entries_now`: one triggered name against the book as it is now — the SIGNAL plan's size and
/// the confirm's, from one function. When the exposure ceiling alone is in the way, the name is
/// tried once more with the cash bounded by the ceiling's headroom (A5).
///
/// # Errors
/// The core's (a decimal overflow, a non-finite config).
pub fn entries_now(
    item: &WatchItem,
    context: &SignalContext,
    config: &SwingConfig,
    day: NaiveDate,
    live: bool,
) -> Result<(Vec<PlanLine>, Vec<Skipped>), baskfy_core::error::CoreError> {
    let reserved_others = i64::try_from(
        context
            .reserved
            .iter()
            .filter(|s| **s != item.symbol)
            .count(),
    )
    .unwrap_or(0);
    let already = context.entries_today + reserved_others;
    let multiplier = first_live_multiplier(context.first_live_sessions_left, live, &config.sizing)?;
    let first = build_entries(
        day,
        std::slice::from_ref(item),
        &context.account,
        context.gate,
        &context.tier,
        config,
        already,
        multiplier,
    )?;
    if !first.lines.is_empty()
        || first.skipped.is_empty()
        || first.skipped[0].reason != SkipReason::ExposureFull
    {
        return Ok((first.lines, first.skipped));
    }
    let account = &context.account;
    let ceiling =
        account.equity * dec_of_float(context.tier.max_exposure_pct) / Decimal::ONE_HUNDRED;
    let headroom = (ceiling - account.open_exposure_inr).max(Decimal::ZERO);
    let mut bounded = account.clone();
    bounded.cash_available = account.cash_available.min(headroom);
    let retry = build_entries(
        day,
        std::slice::from_ref(item),
        &bounded,
        context.gate,
        &context.tier,
        config,
        already,
        multiplier,
    )?;
    if !retry.lines.is_empty() {
        return Ok((retry.lines, Vec::new()));
    }
    let why = retry.skipped.first().map_or_else(
        || "nothing".to_owned(),
        |s| {
            format!("{} {}", skip_str(s.reason), s.detail)
                .trim()
                .to_owned()
        },
    );
    Ok((
        Vec::new(),
        vec![Skipped {
            symbol: item.symbol.clone(),
            reason: SkipReason::ExposureFull,
            detail: format!(
                "{}: book ₹{} of ₹{} leaves ₹{}, and at that size {why}",
                first.skipped[0].detail,
                money(account.open_exposure_inr),
                money(ceiling),
                money(headroom)
            ),
        }],
    ))
}

/// `gate_line`: the heartbeat's gate clause.
#[must_use]
pub fn gate_line(context: &SignalContext) -> String {
    match &context.live {
        None => format!("gate {} (last close)", gate_str(context.gate)),
        Some(live) => format!(
            "gate {} (live {} {}: 10d {} / 20d {})",
            gate_str(context.gate),
            live.index_slug,
            baskfy_providers::py::fixed(live.reading.close, 2),
            baskfy_providers::py::fixed(live.reading.ma_fast, 2),
            baskfy_providers::py::fixed(live.reading.ma_slow, 2),
        ),
    }
}

/// `Decimal` as Python prints it, re-exported for the siblings' sentences.
#[must_use]
pub fn show(d: Decimal) -> String {
    py_dec(d)
}
