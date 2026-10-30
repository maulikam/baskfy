//! Step 11, `refresh_basket` — `baskfy_worker.tasks.basket.run_refresh_basket` and the build it
//! calls, `baskfy_api.baskets.build_current_basket` (M30): the desk's newest uploaded scan lends
//! the four columns no bar series can produce, `momentum_scan.build` makes the scan,
//! `score.score` ranks it and `baskfy_core.basket.build_plan` sizes an all-cash book of
//! `NOTIONAL_CAPITAL`; the result is upserted into `basket_snapshot` on `(as_of, data_version)`.
//!
//! The pieces are Python's, operation for operation:
//!
//! * `_carried`: `scan_*.csv` under `<DESK_ROOT>/data/uploads`, newest `st_mtime` first, the first
//!   one carrying `symbol`, `series` and `CARRIED_COLUMNS`, read as pandas reads it
//!   (`utf-8-sig`, the default NA spellings);
//! * `_bars`: every bar on or before the date for the carried symbols (no exchange filter), each
//!   number `float(Decimal)` with NULL as 0.0, ordered by symbol then date;
//! * `momentum_scan.build(...)` with its defaults — **`data_version=1`**, which is what the
//!   Python call site passes (it passes none), so `screen_run_id` is
//!   `sha256("desk_momentum_v1|<date>|1")[:16]` whatever the published version is;
//! * `build_plan(scored, [], 10_000_000.0, cfg=app.config, clusters={}, tradeable=always)` with
//!   the scan's closes as live prices. With no holdings only the selection, the weights, the
//!   order sizing and the no-trade band can fire; they are reproduced float for float
//!   (`sum` from the integer 0 left to right, `round` half-even on the exact binary value).
//!
//! The desk's `app.config` is imported by Python, and it runs `load_dotenv()` at import, which
//! walks up from `<DESK_ROOT>/app` to the first `.env` and sets every key **not already in the
//! environment**. The only key the basket reads is `FULLY_INVESTED`; [`fully_invested`] reads it
//! the same way (the process environment first, then that `.env`'s line), and nothing else of
//! the file is read or kept.
//!
//! A failure anywhere in the build is recorded, as Python records it, as
//! `skipped_reason = "{type}: {message}"` with the hint, and the chain carries on (a
//! presentation cache never holds back a good `data_version`). Law 2: nothing here sizes a real
//! book or reaches a broker — the plan is computed for an empty notional book and only its
//! target weights are stored.

use std::collections::{BTreeSet, HashMap};
use std::path::{Path, PathBuf};
use std::time::Instant;

use baskfy_db::OrderedJson;
use chrono::NaiveDate;
use sha2::{Digest, Sha256};
use sqlx::{PgConnection, Row};

use super::swing_market::py_round;
use crate::error::WorkerError;
use crate::pyjson;
use crate::steps::StepOutcome;
use crate::steps::compute::desk::{Carried, DeskBars, build_scan};
use crate::steps::compute::score::{ScanRow, Scored, pandas_quantile, score};

/// `baskets.NOTIONAL_CAPITAL`.
const NOTIONAL_CAPITAL: f64 = 10_000_000.0;
/// `momentum_scan.build`'s default `definition`.
const DEFINITION: &str = "desk_momentum_v1";
/// `momentum_scan.build`'s default `data_version` (the call site passes none).
const SCAN_DATA_VERSION: i64 = 1;
/// `momentum_scan.SPLIT_SIGNATURE`.
const SPLIT_SIGNATURE: f64 = 0.35;
/// `momentum_scan.MAX_ADJACENT_DAYS`.
const MAX_ADJACENT_DAYS: i64 = 7;
/// `momentum_scan.CARRIED_COLUMNS`, after `symbol` and `series` (`_carried`'s `wanted`).
const WANTED: [&str; 7] = [
    "symbol",
    "series",
    "marketcap",
    "beta",
    "circuits_three_months",
    "circuits_one_year",
    "is_nifty_fno",
];
/// pandas' default `na_values`.
const PANDAS_NA: [&str; 19] = [
    "", "#N/A", "#N/A N/A", "#NA", "-1.#IND", "-1.#QNAN", "-NaN", "-nan", "1.#IND", "1.#QNAN",
    "<NA>", "N/A", "NA", "NULL", "NaN", "None", "n/a", "nan", "null",
];

/// The desk's `app.config` constants `build_plan` reads with an empty book.
mod desk_cfg {
    /// `CASH_BANDS`.
    pub(super) const CASH_BANDS: [(f64, f64); 3] = [(65.0, 5.0), (45.0, 15.0), (0.0, 35.0)];
    /// `MAX_SINGLE_WEIGHT`.
    pub(super) const MAX_SINGLE_WEIGHT: f64 = 15.0;
    /// `MIN_POSITION_WEIGHT`.
    pub(super) const MIN_POSITION_WEIGHT: f64 = 6.0;
    /// `TARGET_POSITIONS`.
    pub(super) const TARGET_POSITIONS: (i64, i64) = (12, 15);
    /// `MIN_TRADE_VALUE`.
    pub(super) const MIN_TRADE_VALUE: f64 = 10_000.0;
    /// `MIN_TRADE_PCT`.
    pub(super) const MIN_TRADE_PCT: f64 = 2.0;
    /// `MAX_TRADE_COST_PCT`.
    pub(super) const MAX_TRADE_COST_PCT: f64 = 0.20;
    /// `MAX_POS_VS_DAY_VALUE`.
    pub(super) const MAX_POS_VS_DAY_VALUE: f64 = 0.01;
    /// `STOP_MIN`, `STOP_MAX`, `STOP_VOL_MULT`.
    pub(super) const STOP_MIN: f64 = 0.08;
    /// `STOP_MAX`.
    pub(super) const STOP_MAX: f64 = 0.12;
    /// `STOP_VOL_MULT`.
    pub(super) const STOP_VOL_MULT: f64 = 2.2;
}

/// `baskfy_core.costs` rates.
mod costs {
    /// `STT_DELIVERY`.
    pub(super) const STT_DELIVERY: f64 = 0.001;
    /// `EXCHANGE_TXN_NSE`.
    pub(super) const EXCHANGE_TXN_NSE: f64 = 0.000_029_7;
    /// `SEBI_TURNOVER`.
    pub(super) const SEBI_TURNOVER: f64 = 0.000_001;
    /// `STAMP_DUTY_BUY`.
    pub(super) const STAMP_DUTY_BUY: f64 = 0.000_15;
    /// `GST`.
    pub(super) const GST: f64 = 0.18;
    /// `BROKERAGE_DELIVERY`.
    pub(super) const BROKERAGE_DELIVERY: f64 = 0.0;
}

/// `baskets.DESK_ROOT`: `BASKFY_DESK_ROOT` when set; otherwise the desk beside this workspace in
/// the monorepo, as Python finds it relative to its own source file.
fn desk_root() -> PathBuf {
    match std::env::var_os("BASKFY_DESK_ROOT") {
        Some(root) if !root.is_empty() => PathBuf::from(root),
        _ => Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../kite-momentum-rebalancer"),
    }
}

/// `app.config.FULLY_INVESTED`: `os.getenv("FULLY_INVESTED", "false").lower() == "true"` after
/// `load_dotenv()` (which never overrides a key already in the environment).
fn fully_invested(root: &Path) -> bool {
    let value = match std::env::var("FULLY_INVESTED") {
        Ok(v) => Some(v),
        Err(_) => dotenv_value(&root.join("app"), "FULLY_INVESTED"),
    };
    value.is_some_and(|v| v.to_lowercase() == "true")
}

/// python-dotenv's `find_dotenv` from `start` (the first `.env` file walking up to the root)
/// and its value for `key` (the last assignment wins). Only `key`'s line is interpreted.
fn dotenv_value(start: &Path, key: &str) -> Option<String> {
    let mut dir = Some(start);
    while let Some(d) = dir {
        let candidate = d.join(".env");
        if candidate.is_file() {
            let text = std::fs::read_to_string(&candidate).ok()?;
            return parse_dotenv_key(&text, key);
        }
        dir = d.parent();
    }
    None
}

/// One key's value in a dotenv file, python-dotenv's rules for the shapes a flag takes:
/// `[export ]KEY=value`, a quoted value up to its closing quote, an unquoted one cut at
/// whitespace + `#` and right-stripped.
fn parse_dotenv_key(text: &str, key: &str) -> Option<String> {
    let mut found = None;
    for raw in text.lines() {
        let line = raw.trim_start();
        let line = line.strip_prefix("export ").unwrap_or(line);
        let Some((k, v)) = line.split_once('=') else {
            continue;
        };
        if k.trim() != key {
            continue;
        }
        let v = v.trim_start();
        let value = if let Some(rest) = v.strip_prefix('"') {
            rest.split('"').next().unwrap_or("").to_owned()
        } else if let Some(rest) = v.strip_prefix('\'') {
            rest.split('\'').next().unwrap_or("").to_owned()
        } else {
            let mut cut = v.len();
            let bytes = v.as_bytes();
            for i in 1..bytes.len() {
                if bytes[i] == b'#' && bytes[i - 1].is_ascii_whitespace() {
                    cut = i;
                    break;
                }
            }
            v[..cut].trim_end().to_owned()
        };
        found = Some(value);
    }
    found
}

/// A pandas cell: `None` for one of the default NA spellings.
fn na(cell: &str) -> Option<&str> {
    (!PANDAS_NA.contains(&cell)).then_some(cell)
}

/// A pandas numeric cell as a float (`None` for NA).
fn csv_float(cell: &str) -> Result<Option<f64>, WorkerError> {
    na(cell)
        .map(|c| {
            c.trim().parse::<f64>().map_err(|_| {
                WorkerError::python(
                    "ValueError",
                    format!("could not convert string to float: '{c}'"),
                )
            })
        })
        .transpose()
}

/// `_carried()`: the four borrowed columns from the newest uploaded scan that has them, or
/// `None`.
fn carried(root: &Path) -> Result<Option<Vec<Carried>>, WorkerError> {
    let uploads = root.join("data").join("uploads");
    let Ok(entries) = std::fs::read_dir(&uploads) else {
        return Ok(None);
    };
    let mut files: Vec<(PathBuf, std::time::SystemTime)> = Vec::new();
    for entry in entries.flatten() {
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if !(name.starts_with("scan_") && name.ends_with(".csv")) {
            continue;
        }
        let path = entry.path();
        let modified = std::fs::metadata(&path)
            .and_then(|m| m.modified())
            .map_err(|e| WorkerError::python("OSError", e.to_string()))?;
        files.push((path, modified));
    }
    files.sort_by_key(|f| f.0.clone());
    files.sort_by_key(|f| std::cmp::Reverse(f.1));
    for (path, _) in files {
        let bytes =
            std::fs::read(&path).map_err(|e| WorkerError::python("OSError", e.to_string()))?;
        let body = bytes.strip_prefix(b"\xEF\xBB\xBF").unwrap_or(&bytes);
        let mut reader = csv::ReaderBuilder::new()
            .has_headers(true)
            .from_reader(body);
        let headers = reader
            .headers()
            .map_err(|e| WorkerError::python("ParserError", e.to_string()))?
            .clone();
        let mut index = [0_usize; 7];
        let mut complete = true;
        for (k, column) in WANTED.iter().enumerate() {
            match headers.iter().position(|h| h == *column) {
                Some(i) => index[k] = i,
                None => complete = false,
            }
        }
        if !complete {
            continue;
        }
        let mut out = Vec::new();
        for record in reader.records() {
            let record = record.map_err(|e| WorkerError::python("ParserError", e.to_string()))?;
            let cell = |k: usize| record.get(index[k]).unwrap_or("");
            let Some(symbol) = na(cell(0)) else { continue };
            #[allow(clippy::cast_possible_truncation)]
            let int = |v: Option<f64>| v.map(|x| x as i64);
            out.push(Carried {
                symbol: symbol.to_owned(),
                series: na(cell(1)).map(str::to_owned),
                marketcap: csv_float(cell(2))?,
                beta: csv_float(cell(3))?,
                circuits_three_months: int(csv_float(cell(4))?),
                circuits_one_year: int(csv_float(cell(5))?),
                is_nifty_fno: int(csv_float(cell(6))?).unwrap_or(0),
            });
        }
        return Ok(Some(out));
    }
    Ok(None)
}

/// `_bars(session, as_of, symbols)`.
async fn bars(
    conn: &mut PgConnection,
    as_of: NaiveDate,
    symbols: &[String],
) -> Result<DeskBars, WorkerError> {
    let rows = sqlx::query(
        "select i.symbol, b.instrument_id, b.date, b.open::text, b.high::text, b.low::text, \
         b.close::text, b.volume::text, b.close_raw::text, b.volume_raw::text \
         from ohlcv_daily b join instrument i on i.id = b.instrument_id \
         where i.symbol = any($1) and b.date <= $2 order by i.symbol, b.date",
    )
    .bind(symbols)
    .bind(as_of)
    .fetch_all(&mut *conn)
    .await?;
    let mut out = DeskBars::default();
    for row in &rows {
        out.symbol.push(row.try_get(0)?);
        out.instrument_id.push(row.try_get(1)?);
        out.date.push(row.try_get(2)?);
        for (k, column) in out.numbers.iter_mut().enumerate() {
            let text: Option<String> = row.try_get(k + 3)?;
            let value = match text {
                Some(t) => t
                    .parse::<f64>()
                    .map_err(|e| WorkerError::python("ValueError", format!("{e}: {t}")))?,
                None => 0.0,
            };
            column.push(value);
        }
    }
    Ok(out)
}

/// `momentum_scan.screen_run_id(definition, as_of, data_version)`.
fn screen_run_id(as_of: NaiveDate) -> String {
    let digest = Sha256::digest(
        format!(
            "{DEFINITION}|{}|{SCAN_DATA_VERSION}",
            as_of.format("%Y-%m-%d")
        )
        .as_bytes(),
    );
    let hex: String = digest.iter().map(|b| format!("{b:02x}")).collect();
    hex.chars().take(16).collect()
}

/// `momentum_scan.unadjusted_symbols(bars)`: symbols with a step over [`SPLIT_SIGNATURE`]
/// between two bars at most [`MAX_ADJACENT_DAYS`] apart, sorted.
fn unadjusted_symbols(bars: &DeskBars) -> BTreeSet<String> {
    let close = &bars.numbers[3];
    let mut order: Vec<usize> = (0..bars.len()).collect();
    order.sort_by(|a, b| {
        bars.symbol[*a]
            .cmp(&bars.symbol[*b])
            .then(bars.date[*a].cmp(&bars.date[*b]))
    });
    let mut out = BTreeSet::new();
    for pair in order.windows(2) {
        let (p, c) = (pair[0], pair[1]);
        if bars.symbol[p] != bars.symbol[c] {
            continue;
        }
        let prev = close[p];
        if prev.is_nan() || prev <= 0.0 {
            continue;
        }
        if (bars.date[c] - bars.date[p]).num_days() > MAX_ADJACENT_DAYS {
            continue;
        }
        if ((close[c] / prev) - 1.0).abs() > SPLIT_SIGNATURE {
            out.insert(bars.symbol[c].clone());
        }
    }
    out
}

/// `stop_from_vol(price, ann_vol, cfg)`.
fn stop_from_vol(price: f64, ann_vol: f64) -> f64 {
    let raw = ann_vol / 52.0_f64.sqrt() * desk_cfg::STOP_VOL_MULT;
    // np.clip: NaN propagates (as `f64::clamp` does).
    let pct = raw.clamp(desk_cfg::STOP_MIN, desk_cfg::STOP_MAX);
    py_round(price * (1.0 - pct), 1)
}

/// `costs.cost_pct(value, "BUY")`.
fn cost_pct_buy(value: f64) -> f64 {
    let v = value.abs();
    if v == 0.0 {
        return 100.0;
    }
    let exchange = v * costs::EXCHANGE_TXN_NSE;
    let sebi = v * costs::SEBI_TURNOVER;
    let brokerage = v * costs::BROKERAGE_DELIVERY;
    let stt = v * costs::STT_DELIVERY;
    let stamp = v * costs::STAMP_DUTY_BUY;
    let gst = costs::GST * (brokerage + exchange + sebi);
    let dp = 0.0;
    let total = stt + exchange + sebi + stamp + gst + dp + brokerage;
    total / v * 100.0
}

/// Python's `round(x)` on a float: half to even, to an int.
#[allow(clippy::cast_possible_truncation)]
fn py_round_int(x: f64) -> i64 {
    x.round_ties_even() as i64
}

/// Python's `min(a, b)` on floats: `b` only when `b < a`.
fn py_min(a: f64, b: f64) -> f64 {
    if b < a { b } else { a }
}

/// Python's `max(a, b)` on floats: `b` only when `b > a`.
fn py_max(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

/// `sum(values)`: from the integer 0, left to right.
fn py_sum(values: impl IntoIterator<Item = f64>) -> f64 {
    values.into_iter().fold(0.0, |acc, v| acc + v)
}

/// One planned order (`build_plan`'s dict), the fields the basket reads.
struct Order {
    symbol: String,
    weight: f64,
    ref_price: f64,
    stop: f64,
    value: i64,
    rank: Option<i64>,
    score: Option<f64>,
}

/// `build_plan`'s answer, the fields the basket reads.
struct Plan {
    capital: i64,
    cash_target_pct: f64,
    breadth_above_20dma: f64,
    orders: Vec<Order>,
}

/// `build_plan(scored, [], NOTIONAL_CAPITAL, cfg=..., clusters={}, tradeable=lambda _: True,
/// live_prices=...)`: an all-cash book, so no holding is classified and no runner or exit
/// exists.
fn build_plan(
    scan: &[ScanRow],
    scored: &[Scored],
    live: &HashMap<String, f64>,
    fully_invested: bool,
) -> Result<Plan, WorkerError> {
    let price_of = |s: &str| live.get(s).copied();
    let rank_of = |i: usize| scored[i].rank;

    // Eligible names with no live price.
    let unpriced: BTreeSet<&str> = (0..scan.len())
        .filter(|i| scored[*i].reject.is_empty() && price_of(&scan[*i].symbol).is_none())
        .map(|i| scan[i].symbol.as_str())
        .collect();

    let capital = 0.0 + NOTIONAL_CAPITAL;
    let above = scan.iter().filter(|r| r.close > r.ma_20).count();
    #[allow(clippy::cast_precision_loss)]
    let breadth = if scan.is_empty() {
        f64::NAN
    } else {
        above as f64 / scan.len() as f64 * 100.0
    };
    let cash_pct = if fully_invested {
        0.0
    } else {
        desk_cfg::CASH_BANDS
            .iter()
            .find(|(min_b, _)| breadth >= *min_b)
            .map_or(desk_cfg::CASH_BANDS[2].1, |(_, cash)| *cash)
    };
    let invest = capital * (1.0 - cash_pct / 100.0);

    // Selection: eligible and priced, by rank.
    let mut elig: Vec<usize> = (0..scan.len())
        .filter(|i| scored[*i].reject.is_empty() && !unpriced.contains(scan[*i].symbol.as_str()))
        .collect();
    elig.sort_by_key(|i| rank_of(*i).unwrap_or(i64::MAX));
    let elig_scores: Vec<f64> = elig.iter().map(|i| scored[*i].score).collect();
    let q85 = pandas_quantile(&elig_scores, 0.85);
    let above_q = i64::try_from(elig_scores.iter().filter(|s| q85 <= **s).count()).unwrap_or(0);
    let (n_lo, n_hi) = desk_cfg::TARGET_POSITIONS;
    let n = usize::try_from(above_q.clamp(n_lo, n_hi)).unwrap_or(0);
    let mut selected: Vec<(String, f64)> = Vec::new();
    for i in &elig {
        if selected.len() >= n {
            break;
        }
        let symbol = &scan[*i].symbol;
        if !selected.iter().any(|(s, _)| s == symbol) {
            selected.push((symbol.clone(), scored[*i].score));
        }
    }

    // Weights.
    let pool = invest - 0.0;
    let tot = py_sum(selected.iter().map(|(_, sc)| *sc));
    if !selected.is_empty() && tot > 0.0 && (invest == 0.0 || capital == 0.0) {
        return Err(WorkerError::python(
            "ZeroDivisionError",
            "float division by zero",
        ));
    }
    let mut w: Vec<(String, f64)> = if tot <= 0.0 {
        Vec::new()
    } else {
        selected
            .iter()
            .map(|(s, sc)| {
                let v =
                    sc / tot * 100.0 * (invest / capital) * (pool / invest) / (invest / capital);
                (s.clone(), py_min(v, desk_cfg::MAX_SINGLE_WEIGHT))
            })
            .collect()
    };
    let scale = if w.is_empty() {
        0.0
    } else {
        (pool / capital * 100.0) / py_sum(w.iter().map(|(_, v)| *v))
    };
    for (_, v) in &mut w {
        *v *= scale;
    }
    for (_, v) in &mut w {
        *v = if *v > desk_cfg::MIN_POSITION_WEIGHT / 2.0 {
            py_max(*v, desk_cfg::MIN_POSITION_WEIGHT)
        } else {
            *v
        };
    }
    let scale = if w.is_empty() {
        0.0
    } else {
        (pool / capital * 100.0) / py_sum(w.iter().map(|(_, v)| *v))
    };
    for (_, v) in &mut w {
        *v = py_round(*v * scale, 2);
    }
    // The half-size rule needs `listed_on`, which a scan frame never carries; clusters are {}.

    // Orders, by weight descending (a stable sort).
    let by_symbol: HashMap<&str, usize> = scan
        .iter()
        .enumerate()
        .map(|(i, r)| (r.symbol.as_str(), i))
        .collect();
    w.sort_by(|a, b| b.1.total_cmp(&a.1));
    let mut orders = Vec::new();
    for (s, weight) in w {
        let Some(px) = price_of(&s) else { continue };
        let row = by_symbol.get(s.as_str()).copied();
        let vol = row.map_or(0.36, |i| scan[i].volatility_one_year);
        let mut tgt_qty = py_round_int(capital * weight / 100.0 / px);
        let day_val = row.map_or(f64::INFINITY, |i| scan[i].median_volume_one_year);
        #[allow(clippy::cast_precision_loss)]
        if (tgt_qty as f64) * px > day_val * desk_cfg::MAX_POS_VS_DAY_VALUE {
            #[allow(clippy::cast_possible_truncation)]
            let capped = (day_val * desk_cfg::MAX_POS_VS_DAY_VALUE / px) as i64;
            tgt_qty = capped;
        }
        let delta = tgt_qty;
        #[allow(clippy::cast_precision_loss)]
        let mut value = py_round_int(tgt_qty as f64 * px);
        // The no-trade band (qty_now is 0, so pct is 100).
        if delta != 0 && tgt_qty != 0 {
            #[allow(clippy::cast_precision_loss)]
            let val = (delta.abs() as f64) * px;
            let pct = 100.0;
            let cpct = cost_pct_buy(val);
            if val < desk_cfg::MIN_TRADE_VALUE
                || pct < desk_cfg::MIN_TRADE_PCT
                || cpct > desk_cfg::MAX_TRADE_COST_PCT
            {
                value = 0;
            }
        }
        orders.push(Order {
            symbol: s.clone(),
            weight,
            ref_price: px,
            stop: stop_from_vol(px, vol),
            value,
            rank: row.and_then(rank_of),
            score: row.map(|i| scored[i].score),
        });
    }

    Ok(Plan {
        capital: py_round_int(capital),
        cash_target_pct: cash_pct,
        breadth_above_20dma: py_round(breadth, 1),
        orders,
    })
}

/// A float as pydantic serialises it (`NaN`/`inf` as `null`).
fn float_json(x: f64) -> OrderedJson {
    if x.is_finite() {
        OrderedJson::Float(x)
    } else {
        OrderedJson::Null
    }
}

/// `build_current_basket(session, as_of)` as the stored payload, its name count and its suspect
/// count; `None` when there is nothing to build.
async fn build_current_basket(
    conn: &mut PgConnection,
    as_of: NaiveDate,
) -> Result<Option<(OrderedJson, i64, usize)>, WorkerError> {
    let root = desk_root();
    let Some(carried) = carried(&root)? else {
        return Ok(None);
    };
    let mut symbols: Vec<String> = carried.iter().map(|c| c.symbol.clone()).collect();
    symbols.sort();
    let bars = bars(conn, as_of, &symbols).await?;
    if bars.len() == 0 {
        return Ok(None);
    }
    let trading_days: Vec<NaiveDate> =
        sqlx::query_scalar("select distinct date from ohlcv_daily where date <= $1 order by date")
            .bind(as_of)
            .fetch_all(&mut *conn)
            .await?;

    let scan = build_scan(&bars, as_of, &trading_days, &carried)?;
    if scan.is_empty() {
        // `pd.DataFrame([])` has no columns, and `apply_filters` reads `u.ma_50` first.
        return Err(WorkerError::python(
            "AttributeError",
            "'DataFrame' object has no attribute 'ma_50'",
        ));
    }
    let in_scan: BTreeSet<&str> = scan.iter().map(|r| r.symbol.as_str()).collect();
    let suspect: Vec<String> = unadjusted_symbols(&bars)
        .into_iter()
        .filter(|s| in_scan.contains(s.as_str()))
        .collect();
    let scored = score(&scan);

    let live: HashMap<String, f64> = scan
        .iter()
        .filter(|r| r.close > 0.0)
        .map(|r| (r.symbol.clone(), r.close))
        .collect();
    let plan = build_plan(&scan, &scored, &live, fully_invested(&root))?;

    let components: HashMap<&str, [f64; 6]> = scan
        .iter()
        .zip(&scored)
        .map(|(r, s)| (r.symbol.as_str(), s.components))
        .collect();
    let mut orders: Vec<&Order> = plan.orders.iter().collect();
    orders.sort_by_key(|o| o.rank.filter(|r| *r != 0).unwrap_or(1_000_000));
    let mut rows = Vec::new();
    for order in orders {
        if order.weight == 0.0 {
            continue;
        }
        let part = components.get(order.symbol.as_str());
        let comp = |k: usize| part.map_or(OrderedJson::Null, |p| float_json(p[k]));
        let score = order.score.unwrap_or(0.0);
        rows.push(pyjson::object([
            ("rank", pyjson::int(order.rank.unwrap_or(0))),
            ("symbol", pyjson::s(order.symbol.clone())),
            ("score", float_json(if score == 0.0 { 0.0 } else { score })),
            ("weight", float_json(order.weight)),
            ("ref_price", float_json(order.ref_price)),
            ("stop", float_json(order.stop)),
            ("value", pyjson::int(order.value)),
            ("a_trend", comp(0)),
            ("b_momentum", comp(1)),
            ("c_sharpe", comp(2)),
            ("d_consistency", comp(3)),
            ("e_liquidity", comp(4)),
            ("f_penalty", comp(5)),
        ]));
    }
    let names = i64::try_from(rows.len()).unwrap_or(i64::MAX);
    let payload = pyjson::object([
        ("as_of", pyjson::s(as_of.format("%Y-%m-%d").to_string())),
        ("screen_run_id", pyjson::s(screen_run_id(as_of))),
        ("data_version", pyjson::int(SCAN_DATA_VERSION)),
        ("capital", pyjson::int(plan.capital)),
        ("cash_target_pct", float_json(plan.cash_target_pct)),
        ("breadth_above_20dma", float_json(plan.breadth_above_20dma)),
        ("suspect_symbols", pyjson::strings(suspect.clone())),
        ("rows", OrderedJson::Array(rows)),
    ]);
    Ok(Some((payload, names, suspect.len())))
}

fn now_utc() -> chrono::DateTime<chrono::Utc> {
    use baskfy_providers::clock::Clock as _;
    baskfy_providers::clock::system_clock().now()
}

/// `run_refresh_basket(session, outcome, as_of)`: the number of names stored (0 when nothing
/// could be built, which the orchestrator marks `skipped`).
///
/// # Errors
/// The upsert's database error (the build's own failures are recorded, not raised).
pub(crate) async fn run_refresh_basket(
    conn: &mut PgConnection,
    outcome: &mut StepOutcome,
    as_of: NaiveDate,
) -> Result<i64, WorkerError> {
    let started = Instant::now();
    let built = match build_current_basket(conn, as_of).await {
        Ok(b) => b,
        Err(exc) => {
            outcome.note(
                "skipped_reason",
                pyjson::s(format!("{}: {exc}", exc.python_type())),
            );
            outcome.note(
                "hint",
                pyjson::s(
                    "the basket needs bars and one uploaded scan; neither is this step's to create",
                ),
            );
            return Ok(0);
        }
    };
    let Some((payload, names, suspects)) = built else {
        outcome.note("skipped_reason", pyjson::s("no basket could be built"));
        outcome.note(
            "hint",
            pyjson::s(
                "needs bars in the pipeline and one uploaded scan for marketcap, beta and F&O",
            ),
        );
        return Ok(0);
    };
    let elapsed_ms = i64::try_from(started.elapsed().as_millis()).unwrap_or(i64::MAX);
    let text = pyjson::dumps(&payload).map_err(|e| WorkerError::python("ValueError", e))?;
    sqlx::query(
        "INSERT INTO basket_snapshot (as_of, screen_run_id, data_version, payload, computed_ms, \
         computed_at) VALUES ($1, $2, $3, $4::jsonb, $5, $6) \
         ON CONFLICT (as_of, data_version) DO UPDATE SET screen_run_id = excluded.screen_run_id, \
         payload = excluded.payload, computed_ms = excluded.computed_ms, \
         computed_at = excluded.computed_at",
    )
    .bind(as_of)
    .bind(screen_run_id(as_of))
    .bind(SCAN_DATA_VERSION)
    .bind(text)
    .bind(elapsed_ms)
    .bind(now_utc())
    .execute(&mut *conn)
    .await?;
    outcome.rows_out = names;
    outcome.note("as_of", pyjson::s(as_of.format("%Y-%m-%d").to_string()));
    outcome.note("data_version", pyjson::int(SCAN_DATA_VERSION));
    outcome.note("names", pyjson::int(names));
    outcome.note(
        "suspect_symbols",
        pyjson::int(i64::try_from(suspects).unwrap_or(i64::MAX)),
    );
    outcome.note("computed_ms", pyjson::int(elapsed_ms));
    Ok(names)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_screen_run_id_is_the_default_data_versions_digest() {
        let day = NaiveDate::from_ymd_opt(2026, 10, 29).unwrap_or_default();
        // momentum_scan.screen_run_id("desk_momentum_v1", date(2026, 10, 29), 1)
        assert_eq!(screen_run_id(day), "3baf73d9d09449fa");
    }

    #[test]
    fn a_dotenv_flag_is_read_as_python_dotenv_reads_it() {
        let text = "KITE_API_KEY=x\nFULLY_INVESTED=true    # comment\nPORT=1\n";
        assert_eq!(
            parse_dotenv_key(text, "FULLY_INVESTED").as_deref(),
            Some("true")
        );
        assert_eq!(
            parse_dotenv_key("export FULLY_INVESTED=\"True\" # x\n", "FULLY_INVESTED").as_deref(),
            Some("True")
        );
        assert_eq!(
            parse_dotenv_key("FULLY_INVESTED_X=true\n", "FULLY_INVESTED"),
            None
        );
    }

    #[test]
    fn a_stop_is_vol_scaled_and_clipped() {
        // stop_from_vol(100.0, 0.36, cfg): 0.36/sqrt(52)*2.2 = 0.1098..., round(89.0..., 1)
        assert!((stop_from_vol(100.0, 0.36) - 89.0).abs() < 1e-9);
        // clipped to 8% and 12%
        assert!((stop_from_vol(100.0, 0.01) - 92.0).abs() < 1e-9);
        assert!((stop_from_vol(100.0, 2.0) - 88.0).abs() < 1e-9);
    }

    #[test]
    fn a_buy_costs_about_twelve_basis_points() {
        let c = cost_pct_buy(100_000.0);
        assert!(c > 0.11 && c < 0.12, "{c}");
        assert!((cost_pct_buy(0.0) - 100.0).abs() < f64::EPSILON);
    }

    #[test]
    fn python_rounds_half_to_even() {
        assert_eq!(py_round_int(2.5), 2);
        assert_eq!(py_round_int(3.5), 4);
        assert_eq!(py_round_int(-2.5), -2);
    }
}
