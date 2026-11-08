//! Index-led regime I/O (Maulik, 6 Nov 2026): reads `index_master` / `index_bar_daily` /
//! `index_constituents` (0060), the scanners' own output rows (`sw_setup_daily`,
//! `tw_signal_daily`, `vb_signal_daily`), and writes `swing_regime_run` + `swing_candidates`
//! (0061). **Law 2:** candidate rows only — nothing here builds, confirms or sends a plan, and no
//! execution or auto-execute flag is read.

use std::collections::{BTreeMap, HashMap};
use std::sync::OnceLock;

use baskfy_core::regime::{
    BENCHMARK, IndexSeries, RegimeConfig, RegimeMode, RegimeVerdict, UniverseMember,
    candidate_score, rank_score, scan, universe,
};
use chrono::{DateTime, Days, NaiveDate, Utc};
use rust_decimal::Decimal;
use rust_decimal::prelude::FromPrimitive;
use serde_json::json;
use sqlx::PgConnection;

use crate::error::WorkerError;

/// Calendar days of index bars a single-date verdict loads.
const LOOKBACK_DAYS: u64 = 90;

/// The process's regime settings (the worker's env + dotenv, else defaults).
pub fn config() -> &'static RegimeConfig {
    static CFG: OnceLock<RegimeConfig> = OnceLock::new();
    CFG.get_or_init(|| {
        crate::settings::WorkerSettings::from_env()
            .map(|s| s.regime)
            .or_else(|_| RegimeConfig::from_lookup(&|k| std::env::var(k).ok()))
            .unwrap_or_default()
    })
}

fn ve(msg: impl Into<String>) -> WorkerError {
    WorkerError::python("ValueError", msg.into())
}

async fn table_exists(conn: &mut PgConnection, name: &str) -> Result<bool, WorkerError> {
    let (ok,): (bool,) = sqlx::query_as("SELECT to_regclass($1) IS NOT NULL")
        .bind(name)
        .fetch_one(&mut *conn)
        .await?;
    Ok(ok)
}

/// Are 0060's index tables there?
///
/// # Errors
/// The database's.
pub async fn index_tables_ready(conn: &mut PgConnection) -> Result<bool, WorkerError> {
    Ok(table_exists(conn, "index_bar_daily").await?
        && table_exists(conn, "index_constituents").await?)
}

/// Is 0061 there?
///
/// # Errors
/// The database's.
pub async fn candidate_tables_ready(conn: &mut PgConnection) -> Result<bool, WorkerError> {
    Ok(table_exists(conn, "swing_regime_run").await?
        && table_exists(conn, "swing_candidates").await?)
}

/// Enabled live indices' closes in `[from, to]`, and the benchmark's.
///
/// # Errors
/// The database's.
pub async fn load_series(
    conn: &mut PgConnection,
    cfg: &RegimeConfig,
    from: NaiveDate,
    to: NaiveDate,
) -> Result<(Vec<IndexSeries>, Option<IndexSeries>), WorkerError> {
    let rows: Vec<(String, String, NaiveDate, f64)> = sqlx::query_as(
        "SELECT m.name, m.grp, b.date, b.close::float8 FROM index_master m \
         JOIN index_bar_daily b ON b.index_id = m.id \
         WHERE b.date BETWEEN $1 AND $2 AND (m.live OR lower(m.name) = lower($3)) \
         ORDER BY m.name, b.date",
    )
    .bind(from)
    .bind(to)
    .bind(BENCHMARK)
    .fetch_all(&mut *conn)
    .await?;
    let mut by_name: BTreeMap<String, (String, Vec<(NaiveDate, f64)>)> = BTreeMap::new();
    for (name, grp, date, close) in rows {
        by_name
            .entry(name)
            .or_insert_with(|| (grp, Vec::new()))
            .1
            .push((date, close));
    }
    let mut series = Vec::new();
    let mut bench = None;
    for (name, (grp, closes)) in by_name {
        let s = IndexSeries {
            name: name.clone(),
            closes,
        };
        if name.eq_ignore_ascii_case(BENCHMARK) {
            bench = Some(s.clone());
        }
        if cfg.has_group(&grp) {
            series.push(s);
        }
    }
    Ok((series, bench))
}

/// index name → constituent symbols, for `names`.
///
/// # Errors
/// The database's.
pub async fn load_constituents(
    conn: &mut PgConnection,
    names: &[String],
) -> Result<BTreeMap<String, Vec<String>>, WorkerError> {
    let rows: Vec<(String, String)> = sqlx::query_as(
        "SELECT m.name, c.symbol FROM index_constituents c JOIN index_master m ON m.id = c.index_id \
         WHERE m.name = ANY($1) ORDER BY m.name, c.symbol",
    )
    .bind(names)
    .fetch_all(&mut *conn)
    .await?;
    let mut out: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for (name, symbol) in rows {
        out.entry(name).or_default().push(symbol);
    }
    Ok(out)
}

/// `(index name, Kite tradingsymbol)` of the enabled live indices and the benchmark.
///
/// # Errors
/// The database's.
pub async fn quote_symbols(
    conn: &mut PgConnection,
    cfg: &RegimeConfig,
) -> Result<Vec<(String, String)>, WorkerError> {
    let rows: Vec<(String, String, String)> = sqlx::query_as(
        "SELECT name, grp, kite_tradingsymbol FROM index_master \
         WHERE live AND kite_tradingsymbol IS NOT NULL ORDER BY sort_order",
    )
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows
        .into_iter()
        .filter(|(n, g, _)| cfg.has_group(g) || n.eq_ignore_ascii_case(BENCHMARK))
        .map(|(n, _, s)| (n, s))
        .collect())
}

/// Today's live level replaces or extends each series' last close.
fn overlay(series: &mut IndexSeries, today: NaiveDate, level: f64) {
    match series.closes.last_mut() {
        Some(last) if last.0 == today => last.1 = level,
        Some(last) if last.0 > today => {}
        _ => series.closes.push((today, level)),
    }
}

/// The verdict as of `on`, with optional live levels (index name → level) for `on`.
/// `None` when nothing could be evaluated (no tables, no bars): the caller falls back to legacy.
///
/// # Errors
/// The database's.
pub async fn verdict_on(
    conn: &mut PgConnection,
    cfg: &RegimeConfig,
    on: NaiveDate,
    live: Option<&HashMap<String, f64>>,
) -> Result<Option<(RegimeVerdict, Vec<IndexSeries>)>, WorkerError> {
    if !index_tables_ready(conn).await? {
        return Ok(None);
    }
    let from = on
        .checked_sub_days(Days::new(LOOKBACK_DAYS))
        .ok_or_else(|| ve("date out of range"))?;
    let (mut series, mut bench) = load_series(conn, cfg, from, on).await?;
    if let Some(levels) = live {
        for s in series.iter_mut().chain(bench.iter_mut()) {
            if let Some(level) = levels.get(&s.name) {
                overlay(s, on, *level);
            }
        }
    }
    let v = scan(&series, bench.as_ref(), Some(on), cfg.min_leading);
    if v.evaluated == 0 {
        return Ok(None);
    }
    Ok(Some((v, series)))
}

/// The swing gate's index half in index-led mode.
#[derive(Debug, Clone, PartialEq)]
pub struct GateVerdict {
    /// Leading count reached the minimum.
    pub open: bool,
    /// Leaders.
    pub leading: usize,
    /// Indices read.
    pub evaluated: usize,
    /// The minimum.
    pub min_leading: usize,
    /// `bars` or `intraday`.
    pub source: &'static str,
}

/// For the detector's market row: today's latest intraday run when `provisional`, else the
/// verdict from published bars at `on`. `None` in legacy mode or when nothing can be read.
///
/// # Errors
/// The database's.
pub async fn gate_verdict(
    conn: &mut PgConnection,
    on: NaiveDate,
    provisional: bool,
) -> Result<Option<GateVerdict>, WorkerError> {
    let cfg = config();
    if cfg.mode != RegimeMode::IndexLed {
        return Ok(None);
    }
    if provisional && candidate_tables_ready(conn).await? {
        let row: Option<(bool, i32, i32, i32)> = sqlx::query_as(
            "SELECT gate_open, leading_count, evaluated, min_leading FROM swing_regime_run \
             WHERE mode = 'index_led' AND kind = 'intraday' AND as_of = $1 \
             ORDER BY run_at DESC LIMIT 1",
        )
        .bind(on)
        .fetch_optional(&mut *conn)
        .await?;
        if let Some((open, leading, evaluated, min)) = row {
            return Ok(Some(GateVerdict {
                open,
                leading: usize::try_from(leading).unwrap_or(0),
                evaluated: usize::try_from(evaluated).unwrap_or(0),
                min_leading: usize::try_from(min).unwrap_or(0),
                source: "intraday",
            }));
        }
    }
    Ok(verdict_on(conn, cfg, on, None)
        .await?
        .map(|(v, _)| GateVerdict {
            open: v.gate_open,
            leading: v.leading.len(),
            evaluated: v.evaluated,
            min_leading: v.min_leading,
            source: "bars",
        }))
}

/// One scanner hit before the universe filter.
#[derive(Debug, Clone)]
struct Hit {
    symbol: String,
    scanner: &'static str,
    setup: String,
    entry: Option<Decimal>,
    stop: Option<Decimal>,
    scanner_score: f64,
}

type RawHit = (
    String,
    String,
    Option<Decimal>,
    Option<Decimal>,
    Option<f64>,
);

/// Qullamaggie's flag / EP (the swing detector's long setups) on its latest date ≤ `on`.
async fn swing_hits(
    conn: &mut PgConnection,
    user_id: i64,
    on: NaiveDate,
) -> Result<Vec<Hit>, WorkerError> {
    let rows: Vec<RawHit> = sqlx::query_as(
        "SELECT i.symbol, s.setup || ':' || s.status, s.trigger::numeric, s.stop_ref::numeric, \
         s.score::float8 FROM sw_setup_daily s JOIN instrument i ON i.id = s.instrument_id \
         WHERE s.user_id = $1 AND s.setup <> 'PARABOLIC_SHORT' AND s.date = \
         (SELECT max(date) FROM sw_setup_daily WHERE user_id = $1 AND date <= $2)",
    )
    .bind(user_id)
    .bind(on)
    .fetch_all(&mut *conn)
    .await?;
    Ok(rows
        .into_iter()
        .map(|(symbol, setup, entry, stop, score)| Hit {
            symbol,
            scanner: "QULLA",
            setup,
            entry,
            stop,
            scanner_score: score.unwrap_or(0.0) / 100.0,
        })
        .collect())
}

/// TWT / VBT `SIGNAL` rows on their latest date ≤ `on`, scored by rank (turnover order).
async fn signal_hits(
    conn: &mut PgConnection,
    user_id: i64,
    on: NaiveDate,
    table: &str,
    entry_col: &str,
    stop_col: &str,
    scanner: &'static str,
) -> Result<Vec<Hit>, WorkerError> {
    let sql = format!(
        "SELECT i.symbol, '{scanner}', t.{entry_col}::numeric, t.{stop_col}::numeric, \
         t.rank_key::float8 FROM {table} t JOIN instrument i ON i.id = t.instrument_id \
         WHERE t.user_id = $1 AND t.state = 'SIGNAL' AND t.date = \
         (SELECT max(date) FROM {table} WHERE user_id = $1 AND date <= $2) \
         ORDER BY t.rank_key DESC NULLS LAST, i.symbol"
    );
    let rows: Vec<RawHit> = sqlx::query_as(&sql)
        .bind(user_id)
        .bind(on)
        .fetch_all(&mut *conn)
        .await?;
    let n = rows.len();
    Ok(rows
        .into_iter()
        .enumerate()
        .map(|(i, (symbol, setup, entry, stop, _))| Hit {
            symbol,
            scanner,
            setup,
            entry,
            stop,
            scanner_score: rank_score(i, n),
        })
        .collect())
}

fn dec(x: f64, dp: u32) -> Option<Decimal> {
    Decimal::from_f64(x).map(|d| d.round_dp(dp))
}

/// A candidate row as written.
#[derive(Debug, Clone)]
pub struct Candidate {
    /// Symbol.
    pub symbol: String,
    /// `QULLA` / `TWT` / `VBT`.
    pub scanner: &'static str,
    /// The scanner's setup label.
    pub setup: String,
    /// Entry.
    pub entry: Option<Decimal>,
    /// Stop.
    pub stop: Option<Decimal>,
    /// `(entry − stop) / entry × 100`.
    pub risk_pct: Option<Decimal>,
    /// The member it came through.
    pub member: UniverseMember,
    /// `scanner score × universe_score`.
    pub score: f64,
}

/// What a run did.
#[derive(Debug, Clone)]
pub struct RunReport {
    /// Row id (`None` when skipped).
    pub run_id: Option<i64>,
    /// Why it was skipped.
    pub skipped: Option<String>,
    /// The verdict.
    pub gate_open: bool,
    /// Leaders.
    pub leading: Vec<String>,
    /// Universe size.
    pub universe: usize,
    /// Candidate rows written.
    pub candidates: usize,
}

impl RunReport {
    fn skipped(why: &str) -> Self {
        Self {
            run_id: None,
            skipped: Some(why.to_owned()),
            gate_open: false,
            leading: Vec::new(),
            universe: 0,
            candidates: 0,
        }
    }

    /// The report as JSON.
    #[must_use]
    pub fn as_json(&self) -> serde_json::Value {
        json!({
            "run_id": self.run_id, "skipped": self.skipped, "gate_open": self.gate_open,
            "leading": self.leading, "universe": self.universe, "candidates": self.candidates,
        })
    }
}

/// One regime run: verdict, universe, the scanners' rows filtered to it, persisted.
///
/// # Errors
/// The database's.
pub async fn run_candidates(
    conn: &mut PgConnection,
    run_at: DateTime<Utc>,
    as_of: NaiveDate,
    kind: &str,
    user_id: Option<i64>,
    live: Option<&HashMap<String, f64>>,
) -> Result<RunReport, WorkerError> {
    let cfg = config();
    // Informational rows: index_led and per_stock both run it; never a gate outside index_led.
    if cfg.mode == RegimeMode::Legacy {
        return Ok(RunReport::skipped("BASKFY_REGIME_MODE=legacy"));
    }
    if !candidate_tables_ready(conn).await? {
        return Ok(RunReport::skipped(
            "swing_candidates (0061) is not migrated",
        ));
    }
    let Some((verdict, _)) = verdict_on(conn, cfg, as_of, live).await? else {
        return Ok(RunReport::skipped("no index bars to read"));
    };
    let names: Vec<String> = verdict.leading.iter().map(|l| l.index.clone()).collect();
    let members = universe(&verdict.leading, &load_constituents(conn, &names).await?);
    let mut hits = Vec::new();
    if let Some(uid) = user_id {
        hits.extend(swing_hits(conn, uid, as_of).await?);
        hits.extend(
            signal_hits(
                conn,
                uid,
                as_of,
                "tw_signal_daily",
                "entry_reference_close",
                "stop_preview",
                "TWT",
            )
            .await?,
        );
        hits.extend(
            signal_hits(
                conn,
                uid,
                as_of,
                "vb_signal_daily",
                "limit_price",
                "stop_price",
                "VBT",
            )
            .await?,
        );
        let symbols: Vec<String> = members.keys().cloned().collect();
        hits.extend(
            crate::regime_patterns::pattern_hits(conn, &symbols, as_of)
                .await?
                .into_iter()
                .map(|(symbol, scanner, setup, entry, stop, scanner_score)| Hit {
                    symbol,
                    scanner,
                    setup,
                    entry,
                    stop,
                    scanner_score,
                }),
        );
    }
    let mut rows: Vec<Candidate> = hits
        .into_iter()
        .filter_map(|h| {
            let member = members.get(&h.symbol)?.clone();
            let risk_pct = match (h.entry, h.stop) {
                (Some(e), Some(s)) if !e.is_zero() => {
                    Some(((e - s) / e * Decimal::from(100)).round_dp(2))
                }
                _ => None,
            };
            Some(Candidate {
                symbol: h.symbol,
                scanner: h.scanner,
                setup: h.setup,
                entry: h.entry,
                stop: h.stop,
                risk_pct,
                score: candidate_score(h.scanner_score, member.score),
                member,
            })
        })
        .collect();
    rows.sort_by(|a, b| {
        b.score
            .total_cmp(&a.score)
            .then_with(|| a.symbol.cmp(&b.symbol))
    });
    let leading_json = serde_json::Value::Array(
        verdict
            .leading
            .iter()
            .map(|l| json!({"index": l.index, "strength": round4(l.strength), "rs": l.rs.map(round4)}))
            .collect(),
    );
    let i32_of = |n: usize| i32::try_from(n).unwrap_or(i32::MAX);
    let (run_id,): (i64,) = sqlx::query_as(
        "INSERT INTO swing_regime_run (run_at, mode, kind, as_of, gate_open, leading_count, \
         min_leading, evaluated, universe_size, \"leading\") \
         VALUES ($1, 'index_led', $2, $3, $4, $5, $6, $7, $8, CAST($9 AS JSONB)) RETURNING id",
    )
    .bind(run_at)
    .bind(kind)
    .bind(as_of)
    .bind(verdict.gate_open)
    .bind(i32_of(verdict.leading.len()))
    .bind(i32_of(verdict.min_leading))
    .bind(i32_of(verdict.evaluated))
    .bind(i32_of(members.len()))
    .bind(leading_json.to_string())
    .fetch_one(&mut *conn)
    .await?;
    for (i, c) in rows.iter().enumerate() {
        let via = serde_json::Value::Array(
            c.member
                .via
                .iter()
                .map(|v| json!({"index": v.index, "strength": round4(v.strength)}))
                .collect(),
        );
        sqlx::query(
            "INSERT INTO swing_candidates (run_id, run_at, mode, symbol, scanner, setup, entry, \
             stop, risk_pct, via_indices, universe_score, score, rank) VALUES ($1, $2, \
             'index_led', $3, $4, $5, $6, $7, $8, CAST($9 AS JSONB), $10, $11, $12)",
        )
        .bind(run_id)
        .bind(run_at)
        .bind(&c.symbol)
        .bind(c.scanner)
        .bind(&c.setup)
        .bind(c.entry.map(|d| d.round_dp(2)))
        .bind(c.stop.map(|d| d.round_dp(2)))
        .bind(c.risk_pct)
        .bind(via.to_string())
        .bind(dec(c.member.score, 4).unwrap_or_default())
        .bind(dec(c.score, 4).unwrap_or_default())
        .bind(i32_of(i + 1))
        .execute(&mut *conn)
        .await?;
    }
    Ok(RunReport {
        run_id: Some(run_id),
        skipped: None,
        gate_open: verdict.gate_open,
        leading: names,
        universe: members.len(),
        candidates: rows.len(),
    })
}

fn round4(x: f64) -> f64 {
    (x * 10_000.0).round() / 10_000.0
}

/// The last intraday run's time today, if any.
///
/// # Errors
/// The database's.
pub async fn last_intraday_run(
    conn: &mut PgConnection,
    as_of: NaiveDate,
) -> Result<Option<DateTime<Utc>>, WorkerError> {
    if !candidate_tables_ready(conn).await? {
        return Ok(None);
    }
    let row: Option<(DateTime<Utc>,)> = sqlx::query_as(
        "SELECT run_at FROM swing_regime_run WHERE kind = 'intraday' AND as_of = $1 \
         ORDER BY run_at DESC LIMIT 1",
    )
    .bind(as_of)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(row.map(|r| r.0))
}
