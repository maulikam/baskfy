//! `/indices/*`, `/market-health*` and `/listings` — docs/07 §"Market data surfaces".
//!
//! Ports `baskfy_api.routers.market_data` and the service module `baskfy_api.market_data`. All
//! four routes are public (no principal needed), read tables the nightly writes, and answer with
//! the screener's canonical encoder (decimals unquoted at their stored precision) plus
//! `snapshot_headers`:
//!
//! * `GET /indices/dashboard?date=` — the newest snapshot per index on or before the as-of date,
//!   sorted by % change, with a 30-day sparkline each;
//! * `GET /market-health?universe=&date=` — the four breadth gauges for one universe;
//! * `GET /market-health/history?universe=&from=&to=` — the four series plus the index level;
//! * `GET /listings?limit=&cursor=&series=&search=&from=&to=` — the register, newest first,
//!   keyset-paginated.

use axum::Router;
use axum::extract::{RawQuery, State};
use axum::response::Response;
use axum::routing::get;
use baskfy_core::screener::Payload;
use baskfy_db::OrderedJson;
use chrono::{Duration, NaiveDate};
use rust_decimal::Decimal;
use sqlx::Row;
use sqlx::postgres::PgArguments;
use sqlx::query::Query;

use super::meta::reference::universes::{self, Universe};
use super::meta::reference::{
    self as r, Params, canonical_response, p_date, p_dec, p_map, p_opt_date, p_opt_dec, p_opt_int,
    p_opt_str, p_str,
};
use crate::problems::{Problem, ProblemType};
use crate::state::AppState;

/// `SPARKLINE_DAYS`.
pub const SPARKLINE_DAYS: i64 = 30;
/// `MAX_SNAPSHOT_STALENESS`, in days.
pub const MAX_SNAPSHOT_STALENESS_DAYS: i64 = 14;
/// `MAX_HISTORY_SPAN`, in days.
pub const MAX_HISTORY_SPAN_DAYS: i64 = 5 * 366;
/// `schemas.DEFAULT_PAGE_SIZE`.
pub const DEFAULT_PAGE_SIZE: i64 = 100;
/// `schemas.MAX_PAGE_SIZE`.
pub const MAX_PAGE_SIZE: i64 = 500;

/// `GAUGES`: docs/01 §6's four gauges, in the reference product's order.
pub const GAUGES: [(&str, &str); 4] = [
    ("pct_above_200dma", "Above 200 DMA"),
    ("pct_above_50dma", "Above 50 DMA"),
    ("pct_within_10pct_ath", "Within 10% of ATH"),
    ("pct_ret_1y_positive", "1Y Return > 0%"),
];

fn ymd(y: i32, m: u32, d: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, d).unwrap_or_default()
}

/// `NO_LISTING_DATE`.
fn no_listing_date() -> NaiveDate {
    ymd(1, 1, 1)
}

/// `LISTINGS_BACKFILL_SEAM`.
fn listings_backfill_seam() -> NaiveDate {
    ymd(2026, 8, 17)
}

fn invalid(detail: &str, field: &str, message: &str) -> Problem {
    Problem::new(ProblemType::InvalidScreenDefinition, detail).with(
        "errors",
        OrderedJson::Array(vec![OrderedJson::Object(vec![
            ("field".to_owned(), OrderedJson::Str(field.to_owned())),
            ("message".to_owned(), OrderedJson::Str(message.to_owned())),
        ])]),
    )
}

/// `_health_universe(slug)`.
fn health_universe(slug: &str) -> Result<Universe, Problem> {
    let choose = format!(
        "choose one of {}",
        universes::py_list_repr(&universes::market_health_slugs_sorted())
    );
    let Some(universe) = universes::by_slug(slug) else {
        return Err(invalid(
            &format!(
                "{} is not a universe.",
                baskfy_providers::py::repr_str(slug)
            ),
            "universe",
            &choose,
        ));
    };
    if !universe.market_health {
        return Err(invalid(
            &format!(
                "{} has no breadth series; docs/01 §6 covers twelve universes.",
                universe.name
            ),
            "universe",
            &choose,
        ));
    }
    Ok(universe)
}

fn membership(u: Universe) -> Payload {
    p_map(vec![("slug", p_str(u.slug)), ("name", p_str(u.name))])
}

// ---------------------------------------------------------------------------------------------
// /indices/dashboard
// ---------------------------------------------------------------------------------------------

/// `GET /indices/dashboard?date=`.
///
/// # Errors
/// 400 for a bad date, 422/503 from the as-of resolution, 500 for a database failure.
pub async fn get_index_dashboard(
    State(state): State<AppState>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let mut p = Params::new(raw.as_deref());
    let date = p.date("date");
    p.finish()?;
    let as_of = r::resolve_as_of(&state, date.ok().flatten()).await?;
    let data_version = r::current_data_version(&state).await?;
    let floor = as_of - Duration::days(MAX_SNAPSHOT_STALENESS_DAYS);
    let rows = sqlx::query(
        "SELECT d.id, d.slug, d.name, d.is_universe, s.date, s.level, s.change_abs, \
         s.change_pct, s.pe, s.pb, s.div_yield \
         FROM index_def d JOIN ( \
           SELECT DISTINCT ON (index_id) index_id, date, level, change_abs, change_pct, pe, pb, \
                  div_yield \
           FROM index_snapshot_daily WHERE date <= $1 AND date >= $2 \
           ORDER BY index_id, date DESC) s ON s.index_id = d.id \
         ORDER BY s.change_pct DESC NULLS LAST, d.sort_order, d.name",
    )
    .bind(as_of)
    .bind(floor)
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;

    // `_sparklines`: the last thirty levels per index, oldest first.
    let window_floor = as_of - Duration::days(SPARKLINE_DAYS * 3);
    let spark_rows = sqlx::query(
        "SELECT index_id, level FROM ( \
           SELECT index_id, date, level, row_number() OVER ( \
             PARTITION BY index_id ORDER BY date DESC) AS recency \
           FROM index_snapshot_daily \
           WHERE date <= $1 AND date >= $2 AND level IS NOT NULL) ranked \
         WHERE recency <= $3 ORDER BY index_id, date",
    )
    .bind(as_of)
    .bind(window_floor)
    .bind(SPARKLINE_DAYS)
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;
    let mut series: Vec<(i16, Vec<Decimal>)> = Vec::new();
    for row in &spark_rows {
        let id: i16 = row.try_get("index_id").map_err(r::db)?;
        let level = baskfy_db::row::decimal(row, "level").map_err(|e| r::dbe(&e))?;
        match series.last_mut() {
            Some((last, values)) if *last == id => values.push(level),
            _ => series.push((id, vec![level])),
        }
    }

    let mut latest: Option<NaiveDate> = None;
    let mut data = Vec::with_capacity(rows.len());
    for row in &rows {
        let id: i16 = row.try_get("id").map_err(r::db)?;
        let date = baskfy_db::row::date(row, "date").map_err(|e| r::dbe(&e))?;
        latest = Some(latest.map_or(date, |l| l.max(date)));
        let dec = |c: &str| baskfy_db::row::opt_decimal(row, c).map_err(|e| r::dbe(&e));
        let slug: String = row.try_get("slug").map_err(r::db)?;
        let name: String = row.try_get("name").map_err(r::db)?;
        let is_universe: bool = row.try_get("is_universe").map_err(r::db)?;
        let spark = series
            .iter()
            .find(|(i, _)| *i == id)
            .map(|(_, v)| v.iter().copied().map(p_dec).collect())
            .unwrap_or_default();
        data.push(p_map(vec![
            ("slug", Payload::Str(slug)),
            ("name", Payload::Str(name)),
            ("is_universe", Payload::Bool(is_universe)),
            ("level", p_opt_dec(dec("level")?)),
            ("change_abs", p_opt_dec(dec("change_abs")?)),
            ("change_pct", p_opt_dec(dec("change_pct")?)),
            ("pe", p_opt_dec(dec("pe")?)),
            ("pb", p_opt_dec(dec("pb")?)),
            ("div_yield", p_opt_dec(dec("div_yield")?)),
            ("sparkline", Payload::List(spark)),
        ]));
    }
    let board_as_of = latest.unwrap_or(as_of);
    let payload = p_map(vec![
        ("as_of", p_date(board_as_of)),
        ("data_version", Payload::Int(data_version)),
        ("data", Payload::List(data)),
    ]);
    canonical_response(&payload, Some(board_as_of), Some(data_version))
}

// ---------------------------------------------------------------------------------------------
// /market-health
// ---------------------------------------------------------------------------------------------

/// `GET /market-health?universe=&date=`.
///
/// # Errors
/// 400 for a bad parameter or a universe without breadth, 422/503 from the as-of resolution,
/// 500 for a database failure.
pub async fn get_market_health(
    State(state): State<AppState>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let mut p = Params::new(raw.as_deref());
    let universe = p.raw("universe").unwrap_or_else(|| "nifty-500".to_owned());
    let date = p.date("date");
    p.finish()?;
    let universe = health_universe(&universe)?;
    let as_of = r::resolve_as_of(&state, date.ok().flatten()).await?;
    let data_version = r::current_data_version(&state).await?;
    let row = sqlx::query(
        "SELECT date, pct_above_200dma, pct_above_50dma, pct_within_10pct_ath, \
         pct_ret_1y_positive, constituent_count FROM market_health_daily \
         WHERE index_id = $1 AND date <= $2 ORDER BY date DESC LIMIT 1",
    )
    .bind(universe_id(universe))
    .bind(as_of)
    .fetch_optional(state.db.pool())
    .await
    .map_err(r::db)?;
    let earliest: Option<NaiveDate> =
        sqlx::query_scalar("SELECT min(date) FROM market_health_daily WHERE index_id = $1")
            .bind(universe_id(universe))
            .fetch_one(state.db.pool())
            .await
            .map_err(r::db)?;

    let mut gauges = Vec::new();
    for (key, label) in GAUGES {
        let value = match &row {
            Some(row) => baskfy_db::row::opt_decimal(row, key).map_err(|e| r::dbe(&e))?,
            None => None,
        };
        gauges.push(p_map(vec![
            ("key", p_str(key)),
            ("label", p_str(label)),
            ("value", p_opt_dec(value)),
        ]));
    }
    let (health_as_of, count) = match &row {
        Some(row) => (
            baskfy_db::row::date(row, "date").map_err(|e| r::dbe(&e))?,
            row.try_get::<Option<i32>, _>("constituent_count")
                .map_err(r::db)?
                .map(i64::from),
        ),
        None => (as_of, None),
    };
    let payload = p_map(vec![
        ("as_of", p_date(health_as_of)),
        ("data_version", Payload::Int(data_version)),
        ("universe", membership(universe)),
        ("gauges", Payload::List(gauges)),
        ("constituent_count", p_opt_int(count)),
        ("data_available_from", p_opt_date(earliest)),
    ]);
    canonical_response(&payload, Some(health_as_of), Some(data_version))
}

fn universe_id(u: Universe) -> i16 {
    i16::try_from(u.index_id).unwrap_or(i16::MAX)
}

/// `GET /market-health/history?universe=&from=&to=`.
///
/// # Errors
/// 400 for a bad parameter, a reversed or over-long range, or a universe without breadth;
/// 422/503 from the as-of resolution; 500 for a database failure.
pub async fn get_market_health_history(
    State(state): State<AppState>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let mut p = Params::new(raw.as_deref());
    let universe = p.raw("universe").unwrap_or_else(|| "nifty-500".to_owned());
    let from = p.date("from");
    let to = p.date("to");
    p.finish()?;
    let universe = health_universe(&universe)?;
    let end = r::resolve_as_of(&state, to.ok().flatten()).await?;
    let start = from
        .ok()
        .flatten()
        .unwrap_or_else(|| end - Duration::days(365));
    if start > end {
        return Err(invalid(
            &format!(
                "`from` ({}) is after `to` ({}).",
                crate::wire::py_date(start),
                crate::wire::py_date(end)
            ),
            "from",
            "must be on or before `to`",
        ));
    }
    if (end - start).num_days() > MAX_HISTORY_SPAN_DAYS {
        return Err(invalid(
            &format!("Range exceeds the {MAX_HISTORY_SPAN_DAYS}-day maximum for one request."),
            "from",
            "narrow the range",
        ));
    }
    let rows = sqlx::query(
        "SELECT h.date, h.pct_above_200dma, h.pct_above_50dma, h.pct_within_10pct_ath, \
         h.pct_ret_1y_positive, h.constituent_count, s.level \
         FROM market_health_daily h LEFT OUTER JOIN index_snapshot_daily s \
           ON s.index_id = h.index_id AND s.date = h.date \
         WHERE h.index_id = $1 AND h.date >= $2 AND h.date <= $3 ORDER BY h.date",
    )
    .bind(universe_id(universe))
    .bind(start)
    .bind(end)
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;
    let mut points = Vec::with_capacity(rows.len());
    for row in &rows {
        let dec = |c: &str| baskfy_db::row::opt_decimal(row, c).map_err(|e| r::dbe(&e));
        points.push(p_map(vec![
            (
                "date",
                p_date(baskfy_db::row::date(row, "date").map_err(|e| r::dbe(&e))?),
            ),
            ("pct_above_200dma", p_opt_dec(dec("pct_above_200dma")?)),
            ("pct_above_50dma", p_opt_dec(dec("pct_above_50dma")?)),
            (
                "pct_within_10pct_ath",
                p_opt_dec(dec("pct_within_10pct_ath")?),
            ),
            (
                "pct_ret_1y_positive",
                p_opt_dec(dec("pct_ret_1y_positive")?),
            ),
            (
                "constituent_count",
                p_opt_int(
                    row.try_get::<Option<i32>, _>("constituent_count")
                        .map_err(r::db)?
                        .map(i64::from),
                ),
            ),
            ("index_level", p_opt_dec(dec("level")?)),
        ]));
    }
    let data_version = r::current_data_version(&state).await?;
    let payload = p_map(vec![
        ("universe", membership(universe)),
        ("from", p_date(start)),
        ("to", p_date(end)),
        ("points", Payload::List(points)),
    ]);
    canonical_response(&payload, Some(end), Some(data_version))
}

// ---------------------------------------------------------------------------------------------
// /listings
// ---------------------------------------------------------------------------------------------

/// `encode_cursor(listed_on, symbol)`: urlsafe base64 of `date|symbol`, padding stripped.
pub fn encode_cursor(listed_on: Option<NaiveDate>, symbol: &str) -> String {
    use base64::Engine;
    let raw = format!(
        "{}|{symbol}",
        crate::wire::py_date(listed_on.unwrap_or_else(no_listing_date))
    );
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(raw.as_bytes())
}

/// `binascii.a2b_base64(s, strict_mode=False)` after `urlsafe_b64decode`'s `-_` → `+/`:
/// characters outside the alphabet are skipped, a complete pad ends the input.
fn a2b_base64_lax(s: &[u8]) -> Option<Vec<u8>> {
    fn value(c: u8) -> Option<u32> {
        match c {
            b'A'..=b'Z' => Some(u32::from(c - b'A')),
            b'a'..=b'z' => Some(u32::from(c - b'a') + 26),
            b'0'..=b'9' => Some(u32::from(c - b'0') + 52),
            b'+' | b'-' => Some(62),
            b'/' | b'_' => Some(63),
            _ => None,
        }
    }
    let mut out = Vec::new();
    let mut quad_pos = 0_u8;
    let mut pads = 0_u8;
    let mut left: u32 = 0;
    for &c in s {
        if c == b'=' {
            if quad_pos >= 2 {
                pads += 1;
                if quad_pos + pads >= 4 {
                    return Some(out);
                }
            }
            continue;
        }
        let Some(v) = value(c) else {
            continue;
        };
        pads = 0;
        let byte = |x: u32| u8::try_from(x & 0xff).unwrap_or(0);
        match quad_pos {
            0 => {
                quad_pos = 1;
                left = v;
            }
            1 => {
                quad_pos = 2;
                out.push(byte((left << 2) | (v >> 4)));
                left = v & 0x0f;
            }
            2 => {
                quad_pos = 3;
                out.push(byte((left << 4) | (v >> 2)));
                left = v & 0x03;
            }
            _ => {
                quad_pos = 0;
                out.push(byte((left << 6) | v));
                left = 0;
            }
        }
    }
    if quad_pos != 0 {
        return None;
    }
    Some(out)
}

/// `date.fromisoformat` (Python 3.12): `YYYY-MM-DD`, `YYYYMMDD`, `YYYY-Www-D`, `YYYYWwwD`.
fn py_fromisoformat(text: &str) -> Option<NaiveDate> {
    let b = text.as_bytes();
    let digits = |s: &[u8]| s.iter().all(u8::is_ascii_digit) && !s.is_empty();
    let num = |s: &[u8]| -> Option<u32> {
        if !digits(s) {
            return None;
        }
        std::str::from_utf8(s).ok()?.parse().ok()
    };
    let (year, rest) = (b.get(..4)?, b.get(4..)?);
    let year = i32::try_from(num(year)?).ok()?;
    if year < 1 {
        return None;
    }
    match rest {
        [b'-', b'W', w1, w2, b'-', d] | [b'W', w1, w2, d] => {
            let week = num(&[*w1, *w2])?;
            let day = num(&[*d])?;
            if !(1..=7).contains(&day) {
                return None;
            }
            let weekday = chrono::Weekday::try_from(u8::try_from(day - 1).ok()?).ok()?;
            NaiveDate::from_isoywd_opt(year, week, weekday)
        }
        [b'-', b'W', w1, w2] | [b'W', w1, w2] => {
            NaiveDate::from_isoywd_opt(year, num(&[*w1, *w2])?, chrono::Weekday::Mon)
        }
        [b'-', m1, m2, b'-', d1, d2] | [m1, m2, d1, d2] => {
            NaiveDate::from_ymd_opt(year, num(&[*m1, *m2])?, num(&[*d1, *d2])?)
        }
        _ => None,
    }
}

/// `decode_cursor(cursor)`; `None` is `InvalidCursor`.
pub fn decode_cursor(cursor: &str) -> Option<(NaiveDate, String)> {
    if !cursor.is_ascii() {
        return None;
    }
    let pad = (4 - cursor.len() % 4) % 4;
    let padded = format!("{cursor}{}", "=".repeat(pad));
    let bytes = a2b_base64_lax(padded.as_bytes())?;
    let raw = String::from_utf8(bytes).ok()?;
    let (date_text, symbol) = raw.split_once('|').unwrap_or((raw.as_str(), ""));
    Some((py_fromisoformat(date_text)?, symbol.to_owned()))
}

/// `ListingQuery`.
struct ListingQuery {
    limit: i64,
    cursor: Option<(NaiveDate, String)>,
    series: Option<String>,
    search: Option<String>,
    start: Option<NaiveDate>,
    end: Option<NaiveDate>,
}

/// One bound value of the register's statement.
enum Bind {
    Date(NaiveDate),
    Text(String),
}

/// `_listings_filters(query, with_cursor=)`: the WHERE clause and its binds.
fn listing_filters(q: &ListingQuery, with_cursor: bool) -> (String, Vec<Bind>) {
    let mut binds = vec![
        Bind::Date(listings_backfill_seam()),
        Bind::Date(no_listing_date()),
    ];
    let mut sql = String::from(
        "is_active IS true AND (listed_on IS NULL OR listed_on != $1) \
         AND NOT (symbol LIKE '%-RE%')",
    );
    if let Some(series) = q.series.as_deref().filter(|s| !s.is_empty()) {
        binds.push(Bind::Text(r::py_upper(series)));
        sql.push_str(&format!(" AND series = ${}", binds.len()));
    }
    if let Some(search) = q.search.as_deref().filter(|s| !s.is_empty()) {
        binds.push(Bind::Text(format!("%{}%", r::py_strip(search))));
        let n = binds.len();
        sql.push_str(&format!(" AND (symbol ILIKE ${n} OR name ILIKE ${n})"));
    }
    if let Some(start) = q.start {
        binds.push(Bind::Date(start));
        sql.push_str(&format!(" AND listed_on >= ${}", binds.len()));
    }
    if let Some(end) = q.end {
        binds.push(Bind::Date(end));
        sql.push_str(&format!(" AND listed_on <= ${}", binds.len()));
    }
    if with_cursor && let Some((after_date, after_symbol)) = &q.cursor {
        binds.push(Bind::Date(*after_date));
        let d = binds.len();
        binds.push(Bind::Text(after_symbol.clone()));
        let s = binds.len();
        sql.push_str(&format!(
            " AND (coalesce(listed_on, $2) < ${d} OR (coalesce(listed_on, $2) = ${d} AND symbol > ${s}))"
        ));
    }
    (sql, binds)
}

fn bind_all<'q>(
    mut query: Query<'q, sqlx::Postgres, PgArguments>,
    binds: &'q [Bind],
) -> Query<'q, sqlx::Postgres, PgArguments> {
    for b in binds {
        query = match b {
            Bind::Date(d) => query.bind(*d),
            Bind::Text(t) => query.bind(t.as_str()),
        };
    }
    query
}

/// `GET /listings`: the NSE listings register, newest first.
///
/// # Errors
/// 400 for a bad parameter or a cursor this endpoint did not issue, 500 for a database failure.
pub async fn get_listings(
    State(state): State<AppState>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let mut p = Params::new(raw.as_deref());
    let limit = p.int(
        "limit",
        Some(DEFAULT_PAGE_SIZE),
        Some(1),
        Some(MAX_PAGE_SIZE),
    );
    let cursor = p.raw("cursor");
    let series = p.string("series", None, Some(8));
    let search = p.string("search", None, Some(64));
    let from = p.date("from");
    let to = p.date("to");
    p.finish()?;
    let cursor = match cursor {
        None => None,
        Some(c) => Some(decode_cursor(&c).ok_or_else(|| {
            invalid(
                "That cursor did not come from this endpoint.",
                "cursor",
                "omit it to start from the first page",
            )
        })?),
    };
    let query = ListingQuery {
        limit: limit.flatten().unwrap_or(DEFAULT_PAGE_SIZE),
        cursor,
        series: series.ok().flatten(),
        search: search.ok().flatten(),
        start: from.ok().flatten(),
        end: to.ok().flatten(),
    };

    let (count_where, count_binds) = listing_filters(&query, false);
    let count_sql = format!("SELECT count(*) FROM instrument WHERE {count_where}");
    let total: i64 = bind_all(sqlx::query(&count_sql), &count_binds)
        .fetch_one(state.db.pool())
        .await
        .map_err(r::db)?
        .try_get(0)
        .map_err(r::db)?;

    let (page_where, page_binds) = listing_filters(&query, true);
    let page_sql = format!(
        "SELECT symbol, name, series, isin, listed_on FROM instrument WHERE {page_where} \
         ORDER BY coalesce(listed_on, $2) DESC, symbol ASC LIMIT {}",
        query.limit + 1
    );
    let rows = bind_all(sqlx::query(&page_sql), &page_binds)
        .fetch_all(state.db.pool())
        .await
        .map_err(r::db)?;
    let has_more = i64::try_from(rows.len()).unwrap_or(i64::MAX) > query.limit;
    let keep = usize::try_from(query.limit).unwrap_or(usize::MAX);
    let mut data = Vec::new();
    let mut last: Option<(Option<NaiveDate>, String)> = None;
    for row in rows.iter().take(keep) {
        let symbol: String = row.try_get("symbol").map_err(r::db)?;
        let name: String = row.try_get("name").map_err(r::db)?;
        let series: Option<String> = row.try_get("series").map_err(r::db)?;
        let isin: Option<String> = row.try_get("isin").map_err(r::db)?;
        let listed_on = baskfy_db::row::opt_date(row, "listed_on").map_err(|e| r::dbe(&e))?;
        data.push(p_map(vec![
            ("symbol", p_str(&symbol)),
            ("name", Payload::Str(name)),
            ("series", p_opt_str(series.as_deref())),
            ("isin", p_opt_str(isin.as_deref())),
            ("listed_on", p_opt_date(listed_on)),
        ]));
        last = Some((listed_on, symbol));
    }
    let next_cursor = match (has_more, last) {
        (true, Some((listed_on, symbol))) => Payload::Str(encode_cursor(listed_on, &symbol)),
        _ => Payload::Null,
    };
    let data_version = r::current_data_version(&state).await?;
    let payload = p_map(vec![
        ("data", Payload::List(data)),
        ("next_cursor", next_cursor),
        ("total", Payload::Int(total)),
    ]);
    canonical_response(&payload, None, Some(data_version))
}

/// The routes of `baskfy_api.routers.market_data`, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/indices/dashboard", get(get_index_dashboard))
        .route("/market-health", get(get_market_health))
        .route("/market-health/history", get(get_market_health_history))
        .route("/listings", get(get_listings))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_cursor_round_trips_and_garbage_is_refused() {
        let c = encode_cursor(NaiveDate::from_ymd_opt(2026, 10, 1), "ABC|D");
        assert_eq!(
            decode_cursor(&c),
            Some((ymd(2026, 10, 1), "ABC|D".to_owned()))
        );
        let none = encode_cursor(None, "X");
        assert_eq!(decode_cursor(&none), Some((ymd(1, 1, 1), "X".to_owned())));
        assert_eq!(decode_cursor(""), None);
        assert_eq!(decode_cursor("not a cursor"), None);
        assert_eq!(decode_cursor("é"), None);
    }

    #[test]
    fn fromisoformat_takes_pythons_spellings() {
        assert_eq!(py_fromisoformat("2026-10-01"), Some(ymd(2026, 10, 1)));
        assert_eq!(py_fromisoformat("20261001"), Some(ymd(2026, 10, 1)));
        assert_eq!(py_fromisoformat("2026-W01-1"), Some(ymd(2025, 12, 29)));
        assert_eq!(py_fromisoformat("2026-13-01"), None);
        assert_eq!(py_fromisoformat("0000-01-01"), None);
    }

    #[test]
    fn the_health_universe_rules() {
        assert!(health_universe("nifty-500").is_ok());
        let e = health_universe("nifty-fno").err().map(|p| p.detail);
        assert_eq!(
            e.as_deref(),
            Some("NIFTY FNO has no breadth series; docs/01 §6 covers twelve universes.")
        );
        let e = health_universe("x").err().map(|p| p.detail);
        assert_eq!(e.as_deref(), Some("'x' is not a universe."));
    }
}
