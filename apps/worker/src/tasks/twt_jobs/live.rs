//! LV8 — **Scan now** during a session: today's provisional bar from Kite quotes, the same
//! detector over today-so-far, and a `LIVE` plan from what it signals. Ports the TWT half of
//! `baskfy_worker.tasks.live_scan` (`LiveName`, `BarBuild`, `provisional_daily_bars`,
//! `live_names`, `live_frame`, `_in_flight_live_plan`, `build_live_plan_twt`, `run_twt_live`,
//! `LiveReport`) and `swing_scan_now.last_factors`.
//!
//! Every row it writes is stamped `provisional` (the nightly's real bar replaces them), the book's
//! management does not run on a bar that has not closed, and no exits are planned. **It places
//! nothing**: the plan's lines are `PROPOSED`; only the desk (a click, or its own flagged
//! `twt-auto`, TW17/TW19 — never this worker) turns one into an order.

use std::collections::{BTreeMap, BTreeSet};

use baskfy_core::twt::plan::{assemble, build_entries};
use baskfy_db::OrderedJson;
use baskfy_providers::kite::KiteProvider;
use baskfy_providers::records::QuoteRecord;
use chrono::{DateTime, Duration, NaiveDate, Utc};
use polars::prelude::{DataFrame, IntoColumn, NamedFrom, Series};
use rust_decimal::Decimal;
use sqlx::{PgConnection, Row as _};

use super::evening::{candidates_for, gate_for_session, store_plan};
use super::scan::ScanDecision;
use super::sleeve::{book_state, config_row, load_sleeve, slot_multiplier};
use crate::error::WorkerError;
use crate::pyjson::{int, object, s};
use crate::steps::StepOutcome;
use crate::steps::sleeves::for_twt_jobs::{
    core_err, date_column, day_number, load_twt_config, load_twt_universe, pl_err,
    run_detect_twt_for,
};

/// `SOURCE_LIVE`.
pub(crate) const SOURCE_LIVE: &str = "LIVE";

/// `IN_FLIGHT_LINE_STATES`, sorted (the query binds `sorted(...)`).
const IN_FLIGHT_LINE_STATES: [&str; 3] = ["CONFIRMED", "FILLED", "SENT"];

/// `ORDERED_TODAY_STATES`, sorted.
const ORDERED_TODAY_STATES: [&str; 4] = ["CONFIRMED", "FILLED", "PARTIAL", "SENT"];

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

/// One provisional bar, in `twt.BAR_SCHEMA`'s columns.
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

/// `pl.DataFrame(rows, schema=twt.BAR_SCHEMA)`.
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
        Series::new("is_etf".into(), vec![Some(false); n]).into_column(),
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

/// `provisional_daily_bars`: one bar per name that answered, in the adjusted space (`raw x
/// factor`), the raw last price kept as `close_raw`; a name with no factor is skipped and
/// counted, never invented.
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
pub(super) async fn live_names(
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

/// `swing_scan_now.last_factors`: `instrument_id -> adj_factor` of the bar published for `on`
/// (a null factor reads `1.0`).
pub(super) async fn last_factors(
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
pub(super) struct PlanBuild {
    pub(super) plan_id: Option<String>,
    pub(super) entries: i64,
    pub(super) kept: bool,
    pub(super) already_ordered: i64,
}

/// `_in_flight_live_plan`: today's `LIVE` plan's id when any of its lines is in flight.
async fn in_flight_live_plan(
    conn: &mut PgConnection,
    user_id: i64,
    trade_date: NaiveDate,
) -> Result<Option<String>, WorkerError> {
    let row: Option<(String,)> = sqlx::query_as(
        "SELECT p.plan_id::text FROM tw_plan p JOIN tw_plan_line l ON l.plan_id = p.id \
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

/// `build_live_plan_twt`: entries only, sized on the live close; a plan with a line at the
/// broker is kept, a name with an order today is not offered again.
pub(super) async fn build_live_plan_twt(
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
            already_ordered: 0,
        });
    }
    let gate = gate_for_session(conn, user_id, trade_date).await?;
    // `twt.load_twt_config`: the detector's (its two stops), as live_scan imports it.
    let config = load_twt_config(conn, user_id).await?;
    let settings_row = config_row(conn, user_id).await?;
    let sleeve = load_sleeve(conn, user_id, trade_date).await?;
    let equity = sleeve.quantized_equity()?;
    let book = book_state(
        conn,
        user_id,
        trade_date,
        Some(trade_date),
        Some(trade_date),
    )
    .await?;
    let multiplier = slot_multiplier(conn, user_id, execution_enabled, &config).await?;
    let candidates = candidates_for(conn, user_id, trade_date).await?;
    let ordered: Vec<(i64,)> = sqlx::query_as(
        "SELECT instrument_id FROM tw_order WHERE user_id = $1 AND signal_date = $2 \
         AND state = ANY($3)",
    )
    .bind(user_id)
    .bind(trade_date)
    .bind(&ORDERED_TODAY_STATES[..])
    .fetch_all(&mut *conn)
    .await?;
    let ordered: BTreeSet<i64> = ordered.into_iter().map(|r| r.0).collect();
    let offered: Vec<_> = candidates
        .iter()
        .filter(|c| !ordered.contains(&c.instrument_id))
        .cloned()
        .collect();
    let (entries, skips) = build_entries(
        &offered,
        gate,
        equity,
        &book,
        &config,
        multiplier,
        settings_row.as_ref().map(|r| r.max_open_positions),
        None,
    )
    .map_err(core_err)?;
    let dropped = i64::try_from(candidates.len() - offered.len()).unwrap_or(0);
    if entries.is_empty() {
        return Ok(PlanBuild {
            plan_id: None,
            entries: 0,
            kept: false,
            already_ordered: dropped,
        });
    }
    let hhmm = (now + Duration::minutes(330)).format("%H:%M").to_string();
    let entries: Vec<_> = entries
        .into_iter()
        .map(|mut line| {
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
        already_ordered: dropped,
    })
}

/// `run_twt_live(...)`: the provisional bar, the detector, the `LIVE` plan; returns
/// `LiveReport.as_detail()` (Python's `dict` merge: the detector's own keys update the report's
/// in place, so `entries` and `signals` are the funnel's).
pub(crate) async fn run_twt_live(
    conn: &mut PgConnection,
    user_id: i64,
    decision: &ScanDecision,
    quotes: &KiteProvider,
    now: DateTime<Utc>,
    execution_enabled: bool,
) -> Result<OrderedJson, WorkerError> {
    let config = load_twt_config(conn, user_id).await?;
    let universe = load_twt_universe(conn, &config).await?;
    let names = live_names(conn, &universe).await?;
    let build = live_frame(conn, &names, decision, quotes).await?;
    run_twt_live_on(conn, user_id, decision, &build, now, execution_enabled).await
}

pub(crate) async fn run_twt_live_on(
    conn: &mut PgConnection,
    user_id: i64,
    decision: &ScanDecision,
    build: &BarBuild,
    now: DateTime<Utc>,
    execution_enabled: bool,
) -> Result<OrderedJson, WorkerError> {
    let mut outcome = StepOutcome::default();
    let signals = run_detect_twt_for(
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
        build_live_plan_twt(conn, user_id, decision.session_date, execution_enabled, now).await?
    } else {
        PlanBuild::default()
    };
    let mut pairs: Vec<(String, OrderedJson)> = vec![
        (
            "session_date".to_owned(),
            s(decision.session_date.format("%Y-%m-%d").to_string()),
        ),
        ("provisional".to_owned(), OrderedJson::Bool(true)),
        ("signals".to_owned(), int(signals)),
        (
            "plan_id".to_owned(),
            built.plan_id.clone().map_or(OrderedJson::Null, s),
        ),
        ("entries".to_owned(), int(built.entries)),
    ];
    pairs.extend(build.as_detail());
    pairs.push(("plan_kept".to_owned(), OrderedJson::Bool(built.kept)));
    pairs.push(("already_ordered".to_owned(), int(built.already_ordered)));
    pairs.push(("status".to_owned(), s(outcome.status.value())));
    pairs.extend(outcome.detail);
    Ok(object(pairs))
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

    #[test]
    fn a_bar_is_built_in_the_adjusted_space_and_the_misses_are_counted() {
        // test_live_scan.py's provisional_daily_bars: raw x factor, close_raw raw, a zero side
        // takes the last price, a missing factor and a bad price are counted, not invented.
        let names = vec![
            LiveName {
                instrument_id: 1,
                symbol: "AAA".to_owned(),
            },
            LiveName {
                instrument_id: 2,
                symbol: "BBB".to_owned(),
            },
            LiveName {
                instrument_id: 3,
                symbol: "CCC".to_owned(),
            },
            LiveName {
                instrument_id: 4,
                symbol: "DDD".to_owned(),
            },
        ];
        let quotes = vec![
            quote("AAA", 10_000, None, -5),
            quote("BBB", 20_000, Some(19_000), 10),
            quote("CCC", 0, None, 1),
            quote("ZZZ", 100, None, 1),
        ];
        let factors = BTreeMap::from([(1, 0.5), (3, 1.0)]);
        let day = NaiveDate::from_ymd_opt(2026, 10, 30).unwrap_or_default();
        let build = provisional_daily_bars(&names, &quotes, &factors, day)
            .unwrap_or_else(|_| unreachable!());
        assert_eq!(build.quotes, 4);
        assert_eq!(build.frame.height(), 1);
        assert_eq!(
            (build.no_quote, build.no_factor, build.bad_price),
            (1, 1, 1)
        );
        let close = build
            .frame
            .column("close")
            .ok()
            .and_then(|c| c.f64().ok().and_then(|ca| ca.get(0)));
        assert_eq!(close, Some(50.0));
        let low = build
            .frame
            .column("low")
            .ok()
            .and_then(|c| c.f64().ok().and_then(|ca| ca.get(0)));
        // A zero low takes the last price; low = min(last, last) x factor.
        assert_eq!(low, Some(50.0));
        let volume = build
            .frame
            .column("volume")
            .ok()
            .and_then(|c| c.f64().ok().and_then(|ca| ca.get(0)));
        assert_eq!(volume, Some(0.0));
    }

    #[test]
    fn the_frame_has_the_detectors_columns() {
        let frame = bar_frame(Vec::new()).unwrap_or_default();
        let names: Vec<String> = frame
            .get_column_names()
            .iter()
            .map(|n| n.to_string())
            .collect();
        assert_eq!(
            names,
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
                "adj_factor",
                "is_etf"
            ]
        );
    }
}
