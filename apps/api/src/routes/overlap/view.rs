//! `baskfy_api.overlap` — the day's candidates across the sleeves, one row per stock, from what
//! was stored (wave 6, lane api_routes; R5.21 closed). Never re-run: every row is the scans' own
//! facts restated, a screen's newest stored rank, the swing feed's filing and results date, the
//! tag on that headline and the attention opinion on the row — **display context**, read by no
//! rank, size or order path.
//!
//! One function, [`overlap`], serves `GET /overlap`, the review labels' re-read of a row
//! (`_row_on_the_list`) and the worker's `baskfy.overlap.catalyst_scan` (which passes no cache, as
//! Python's does). Every statement is Python's, on one connection; the two cache notes Python
//! makes on the read path (`SADD catalyst_tag:wanted`, `HSET candidate_review:wanted`, each with
//! a three-day TTL) are made the same way, atomically, and only when a cache is configured.

use std::collections::{BTreeSet, HashMap};

use baskfy_core::decimal::WideDecimal;
use baskfy_db::OrderedJson;
use baskfy_providers::ratelimit::ScriptValue;
use baskfy_providers::secret::Secret;
use chrono::{DateTime, Duration, NaiveDate, Utc};
use rust_decimal::Decimal;
use sqlx::postgres::PgRow;
use sqlx::{PgConnection, Row};

use super::review::{
    self, Num, ReviewOpinion, RowContext, RowFacts, SOURCE_LABELLED, opinion_from_laya, review_key,
    review_state, rules_opinion,
};
use super::tags::{
    EVENT_TYPES, Tag, cache_key, priority_of, resolve_tag, tag_from_laya, tag_headline,
};
use crate::problems::{Problem, internal_error};
use crate::routes::options::derivatives::shared::{col, db_err};
use crate::routes::sleeves::pyd::py_strip;

/// `sw_catalyst.source` of an exchange announcement.
pub const ANNOUNCEMENT: &str = "NSE_ANNOUNCEMENT";
/// `sw_catalyst.source` of the results calendar.
pub const EVENT_CALENDAR: &str = "NSE_EVENT_CALENDAR";
/// `MATERIAL_WINDOW_DAYS`.
pub const MATERIAL_WINDOW_DAYS: i64 = 45;
/// `FILINGS_PER_NAME`.
pub const FILINGS_PER_NAME: usize = 40;
/// `WANTED_KEY` / `WANTED_TTL_S`.
pub const WANTED_KEY: &str = "catalyst_tag:wanted";
/// Three days.
pub const WANTED_TTL_S: i64 = 3 * 24 * 3600;
/// `HEARTBEAT_KEY`.
pub const HEARTBEAT_KEY: &str = "catalyst_tag:heartbeat";
/// `REVIEW_WANTED_KEY`.
pub const REVIEW_WANTED_KEY: &str = "candidate_review:wanted";

/// `Strategy` values, in the order the readers run.
pub const STRATEGIES: [&str; 3] = ["swing", "volume_breakout", "three_weeks_tight"];

/// `STRATEGY_NAMES`.
#[must_use]
pub fn strategy_name(strategy: &str) -> &'static str {
    match strategy {
        "swing" => "Swing",
        "volume_breakout" => "Volume breakout",
        _ => "Three weeks tight",
    }
}

/// `STRATEGY_REFS`.
#[must_use]
pub fn strategy_ref(strategy: &str) -> &'static str {
    match strategy {
        "swing" => "/swing",
        "volume_breakout" => "/vbt",
        _ => "/twt",
    }
}

/// `StrategyHit`.
#[derive(Debug, Clone)]
pub struct StrategyHit {
    /// `strategy`.
    pub strategy: &'static str,
    /// `as_of`.
    pub as_of: NaiveDate,
    /// `detail`.
    pub detail: String,
    /// `actionable`.
    pub actionable: bool,
    /// `close`.
    pub close: Option<Decimal>,
    /// `numbers`.
    pub numbers: Vec<(&'static str, Num)>,
}

/// `ScreenHit`.
#[derive(Debug, Clone)]
pub struct ScreenHit {
    /// `name`.
    pub name: String,
    /// `public_id`.
    pub public_id: String,
    /// `is_template`.
    pub is_template: bool,
    /// `as_of`.
    pub as_of: NaiveDate,
    /// `rank`.
    pub rank: Option<i64>,
    /// `of`.
    pub of: Option<i64>,
    /// `definition_changed`.
    pub definition_changed: bool,
}

/// `CatalystView`.
#[derive(Debug, Clone)]
pub struct CatalystView {
    /// `headline`.
    pub headline: Option<String>,
    /// `published_at`.
    pub published_at: Option<DateTime<Utc>>,
    /// `url`.
    pub url: Option<String>,
    /// `earnings_date`.
    pub earnings_date: Option<NaiveDate>,
}

impl CatalystView {
    /// `empty`: nothing to link out to.
    #[must_use]
    pub fn empty(&self) -> bool {
        self.url.is_none() && self.earnings_date.is_none()
    }
}

/// `CandidateRow`.
#[derive(Debug, Clone)]
pub struct CandidateRow {
    /// `instrument_id`.
    pub instrument_id: i64,
    /// `symbol`.
    pub symbol: String,
    /// `name`.
    pub name: String,
    /// `close`.
    pub close: Option<Decimal>,
    /// `strategies`.
    pub strategies: Vec<StrategyHit>,
    /// `screens`.
    pub screens: Vec<ScreenHit>,
    /// `catalyst`.
    pub catalyst: Option<CatalystView>,
    /// `catalyst_tag`.
    pub(crate) catalyst_tag: Option<Tag>,
    /// `opinion`.
    pub opinion: Option<ReviewOpinion>,
    /// `laya`.
    pub laya: Option<ReviewOpinion>,
    /// `context`.
    pub context: Option<RowContext>,
}

impl CandidateRow {
    /// `facts()`.
    #[must_use]
    pub fn facts(&self) -> Vec<RowFacts> {
        self.strategies
            .iter()
            .map(|h| RowFacts {
                strategy: h.strategy,
                detail: h.detail.clone(),
                actionable: h.actionable,
                numbers: h.numbers.clone(),
            })
            .collect()
    }

    /// `state()`: what Laya is shown, and what a label is keyed on.
    #[must_use]
    pub fn state(&self) -> Vec<(&'static str, String)> {
        review_state(
            &self.facts(),
            self.catalyst.as_ref().and_then(|c| c.headline.as_deref()),
            self.context.as_ref(),
        )
    }

    /// `strategy_count`.
    #[must_use]
    pub fn strategy_count(&self) -> usize {
        self.strategies.len()
    }

    /// `actionable`.
    #[must_use]
    pub fn actionable(&self) -> bool {
        self.strategies.iter().any(|h| h.actionable)
    }
}

/// `OverlapView`.
#[derive(Debug, Clone)]
pub struct OverlapView {
    /// `sessions`, in [`STRATEGIES`] order.
    pub sessions: [Option<NaiveDate>; 3],
    /// `rows`.
    pub rows: Vec<CandidateRow>,
    /// `strategies_read`.
    pub strategies_read: bool,
    /// `screens_checked`.
    pub screens_checked: i64,
}

/// One instrument as the readers join it.
#[derive(Debug, Clone)]
struct Instrument {
    id: i64,
    symbol: String,
    name: String,
}

fn instrument(r: &PgRow) -> Result<Instrument, Problem> {
    Ok(Instrument {
        id: col::i64(r, "i_id")?,
        symbol: col::text(r, "i_symbol")?,
        name: col::text(r, "i_name")?,
    })
}

fn dec(r: &PgRow, c: &str) -> Result<Num, Problem> {
    Ok(Num::dec(col::opt_dec(r, c)?))
}

fn small(r: &PgRow, c: &str) -> Result<Option<i64>, Problem> {
    r.try_get::<Option<i16>, _>(c)
        .map(|v| v.map(i64::from))
        .map_err(db_err)
}

fn big(r: &PgRow, c: &str) -> Result<Num, Problem> {
    Ok(Num::int(col::opt_i64(r, c)?))
}

fn flag(r: &PgRow, c: &str) -> Result<Num, Problem> {
    Ok(Num::bool(r.try_get::<Option<bool>, _>(c).map_err(db_err)?))
}

/// `", ".join(failed_filters)` or `None` (an empty or absent list).
fn failed(r: &PgRow, c: &str) -> Result<Num, Problem> {
    let list: Option<Vec<String>> = r.try_get(c).map_err(db_err)?;
    Ok(match list {
        Some(items) if !items.is_empty() => Num::Str(items.join(", ")),
        _ => Num::Null,
    })
}

type Found = (Option<NaiveDate>, Vec<(Instrument, StrategyHit)>);

/// `swing.latest_detected_date`: the market row's newest date, else the setups'.
async fn latest_detected_date(
    conn: &mut PgConnection,
    user_id: i64,
) -> Result<Option<NaiveDate>, Problem> {
    let market: Option<NaiveDate> =
        sqlx::query_scalar("SELECT max(date) FROM sw_market_daily WHERE user_id = $1")
            .bind(user_id)
            .fetch_one(&mut *conn)
            .await
            .map_err(db_err)?;
    if market.is_some() {
        return Ok(market);
    }
    sqlx::query_scalar("SELECT max(date) FROM sw_setup_daily WHERE user_id = $1")
        .bind(user_id)
        .fetch_one(&mut *conn)
        .await
        .map_err(db_err)
}

/// `TRADEABLE_SETUPS` (PARABOLIC_SHORT is detect-only).
const TRADEABLE_SETUPS: [&str; 2] = ["FLAG", "EP"];

async fn swing(conn: &mut PgConnection, user_id: i64) -> Result<Found, Problem> {
    let Some(day) = latest_detected_date(conn, user_id).await? else {
        return Ok((None, Vec::new()));
    };
    let rows = sqlx::query(
        "SELECT s.*, i.id AS i_id, i.symbol AS i_symbol, i.name AS i_name FROM sw_setup_daily s \
         JOIN instrument i ON i.id = s.instrument_id WHERE s.user_id = $1 AND s.date = $2 \
         ORDER BY s.instrument_id, s.setup",
    )
    .bind(user_id)
    .bind(day)
    .fetch_all(&mut *conn)
    .await
    .map_err(db_err)?;
    let mut hits = Vec::with_capacity(rows.len());
    for r in &rows {
        let setup = col::text(r, "setup")?;
        let status = col::text(r, "status")?;
        let close = col::opt_dec(r, "close")?;
        hits.push((
            instrument(r)?,
            StrategyHit {
                strategy: "swing",
                as_of: day,
                detail: format!("{setup} · {status}"),
                actionable: TRADEABLE_SETUPS.contains(&setup.as_str()),
                close,
                numbers: vec![
                    ("score", dec(r, "score")?),
                    ("close", Num::dec(close)),
                    ("trigger", dec(r, "trigger")?),
                    ("stop_ref", dec(r, "stop_ref")?),
                    ("pivot_high", dec(r, "pivot_high")?),
                    ("gap_pct", dec(r, "gap_pct")?),
                    ("rvol", dec(r, "rvol")?),
                    ("base_depth_pct", dec(r, "base_depth_pct")?),
                    ("base_bars", Num::int(small(r, "base_bars")?)),
                    ("tightness_adr", dec(r, "tightness_adr")?),
                    ("dryup_ratio", dec(r, "dryup_ratio")?),
                    ("prior_move_pct", dec(r, "prior_move_pct")?),
                    ("adr_pct", dec(r, "adr_pct")?),
                    ("dist_ma_fast_pct", dec(r, "dist_ma_fast_pct")?),
                    ("dist_ma_slow_pct", dec(r, "dist_ma_slow_pct")?),
                    ("up_streak", Num::int(small(r, "up_streak")?)),
                    ("turnover_avg", big(r, "turnover_avg")?),
                    ("locked_upper_circuit", flag(r, "locked_upper_circuit")?),
                    ("listed_within_2y", flag(r, "listed_within_2y")?),
                    ("sector_slug", Num::str(col::opt_text(r, "sector_slug")?)),
                ],
            },
        ));
    }
    Ok((Some(day), hits))
}

/// `_VBT_STATE.get(state, state.lower().replace("_", " "))`.
fn vbt_state(state: &str) -> String {
    match state {
        "SIGNAL" => "signal".to_owned(),
        "SCAN_ONLY" => "scanned, did not pass the filters".to_owned(),
        other => other.to_lowercase().replace('_', " "),
    }
}

async fn latest_breadth(
    conn: &mut PgConnection,
    table: &str,
    user_id: i64,
) -> Result<Option<NaiveDate>, Problem> {
    sqlx::query_scalar(&format!(
        "SELECT date FROM {table} WHERE user_id = $1 ORDER BY date DESC LIMIT 1"
    ))
    .bind(user_id)
    .fetch_optional(&mut *conn)
    .await
    .map_err(db_err)
}

async fn volume_breakout(conn: &mut PgConnection, user_id: i64) -> Result<Found, Problem> {
    let Some(day) = latest_breadth(conn, "vb_breadth_daily", user_id).await? else {
        return Ok((None, Vec::new()));
    };
    let rows = sqlx::query(
        "SELECT s.*, i.id AS i_id, i.symbol AS i_symbol, i.name AS i_name FROM vb_signal_daily s \
         JOIN instrument i ON i.id = s.instrument_id WHERE s.user_id = $1 AND s.date = $2 \
         ORDER BY s.instrument_id",
    )
    .bind(user_id)
    .bind(day)
    .fetch_all(&mut *conn)
    .await
    .map_err(db_err)?;
    let mut hits = Vec::with_capacity(rows.len());
    for r in &rows {
        let state = col::text(r, "state")?;
        let close = col::opt_dec(r, "close_raw")?;
        hits.push((
            instrument(r)?,
            StrategyHit {
                strategy: "volume_breakout",
                as_of: day,
                detail: vbt_state(&state),
                actionable: state == "SIGNAL",
                close,
                numbers: vec![
                    ("close", Num::dec(close)),
                    ("rvol", dec(r, "rvol")?),
                    ("change_pct", dec(r, "change_pct")?),
                    ("close_position", dec(r, "close_position")?),
                    ("ret_20_pct", dec(r, "ret_20_pct")?),
                    ("volume", big(r, "volume")?),
                    ("vol_sma_50", big(r, "vol_sma_50")?),
                    ("turnover_avg_20", big(r, "turnover_avg_20")?),
                    ("sma_200", dec(r, "sma_200")?),
                    ("ema_21", dec(r, "ema_21")?),
                    ("high_20_prior", dec(r, "high_20_prior")?),
                    ("limit_price", dec(r, "limit_price")?),
                    ("stop_price", dec(r, "stop_price")?),
                    ("locked_upper_circuit", flag(r, "locked_upper_circuit")?),
                    ("failed_filters", failed(r, "failed_filters")?),
                ],
            },
        ));
    }
    Ok((Some(day), hits))
}

async fn three_weeks_tight(conn: &mut PgConnection, user_id: i64) -> Result<Found, Problem> {
    let Some(day) = latest_breadth(conn, "tw_breadth_daily", user_id).await? else {
        return Ok((None, Vec::new()));
    };
    let rows = sqlx::query(
        "SELECT s.*, i.id AS i_id, i.symbol AS i_symbol, i.name AS i_name, \
         e.instrument_id AS e_instrument_id, e.state AS e_state, \
         e.sessions_out_before AS e_sessions_out_before, e.stop_preview AS e_stop_preview, \
         e.failed_filters AS e_failed_filters FROM tw_state_daily s \
         JOIN instrument i ON i.id = s.instrument_id \
         LEFT OUTER JOIN tw_signal_daily e ON e.user_id = s.user_id AND e.date = s.date \
         AND e.instrument_id = s.instrument_id \
         WHERE s.user_id = $1 AND s.date = $2 ORDER BY s.instrument_id",
    )
    .bind(user_id)
    .bind(day)
    .fetch_all(&mut *conn)
    .await
    .map_err(db_err)?;
    let mut hits = Vec::with_capacity(rows.len());
    for r in &rows {
        let has_event = col::opt_i64(r, "e_instrument_id")?.is_some();
        let signal = if has_event {
            col::opt_text(r, "e_state")?
        } else {
            None
        };
        let sessions = small(r, "sessions_in_state")?;
        let mut detail = match sessions {
            Some(n) => format!("tight {n} session{}", if n == 1 { "" } else { "s" }),
            None => "tight None sessions".to_owned(),
        };
        if let Some(sig) = signal.as_deref().filter(|s| !s.is_empty()) {
            detail.push_str(&format!(" · {}", sig.to_lowercase().replace('_', " ")));
        }
        let close = col::opt_dec(r, "close_raw")?;
        let (out, stop, failed_text) = if has_event {
            (
                Num::int(small(r, "e_sessions_out_before")?),
                dec(r, "e_stop_preview")?,
                failed(r, "e_failed_filters")?,
            )
        } else {
            (Num::Null, Num::Null, Num::Null)
        };
        hits.push((
            instrument(r)?,
            StrategyHit {
                strategy: "three_weeks_tight",
                as_of: day,
                detail,
                actionable: signal.as_deref() == Some("SIGNAL"),
                close,
                numbers: vec![
                    ("close", Num::dec(close)),
                    ("week_range_pct", dec(r, "week_range_pct")?),
                    ("week_close_0", dec(r, "week_close_0")?),
                    ("week_close_1", dec(r, "week_close_1")?),
                    ("week_close_2", dec(r, "week_close_2")?),
                    ("sessions_in_state", Num::int(sessions)),
                    ("month_low_ratio", dec(r, "month_low_ratio")?),
                    ("sma_dma", dec(r, "sma_dma")?),
                    ("volume", big(r, "volume")?),
                    ("vol_sma_50", big(r, "vol_sma_50")?),
                    ("turnover_avg_20", big(r, "turnover_avg_20")?),
                    ("locked_upper_circuit", flag(r, "locked_upper_circuit")?),
                    ("sessions_out_before", out),
                    ("stop_preview", stop),
                    ("failed_filters", failed_text),
                ],
            },
        ));
    }
    Ok((Some(day), hits))
}

/// `isinstance(value, int)` for a stored JSON number (a `bool` is an `int` in Python).
fn py_int(v: Option<&OrderedJson>) -> Option<i64> {
    match v {
        Some(OrderedJson::Int(i)) => Some(*i),
        Some(OrderedJson::UInt(u)) => i64::try_from(*u).ok(),
        Some(OrderedJson::Bool(b)) => Some(i64::from(*b)),
        _ => None,
    }
}

/// `_screens(session, user_id=, instrument_ids=)`: the newest stored run of each visible screen,
/// inverted to instrument → hits; and how many screens were checked.
async fn screens(
    conn: &mut PgConnection,
    user_id: i64,
    instrument_ids: &BTreeSet<i64>,
) -> Result<(HashMap<i64, Vec<ScreenHit>>, i64), Problem> {
    let screens = sqlx::query(
        "SELECT id, public_id, name, definition, is_example FROM screen \
         WHERE user_id = $1 OR is_example IS true ORDER BY is_example, name",
    )
    .bind(user_id)
    .fetch_all(&mut *conn)
    .await
    .map_err(db_err)?;
    let checked = i64::try_from(screens.len()).unwrap_or(i64::MAX);
    if screens.is_empty() || instrument_ids.is_empty() {
        return Ok((HashMap::new(), checked));
    }
    let ids: Vec<i64> = screens
        .iter()
        .map(|s| col::i64(s, "id"))
        .collect::<Result<_, _>>()?;
    let newest = sqlx::query(
        "SELECT DISTINCT ON (screen_id) screen_id, as_of, definition_hash, result_count, results \
         FROM screen_run WHERE screen_id = ANY($1) \
         ORDER BY screen_id, as_of DESC, created_at DESC",
    )
    .bind(&ids)
    .fetch_all(&mut *conn)
    .await
    .map_err(db_err)?;
    let runs: HashMap<i64, &PgRow> = newest
        .iter()
        .map(|r| Ok((col::i64(r, "screen_id")?, r)))
        .collect::<Result<_, Problem>>()?;
    let mut hits: HashMap<i64, Vec<ScreenHit>> = HashMap::new();
    for screen in &screens {
        let Some(run) = runs.get(&col::i64(screen, "id")?) else {
            continue;
        };
        let definition = col::object(screen, "definition")?;
        let stored_hash = col::opt_text(run, "definition_hash")?;
        let changed = crate::routes::instruments::definition_hash(&definition) != stored_hash;
        let name = col::text(screen, "name")?;
        let public_id = col::text(screen, "public_id")?;
        let is_template = col::boolean(screen, "is_example")?;
        let as_of = col::date(run, "as_of")?;
        let of = r_int(run, "result_count")?;
        let results = col::opt_json(run, "results")?.unwrap_or(OrderedJson::Null);
        let OrderedJson::Array(items) = results else {
            continue;
        };
        for result in &items {
            let Some(instrument_id) = py_int(result.get("instrument_id")) else {
                continue;
            };
            if !instrument_ids.contains(&instrument_id) {
                continue;
            }
            hits.entry(instrument_id).or_default().push(ScreenHit {
                name: name.clone(),
                public_id: public_id.clone(),
                is_template,
                as_of,
                rank: py_int(result.get("rank")),
                of,
                definition_changed: changed,
            });
        }
    }
    Ok((hits, checked))
}

/// An `integer` column that may be null.
fn r_int(r: &PgRow, c: &str) -> Result<Option<i64>, Problem> {
    r.try_get::<Option<i32>, _>(c)
        .map(|v| v.map(i64::from))
        .or_else(|_| col::opt_i64(r, c))
}

/// `PRIORITY_OF` as `_PRIORITY_RANK`.
fn priority_rank(event_type: &str) -> u8 {
    match priority_of(event_type) {
        "high" => 2,
        "medium" => 1,
        _ => 0,
    }
}

/// One `sw_catalyst` row.
struct Filing {
    id: i64,
    headline: Option<String>,
    published_at: Option<DateTime<Utc>>,
    url: Option<String>,
}

/// `best_catalyst_for(session, user_id=, instrument_ids=, today=)`: per name, the highest
/// priority filing inside the material window (newest first within it), else the newest of any
/// age; the results date is the calendar's.
async fn best_catalyst_for(
    conn: &mut PgConnection,
    user_id: i64,
    instrument_ids: &[i64],
    today: NaiveDate,
) -> Result<HashMap<i64, CatalystView>, Problem> {
    let wanted: Vec<i64> = instrument_ids
        .iter()
        .copied()
        .collect::<BTreeSet<i64>>()
        .into_iter()
        .collect();
    if wanted.is_empty() {
        return Ok(HashMap::new());
    }
    // `latest_for`: the calendar's earnings date per name (the first dated calendar row in the
    // feed's order).
    let all = sqlx::query(
        "SELECT instrument_id, source, earnings_date FROM sw_catalyst WHERE user_id = $1 \
         AND instrument_id = ANY($2) ORDER BY instrument_id, published_at DESC NULLS LAST, id DESC",
    )
    .bind(user_id)
    .bind(&wanted)
    .fetch_all(&mut *conn)
    .await
    .map_err(db_err)?;
    let mut earnings: HashMap<i64, NaiveDate> = HashMap::new();
    for r in &all {
        if col::text(r, "source")? == EVENT_CALENDAR
            && let Some(e) = col::opt_date(r, "earnings_date")?
        {
            earnings.entry(col::i64(r, "instrument_id")?).or_insert(e);
        }
    }
    let rows = sqlx::query(
        "SELECT id, instrument_id, headline, published_at, url FROM sw_catalyst \
         WHERE user_id = $1 AND instrument_id = ANY($2) AND source = $3 \
         ORDER BY instrument_id, published_at DESC NULLS LAST, id DESC",
    )
    .bind(user_id)
    .bind(&wanted)
    .bind(ANNOUNCEMENT)
    .fetch_all(&mut *conn)
    .await
    .map_err(db_err)?;
    let mut per_name: HashMap<i64, Vec<Filing>> = HashMap::new();
    for r in &rows {
        let list = per_name.entry(col::i64(r, "instrument_id")?).or_default();
        if list.len() < FILINGS_PER_NAME {
            list.push(Filing {
                id: col::i64(r, "id")?,
                headline: col::opt_text(r, "headline")?,
                published_at: col::opt_ts(r, "published_at")?,
                url: col::opt_text(r, "url")?,
            });
        }
    }
    let horizon = today - Duration::days(MATERIAL_WINDOW_DAYS);
    let mut chosen = HashMap::new();
    for id in wanted {
        let earnings_date = earnings.get(&id).copied();
        let filings = per_name.remove(&id).unwrap_or_default();
        if filings.is_empty() {
            if earnings_date.is_some() {
                chosen.insert(
                    id,
                    CatalystView {
                        headline: None,
                        published_at: None,
                        url: None,
                        earnings_date,
                    },
                );
            }
            continue;
        }
        let recent: Vec<&Filing> = filings
            .iter()
            .filter(|f| f.published_at.is_some_and(|p| p.date_naive() >= horizon))
            .collect();
        let best = recent
            .iter()
            .copied()
            .max_by(|a, b| {
                let key = |f: &Filing| {
                    (
                        priority_rank(tag_headline(f.headline.as_deref().unwrap_or("")).event_type),
                        f.published_at,
                        f.id,
                    )
                };
                key(a).cmp(&key(b))
            })
            .or_else(|| filings.first());
        if let Some(best) = best {
            chosen.insert(
                id,
                CatalystView {
                    headline: best.headline.clone(),
                    published_at: best.published_at,
                    url: best.url.clone(),
                    earnings_date,
                },
            );
        }
    }
    Ok(chosen)
}

/// `MGET` the keys, then (atomically) note the misses: `SADD set ... EXPIRE` or
/// `HSET hash ... EXPIRE`. Returns each key's stored bytes.
const LAYA_MGET_SCRIPT: &str = "local n = #KEYS - 1 \
    local out = {} \
    local missing = {} \
    for i = 1, n do \
      local v = redis.call('GET', KEYS[i]) \
      out[i] = v \
      if not v then table.insert(missing, ARGV[i]) end \
    end \
    if #missing > 0 then \
      redis.call('SADD', KEYS[n + 1], unpack(missing)) \
      redis.call('EXPIRE', KEYS[n + 1], ARGV[n + 1]) \
    end \
    return out";

const REVIEW_MGET_SCRIPT: &str = "local n = #KEYS - 1 \
    local out = {} \
    local missing = {} \
    for i = 1, n do \
      local v = redis.call('GET', KEYS[i]) \
      out[i] = v \
      if not v then \
        table.insert(missing, KEYS[i]) \
        table.insert(missing, ARGV[i]) \
      end \
    end \
    if #missing > 0 then \
      redis.call('HSET', KEYS[n + 1], unpack(missing)) \
      redis.call('EXPIRE', KEYS[n + 1], ARGV[n + 1]) \
    end \
    return out";

fn usable(cache: Option<&Secret>) -> Option<&Secret> {
    cache.filter(|c| !c.expose().is_empty())
}

async fn mget_noting(
    cache: &Secret,
    script: &'static str,
    keys: Vec<String>,
    args: Vec<String>,
    target: &str,
) -> Option<Vec<Option<Vec<u8>>>> {
    let n = keys.len();
    let mut all_keys = keys;
    all_keys.push(target.to_owned());
    let mut all_args = args;
    all_args.push(WANTED_TTL_S.to_string());
    match crate::cache::eval(cache, script, &all_keys, &all_args).await {
        Ok(ScriptValue::Array(items)) if items.len() == n => Some(
            items
                .into_iter()
                .map(|v| match v {
                    ScriptValue::Bytes(b) => Some(b),
                    _ => None,
                })
                .collect(),
        ),
        Ok(ScriptValue::Array(items)) => {
            // Lua drops a table's trailing nils; pad them back.
            let mut out: Vec<Option<Vec<u8>>> = items
                .into_iter()
                .map(|v| match v {
                    ScriptValue::Bytes(b) => Some(b),
                    _ => None,
                })
                .collect();
            out.resize(n, None);
            Some(out)
        }
        Ok(_) => Some(vec![None; n]),
        Err(e) => {
            crate::logging::warning(
                "baskfy_api.overlap",
                &format!("could not read the sidecar's answers: {e}"),
                &[],
            );
            None
        }
    }
}

/// `laya_answers(cache, headlines)`: the sidecar's cached answer per headline; the misses are
/// noted in the wanted set.
async fn laya_answers(
    cache: Option<&Secret>,
    headlines: &[String],
) -> HashMap<String, OrderedJson> {
    let Some(cache) = usable(cache) else {
        return HashMap::new();
    };
    if headlines.is_empty() {
        return HashMap::new();
    }
    let keys: Vec<String> = headlines.iter().map(|h| cache_key(h)).collect();
    let Some(values) = mget_noting(
        cache,
        LAYA_MGET_SCRIPT,
        keys,
        headlines.to_vec(),
        WANTED_KEY,
    )
    .await
    else {
        return HashMap::new();
    };
    let mut found = HashMap::new();
    for (headline, value) in headlines.iter().zip(values) {
        if let Some(parsed) = value
            .and_then(|b| String::from_utf8(b).ok())
            .and_then(|t| OrderedJson::parse(&t).ok())
        {
            found.insert(headline.clone(), parsed);
        }
    }
    found
}

/// `review_opinions(cache, states)`: the sidecar's cached opinion per row key; the misses are
/// queued for it with their state JSON.
pub async fn review_opinions(
    cache: Option<&Secret>,
    states: &[(String, Vec<(&'static str, String)>)],
) -> HashMap<String, ReviewOpinion> {
    let Some(cache) = usable(cache) else {
        return HashMap::new();
    };
    if states.is_empty() {
        return HashMap::new();
    }
    let keys: Vec<String> = states.iter().map(|(k, _)| k.clone()).collect();
    let args: Vec<String> = states.iter().map(|(_, s)| review::state_json(s)).collect();
    let Some(values) = mget_noting(cache, REVIEW_MGET_SCRIPT, keys, args, REVIEW_WANTED_KEY).await
    else {
        return HashMap::new();
    };
    let mut found = HashMap::new();
    for ((key, _), value) in states.iter().zip(values) {
        let Some(bytes) = value else { continue };
        let opinion = String::from_utf8(bytes)
            .ok()
            .and_then(|t| OrderedJson::parse(&t).ok())
            .and_then(|j| opinion_from_laya(&j));
        if let Some(o) = opinion {
            found.insert(key.clone(), o);
        }
    }
    found
}

/// `corrections_for(session, user_id=, headlines=)`.
async fn corrections_for(
    conn: &mut PgConnection,
    user_id: i64,
    headlines: &[String],
) -> Result<HashMap<String, &'static str>, Problem> {
    if headlines.is_empty() {
        return Ok(HashMap::new());
    }
    let mut by_key: Vec<(String, Vec<String>)> = Vec::new();
    for h in headlines {
        let key = cache_key(h);
        match by_key.iter_mut().find(|(k, _)| *k == key) {
            Some((_, list)) => list.push(h.clone()),
            None => by_key.push((key, vec![h.clone()])),
        }
    }
    let keys: Vec<String> = by_key.iter().map(|(k, _)| k.clone()).collect();
    let rows = sqlx::query(
        "SELECT headline_key, event_type FROM catalyst_tag_correction WHERE user_id = $1 \
         AND headline_key = ANY($2)",
    )
    .bind(user_id)
    .bind(&keys)
    .fetch_all(&mut *conn)
    .await
    .map_err(db_err)?;
    let mut out = HashMap::new();
    for r in &rows {
        let key = col::text(r, "headline_key")?;
        let event = col::text(r, "event_type")?;
        let event: &'static str = EVENT_TYPES
            .iter()
            .copied()
            .find(|e| *e == event)
            .ok_or_else(internal_error)?;
        if let Some((_, list)) = by_key.iter().find(|(k, _)| *k == key) {
            for h in list {
                out.insert(h.clone(), event);
            }
        }
    }
    Ok(out)
}

/// `labels_for(session, user_id=, keys=)`.
async fn labels_for(
    conn: &mut PgConnection,
    user_id: i64,
    keys: &[String],
) -> Result<HashMap<String, &'static str>, Problem> {
    if keys.is_empty() {
        return Ok(HashMap::new());
    }
    let sorted: Vec<String> = keys
        .iter()
        .cloned()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let rows = sqlx::query(
        "SELECT review_key, label FROM candidate_review_label WHERE user_id = $1 \
         AND review_key = ANY($2)",
    )
    .bind(user_id)
    .bind(&sorted)
    .fetch_all(&mut *conn)
    .await
    .map_err(db_err)?;
    let mut out = HashMap::new();
    for r in &rows {
        let label = col::text(r, "label")?;
        let label: &'static str = review::REVIEW_LABELS
            .iter()
            .copied()
            .find(|l| *l == label)
            .ok_or_else(internal_error)?;
        out.insert(col::text(r, "review_key")?, label);
    }
    Ok(out)
}

/// `_gates(session, user_id=, sessions=)`: each strategy's gate word (and breadth) on its own
/// latest session, in strategy order.
async fn gates(
    conn: &mut PgConnection,
    user_id: i64,
    sessions: &[Option<NaiveDate>; 3],
) -> Result<(Vec<(String, String)>, Vec<(String, WideDecimal)>), Problem> {
    let mut gates = Vec::new();
    let mut breadth = Vec::new();
    if let Some(day) = sessions[0] {
        let market: Option<Option<String>> =
            sqlx::query_scalar("SELECT gate FROM sw_market_daily WHERE user_id = $1 AND date = $2")
                .bind(user_id)
                .bind(day)
                .fetch_optional(&mut *conn)
                .await
                .map_err(db_err)?;
        if let Some(Some(gate)) = market
            && !gate.is_empty()
        {
            gates.push(("Swing".to_owned(), gate));
        }
    }
    for (i, table, name) in [
        (1, "vb_breadth_daily", "Volume breakout"),
        (2, "tw_breadth_daily", "Three weeks tight"),
    ] {
        let Some(day) = sessions[i] else { continue };
        let row = sqlx::query(&format!(
            "SELECT gate, pct_above_dma FROM {table} WHERE user_id = $1 AND date = $2"
        ))
        .bind(user_id)
        .bind(day)
        .fetch_optional(&mut *conn)
        .await
        .map_err(db_err)?;
        if let Some(r) = row {
            let gate = col::opt_text(&r, "gate")?.unwrap_or_else(|| "None".to_owned());
            gates.push((name.to_owned(), gate));
            if let Some(pct) = col::opt_dec(&r, "pct_above_dma")? {
                breadth.push((name.to_owned(), WideDecimal::from(pct)));
            }
        }
    }
    Ok((gates, breadth))
}

/// `dict` update semantics: a repeated key keeps its first place and takes the last value.
fn put<V>(entries: &mut Vec<(String, V)>, key: String, value: V) {
    if let Some(slot) = entries.iter_mut().find(|(k, _)| *k == key) {
        slot.1 = value;
    } else {
        entries.push((key, value));
    }
}

/// `_context(row, gates, breadth)`.
fn context(
    row: &CandidateRow,
    gates: &[(String, String)],
    breadth: &[(String, WideDecimal)],
) -> RowContext {
    let names: BTreeSet<&str> = row
        .strategies
        .iter()
        .map(|h| strategy_name(h.strategy))
        .collect();
    let sector = row.strategies.iter().find_map(|h| {
        h.numbers.iter().find_map(|(k, v)| match (k, v) {
            (&"sector_slug", Num::Str(s)) => Some(s.clone()),
            _ => None,
        })
    });
    let mut sessions = Vec::new();
    for h in &row.strategies {
        put(&mut sessions, strategy_name(h.strategy).to_owned(), h.as_of);
    }
    let mut screen_runs = Vec::new();
    for s in &row.screens {
        put(&mut screen_runs, s.name.clone(), s.as_of);
    }
    RowContext {
        gates: gates
            .iter()
            .filter(|(n, _)| names.contains(n.as_str()))
            .cloned()
            .collect(),
        breadth_pct: breadth
            .iter()
            .filter(|(n, _)| names.contains(n.as_str()))
            .cloned()
            .collect(),
        sector,
        screens: row
            .screens
            .iter()
            .map(|s| (s.name.clone(), s.rank, s.of))
            .collect(),
        sessions,
        filing_published: row
            .catalyst
            .as_ref()
            .and_then(|c| c.published_at)
            .map(review::ist_date),
        earnings_date: row.catalyst.as_ref().and_then(|c| c.earnings_date),
        screen_runs,
        screens_changed: row
            .screens
            .iter()
            .filter(|s| s.definition_changed)
            .map(|s| s.name.clone())
            .collect(),
    }
}

/// `_rules_opinion(row)`.
fn row_rules_opinion(row: &CandidateRow) -> ReviewOpinion {
    let tag = row.catalyst_tag.as_ref();
    rules_opinion(
        &row.facts(),
        row.catalyst.as_ref().and_then(|c| c.headline.as_deref()),
        tag.map(|t| t.event_type),
        tag.map(|t| priority_of(t.event_type)),
        row.context.as_ref(),
    )
}

/// `overlap(session, user_id=, strategies_user_id=, scope=, cache=)`: every stock on any
/// strategy's latest session, with what each strategy said about it. `today_utc` is
/// `datetime.now(UTC).date()` (the material window's horizon); `cache` is the deployment's Redis
/// URL (`None` for the worker, as Python's task passes none).
///
/// # Errors
/// 500 (the database; a stored enum Python would refuse).
pub async fn overlap(
    conn: &mut PgConnection,
    user_id: i64,
    strategies_user_id: Option<i64>,
    scope_all: bool,
    cache: Option<&Secret>,
    today_utc: NaiveDate,
) -> Result<OverlapView, Problem> {
    let mut sessions: [Option<NaiveDate>; 3] = [None; 3];
    let mut instruments: HashMap<i64, Instrument> = HashMap::new();
    let mut order: Vec<i64> = Vec::new();
    let mut hits: HashMap<i64, Vec<StrategyHit>> = HashMap::new();
    if let Some(su) = strategies_user_id {
        for (i, found) in [
            swing(conn, su).await?,
            volume_breakout(conn, su).await?,
            three_weeks_tight(conn, su).await?,
        ]
        .into_iter()
        .enumerate()
        {
            sessions[i] = found.0;
            for (inst, hit) in found.1 {
                let id = inst.id;
                instruments.insert(id, inst);
                if !hits.contains_key(&id) {
                    order.push(id);
                }
                hits.entry(id).or_default().push(hit);
            }
        }
    }
    let kept: Vec<i64> = order
        .into_iter()
        .filter(|id| scope_all || hits.get(id).is_some_and(|h| h.iter().any(|x| x.actionable)))
        .collect();
    let kept_set: BTreeSet<i64> = kept.iter().copied().collect();
    let (screen_hits, screens_checked) = screens(conn, user_id, &kept_set).await?;
    let catalysts = match strategies_user_id {
        Some(su) if !kept.is_empty() => best_catalyst_for(conn, su, &kept, today_utc).await?,
        _ => HashMap::new(),
    };
    let headlines: Vec<String> = catalysts
        .values()
        .filter_map(|v| v.headline.clone())
        .filter(|h| !py_strip(h).is_empty())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let answers = laya_answers(cache, &headlines).await;
    let corrections = corrections_for(conn, user_id, &headlines).await?;

    let mut rows: Vec<CandidateRow> = Vec::with_capacity(kept.len());
    for id in &kept {
        let found = hits.remove(id).unwrap_or_default();
        let inst = instruments.get(id).ok_or_else(internal_error)?;
        let catalyst = catalysts.get(id).cloned();
        let headline = catalyst
            .as_ref()
            .and_then(|c| c.headline.clone())
            .unwrap_or_default();
        let catalyst_tag = match &catalyst {
            Some(c)
                if c.headline
                    .as_deref()
                    .is_some_and(|h| !py_strip(h).is_empty()) =>
            {
                let laya = tag_from_laya(answers.get(&headline));
                Some(resolve_tag(
                    &tag_headline(&headline),
                    laya.as_ref(),
                    corrections.get(&headline).copied(),
                ))
            }
            _ => None,
        };
        rows.push(CandidateRow {
            instrument_id: *id,
            symbol: inst.symbol.clone(),
            name: inst.name.clone(),
            close: found.iter().find_map(|h| h.close),
            strategies: found,
            screens: screen_hits.get(id).cloned().unwrap_or_default(),
            catalyst,
            catalyst_tag,
            opinion: None,
            laya: None,
            context: None,
        });
    }
    rows.sort_by(|a, b| {
        (
            std::cmp::Reverse(a.strategy_count()),
            !a.actionable(),
            &a.symbol,
        )
            .cmp(&(
                std::cmp::Reverse(b.strategy_count()),
                !b.actionable(),
                &b.symbol,
            ))
    });
    let (gate_words, breadth) = match strategies_user_id {
        Some(su) => gates(conn, su, &sessions).await?,
        None => (Vec::new(), Vec::new()),
    };
    for row in &mut rows {
        row.context = Some(context(row, &gate_words, &breadth));
    }
    let mut states: Vec<(String, Vec<(&'static str, String)>)> = Vec::new();
    let mut keys: Vec<String> = Vec::with_capacity(rows.len());
    for row in &rows {
        let state = row.state();
        let key = review_key(&state);
        put(&mut states, key.clone(), state);
        keys.push(key);
    }
    let opinions = review_opinions(cache, &states).await;
    let labels = labels_for(conn, user_id, &keys).await?;
    for (row, key) in rows.iter_mut().zip(&keys) {
        let laya = opinions.get(key).cloned();
        let opinion = if let Some(label) = labels.get(key) {
            ReviewOpinion {
                label,
                confidence: 0.0,
                source: SOURCE_LABELLED,
                reason: None,
            }
        } else if let Some(l) = laya
            .as_ref()
            .filter(|l| l.confidence >= review::REVIEW_CONFIDENCE_FLOOR)
        {
            l.clone()
        } else {
            row_rules_opinion(row)
        };
        row.opinion = Some(opinion);
        row.laya = laya;
    }
    Ok(OverlapView {
        sessions,
        rows,
        strategies_read: strategies_user_id.is_some(),
        screens_checked,
    })
}

/// `laya_last_pass(cache)`: the sidecar's last heartbeat, `None` on any doubt.
pub async fn laya_last_pass(
    cache: Option<&Secret>,
) -> Option<chrono::DateTime<chrono::FixedOffset>> {
    let cache = usable(cache)?;
    let raw = crate::cache::cache_get(cache, HEARTBEAT_KEY).await?;
    let payload = OrderedJson::parse(&String::from_utf8(raw).ok()?).ok()?;
    let Some(OrderedJson::Str(at)) = payload.get("at") else {
        return None;
    };
    // `strptime(at, "%Y-%m-%dT%H:%M:%S%z")`: an offset as `+0530`, `+05:30` or `Z`.
    let at = at.trim_end_matches('Z').to_owned() + if at.ends_with('Z') { "+0000" } else { "" };
    chrono::DateTime::parse_from_str(&at, "%Y-%m-%dT%H:%M:%S%z")
        .or_else(|_| chrono::DateTime::parse_from_str(&at, "%Y-%m-%dT%H:%M:%S%:z"))
        .ok()
}

/// `LayaStatus`: `(answered, shown, of)` over the rows with a filing.
#[must_use]
pub fn laya_status(rows: &[CandidateRow]) -> (i64, i64, i64) {
    let with_filing: Vec<&CandidateRow> = rows
        .iter()
        .filter(|r| {
            r.catalyst
                .as_ref()
                .and_then(|c| c.headline.as_deref())
                .is_some_and(|h| !h.is_empty())
        })
        .collect();
    let count = |f: &dyn Fn(&Tag) -> bool| {
        i64::try_from(
            with_filing
                .iter()
                .filter(|r| r.catalyst_tag.as_ref().is_some_and(f))
                .count(),
        )
        .unwrap_or(i64::MAX)
    };
    let answered = count(&|t: &Tag| {
        t.source == "laya"
            || t.disagrees_with
                .as_deref()
                .unwrap_or("")
                .starts_with("laya:")
    });
    let shown = count(&|t: &Tag| t.source == "laya");
    (
        answered,
        shown,
        i64::try_from(with_filing.len()).unwrap_or(i64::MAX),
    )
}

/// `_opinion_confidence(opinion)`: four decimals, as the column stores it.
fn stored_confidence(opinion: Option<&ReviewOpinion>) -> Option<Decimal> {
    opinion.and_then(|o| format!("{:.4}", o.confidence).parse::<Decimal>().ok())
}

/// `label_row(session, user_id=, row=, label=, note=, laya_opinion=)`: one row per
/// `(user, review_key)`, updated in place on a second label (only the columns that changed, and
/// `updated_at` with them, as the ORM flushes).
///
/// # Errors
/// 500 (the database).
pub async fn label_row(
    conn: &mut PgConnection,
    user_id: i64,
    row: &CandidateRow,
    label: &'static str,
    note: Option<String>,
    laya_opinion: Option<&ReviewOpinion>,
) -> Result<ReviewOpinion, Problem> {
    let state = row.state();
    let key = review_key(&state);
    let laya_label = laya_opinion.map(|o| o.label.to_owned());
    let laya_conf = stored_confidence(laya_opinion);
    let stored = sqlx::query(
        "SELECT id, instrument_id, symbol, label, note, laya_label, laya_confidence \
         FROM candidate_review_label WHERE user_id = $1 AND review_key = $2",
    )
    .bind(user_id)
    .bind(&key)
    .fetch_optional(&mut *conn)
    .await
    .map_err(db_err)?;
    match stored {
        None => {
            sqlx::query(
                "INSERT INTO candidate_review_label (user_id, review_key, state, instrument_id, \
                 symbol, label, note, laya_label, laya_confidence) \
                 VALUES ($1, $2, CAST($3 AS JSONB), $4, $5, $6, $7, $8, $9)",
            )
            .bind(user_id)
            .bind(&key)
            .bind(review::state_json(&state))
            .bind(row.instrument_id)
            .bind(&row.symbol)
            .bind(label)
            .bind(&note)
            .bind(&laya_label)
            .bind(laya_conf)
            .execute(&mut *conn)
            .await
            .map_err(db_err)?;
        }
        Some(r) => {
            let id = col::i64(&r, "id")?;
            let mut sets: Vec<String> = Vec::new();
            let changed_int = col::i64(&r, "instrument_id")? != row.instrument_id;
            let changed_symbol = col::text(&r, "symbol")? != row.symbol;
            let changed_label = col::text(&r, "label")? != label;
            let changed_note = col::opt_text(&r, "note")? != note;
            let changed_laya = col::opt_text(&r, "laya_label")? != laya_label;
            let changed_conf = col::opt_dec(&r, "laya_confidence")? != laya_conf;
            for (changed, set) in [
                (changed_int, "instrument_id = $2"),
                (changed_symbol, "symbol = $3"),
                (changed_label, "label = $4"),
                (changed_note, "note = $5"),
                (changed_laya, "laya_label = $6"),
                (changed_conf, "laya_confidence = $7"),
            ] {
                if changed {
                    sets.push(set.to_owned());
                }
            }
            if !sets.is_empty() {
                sqlx::query(&format!(
                    "UPDATE candidate_review_label SET {}, updated_at = now() WHERE id = $1",
                    sets.join(", ")
                ))
                .bind(id)
                .bind(row.instrument_id)
                .bind(&row.symbol)
                .bind(label)
                .bind(&note)
                .bind(&laya_label)
                .bind(laya_conf)
                .execute(&mut *conn)
                .await
                .map_err(db_err)?;
            }
        }
    }
    Ok(ReviewOpinion {
        label,
        confidence: 0.0,
        source: SOURCE_LABELLED,
        reason: None,
    })
}

/// `clear_label(session, user_id=, row=)`: `false` when there was none to remove.
///
/// # Errors
/// 500 (the database).
pub async fn clear_label(
    conn: &mut PgConnection,
    user_id: i64,
    row: &CandidateRow,
) -> Result<bool, Problem> {
    let key = review_key(&row.state());
    let id: Option<i64> = sqlx::query_scalar(
        "SELECT id FROM candidate_review_label WHERE user_id = $1 AND review_key = $2",
    )
    .bind(user_id)
    .bind(&key)
    .fetch_optional(&mut *conn)
    .await
    .map_err(db_err)?;
    let Some(id) = id else {
        return Ok(false);
    };
    sqlx::query("DELETE FROM candidate_review_label WHERE id = $1")
        .bind(id)
        .execute(&mut *conn)
        .await
        .map_err(db_err)?;
    Ok(true)
}
