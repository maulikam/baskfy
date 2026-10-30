//! Lane **analytics** — the read-mostly pages and the daily collection: performance, tradebook,
//! indices, regime, settings, ops (ported by the wave-D *views* lane, which also owns
//! `session.rs`).
//!
//! **STATUS (views lane):** ported and graded by `tools/parity/desk_diff.py`
//! (`desk_cases/views.json`): `GET /settings`, `GET /settings/data`, `POST /settings`,
//! `POST /settings/reset`, `GET /ops`, `GET /ops/data`, `GET /ops/job/{job_id}`, `POST /ops/run`.
//! Python ported as private submodules: `analytics/settings.py` ([`desk_settings`]),
//! `analytics/ops.py` ([`ops`]), `analytics/daily_runs.py` ([`daily_runs`]).
//!
//! **Lane analytics_regime** ([`analytics_regime`]): `GET /regime` and `GET /regime/data`, with
//! [`regime`], [`regime_store`], [`breadth`], [`index_cache`] and [`regime_view`] ported (reads;
//! the evaluation CLI `regime_run.py`, its engine and `/indices*` stay on Python — DECISIONS-RS R-ar.1).
//! **Lane analytics_views** ([`analytics_views`]): see its module docs.
//!
//! **Not ported yet (loud):** the routes and modules no lane above names
//! (`/tradebook*`, `/indices*`, `/options*`, `index_view`, `nse_indices`, `regime_run`, …) still
//! answer the app's 404 and the Python desk serves them. See `docs/rust-rewrite/STATUS.md`.
//!
//! The lane never edits `lib.rs`, `app.rs` or `lanes.rs`.

pub mod analytics_regime;
pub mod analytics_views;
pub mod breadth;
pub mod daily_runs;
pub mod desk_settings;
pub mod index_cache;
pub mod ops;
pub mod regime;
pub mod regime_store;
pub mod regime_view;
pub mod web;

use axum::Router;
use axum::body::{Body, Bytes};
use axum::extract::{Path as AxPath, State};
use axum::http::{Response, StatusCode, Uri};
use axum::routing::{get, post};

use crate::app::AppState;
use crate::storage::{Conn, DbValue, StorageError};
use crate::wire::{PyValue, dict};

use desk_settings::Failure;
use web::{MultiDict, detail, json, redirect_303, render, storage_500};

/// The lane's routes.
pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/settings", get(settings_page).post(settings_save))
        .route("/settings/data", get(settings_data))
        .route("/settings/reset", post(settings_reset))
        .route("/ops", get(ops_page))
        .route("/ops/data", get(ops_data))
        .route("/ops/run", post(ops_run))
        .route("/ops/job/{job_id}", get(ops_job))
        .merge(analytics_views::routes())
        .merge(analytics_regime::routes())
}

/// The lane's template globals and filters.
pub fn register_templates(templates: &mut crate::templates::Templates) {
    let _ = templates;
}

/// Run one of the lane's background services by name; `None` if the name is not this lane's.
pub async fn run_service(name: &str, state: &AppState) -> Option<Result<(), String>> {
    let _ = state;
    let _ = name;
    None
}

/// `with db.connect() as conn: db.migrate(conn)`.
async fn connect(state: &AppState) -> Result<Conn, StorageError> {
    let mut conn = state.db.connect().await?;
    conn.migrate().await?;
    Ok(conn)
}

/// `main._latest_nav()`: NAV from the most recent stored snapshot, 0.0 on any failure.
pub async fn latest_nav(state: &AppState) -> f64 {
    let Ok(mut conn) = connect(state).await else {
        return 0.0;
    };
    let Ok(cur) = conn
        .execute(
            "SELECT date, nav, invested, cash, index_value FROM snapshots ORDER BY date",
            &[],
        )
        .await
    else {
        return 0.0;
    };
    cur.rows
        .last()
        .and_then(|r| r.get("nav"))
        .and_then(|v| match v {
            DbValue::Text(s) => crate::settings::py_float(s),
            other => other.as_f64(),
        })
        .unwrap_or(0.0)
}

fn failure_500(e: &Failure) -> Response<Body> {
    web::internal_error(&e.to_string())
}

// ---------------------------------------------------------------------------------------------
// settings
// ---------------------------------------------------------------------------------------------

async fn settings_page(State(state): State<AppState>, uri: Uri) -> Response<Body> {
    let q = MultiDict::from_query(&uri);
    let env = desk_settings::process_env;
    let settings = state.settings_snapshot();
    let mut conn = match connect(&state).await {
        Ok(c) => c,
        Err(e) => return storage_500(&e),
    };
    let eff = match desk_settings::effective(&mut conn, &settings, &env).await {
        Ok(e) => e,
        Err(e) => return failure_500(&e),
    };
    let history = match desk_settings::history(&mut conn, 25).await {
        Ok(h) => h,
        Err(e) => return storage_500(&e),
    };
    let overrides = match desk_settings::stored_overrides(&mut conn).await {
        Ok(o) => o,
        Err(e) => return failure_500(&e),
    };
    let nav = latest_nav(&state).await;
    let risk = match desk_settings::risk_preview(&settings, &overrides, nav) {
        Ok(r) => r,
        Err(e) => return failure_500(&e),
    };
    let locked = desk_settings::locked_view(&settings)
        .into_iter()
        .map(|(k, v, why)| {
            PyValue::Map(dict([
                ("key", PyValue::Str(k.to_owned())),
                ("value", v),
                ("why", PyValue::Str(why.to_owned())),
            ]))
        })
        .collect();
    let ctx = dict([
        ("eff", PyValue::Map(desk_settings::effective_py(&eff))),
        (
            "groups",
            PyValue::List(
                desk_settings::groups()
                    .into_iter()
                    .map(|g| PyValue::Str(g.to_owned()))
                    .collect(),
            ),
        ),
        ("locked", PyValue::List(locked)),
        ("history", history),
        ("saved", PyValue::Str(q.str_or_empty("saved"))),
        ("error", PyValue::Str(q.str_or_empty("error"))),
        ("dry_run", PyValue::Bool(settings.dry_run)),
        ("risk", risk),
    ]);
    render(&state, uri.path(), "settings.html", ctx)
}

async fn settings_data(State(state): State<AppState>) -> Response<Body> {
    let env = desk_settings::process_env;
    let settings = state.settings_snapshot();
    let mut conn = match connect(&state).await {
        Ok(c) => c,
        Err(e) => return storage_500(&e),
    };
    let eff = match desk_settings::effective(&mut conn, &settings, &env).await {
        Ok(e) => e,
        Err(e) => return failure_500(&e),
    };
    let mut out = crate::wire::PyDict::new();
    for e in &eff {
        out.insert(
            e.spec.key,
            PyValue::Map(dict([
                ("value", PyValue::Str(e.text.clone())),
                ("source", PyValue::Str(e.source.to_owned())),
                ("group", PyValue::Str(e.spec.group.to_owned())),
                ("label", PyValue::Str(e.spec.label.to_owned())),
            ])),
        );
    }
    let locked = desk_settings::locked_view(&settings)
        .into_iter()
        .map(|(k, v, _)| {
            PyValue::Map(dict([
                ("key", PyValue::Str(k.to_owned())),
                ("value", PyValue::Str(v.py_str())),
            ]))
        })
        .collect();
    let history = match desk_settings::history(&mut conn, 25).await {
        Ok(h) => h,
        Err(e) => return storage_500(&e),
    };
    json(
        StatusCode::OK,
        &PyValue::Map(dict([
            ("settings", PyValue::Map(out)),
            ("locked", PyValue::List(locked)),
            ("history", history),
        ])),
    )
}

async fn settings_save(State(state): State<AppState>, body: Bytes) -> Response<Body> {
    let form = MultiDict::from_form(&body);
    let note = form.get("note").unwrap_or_default().trim().to_owned();
    let mut updates: Vec<(String, String)> = form
        .items()
        .into_iter()
        .filter(|(k, _)| {
            desk_settings::by_key(k).is_some() && !desk_settings::SECRET_KEYS.contains(&k.as_str())
        })
        .collect();
    let rendered = form.get("_form_bools").unwrap_or_default().to_owned();
    for key in rendered.split(',').map(str::trim).filter(|k| !k.is_empty()) {
        if let Some(spec) = desk_settings::by_key(key)
            && spec.kind == "bool"
            && !updates.iter().any(|(k, _)| k == key)
        {
            updates.push((key.to_owned(), "false".to_owned()));
        }
    }
    let mut conn = match connect(&state).await {
        Ok(c) => c,
        Err(e) => return storage_500(&e),
    };
    let now = web::now_iso_seconds(&state);
    let env = desk_settings::process_env;
    match desk_settings::save(&mut conn, &state.settings, &env, &updates, &note, &now).await {
        Ok(res) => {
            let msg = if res.changed.is_empty() {
                "no changes".to_owned()
            } else {
                format!("{} changed: {}", res.changed.len(), res.changed.join(", "))
            };
            redirect_303(&format!("/settings?saved={msg}"))
        }
        Err(Failure::Refused(e)) => redirect_303(&format!("/settings?error={e}")),
        Err(e) => failure_500(&e),
    }
}

async fn settings_reset(State(state): State<AppState>, body: Bytes) -> Response<Body> {
    let form = MultiDict::from_form(&body);
    let keys = form.getlist("key");
    let keys = if keys.is_empty() { None } else { Some(keys) };
    let mut conn = match connect(&state).await {
        Ok(c) => c,
        Err(e) => return storage_500(&e),
    };
    let now = web::now_iso_seconds(&state);
    let env = desk_settings::process_env;
    match desk_settings::reset(&mut conn, &state.settings, &env, keys, &now).await {
        Ok(reset) => redirect_303(&format!(
            "/settings?saved=reset {} override(s)",
            reset.len()
        )),
        Err(e) => failure_500(&e),
    }
}

// ---------------------------------------------------------------------------------------------
// operations
// ---------------------------------------------------------------------------------------------

async fn ops_page(State(state): State<AppState>, uri: Uri) -> Response<Body> {
    let q = MultiDict::from_query(&uri);
    let mut conn = match connect(&state).await {
        Ok(c) => c,
        Err(e) => return storage_500(&e),
    };
    let mut ctx = match ops::view(&mut conn, &state).await {
        Ok(c) => c,
        Err(e) => return storage_500(&e),
    };
    let today = state.clock.today_ist();
    match daily_runs::status(&mut conn, today).await {
        Ok(s) => ctx.insert("collection", PyValue::Map(s)),
        Err(e) => return storage_500(&e),
    }
    match daily_runs::history(&mut conn, 10).await {
        Ok(h) => ctx.insert(
            "daily_history",
            PyValue::List(h.into_iter().map(PyValue::Map).collect()),
        ),
        Err(e) => return storage_500(&e),
    }
    ctx.insert("started", PyValue::Str(q.str_or_empty("started")));
    ctx.insert("error", PyValue::Str(q.str_or_empty("error")));
    render(&state, uri.path(), "ops.html", ctx)
}

async fn ops_run(State(state): State<AppState>, body: Bytes) -> Response<Body> {
    let form = MultiDict::from_form(&body);
    let name = form.get("op").unwrap_or_default().to_owned();
    let values: Vec<(String, String)> = form
        .items()
        .into_iter()
        .filter(|(k, _)| k != "op")
        .collect();
    let mut conn = match connect(&state).await {
        Ok(c) => c,
        Err(e) => return storage_500(&e),
    };
    match ops::start(&mut conn, &state, &name, &values).await {
        Ok(res) => redirect_303(&format!("/ops?started={}", res.name)),
        Err(ops::StartError::Refused(e)) => redirect_303(&format!("/ops?error={e}")),
        Err(ops::StartError::Storage(e)) => storage_500(&e),
    }
}

async fn ops_data(State(state): State<AppState>) -> Response<Body> {
    let mut conn = match connect(&state).await {
        Ok(c) => c,
        Err(e) => return storage_500(&e),
    };
    let running = match ops::running_job(&mut conn).await {
        Ok(r) => r.map_or(PyValue::None, PyValue::Map),
        Err(e) => return storage_500(&e),
    };
    let history = match ops::history(&mut conn, 25).await {
        Ok(h) => h,
        Err(e) => return storage_500(&e),
    };
    json(
        StatusCode::OK,
        &PyValue::Map(dict([
            ("running", running),
            ("history", history),
            ("operations", ops::operations_listing(&state.paths.root)),
        ])),
    )
}

/// pydantic's lax `int` from a path segment.
fn path_int(raw: &str) -> Option<i64> {
    crate::settings::py_int(raw)
}

async fn ops_job(
    State(state): State<AppState>,
    AxPath(job_id): AxPath<String>,
    uri: Uri,
) -> Response<Body> {
    let Some(id) = path_int(&job_id) else {
        return web::int_parsing("path", "job_id", &job_id);
    };
    let mut conn = match connect(&state).await {
        Ok(c) => c,
        Err(e) => return storage_500(&e),
    };
    match ops::job(&mut conn, id).await {
        Ok(Some(j)) => render(
            &state,
            uri.path(),
            "ops_job.html",
            dict([("j", PyValue::Map(j))]),
        ),
        Ok(None) => detail(StatusCode::NOT_FOUND, "Unknown job"),
        Err(e) => storage_500(&e),
    }
}
