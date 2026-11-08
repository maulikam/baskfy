//! `/indices` — the index service (`crate::indices`): the board, one index, its constituents and
//! history, and the staff refresh. Public reads; live Kite quotes in market hours, else the last
//! close; every answer carries `freshness`.

use std::collections::HashMap;

use axum::Router;
use axum::extract::{Extension, Path, RawQuery, State};
use axum::http::HeaderMap;
use axum::response::Response;
use axum::routing::{get, post};
use chrono::{DateTime, Duration, NaiveDate, Utc};
use rust_decimal::Decimal;
use rust_decimal::prelude::ToPrimitive;
use serde_json::{Value, json};
use sqlx::Row;

use crate::auth::{Principal, require_staff};
use crate::indices::quotes::{self, Fresh};
use crate::indices::{self, history, master, nse};
use crate::problems::{Problem, bad_request, not_found};
use crate::session::{self, WritePool};
use crate::state::AppState;
use crate::wire;

fn dbp(e: sqlx::Error) -> Problem {
    crate::db_failure(&e.into())
}

fn sp(e: String) -> Problem {
    crate::logging::error("baskfy_api.indices", "index service", &[("error", &e)]);
    crate::problems::internal_error()
}

fn num(d: Option<Decimal>) -> Value {
    d.and_then(|d| d.to_f64()).map_or(Value::Null, |f| json!(f))
}

fn pct(a: Decimal, b: Decimal) -> Option<Decimal> {
    (!b.is_zero()).then(|| ((a / b - Decimal::ONE) * Decimal::ONE_HUNDRED).round_dp(2))
}

fn query_map(raw: Option<&str>) -> HashMap<String, String> {
    raw.unwrap_or_default()
        .split('&')
        .filter_map(|kv| {
            let (k, v) = kv.split_once('=').unwrap_or((kv, ""));
            let dec = |s: &str| {
                percent_encoding::percent_decode_str(&s.replace('+', " "))
                    .decode_utf8_lossy()
                    .into_owned()
            };
            (!k.is_empty()).then(|| (dec(k), dec(v)))
        })
        .collect()
}

fn group_of(param: &str) -> Option<&'static str> {
    match param.to_lowercase().as_str() {
        "broad" | "broad market" => Some("Broad market"),
        "sectoral" => Some("Sectoral"),
        "thematic" => Some("Thematic"),
        "strategy" => Some("Strategy"),
        _ => None,
    }
}

fn freshness(fresh: Fresh, as_of: Option<String>) -> Value {
    json!({"state": fresh.as_str(), "as_of": as_of})
}

// ---------------------------------------------------------------------------------------------
// the board
// ---------------------------------------------------------------------------------------------

#[derive(Debug, Clone, Default)]
struct LatestStats {
    date: Option<NaiveDate>,
    close: Option<Decimal>,
    chg_pct: Option<Decimal>,
    dma20: Option<Decimal>,
    cross: Option<bool>,
    vs20: Option<Decimal>,
    ret_5d: Option<Decimal>,
    ret_30d: Option<Decimal>,
    ret_1y: Option<Decimal>,
    hi: Option<Decimal>,
    lo: Option<Decimal>,
    dist: Option<Decimal>,
}

#[derive(Debug, Clone)]
struct Board {
    id: i16,
    name: String,
    grp: String,
    live: bool,
    symbol: Option<String>,
    stats: LatestStats,
    quote: Option<quotes::Quote>,
    pe: Option<Decimal>,
    pb: Option<Decimal>,
    dy: Option<Decimal>,
    spark: Vec<Decimal>,
    swing_hits: i64,
}

impl Board {
    fn level(&self) -> Option<Decimal> {
        self.quote.map(|q| q.last).or(self.stats.close)
    }

    fn chg_pct(&self) -> Option<Decimal> {
        match self.quote {
            Some(q) => q.prev_close.and_then(|p| pct(q.last, p)),
            None => self.stats.chg_pct,
        }
    }

    fn vs20(&self) -> Option<Decimal> {
        match (self.quote, self.stats.dma20) {
            (Some(q), Some(d)) => pct(q.last, d),
            _ => self.stats.vs20,
        }
    }

    fn above_20(&self) -> Option<bool> {
        self.level().zip(self.stats.dma20).map(|(l, d)| l > d)
    }

    fn hi(&self) -> Option<Decimal> {
        match (self.stats.hi, self.quote) {
            (Some(h), Some(q)) => Some(h.max(q.last)),
            (h, _) => h,
        }
    }

    fn dist(&self) -> Option<Decimal> {
        match self.quote {
            Some(q) => self.hi().and_then(|h| pct(q.last, h)),
            None => self.stats.dist,
        }
    }

    fn json(&self, fresh: Fresh, quoted_at: Option<DateTime<Utc>>, today: NaiveDate) -> Value {
        let as_of = match (self.quote, quoted_at) {
            (Some(_), Some(t)) => Some(t.to_rfc3339()),
            _ => self.stats.date.map(|d| d.to_string()),
        };
        let old = self
            .stats
            .date
            .is_none_or(|d| today - d > Duration::days(7));
        json!({
            "id": self.id,
            "slug": indices::slugify(&self.name),
            "name": self.name,
            "group": self.grp,
            "live": self.live,
            "level": num(self.level()),
            "chg_pct": num(self.chg_pct()),
            "vs20_pct": num(self.vs20()),
            "cross_10_20": self.stats.cross,
            "above_20": self.above_20(),
            "ret_5d": num(self.stats.ret_5d),
            "ret_30d": num(self.stats.ret_30d),
            "ret_1y": num(self.stats.ret_1y),
            "hi_52w": num(self.hi()),
            "lo_52w": num(self.stats.lo),
            "dist_52w_hi_pct": num(self.dist()),
            "pe": num(self.pe),
            "pb": num(self.pb),
            "div_yield": num(self.dy),
            "spark_30d": self.spark.iter().map(|d| num(Some(*d))).collect::<Vec<_>>(),
            "swing_hits": self.swing_hits,
            "stale": old || (self.live && fresh == Fresh::Stale),
            "as_of": as_of,
        })
    }
}

/// The latest swing run's `(symbol, scanner, via_indices)`, or empty while the table is absent.
async fn swing_latest(state: &AppState) -> Result<Vec<(String, String, Value)>, Problem> {
    let exists: (bool,) = sqlx::query_as("SELECT to_regclass('swing_candidates') IS NOT NULL")
        .fetch_one(state.db.pool())
        .await
        .map_err(dbp)?;
    if !exists.0 {
        return Ok(Vec::new());
    }
    let rows = sqlx::query(
        "SELECT symbol, scanner, via_indices FROM swing_candidates \
         WHERE run_at = (SELECT max(run_at) FROM swing_candidates)",
    )
    .fetch_all(state.db.pool())
    .await
    .map_err(dbp)?;
    let mut out = Vec::new();
    for r in rows {
        let symbol: String = r.try_get("symbol").map_err(dbp)?;
        let scanner: Option<String> = r.try_get("scanner").map_err(dbp)?;
        let via: Option<Value> = r.try_get("via_indices").map_err(dbp)?;
        out.push((
            symbol.to_uppercase(),
            scanner.unwrap_or_default(),
            via.unwrap_or(Value::Null),
        ));
    }
    Ok(out)
}

fn via_names(via: &Value) -> Vec<String> {
    via.as_array()
        .map(|a| {
            a.iter()
                .filter_map(|v| match v {
                    Value::String(s) => Some(s.to_lowercase()),
                    Value::Object(o) => o.get("index").map(|i| match i {
                        Value::String(s) => s.to_lowercase(),
                        other => other.to_string(),
                    }),
                    _ => None,
                })
                .collect()
        })
        .unwrap_or_default()
}

fn dec_col(r: &sqlx::postgres::PgRow, c: &str) -> Result<Option<Decimal>, Problem> {
    r.try_get::<Option<Decimal>, _>(c).map_err(dbp)
}

async fn load_board(
    state: &AppState,
) -> Result<(Vec<Board>, Fresh, Option<DateTime<Utc>>), Problem> {
    let pool = state.db.pool();
    let rows = sqlx::query(
        "SELECT m.id, m.name, m.grp, m.live, m.kite_tradingsymbol, s.date, s.close, s.chg_pct, \
         s.dma20, s.cross_10_20, s.vs20_pct, s.ret_5d, s.ret_30d, s.ret_1y, s.hi_52w, \
         s.lo_52w, s.dist_52w_hi_pct FROM index_master m LEFT JOIN LATERAL ( \
           SELECT * FROM index_stats_daily x WHERE x.index_id = m.id ORDER BY x.date DESC LIMIT 1 \
         ) s ON true ORDER BY m.sort_order",
    )
    .fetch_all(pool)
    .await
    .map_err(dbp)?;
    let mut conn = pool.acquire().await.map_err(dbp)?;
    let defs = master::index_def_map(&mut conn).await.map_err(sp)?;
    drop(conn);
    let vals = sqlx::query(
        "SELECT DISTINCT ON (index_id) index_id, pe, pb, div_yield FROM index_snapshot_daily \
         WHERE date >= CURRENT_DATE - 30 ORDER BY index_id, date DESC",
    )
    .fetch_all(pool)
    .await
    .map_err(dbp)?;
    let mut val: HashMap<i16, [Option<Decimal>; 3]> = HashMap::new();
    for r in &vals {
        let id: i16 = r.try_get("index_id").map_err(dbp)?;
        val.insert(
            id,
            [
                dec_col(r, "pe")?,
                dec_col(r, "pb")?,
                dec_col(r, "div_yield")?,
            ],
        );
    }
    let sparks: Vec<(i16, Decimal)> = sqlx::query_as(
        "SELECT index_id, close FROM (SELECT index_id, date, close, row_number() OVER \
         (PARTITION BY index_id ORDER BY date DESC) AS r FROM index_stats_daily) t \
         WHERE r <= 30 ORDER BY index_id, date",
    )
    .fetch_all(pool)
    .await
    .map_err(dbp)?;
    let mut spark: HashMap<i16, Vec<Decimal>> = HashMap::new();
    for (id, c) in sparks {
        spark.entry(id).or_default().push(c);
    }
    let swing = swing_latest(state).await?;
    let mut board = Vec::with_capacity(rows.len());
    for r in &rows {
        let id: i16 = r.try_get("id").map_err(dbp)?;
        let name: String = r.try_get("name").map_err(dbp)?;
        let v = defs
            .get(&id)
            .and_then(|d| val.get(d))
            .copied()
            .unwrap_or_default();
        let key = name.to_lowercase();
        let slug = indices::slugify(&name);
        let hits = swing
            .iter()
            .filter(|(_, _, via)| {
                via_names(via)
                    .iter()
                    .any(|n| *n == key || *n == slug || *n == id.to_string())
            })
            .count();
        board.push(Board {
            id,
            name,
            grp: r.try_get("grp").map_err(dbp)?,
            live: r.try_get("live").map_err(dbp)?,
            symbol: r.try_get("kite_tradingsymbol").map_err(dbp)?,
            stats: LatestStats {
                date: r.try_get("date").map_err(dbp)?,
                close: dec_col(r, "close")?,
                chg_pct: dec_col(r, "chg_pct")?,
                dma20: dec_col(r, "dma20")?,
                cross: r.try_get("cross_10_20").map_err(dbp)?,
                vs20: dec_col(r, "vs20_pct")?,
                ret_5d: dec_col(r, "ret_5d")?,
                ret_30d: dec_col(r, "ret_30d")?,
                ret_1y: dec_col(r, "ret_1y")?,
                hi: dec_col(r, "hi_52w")?,
                lo: dec_col(r, "lo_52w")?,
                dist: dec_col(r, "dist_52w_hi_pct")?,
            },
            quote: None,
            pe: v[0],
            pb: v[1],
            dy: v[2],
            spark: spark.remove(&id).unwrap_or_default(),
            swing_hits: i64::try_from(hits).unwrap_or(0),
        });
    }
    let symbols: Vec<String> = board
        .iter()
        .filter(|b| b.live)
        .filter_map(|b| b.symbol.clone())
        .collect();
    let (quoted, mut fresh, mut at) = quotes::index_quotes(&symbols).await;
    for b in &mut board {
        if let Some(s) = &b.symbol {
            b.quote = quoted.get(s).copied();
        }
    }
    // NSE allIndices fills what Kite did not quote (no token, or Kite down).
    if indices::market_open(indices::now_ist()) && board.iter().any(|b| b.quote.is_none()) {
        let nse = indices::nse_board::board().await;
        let mut filled = false;
        for b in board.iter_mut().filter(|b| b.quote.is_none()) {
            b.quote = nse.get(&indices::nse_board::key(&b.name)).copied();
            filled |= b.quote.is_some();
        }
        if filled && fresh != Fresh::Live {
            fresh = Fresh::Live;
            at = Some(Utc::now());
        }
    }
    Ok((board, fresh, at))
}

fn board_as_of(board: &[Board], fresh: Fresh, at: Option<DateTime<Utc>>) -> Option<String> {
    match (fresh, at) {
        (Fresh::Live | Fresh::Stale, Some(t)) => Some(t.to_rfc3339()),
        _ => board
            .iter()
            .filter_map(|b| b.stats.date)
            .max()
            .map(|d| d.to_string()),
    }
}

async fn regime(state: &AppState, board: &[Board]) -> Result<Value, Problem> {
    let with = board.iter().filter(|b| b.above_20().is_some()).count();
    let above = board.iter().filter(|b| b.above_20() == Some(true)).count();
    let crossed = board.iter().filter(|b| b.stats.cross == Some(true)).count();
    let crossable = board.iter().filter(|b| b.stats.cross.is_some()).count();
    let breadth: Vec<(NaiveDate, Option<Decimal>)> = sqlx::query_as(
        "SELECT date, round(100.0 * avg(CASE WHEN close > dma20 THEN 1 ELSE 0 END), 2) \
         FROM index_stats_daily WHERE dma20 IS NOT NULL GROUP BY date ORDER BY date DESC LIMIT 30",
    )
    .fetch_all(state.db.pool())
    .await
    .map_err(dbp)?;
    let cross_pct = if crossable == 0 {
        Value::Null
    } else {
        json!(((crossed as f64) * 10000.0 / (crossable as f64)).round() / 100.0)
    };
    Ok(json!({
        "above_20": above,
        "total": with,
        "cross_pct": cross_pct,
        "breadth_30d": breadth.iter().rev()
            .map(|(d, p)| json!({"date": d.to_string(), "above_20_pct": num(*p)}))
            .collect::<Vec<_>>(),
    }))
}

/// `GET /indices?group=&q=&sort=`.
///
/// # Errors
/// 400 for an unknown group or sort; 500 for a database failure.
pub async fn list(
    State(state): State<AppState>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let q = query_map(raw.as_deref());
    let group = match q.get("group").map(String::as_str) {
        None | Some("" | "all") => None,
        Some(g) => Some(group_of(g).ok_or_else(|| {
            bad_request("group must be broad, sectoral, thematic or strategy.", None)
        })?),
    };
    let sort = q.get("sort").map_or("vs20_desc", String::as_str);
    if !matches!(sort, "vs20_desc" | "chg_desc" | "ret_1y_desc" | "name") {
        return Err(bad_request(
            "sort must be vs20_desc, chg_desc, ret_1y_desc or name.",
            None,
        ));
    }
    let needle = q
        .get("q")
        .map(|s| s.trim().to_lowercase())
        .unwrap_or_default();
    let (board, fresh, at) = load_board(&state).await?;
    let summary = regime(&state, &board).await?;
    let today = indices::now_ist().date_naive();
    let mut rows: Vec<&Board> = board
        .iter()
        .filter(|b| group.is_none_or(|g| b.grp == g))
        .filter(|b| needle.is_empty() || b.name.to_lowercase().contains(&needle))
        .collect();
    let key = |b: &Board| -> Option<Decimal> {
        match sort {
            "chg_desc" => b.chg_pct(),
            "ret_1y_desc" => b.stats.ret_1y,
            _ => b.vs20(),
        }
    };
    if sort == "name" {
        rows.sort_by_key(|a| a.name.to_lowercase());
    } else {
        rows.sort_by(|a, b| match (key(a), key(b)) {
            (Some(x), Some(y)) => y.cmp(&x),
            (Some(_), None) => std::cmp::Ordering::Less,
            (None, Some(_)) => std::cmp::Ordering::Greater,
            (None, None) => std::cmp::Ordering::Equal,
        });
    }
    Ok(wire::json(&json!({
        "freshness": freshness(fresh, board_as_of(&board, fresh, at)),
        "data": rows.iter().map(|b| b.json(fresh, at, today)).collect::<Vec<_>>(),
        "regime": summary,
    })))
}

fn find<'a>(board: &'a [Board], key: &str) -> Option<&'a Board> {
    match key.parse::<i16>() {
        Ok(id) => board.iter().find(|b| b.id == id),
        Err(_) => {
            let k = key.to_lowercase();
            board.iter().find(|b| indices::slugify(&b.name) == k)
        }
    }
}

// ---------------------------------------------------------------------------------------------
// constituents
// ---------------------------------------------------------------------------------------------

struct Member {
    symbol: String,
    name: String,
    isin: String,
    weight: Option<Decimal>,
    ltp: Option<Decimal>,
    chg: Option<Decimal>,
    vs20: Option<Decimal>,
    above: Option<bool>,
    dist: Option<Decimal>,
    hits: Vec<String>,
}

async fn members(
    state: &AppState,
    index_id: i16,
) -> Result<(Vec<Member>, Option<NaiveDate>, Option<DateTime<Utc>>, Fresh), Problem> {
    let pool = state.db.pool();
    let rows = sqlx::query(
        "WITH fd AS (SELECT max(date) AS d FROM factor_daily) \
         SELECT c.symbol, c.name, c.isin, c.as_of, l.close AS fclose, l.close_raw, \
                coalesce(c.weight, round(100 * m.marketcap_cr / nullif(sum(m.marketcap_cr) OVER (), 0), 2)) \
                  AS weight, \
                a.ma_20, f.high_1y, p.close_raw AS prev_raw \
         FROM index_constituents c \
         LEFT JOIN LATERAL (SELECT id FROM instrument i WHERE i.symbol = c.symbol \
                AND i.instrument_type = 'EQ' ORDER BY (i.exchange_id = (SELECT id FROM exchange WHERE code = 'NSE')) DESC, i.id LIMIT 1) i ON true \
         LEFT JOIN factor_daily f ON f.instrument_id = i.id AND f.date = (SELECT d FROM fd) \
         LEFT JOIN LATERAL (SELECT u.marketcap_cr FROM fundamental_daily u WHERE u.instrument_id = i.id \
                ORDER BY u.date DESC LIMIT 1) m ON true \
         LEFT JOIN LATERAL (SELECT o.close, o.close_raw FROM ohlcv_daily o WHERE o.instrument_id = i.id \
                ORDER BY o.date DESC LIMIT 1) l ON true \
         LEFT JOIN LATERAL (SELECT o.close_raw FROM ohlcv_daily o WHERE o.instrument_id = i.id \
                ORDER BY o.date DESC OFFSET 1 LIMIT 1) p ON true \
         LEFT JOIN LATERAL (SELECT avg(t.close) AS ma_20 FROM (SELECT o.close FROM ohlcv_daily o \
                WHERE o.instrument_id = i.id ORDER BY o.date DESC LIMIT 20) t) a ON true \
         WHERE c.index_id = $1 ORDER BY c.weight DESC NULLS LAST, c.symbol",
    )
    .bind(index_id)
    .fetch_all(pool)
    .await
    .map_err(dbp)?;
    let mut as_of: Option<NaiveDate> = None;
    let symbols: Vec<String> = rows
        .iter()
        .filter_map(|r| r.try_get::<String, _>("symbol").ok())
        .collect();
    let open = indices::market_open(indices::now_ist());
    let (live, fresh) = if open {
        match crate::live_prices::live_quote_details(&symbols).await {
            Ok(m) if !m.is_empty() => (m, Fresh::Live),
            _ => (HashMap::new(), Fresh::Stale),
        }
    } else {
        (HashMap::new(), Fresh::Close)
    };
    let quoted_at = (fresh == Fresh::Live).then(Utc::now);
    let swing = swing_latest(state).await?;
    let mut out = Vec::with_capacity(rows.len());
    for r in &rows {
        let symbol: String = r.try_get("symbol").map_err(dbp)?;
        let d: NaiveDate = r.try_get("as_of").map_err(dbp)?;
        as_of = Some(as_of.map_or(d, |a| a.max(d)));
        let close_raw = dec_col(r, "close_raw")?;
        let fclose = dec_col(r, "fclose")?;
        let ma20 = dec_col(r, "ma_20")?;
        let hi = dec_col(r, "high_1y")?;
        let prev_raw = dec_col(r, "prev_raw")?;
        let q = live.get(&symbol);
        let ltp = q.map(|q| q.last_price).or(close_raw);
        let chg = match q {
            Some(q) => q.prev_close.and_then(|p| pct(q.last_price, p)),
            None => close_raw.zip(prev_raw).and_then(|(c, p)| pct(c, p)),
        };
        let px = q.map(|q| q.last_price).or(fclose);
        let hits: Vec<String> = swing
            .iter()
            .filter(|(s, _, _)| *s == symbol)
            .map(|(_, sc, _)| sc.clone())
            .collect();
        out.push(Member {
            name: r
                .try_get::<Option<String>, _>("name")
                .map_err(dbp)?
                .unwrap_or_default(),
            isin: r
                .try_get::<Option<String>, _>("isin")
                .map_err(dbp)?
                .unwrap_or_default(),
            weight: dec_col(r, "weight")?,
            ltp,
            chg,
            vs20: px.zip(ma20).and_then(|(p, m)| pct(p, m)),
            above: px.zip(ma20).map(|(p, m)| p > m),
            dist: px.zip(hi).and_then(|(p, h)| pct(p, h.max(p))),
            hits,
            symbol,
        });
    }
    Ok((out, as_of, quoted_at, fresh))
}

async fn constituents_stale(state: &AppState, index_id: i16) -> Result<bool, Problem> {
    let row: (Option<DateTime<Utc>>,) =
        sqlx::query_as("SELECT last_constituents_at FROM index_master WHERE id = $1")
            .bind(index_id)
            .fetch_one(state.db.pool())
            .await
            .map_err(dbp)?;
    Ok(row
        .0
        .is_none_or(|t| Utc::now() - t > Duration::days(indices::CONSTITUENTS_MAX_AGE_DAYS)))
}

/// `GET /indices/{id}/constituents`.
///
/// # Errors
/// 404 for an unknown index; 500 for a database failure.
pub async fn get_constituents(
    State(state): State<AppState>,
    Path(key): Path<String>,
) -> Result<Response, Problem> {
    let id = resolve_id(&state, &key).await?;
    let (rows, as_of, at, fresh) = members(&state, id).await?;
    let stale = constituents_stale(&state, id).await?;
    let fresh_as_of = at
        .map(|t| t.to_rfc3339())
        .or_else(|| as_of.map(|d| d.to_string()));
    Ok(wire::json(&json!({
        "freshness": freshness(fresh, fresh_as_of),
        "constituents_as_of": as_of.map(|d| d.to_string()),
        "stale_constituents": stale,
        "data": rows.iter().map(|m| json!({
            "symbol": m.symbol,
            "name": m.name,
            "isin": m.isin,
            "weight": num(m.weight),
            "ltp": num(m.ltp),
            "chg_pct": num(m.chg),
            "vs20_pct": num(m.vs20),
            "above_20": m.above,
            "dist_52w_hi_pct": num(m.dist),
            "hits": m.hits,
        })).collect::<Vec<_>>(),
    })))
}

async fn resolve_id(state: &AppState, key: &str) -> Result<i16, Problem> {
    let rows: Vec<(i16, String)> = sqlx::query_as("SELECT id, name FROM index_master")
        .fetch_all(state.db.pool())
        .await
        .map_err(dbp)?;
    let k = key.to_lowercase();
    rows.into_iter()
        .find(|(id, name)| id.to_string() == k || indices::slugify(name) == k)
        .map(|(id, _)| id)
        .ok_or_else(|| not_found("index", key))
}

// ---------------------------------------------------------------------------------------------
// one index
// ---------------------------------------------------------------------------------------------

/// `GET /indices/{id}` (numeric id or slug).
///
/// # Errors
/// 404 for an unknown index; 500 for a database failure.
pub async fn get_one(
    State(state): State<AppState>,
    Path(key): Path<String>,
) -> Result<Response, Problem> {
    let (board, fresh, at) = load_board(&state).await?;
    let b = find(&board, &key).ok_or_else(|| not_found("index", &key))?;
    let today = indices::now_ist().date_naive();
    let last2: Vec<(NaiveDate, Decimal, Decimal, Decimal, Decimal)> = sqlx::query_as(
        "SELECT date, open, high, low, close FROM index_bar_daily WHERE index_id = $1 \
         ORDER BY date DESC LIMIT 2",
    )
    .bind(b.id)
    .fetch_all(state.db.pool())
    .await
    .map_err(dbp)?;
    let closes: Vec<(Decimal,)> = sqlx::query_as(
        "SELECT close FROM index_stats_daily WHERE index_id = $1 ORDER BY date DESC LIMIT 2",
    )
    .bind(b.id)
    .fetch_all(state.db.pool())
    .await
    .map_err(dbp)?;
    let (open, high, low, prev) = match b.quote {
        Some(q) => (q.open, q.high, q.low, q.prev_close),
        None => {
            let bar = last2.first().filter(|r| Some(r.0) == b.stats.date);
            (
                bar.map(|r| r.1),
                bar.map(|r| r.2),
                bar.map(|r| r.3),
                closes.get(1).map(|c| c.0),
            )
        }
    };
    let (members, cons_as_of, _, _) = members(&state, b.id).await?;
    let up = members
        .iter()
        .filter(|m| m.chg.is_some_and(|c| c > Decimal::ZERO))
        .count();
    let down = members
        .iter()
        .filter(|m| m.chg.is_some_and(|c| c < Decimal::ZERO))
        .count();
    let bench = board
        .iter()
        .find(|x| x.name == "Nifty 50")
        .and_then(|x| x.stats.ret_30d);
    let leading = b.above_20() == Some(true)
        && b.stats.cross == Some(true)
        && b.stats.ret_30d.zip(bench).is_some_and(|(r, n)| r > n);
    let mut row = b.json(fresh, at, today);
    if let Value::Object(m) = &mut row {
        m.insert("open".into(), num(open));
        m.insert("high".into(), num(high));
        m.insert("low".into(), num(low));
        m.insert("prev_close".into(), num(prev));
        m.insert("constituents_up".into(), json!(up));
        m.insert("constituents_down".into(), json!(down));
        m.insert(
            "constituents_as_of".into(),
            json!(cons_as_of.map(|d| d.to_string())),
        );
    }
    let row_fresh = if b.live { fresh } else { Fresh::Close };
    Ok(wire::json(&json!({
        "freshness": freshness(row_fresh, board_as_of(std::slice::from_ref(b), row_fresh, at)),
        "index": row,
        "leading": leading,
    })))
}

// ---------------------------------------------------------------------------------------------
// history
// ---------------------------------------------------------------------------------------------

/// `GET /indices/{id}/history?days=365`: stored bars (the NSE level where Kite has none), with
/// DMAs. An index with no stored bars is read from Kite on demand (not persisted: a GET is
/// read-only; the nightly and the refresh persist).
///
/// # Errors
/// 400 for a bad `days`; 404 for an unknown index; 500 for a database failure.
pub async fn get_history(
    State(state): State<AppState>,
    Path(key): Path<String>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let q = query_map(raw.as_deref());
    let days: i64 = match q.get("days") {
        None => 365,
        Some(d) => d
            .parse()
            .ok()
            .filter(|d| (1..=3650).contains(d))
            .ok_or_else(|| bad_request("days must be an integer from 1 to 3650.", None))?,
    };
    let id = resolve_id(&state, &key).await?;
    let today = indices::now_ist().date_naive();
    let from = today - Duration::days(days);
    let warm = from - Duration::days(120);
    let mut conn = state.db.pool().acquire().await.map_err(dbp)?;
    let defs = master::index_def_map(&mut conn).await.map_err(sp)?;
    let mut pts = history::series(&mut conn, id, defs.get(&id).copied(), warm)
        .await
        .map_err(sp)?;
    let (count, token): (i64, Option<i64>) = sqlx::query_as(
        "SELECT (SELECT count(*) FROM index_bar_daily WHERE index_id = $1), \
         (SELECT kite_token FROM index_master WHERE id = $1 AND live)",
    )
    .bind(id)
    .fetch_one(&mut *conn)
    .await
    .map_err(dbp)?;
    drop(conn);
    if count == 0
        && let Some(token) = token
        && crate::live_prices::quotes_permitted().unwrap_or(false)
    {
        let api_key = std::env::var("BASKFY_KITE_API_KEY").unwrap_or_default();
        if let Ok(kite) = crate::live_prices::kite_provider(api_key.trim()).await
            && let Ok(bars) = history::fetch_bars(&kite, token, warm, today).await
        {
            let mut by: std::collections::BTreeMap<NaiveDate, history::Point> =
                pts.into_iter().map(|p| (p.date, p)).collect();
            for b in bars {
                by.insert(
                    b.date,
                    history::Point {
                        date: b.date,
                        open: Some(b.open),
                        high: b.high,
                        low: b.low,
                        close: b.close,
                    },
                );
            }
            pts = by.into_values().collect();
        }
    }
    let data: Vec<Value> = (0..pts.len())
        .filter(|&i| pts[i].date >= from)
        .map(|i| {
            let p = pts[i];
            let bar = p.open.is_some();
            json!({
                "date": p.date.to_string(),
                "open": num(p.open),
                "high": if bar { num(Some(p.high)) } else { Value::Null },
                "low": if bar { num(Some(p.low)) } else { Value::Null },
                "close": num(Some(p.close)),
                "dma10": num(history::sma(&pts, i, 10)),
                "dma20": num(history::sma(&pts, i, 20)),
                "dma50": num(history::sma(&pts, i, 50)),
            })
        })
        .collect();
    Ok(wire::json(&json!({"data": data})))
}

// ---------------------------------------------------------------------------------------------
// refresh (staff)
// ---------------------------------------------------------------------------------------------

/// `POST /indices/refresh?force=&history=`: the master join (Kite's instruments when a session
/// exists), due constituent lists (all with `force=true`), and, unless `history=false`, bars and
/// stats. Staff only; a cookie-authenticated call needs the CSRF header.
///
/// # Errors
/// 401/404 from the staff gate or CSRF; 500 for a database failure.
pub async fn refresh_indices(
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    headers: HeaderMap,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    require_staff(&principal)?;
    let hv = |k: &str| {
        headers
            .get(k)
            .and_then(|v| v.to_str().ok())
            .map(str::to_owned)
    };
    if crate::auth::bearer_token(hv("authorization").as_deref()).is_none() {
        crate::csrf::require_csrf(hv("cookie").as_deref(), hv("x-csrf-token").as_deref())?;
    }
    let pool = crate::routes::screens::write_kit::pool(write)?;
    let q = query_map(raw.as_deref());
    let truthy = |k: &str, d: bool| {
        q.get(k).map_or(d, |v| {
            matches!(v.to_lowercase().as_str(), "1" | "true" | "yes" | "on")
        })
    };
    let force = truthy("force", false);
    let with_history = truthy("history", true);

    let api_key = std::env::var("BASKFY_KITE_API_KEY").unwrap_or_default();
    let kite = if crate::live_prices::quotes_permitted().unwrap_or(false) {
        crate::live_prices::kite_provider(api_key.trim()).await.ok()
    } else {
        None
    };
    let kite_rows = match &kite {
        Some(k) => master::kite_index_instruments(k).await.unwrap_or_default(),
        None => Vec::new(),
    };
    let refreshed: i64 = session::run(&pool, move |tx| {
        Box::pin(async move { master::refresh_master(tx, &kite_rows).await.map_err(sp) })
    })
    .await?;

    let due: Vec<(i16, String, Option<String>)> = session::run(&pool, move |tx| {
        Box::pin(async move { nse::due(tx, force).await.map_err(sp) })
    })
    .await?;
    let (got, failed) = nse::fetch_due(&due).await;
    let ok = i64::try_from(got.len()).unwrap_or(0);

    let today = indices::now_ist().date_naive();
    let mut bars: Vec<(i16, Vec<history::Bar>)> = Vec::new();
    let mut bars_failed = 0_i64;
    if with_history && let Some(k) = &kite {
        let plan = session::run(&pool, move |tx| {
            Box::pin(async move { history::plan(tx, today, None).await.map_err(sp) })
        })
        .await?;
        for (id, token, start, end) in plan {
            match history::fetch_bars(k, token, start, end).await {
                Ok(b) => bars.push((id, b)),
                Err(_) => bars_failed += 1,
            }
        }
    }
    let bar_count = i64::try_from(bars.len()).unwrap_or(0);
    let stats_rows: i64 = session::run(&pool, move |tx| {
        Box::pin(async move {
            for (id, stem, rows) in &got {
                nse::write(tx, *id, stem, rows).await.map_err(sp)?;
            }
            for (id, b) in &bars {
                history::write_bars(tx, *id, b).await.map_err(sp)?;
            }
            if with_history {
                history::recompute_all_stats(tx).await.map_err(sp)
            } else {
                Ok(0)
            }
        })
    })
    .await?;
    Ok(wire::json(&json!({
        "refreshed_master": refreshed,
        "constituents_ok": ok,
        "constituents_failed": failed,
        "history_ok": bar_count,
        "history_failed": bars_failed,
        "stats_rows": stats_rows,
        "kite": kite.is_some(),
    })))
}

/// The `/indices` routes.
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/indices", get(list))
        .route("/indices/refresh", post(refresh_indices))
        .route("/indices/{id}", get(get_one))
        .route("/indices/{id}/constituents", get(get_constituents))
        .route("/indices/{id}/history", get(get_history))
}
