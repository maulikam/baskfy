//! `/tradebook*` (`main.py`): the Console tradebook import, corporate actions, and the lots
//! status built from stored rows and the latest snapshot (never a live Kite call).

use axum::Router;
use axum::body::{Body, Bytes};
use axum::extract::State;
use axum::http::{HeaderMap, Response, StatusCode, Uri, header};
use axum::routing::{get, post};

use super::get_only;
use crate::app::AppState;
use crate::lanes::analytics::analytics_views::corporate_actions as ca;
use crate::lanes::analytics::web::{self, MultiDict};
use crate::lanes::weekly::analyze::{Part, parse_multipart};
use crate::lanes::weekly::weekly_books::{pyconv, snapshot_db, tradebook as tb};
use crate::storage::{Conn, DbValue, StorageError};
use crate::wire::{PyDict, PyValue, dict};

/// The routes.
pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/tradebook", get_only(get(page)).post(upload))
        .route("/tradebook/corporate-action", post(record_action))
        .route("/tradebook/corporate-action/delete", post(delete_action))
        .route("/tradebook/data", get_only(get(data)))
}

fn content_type(headers: &HeaderMap) -> String {
    headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_owned()
}

/// Starlette's form: multipart parts, or urlencoded pairs (as text parts).
fn form_parts(headers: &HeaderMap, body: &[u8]) -> Vec<Part> {
    let ct = content_type(headers);
    let lower = ct.to_ascii_lowercase();
    if lower.starts_with("multipart/form-data") {
        return parse_multipart(&ct, body).unwrap_or_default();
    }
    if lower.starts_with("application/x-www-form-urlencoded") {
        return MultiDict::from_form(body)
            .0
            .into_iter()
            .map(|(name, value)| Part {
                name,
                filename: None,
                data: value.into_bytes(),
            })
            .collect();
    }
    Vec::new()
}

/// `Form(...)` fields: the last value of each text field.
fn text_field(parts: &[Part], name: &str) -> Option<String> {
    parts
        .iter()
        .rev()
        .find(|p| p.name == name && p.filename.is_none())
        .map(|p| String::from_utf8_lossy(&p.data).into_owned())
}

fn missing(names: &[&str]) -> Response<Body> {
    let items: Vec<PyValue> = names
        .iter()
        .map(|name| {
            PyValue::Map(dict([
                ("type", PyValue::Str("missing".to_owned())),
                (
                    "loc",
                    PyValue::List(vec![
                        PyValue::Str("body".to_owned()),
                        PyValue::Str((*name).to_owned()),
                    ]),
                ),
                ("msg", PyValue::Str("Field required".to_owned())),
                ("input", PyValue::None),
            ]))
        })
        .collect();
    web::json(
        StatusCode::UNPROCESSABLE_ENTITY,
        &PyValue::Map(dict([("detail", PyValue::List(items))])),
    )
}

/// The required fields in signature order, or FastAPI's 422 naming every absent one.
fn required(parts: &[Part], names: &[&'static str]) -> Result<Vec<String>, Box<Response<Body>>> {
    let values: Vec<Option<String>> = names.iter().map(|n| text_field(parts, n)).collect();
    let absent: Vec<&str> = names
        .iter()
        .zip(&values)
        .filter(|(_, v)| v.is_none())
        .map(|(n, _)| *n)
        .collect();
    if !absent.is_empty() {
        return Err(Box::new(missing(&absent)));
    }
    Ok(values.into_iter().flatten().collect())
}

/// `_snapshot_positions()`: the latest snapshot's non-excluded positions; any failure is `[]`.
async fn snapshot_positions(state: &AppState) -> Vec<PyDict> {
    async fn read(state: &AppState) -> Option<Vec<PyDict>> {
        let mut conn = state.db.connect().await.ok()?;
        conn.migrate().await.ok()?;
        let rows = snapshot_db::snapshot_series(&mut conn).await.ok()?;
        let date = match rows.last()?.get("date")? {
            DbValue::Text(s) => s.clone(),
            other => crate::lanes::weekly::weekly_books::db_to_py(other).py_str(),
        };
        let snap = snapshot_db::get_snapshot(&mut conn, &date).await.ok()??;
        let raw = match snap.get("holdings_json")? {
            DbValue::Text(s) | DbValue::Json(s) => s.clone(),
            _ => return None,
        };
        let PyValue::Map(doc) = PyValue::from_json(raw.as_bytes()).ok()? else {
            return None;
        };
        let positions = match doc.get("positions") {
            None => return Some(Vec::new()),
            Some(PyValue::List(l)) => l.clone(),
            Some(_) => return None,
        };
        let mut out = Vec::new();
        for p in positions {
            let PyValue::Map(m) = p else { return None };
            if !m.get("excluded").is_some_and(PyValue::truthy) {
                out.push(m);
            }
        }
        Some(out)
    }
    read(state).await.unwrap_or_default()
}

async fn open(state: &AppState) -> Result<Conn, StorageError> {
    let mut conn = state.db.connect().await?;
    conn.migrate().await?;
    Ok(conn)
}

/// `GET /tradebook?imported=&error=`.
async fn page(State(state): State<AppState>, uri: Uri) -> Response<Body> {
    let q = MultiDict::from_query(&uri);
    let positions = snapshot_positions(&state).await;
    let today = state.clock.now_ist().date_naive();
    let result = async {
        let mut conn = open(&state).await?;
        let st = tb::status(&mut conn, &positions, today).await?;
        let actions = ca::listing(&mut conn).await?;
        Ok::<_, StorageError>((st, actions))
    }
    .await;
    let (st, actions) = match result {
        Ok(v) => v,
        Err(e) => return web::storage_500(&e),
    };
    let fills = st.get("fills").cloned().unwrap_or(PyValue::None);
    web::render(
        &state,
        uri.path(),
        "tradebook.html",
        dict([
            ("s", PyValue::Map(st)),
            ("f", fills),
            (
                "actions",
                PyValue::List(actions.into_iter().map(PyValue::Map).collect()),
            ),
            ("imported", PyValue::Str(q.str_or_empty("imported"))),
            ("error", PyValue::Str(q.str_or_empty("error"))),
            ("have_snapshot", PyValue::Bool(!positions.is_empty())),
        ]),
    )
}

/// `GET /tradebook/data`.
async fn data(State(state): State<AppState>) -> Response<Body> {
    let positions = snapshot_positions(&state).await;
    let today = state.clock.now_ist().date_naive();
    let result = async {
        let mut conn = open(&state).await?;
        tb::status(&mut conn, &positions, today).await
    }
    .await;
    match result {
        Ok(st) => web::json(StatusCode::OK, &PyValue::Map(st)),
        Err(e) => web::storage_500(&e),
    }
}

fn not_an_upload(input: &str) -> Response<Body> {
    let item = PyValue::Map(dict([
        ("type", PyValue::Str("value_error".to_owned())),
        (
            "loc",
            PyValue::List(vec![
                PyValue::Str("body".to_owned()),
                PyValue::Str("file".to_owned()),
            ]),
        ),
        (
            "msg",
            PyValue::Str("Value error, Expected UploadFile, received: <class 'str'>".to_owned()),
        ),
        ("input", PyValue::Str(input.to_owned())),
        (
            "ctx",
            PyValue::Map(dict([("error", PyValue::Map(PyDict::new()))])),
        ),
    ]));
    web::json(
        StatusCode::UNPROCESSABLE_ENTITY,
        &PyValue::Map(dict([("detail", PyValue::List(vec![item]))])),
    )
}

fn s(v: Option<&PyValue>) -> String {
    v.map_or_else(|| "None".to_owned(), PyValue::py_str)
}

/// `POST /tradebook` (`file: UploadFile`): store the export under `data/uploads/`, import it,
/// rebuild the lots it covers.
async fn upload(State(state): State<AppState>, headers: HeaderMap, body: Bytes) -> Response<Body> {
    let parts = form_parts(&headers, &body);
    let Some(file) = parts.iter().rev().find(|p| p.name == "file") else {
        return missing(&["file"]);
    };
    if file.filename.is_none() {
        return not_an_upload(&String::from_utf8_lossy(&file.data));
    }
    let dir = state.paths.resolve("data/uploads");
    if let Err(e) = std::fs::create_dir_all(&dir) {
        return web::internal_error(&e.to_string());
    }
    #[allow(clippy::cast_possible_truncation)]
    let stamp = state.clock.time().trunc() as i64;
    let dest = dir.join(format!("tradebook_{stamp}.csv"));
    if let Err(e) = std::fs::write(&dest, &file.data) {
        return web::internal_error(&e.to_string());
    }
    let now = state.clock.now_ist().naive_local();
    let result = async {
        let mut conn = open(&state).await.map_err(tb::ImportError::from)?;
        tb::import_tradebook(&mut conn, &dest, true, now).await
    }
    .await;
    match result {
        Ok(res) => {
            let mut msg = format!(
                "{} fills, {} symbols, {} open lots, {} closed ({} to {})",
                s(res.get("fills")),
                s(res.get("symbols")),
                s(res.get("open_lots")),
                s(res.get("closed_trades")),
                s(res.get("first_trade")),
                s(res.get("last_trade")),
            );
            if let Some(PyValue::List(u)) = res.get("unmatched_sells")
                && !u.is_empty()
            {
                msg.push_str(&format!(" — {} unmatched sells", u.len()));
            }
            web::redirect_303(&format!("/tradebook?imported={msg}"))
        }
        Err(tb::ImportError::Tradebook(e)) => web::redirect_303(&format!("/tradebook?error={e}")),
        Err(e) => web::internal_error(&e.to_string()),
    }
}

/// `POST /tradebook/corporate-action`: record a bonus or split, then rebuild that symbol's lots.
async fn record_action(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Response<Body> {
    let parts = form_parts(&headers, &body);
    let fields = match required(
        &parts,
        &["symbol", "kind", "ex_date", "ratio_new", "ratio_old"],
    ) {
        Ok(f) => f,
        Err(r) => return *r,
    };
    let note = text_field(&parts, "note").unwrap_or_default();
    let today = state.clock.now_ist().date_naive();
    let action = match ca::parse(
        &fields[0], &fields[1], &fields[2], &fields[3], &fields[4], &note, today,
    ) {
        Ok(a) => a,
        Err(e) => return web::redirect_303(&format!("/tradebook?error={e}")),
    };
    let now_iso = web::now_iso_seconds(&state);
    let result = async {
        let mut conn = open(&state).await?;
        ca::record(&mut conn, &action, &now_iso).await?;
        tb::rebuild_symbols(&mut conn, std::slice::from_ref(&action.symbol)).await
    }
    .await;
    match result {
        Ok(res) => web::redirect_303(&format!(
            "/tradebook?imported={}: {} · rebuilt to {} open lots",
            action.symbol,
            action.describe(),
            res.open_lots
        )),
        Err(e) => web::storage_500(&e),
    }
}

/// `POST /tradebook/corporate-action/delete`: remove one, rebuild the symbol (derived, so it
/// self-heals).
async fn delete_action(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Response<Body> {
    let parts = form_parts(&headers, &body);
    let fields = match required(&parts, &["action_id", "symbol"]) {
        Ok(f) => f,
        Err(r) => return *r,
    };
    let action_id = match pyconv::int(&PyValue::Str(fields[0].clone())) {
        Ok(i) => i,
        Err(e) => return web::internal_error(&e),
    };
    let symbol = fields[1].clone();
    let result = async {
        let mut conn = open(&state).await?;
        ca::remove(&mut conn, action_id).await?;
        tb::rebuild_symbols(&mut conn, std::slice::from_ref(&symbol)).await
    }
    .await;
    match result {
        Ok(_) => web::redirect_303(&format!(
            "/tradebook?imported=removed, {symbol} lots rebuilt"
        )),
        Err(e) => web::storage_500(&e),
    }
}
