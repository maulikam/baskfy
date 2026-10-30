//! `app/analytics/regime_store.py`: persistence for regime decisions, exposure reconciliation
//! and book snapshots — the **reads**, plus `record_exposure`.
//!
//! Two separate facts, two tables: `regime_evaluations` (what the market said) and
//! `regime_exposure` (what the portfolio actually is). `run_id='canonical'` is reserved for the
//! one committed record per `evaluation_id`.
//!
//! The writers take the engine's [`RegimeState`]: [`save_preview`] (a non-binding preview under
//! its own `run_id`), [`commit_evaluation`] (the canonical weekly decision, **refused under
//! `DRY_RUN`** and outside propose/enforce — defence in depth, the rule never depends on a caller
//! remembering it) and [`save_book_snapshot`]. [`previous_regime`] rebuilds the engine's
//! `previous` input from the last **committed** decision only, so a preview can never move the
//! live policy baseline.

use chrono::NaiveDate;

use super::regime::engine::{
    BookWeights, ExecutionStatus, PreviousRegime, RegimeMode, RegimeState, RegimeTier, StrMap,
    WeightSource, to_py, weights_map,
};
use crate::storage::{Conn, DbValue, Param, StorageError};
use crate::wire::{PyDict, PyValue};

use super::web::row_dict;

/// `CANONICAL`.
pub const CANONICAL: &str = "canonical";

async fn one(conn: &mut Conn, sql: &str, args: &[Param]) -> Result<Option<PyDict>, StorageError> {
    let cur = conn.execute(sql, args).await?;
    Ok(cur.fetchone().map(row_dict))
}

async fn all(conn: &mut Conn, sql: &str, args: &[Param]) -> Result<Vec<PyDict>, StorageError> {
    let cur = conn.execute(sql, args).await?;
    Ok(cur.rows.iter().map(row_dict).collect())
}

/// `get_canonical(conn, evaluation_id)`.
///
/// # Errors
/// The driver's.
pub async fn get_canonical(
    conn: &mut Conn,
    evaluation_id: &str,
) -> Result<Option<PyDict>, StorageError> {
    one(
        conn,
        "SELECT * FROM regime_evaluations WHERE evaluation_id=? AND run_id=?",
        &[evaluation_id.into(), CANONICAL.into()],
    )
    .await
}

/// `previews_for(conn, evaluation_id)`.
///
/// # Errors
/// The driver's.
pub async fn previews_for(
    conn: &mut Conn,
    evaluation_id: &str,
) -> Result<Vec<PyDict>, StorageError> {
    all(
        conn,
        "SELECT * FROM regime_evaluations WHERE evaluation_id=? AND run_id<>? ORDER BY created_at",
        &[evaluation_id.into(), CANONICAL.into()],
    )
    .await
}

/// `latest_committed(conn)`.
///
/// # Errors
/// The driver's.
pub async fn latest_committed(conn: &mut Conn) -> Result<Option<PyDict>, StorageError> {
    one(
        conn,
        "SELECT * FROM regime_evaluations WHERE run_id=? ORDER BY scheduled_week_end DESC, id DESC LIMIT 1",
        &[CANONICAL.into()],
    )
    .await
}

/// `latest_evaluation(conn)`: the most recent evaluation of any kind.
///
/// # Errors
/// The driver's.
pub async fn latest_evaluation(conn: &mut Conn) -> Result<Option<PyDict>, StorageError> {
    one(
        conn,
        "SELECT * FROM regime_evaluations ORDER BY scheduled_week_end DESC, id DESC LIMIT 1",
        &[],
    )
    .await
}

fn truthy(v: Option<&PyValue>) -> bool {
    match v {
        None | Some(PyValue::None) => false,
        Some(PyValue::Bool(b)) => *b,
        Some(PyValue::Int(i)) => *i != 0,
        Some(PyValue::Float(f)) => *f != 0.0,
        Some(PyValue::Str(s)) => !s.is_empty(),
        Some(PyValue::List(l)) => !l.is_empty(),
        Some(PyValue::Map(m)) => !m.is_empty(),
        Some(_) => true,
    }
}

/// `"RECOVERY_OVERRIDE_UNCONFIRMED" in json.loads(r["reason_codes_json"])`, raising where Python
/// raises: `json.loads` of NULL or of text that is not JSON, and `in` over a number or a bool.
/// `in` over a JSON string is a substring test and over an object a key test, as in Python.
/// `build` turns the error into `recovery_confirmations = None`, exactly as its `except` does.
fn holds_recovery_code(v: Option<&PyValue>) -> Result<bool, StorageError> {
    const CODE: &str = "RECOVERY_OVERRIDE_UNCONFIRMED";
    let text = match v {
        Some(PyValue::Str(s)) => s.clone(),
        _ => {
            return Err(StorageError::Other(
                "TypeError: the JSON object must be str, bytes or bytearray, not NoneType"
                    .to_owned(),
            ));
        }
    };
    match PyValue::from_json(text.as_bytes()) {
        Ok(PyValue::List(items)) => Ok(items
            .iter()
            .any(|i| matches!(i, PyValue::Str(s) if s == CODE))),
        Ok(PyValue::Str(s)) => Ok(s.contains(CODE)),
        Ok(PyValue::Map(m)) => Ok(m.get(CODE).is_some()),
        Ok(PyValue::None) => Err(StorageError::Other(
            "TypeError: argument of type 'NoneType' is not iterable".to_owned(),
        )),
        Ok(_) => Err(StorageError::Other(
            "TypeError: argument of type is not iterable".to_owned(),
        )),
        Err(e) => Err(StorageError::Other(format!("JSONDecodeError: {e}"))),
    }
}

/// `count_recovery_confirmations(conn)`: consecutive most-recent committed evaluations whose
/// recovery conditions held.
///
/// # Errors
/// The driver's.
pub async fn count_recovery_confirmations(conn: &mut Conn) -> Result<i64, StorageError> {
    let rows = all(
        conn,
        "SELECT override_active, reason_codes_json FROM regime_evaluations WHERE run_id=? ORDER BY scheduled_week_end DESC",
        &[CANONICAL.into()],
    )
    .await?;
    let mut n = 0;
    for r in rows {
        // Python decodes before it tests `override_active`, so a bad row raises either way.
        let has_code = holds_recovery_code(r.get("reason_codes_json"))?;
        if truthy(r.get("override_active")) || has_code {
            n += 1;
        } else {
            break;
        }
    }
    Ok(n)
}

/// `committed_history(conn)`.
///
/// # Errors
/// The driver's.
pub async fn committed_history(conn: &mut Conn) -> Result<Vec<PyDict>, StorageError> {
    all(
        conn,
        "SELECT * FROM regime_evaluations WHERE run_id=? ORDER BY scheduled_week_end",
        &[CANONICAL.into()],
    )
    .await
}

/// `record_exposure(...)`: append (or update in place) one exposure observation.
///
/// # Errors
/// The driver's.
#[allow(clippy::too_many_arguments)]
pub async fn record_exposure(
    conn: &mut Conn,
    evaluation_id: &str,
    actual_equity_pct: f64,
    target_equity_cap_pct: f64,
    pending_buy_pct: f64,
    pending_sell_pct: f64,
    execution_status: &str,
    observed_at: &str,
    note: Option<&str>,
) -> Result<PyDict, StorageError> {
    let gap = crate::lanes::weekly::pandas::py_round(actual_equity_pct - target_equity_cap_pct, 6);
    let args = vec![
        Param::from(evaluation_id),
        Param::from(observed_at),
        Param::from(actual_equity_pct),
        Param::from(target_equity_cap_pct),
        Param::from(gap),
        Param::from(pending_buy_pct),
        Param::from(pending_sell_pct),
        Param::from(execution_status),
        Param::from(note.map(str::to_owned)),
    ];
    conn.transaction(|c| {
        Box::pin(async move {
            c.execute(
                "INSERT INTO regime_exposure(evaluation_id, observed_at, actual_equity_pct, \
                 target_equity_cap_pct, exposure_gap_pct, pending_buy_pct, pending_sell_pct, \
                 execution_status, note) VALUES(?,?,?,?,?,?,?,?,?) \
                 ON CONFLICT(evaluation_id, observed_at) DO UPDATE SET \
                 actual_equity_pct=excluded.actual_equity_pct, \
                 target_equity_cap_pct=excluded.target_equity_cap_pct, \
                 exposure_gap_pct=excluded.exposure_gap_pct, \
                 pending_buy_pct=excluded.pending_buy_pct, \
                 pending_sell_pct=excluded.pending_sell_pct, \
                 execution_status=excluded.execution_status, note=excluded.note",
                &args,
            )
            .await?;
            Ok(())
        })
    })
    .await?;
    Ok(crate::wire::dict([
        ("evaluation_id", PyValue::Str(evaluation_id.to_owned())),
        ("observed_at", PyValue::Str(observed_at.to_owned())),
        ("exposure_gap_pct", PyValue::Float(gap)),
        (
            "execution_status",
            PyValue::Str(execution_status.to_owned()),
        ),
    ]))
}

/// `latest_exposure(conn, evaluation_id=None)`.
///
/// # Errors
/// The driver's.
pub async fn latest_exposure(
    conn: &mut Conn,
    evaluation_id: Option<&str>,
) -> Result<Option<PyDict>, StorageError> {
    match evaluation_id.filter(|e| !e.is_empty()) {
        Some(e) => {
            one(
                conn,
                "SELECT * FROM regime_exposure WHERE evaluation_id=? ORDER BY observed_at DESC, id DESC LIMIT 1",
                &[e.into()],
            )
            .await
        }
        None => {
            one(
                conn,
                "SELECT * FROM regime_exposure ORDER BY observed_at DESC, id DESC LIMIT 1",
                &[],
            )
            .await
        }
    }
}

/// `open_exposure_gap(conn, tolerance_pct)`: the latest observation still above its cap.
///
/// # Errors
/// The driver's.
pub async fn open_exposure_gap(
    conn: &mut Conn,
    tolerance_pct: f64,
) -> Result<Option<PyDict>, StorageError> {
    let row = latest_exposure(conn, None).await?;
    Ok(row.filter(|r| {
        r.get("exposure_gap_pct")
            .and_then(|v| match v {
                PyValue::Float(f) => Some(*f),
                #[allow(clippy::cast_precision_loss)]
                PyValue::Int(i) => Some(*i as f64),
                _ => None,
            })
            .is_some_and(|g| g > tolerance_pct)
    }))
}

/// `exposure_history(conn, evaluation_id)`.
///
/// # Errors
/// The driver's.
pub async fn exposure_history(
    conn: &mut Conn,
    evaluation_id: &str,
) -> Result<Vec<PyDict>, StorageError> {
    all(
        conn,
        "SELECT * FROM regime_exposure WHERE evaluation_id=? ORDER BY observed_at",
        &[evaluation_id.into()],
    )
    .await
}

/// `last_r1_snapshot(conn)`'s row (weights, source, coverage), or `None`.
///
/// # Errors
/// The driver's.
pub async fn last_r1_snapshot(conn: &mut Conn) -> Result<Option<PyDict>, StorageError> {
    one(
        conn,
        "SELECT * FROM regime_book_snapshots WHERE policy_tier='R1' ORDER BY scheduled_week_end DESC LIMIT 1",
        &[],
    )
    .await
}

/// `DbValue` → `bool(value)` (used by readers of the stored ints).
#[must_use]
pub fn db_truthy(v: Option<&DbValue>) -> bool {
    match v {
        None | Some(DbValue::Null) => false,
        Some(DbValue::Int(i)) => *i != 0,
        Some(DbValue::Bool(b)) => *b,
        Some(DbValue::Float(f)) => *f != 0.0,
        Some(DbValue::Text(s) | DbValue::Json(s)) => !s.is_empty(),
        Some(_) => true,
    }
}

/// Why a write was refused.
#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    /// `DryRunCommitError`: a policy tier may not be committed under `DRY_RUN` (or in observe).
    #[error("{0}")]
    DryRunCommit(String),
    /// `AlreadyCommittedError`: the week's canonical decision already exists.
    #[error("{0}")]
    AlreadyCommitted(String),
    /// `ValueError` (the reserved `run_id`).
    #[error("{0}")]
    Value(String),
    /// The driver's.
    #[error(transparent)]
    Storage(#[from] StorageError),
}

fn json_str(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7f => {
                let mut buf = [0_u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// `json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)` — the store's `_j`.
#[must_use]
pub fn dumps_sorted(value: &PyValue) -> String {
    match value {
        PyValue::None => "null".to_owned(),
        PyValue::Bool(true) => "true".to_owned(),
        PyValue::Bool(false) => "false".to_owned(),
        PyValue::Int(i) => i.to_string(),
        PyValue::Float(f) => {
            if f.is_nan() {
                "NaN".to_owned()
            } else if f.is_infinite() {
                if *f > 0.0 { "Infinity" } else { "-Infinity" }.to_owned()
            } else {
                baskfy_core::py::float_repr(*f)
            }
        }
        PyValue::Str(s) => json_str(s),
        PyValue::List(items) => {
            let inner: Vec<String> = items.iter().map(dumps_sorted).collect();
            format!("[{}]", inner.join(","))
        }
        PyValue::Map(m) => {
            let mut pairs: Vec<(&str, &PyValue)> = m.iter().collect();
            pairs.sort_by(|a, b| a.0.cmp(b.0));
            let inner: Vec<String> = pairs
                .iter()
                .map(|(k, v)| format!("{}:{}", json_str(k), dumps_sorted(v)))
                .collect();
            format!("{{{}}}", inner.join(","))
        }
        other => json_str(&other.py_str()),
    }
}

fn iso(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

fn opt_f(x: Option<f64>) -> Param {
    Param::from(x)
}

/// `_insert(conn, state, run_id=, dry_run=, mode=, committed=, input_snapshot=)`: one row; its id.
#[allow(clippy::too_many_arguments)]
async fn insert(
    conn: &mut Conn,
    state: &RegimeState,
    run_id: &str,
    dry_run: bool,
    mode: RegimeMode,
    committed: bool,
    input_snapshot: Option<&PyDict>,
    now: &str,
) -> Result<i64, StorageError> {
    let mut snapshot = input_snapshot.cloned().unwrap_or_default();
    let mut setdefault = |k: &str, v: PyValue| {
        if snapshot.get(k).is_none() {
            snapshot.insert(k, v);
        }
    };
    setdefault("index_diagnostics", to_py(&state.index_diagnostics));
    let mut health = PyDict::new();
    for (k, v) in &state.health {
        health.insert(k.to_string(), PyValue::Float(*v));
    }
    setdefault("health", PyValue::Map(health));
    setdefault(
        "bearish_stack_weight",
        PyValue::Float(state.bearish_stack_weight),
    );
    setdefault(
        "breadth_pct",
        state.breadth_pct.map_or(PyValue::None, PyValue::Float),
    );
    setdefault(
        "breadth_coverage_pct",
        state
            .breadth_coverage_pct
            .map_or(PyValue::None, PyValue::Float),
    );
    let strs = |v: &[String]| PyValue::List(v.iter().cloned().map(PyValue::Str).collect());
    let args: Vec<Param> = vec![
        state.evaluation_id.as_str().into(),
        run_id.into(),
        Param::from(i64::from(committed)),
        Param::from(i64::from(dry_run)),
        mode.as_str().into(),
        iso(state.scheduled_week_end).into(),
        iso(state.signal_session_date).into(),
        iso(state.as_of_date).into(),
        state.raw_candidate_tier.as_str().into(),
        Param::from(state.previous_policy_tier.map(|t| t.as_str().to_owned())),
        state.policy_tier.as_str().into(),
        Param::from(i64::from(state.transition_limited)),
        state.new_buys.as_str().into(),
        state.forced_action.as_str().into(),
        Param::from(i64::from(state.override_active)),
        Param::from(i64::from(state.data_stale)),
        Param::from(i64::from(state.manual_action_required)),
        opt_f(state.breadth_pct),
        opt_f(state.breadth_coverage_pct),
        dumps_sorted(&PyValue::Map(weights_map(&state.book_category_weights))).into(),
        state.book_weight_source.as_str().into(),
        dumps_sorted(&PyValue::Map(snapshot)).into(),
        dumps_sorted(&strs(&state.reason_codes)).into(),
        dumps_sorted(&strs(&state.reasons)).into(),
        iso(state.next_evaluation_date).into(),
        Param::from(state.last_transition_date.map(iso)),
        state.algorithm_version.as_str().into(),
        state.config_hash.as_str().into(),
        now.into(),
    ];
    let cur = conn
        .execute(
            "INSERT INTO regime_evaluations(evaluation_id, run_id, committed, dry_run, mode, \
             scheduled_week_end, signal_session_date, data_as_of, raw_candidate_tier, \
             previous_policy_tier, policy_tier, transition_limited, new_buys, forced_action, \
             override_active, data_stale, manual_action_required, breadth_pct, \
             breadth_coverage_pct, book_weights_json, book_weight_source, input_snapshot_json, \
             reason_codes_json, reasons_json, next_evaluation_date, last_transition_date, \
             algorithm_version, config_hash, created_at) \
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
            &args,
        )
        .await?;
    Ok(cur
        .fetchone()
        .and_then(|r| r.get("id"))
        .and_then(DbValue::as_i64)
        .unwrap_or(0))
}

/// `save_preview(conn, state, mode=, dry_run=True, run_id=None, input_snapshot=None)`: persist a
/// non-binding preview; never touches the canonical record. `preview_hex` is
/// `uuid.uuid4().hex`, of which the first 12 digits name the run.
///
/// # Errors
/// [`StoreError::Value`] for the reserved `run_id`; the driver's.
#[allow(clippy::too_many_arguments)]
pub async fn save_preview(
    conn: &mut Conn,
    state: &RegimeState,
    mode: RegimeMode,
    dry_run: bool,
    run_id: Option<&str>,
    input_snapshot: Option<&PyDict>,
    preview_hex: &str,
    now: &str,
) -> Result<String, StoreError> {
    let rid = match run_id.filter(|r| !r.is_empty()) {
        Some(r) => r.to_owned(),
        None => format!("preview-{}", &preview_hex[..preview_hex.len().min(12)]),
    };
    if rid == CANONICAL {
        return Err(StoreError::Value(
            "run_id 'canonical' is reserved for committed decisions".to_owned(),
        ));
    }
    let st = state.clone();
    let snap = input_snapshot.cloned();
    let rid2 = rid.clone();
    let now = now.to_owned();
    conn.transaction(move |c| {
        Box::pin(async move {
            insert(c, &st, &rid2, dry_run, mode, false, snap.as_ref(), &now).await?;
            Ok(())
        })
    })
    .await?;
    Ok(rid)
}

/// `commit_evaluation(conn, state, mode=, dry_run=, input_snapshot=None)`: the canonical weekly
/// decision, exactly once per `evaluation_id`. **Refuses under `DRY_RUN`** — committing a tier
/// transition is one of the three things `DRY_RUN` must never do.
///
/// # Errors
/// [`StoreError::DryRunCommit`] under `DRY_RUN` or outside propose/enforce;
/// [`StoreError::AlreadyCommitted`] for a second commit of the week; the driver's.
pub async fn commit_evaluation(
    conn: &mut Conn,
    state: &RegimeState,
    mode: RegimeMode,
    dry_run: bool,
    input_snapshot: Option<&PyDict>,
    now: &str,
) -> Result<&'static str, StoreError> {
    if dry_run {
        return Err(StoreError::DryRunCommit(format!(
            "DRY_RUN is active: refusing to commit policy tier {} for {}. Use save_preview() \
             instead.",
            state.policy_tier.as_str(),
            iso(state.scheduled_week_end)
        )));
    }
    if !matches!(mode, RegimeMode::Enforce | RegimeMode::Propose) {
        return Err(StoreError::DryRunCommit(format!(
            "mode={} does not commit policy tiers; use save_preview()",
            mode.as_str()
        )));
    }
    let st = state.clone();
    let snap = input_snapshot.cloned();
    let now2 = now.to_owned();
    let res = conn
        .transaction(move |c| {
            Box::pin(async move {
                insert(c, &st, CANONICAL, false, mode, true, snap.as_ref(), &now2).await?;
                Ok(())
            })
        })
        .await;
    match res {
        Ok(()) => Ok(CANONICAL),
        Err(StorageError::Driver(sqlx::Error::Database(e))) if e.is_unique_violation() => {
            Err(StoreError::AlreadyCommitted(format!(
                "evaluation {} ({}) is already committed — a week's policy transition happens \
                 once",
                state.evaluation_id,
                iso(state.scheduled_week_end)
            )))
        }
        Err(e) => Err(StoreError::Storage(e)),
    }
}

fn row_text(r: &PyDict, k: &str) -> Option<String> {
    match r.get(k) {
        Some(PyValue::Str(s)) => Some(s.clone()),
        _ => None,
    }
}

fn row_f64(r: &PyDict, k: &str) -> Option<f64> {
    match r.get(k) {
        Some(PyValue::Float(f)) => Some(*f),
        #[allow(clippy::cast_precision_loss)]
        Some(PyValue::Int(i)) => Some(*i as f64),
        _ => None,
    }
}

fn weights_from_json(text: &str) -> Result<Vec<(String, f64)>, StorageError> {
    match PyValue::from_json(text.as_bytes()) {
        Ok(PyValue::Map(m)) => Ok(m
            .iter()
            .map(|(k, v)| {
                let x = match v {
                    PyValue::Float(f) => *f,
                    #[allow(clippy::cast_precision_loss)]
                    PyValue::Int(i) => *i as f64,
                    _ => f64::NAN,
                };
                (k.to_owned(), x)
            })
            .collect()),
        Ok(_) => Err(StorageError::Other(
            "TypeError: book weights are not a dict".to_owned(),
        )),
        Err(e) => Err(StorageError::Other(format!("JSONDecodeError: {e}"))),
    }
}

fn parse_date(text: &str) -> Result<NaiveDate, StorageError> {
    super::ops::parse_iso_date(text).ok_or_else(|| {
        StorageError::Other(format!("ValueError: Invalid isoformat string: '{text}'"))
    })
}

/// `previous_regime(conn, recovery_confirmations=None)`: the engine's `previous`, from the last
/// **committed** decision only — previews are invisible here.
///
/// # Errors
/// The driver's; a stored value Python raises on.
pub async fn previous_regime(
    conn: &mut Conn,
    recovery_confirmations: Option<i64>,
) -> Result<Option<PreviousRegime>, StorageError> {
    let Some(row) = latest_committed(conn).await? else {
        return Ok(None);
    };
    let weights = weights_from_json(&row_text(&row, "book_weights_json").unwrap_or_default())?;
    let source_text = row_text(&row, "book_weight_source").unwrap_or_default();
    let source = WeightSource::parse(&source_text).ok_or_else(|| {
        StorageError::Other(format!(
            "ValueError: '{source_text}' is not a valid WeightSource"
        ))
    })?;
    let coverage = row_f64(&row, "breadth_coverage_pct")
        .filter(|c| !super::regime::engine::feq(*c, 0.0))
        .unwrap_or(0.0);
    let tier_text = row_text(&row, "policy_tier").unwrap_or_default();
    let tier = RegimeTier::parse(&tier_text).ok_or_else(|| {
        StorageError::Other(format!(
            "ValueError: '{tier_text}' is not a valid RegimeTier"
        ))
    })?;
    let week = parse_date(&row_text(&row, "scheduled_week_end").unwrap_or_default())?;
    let last = match row_text(&row, "last_transition_date").filter(|s| !s.is_empty()) {
        Some(t) => Some(parse_date(&t)?),
        None => None,
    };
    let confirmations = match recovery_confirmations {
        Some(n) => n,
        None => count_recovery_confirmations(conn).await?,
    };
    Ok(Some(PreviousRegime {
        policy_tier: tier,
        scheduled_week_end: week,
        last_transition_date: last,
        recovery_confirmations: confirmations,
        book_weights: Some(BookWeights {
            weights: StrMap(weights),
            source,
            coverage_pct: coverage,
            reason_codes: Vec::new(),
        }),
    }))
}

/// `save_book_snapshot(conn, evaluation_id=, scheduled_week_end=, policy_tier=, weights=)`:
/// freeze the full-risk book composition for the evaluation week (upsert).
///
/// # Errors
/// The driver's.
pub async fn save_book_snapshot(
    conn: &mut Conn,
    evaluation_id: &str,
    scheduled_week_end: NaiveDate,
    policy_tier: RegimeTier,
    weights: &BookWeights,
    now: &str,
) -> Result<(), StorageError> {
    let args: Vec<Param> = vec![
        evaluation_id.into(),
        iso(scheduled_week_end).into(),
        policy_tier.as_str().into(),
        dumps_sorted(&PyValue::Map(weights_map(&weights.weights))).into(),
        weights.source.as_str().into(),
        Param::from(weights.coverage_pct),
        now.into(),
    ];
    conn.transaction(move |c| {
        Box::pin(async move {
            c.execute(
                "INSERT INTO regime_book_snapshots(evaluation_id, scheduled_week_end, \
                 policy_tier, weights_json, source, coverage_pct, created_at) \
                 VALUES(?,?,?,?,?,?,?) ON CONFLICT(evaluation_id) DO UPDATE SET \
                 weights_json=excluded.weights_json, source=excluded.source, \
                 coverage_pct=excluded.coverage_pct, policy_tier=excluded.policy_tier",
                &args,
            )
            .await?;
            Ok(())
        })
    })
    .await
}

/// `last_r1_snapshot(conn)` as the engine's [`BookWeights`]: the most recent **R1** full-risk
/// weights (a reduced book is never offered as next week's strategic weights).
///
/// # Errors
/// The driver's; a stored value Python raises on.
pub async fn last_r1_book_weights(conn: &mut Conn) -> Result<Option<BookWeights>, StorageError> {
    let Some(row) = last_r1_snapshot(conn).await? else {
        return Ok(None);
    };
    let weights = weights_from_json(&row_text(&row, "weights_json").unwrap_or_default())?;
    let source_text = row_text(&row, "source").unwrap_or_default();
    let source = WeightSource::parse(&source_text).ok_or_else(|| {
        StorageError::Other(format!(
            "ValueError: '{source_text}' is not a valid WeightSource"
        ))
    })?;
    Ok(Some(BookWeights {
        weights: StrMap(weights),
        source,
        coverage_pct: row_f64(&row, "coverage_pct").unwrap_or(f64::NAN),
        reason_codes: Vec::new(),
    }))
}

/// `record_exposure` with an [`ExecutionStatus`].
///
/// # Errors
/// The driver's.
#[allow(clippy::too_many_arguments)]
pub async fn record_exposure_status(
    conn: &mut Conn,
    evaluation_id: &str,
    actual_equity_pct: f64,
    target_equity_cap_pct: f64,
    pending_buy_pct: f64,
    pending_sell_pct: f64,
    execution_status: ExecutionStatus,
    observed_at: &str,
    note: Option<&str>,
) -> Result<PyDict, StorageError> {
    record_exposure(
        conn,
        evaluation_id,
        actual_equity_pct,
        target_equity_cap_pct,
        pending_buy_pct,
        pending_sell_pct,
        execution_status.as_str(),
        observed_at,
        note,
    )
    .await
}

#[cfg(test)]
mod tests {
    use super::holds_recovery_code;
    use crate::wire::PyValue;

    fn s(x: &str) -> PyValue {
        PyValue::Str(x.to_owned())
    }

    #[test]
    fn the_recovery_code_test_raises_where_python_raises() {
        // Python: `codes = json.loads(r["reason_codes_json"])` then `"..." in codes`.
        assert!(matches!(
            holds_recovery_code(Some(&s(r#"["RECOVERY_OVERRIDE_UNCONFIRMED"]"#))),
            Ok(true)
        ));
        assert!(matches!(
            holds_recovery_code(Some(&s(r#"["R2_DEFAULT"]"#))),
            Ok(false)
        ));
        assert!(matches!(holds_recovery_code(Some(&s("[]"))), Ok(false)));
        // A JSON string: `in` is a substring test.
        assert!(matches!(
            holds_recovery_code(Some(&s(r#""xRECOVERY_OVERRIDE_UNCONFIRMEDx""#))),
            Ok(true)
        ));
        // NULL, not-JSON, `null` and a number all raise, which `build` reports as None.
        assert!(holds_recovery_code(Some(&PyValue::None)).is_err());
        assert!(holds_recovery_code(None).is_err());
        assert!(holds_recovery_code(Some(&s("not json"))).is_err());
        assert!(holds_recovery_code(Some(&s("null"))).is_err());
        assert!(holds_recovery_code(Some(&s("3"))).is_err());
    }
}
