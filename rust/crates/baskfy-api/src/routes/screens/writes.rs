//! The **write** routes of `baskfy_api.routers.screens` (wave 4c, lane writes-screens):
//!
//! * `POST /screens` — create (honours `Idempotency-Key`: a replay answers the screen the first
//!   request made, while it still exists and is visible);
//! * `PATCH /screens/{public_id}` — rename, redefine, re-column one of your own screens;
//! * `DELETE /screens/{public_id}` — delete one of your own screens (`screen_run` rows go with it,
//!   by the foreign key);
//! * `POST /screens/{public_id}/duplicate` — copy any visible screen into one you own (honours
//!   `Idempotency-Key` too).
//!
//! Every statement runs in the request's one transaction ([`crate::session::run`]). An example
//! screen (`user_id IS NULL`) is never editable: `not-found` with the "read-only; duplicate it"
//! sentence. Not-yours is the same 404 as no-such-screen.
//!
//! **Not ported** (they run the screener, which no Rust module executes yet; the Python API keeps
//! serving them — DECISIONS-RS, lane writes-screens R6.13): `POST /screens/preview`, `/explain`, `/selection` and
//! `POST /screens/{public_id}/run`. Nothing here places, modifies or cancels an order (law 2).

use axum::extract::{Extension, Path, Request, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use baskfy_core::screener::{ScreenDefinition, resolve_columns};
use baskfy_db::{OrderedJson, row};
use sqlx::Row;

use super::body_model::{self, Errors, Model, Ty, V, opt, req};
use super::write_kit::{self, db};
use super::{ScreenOut, ScreenRow, definition_dump, stored_definition, visible_to};
use crate::auth::{Principal, require_authenticated};
use crate::entitlements::entitlements_for;
use crate::problems::{Problem, ProblemType, internal_error, not_found};
use crate::session::{self, Tx, WritePool};
use crate::state::AppState;
use crate::wire;

/// `PUBLIC_ID_BYTES`: twelve hex characters, the seed ids' length.
const PUBLIC_ID_BYTES: usize = 6;

/// `seed_data.DEFAULT_COLUMNS`.
const DEFAULT_COLUMNS: [&str; 3] = ["ret_12m", "vol_12m", "close_raw"];

const SCREEN_COLUMNS: &str = "id, public_id, user_id, name, definition, columns, is_example, \
                              created_at, updated_at";

/// `ScreenCreate`.
fn screen_create() -> Model {
    Model {
        fields: vec![
            req("name", Ty::Str(Some(1), Some(120))),
            req("definition", Ty::Definition),
            opt(
                "columns",
                Ty::Opt(Box::new(Ty::List(Box::new(Ty::Str(None, None)), None))),
            ),
        ],
        forbid_extra: true,
    }
}

/// `ScreenUpdate`.
fn screen_update() -> Model {
    Model {
        fields: vec![
            opt("name", Ty::Opt(Box::new(Ty::Str(Some(1), Some(120))))),
            opt("definition", Ty::Opt(Box::new(Ty::Definition))),
            opt(
                "columns",
                Ty::Opt(Box::new(Ty::List(Box::new(Ty::Str(None, None)), None))),
            ),
        ],
        forbid_extra: true,
    }
}

/// `ScreenDuplicate`.
fn screen_duplicate() -> Model {
    Model {
        fields: vec![opt("name", Ty::Opt(Box::new(Ty::Str(Some(1), Some(120)))))],
        forbid_extra: true,
    }
}

fn screen_row(r: &sqlx::postgres::PgRow) -> Result<ScreenRow, Problem> {
    Ok(ScreenRow {
        id: r.try_get("id").map_err(db)?,
        public_id: r.try_get("public_id").map_err(db)?,
        user_id: r.try_get("user_id").map_err(db)?,
        name: r.try_get("name").map_err(db)?,
        definition: row::jsonb(r, "definition").map_err(|e| crate::db_failure(&e))?,
        columns: row::jsonb(r, "columns").map_err(|e| crate::db_failure(&e))?,
        is_example: r.try_get("is_example").map_err(db)?,
        created_at: row::timestamptz(r, "created_at").map_err(|e| crate::db_failure(&e))?,
        updated_at: row::timestamptz(r, "updated_at").map_err(|e| crate::db_failure(&e))?,
    })
}

/// `_new_public_id()`: `secrets.token_hex(6)`.
fn new_public_id() -> String {
    crate::security::token_hex(PUBLIC_ID_BYTES)
}

/// The `Idempotency-Key` header as Starlette reads it (Latin-1).
fn idempotency_key(request: &Request) -> Option<String> {
    request
        .headers()
        .get(crate::idempotency::HEADER)
        .map(|v| v.as_bytes().iter().map(|&b| char::from(b)).collect())
}

/// `_validated_columns(requested)`: the defaults for `None`; an unknown key is the
/// `ScreenQueryError` 400 (`{"field": "definition", "message": ...}`, no `type`).
fn validated_columns(requested: Option<&[V]>) -> Result<Vec<String>, Problem> {
    let Some(items) = requested else {
        return Ok(DEFAULT_COLUMNS.iter().map(|s| (*s).to_owned()).collect());
    };
    let names: Vec<String> = items
        .iter()
        .filter_map(|v| v.str().map(str::to_owned))
        .collect();
    resolve_columns(&names).map_err(|e| {
        crate::problems::invalid_screen_definition(vec![vec![
            (
                "field".to_owned(),
                OrderedJson::Str("definition".to_owned()),
            ),
            ("message".to_owned(), OrderedJson::Str(e.to_string())),
        ]])
    })?;
    Ok(names)
}

fn columns_json(columns: &[String]) -> Result<String, Problem> {
    OrderedJson::Array(columns.iter().cloned().map(OrderedJson::Str).collect())
        .to_python_json()
        .map_err(|_| internal_error())
}

fn definition_json(definition: &ScreenDefinition) -> Result<String, Problem> {
    definition_dump(definition)
        .to_python_json()
        .map_err(|_| internal_error())
}

async fn load_tx(tx: &mut Tx, public_id: &str) -> Result<Option<ScreenRow>, Problem> {
    let found = sqlx::query(&format!(
        "SELECT {SCREEN_COLUMNS} FROM screen WHERE public_id = $1"
    ))
    .bind(public_id)
    .fetch_optional(&mut **tx)
    .await
    .map_err(db)?;
    found.as_ref().map(screen_row).transpose()
}

/// `_load_visible`.
async fn load_visible(
    tx: &mut Tx,
    public_id: &str,
    principal: &Principal,
) -> Result<ScreenRow, Problem> {
    match load_tx(tx, public_id).await? {
        Some(s) if visible_to(s.user_id, principal) => Ok(s),
        _ => Err(not_found("screen", public_id)),
    }
}

/// `_load_editable`.
async fn load_editable(
    tx: &mut Tx,
    public_id: &str,
    principal: &Principal,
) -> Result<ScreenRow, Problem> {
    let screen = load_visible(tx, public_id, principal).await?;
    if screen.user_id.is_none() || screen.user_id != principal.user_id {
        return Err(Problem::new(
            ProblemType::NotFound,
            format!(
                "Screen {} is read-only; duplicate it to make an editable copy.",
                baskfy_providers::py::repr_str(public_id)
            ),
        ));
    }
    Ok(screen)
}

/// `_replayed_screen`: the screen a previous request with this key created, if it still exists
/// and is visible; otherwise no record at all.
async fn replayed(
    state: &AppState,
    tx: &mut Tx,
    principal: &Principal,
    scope: &str,
    key: Option<&str>,
) -> Result<Option<ScreenOut>, Problem> {
    let Some(remembered) = crate::idempotency::replay(&state.settings.redis_url, scope, key).await
    else {
        return Ok(None);
    };
    match load_tx(tx, &remembered).await? {
        Some(s) if visible_to(s.user_id, principal) => Ok(Some(super::to_out(&s, principal)?)),
        _ => Ok(None),
    }
}

/// The INSERT both create paths share; returns the row as the ORM holds it after the flush.
async fn insert_screen(
    tx: &mut Tx,
    user_id: i64,
    name: &str,
    definition_json: &str,
    columns_json: &str,
) -> Result<ScreenRow, Problem> {
    let r = sqlx::query(&format!(
        "INSERT INTO screen (public_id, user_id, name, definition, columns, is_example) \
         VALUES ($1, $2, $3, ($4::text)::jsonb, ($5::text)::jsonb, false) \
         RETURNING {SCREEN_COLUMNS}"
    ))
    .bind(new_public_id())
    .bind(user_id)
    .bind(name)
    .bind(definition_json)
    .bind(columns_json)
    .fetch_one(&mut **tx)
    .await
    .map_err(db)?;
    screen_row(&r)
}

/// `POST /screens` (`create_screen`).
///
/// # Errors
/// 400 (body, unknown column), 401, 402 (`max_screens`), 500.
pub async fn create_screen(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let key = idempotency_key(&request);
    let received = write_kit::read(request).await?;
    let principal = require_authenticated(&principal)?.clone();
    let entitlements = entitlements_for(&state.db, &principal, &state.settings).await?;
    let mut e = Errors::new();
    let body = body_model::body(&mut e, &received, &screen_create(), true);
    e.finish()?;
    let body = body.ok_or_else(internal_error)?;
    let user_id = principal.require_user()?;
    let pool = write_kit::pool(write)?;
    let st = state.clone();
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let scope = format!("screen:create:{user_id}");
            if let Some(out) = replayed(&st, tx, &principal, &scope, key.as_deref()).await? {
                return Ok(out);
            }
            let count: i64 = sqlx::query_scalar("SELECT count(*) FROM screen WHERE user_id = $1")
                .bind(user_id)
                .fetch_one(&mut **tx)
                .await
                .map_err(db)?;
            if count >= entitlements.max_screens {
                return Err(crate::problems::payment_required(
                    "max_screens",
                    Some(&format!(
                        "Your plan allows {} saved screens.",
                        entitlements.max_screens
                    )),
                ));
            }
            let V::Definition(definition) = body.get("definition") else {
                return Err(internal_error());
            };
            let columns = validated_columns(match body.get("columns") {
                V::List(items) => Some(items.as_slice()),
                _ => None,
            })?;
            let name = body.get("name").str().unwrap_or_default().to_owned();
            let screen = insert_screen(
                tx,
                user_id,
                &name,
                &definition_json(definition)?,
                &columns_json(&columns)?,
            )
            .await?;
            crate::idempotency::remember(
                &st.settings.redis_url,
                &scope,
                key.as_deref(),
                &screen.public_id,
            )
            .await;
            super::to_out(&screen, &principal)
        })
    })
    .await?;
    Ok(wire::json_status(StatusCode::CREATED, &out))
}

/// `PATCH /screens/{public_id}` (`update_screen`).
///
/// # Errors
/// 400 (body, unknown column), 401, 404 (absent, not yours, an example screen), 500.
pub async fn update_screen(
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(public_id): Path<String>,
    request: Request,
) -> Result<Response, Problem> {
    let received = write_kit::read(request).await?;
    let principal = require_authenticated(&principal)?.clone();
    let mut e = Errors::new();
    let body = body_model::body(&mut e, &received, &screen_update(), true);
    e.finish()?;
    let body = body.ok_or_else(internal_error)?;
    let pool = write_kit::pool(write)?;
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let mut screen = load_editable(tx, &public_id, &principal).await?;
            if let Some(name) = body.get("name").str() {
                screen.name = name.to_owned();
            }
            let mut definition_text: Option<String> = None;
            if let V::Definition(definition) = body.get("definition") {
                let dumped = definition_dump(definition);
                definition_text = Some(dumped.to_python_json().map_err(|_| internal_error())?);
                screen.definition = dumped;
            }
            let mut columns_text: Option<String> = None;
            if let V::List(items) = body.get("columns") {
                let columns = validated_columns(Some(items))?;
                columns_text = Some(columns_json(&columns)?);
                screen.columns =
                    OrderedJson::Array(columns.into_iter().map(OrderedJson::Str).collect());
            }
            let now = write_kit::now_utc();
            sqlx::query(
                "UPDATE screen SET name = $1, \
                 definition = COALESCE(($2::text)::jsonb, definition), \
                 columns = COALESCE(($3::text)::jsonb, columns), updated_at = $4 WHERE id = $5",
            )
            .bind(&screen.name)
            .bind(definition_text)
            .bind(columns_text)
            .bind(now)
            .bind(screen.id)
            .execute(&mut **tx)
            .await
            .map_err(db)?;
            screen.updated_at = now;
            super::to_out(&screen, &principal)
        })
    })
    .await?;
    Ok(wire::json(&out))
}

/// `DELETE /screens/{public_id}` (`delete_screen`): 204.
///
/// # Errors
/// 401, 404 (absent, not yours, an example screen), 500.
pub async fn delete_screen(
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(public_id): Path<String>,
) -> Result<Response, Problem> {
    // A DELETE has no body parameter; FastAPI never reads one.
    let principal = require_authenticated(&principal)?.clone();
    let pool = write_kit::pool(write)?;
    session::run(&pool, move |tx| {
        Box::pin(async move {
            let screen = load_editable(tx, &public_id, &principal).await?;
            sqlx::query("DELETE FROM screen WHERE id = $1")
                .bind(screen.id)
                .execute(&mut **tx)
                .await
                .map_err(db)?;
            Ok(())
        })
    })
    .await?;
    Ok(StatusCode::NO_CONTENT.into_response())
}

/// `POST /screens/{public_id}/duplicate` (`duplicate_screen`): 201.
///
/// # Errors
/// 400 (body), 401, 404 (absent or not yours), 500.
pub async fn duplicate_screen(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    Path(public_id): Path<String>,
    request: Request,
) -> Result<Response, Problem> {
    let key = idempotency_key(&request);
    let received = write_kit::read(request).await?;
    let principal = require_authenticated(&principal)?.clone();
    let mut e = Errors::new();
    let body = body_model::body(&mut e, &received, &screen_duplicate(), false);
    e.finish()?;
    let body = body.ok_or_else(internal_error)?;
    let user_id = principal.require_user()?;
    let pool = write_kit::pool(write)?;
    let st = state.clone();
    let out = session::run(&pool, move |tx| {
        Box::pin(async move {
            let source = load_visible(tx, &public_id, &principal).await?;
            let scope = format!("screen:duplicate:{user_id}:{public_id}");
            if let Some(out) = replayed(&st, tx, &principal, &scope, key.as_deref()).await? {
                return Ok(out);
            }
            let name = match body.get("name").str() {
                Some(n) if !n.is_empty() => n.to_owned(),
                _ => format!("{} (copy)", source.name),
            };
            // `dict(source.definition)` / `list(source.columns)`: the stored JSON as Python
            // decoded it, written back through `json.dumps`.
            let definition = source
                .definition
                .to_python_json()
                .map_err(|_| internal_error())?;
            let columns = source
                .columns
                .to_python_json()
                .map_err(|_| internal_error())?;
            let copy = insert_screen(tx, user_id, &name, &definition, &columns).await?;
            crate::idempotency::remember(
                &st.settings.redis_url,
                &scope,
                key.as_deref(),
                &copy.public_id,
            )
            .await;
            // `_to_out` re-validates the copy's definition (a 500 if the source's no longer does).
            let _ = stored_definition(&copy.definition)?;
            super::to_out(&copy, &principal)
        })
    })
    .await?;
    Ok(wire::json_status(StatusCode::CREATED, &out))
}
