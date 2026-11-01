//! `GET /` (`main.py: home`): session state, collection health, stop coverage against the
//! broker, and the post-login collection's progress. Read-only; every part degrades to `None`.

use axum::Router;
use axum::body::Body;
use axum::extract::State;
use axum::http::{Response, Uri};
use axum::routing::get;

use super::get_only;
use crate::app::AppState;
use crate::lanes::analytics::analytics_views::protection;
use crate::lanes::analytics::web::{self, MultiDict};
use crate::lanes::analytics::{daily_runs, ops};
use crate::lanes::exec_core::exec_kite;
use crate::lanes::exec_core::stops::StopConfig;
use crate::wire::{PyDict, PyValue, dict};

/// The route.
pub fn routes() -> Router<AppState> {
    Router::new().route("/", get_only(get(home)))
}

#[allow(clippy::print_stderr)]
fn warn(line: &str) {
    eprintln!("WARNING:root:{line}");
}

async fn collection(state: &AppState) -> Option<PyDict> {
    let mut conn = state.db.connect().await.ok()?;
    conn.migrate().await.ok()?;
    daily_runs::status(&mut conn, state.clock.now_ist().date_naive())
        .await
        .ok()
}

/// `protection.from_kite(kite())`: two reads, no writes.
async fn coverage(state: &AppState) -> Result<PyDict, String> {
    let k = exec_kite::kite(state).map_err(|e| e.message)?;
    let holdings = k.holdings().await.map_err(|e| e.message)?;
    let gtts = k.get_gtts().await.map_err(|e| e.message)?;
    let held: Vec<PyValue> = holdings.into_iter().map(PyValue::Map).collect();
    Ok(protection::review(&held, &gtts, StopConfig::default()).to_py())
}

async fn autorun(state: &AppState, logged_in: &str, started: &str, note: &str) -> Option<PyDict> {
    let mut conn = state.db.connect().await.ok()?;
    let running = ops::running_job(&mut conn).await.ok()?;
    let last = ops::last_run(&mut conn)
        .await
        .ok()?
        .into_iter()
        .find(|(name, _)| name == "autorun")
        .map(|(_, row)| row);
    let ours = running.filter(|r| r.get("name").is_some_and(|n| n.py_str() == "autorun"));
    if logged_in.is_empty() && started.is_empty() && ours.is_none() && last.is_none() {
        return None;
    }
    Some(dict([
        ("just_logged_in", PyValue::Bool(!logged_in.is_empty())),
        ("note", PyValue::Str(note.to_owned())),
        ("running", ours.map_or(PyValue::None, PyValue::Map)),
        ("last", last.map_or(PyValue::None, PyValue::Map)),
    ]))
}

async fn home(State(state): State<AppState>, uri: Uri) -> Response<Body> {
    let q = MultiDict::from_query(&uri);
    let authed = match exec_kite::kite(&state) {
        Ok(k) => k.is_authed().await,
        Err(_) => false,
    };
    let collection = collection(&state).await;
    let protection = if authed {
        match coverage(&state).await {
            Ok(p) => Some(p),
            Err(e) => {
                warn(&format!("could not check stop coverage: {e}"));
                None
            }
        }
    } else {
        None
    };
    let auto = autorun(
        &state,
        &q.str_or_empty("logged_in"),
        &q.str_or_empty("autorun"),
        &q.str_or_empty("note"),
    )
    .await;
    let opt = |d: Option<PyDict>| d.map_or(PyValue::None, PyValue::Map);
    web::render(
        &state,
        uri.path(),
        "index.html",
        dict([
            ("authed", PyValue::Bool(authed)),
            ("dry_run", PyValue::Bool(state.settings_snapshot().dry_run)),
            ("collection", opt(collection)),
            ("auto", opt(auto)),
            ("protection", opt(protection)),
        ]),
    )
}
