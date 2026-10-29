//! ``/options/*`` — the NIFTY options run's read surface (OP5). Ports
//! `baskfy_api.routers.options` (`APIRouter(prefix="/options", tags=["options"])`) and the reads
//! it calls (`baskfy_api.options_read`, the read half of `baskfy_api.options_settings`), in
//! `routes/derivatives/options_read.rs`.
//!
//! Ported (the derivatives lane, RS5): every GET — `/today`, `/scan/{sleeve}`, `/chain`,
//! `/positions`, `/sessions`, `/journal`, `/backtest`, `/calendar`, `/config`.
//!
//! **Not registered:** `POST /event-day`, `DELETE /event-day` and `PATCH /config` — the two
//! money-free writes `05` §2 allows. The pool is read-only (DECISIONS-RS R5.2), so Python keeps
//! serving them until `op_event_day`, `op_book_config`, `op_sleeve_config` and
//! `op_config_audit` are writable from Rust.
//!
//! READ-ONLY WHERE MONEY IS CONCERNED: no execute, confirm, plan or close route, ever (law 2;
//! the desk's `/nifty-options` holds the only Confirm). The execution flags are read only to
//! report them. The clock: `live` is true only while the NSE session is open **and** the rows
//! are today's; nothing here reaches Kite (the collector is the one reader of the chain).
//!
//! This file also declares the lane's private modules (`routes/derivatives/`), which the other
//! three derivatives routers reach as `super::options::derivatives`.

use axum::Router;
use axum::extract::{Extension, Path, RawQuery, State};
use axum::response::Response;
use axum::routing::get;
use chrono::{Datelike, Duration};

use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, bad_request, internal_error, not_found};
use crate::query::{QueryParams, Validation};
use crate::settings::Settings;
use crate::state::AppState;
use crate::wire;

#[path = "derivatives/mod.rs"]
pub(crate) mod derivatives;

use derivatives::options_core;
use derivatives::options_read::{
    self as reads, BacktestOut, CeilingsOut, ConfigOut, GateOut, PositionsOut, ScanHistoryOut,
    SessionsOut, TodayInputs,
};
use derivatives::shared::{now_ist, scoped_sole_user_id, validate_int};

/// `MAX_SESSION_WINDOW_DAYS`.
pub const MAX_SESSION_WINDOW_DAYS: i64 = 400;

/// `_gates(settings)`: `PAPER` while a group's execution flag is off; `DESK_DECIDES` when on
/// (the API cannot see the desk's own switches — never a guess of `LIVE`).
fn gates(settings: &Settings) -> Vec<GateOut> {
    [
        ("O1M", settings.options_o1m_execution_enabled),
        ("O1W", settings.options_o1w_execution_enabled),
        ("O2", settings.options_o2_execution_enabled),
        ("O3", settings.options_o3_execution_enabled),
    ]
    .into_iter()
    .map(|(group, enabled)| GateOut {
        group,
        execution_enabled: enabled,
        mode: if enabled { "DESK_DECIDES" } else { "PAPER" },
    })
    .collect()
}

/// `_sleeve(value)`: the sleeve code, or 404 naming the value as sent.
fn sleeve_or_404(value: &str) -> Result<&'static str, Problem> {
    options_core::sleeve(value).ok_or_else(|| not_found("sleeve", value))
}

/// `_optional_sleeve(value)`.
fn optional_sleeve(value: Option<&str>) -> Result<Option<&'static str>, Problem> {
    value.map(sleeve_or_404).transpose()
}

/// `GET /options/today`: `05` §2's header strip, the four panels and the clock, in one call.
///
/// # Errors
/// 401 anonymous; 404 a caller who is not the sole tenant; 503 no sole tenant configured; 500 a
/// database failure.
pub async fn get_options_today(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let db = &state.db;
    let settings = &state.settings;
    let user_id = scoped_sole_user_id(db, principal.user_id, None).await?;
    // `_market_state`.
    let now = now_ist();
    let today = now.date_naive();
    let session_day =
        today.weekday().num_days_from_monday() < 5 && reads::is_session_day(db, today).await?;
    let market_open = options_core::is_nse_session_open(session_day, now.time());
    let view = reads::today_view(
        db,
        TodayInputs {
            user_id,
            now,
            session_day,
            market_open,
            collect_enabled: settings.options_collect_enabled,
            scan_enabled: settings.options_scan_enabled,
            gates: gates(settings),
        },
    )
    .await?;
    Ok(wire::json(&view))
}

/// `GET /options/scan/{sleeve}?date=`: one sleeve's scan rows through a session (default the
/// latest scanned one).
///
/// # Errors
/// As [`get_options_today`]; 400 a bad `date`; 404 an unknown sleeve.
pub async fn get_options_scan(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    Path(sleeve): Path<String>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let date = v.date("date", q.get("date"));
    v.finish()?;
    let db = &state.db;
    let user_id = scoped_sole_user_id(db, principal.user_id, None).await?;
    let code = sleeve_or_404(&sleeve)?;
    let day = match date {
        Some(d) => Some(d),
        None => reads::latest_scan_date(db, user_id, now_ist().date_naive()).await?,
    };
    let rows = match day {
        Some(d) => reads::scan_history(db, user_id, code, d).await?,
        None => Vec::new(),
    };
    Ok(wire::json(&ScanHistoryOut {
        sleeve: code,
        trade_date: day,
        rows,
    }))
}

/// `GET /options/chain?expiry=`: the named expiry, or the nearest two, around ATM.
///
/// # Errors
/// As [`get_options_today`]; 400 a bad `expiry`.
pub async fn get_options_chain(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let expiry = v.date("expiry", q.get("expiry"));
    v.finish()?;
    let db = &state.db;
    scoped_sole_user_id(db, principal.user_id, None).await?;
    Ok(wire::json(&reads::chain(db, now_ist(), expiry).await?))
}

/// `GET /options/positions`: open positions and today's closed trades.
///
/// # Errors
/// As [`get_options_today`].
pub async fn get_options_positions(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let db = &state.db;
    let user_id = scoped_sole_user_id(db, principal.user_id, None).await?;
    let now = now_ist();
    Ok(wire::json(&PositionsOut {
        positions: reads::open_positions(db, user_id, now).await?,
        closed_today: reads::closed_on(db, user_id, now.date_naive()).await?,
    }))
}

/// `GET /options/sessions?from=&to=&sleeve=`: sessions in a window (30 days to `to` by default,
/// at most 400).
///
/// # Errors
/// As [`get_options_today`]; 400 bad dates or a bad window; 404 an unknown sleeve.
pub async fn get_options_sessions(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let date_from = v.date("from", q.get("from"));
    let date_to = v.date("to", q.get("to"));
    v.finish()?;
    let db = &state.db;
    let user_id = scoped_sole_user_id(db, principal.user_id, None).await?;
    let end = date_to.unwrap_or_else(|| now_ist().date_naive());
    let start = date_from.unwrap_or(end - Duration::days(30));
    if start > end || (end - start).num_days() > MAX_SESSION_WINDOW_DAYS {
        return Err(bad_request(
            &format!(
                "from must be on or before to, and the window at most \
                 {MAX_SESSION_WINDOW_DAYS} days"
            ),
            None,
        ));
    }
    let sleeve = optional_sleeve(q.get("sleeve"))?;
    Ok(wire::json(&SessionsOut {
        sessions: reads::sessions(db, user_id, start, end, sleeve).await?,
    }))
}

/// `GET /options/journal?sleeve=`: `04` §12's summary, never pooled, and the paper progress.
///
/// # Errors
/// As [`get_options_today`]; 404 an unknown sleeve.
pub async fn get_options_journal(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let db = &state.db;
    let user_id = scoped_sole_user_id(db, principal.user_id, None).await?;
    let sleeve = optional_sleeve(q.get("sleeve"))?;
    Ok(wire::json(&reads::journal(db, user_id, sleeve).await?))
}

/// `GET /options/backtest?sleeve=`: the newest run per sleeve per tier, caveat on the row.
///
/// # Errors
/// As [`get_options_journal`].
pub async fn get_options_backtest(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let db = &state.db;
    let user_id = scoped_sole_user_id(db, principal.user_id, None).await?;
    let sleeve = optional_sleeve(q.get("sleeve"))?;
    let runs = reads::backtests(db, user_id, sleeve).await?;
    let reason = runs.is_empty().then_some(
        "No options backtest has been run yet (OP12 builds them; Tier 3 waits on data).",
    );
    Ok(wire::json(&BacktestOut { runs, reason }))
}

/// `GET /options/calendar?year=`: the year's expiries and event days (2020-2100).
///
/// # Errors
/// As [`get_options_today`]; 400 a bad `year`.
pub async fn get_options_calendar(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let q = QueryParams::parse(raw.as_deref());
    let mut v = Validation::new();
    let year = validate_int(&mut v, "year", q.get("year"), Some(2020), Some(2100));
    v.finish()?;
    let db = &state.db;
    let user_id = scoped_sole_user_id(db, principal.user_id, None).await?;
    let year = match year {
        Some(y) => y
            .value
            .and_then(|x| i32::try_from(x).ok())
            .ok_or_else(internal_error)?,
        None => now_ist().year(),
    };
    Ok(wire::json(&reads::calendar(db, user_id, year).await?))
}

/// `GET /options/config`: the settings, the server's ceilings, every `04` threshold read-only,
/// and the audit trail.
///
/// # Errors
/// As [`get_options_today`].
pub async fn get_options_config(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?;
    let db = &state.db;
    let settings = &state.settings;
    let user_id = scoped_sole_user_id(db, principal.user_id, None).await?;
    let (book, sleeves) = match reads::read_config(db, user_id).await? {
        Some((book, sleeves)) => (Some(book), sleeves),
        None => (None, Vec::new()),
    };
    let audit = derivatives::fno_read::audit_trail(db, "op_config_audit", user_id).await?;
    Ok(wire::json(&ConfigOut {
        seeded: book.is_some(),
        book,
        sleeves,
        ceilings: CeilingsOut {
            risk_per_trade_inr_max: settings.options_risk_per_trade_inr_max,
            risk_pct_max: settings.options_risk_pct_max,
            max_lots_max: settings.options_max_lots_max,
            book_daily_loss_inr_max: settings.options_book_daily_loss_inr_max,
            book_monthly_loss_inr_max: settings.options_book_monthly_loss_inr_max,
            hard_exit_latest: settings.options_hard_exit_latest,
        },
        gates: gates(settings),
        thresholds: reads::thresholds(),
        audit,
    }))
}

/// The ported `/options` routes, relative to `/api/v1` (the writes stay on Python).
pub fn router() -> Router<AppState> {
    Router::new()
        .route("/options/today", get(get_options_today))
        .route("/options/scan/{sleeve}", get(get_options_scan))
        .route("/options/chain", get(get_options_chain))
        .route("/options/positions", get(get_options_positions))
        .route("/options/sessions", get(get_options_sessions))
        .route("/options/journal", get(get_options_journal))
        .route("/options/backtest", get(get_options_backtest))
        .route("/options/calendar", get(get_options_calendar))
        .route("/options/config", get(get_options_config))
}
