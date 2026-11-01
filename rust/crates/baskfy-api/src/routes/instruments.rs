//! `/instruments/*` — docs/07 §Instruments (Prompt 10 deliverable 1).
//!
//! Ports `baskfy_api.routers.instruments` (with `baskfy_api.instruments` and
//! `baskfy_api.instrument_appearances` in [`super::meta::reference`]):
//!
//! * `GET /instruments?search=&limit=` — the ⌘K typeahead (pydantic JSON);
//! * `GET /instruments/{symbol}?as_of=` — the factsheet (canonical JSON, snapshot headers);
//! * `GET /instruments/{symbol}/history?field=&from=&to=` — one field's series;
//! * `GET /instruments/{symbol}/corporate-actions`;
//! * `GET /instruments/{symbol}/appearances` — stored screen and strategy results naming it;
//! * `GET /instruments/{symbol}/rank-history?screen=&limit=` — its rank in a screen over time.
//!
//! Matching: Python registers `""` and `/{symbol}` first, then the deeper paths; no two of these
//! overlap, so axum's static-first preference changes nothing here.

use axum::extract::{Path, RawQuery, State};
use axum::response::Response;
use axum::routing::get;
use axum::{Extension, Router};
use baskfy_core::screener::{Payload, ScreenDefinition};
use baskfy_db::OrderedJson;
use chrono::{Duration, NaiveDate};
use serde::Serialize;
use sqlx::Row;

use super::meta::reference::factsheet::{self, Instrument};
use super::meta::reference::{
    self as r, Params, canonical_response, p_date, p_map, p_str, scoped_sole_user_id,
};
use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, ProblemType, not_found};
use crate::state::AppState;
use crate::wire;

/// `DEFAULT_SEARCH_LIMIT`.
pub const DEFAULT_SEARCH_LIMIT: i64 = 8;
/// `MAX_SEARCH_LIMIT`.
pub const MAX_SEARCH_LIMIT: i64 = 50;
/// `MAX_HISTORY_DAYS`.
pub const MAX_HISTORY_DAYS: i64 = 366 * 15;
/// `DEFAULT_HISTORY_FIELD`.
pub const DEFAULT_HISTORY_FIELD: &str = "close";

/// `HISTORY_FIELDS`: the series `/history` serves, and the table holding each.
pub const HISTORY_FIELDS: [(&str, &str); 13] = [
    ("close", "ohlcv_daily"),
    ("close_raw", "ohlcv_daily"),
    ("volume", "ohlcv_daily"),
    ("volume_raw", "ohlcv_daily"),
    ("ret_12m", "factor_daily"),
    ("sharpe_12m", "factor_daily"),
    ("vol_12m", "factor_daily"),
    ("rsi_12m", "factor_daily"),
    ("beta_12m", "factor_daily"),
    ("pe", "factor_daily"),
    ("marketcap_cr", "factor_daily"),
    ("ma_200", "factor_daily"),
    ("median_vol_12m", "factor_daily"),
];

fn invalid(detail: &str, field: &str, message: &str) -> Problem {
    Problem::new(ProblemType::InvalidScreenDefinition, detail).with(
        "errors",
        OrderedJson::Array(vec![OrderedJson::Object(vec![
            ("field".to_owned(), OrderedJson::Str(field.to_owned())),
            ("message".to_owned(), OrderedJson::Str(message.to_owned())),
        ])]),
    )
}

async fn instrument_or_404(state: &AppState, symbol: &str) -> Result<Instrument, Problem> {
    factsheet::load_instrument(state, symbol)
        .await?
        .ok_or_else(|| not_found("instrument", symbol))
}

/// `InstrumentHitOut`.
#[derive(Debug, Serialize)]
struct InstrumentHitOut {
    symbol: String,
    name: String,
    series: Option<String>,
}

/// `InstrumentSearchOut`.
#[derive(Debug, Serialize)]
struct InstrumentSearchOut {
    data: Vec<InstrumentHitOut>,
}

/// `GET /instruments?search=&limit=`: the typeahead. Open to anonymous callers.
///
/// # Errors
/// 400 for a missing or bad parameter, 500 for a database failure.
pub async fn search(
    State(state): State<AppState>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let mut p = Params::new(raw.as_deref());
    let needle = p.required_string("search", Some(1), Some(64));
    let limit = p.int(
        "limit",
        Some(DEFAULT_SEARCH_LIMIT),
        Some(1),
        Some(MAX_SEARCH_LIMIT),
    );
    p.finish()?;
    let hits = factsheet::search_instruments(
        &state,
        needle.as_deref().unwrap_or(""),
        limit.flatten().unwrap_or(DEFAULT_SEARCH_LIMIT),
    )
    .await?;
    Ok(wire::json(&InstrumentSearchOut {
        data: hits
            .into_iter()
            .map(|h| InstrumentHitOut {
                symbol: h.symbol,
                name: h.name,
                series: h.series,
            })
            .collect(),
    }))
}

/// `GET /instruments/{symbol}?as_of=`: docs/01 §5's eleven blocks.
///
/// # Errors
/// 400 for a bad date, 422/503 from the as-of resolution, 404 for an unknown symbol, 500 for a
/// database failure.
pub async fn get_factsheet(
    State(state): State<AppState>,
    Path(symbol): Path<String>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let mut p = Params::new(raw.as_deref());
    let as_of = p.date("as_of");
    p.finish()?;
    let as_of = r::resolve_as_of(&state, as_of.ok().flatten()).await?;
    let data_version = r::current_data_version(&state).await?;
    let Some(payload) = factsheet::build_factsheet(&state, &symbol, as_of, data_version).await?
    else {
        return Err(not_found("instrument", &symbol));
    };
    canonical_response(&payload, Some(as_of), Some(data_version))
}

/// `GET /instruments/{symbol}/history?field=&from=&to=`.
///
/// # Errors
/// 400 for an unknown field, a bad date or a reversed / over-long range; 422/503 from the as-of
/// resolution; 404 for an unknown symbol; 500 for a database failure.
pub async fn get_history(
    State(state): State<AppState>,
    Path(symbol): Path<String>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let mut p = Params::new(raw.as_deref());
    let field = p
        .raw("field")
        .unwrap_or_else(|| DEFAULT_HISTORY_FIELD.to_owned());
    let from = p.date("from");
    let to = p.date("to");
    p.finish()?;
    let Some((_, table)) = HISTORY_FIELDS.iter().find(|(f, _)| *f == field) else {
        let mut names: Vec<&str> = HISTORY_FIELDS.iter().map(|(f, _)| *f).collect();
        names.sort_unstable();
        return Err(invalid(
            &format!(
                "{} is not a series this endpoint serves.",
                baskfy_providers::py::repr_str(&field)
            ),
            "field",
            &format!("choose one of {}", r::universes::py_list_repr(&names)),
        ));
    };
    let end = r::resolve_as_of(&state, to.ok().flatten()).await?;
    let start = from
        .ok()
        .flatten()
        .unwrap_or_else(|| end - Duration::days(365));
    if start > end {
        return Err(invalid(
            &format!(
                "`from` ({}) is after `to` ({}).",
                wire::py_date(start),
                wire::py_date(end)
            ),
            "from",
            "must be on or before `to`",
        ));
    }
    if (end - start).num_days() > MAX_HISTORY_DAYS {
        return Err(invalid(
            &format!("Range exceeds the {MAX_HISTORY_DAYS}-day maximum for one request."),
            "from",
            &format!("at most {MAX_HISTORY_DAYS} days before `to`"),
        ));
    }
    let instrument = instrument_or_404(&state, &symbol).await?;
    let sql = format!(
        "SELECT date, {field} AS value FROM {table} \
         WHERE instrument_id = $1 AND date >= $2 AND date <= $3 ORDER BY date ASC"
    );
    let rows = sqlx::query(&sql)
        .bind(instrument.id)
        .bind(start)
        .bind(end)
        .fetch_all(state.db.pool())
        .await
        .map_err(r::db)?;
    let mut points = Vec::with_capacity(rows.len());
    for row in &rows {
        let cells = factsheet::read_row(row)?;
        let date = baskfy_db::row::date(row, "date").map_err(|e| r::dbe(&e))?;
        let value = match cells.get("value") {
            Some(factsheet::Cell::Dec(d)) => r::p_dec(*d),
            Some(factsheet::Cell::Int(i)) => Payload::Int(*i),
            _ => Payload::Null,
        };
        points.push(p_map(vec![("date", p_date(date)), ("value", value)]));
    }
    let data_version = r::current_data_version(&state).await?;
    let payload = p_map(vec![
        ("symbol", p_str(&instrument.symbol)),
        ("field", p_str(&field)),
        ("from", p_date(start)),
        ("to", p_date(end)),
        ("points", Payload::List(points)),
    ]);
    canonical_response(&payload, Some(end), Some(data_version))
}

/// `GET /instruments/{symbol}/corporate-actions`: newest first.
///
/// # Errors
/// 404 for an unknown symbol, 500 for a database failure.
pub async fn get_corporate_actions(
    State(state): State<AppState>,
    Path(symbol): Path<String>,
) -> Result<Response, Problem> {
    let instrument = instrument_or_404(&state, &symbol).await?;
    let rows = sqlx::query(
        "SELECT action_type, ex_date, ratio_from, ratio_to, amount FROM corporate_action \
         WHERE instrument_id = $1 ORDER BY ex_date DESC",
    )
    .bind(instrument.id)
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;
    let payload = p_map(vec![
        ("symbol", p_str(&instrument.symbol)),
        ("data", factsheet::corporate_actions(&rows)?),
    ]);
    canonical_response(&payload, None, None)
}

// ---------------------------------------------------------------------------------------------
// /appearances — `baskfy_api.instrument_appearances`
// ---------------------------------------------------------------------------------------------

/// `AppearanceOut`.
#[derive(Debug, Serialize)]
struct AppearanceOut {
    kind: &'static str,
    name: String,
    #[serde(rename = "ref")]
    reference: String,
    #[serde(serialize_with = "wire::ser::date")]
    as_of: NaiveDate,
    rank: Option<i64>,
    of: Option<i64>,
    detail: Option<String>,
    definition_changed: bool,
}

/// `AppearancesOut`.
#[derive(Debug, Serialize)]
struct AppearancesOut {
    symbol: String,
    appearances: Vec<AppearanceOut>,
    screens_checked: i64,
    screens_never_run: Vec<String>,
    strategies_checked: Vec<&'static str>,
}

/// Python's `entry.get("instrument_id") == instrument_id` for one stored JSON value.
#[allow(clippy::float_cmp)]
fn json_equals_int(v: Option<&OrderedJson>, id: i64) -> bool {
    match v {
        Some(OrderedJson::Int(i)) => *i == id,
        Some(OrderedJson::UInt(u)) => i64::try_from(*u).is_ok_and(|u| u == id),
        #[allow(clippy::cast_precision_loss)]
        Some(OrderedJson::Float(f)) => *f == id as f64,
        Some(OrderedJson::Bool(b)) => i64::from(*b) == id,
        _ => false,
    }
}

/// `isinstance(rank, int)` (a bool is an int in Python).
fn json_int(v: Option<&OrderedJson>) -> Option<i64> {
    match v {
        Some(OrderedJson::Int(i)) => Some(*i),
        Some(OrderedJson::Bool(b)) => Some(i64::from(*b)),
        _ => None,
    }
}

/// The first stored result row naming the instrument; a non-dict entry is Python's
/// `AttributeError` (a 500).
fn find_hit(results: &OrderedJson, id: i64) -> Result<Option<&OrderedJson>, Problem> {
    let OrderedJson::Array(entries) = results else {
        return Err(r::unexpected("screen_run.results is not a list"));
    };
    for entry in entries {
        if !matches!(entry, OrderedJson::Object(_)) {
            return Err(r::unexpected("screen_run.results entry has no .get"));
        }
        if json_equals_int(entry.get("instrument_id"), id) {
            return Ok(Some(entry));
        }
    }
    Ok(None)
}

/// A stored `jsonb` value as the Python object `json.loads` makes.
fn to_payload(v: &OrderedJson) -> Payload {
    match v {
        OrderedJson::Null => Payload::Null,
        OrderedJson::Bool(b) => Payload::Bool(*b),
        OrderedJson::Int(i) => Payload::Int(*i),
        OrderedJson::UInt(u) => {
            i64::try_from(*u).map_or(Payload::Opaque("int".to_owned()), Payload::Int)
        }
        OrderedJson::BigInt(_) => Payload::Opaque("int".to_owned()),
        OrderedJson::Float(f) => Payload::Float(*f),
        OrderedJson::Str(s) => Payload::Str(s.clone()),
        OrderedJson::Array(items) => Payload::List(items.iter().map(to_payload).collect()),
        OrderedJson::Object(entries) => Payload::Map(
            entries
                .iter()
                .map(|(k, v)| (k.clone(), to_payload(v)))
                .collect(),
        ),
    }
}

/// `_definition_hash(screen)`: `None` when the saved definition no longer validates.
pub(crate) fn definition_hash(definition: &OrderedJson) -> Option<String> {
    ScreenDefinition::model_validate(&to_payload(definition))
        .ok()
        .map(|d| d.definition_hash())
}

async fn screen_appearances(
    state: &AppState,
    user_id: i64,
    instrument_id: i64,
) -> Result<(Vec<AppearanceOut>, i64, Vec<String>), Problem> {
    let screens = sqlx::query(
        "SELECT id, public_id, name, definition, is_example FROM screen \
         WHERE user_id = $1 OR is_example IS true ORDER BY is_example, name",
    )
    .bind(user_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;
    if screens.is_empty() {
        return Ok((Vec::new(), 0, Vec::new()));
    }
    let ids: Vec<i64> = screens
        .iter()
        .map(|s| s.try_get::<i64, _>("id"))
        .collect::<Result<_, _>>()
        .map_err(r::db)?;
    let runs = sqlx::query(
        "SELECT DISTINCT ON (screen_id) screen_id, as_of, definition_hash, result_count, results \
         FROM screen_run WHERE screen_id = ANY($1) \
         ORDER BY screen_id, as_of DESC, created_at DESC",
    )
    .bind(&ids)
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;
    let mut found = Vec::new();
    let mut never = Vec::new();
    for screen in &screens {
        let id: i64 = screen.try_get("id").map_err(r::db)?;
        let name: String = screen.try_get("name").map_err(r::db)?;
        let Some(run) = runs
            .iter()
            .find(|run| run.try_get::<i64, _>("screen_id").is_ok_and(|s| s == id))
        else {
            never.push(name);
            continue;
        };
        let results = baskfy_db::row::jsonb(run, "results").map_err(|e| r::dbe(&e))?;
        let Some(hit) = find_hit(&results, instrument_id)? else {
            continue;
        };
        let is_example: bool = screen.try_get("is_example").map_err(r::db)?;
        let definition = baskfy_db::row::jsonb(screen, "definition").map_err(|e| r::dbe(&e))?;
        let stored_hash: String = run.try_get("definition_hash").map_err(r::db)?;
        found.push(AppearanceOut {
            kind: if is_example { "template" } else { "screen" },
            name,
            reference: screen.try_get("public_id").map_err(r::db)?,
            as_of: baskfy_db::row::date(run, "as_of").map_err(|e| r::dbe(&e))?,
            rank: json_int(hit.get("rank")),
            of: Some(i64::from(
                run.try_get::<i32, _>("result_count").map_err(r::db)?,
            )),
            detail: None,
            definition_changed: definition_hash(&definition).as_deref() != Some(&stored_hash),
        });
    }
    Ok((
        found,
        i64::try_from(screens.len()).unwrap_or(i64::MAX),
        never,
    ))
}

async fn swing_appearances(
    state: &AppState,
    user_id: i64,
    instrument_id: i64,
) -> Result<Vec<AppearanceOut>, Problem> {
    let market: Option<NaiveDate> =
        sqlx::query_scalar("SELECT max(date) FROM sw_market_daily WHERE user_id = $1")
            .bind(user_id)
            .fetch_one(state.db.pool())
            .await
            .map_err(r::db)?;
    let day = match market {
        Some(d) => Some(d),
        None => sqlx::query_scalar("SELECT max(date) FROM sw_setup_daily WHERE user_id = $1")
            .bind(user_id)
            .fetch_one(state.db.pool())
            .await
            .map_err(r::db)?,
    };
    let Some(day) = day else {
        return Ok(Vec::new());
    };
    let rows = sqlx::query(
        "SELECT setup, status, score FROM sw_setup_daily \
         WHERE user_id = $1 AND date = $2 AND instrument_id = $3",
    )
    .bind(user_id)
    .bind(day)
    .bind(instrument_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;
    rows.iter()
        .map(|row| {
            let setup: String = row.try_get("setup").map_err(r::db)?;
            let status: String = row.try_get("status").map_err(r::db)?;
            let score = baskfy_db::row::decimal(row, "score").map_err(|e| r::dbe(&e))?;
            Ok(AppearanceOut {
                kind: "swing",
                name: "Swing".to_owned(),
                reference: "/swing".to_owned(),
                as_of: day,
                rank: None,
                of: None,
                detail: Some(format!(
                    "{setup} · {status} · score {}",
                    wire::py_decimal(score)
                )),
                definition_changed: false,
            })
        })
        .collect()
}

async fn volume_breakout_appearances(
    state: &AppState,
    user_id: i64,
    instrument_id: i64,
) -> Result<Vec<AppearanceOut>, Problem> {
    let day: Option<NaiveDate> = sqlx::query_scalar(
        "SELECT date FROM vb_breadth_daily WHERE user_id = $1 ORDER BY date DESC LIMIT 1",
    )
    .bind(user_id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(r::db)?;
    let Some(day) = day else {
        return Ok(Vec::new());
    };
    let rows: Vec<String> = sqlx::query_scalar(
        "SELECT state FROM vb_signal_daily WHERE user_id = $1 AND date = $2 AND instrument_id = $3",
    )
    .bind(user_id)
    .bind(day)
    .bind(instrument_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;
    if rows.len() > 1 {
        return Err(r::unexpected("MultipleResultsFound: vb_signal_daily"));
    }
    let Some(signal) = rows.into_iter().next() else {
        return Ok(Vec::new());
    };
    let detail = match signal.as_str() {
        "SIGNAL" => "signal".to_owned(),
        "SCAN_ONLY" => "scanned, did not pass the filters".to_owned(),
        other => other.to_lowercase().replace('_', " "),
    };
    Ok(vec![AppearanceOut {
        kind: "volume_breakout",
        name: "Volume breakout".to_owned(),
        reference: "/vbt".to_owned(),
        as_of: day,
        rank: None,
        of: None,
        detail: Some(detail),
        definition_changed: false,
    }])
}

async fn three_weeks_tight_appearances(
    state: &AppState,
    user_id: i64,
    instrument_id: i64,
) -> Result<Vec<AppearanceOut>, Problem> {
    let day: Option<NaiveDate> = sqlx::query_scalar(
        "SELECT date FROM tw_breadth_daily WHERE user_id = $1 ORDER BY date DESC LIMIT 1",
    )
    .bind(user_id)
    .fetch_optional(state.db.pool())
    .await
    .map_err(r::db)?;
    let Some(day) = day else {
        return Ok(Vec::new());
    };
    let rows = sqlx::query(
        "SELECT s.sessions_in_state, g.state FROM tw_state_daily s \
         LEFT OUTER JOIN tw_signal_daily g ON g.user_id = s.user_id AND g.date = s.date \
           AND g.instrument_id = s.instrument_id \
         WHERE s.user_id = $1 AND s.date = $2 AND s.instrument_id = $3",
    )
    .bind(user_id)
    .bind(day)
    .bind(instrument_id)
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;
    if rows.len() > 1 {
        return Err(r::unexpected("MultipleResultsFound: tw_state_daily"));
    }
    let Some(row) = rows.first() else {
        return Ok(Vec::new());
    };
    let sessions: i16 = row.try_get("sessions_in_state").map_err(r::db)?;
    let signal: Option<String> = row.try_get("state").map_err(r::db)?;
    let mut detail = format!(
        "tight {sessions} session{}",
        if sessions == 1 { "" } else { "s" }
    );
    if let Some(signal) = signal.filter(|s| !s.is_empty()) {
        detail.push_str(&format!(" · {}", signal.to_lowercase().replace('_', " ")));
    }
    Ok(vec![AppearanceOut {
        kind: "three_weeks_tight",
        name: "Three weeks tight".to_owned(),
        reference: "/twt".to_owned(),
        as_of: day,
        rank: None,
        of: None,
        detail: Some(detail),
        definition_changed: false,
    }])
}

/// `GET /instruments/{symbol}/appearances`: every stored screen run and strategy scan naming
/// the stock. Screens are the caller's own plus the templates; strategy scans are read only for
/// the sole tenant.
///
/// # Errors
/// 401 for an anonymous caller, 404 for an unknown symbol, 500 for a database failure.
pub async fn get_appearances(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(symbol): Path<String>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let user_id = principal.require_user()?;
    let instrument = instrument_or_404(&state, &symbol).await?;
    let sole = scoped_sole_user_id(&state, Some(user_id), Some("instrument appearances"))
        .await
        .ok();
    let (mut found, checked, never) = screen_appearances(&state, user_id, instrument.id).await?;
    let mut strategies = Vec::new();
    if let Some(sole) = sole {
        strategies = vec!["Swing", "Volume breakout", "Three weeks tight"];
        found.extend(swing_appearances(&state, sole, instrument.id).await?);
        found.extend(volume_breakout_appearances(&state, sole, instrument.id).await?);
        found.extend(three_weeks_tight_appearances(&state, sole, instrument.id).await?);
    }
    Ok(wire::json(&AppearancesOut {
        symbol: instrument.symbol,
        appearances: found,
        screens_checked: checked,
        screens_never_run: never,
        strategies_checked: strategies,
    }))
}

/// `GET /instruments/{symbol}/rank-history?screen=&limit=`: the stored audit trail of what the
/// screen returned, oldest first.
///
/// # Errors
/// 400 for a missing or bad parameter, 404 for an unknown symbol or a screen that is missing or
/// not the caller's, 500 for a database failure.
pub async fn get_rank_history(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(symbol): Path<String>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let mut p = Params::new(raw.as_deref());
    let screen = p.required_string("screen", None, None);
    let limit = p.int("limit", Some(120), Some(1), Some(500));
    p.finish()?;
    let screen = screen.unwrap_or_default();
    let limit = limit.flatten().unwrap_or(120);
    let instrument = instrument_or_404(&state, &symbol).await?;
    let found = sqlx::query("SELECT id, public_id, name, user_id FROM screen WHERE public_id = $1")
        .bind(&screen)
        .fetch_all(state.db.pool())
        .await
        .map_err(r::db)?;
    if found.len() > 1 {
        return Err(r::unexpected("MultipleResultsFound: screen"));
    }
    let Some(found) = found.first() else {
        return Err(not_found("screen", &screen));
    };
    // The screen's own visibility rule (`routers/screens.py`): a private screen's history is its
    // owner's only, and not-yours is the same 404 as no-such-screen (DECISIONS-RS R5.28, fixed in
    // Python first on 30 Oct 2026; until then the route had no principal at all).
    let owner: Option<i64> = found.try_get("user_id").map_err(r::db)?;
    if !super::screens::visible_to(owner, &principal) {
        return Err(not_found("screen", &screen));
    }
    let screen_id: i64 = found.try_get("id").map_err(r::db)?;
    let runs = sqlx::query(
        "SELECT as_of, result_count, results FROM screen_run WHERE screen_id = $1 \
         ORDER BY as_of DESC LIMIT $2",
    )
    .bind(screen_id)
    .bind(limit)
    .fetch_all(state.db.pool())
    .await
    .map_err(r::db)?;
    let mut points: Vec<(NaiveDate, i64, i64)> = Vec::new();
    for run in &runs {
        let results = baskfy_db::row::jsonb(run, "results").map_err(|e| r::dbe(&e))?;
        if let Some(entry) = find_hit(&results, instrument.id)?
            && let Some(rank) = json_int(entry.get("rank"))
        {
            points.push((
                baskfy_db::row::date(run, "as_of").map_err(|e| r::dbe(&e))?,
                rank,
                i64::from(run.try_get::<i32, _>("result_count").map_err(r::db)?),
            ));
        }
    }
    points.sort_by_key(|p| p.0);
    let payload = p_map(vec![
        ("symbol", p_str(&instrument.symbol)),
        (
            "screen_public_id",
            Payload::Str(found.try_get("public_id").map_err(r::db)?),
        ),
        (
            "screen_name",
            Payload::Str(found.try_get("name").map_err(r::db)?),
        ),
        (
            "data",
            Payload::List(
                points
                    .into_iter()
                    .map(|(as_of, rank, count)| {
                        p_map(vec![
                            ("as_of", p_date(as_of)),
                            ("rank", Payload::Int(rank)),
                            ("result_count", Payload::Int(count)),
                        ])
                    })
                    .collect(),
            ),
        ),
    ]);
    canonical_response(&payload, None, None)
}

/// The routes of `baskfy_api.routers.instruments`, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/instruments", get(search))
        .route("/instruments/{symbol}", get(get_factsheet))
        .route("/instruments/{symbol}/history", get(get_history))
        .route(
            "/instruments/{symbol}/corporate-actions",
            get(get_corporate_actions),
        )
        .route("/instruments/{symbol}/appearances", get(get_appearances))
        .route("/instruments/{symbol}/rank-history", get(get_rank_history))
}
