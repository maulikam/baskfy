//! GTT-TRAIL (Maulik, 12 Nov 2026): raise resting GTT stops as profit builds — breakeven, then a
//! trail under the highest close — per-strategy policy, a daily job after the close and an
//! on-demand preview. A stop only moves up; nothing is sent unless `BASKFY_GTT_TRAIL_ENABLED`
//! is true and the strategy's policy is enabled. Every GTT write is [`apply::raise_gtt_trigger`].

pub mod apply;
pub mod policy;
pub mod store;

use std::collections::HashMap;

use axum::Router;
use axum::body::{Body, Bytes};
use axum::extract::State;
use axum::http::{Response, StatusCode, Uri, header};
use axum::routing::get;
use chrono::{DateTime, Datelike, Duration, FixedOffset, NaiveDate, Timelike, Weekday};
use serde::Deserialize;
use serde_json::json;

use self::apply::{Outcome, TrailReq, raise_gtt_trigger};
use self::policy::{Inputs, Policy, WIRED, closes_since, propose};
use self::store::{Entry, Position};
use crate::app::AppState;
use crate::lanes::exec_core::lifecycle::gtt_index;
use crate::lanes::weekly::score::ScoringConfig;
use crate::wire::PyValue;

/// Routes: settings (GET, PUT/POST), preview (GET), journal (GET), run (POST, confirm).
pub fn routes() -> Router<AppState> {
    Router::new()
        .route(
            "/gtt-trail/settings",
            get(settings_get).put(settings_put).post(settings_put),
        )
        .route("/gtt-trail/preview", get(preview_get))
        .route("/gtt-trail/journal", get(journal_get))
        .route("/gtt-trail/run", axum::routing::post(run_post))
}

fn json_response(status: StatusCode, body: &serde_json::Value) -> Response<Body> {
    Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from(body.to_string()))
        .unwrap_or_else(|_| Response::new(Body::from("{}")))
}

fn fail(status: StatusCode, detail: impl Into<String>) -> Response<Body> {
    json_response(status, &json!({ "detail": detail.into() }))
}

#[allow(clippy::print_stderr)]
fn log(line: &str) {
    eprintln!("gtt-trail: {line}");
}

/// The last completed session's date: today only after 15:30 IST (no provisional bar).
#[must_use]
pub fn last_completed(now: DateTime<FixedOffset>) -> NaiveDate {
    let day = now.date_naive();
    if now.hour() * 60 + now.minute() >= 15 * 60 + 30 {
        day
    } else {
        day - Duration::days(1)
    }
}

/// One preview line.
#[derive(Debug, Clone, PartialEq)]
pub struct Line {
    /// The sleeve position.
    pub pos: Position,
    /// The strategy's policy is on.
    pub enabled: bool,
    /// The last bar read.
    pub as_of: Option<NaiveDate>,
    /// Highest exchange close since entry.
    pub highest_close: Option<f64>,
    /// Last exchange close.
    pub last_close: Option<f64>,
    /// The resting trigger.
    pub current_stop: Option<f64>,
    /// The proposed trigger.
    pub proposed_stop: Option<f64>,
    /// Proposed stop vs entry, %.
    pub locked_pct: Option<f64>,
    /// The proposal raises the stop.
    pub would_change: bool,
    /// Why.
    pub reason: String,
    /// Why the job may not act on it even when it would change (no entry, no GTT, paper).
    pub blocker: String,
}

/// Pure: the proposal for each position from its bars and its strategy's policy.
#[must_use]
pub fn lines(
    policies: &[Policy],
    positions: Vec<Position>,
    bars: &HashMap<i64, Vec<policy::Bar>>,
    as_of: NaiveDate,
) -> Vec<Line> {
    positions
        .into_iter()
        .map(|pos| {
            let pol = policies
                .iter()
                .find(|p| p.strategy == pos.strategy)
                .cloned()
                .unwrap_or_else(|| Policy::default_for(&pos.strategy));
            let current = pos.current_stop();
            let mut line = Line {
                enabled: pol.enabled,
                as_of: None,
                highest_close: None,
                last_close: None,
                current_stop: current,
                proposed_stop: None,
                locked_pct: None,
                would_change: false,
                reason: String::new(),
                blocker: String::new(),
                pos,
            };
            let (Some(entry), Some(entry_date)) = (line.pos.entry, line.pos.entry_date) else {
                line.reason = "no known entry".to_owned();
                line.blocker = line.reason.clone();
                return line;
            };
            let Some(stop) = current else {
                line.reason = "no stop on record".to_owned();
                line.blocker = line.reason.clone();
                return line;
            };
            let series = bars
                .get(&line.pos.instrument_id)
                .map_or(&[][..], Vec::as_slice);
            let (hi, last, on) = match closes_since(series, entry_date, as_of) {
                Ok(v) => v,
                Err(e) => {
                    line.reason = e;
                    line.blocker = line.reason.clone();
                    return line;
                }
            };
            line.as_of = Some(on);
            line.highest_close = Some(hi);
            line.last_close = Some(last);
            let p = propose(
                &pol,
                &Inputs {
                    entry,
                    current_stop: stop,
                    highest_close: hi,
                    last_close: last,
                },
            );
            line.proposed_stop = Some(p.new_stop);
            line.locked_pct = Some(p.locked_pct);
            line.would_change = p.changed;
            line.reason = p.reason;
            if line.pos.broker_gtt_id().is_none() {
                line.blocker = "no broker GTT id (paper or unarmed)".to_owned();
            } else if line.pos.gtt_trigger.is_none() {
                line.blocker = "GTT trigger not on record".to_owned();
            }
            line
        })
        .collect()
}

fn line_json(l: &Line) -> serde_json::Value {
    json!({
        "strategy": l.pos.strategy,
        "position_id": l.pos.id,
        "symbol": l.pos.symbol,
        "qty": l.pos.qty,
        "entry": l.pos.entry,
        "entry_date": l.pos.entry_date.map(|d| d.to_string()),
        "as_of": l.as_of.map(|d| d.to_string()),
        "last_close": l.last_close,
        "highest_close": l.highest_close,
        "current_stop": l.current_stop,
        "proposed_stop": l.proposed_stop,
        "locked_pct": l.locked_pct,
        "would_change": l.would_change,
        "reason": l.reason,
        "blocker": l.blocker,
        "gtt_id": l.pos.gtt_id,
        "policy_enabled": l.enabled,
    })
}

async fn build(state: &AppState) -> Result<(Vec<Policy>, Vec<Line>, NaiveDate), String> {
    let s = state.settings_snapshot();
    let mut conn = state.db.connect().await.map_err(|e| e.to_string())?;
    let policies = store::policies(&mut conn)
        .await
        .map_err(|e| format!("trail policy table unreadable (migration 0014?): {e}"))?;
    let positions = store::open_positions(&mut conn, s.sole_user_id)
        .await
        .map_err(|e| e.to_string())?;
    let as_of = last_completed(state.clock.now_ist());
    let from = positions
        .iter()
        .filter_map(|p| p.entry_date)
        .min()
        .unwrap_or(as_of);
    let mut ids: Vec<i64> = positions.iter().map(|p| p.instrument_id).collect();
    ids.sort_unstable();
    ids.dedup();
    let bars = store::bars(&mut conn, &ids, from, as_of)
        .await
        .map_err(|e| e.to_string())?;
    Ok((
        policies.clone(),
        lines(&policies, positions, &bars, as_of),
        as_of,
    ))
}

fn policies_json(state: &AppState, policies: &[Policy]) -> serde_json::Value {
    let s = state.settings_snapshot();
    json!({
        "master_enabled": s.gtt_trail_enabled,
        "dry_run": s.dry_run,
        "wired": WIRED,
        "policies": policies,
    })
}

async fn settings_get(State(state): State<AppState>) -> Response<Body> {
    let mut conn = match state.db.connect().await {
        Ok(c) => c,
        Err(e) => return fail(StatusCode::SERVICE_UNAVAILABLE, e.to_string()),
    };
    match store::policies(&mut conn).await {
        Ok(p) => json_response(StatusCode::OK, &policies_json(&state, &p)),
        Err(e) => fail(
            StatusCode::SERVICE_UNAVAILABLE,
            format!("trail policy table unreadable (migration 0014?): {e}"),
        ),
    }
}

#[derive(Deserialize)]
struct SettingsIn {
    policies: Vec<Policy>,
    #[serde(default)]
    by: Option<String>,
}

async fn settings_put(State(state): State<AppState>, body: Bytes) -> Response<Body> {
    let parsed: SettingsIn = match serde_json::from_slice(&body) {
        Ok(v) => v,
        Err(e) => {
            return fail(
                StatusCode::BAD_REQUEST,
                format!("policies are required: {e}"),
            );
        }
    };
    for p in &parsed.policies {
        if let Err(why) = p.validate() {
            return fail(StatusCode::UNPROCESSABLE_ENTITY, why);
        }
    }
    let by = parsed
        .by
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .unwrap_or("desk")
        .chars()
        .take(64)
        .collect::<String>();
    let mut conn = match state.db.connect().await {
        Ok(c) => c,
        Err(e) => return fail(StatusCode::SERVICE_UNAVAILABLE, e.to_string()),
    };
    let now = state.clock.now_ist();
    for p in &parsed.policies {
        if let Err(e) = store::save_policy(&mut conn, p, now, &by).await {
            return fail(StatusCode::SERVICE_UNAVAILABLE, e.to_string());
        }
    }
    match store::policies(&mut conn).await {
        Ok(p) => json_response(StatusCode::OK, &policies_json(&state, &p)),
        Err(e) => fail(StatusCode::SERVICE_UNAVAILABLE, e.to_string()),
    }
}

async fn preview_get(State(state): State<AppState>) -> Response<Body> {
    match build(&state).await {
        Ok((policies, lines, as_of)) => {
            let s = state.settings_snapshot();
            json_response(
                StatusCode::OK,
                &json!({
                    "as_of": as_of.to_string(),
                    "master_enabled": s.gtt_trail_enabled,
                    "dry_run": s.dry_run,
                    "policies": policies,
                    "rows": lines.iter().map(line_json).collect::<Vec<_>>(),
                }),
            )
        }
        Err(e) => fail(StatusCode::SERVICE_UNAVAILABLE, e),
    }
}

async fn journal_get(State(state): State<AppState>, uri: Uri) -> Response<Body> {
    let limit = uri
        .query()
        .unwrap_or("")
        .split('&')
        .find_map(|kv| kv.strip_prefix("limit="))
        .and_then(|v| v.parse::<i64>().ok())
        .unwrap_or(100);
    let mut conn = match state.db.connect().await {
        Ok(c) => c,
        Err(e) => return fail(StatusCode::SERVICE_UNAVAILABLE, e.to_string()),
    };
    match store::journal_rows(&mut conn, limit).await {
        Ok(rows) => json_response(StatusCode::OK, &json!({ "rows": rows })),
        Err(e) => fail(StatusCode::SERVICE_UNAVAILABLE, e.to_string()),
    }
}

#[derive(Deserialize)]
struct RunIn {
    confirm: bool,
}

async fn run_post(State(state): State<AppState>, body: Bytes) -> Response<Body> {
    let ok = serde_json::from_slice::<RunIn>(&body).is_ok_and(|r| r.confirm);
    if !ok {
        return fail(
            StatusCode::BAD_REQUEST,
            "confirm true is required; nothing was sent",
        );
    }
    // During the session the inputs are the previous close: a stop raised against a stale close can
    // sit above the live price and fire at once (review, 12 Nov 2026). The daily run is at 20:30.
    let now = state.clock.now_ist();
    let minutes = now.hour() * 60 + now.minute();
    if !matches!(now.weekday(), Weekday::Sat | Weekday::Sun)
        && (9 * 60 + 15..=15 * 60 + 30).contains(&minutes)
    {
        return fail(
            StatusCode::CONFLICT,
            "manual runs are blocked during market hours (09:15-15:30 IST); the daily run is at 20:30",
        );
    }
    match run(&state, "manual").await {
        Ok(results) => json_response(StatusCode::OK, &json!({ "results": results })),
        Err(e) => fail(StatusCode::SERVICE_UNAVAILABLE, e),
    }
}

fn entry_of(l: &Line, source: &str, new_stop: Option<f64>, outcome: &str, detail: &str) -> Entry {
    Entry {
        source: source.to_owned(),
        strategy: l.pos.strategy.clone(),
        position_id: l.pos.id,
        symbol: l.pos.symbol.clone(),
        qty: l.pos.qty,
        entry: l.pos.entry,
        as_of: l.as_of,
        highest_close: l.highest_close,
        last_close: l.last_close,
        old_stop: l.current_stop,
        new_stop,
        reason: l.reason.clone(),
        outcome: outcome.to_owned(),
        detail: detail.to_owned(),
    }
}

/// Apply every enabled strategy's proposals, journalling each. Live (flag on, not DRY_RUN) reads
/// the broker's GTT list first and acts only on an active GTT whose leg it keeps unchanged.
///
/// # Errors
/// The database, before anything is sent.
#[allow(clippy::too_many_lines)]
pub async fn run(state: &AppState, source: &str) -> Result<Vec<serde_json::Value>, String> {
    let (_, lines, _) = build(state).await?;
    let settings = state.settings_snapshot();
    let excluded = ScoringConfig::default().excluded_symbols;
    let live = settings.gtt_trail_enabled && !settings.dry_run;
    let todo: Vec<&Line> = lines
        .iter()
        .filter(|l| l.enabled && l.would_change)
        .collect();
    let mut conn = state.db.connect().await.map_err(|e| e.to_string())?;
    let now = state.clock.now_ist();
    let broker = if live && !todo.is_empty() {
        match state.kite.get_gtts().await {
            Ok(PyValue::List(rows)) => Some(gtt_index(&rows)),
            Ok(_) => Some(gtt_index(&[])),
            Err(e) => {
                let why = format!("broker GTT list unavailable: {e}");
                for l in &todo {
                    let en = entry_of(l, source, l.proposed_stop, "ERROR", &why);
                    store::journal(&mut conn, &en, now)
                        .await
                        .map_err(|e| e.to_string())?;
                }
                // Count the day as run, so a failed list is not retried every five minutes.
                if source == "daily" {
                    store::journal(&mut conn, &run_marker(now, 0, &why), now)
                        .await
                        .map_err(|e| e.to_string())?;
                }
                return Ok(vec![json!({ "outcome": "ERROR", "detail": why })]);
            }
        }
    } else {
        None
    };
    let mut results = Vec::new();
    for l in todo {
        let new_stop = l.proposed_stop.unwrap_or(0.0);
        let (outcome, detail) = if !l.blocker.is_empty() {
            ("SKIPPED".to_owned(), l.blocker.clone())
        } else {
            let gtt_id = l.pos.broker_gtt_id().unwrap_or(0);
            let mut qty = l.pos.qty;
            let mut old = l.current_stop.unwrap_or(0.0);
            let mut skip = String::new();
            if let Some(index) = &broker {
                match index.get(&gtt_id.to_string()) {
                    None => skip = format!("GTT {gtt_id} is not at the broker"),
                    Some(row) if row.status != "active" => {
                        skip = format!("GTT {gtt_id} is {}", row.status);
                    }
                    Some(row) if !row.symbol.eq_ignore_ascii_case(&l.pos.symbol) => {
                        skip = format!("GTT {gtt_id} is for {}", row.symbol);
                    }
                    Some(row) => {
                        qty = row.quantity.unwrap_or(0);
                        if let Some(t) = row.trigger.and_then(|t| t.to_string().parse::<f64>().ok())
                        {
                            old = t;
                        }
                    }
                }
            }
            if skip.is_empty() {
                let req = TrailReq {
                    strategy: l.pos.strategy.clone(),
                    symbol: l.pos.symbol.clone(),
                    series: l.pos.series.clone(),
                    gtt_id,
                    qty,
                    old_trigger: old,
                    new_trigger: new_stop,
                    last_price: l.last_close.unwrap_or(0.0),
                    client_id: format!("trail:{}:{}:{new_stop}", l.pos.strategy, l.pos.id),
                    limit_fraction: limit_fraction(&l.pos.strategy, &settings),
                };
                let out =
                    raise_gtt_trigger(state.gateway.as_ref(), &settings, &excluded, &req).await;
                if out == Outcome::Modified
                    && let Err(e) = store::raise_stop(
                        &mut conn,
                        &l.pos.strategy,
                        l.pos.id,
                        settings.sole_user_id,
                        new_stop,
                        now,
                    )
                    .await
                {
                    log(&format!(
                        "{}: modified at broker, book not updated: {e}",
                        l.pos.symbol
                    ));
                }
                (out.label().to_owned(), out.detail())
            } else {
                ("SKIPPED".to_owned(), skip)
            }
        };
        let en = entry_of(l, source, Some(new_stop), &outcome, &detail);
        store::journal(&mut conn, &en, now)
            .await
            .map_err(|e| e.to_string())?;
        results.push(json!({
            "symbol": l.pos.symbol,
            "strategy": l.pos.strategy,
            "old_stop": l.current_stop,
            "new_stop": new_stop,
            "outcome": outcome,
            "detail": detail,
        }));
    }
    if source == "daily" {
        let detail = if live {
            "live"
        } else if settings.gtt_trail_enabled {
            "dry run"
        } else {
            "flag off"
        };
        store::journal(&mut conn, &run_marker(now, results.len(), detail), now)
            .await
            .map_err(|e| e.to_string())?;
    }
    Ok(results)
}

/// The once-a-day marker `run_if_due` looks for.
fn run_marker(now: DateTime<FixedOffset>, proposals: usize, detail: &str) -> Entry {
    Entry {
        source: "daily".to_owned(),
        strategy: "*".to_owned(),
        position_id: 0,
        symbol: "*".to_owned(),
        qty: 0,
        entry: None,
        as_of: Some(last_completed(now)),
        highest_close: None,
        last_close: None,
        old_stop: None,
        new_stop: None,
        reason: format!("{proposals} proposals"),
        outcome: "RUN".to_owned(),
        detail: detail.to_owned(),
    }
}

/// The sleeve's GTT sell-limit fraction, as its own arm/raise sends it.
fn limit_fraction(strategy: &str, settings: &crate::settings::Settings) -> Option<f64> {
    match strategy {
        "swing" => Some(settings.swing_gtt_limit_fraction),
        "twt" => Some(crate::lanes::twt::execute::twt_limit_fraction()),
        "vbt" => Some(
            baskfy_core::vbt::config::VbtConfig::default()
                .exits
                .gtt_limit_fraction,
        ),
        _ => None,
    }
}

/// Due on a weekday from 20:30 IST (the day's bars published), once a day.
async fn run_if_due(state: &AppState) {
    let now = state.clock.now_ist();
    if matches!(now.weekday(), Weekday::Sat | Weekday::Sun)
        || now.hour() < 20
        || (now.hour() == 20 && now.minute() < 30)
    {
        return;
    }
    let Ok(mut conn) = state.db.connect().await else {
        return;
    };
    let Ok(policies) = store::policies(&mut conn).await else {
        return;
    };
    if !policies.iter().any(|p| p.enabled) {
        return;
    }
    let Some(midnight) = now
        .date_naive()
        .and_hms_opt(0, 0, 0)
        .and_then(|n| n.and_local_timezone(*now.offset()).single())
    else {
        return;
    };
    if store::ran_since(&mut conn, midnight).await.unwrap_or(true) {
        return;
    }
    drop(conn);
    match run(state, "daily").await {
        Ok(r) => log(&format!("daily run: {} proposals", r.len())),
        Err(e) => log(&format!("daily run failed: {e}")),
    }
}

/// The daily job: checked every five minutes, runs once a weekday evening.
pub fn spawn_daily(state: AppState) {
    tokio::spawn(async move {
        loop {
            run_if_due(&state).await;
            tokio::time::sleep(std::time::Duration::from_secs(300)).await;
        }
    });
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

    use super::*;
    use chrono::TimeZone;

    fn ist(h: u32, m: u32) -> DateTime<FixedOffset> {
        let tz = FixedOffset::east_opt(19_800).unwrap_or_else(|| unreachable!());
        tz.with_ymd_and_hms(2026, 11, 12, h, m, 0)
            .single()
            .unwrap_or_else(|| unreachable!())
    }

    #[test]
    fn last_completed_session_never_today_while_open() {
        let d = |n| NaiveDate::from_ymd_opt(2026, 11, n).unwrap_or_else(|| unreachable!());
        assert_eq!(last_completed(ist(10, 0)), d(11));
        assert_eq!(last_completed(ist(15, 29)), d(11));
        assert_eq!(last_completed(ist(15, 30)), d(12));
    }

    fn pos(gtt: Option<&str>) -> Position {
        Position {
            strategy: "twt".to_owned(),
            id: 9,
            instrument_id: 2495,
            symbol: "SUNTV".to_owned(),
            series: Some("EQ".to_owned()),
            qty: 204,
            entry: Some(610.7669),
            entry_date: NaiveDate::from_ymd_opt(2026, 11, 9),
            stop: Some(488.6),
            gtt_id: gtt.map(str::to_owned),
            gtt_trigger: Some(488.6),
            simulated: false,
        }
    }

    #[test]
    fn suntv_breakeven_from_published_bars() {
        let d = |n| NaiveDate::from_ymd_opt(2026, 11, n).unwrap_or_else(|| unreachable!());
        let mut bars = HashMap::new();
        bars.insert(
            2495,
            vec![
                (d(9), 609.9, 609.9),
                (d(11), 653.9, 653.9),
                (d(12), 700.0, 700.0),
            ],
        );
        let mut p = Policy::default_for("twt");
        p.enabled = true;
        let out = lines(
            &[p],
            vec![pos(Some("344073747")), pos(Some("SIM-x"))],
            &bars,
            d(11),
        );
        assert!(out[0].would_change, "{:?}", out[0]);
        assert_eq!(out[0].as_of, Some(d(11)), "the 12th is not read");
        // entry 610.7669 × 1.005 = 613.82, rounded UP to the tick: never below entry plus the buffer
        assert!((out[0].proposed_stop.unwrap_or(0.0) - 613.85).abs() < 1e-9);
        assert!(out[0].blocker.is_empty());
        assert!(!out[1].blocker.is_empty(), "a paper GTT is never acted on");
    }
}
