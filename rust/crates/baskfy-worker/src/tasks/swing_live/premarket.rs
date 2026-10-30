//! SW6 — the morning before the open (`tasks/swing_premarket.py`): refresh the levels (08:50),
//! find the gaps and rebuild the plan (09:16, `source=MORNING`); and the plan rebuilt against a
//! session in progress (`tasks/swing_intraday.py`, `source=INTRADAY`).
//!
//! Every plan piece is the evening's — the `swing_eod` lane's port of `manage_open_positions`,
//! `watch_items`, `sleeve_account`, `held_instrument_ids`, `settle_ladder`, `assemble`,
//! `store_plan`, `auto_watch` and `refresh_focus` — imported, never reimplemented, so a MORNING or
//! INTRADAY line cannot be sized by rules the EOD preview does not share.
//!
//! The flag `BASKFY_SWING_EP_PREMARKET_ENABLED` gates the quote pull only; the level refresh and
//! the morning plan run on data the nightly already wrote and cost no Kite call. The 09:16
//! stage settles the previous session's ladder only when the evening never did (A10).
//!
//! **Law 2.** A plan is rows a person confirms on the desk; nothing here sends it.

use std::collections::BTreeSet;

use baskfy_core::decimal::{Rounding, WideDecimal};
use baskfy_core::swing::market::{ExposureTier, MarketGate};
use baskfy_core::swing::opening_range::{LiveGapVerdict, live_gap, live_gap_score};
use baskfy_core::swing::plan::{LineKind, PlanLine, build_entries, first_live_multiplier};
use baskfy_core::swing::setups::SwingConfig;
use baskfy_db::OrderedJson;
use baskfy_providers::kite::KiteProvider;
use baskfy_providers::records::QuoteRecord;
use chrono::{DateTime, Duration, NaiveDate, NaiveDateTime, NaiveTime, Utc};
use rust_decimal::Decimal;
use sqlx::{PgConnection, Row as _};

use super::TaskContext;
use super::scan::{LiquidName, liquid_universe, load_swing_config};
use crate::error::WorkerError;
use crate::pyjson::{int, object, s, strings};
use crate::steps::log_warning;
use crate::tasks::swing_eod::for_swing_live as eod;

/// `STAGE_LEVELS` / `STAGE_GAPS`.
const STAGE_LEVELS: &str = "LEVELS";
/// `STAGE_GAPS`.
pub(super) const STAGE_GAPS: &str = "GAPS";
/// `LIVE_GAP_SOURCE`.
const LIVE_GAP_SOURCE: &str = "DETECTOR";
/// `INTRADAY`.
const INTRADAY: &str = "INTRADAY";
/// `NSE_EXCHANGE_ID`.
const NSE_EXCHANGE_ID: i16 = 1;

fn iso(day: NaiveDate) -> String {
    day.format("%Y-%m-%d").to_string()
}

fn count(n: usize) -> i64 {
    i64::try_from(n).unwrap_or(i64::MAX)
}

fn core_err(e: baskfy_core::error::CoreError) -> WorkerError {
    WorkerError::python("InvalidOperation", e.to_string())
}

fn dec_err(e: baskfy_core::decimal::DecimalError) -> WorkerError {
    WorkerError::python("InvalidOperation", e.to_string())
}

/// `PREOPEN_START`.
fn preopen_start() -> NaiveTime {
    NaiveTime::from_hms_opt(9, 0, 0).unwrap_or(NaiveTime::MIN)
}

/// `PremarketReport`.
#[derive(Debug, Clone)]
struct PremarketReport {
    session_date: NaiveDate,
    stage: String,
    levels_refreshed: i64,
    levels_unchanged: i64,
    quotes_pulled: i64,
    universe: i64,
    gap_candidates: Vec<String>,
    gaps_added: i64,
    gaps_already_watched: i64,
    plan_id: Option<String>,
    entry_lines: i64,
    pending_lines: i64,
    exit_lines: i64,
    skips: i64,
    skipped_reason: Option<String>,
    focus: i64,
    ladder_caught_up: bool,
    first_live_sessions_left: i64,
    risk_multiplier: String,
    risk_pct_in_force: String,
}

impl PremarketReport {
    fn new(session_date: NaiveDate, stage: &str) -> Self {
        Self {
            session_date,
            stage: stage.to_owned(),
            levels_refreshed: 0,
            levels_unchanged: 0,
            quotes_pulled: 0,
            universe: 0,
            gap_candidates: Vec::new(),
            gaps_added: 0,
            gaps_already_watched: 0,
            plan_id: None,
            entry_lines: 0,
            pending_lines: 0,
            exit_lines: 0,
            skips: 0,
            skipped_reason: None,
            focus: 0,
            ladder_caught_up: false,
            first_live_sessions_left: 0,
            risk_multiplier: "1".to_owned(),
            risk_pct_in_force: "0.500".to_owned(),
        }
    }

    fn as_detail(&self) -> Vec<(String, OrderedJson)> {
        vec![
            ("session_date".to_owned(), s(iso(self.session_date))),
            ("stage".to_owned(), s(self.stage.clone())),
            (
                "levels".to_owned(),
                object([
                    ("refreshed", int(self.levels_refreshed)),
                    ("unchanged", int(self.levels_unchanged)),
                ]),
            ),
            (
                "gaps".to_owned(),
                object([
                    ("universe", int(self.universe)),
                    ("quotes", int(self.quotes_pulled)),
                    ("candidates", strings(self.gap_candidates.clone())),
                    ("added", int(self.gaps_added)),
                    ("already_watched", int(self.gaps_already_watched)),
                ]),
            ),
            (
                "plan".to_owned(),
                object([
                    ("plan_id", crate::pyjson::opt_s(self.plan_id.clone())),
                    ("entries", int(self.entry_lines)),
                    ("pending", int(self.pending_lines)),
                    ("exits", int(self.exit_lines)),
                    ("skips", int(self.skips)),
                    ("risk_multiplier", s(self.risk_multiplier.clone())),
                    ("risk_pct_in_force", s(self.risk_pct_in_force.clone())),
                ]),
            ),
            ("focus".to_owned(), int(self.focus)),
            (
                "ladder_caught_up".to_owned(),
                OrderedJson::Bool(self.ladder_caught_up),
            ),
            (
                "first_live_sessions_left".to_owned(),
                int(self.first_live_sessions_left),
            ),
            (
                "skipped_reason".to_owned(),
                crate::pyjson::opt_s(self.skipped_reason.clone()),
            ),
        ]
    }
}

// --- 08:50: the levels ---------------------------------------------------------------

/// `_latest_adj_factors`: `instrument_id -> adj_factor` of the latest bar at or before `on`
/// (`Decimal(1)` for a null factor).
async fn latest_adj_factors(
    conn: &mut PgConnection,
    instrument_ids: &[i64],
    on: NaiveDate,
) -> Result<std::collections::BTreeMap<i64, Decimal>, WorkerError> {
    if instrument_ids.is_empty() {
        return Ok(std::collections::BTreeMap::new());
    }
    let rows = sqlx::query(
        "SELECT DISTINCT ON (ohlcv_daily.instrument_id) ohlcv_daily.instrument_id, \
         ohlcv_daily.adj_factor FROM ohlcv_daily WHERE ohlcv_daily.instrument_id = ANY($1) \
         AND ohlcv_daily.date <= $2 ORDER BY ohlcv_daily.instrument_id, ohlcv_daily.date DESC",
    )
    .bind(instrument_ids)
    .bind(on)
    .fetch_all(&mut *conn)
    .await?;
    let mut latest = std::collections::BTreeMap::new();
    for row in &rows {
        let id: i64 = row.try_get("instrument_id")?;
        let factor = baskfy_db::row::opt_decimal(row, "adj_factor")?.unwrap_or(Decimal::ONE);
        latest.insert(id, factor);
    }
    Ok(latest)
}

/// `_rescale(level, was=, now=)`: an exchange price written under factor `was`, re-expressed
/// under factor `now`, quantized to `0.01` (half-even, Python's context).
fn rescale(
    level: Option<Decimal>,
    was: Decimal,
    now: Decimal,
) -> Result<Option<Decimal>, WorkerError> {
    let Some(level) = level else {
        return Ok(None);
    };
    if was <= Decimal::ZERO || now <= Decimal::ZERO || was == now {
        return Ok(Some(level));
    }
    let value = WideDecimal::from(level)
        .mul(&WideDecimal::from(was))
        .div(&WideDecimal::from(now))
        .map_err(dec_err)?
        .quantize(-2, Rounding::HalfEven)
        .map_err(dec_err)?;
    Ok(Some(value.try_to_decimal().map_err(dec_err)?))
}

/// `refresh_levels`: re-express every non-`MANUAL` watch row's levels under the latest bar's
/// adjustment factor → `(refreshed, unchanged)`.
async fn refresh_levels(
    conn: &mut PgConnection,
    user_id: i64,
    on: NaiveDate,
) -> Result<(i64, i64), WorkerError> {
    let rows = sqlx::query(
        "SELECT sw_watch.id, sw_watch.instrument_id, sw_watch.setup, sw_watch.setup_daily_date, \
         sw_watch.trigger, sw_watch.stop_ref FROM sw_watch WHERE sw_watch.user_id = $1 \
         AND sw_watch.state = 'WATCHING' AND sw_watch.source != 'MANUAL'",
    )
    .bind(user_id)
    .fetch_all(&mut *conn)
    .await?;
    if rows.is_empty() {
        return Ok((0, 0));
    }
    struct Watched {
        id: i64,
        instrument_id: i64,
        setup: String,
        setup_daily_date: Option<NaiveDate>,
        trigger: Option<Decimal>,
        stop_ref: Option<Decimal>,
    }
    let mut watching = Vec::with_capacity(rows.len());
    for row in &rows {
        watching.push(Watched {
            id: row.try_get("id")?,
            instrument_id: row.try_get("instrument_id")?,
            setup: row.try_get("setup")?,
            setup_daily_date: row.try_get("setup_daily_date")?,
            trigger: baskfy_db::row::opt_decimal(row, "trigger")?,
            stop_ref: baskfy_db::row::opt_decimal(row, "stop_ref")?,
        });
    }
    let ids: Vec<i64> = watching.iter().map(|w| w.instrument_id).collect();
    let factors = latest_adj_factors(conn, &ids, on).await?;
    let (mut refreshed, mut unchanged) = (0, 0);
    for row in &watching {
        let detected = match row.setup_daily_date {
            None => None,
            Some(day) => {
                sqlx::query(
                    "SELECT sw_setup_daily.trigger, sw_setup_daily.stop_ref, \
                 sw_setup_daily.adj_factor FROM sw_setup_daily \
                 WHERE sw_setup_daily.user_id = $1 AND sw_setup_daily.instrument_id = $2 \
                 AND sw_setup_daily.setup = $3 AND sw_setup_daily.date = $4",
                )
                .bind(user_id)
                .bind(row.instrument_id)
                .bind(&row.setup)
                .bind(day)
                .fetch_optional(&mut *conn)
                .await?
            }
        };
        let latest = factors.get(&row.instrument_id).copied();
        let was = match &detected {
            Some(d) => baskfy_db::row::opt_decimal(d, "adj_factor")?,
            None => None,
        };
        let (Some(detected), Some(factor_now), Some(factor_was)) = (detected, latest, was) else {
            unchanged += 1;
            continue;
        };
        let trigger = rescale(
            baskfy_db::row::opt_decimal(&detected, "trigger")?,
            factor_was,
            factor_now,
        )?;
        let stop_ref = rescale(
            baskfy_db::row::opt_decimal(&detected, "stop_ref")?,
            factor_was,
            factor_now,
        )?;
        if trigger == row.trigger && stop_ref == row.stop_ref {
            unchanged += 1;
            continue;
        }
        // The ORM sends only the columns whose value changed (and the `onupdate` stamp).
        let mut sets: Vec<&str> = Vec::new();
        if trigger != row.trigger {
            sets.push("trigger = $1");
        }
        if stop_ref != row.stop_ref {
            sets.push("stop_ref = $2");
        }
        sqlx::query(&format!(
            "UPDATE sw_watch SET {}, updated_at = now() WHERE sw_watch.id = $3",
            sets.join(", ")
        ))
        .bind(trigger)
        .bind(stop_ref)
        .bind(row.id)
        .execute(&mut *conn)
        .await?;
        refreshed += 1;
    }
    Ok((refreshed, unchanged))
}

// --- 09:16: the gaps -----------------------------------------------------------------

/// `minutes_since_preopen`: never below one.
fn minutes_since_preopen(now: NaiveTime) -> i64 {
    let minutes = |t: NaiveTime| {
        i64::from(chrono::Timelike::hour(&t)) * 60 + i64::from(chrono::Timelike::minute(&t))
    };
    (minutes(now) - minutes(preopen_start())).max(1)
}

/// `gap_price`: `ohlc.open` when positive, else the last print.
fn gap_price(quote: &QuoteRecord) -> Decimal {
    match quote.open {
        Some(open) if open > Decimal::ZERO => open,
        _ => quote.last_price,
    }
}

/// `evaluate_gaps`: every name whose quote says it is gapping on volume.
fn evaluate_gaps<'a>(
    names: &'a [LiquidName],
    quotes: &'a [QuoteRecord],
    at: NaiveTime,
    config: &SwingConfig,
) -> Result<Vec<(&'a LiquidName, &'a QuoteRecord, LiveGapVerdict)>, WorkerError> {
    // `{quote.symbol: quote for quote in quotes}`: the last quote of a symbol wins.
    let mut by_symbol: std::collections::BTreeMap<&str, &QuoteRecord> =
        std::collections::BTreeMap::new();
    for q in quotes {
        by_symbol.insert(q.symbol.as_str(), q);
    }
    let minutes = minutes_since_preopen(at);
    let mut found = Vec::new();
    for name in names {
        let Some(quote) = by_symbol.get(name.symbol.as_str()) else {
            continue;
        };
        let prev_close = match quote.prev_close {
            Some(p) if !p.is_zero() => p,
            _ => name.prev_close,
        };
        let verdict = live_gap(
            prev_close,
            gap_price(quote),
            quote.volume,
            name.avg_daily_volume,
            minutes,
            &config.opening_range,
        )
        .map_err(core_err)?;
        if verdict.is_candidate {
            found.push((name, *quote, verdict));
        }
    }
    Ok(found)
}

/// `_sessions_ahead` (the premarket's): the trading day `count` sessions after `on`, or
/// `on + count + 2` calendar days when the calendar runs out first.
async fn sessions_ahead(
    conn: &mut PgConnection,
    on: NaiveDate,
    n: i64,
) -> Result<NaiveDate, WorkerError> {
    let rows: Vec<(NaiveDate,)> = sqlx::query_as(
        "SELECT trading_day.date FROM trading_day WHERE trading_day.exchange_id = $1 \
         AND trading_day.is_trading_day IS true AND trading_day.date > $2 \
         ORDER BY trading_day.date LIMIT $3",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(on)
    .bind(n)
    .fetch_all(&mut *conn)
    .await?;
    if count(rows.len()) < n {
        return Ok(on + Duration::days(n + 2));
    }
    rows.last()
        .map(|(d,)| *d)
        .ok_or_else(|| WorkerError::python("IndexError", "list index out of range"))
}

/// `watch_live_gaps` → `(candidate symbols, added, already watched)`.
async fn watch_live_gaps(
    conn: &mut PgConnection,
    user_id: i64,
    on: NaiveDate,
    names: &[LiquidName],
    quotes: &[QuoteRecord],
    at: NaiveTime,
    config: &SwingConfig,
) -> Result<(Vec<String>, i64, i64), WorkerError> {
    let found = evaluate_gaps(names, quotes, at, config)?;
    if found.is_empty() {
        return Ok((Vec::new(), 0, 0));
    }
    let watched_rows: Vec<(i64,)> = sqlx::query_as(
        "SELECT sw_watch.instrument_id FROM sw_watch WHERE sw_watch.user_id = $1 \
         AND sw_watch.state = 'WATCHING'",
    )
    .bind(user_id)
    .fetch_all(&mut *conn)
    .await?;
    let mut watched: BTreeSet<i64> = watched_rows.into_iter().map(|(id,)| id).collect();
    let expiry = sessions_ahead(conn, on, config.ep.valid_bars).await?;
    let (mut added, mut already) = (0, 0);
    for (name, quote, verdict) in &found {
        if watched.contains(&name.instrument_id) {
            already += 1;
            continue;
        }
        let note = format!(
            "live gap {}% on {}x pace at {}",
            verdict.gap_pct,
            verdict.volume_pace,
            at.format("%H:%M")
        );
        let score = live_gap_score(verdict, &config.ep).map_err(core_err)?;
        sqlx::query(
            "INSERT INTO sw_watch (user_id, instrument_id, setup, source, added_on, expires_on, \
             trigger, stop_ref, setup_daily_date, note, catalyst, state, score, adr_pct) \
             VALUES ($1, $2, 'EP', $3, $4, $5, $6, NULL, NULL, $7, NULL, 'WATCHING', $8, $9)",
        )
        .bind(user_id)
        .bind(name.instrument_id)
        .bind(LIVE_GAP_SOURCE)
        .bind(on)
        .bind(expiry)
        .bind(quote.last_price)
        .bind(&note)
        .bind(score)
        .bind(name.adr_pct)
        .execute(&mut *conn)
        .await?;
        watched.insert(name.instrument_id);
        added += 1;
    }
    Ok((
        found.iter().map(|(n, _, _)| n.symbol.clone()).collect(),
        added,
        already,
    ))
}

// --- the plan --------------------------------------------------------------------------

/// The day's `sw_market_daily` row, as the plans read it.
struct Market {
    gate: MarketGate,
    tier: ExposureTier,
    row: eod::MarketRow,
}

fn gate_of(text: &str) -> Result<MarketGate, WorkerError> {
    match text {
        "GREEN" => Ok(MarketGate::Green),
        "AMBER" => Ok(MarketGate::Amber),
        "RED" => Ok(MarketGate::Red),
        other => Err(WorkerError::python(
            "ValueError",
            format!("'{other}' is not a valid MarketGate"),
        )),
    }
}

/// `MarketGate.value`.
fn gate_value(gate: MarketGate) -> &'static str {
    match gate {
        MarketGate::Green => "GREEN",
        MarketGate::Amber => "AMBER",
        MarketGate::Red => "RED",
    }
}

async fn market_row(
    conn: &mut PgConnection,
    user_id: i64,
    on: NaiveDate,
) -> Result<Option<Market>, WorkerError> {
    let row = sqlx::query(
        "SELECT date, gate, exposure_level, max_open_positions, \
         max_exposure_pct::float8 AS max_exposure_pct, new_entries_allowed, drawdown_locked, \
         detail FROM sw_market_daily WHERE user_id = $1 AND date = $2",
    )
    .bind(user_id)
    .bind(on)
    .fetch_optional(&mut *conn)
    .await?;
    let Some(row) = row else {
        return Ok(None);
    };
    let level: i16 = row.try_get("exposure_level")?;
    let positions: i16 = row.try_get("max_open_positions")?;
    let gate_text: String = row.try_get("gate")?;
    let locked: Option<bool> = row.try_get("drawdown_locked")?;
    Ok(Some(Market {
        gate: gate_of(&gate_text)?,
        tier: ExposureTier {
            level: i64::from(level),
            max_open_positions: i64::from(positions),
            max_exposure_pct: row.try_get("max_exposure_pct")?,
            new_entries_allowed: row.try_get("new_entries_allowed")?,
            drawdown_locked: locked.unwrap_or(false),
        },
        row: eod::MarketRow {
            date: row.try_get("date")?,
            gate: gate_text,
            exposure_level: i64::from(level),
            detail: baskfy_db::row::opt_jsonb(&row, "detail")?,
        },
    }))
}

/// The `sw_config` columns the plans read beyond the swing config: the capital and the
/// first-live countdown.
async fn sleeve_row(
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<(Option<Decimal>, i64), WorkerError> {
    let row = sqlx::query(
        "SELECT sleeve_capital_inr, first_live_sessions_left FROM sw_config WHERE user_id = $1",
    )
    .bind(user_id)
    .fetch_optional(&mut *conn)
    .await?;
    let Some(row) = row else {
        return Ok((None, 0));
    };
    let left: i16 = row.try_get("first_live_sessions_left")?;
    Ok((
        Some(baskfy_db::row::decimal(&row, "sleeve_capital_inr")?),
        i64::from(left),
    ))
}

/// What a plan build counted.
struct Built {
    plan_id: String,
    entry_lines: i64,
    pending_lines: i64,
    exit_lines: i64,
    skips: i64,
    watching: i64,
    left: i64,
    multiplier: Decimal,
    risk_pct_in_force: String,
}

/// The shared body of `build_morning_plan` and `build_intraday_plan`: exits re-derived on
/// `exits_on`, the watchlist read on `exits_on`, entries for `as_of`, the plan stored.
#[allow(clippy::too_many_arguments)]
async fn build_plan(
    conn: &mut PgConnection,
    user_id: i64,
    as_of: NaiveDate,
    exits_on: NaiveDate,
    market: &Market,
    config: &SwingConfig,
    source: &str,
    now: DateTime<Utc>,
    execution_enabled: bool,
) -> Result<Built, WorkerError> {
    let (exits, _, _) = eod::manage_open_positions(conn, user_id, exits_on, config).await?;
    let (items, mut instrument_ids) = eod::watch_items(conn, user_id, exits_on).await?;
    let (capital, left) = sleeve_row(conn, user_id).await?;
    let account = eod::sleeve_account(conn, user_id, capital).await?;
    let multiplier =
        first_live_multiplier(left, execution_enabled, &config.sizing).map_err(core_err)?;
    let risk = eod::risk_pct_in_force(config, multiplier)?;
    let entries = build_entries(
        as_of,
        &items,
        &account,
        market.gate,
        &market.tier,
        config,
        0,
        multiplier,
    )
    .map_err(core_err)?;
    let kind_count =
        |kind: LineKind, lines: &[PlanLine]| count(lines.iter().filter(|l| l.kind == kind).count());
    let entry_lines = kind_count(LineKind::BuyOnTrigger, &entries.lines);
    let pending_lines = kind_count(LineKind::PendingRange, &entries.lines);
    let exit_lines = count(exits.len());
    let skips = count(entries.skipped.len());
    let watching = count(items.len());
    let plan = eod::assemble(
        as_of,
        market.gate,
        market.tier.clone(),
        entries.lines,
        exits,
        entries.skipped,
    )?;
    instrument_ids.extend(eod::held_instrument_ids(conn, user_id).await?);
    let plan_id = eod::store_plan(conn, &plan, user_id, source, &instrument_ids, now).await?;
    Ok(Built {
        plan_id,
        entry_lines,
        pending_lines,
        exit_lines,
        skips,
        watching,
        left,
        multiplier,
        risk_pct_in_force: risk,
    })
}

/// `catch_up_ladder` (A10): settle the previous session only if the evening never did.
async fn catch_up_ladder(
    conn: &mut PgConnection,
    user_id: i64,
    last_session: NaiveDate,
    config: &SwingConfig,
    now: DateTime<Utc>,
) -> Result<bool, WorkerError> {
    let Some(market) = market_row(conn, user_id, last_session).await? else {
        return Ok(false);
    };
    let settled = market
        .row
        .detail
        .as_ref()
        .and_then(|d| d.get("ladder"))
        .filter(|l| matches!(l, OrderedJson::Object(_)))
        .and_then(|l| l.get("settled_by"))
        .is_some_and(truthy);
    if settled {
        return Ok(false);
    }
    log_warning(&format!(
        "no settlement record for {}; the evening did not run — settling it now as a catch-up",
        iso(last_session)
    ));
    eod::settle_ladder(conn, user_id, &market.row, config, now).await?;
    Ok(true)
}

/// Python truthiness of a JSON value.
fn truthy(value: &OrderedJson) -> bool {
    match value {
        OrderedJson::Null => false,
        OrderedJson::Bool(b) => *b,
        OrderedJson::Int(i) => *i != 0,
        OrderedJson::UInt(u) => *u != 0,
        OrderedJson::Float(f) => *f != 0.0,
        OrderedJson::Str(s) => !s.is_empty(),
        OrderedJson::Array(a) => !a.is_empty(),
        OrderedJson::Object(o) => !o.is_empty(),
        // A big integer is out of `i64`'s range, so never zero.
        OrderedJson::BigInt(_) => true,
    }
}

/// `recent_trading_days(session, as_of, 1)`'s last day.
async fn last_session_before(
    conn: &mut PgConnection,
    day: NaiveDate,
) -> Result<Option<NaiveDate>, WorkerError> {
    let days =
        crate::tasks::swing_eod::recent_trading_days(conn, day - Duration::days(1), 1).await?;
    Ok(days.last().copied())
}

/// `run_swing_premarket(session, outcome, session_date, user_id=, stage=, ep_premarket_enabled=,
/// quotes=, now=, execution_enabled=)` → `report.as_detail()`.
///
/// # Errors
/// The database's and the quote source's (the task rolls back).
#[allow(clippy::too_many_arguments)]
pub(super) async fn run_swing_premarket(
    ctx: &TaskContext,
    conn: &mut PgConnection,
    session_date: NaiveDate,
    user_id: i64,
    stage: &str,
    ep_premarket_enabled: bool,
    quotes: Option<&KiteProvider>,
    execution_enabled: bool,
) -> Result<Vec<(String, OrderedJson)>, WorkerError> {
    // `now=dt.datetime.now(tz=IST).replace(tzinfo=None)`, read once by the task.
    let stamp: NaiveDateTime = ctx.now_ist();
    let mut report = PremarketReport::new(session_date, stage);
    let resolved = load_swing_config(conn, user_id).await?;

    let Some(last_session) = last_session_before(conn, session_date).await? else {
        report.skipped_reason =
            Some("no trading day before the session date in the calendar".to_owned());
        return Ok(report.as_detail());
    };

    (report.levels_refreshed, report.levels_unchanged) =
        refresh_levels(conn, user_id, last_session).await?;
    if stage == STAGE_LEVELS {
        return Ok(report.as_detail());
    }

    match (ep_premarket_enabled, quotes) {
        (true, Some(source)) => {
            let names = liquid_universe(conn, last_session, &resolved).await?;
            report.universe = count(names.len());
            let pulled = if names.is_empty() {
                Vec::new()
            } else {
                let symbols: Vec<&str> = names.iter().map(|n| n.symbol.as_str()).collect();
                source.quotes(&symbols, "NSE").await?
            };
            report.quotes_pulled = count(pulled.len());
            (
                report.gap_candidates,
                report.gaps_added,
                report.gaps_already_watched,
            ) = watch_live_gaps(
                conn,
                user_id,
                session_date,
                &names,
                &pulled,
                stamp.time(),
                &resolved,
            )
            .await?;
        }
        (true, None) => {
            log_warning("BASKFY_SWING_EP_PREMARKET_ENABLED is on but no quote source was wired")
        }
        _ => {}
    }
    report.focus = eod::refresh_focus(conn, user_id, &resolved).await?;

    // `stamp.replace(tzinfo=IST_OFFSET)`: the naive IST clock as an aware instant.
    let built_at = (stamp - Duration::minutes(super::super::context::IST_OFFSET_MINUTES)).and_utc();
    report.ladder_caught_up =
        catch_up_ladder(conn, user_id, last_session, &resolved, built_at).await?;

    // `build_morning_plan`.
    match market_row(conn, user_id, last_session).await? {
        None => {
            report.skipped_reason = Some(format!(
                "no sw_market_daily row for {}; the detectors have not run",
                iso(last_session)
            ));
        }
        Some(market) => {
            let built = build_plan(
                conn,
                user_id,
                session_date,
                last_session,
                &market,
                &resolved,
                "MORNING",
                built_at,
                execution_enabled,
            )
            .await?;
            report.first_live_sessions_left = built.left;
            report.risk_multiplier = built.multiplier.to_string();
            report.risk_pct_in_force = built.risk_pct_in_force;
            report.entry_lines = built.entry_lines;
            report.pending_lines = built.pending_lines;
            report.exit_lines = built.exit_lines;
            report.skips = built.skips;
            report.plan_id = Some(built.plan_id);
        }
    }
    Ok(report.as_detail())
}

/// `build_intraday_plan(session, user_id=, on=, now=, execution_enabled=)` →
/// `IntradayPlanReport.as_detail()`. Does not settle the ladder and does not count a session.
///
/// # Errors
/// The database's.
pub(super) async fn build_intraday_plan(
    ctx: &TaskContext,
    conn: &mut PgConnection,
    user_id: i64,
    on: NaiveDate,
    now: DateTime<Utc>,
    execution_enabled: bool,
) -> Result<OrderedJson, WorkerError> {
    let _ = ctx;
    let resolved = load_swing_config(conn, user_id).await?;
    let report = |plan_id: Option<String>,
                  counters: [i64; 7],
                  skipped_reason: Option<String>,
                  detail: Vec<(String, OrderedJson)>| {
        let mut out = vec![
            ("plan_id".to_owned(), crate::pyjson::opt_s(plan_id)),
            ("as_of".to_owned(), s(iso(on))),
            ("source".to_owned(), s(INTRADAY)),
            ("watch_added".to_owned(), int(counters[0])),
            ("watch_expired".to_owned(), int(counters[1])),
            ("watching".to_owned(), int(counters[2])),
            ("entry_lines".to_owned(), int(counters[3])),
            ("pending_lines".to_owned(), int(counters[4])),
            ("exit_lines".to_owned(), int(counters[5])),
            ("skips".to_owned(), int(counters[6])),
            (
                "skipped_reason".to_owned(),
                crate::pyjson::opt_s(skipped_reason),
            ),
        ];
        for (k, v) in detail {
            crate::pyjson::update(&mut out, &k, v);
        }
        OrderedJson::Object(out)
    };
    let Some(market) = market_row(conn, user_id, on).await? else {
        return Ok(report(
            None,
            [0; 7],
            Some(format!(
                "no sw_market_daily row for {}; run the scan before the plan",
                iso(on)
            )),
            Vec::new(),
        ));
    };
    let watched = eod::auto_watch(conn, user_id, on, &resolved).await?;
    let built = build_plan(
        conn,
        user_id,
        on,
        on,
        &market,
        &resolved,
        INTRADAY,
        now,
        execution_enabled,
    )
    .await?;
    let detail = vec![
        (
            "risk_multiplier".to_owned(),
            s(built.multiplier.to_string()),
        ),
        ("risk_pct_in_force".to_owned(), s(built.risk_pct_in_force)),
        ("first_live_sessions_left".to_owned(), int(built.left)),
        ("gate".to_owned(), s(gate_value(market.gate))),
        ("exposure_level".to_owned(), int(market.tier.level)),
    ];
    Ok(report(
        Some(built.plan_id),
        [
            watched.added,
            watched.expired,
            built.watching,
            built.entry_lines,
            built.pending_lines,
            built.exit_lines,
            built.skips,
        ],
        None,
        detail,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dec(text: &str) -> Decimal {
        text.parse().unwrap_or_default()
    }

    fn gapper() -> LiquidName {
        LiquidName {
            instrument_id: 1,
            symbol: "GAPPER".to_owned(),
            avg_daily_volume: Decimal::from(1_000_000),
            prev_close: Decimal::from(100),
            adr_pct: Decimal::ZERO,
            turnover_avg: None,
        }
    }

    fn quote(last: &str, volume: i64, prev_close: Option<&str>, open: Option<&str>) -> QuoteRecord {
        let mut f = baskfy_providers::records::QuoteFields::new("GAPPER", dec(last));
        f.volume = volume;
        f.prev_close = prev_close.map(dec);
        f.open = open.map(dec);
        QuoteRecord::new(f).unwrap_or_else(|_| unreachable!())
    }

    fn nine_oh_nine() -> NaiveTime {
        NaiveTime::from_hms_opt(9, 9, 0).unwrap_or(NaiveTime::MIN)
    }

    fn gaps(quotes: &[QuoteRecord], at: NaiveTime) -> Vec<(String, Decimal)> {
        let names = [gapper()];
        evaluate_gaps(&names, quotes, at, &SwingConfig::default())
            .unwrap_or_default()
            .into_iter()
            .map(|(n, _, v)| (n.symbol.clone(), v.gap_pct))
            .collect()
    }

    #[test]
    fn a_ten_percent_gap_on_four_times_pace_is_a_candidate() {
        assert_eq!(
            gaps(&[quote("112", 100_000, None, None)], nine_oh_nine()),
            [("GAPPER".to_owned(), dec("12.00"))]
        );
    }

    #[test]
    fn the_gap_alone_is_not_enough_nor_the_volume_alone() {
        assert!(gaps(&[quote("112", 50_000, None, None)], nine_oh_nine()).is_empty());
        assert!(gaps(&[quote("105", 100_000, None, None)], nine_oh_nine()).is_empty());
    }

    #[test]
    fn the_exchange_previous_close_beats_the_bar_table() {
        assert_eq!(
            gaps(&[quote("56", 100_000, Some("50"), None)], nine_oh_nine()),
            [("GAPPER".to_owned(), dec("12.00"))]
        );
        assert_eq!(
            gaps(&[quote("112", 100_000, None, None)], nine_oh_nine()).len(),
            1
        );
    }

    #[test]
    fn the_gap_is_measured_from_ohlc_open_when_the_quote_carries_it() {
        let at = NaiveTime::from_hms_opt(9, 16, 0).unwrap_or(NaiveTime::MIN);
        let q = quote("104", 200_000, Some("100"), Some("112"));
        assert_eq!(
            gaps(std::slice::from_ref(&q), at),
            [("GAPPER".to_owned(), dec("12.00"))]
        );
        assert_eq!(gap_price(&q), dec("112"));
        assert_eq!(gap_price(&quote("112", 1, None, None)), dec("112"));
        assert_eq!(gap_price(&quote("111", 1, None, Some("0"))), dec("111"));
    }

    #[test]
    fn a_name_with_no_quote_is_not_a_candidate() {
        assert!(gaps(&[], nine_oh_nine()).is_empty());
    }

    #[test]
    fn a_level_is_rescaled_only_when_the_factor_moved() {
        assert_eq!(
            rescale(Some(dec("110.00")), dec("1"), dec("1")).unwrap_or_default(),
            Some(dec("110.00"))
        );
        assert_eq!(
            rescale(Some(dec("110.00")), dec("1"), dec("0.5")).unwrap_or_default(),
            Some(dec("220.00"))
        );
        assert_eq!(
            rescale(Some(dec("100")), dec("1"), dec("3")).unwrap_or_default(),
            Some(dec("33.33"))
        );
        assert_eq!(rescale(None, dec("1"), dec("2")).unwrap_or_default(), None);
        assert_eq!(
            rescale(Some(dec("5")), dec("0"), dec("2")).unwrap_or_default(),
            Some(dec("5"))
        );
    }

    #[test]
    fn the_preopen_pace_clock_never_reads_below_one_minute() {
        let at = |h, m| NaiveTime::from_hms_opt(h, m, 0).unwrap_or(NaiveTime::MIN);
        assert_eq!(minutes_since_preopen(at(9, 0)), 1);
        assert_eq!(minutes_since_preopen(at(8, 50)), 1);
        assert_eq!(minutes_since_preopen(at(9, 16)), 16);
        assert_eq!(minutes_since_preopen(at(9, 9)), 9);
    }
}
