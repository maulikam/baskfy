//! LV8 — the desk's **Re-detect** during a session: today's provisional bar from Kite quotes, the
//! same detector over today-so-far, and a `LIVE` plan of `BUY_AT_MARKET` lines from what it
//! signals. Ports the VBT half of `baskfy_worker.tasks.live_scan` (`LiveName`, `BarBuild`,
//! `provisional_daily_bars` in VBT's schema — no `is_etf` column — `live_names`, `live_frame`,
//! `_in_flight_live_plan`, `build_live_plan_vbt`, `run_vbt_live`, `LiveReport`) and
//! `swing_scan_now.last_factors`.
//!
//! Every row it writes is stamped `provisional` (the nightly's real bar replaces them); no exits
//! are planned from a bar that has not closed. **It places nothing**: the plan's lines are
//! `PROPOSED`, and VBT has no auto-execute flag — a person confirms each line on the desk.

use std::collections::{BTreeMap, BTreeSet};

use baskfy_core::vbt::plan::{BookState, LineKind, assemble, build_entries};
use baskfy_core::vbt::sizing::first_live_multiplier;
use baskfy_db::OrderedJson;
use baskfy_providers::kite::KiteProvider;
use baskfy_providers::records::QuoteRecord;
use chrono::{DateTime, Duration, NaiveDate, Utc};
use polars::prelude::{DataFrame, IntoColumn, NamedFrom, Series};
use rust_decimal::Decimal;
use sqlx::{PgConnection, Row as _};

use super::book::naked_positions;
use super::evening::{
    IST_MINUTES, candidates_for, config_row, entries_confirmed_today, gate_for_session, iso,
    store_plan,
};
use super::rescan::ScanDecision;
use super::sleeve::{LIVE_ORDER_STATES, live_orders, load_sleeve, open_positions, symbols_for};
use crate::error::WorkerError;
use crate::pyjson::{int, object, s};
use crate::steps::StepOutcome;
use crate::steps::sleeves::for_vbt_jobs::{
    core_err, date_column, day_number, load_vbt_config, load_vbt_universe, pl_err,
    run_detect_vbt_for,
};

/// `SOURCE_LIVE`.
pub(crate) const SOURCE_LIVE: &str = "LIVE";

/// `IN_FLIGHT_LINE_STATES`, sorted (the query binds `sorted(...)`).
const IN_FLIGHT_LINE_STATES: [&str; 3] = ["CONFIRMED", "FILLED", "SENT"];

/// `LiveName`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct LiveName {
    pub(crate) instrument_id: i64,
    pub(crate) symbol: String,
}

/// `BarBuild`: the frame and what became of the quotes on the way.
#[derive(Debug, Clone)]
pub(crate) struct BarBuild {
    pub(crate) frame: DataFrame,
    pub(crate) quotes: i64,
    pub(crate) no_quote: i64,
    pub(crate) no_factor: i64,
    pub(crate) bad_price: i64,
}

impl BarBuild {
    fn empty(quotes: i64) -> Result<Self, WorkerError> {
        Ok(Self {
            frame: bar_frame(Vec::new())?,
            quotes,
            no_quote: 0,
            no_factor: 0,
            bad_price: 0,
        })
    }

    /// `as_detail()`.
    fn as_detail(&self) -> Vec<(String, OrderedJson)> {
        vec![
            ("quotes".to_owned(), int(self.quotes)),
            (
                "bars_built".to_owned(),
                int(i64::try_from(self.frame.height()).unwrap_or(i64::MAX)),
            ),
            (
                "skipped".to_owned(),
                object([
                    ("no_quote", int(self.no_quote)),
                    ("no_factor", int(self.no_factor)),
                    ("bad_price", int(self.bad_price)),
                ]),
            ),
        ]
    }
}

/// One provisional bar, in `vbt.BAR_SCHEMA`'s columns.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct LiveBar {
    pub(crate) instrument_id: i64,
    pub(crate) symbol: String,
    pub(crate) date: NaiveDate,
    pub(crate) open: f64,
    pub(crate) high: f64,
    pub(crate) low: f64,
    pub(crate) close: f64,
    pub(crate) close_raw: f64,
    pub(crate) volume: f64,
    pub(crate) upper_circuit: Option<f64>,
    pub(crate) adj_factor: f64,
}

/// `pl.DataFrame(rows, schema=vbt.BAR_SCHEMA)` — no `is_etf` (VBT's loader has none).
fn bar_frame(rows: Vec<LiveBar>) -> Result<DataFrame, WorkerError> {
    let n = rows.len();
    let col = |name: &str, v: Vec<Option<f64>>| Series::new(name.into(), v).into_column();
    let columns = vec![
        Series::new(
            "instrument_id".into(),
            rows.iter().map(|r| r.instrument_id).collect::<Vec<i64>>(),
        )
        .into_column(),
        Series::new(
            "symbol".into(),
            rows.iter()
                .map(|r| r.symbol.clone())
                .collect::<Vec<String>>(),
        )
        .into_column(),
        date_column(
            "date",
            rows.iter().map(|r| Some(day_number(r.date))).collect(),
        )?,
        col("open", rows.iter().map(|r| Some(r.open)).collect()),
        col("high", rows.iter().map(|r| Some(r.high)).collect()),
        col("low", rows.iter().map(|r| Some(r.low)).collect()),
        col("close", rows.iter().map(|r| Some(r.close)).collect()),
        col(
            "close_raw",
            rows.iter().map(|r| Some(r.close_raw)).collect(),
        ),
        col("volume", rows.iter().map(|r| Some(r.volume)).collect()),
        col(
            "upper_circuit",
            rows.iter().map(|r| r.upper_circuit).collect(),
        ),
        col(
            "adj_factor",
            rows.iter().map(|r| Some(r.adj_factor)).collect(),
        ),
    ];
    DataFrame::new(n, columns).map_err(|e| pl_err(&e))
}

/// `float(Decimal)`: correctly rounded, as Python's.
fn money(value: Decimal) -> f64 {
    value.to_string().parse::<f64>().unwrap_or(f64::NAN)
}

/// `_money(v) or last`: a missing or zero side takes the last price.
fn or_last(value: Option<Decimal>, last: f64) -> f64 {
    match value.map(money) {
        Some(v) if v != 0.0 => v,
        _ => last,
    }
}

/// `provisional_daily_bars(..., schema=vbt.BAR_SCHEMA)`: one bar per name that answered, in the
/// adjusted space (`raw x factor`), the raw last price kept as `close_raw`; a name with no factor
/// or no positive price is skipped and counted, never invented.
pub(crate) fn provisional_daily_bars(
    names: &[LiveName],
    quotes: &[QuoteRecord],
    factors: &BTreeMap<i64, f64>,
    on: NaiveDate,
) -> Result<BarBuild, WorkerError> {
    let by_symbol: BTreeMap<&str, &LiveName> =
        names.iter().map(|n| (n.symbol.as_str(), n)).collect();
    let mut build = BarBuild::empty(i64::try_from(quotes.len()).unwrap_or(i64::MAX))?;
    let mut rows = Vec::new();
    let mut answered: BTreeSet<&str> = BTreeSet::new();
    for quote in quotes {
        let Some(name) = by_symbol.get(quote.symbol.as_str()) else {
            continue;
        };
        answered.insert(name.symbol.as_str());
        let factor = match factors.get(&name.instrument_id) {
            Some(f) if *f > 0.0 => *f,
            _ => {
                build.no_factor += 1;
                continue;
            }
        };
        let last = money(quote.last_price);
        if last.is_nan() || last <= 0.0 {
            build.bad_price += 1;
            continue;
        }
        let open = or_last(quote.open, last);
        let high = or_last(quote.high, last).max(last);
        let low = or_last(quote.low, last).min(last);
        rows.push(LiveBar {
            instrument_id: name.instrument_id,
            symbol: name.symbol.clone(),
            date: on,
            open: open * factor,
            high: high * factor,
            low: low * factor,
            close: last * factor,
            close_raw: last,
            volume: quote.volume.max(0) as f64,
            upper_circuit: quote.upper_circuit.map(|c| money(c) * factor),
            adj_factor: factor,
        });
    }
    build.no_quote = i64::try_from(by_symbol.len() - answered.len()).unwrap_or(0);
    if !rows.is_empty() {
        build.frame = bar_frame(rows)?;
    }
    Ok(build)
}

/// `live_names`: the universe's `(id, symbol)`, sorted by symbol.
async fn live_names(
    conn: &mut PgConnection,
    ids: &BTreeSet<i64>,
) -> Result<Vec<LiveName>, WorkerError> {
    if ids.is_empty() {
        return Ok(Vec::new());
    }
    let wanted: Vec<i64> = ids.iter().copied().collect();
    let rows = sqlx::query("SELECT id, symbol FROM instrument WHERE id = ANY($1)")
        .bind(&wanted)
        .fetch_all(&mut *conn)
        .await?;
    let mut names = rows
        .iter()
        .map(|row| {
            Ok(LiveName {
                instrument_id: row.try_get("id")?,
                symbol: row.try_get("symbol")?,
            })
        })
        .collect::<Result<Vec<_>, WorkerError>>()?;
    names.sort_by(|a, b| a.symbol.cmp(&b.symbol));
    Ok(names)
}

/// `swing_scan_now.last_factors`: `instrument_id -> adj_factor` of the bar published for `on`.
async fn last_factors(
    conn: &mut PgConnection,
    on: NaiveDate,
    ids: &[i64],
) -> Result<BTreeMap<i64, f64>, WorkerError> {
    if ids.is_empty() {
        return Ok(BTreeMap::new());
    }
    let rows: Vec<(i64, Option<f64>)> = sqlx::query_as(
        "SELECT instrument_id, adj_factor::float8 FROM ohlcv_daily \
         WHERE date = $1 AND instrument_id = ANY($2)",
    )
    .bind(on)
    .bind(ids)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows
        .into_iter()
        .map(|(id, factor)| (id, factor.unwrap_or(1.0)))
        .collect())
}

/// `live_frame`: the universe as of the last published session, quoted, one bar each for today.
async fn live_frame(
    conn: &mut PgConnection,
    names: &[LiveName],
    decision: &ScanDecision,
    quotes: &KiteProvider,
) -> Result<BarBuild, WorkerError> {
    if names.is_empty() {
        return BarBuild::empty(0);
    }
    let ids: Vec<i64> = names.iter().map(|n| n.instrument_id).collect();
    let factors = last_factors(conn, decision.published_as_of, &ids).await?;
    let symbols: Vec<&str> = names.iter().map(|n| n.symbol.as_str()).collect();
    let records = quotes
        .quotes(&symbols, "NSE")
        .await
        .map_err(WorkerError::Provider)?;
    provisional_daily_bars(names, &records, &factors, decision.session_date)
}

/// `PlanBuild`.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
struct PlanBuild {
    plan_id: Option<String>,
    entries: i64,
    kept: bool,
}

/// `_in_flight_live_plan`: today's `LIVE` plan's id when any of its lines is in flight.
async fn in_flight_live_plan(
    conn: &mut PgConnection,
    user_id: i64,
    trade_date: NaiveDate,
) -> Result<Option<String>, WorkerError> {
    let row: Option<(String,)> = sqlx::query_as(
        "SELECT p.plan_id::text FROM vb_plan p JOIN vb_plan_line l ON l.plan_id = p.id \
         WHERE p.user_id = $1 AND p.session_date = $2 AND p.source = $3 AND l.state = ANY($4) \
         LIMIT 1",
    )
    .bind(user_id)
    .bind(trade_date)
    .bind(SOURCE_LIVE)
    .bind(&IN_FLIGHT_LINE_STATES[..])
    .fetch_optional(&mut *conn)
    .await?;
    Ok(row.map(|r| r.0))
}

/// `build_live_plan_vbt`: the evening's parts without its sweep, exit queue or settle; every
/// entry re-kinded `BUY_AT_MARKET` (same size, same stop). A plan with a line at the broker is
/// kept; a name with a working order is already `build_entries`' refusal.
async fn build_live_plan_vbt(
    conn: &mut PgConnection,
    user_id: i64,
    trade_date: NaiveDate,
    execution_enabled: bool,
    now: DateTime<Utc>,
) -> Result<PlanBuild, WorkerError> {
    if let Some(kept) = in_flight_live_plan(conn, user_id, trade_date).await? {
        return Ok(PlanBuild {
            plan_id: Some(kept),
            entries: 0,
            kept: true,
        });
    }
    let config = load_vbt_config(conn, user_id).await?;
    let positions = open_positions(conn, user_id).await?;
    let orders = live_orders(conn, user_id).await?;
    let ids: BTreeSet<i64> = positions
        .iter()
        .map(|p| p.instrument_id)
        .chain(orders.iter().map(|o| o.instrument_id))
        .collect();
    let symbols = symbols_for(conn, &ids.into_iter().collect::<Vec<_>>()).await?;
    let sleeve = load_sleeve(conn, user_id, trade_date)
        .await?
        .quantize()
        .map_err(core_err)?;
    let gate = gate_for_session(conn, user_id, trade_date).await?;
    let candidates = candidates_for(conn, user_id, trade_date).await?;
    let cfg = config_row(conn, user_id).await?;
    let multiplier = first_live_multiplier(
        cfg.map_or(0, |c| c.first_live_sessions_left),
        execution_enabled,
        &config.sizing,
    )
    .map_err(core_err)?;
    let mut open_entry_counts: BTreeMap<i64, i64> = BTreeMap::new();
    for p in &positions {
        *open_entry_counts.entry(p.instrument_id).or_insert(0) += 1;
    }
    let book = BookState {
        open_instrument_ids: positions.iter().map(|p| p.instrument_id).collect(),
        working_instrument_ids: orders
            .iter()
            .filter(|o| LIVE_ORDER_STATES.contains(&o.state.as_str()))
            .map(|o| o.instrument_id)
            .collect(),
        open_exposure_inr: sleeve.open_exposure_inr().map_err(core_err)?,
        cash_available_inr: sleeve.cash_available_inr().map_err(core_err)?,
        // A LIVE plan executes today.
        entries_already_this_session: entries_confirmed_today(conn, user_id, Some(trade_date))
            .await?,
        positions_naked_of_gtt: naked_positions(&positions, &symbols),
        open_entry_counts,
    };
    let equity = sleeve.equity_inr().map_err(core_err)?;
    let (entries, skips) = build_entries(
        &candidates,
        gate,
        equity,
        &book,
        &config,
        multiplier,
        cfg.map(|c| c.max_open_positions),
        None,
    )
    .map_err(core_err)?;
    if entries.is_empty() {
        return Ok(PlanBuild::default());
    }
    let hhmm = (now + Duration::minutes(IST_MINUTES))
        .format("%H:%M")
        .to_string();
    let entries: Vec<_> = entries
        .into_iter()
        .map(|mut line| {
            line.kind = LineKind::BuyAtMarket;
            line.note = format!("live scan {hhmm}: buy now, at market; {}", line.note);
            line
        })
        .collect();
    let plan = assemble(trade_date, gate, equity, &[], &entries, &skips, SOURCE_LIVE);
    let plan_id = store_plan(conn, user_id, trade_date, SOURCE_LIVE, &plan, now).await?;
    Ok(PlanBuild {
        plan_id: Some(plan_id),
        entries: i64::try_from(entries.len()).unwrap_or(i64::MAX),
        kept: false,
    })
}

/// `run_vbt_live(...)`: the provisional bar, the detector, the `LIVE` plan; returns
/// `LiveReport.as_detail()` (Python's dict merge: the detector's own keys — `signals`,
/// `provisional` — update the report's in place).
pub(crate) async fn run_vbt_live(
    conn: &mut PgConnection,
    user_id: i64,
    decision: &ScanDecision,
    quotes: &KiteProvider,
    now: DateTime<Utc>,
    execution_enabled: bool,
) -> Result<OrderedJson, WorkerError> {
    let config = load_vbt_config(conn, user_id).await?;
    let universe = load_vbt_universe(conn, &config).await?;
    let names = live_names(conn, &universe).await?;
    let build = live_frame(conn, &names, decision, quotes).await?;
    let mut outcome = StepOutcome::default();
    let signals = run_detect_vbt_for(
        conn,
        &mut outcome,
        decision.session_date,
        user_id,
        None,
        Some(&build.frame),
        true,
    )
    .await?;
    let built = if signals != 0 {
        build_live_plan_vbt(conn, user_id, decision.session_date, execution_enabled, now).await?
    } else {
        PlanBuild::default()
    };
    let mut pairs: Vec<(String, OrderedJson)> = vec![
        ("session_date".to_owned(), s(iso(decision.session_date))),
        ("provisional".to_owned(), OrderedJson::Bool(true)),
        ("signals".to_owned(), int(signals)),
        (
            "plan_id".to_owned(),
            built.plan_id.clone().map_or(OrderedJson::Null, s),
        ),
        ("entries".to_owned(), int(built.entries)),
    ];
    for (k, v) in build.as_detail() {
        crate::pyjson::update(&mut pairs, &k, v);
    }
    crate::pyjson::update(&mut pairs, "plan_kept", OrderedJson::Bool(built.kept));
    crate::pyjson::update(&mut pairs, "status", s(outcome.status.value()));
    for (k, v) in outcome.detail {
        crate::pyjson::update(&mut pairs, &k, v);
    }
    Ok(OrderedJson::Object(pairs))
}

#[cfg(test)]
mod tests {
    use baskfy_providers::records::QuoteFields;

    use super::*;

    fn quote(symbol: &str, last: i64, open: Option<i64>, volume: i64) -> QuoteRecord {
        let mut f = QuoteFields::new(symbol, Decimal::new(last, 2));
        f.open = open.map(|o| Decimal::new(o, 2));
        f.high = Some(Decimal::new(last + 100, 2));
        f.low = Some(Decimal::ZERO);
        f.volume = volume;
        QuoteRecord::new(f).unwrap_or_else(|_| unreachable!())
    }

    fn names() -> Vec<LiveName> {
        ["AAA", "BBB", "CCC", "DDD"]
            .iter()
            .zip(1..)
            .map(|(s, id)| LiveName {
                instrument_id: id,
                symbol: (*s).to_owned(),
            })
            .collect()
    }

    fn col(frame: &DataFrame, name: &str) -> Option<f64> {
        frame
            .column(name)
            .ok()
            .and_then(|c| c.f64().ok().and_then(|ca| ca.get(0)))
    }

    #[test]
    fn bars_in_the_vbt_schema_carry_no_etf_column() {
        // test_live_scan.py::test_bars_in_the_vbt_schema_carry_no_etf_column.
        let frame = bar_frame(Vec::new()).unwrap_or_default();
        let got: Vec<String> = frame
            .get_column_names()
            .iter()
            .map(|n| n.to_string())
            .collect();
        assert_eq!(
            got,
            [
                "instrument_id",
                "symbol",
                "date",
                "open",
                "high",
                "low",
                "close",
                "close_raw",
                "volume",
                "upper_circuit",
                "adj_factor"
            ]
        );
    }

    #[test]
    fn bars_skip_and_count_a_name_with_no_quote_no_factor_or_no_price() {
        // test_live_scan.py::test_bars_skip_and_count_a_name_with_no_quote_no_factor_or_no_price
        // and ::test_bars_one_per_quoted_name_in_the_sleeve_schema_adjusted_with_the_raw_close.
        let quotes = vec![
            quote("AAA", 10_000, None, -5),
            quote("BBB", 20_000, Some(19_000), 10),
            quote("CCC", 0, None, 1),
            quote("ZZZ", 100, None, 1),
        ];
        let factors = BTreeMap::from([(1, 0.5), (3, 1.0)]);
        let day = NaiveDate::from_ymd_opt(2026, 10, 30).unwrap_or_default();
        let build = provisional_daily_bars(&names(), &quotes, &factors, day)
            .unwrap_or_else(|_| unreachable!());
        assert_eq!(build.quotes, 4);
        assert_eq!(build.frame.height(), 1);
        assert_eq!(
            (build.no_quote, build.no_factor, build.bad_price),
            (1, 1, 1)
        );
        assert_eq!(col(&build.frame, "close"), Some(50.0));
        assert_eq!(col(&build.frame, "close_raw"), Some(100.0));
        assert_eq!(col(&build.frame, "volume"), Some(0.0));
    }

    #[test]
    fn bars_take_the_last_price_for_a_missing_side_and_keep_high_above_last() {
        // test_live_scan.py::test_bars_take_the_last_price_for_a_missing_ohlc_side_and_keep_
        // high_above_last.
        let mut f = QuoteFields::new("AAA", Decimal::new(10_500, 2));
        f.open = None;
        f.high = Some(Decimal::new(10_000, 2));
        f.low = None;
        let q = QuoteRecord::new(f).unwrap_or_else(|_| unreachable!());
        let factors = BTreeMap::from([(1, 1.0)]);
        let day = NaiveDate::from_ymd_opt(2026, 10, 30).unwrap_or_default();
        let build = provisional_daily_bars(&names()[..1], &[q], &factors, day)
            .unwrap_or_else(|_| unreachable!());
        assert_eq!(col(&build.frame, "open"), Some(105.0));
        assert_eq!(col(&build.frame, "high"), Some(105.0));
        assert_eq!(col(&build.frame, "low"), Some(105.0));
    }
}
