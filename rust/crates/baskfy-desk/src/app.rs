//! The axum app (DECISIONS-RS R8.1): `app/main.py`'s `FastAPI(...)` with its two middlewares,
//! assembled from the lanes' routers. **Lanes never edit this file**; each owns
//! `src/lanes/<lane>.rs` and exposes `routes()`, `register_templates()` and `run_service()`.
//!
//! Middleware order is Python's: `PathPrefix` outermost (it rewrites the final bytes), then
//! `DeskSecurity`, then the routes.
//!
//! **Law 2 in the types.** [`AppState`] carries an `Arc<dyn KiteReads>` and an
//! `Arc<dyn Gateway>`; the `KiteWrites` handle goes into `lanes::exec_core::gateway(...)` once,
//! at build, and nowhere else.
//!
//! **The harness hooks** (`--harness`, never in production): `POST /__harness/clock`
//! (`{"iso": "2026-10-30T09:16:00+05:30"}` or `{"advance": 60}`) moves the [`FixedClock`];
//! `GET /__harness/kite-calls` lists the replay Kite's recorded calls. `tools/parity/
//! desk_harness.py` adds the same two routes to the Python desk.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use axum::Router;
use axum::body::Body;
use axum::extract::State;
use axum::http::{HeaderValue, Response, StatusCode};
use axum::routing::{get, post};

use crate::clock::{Clock, FixedClock};
use crate::fake_kite::ReplayKite;
use crate::gateway::Gateway;
use crate::ids::IdSource;
use crate::kite::{KiteReads, KiteWrites};
use crate::plan::PlanStore;
use crate::settings::{Settings, SharedSettings};
use crate::storage::Db;
use crate::templates::Templates;
use crate::wire::{PyValue, dict, orjson_dumps};

/// Where the desk's files are: the Python desk's working directory (`kite-momentum-rebalancer`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeskPaths {
    /// The working directory every relative path (`data/…`, `DB_PATH`) resolves against.
    pub root: PathBuf,
    /// `app/templates`.
    pub templates: PathBuf,
    /// `app/static`.
    pub static_dir: PathBuf,
}

impl DeskPaths {
    /// The layout under `root`.
    #[must_use]
    pub fn under(root: &Path) -> Self {
        Self {
            root: root.to_path_buf(),
            templates: root.join("app").join("templates"),
            static_dir: root.join("app").join("static"),
        }
    }

    /// A path relative to the working directory, as Python's `open("data/…")` resolves it.
    #[must_use]
    pub fn resolve(&self, relative: &str) -> PathBuf {
        let p = Path::new(relative);
        if p.is_absolute() {
            p.to_path_buf()
        } else {
            self.root.join(p)
        }
    }
}

/// The harness's handles (present only under `--harness`).
#[derive(Debug, Clone)]
pub struct Harness {
    /// The settable clock both desks share a script for.
    pub clock: Arc<FixedClock>,
    /// The replay Kite, for `/__harness/kite-calls`.
    pub kite: Arc<ReplayKite>,
}

/// Everything a handler can reach.
#[derive(Clone)]
pub struct AppState {
    /// The settings, mutable at run time (the settings page; the gateway's live read).
    pub settings: SharedSettings,
    /// The clock.
    pub clock: Arc<dyn Clock>,
    /// `uuid4`.
    pub ids: Arc<dyn IdSource>,
    /// The desk's database.
    pub db: Db,
    /// Kite reads (no order verb: law 2).
    pub kite: Arc<dyn KiteReads>,
    /// The one path to an order.
    pub gateway: Arc<dyn Gateway>,
    /// `/analyze`'s plans.
    pub plans: Arc<PlanStore>,
    /// The page templates.
    pub templates: Arc<Templates>,
    /// The working-directory layout.
    pub paths: DeskPaths,
    /// The harness hooks, if any.
    pub harness: Option<Harness>,
}

impl std::fmt::Debug for AppState {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("AppState")
            .field("paths", &self.paths)
            .field("harness", &self.harness.is_some())
            .finish_non_exhaustive()
    }
}

impl AppState {
    /// A copy of the settings as they stand now.
    #[must_use]
    pub fn settings_snapshot(&self) -> Settings {
        match self.settings.read() {
            Ok(s) => s.clone(),
            Err(poisoned) => poisoned.into_inner().clone(),
        }
    }
}

/// The parts [`build_state`] assembles an [`AppState`] from.
pub struct StateParts {
    /// The settings.
    pub settings: Settings,
    /// The clock.
    pub clock: Arc<dyn Clock>,
    /// `uuid4`.
    pub ids: Arc<dyn IdSource>,
    /// The database.
    pub db: Db,
    /// The Kite client — reads and writes; the writes half goes to the gateway only.
    pub kite: Arc<dyn KiteWrites>,
    /// The layout.
    pub paths: DeskPaths,
    /// The harness hooks.
    pub harness: Option<Harness>,
}

/// Wire the state: templates with their globals, the gateway from `lanes::exec_core`.
#[must_use]
pub fn build_state(parts: StateParts) -> AppState {
    let StateParts {
        settings,
        clock,
        ids,
        db,
        kite,
        paths,
        harness,
    } = parts;
    let asset_v = crate::templates::asset_version(&paths.static_dir);
    let options_enabled = crate::lanes::options::lab_enabled(&settings);
    let mut templates = Templates::new(&paths.templates, &asset_v, options_enabled);
    crate::lanes::register_templates(&mut templates);
    let settings: SharedSettings = Arc::new(std::sync::RwLock::new(settings));
    let kite_for_reads = kite.clone();
    let gateway = crate::lanes::exec_core::gateway(crate::lanes::exec_core::GatewayParts {
        kite,
        settings: settings.clone(),
        clock: clock.clone(),
        ids: ids.clone(),
        journal_path: paths.resolve(crate::gateway::JOURNAL),
        db: db.clone(),
    });
    let reads = crate::lanes::exec_core::limited_reads(kite_for_reads, &gateway, &settings);
    AppState {
        settings,
        clock,
        ids,
        db,
        kite: reads,
        gateway,
        plans: Arc::new(PlanStore::default()),
        templates: Arc::new(templates),
        paths,
        harness,
    }
}

fn json(status: StatusCode, value: &PyValue) -> Response<Body> {
    let mut r = Response::new(Body::from(orjson_dumps(value).unwrap_or_default()));
    *r.status_mut() = status;
    r.headers_mut().insert(
        axum::http::header::CONTENT_TYPE,
        HeaderValue::from_static("application/json"),
    );
    r
}

async fn harness_clock(State(state): State<AppState>, body: axum::body::Bytes) -> Response<Body> {
    let Some(h) = &state.harness else {
        return json(
            StatusCode::NOT_FOUND,
            &PyValue::Map(dict([("detail", PyValue::Str("Not Found".to_owned()))])),
        );
    };
    let parsed: serde_json::Value = serde_json::from_slice(&body).unwrap_or_default();
    if let Some(iso) = parsed.get("iso").and_then(serde_json::Value::as_str) {
        match chrono::DateTime::parse_from_rfc3339(iso) {
            Ok(at) => h.clock.set(at),
            Err(e) => {
                return json(
                    StatusCode::BAD_REQUEST,
                    &PyValue::Map(dict([("detail", PyValue::Str(e.to_string()))])),
                );
            }
        }
    }
    if let Some(step) = parsed.get("advance").and_then(serde_json::Value::as_f64) {
        h.clock.advance(step);
    }
    let now = state.clock.now_ist().to_rfc3339();
    json(
        StatusCode::OK,
        &PyValue::Map(dict([("now", PyValue::Str(now))])),
    )
}

async fn harness_kite_calls(State(state): State<AppState>) -> Response<Body> {
    let Some(h) = &state.harness else {
        return json(
            StatusCode::NOT_FOUND,
            &PyValue::Map(dict([("detail", PyValue::Str("Not Found".to_owned()))])),
        );
    };
    let lines: Vec<PyValue> = h
        .kite
        .calls()
        .iter()
        .enumerate()
        .filter_map(|(i, c)| ReplayKite::log_line(i + 1, c).ok())
        .filter_map(|l| PyValue::from_json(l.as_bytes()).ok())
        .collect();
    json(StatusCode::OK, &PyValue::List(lines))
}

/// Starlette's 404: `{"detail":"Not Found"}`.
async fn not_found() -> Response<Body> {
    json(
        StatusCode::NOT_FOUND,
        &PyValue::Map(dict([("detail", PyValue::Str("Not Found".to_owned()))])),
    )
}

/// Starlette's 405 (the `Allow` header is the routing lane's to grade per route).
async fn method_not_allowed() -> Response<Body> {
    json(
        StatusCode::METHOD_NOT_ALLOWED,
        &PyValue::Map(dict([(
            "detail",
            PyValue::Str("Method Not Allowed".to_owned()),
        )])),
    )
}

/// The app: every lane's routes, the harness hooks when present, then `DeskSecurity`, then
/// `PathPrefix` outermost.
pub fn build_app(state: AppState) -> Router {
    let prefix = crate::path_prefix::normalise(&state.settings_snapshot().desk_path_prefix);
    let mut router = crate::lanes::routes();
    if state.harness.is_some() {
        router = router
            .route("/__harness/clock", post(harness_clock))
            .route("/__harness/kite-calls", get(harness_kite_calls));
    }
    router
        .fallback(not_found)
        .method_not_allowed_fallback(method_not_allowed)
        .layer(axum::middleware::from_fn_with_state(
            state.clone(),
            crate::websec::layer,
        ))
        .layer(axum::middleware::from_fn_with_state(
            prefix,
            crate::path_prefix::layer,
        ))
        .with_state(state)
}
