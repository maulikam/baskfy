//! The **write** routes of `baskfy_api.routers.watchlist` (wave 4c, lane writes-screens):
//!
//! * `POST /watchlist/instruments` — watch an active instrument (re-watching restamps it);
//! * `DELETE /watchlist/instruments/{symbol}` — stop watching it (204);
//! * `PUT /watchlist/discover-preferences` — save the discover preferences (insert or update).
//!
//! Sole-tenant surfaces (`scoped_sole_user_id`), one transaction per request. A watchlist is a
//! list of names: nothing here places, modifies or cancels an order (law 2).

use axum::extract::{Extension, Path, Request, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use baskfy_core::decimal::Rounding;
use baskfy_db::row;
use rust_decimal::Decimal;
use sqlx::Row;

use super::{DiscoverPreferencesOut, InstrumentWatchItemOut, moved_pct};
use crate::auth::{Principal, require_authenticated};
use crate::problems::{Problem, internal_error, not_found};
use crate::routes::common::py_space;
use crate::routes::screens::accounts::scoped_sole_user_id;
use crate::routes::screens::body_model::{self, Errors, Model, Ty, opt, req};
use crate::routes::screens::write_kit::{self, db};
use crate::session::{self, Tx, WritePool};
use crate::state::AppState;
use crate::wire;

/// `SYMBOL_MAX`.
const SYMBOL_MAX: usize = 32;

/// `GoalKey`, `HorizonKey`, `RiskKey`, `RebalanceKey`.
const GOALS: &[&str] = &["steady-compounding", "long-term-growth", "high-growth"];
const HORIZONS: &[&str] = &["1-3", "3-5", "5-plus"];
const RISKS: &[&str] = &["lower", "moderate", "higher"];
const REBALANCES: &[&str] = &["any", "WEEKLY", "MONTHLY", "QUARTERLY"];

/// `InstrumentWatchAddIn`.
fn add_in() -> Model {
    Model {
        fields: vec![req("symbol", Ty::Str(Some(1), Some(SYMBOL_MAX)))],
        forbid_extra: true,
    }
}

/// `DiscoverPreferencesIn`.
fn preferences_in() -> Model {
    Model {
        fields: vec![
            req("goal", Ty::Lit(GOALS)),
            req("horizon", Ty::Lit(HORIZONS)),
            req("risk", Ty::Lit(RISKS)),
            req(
                "amount",
                Ty::Dec {
                    gt: None,
                    ge: Some("1000"),
                    le: Some("100000000"),
                },
            ),
            req("rebalance", Ty::Lit(REBALANCES)),
            opt("onboarding_completed", Ty::Bool),
        ],
        forbid_extra: true,
    }
}

/// `symbol.strip().upper()`.
fn needle(symbol: &str) -> String {
    symbol.trim_matches(py_space).to_uppercase()
}

/// One resolved instrument.
struct Instrument {
    id: i64,
    symbol: String,
    name: String,
}

/// `_resolve_active_instrument`: the active instrument with this symbol, preferring EQ cash when
/// the symbol collides across types.
async fn resolve_active(tx: &mut Tx, symbol: &str) -> Result<Option<Instrument>, Problem> {
    let rows = sqlx::query(
        "SELECT id, symbol, name, instrument_type FROM instrument \
         WHERE symbol = $1 AND is_active IS true ORDER BY id ASC LIMIT 2",
    )
    .bind(needle(symbol))
    .fetch_all(&mut **tx)
    .await
    .map_err(db)?;
    let pick = if rows.len() > 1 {
        let mut chosen = None;
        for r in &rows {
            let kind: Option<String> = r.try_get("instrument_type").map_err(db)?;
            if kind.as_deref() == Some("EQ") {
                chosen = Some(r);
                break;
            }
        }
        chosen.or_else(|| rows.first())
    } else {
        rows.first()
    };
    pick.map(|r| {
        Ok(Instrument {
            id: r.try_get("id").map_err(db)?,
            symbol: r.try_get("symbol").map_err(db)?,
            name: r.try_get("name").map_err(db)?,
        })
    })
    .transpose()
}

/// `_last_close`.
async fn last_close(tx: &mut Tx, instrument_id: i64) -> Result<Option<Decimal>, Problem> {
    let last = sqlx::query(
        "SELECT close FROM ohlcv_daily WHERE instrument_id = $1 ORDER BY date DESC LIMIT 1",
    )
    .bind(instrument_id)
    .fetch_optional(&mut **tx)
    .await
    .map_err(db)?;
    last.map(|l| row::decimal(&l, "close").map_err(|e| crate::db_failure(&e)))
        .transpose()
}

/// `POST /watchlist/instruments` (`add_instrument_watch`): 201.
///
/// # Errors
/// 400 (body), 401, the sole-tenant refusals, 404 (no such active instrument), 500.
pub async fn add_instrument_watch(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let received = write_kit::read(request).await?;
    let principal = require_authenticated(&principal)?.clone();
    let mut e = Errors::new();
    let body = body_model::body(&mut e, &received, &add_in(), true);
    e.finish()?;
    let body = body.ok_or_else(internal_error)?;
    let symbol = body.get("symbol").str().unwrap_or_default().to_owned();
    let user_id = scoped_sole_user_id(&state, principal.user_id, None).await?;
    let pool = write_kit::pool(write)?;
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let Some(instrument) = resolve_active(tx, &symbol).await? else {
                return Err(not_found("instrument", &needle(&symbol)));
            };
            let existing: Option<i64> = sqlx::query_scalar(
                "SELECT id FROM instrument_watch_item WHERE user_id = $1 AND instrument_id = $2",
            )
            .bind(user_id)
            .bind(instrument.id)
            .fetch_optional(&mut **tx)
            .await
            .map_err(db)?;
            let now = write_kit::now_utc();
            let last = last_close(tx, instrument.id).await?;
            let last_text = last.map(write_kit::dec_str);
            let r = match existing {
                Some(id) => sqlx::query(
                    "UPDATE instrument_watch_item SET watched_at = $1, \
                     close_at_watch = ($2::text)::numeric WHERE id = $3 \
                     RETURNING watched_at, close_at_watch",
                )
                .bind(now)
                .bind(last_text)
                .bind(id)
                .fetch_one(&mut **tx)
                .await
                .map_err(db)?,
                None => sqlx::query(
                    "INSERT INTO instrument_watch_item \
                     (user_id, instrument_id, watched_at, close_at_watch) \
                     VALUES ($1, $2, $3, ($4::text)::numeric) RETURNING watched_at, close_at_watch",
                )
                .bind(user_id)
                .bind(instrument.id)
                .bind(now)
                .bind(last_text)
                .fetch_one(&mut **tx)
                .await
                .map_err(db)?,
            };
            // `session.refresh(item)`: the stored (rounded) values.
            let close_at_watch =
                row::opt_decimal(&r, "close_at_watch").map_err(|e| crate::db_failure(&e))?;
            Ok(InstrumentWatchItemOut {
                symbol: instrument.symbol,
                name: instrument.name,
                watched_at: row::timestamptz(&r, "watched_at")
                    .map_err(|e| crate::db_failure(&e))?,
                close_at_watch,
                last_close: last,
                moved_pct: moved_pct(close_at_watch, last),
            })
        })
    })
    .await?;
    Ok(wire::json_status(StatusCode::CREATED, &out))
}

/// `DELETE /watchlist/instruments/{symbol}` (`remove_instrument_watch`): 204.
///
/// # Errors
/// 400 (the path's length), 401, the sole-tenant refusals, 404, 500.
pub async fn remove_instrument_watch(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(symbol): Path<String>,
) -> Result<Response, Problem> {
    let principal = require_authenticated(&principal)?.clone();
    let mut e = Errors::new();
    body_model::path_str(&mut e, "symbol", &symbol, Some(1), Some(SYMBOL_MAX));
    e.finish()?;
    let user_id = scoped_sole_user_id(&state, principal.user_id, None).await?;
    let pool = write_kit::pool(write)?;
    session::run(&pool, move |tx| {
        Box::pin(async move {
            let Some(instrument) = resolve_active(tx, &symbol).await? else {
                return Err(not_found("instrument", &needle(&symbol)));
            };
            let item: Option<i64> = sqlx::query_scalar(
                "SELECT id FROM instrument_watch_item WHERE user_id = $1 AND instrument_id = $2",
            )
            .bind(user_id)
            .bind(instrument.id)
            .fetch_optional(&mut **tx)
            .await
            .map_err(db)?;
            let Some(id) = item else {
                return Err(not_found("watchlist instrument", &needle(&symbol)));
            };
            sqlx::query("DELETE FROM instrument_watch_item WHERE id = $1")
                .bind(id)
                .execute(&mut **tx)
                .await
                .map_err(db)?;
            Ok(())
        })
    })
    .await?;
    // FastAPI's 204 for a handler returning `None`: an empty `JSONResponse`, its content type set.
    Ok((
        StatusCode::NO_CONTENT,
        [(axum::http::header::CONTENT_TYPE, crate::wire::JSON)],
    )
        .into_response())
}

/// `PUT /watchlist/discover-preferences` (`put_discover_preferences`).
///
/// # Errors
/// 400 (body), 401, the sole-tenant refusals, 500.
pub async fn put_discover_preferences(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let received = write_kit::read(request).await?;
    let principal = require_authenticated(&principal)?.clone();
    let mut e = Errors::new();
    let body = body_model::body(&mut e, &received, &preferences_in(), true);
    e.finish()?;
    let body = body.ok_or_else(internal_error)?;
    let user_id = scoped_sole_user_id(&state, principal.user_id, None).await?;
    let text = |k: &str| body.get(k).str().unwrap_or_default().to_owned();
    let (goal, horizon, risk, rebalance) = (
        text("goal"),
        text("horizon"),
        text("risk"),
        text("rebalance"),
    );
    let completed = body.get("onboarding_completed").bool().unwrap_or(false);
    // `body.amount.quantize(Decimal("0.01"))` (the default context: half-even).
    let amount = body
        .get("amount")
        .dec()
        .ok_or_else(internal_error)?
        .quantize(-2, Rounding::HalfEven)
        .map_err(|_| internal_error())?
        .to_sci_string();
    let pool = write_kit::pool(write)?;
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let now = write_kit::now_utc();
            let existing = sqlx::query(
                "SELECT onboarding_completed_at FROM user_discover_preferences WHERE user_id = $1",
            )
            .bind(user_id)
            .fetch_optional(&mut **tx)
            .await
            .map_err(db)?;
            let r = match existing {
                None => sqlx::query(
                    "INSERT INTO user_discover_preferences (user_id, goal, horizon, risk, amount, \
                     rebalance, onboarding_completed_at, updated_at) \
                     VALUES ($1, $2, $3, $4, ($5::text)::numeric, $6, $7, $8) \
                     RETURNING goal, horizon, risk, amount, rebalance, onboarding_completed_at, \
                     updated_at",
                )
                .bind(user_id)
                .bind(&goal)
                .bind(&horizon)
                .bind(&risk)
                .bind(&amount)
                .bind(&rebalance)
                .bind(completed.then_some(now))
                .bind(now)
                .fetch_one(&mut **tx)
                .await
                .map_err(db)?,
                Some(row_found) => {
                    let stamped = row::opt_timestamptz(&row_found, "onboarding_completed_at")
                        .map_err(|e| crate::db_failure(&e))?;
                    let completed_at = if completed && stamped.is_none() {
                        Some(now)
                    } else {
                        stamped
                    };
                    sqlx::query(
                        "UPDATE user_discover_preferences SET goal = $2, horizon = $3, \
                         risk = $4, amount = ($5::text)::numeric, rebalance = $6, \
                         onboarding_completed_at = $7, updated_at = $8 WHERE user_id = $1 \
                         RETURNING goal, horizon, risk, amount, rebalance, \
                         onboarding_completed_at, updated_at",
                    )
                    .bind(user_id)
                    .bind(&goal)
                    .bind(&horizon)
                    .bind(&risk)
                    .bind(&amount)
                    .bind(&rebalance)
                    .bind(completed_at)
                    .bind(now)
                    .fetch_one(&mut **tx)
                    .await
                    .map_err(db)?
                }
            };
            let dbe = |e: baskfy_db::DbError| crate::db_failure(&e);
            Ok(DiscoverPreferencesOut {
                goal: r.try_get("goal").map_err(db)?,
                horizon: r.try_get("horizon").map_err(db)?,
                risk: r.try_get("risk").map_err(db)?,
                amount: row::decimal(&r, "amount").map_err(dbe)?,
                rebalance: r.try_get("rebalance").map_err(db)?,
                onboarding_completed_at: row::opt_timestamptz(&r, "onboarding_completed_at")
                    .map_err(dbe)?,
                updated_at: Some(row::timestamptz(&r, "updated_at").map_err(dbe)?),
                saved: true,
            })
        })
    })
    .await?;
    Ok(wire::json(&out))
}
