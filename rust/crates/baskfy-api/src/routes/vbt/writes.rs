//! The volume-breakout sleeve's money-free writes (lane writes-sleeves): `POST /vbt/scan` (VB12)
//! and `PATCH /vbt/config` — ports of `routers/vbt.py`'s two write handlers,
//! `vbt_scan.request_scan` and `vbt_settings.apply_patch`.
//!
//! A scan inserts one `vb_scan_run` row (`source = 'desk'`, `detail = {"source": "web"}`) and
//! publishes the worker's re-detect; the settings patch writes four numbers under server
//! ceilings. The DRY_RUN counter and the first-live countdown are not fields of the model, so a
//! caller cannot even name them. No order path, no broker (law 2).

use std::cmp::Ordering;

use axum::extract::{Extension, Request, State};
use axum::response::Response;
use rust_decimal::Decimal;
use sqlx::Row;

use crate::auth::{Principal, require_authenticated};
use crate::body::receive;
use crate::problems::{Problem, not_found, setting_above_ceiling};
use crate::routes::sleeves::common::{db, scoped_sole_user_id};
use crate::routes::sleeves::pyd::{
    Bound, Default as D, Model, PV, PyDec, Ty, dec_const, validate_body,
};
use crate::routes::sleeves::writes::{
    ScanSpec, Stored, has_queue, now_micros, pool, publish_after_commit, queued_response,
    read_body, request_scan, write_changes,
};
use crate::session::{self, WritePool};
use crate::state::AppState;

/// `vbt_scan.SCAN_TASK_NAME`.
pub const SCAN_TASK_NAME: &str = "baskfy.vbt.rescan";

/// `POST /vbt/scan` (VB12): queue a re-detection of the last published session.
///
/// # Errors
/// 401, 404/503 (the tenant), 409 (a scan in flight), 429 (inside the minute), 500.
pub async fn post_vbt_scan(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let user_id = scoped_sole_user_id(&state, principal.user_id, None).await?;
    let pool = pool(write)?;
    let spec = ScanSpec {
        table: "vb_scan_run",
        insert_sql: "INSERT INTO vb_scan_run (user_id, requested_at, status, source, detail) \
                     VALUES ($1, $2, 'QUEUED', 'desk', '{\"source\": \"web\"}'::jsonb) RETURNING id",
        task: SCAN_TASK_NAME,
        min_interval_seconds: state.settings.vbt_scan_min_interval_seconds,
        stale_after_seconds: state.settings.vbt_scan_stale_after_seconds,
    };
    let (run_id, requested_at) = request_scan(&pool, user_id, spec).await?;
    if has_queue(&state.settings.redis_url) {
        publish_after_commit(
            &state.settings.redis_url,
            &pool,
            spec.table,
            spec.task,
            run_id,
        )
        .await;
    } else {
        crate::logging::warning(
            "baskfy_api.vbt_scan",
            &format!("vbt scan {run_id} queued with no broker; the worker's sweep will publish it"),
            &[],
        );
    }
    Ok(queued_response(run_id, requested_at))
}

/// `VbtConfigPatch`.
fn config_patch() -> Model {
    Model {
        fields: vec![
            (
                "sleeve_capital_inr",
                Ty::Dec(vec![Bound::Ge("0")]).opt(),
                D::Unset,
            ),
            (
                "max_open_positions",
                Ty::Int(vec![Bound::Gt("0")]).opt(),
                D::Unset,
            ),
            (
                "max_position_pct",
                Ty::Dec(vec![Bound::Gt("0"), Bound::Le("100")]).opt(),
                D::Unset,
            ),
            (
                "stop_pct",
                Ty::Dec(vec![Bound::Gt("0"), Bound::Le("100")]).opt(),
                D::Unset,
            ),
        ],
        forbid_extra: true,
        before: None,
        after: None,
    }
}

/// `VbtCeilings.check` over every change, in order.
fn check_ceilings(state: &AppState, changes: &[(String, PV)]) -> Result<(), Problem> {
    let s = &state.settings;
    for (field, value) in changes {
        let (ceiling, env): (PyDec, &str) = match field.as_str() {
            "max_open_positions" => (
                dec_const(Decimal::from(s.vbt_max_open_positions_max)),
                "BASKFY_VBT_MAX_OPEN_POSITIONS_MAX",
            ),
            "max_position_pct" => (
                dec_const(s.vbt_max_position_pct_max),
                "BASKFY_VBT_MAX_POSITION_PCT_MAX",
            ),
            "stop_pct" => (dec_const(s.vbt_stop_pct_max), "BASKFY_VBT_STOP_PCT_MAX"),
            _ => continue,
        };
        let Some(v) = value.as_pydec() else { continue };
        if v.cmp_num(&ceiling) == Ordering::Greater {
            return Err(setting_above_ceiling(
                field,
                &value.py_str(),
                &ceiling.py_str(),
                env,
            ));
        }
    }
    Ok(())
}

/// `PATCH /vbt/config`: four numbers, audited, bounded by the server's ceilings.
///
/// # Errors
/// 400 (body), 401, 404 (tenant; not seeded), 422 (a ceiling), 503, 500.
pub async fn patch_vbt_config(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<WritePool>>,
    request: Request,
) -> Result<Response, Problem> {
    let (content_type, bytes) = read_body(request).await?;
    let received = receive(content_type.as_deref(), &bytes)?;
    require_authenticated(&principal)?;
    let patch =
        validate_body(&received, &config_patch()).map_err(|f| f.into_problem(Vec::new()))?;
    let user_id = scoped_sole_user_id(&state, principal.user_id, None).await?;
    let changes = patch.changes();
    let changed_by = format!("user:{user_id}");
    let now = now_micros();
    let pool = pool(write)?;
    let ceilings_state = state.clone();
    session::run(&pool, move |tx| {
        Box::pin(async move {
            let row = sqlx::query(
                "SELECT sleeve_capital_inr, max_open_positions::bigint AS max_open_positions, \
                        max_position_pct, stop_pct, updated_by \
                 FROM vb_config WHERE user_id = $1",
            )
            .bind(user_id)
            .fetch_optional(&mut **tx)
            .await
            .map_err(db)?;
            let Some(row) = row else {
                return Err(not_found(
                    "volume-breakout sleeve",
                    &format!("user:{user_id}"),
                ));
            };
            check_ceilings(&ceilings_state, &changes)?;
            let d = |c: &str| -> Result<Stored, Problem> {
                Ok(Stored::Dec(crate::routes::sleeves::common::dec(&row, c)?))
            };
            let stored = vec![
                ("sleeve_capital_inr".to_owned(), d("sleeve_capital_inr")?),
                (
                    "max_open_positions".to_owned(),
                    Stored::Int(row.try_get("max_open_positions").map_err(db)?),
                ),
                ("max_position_pct".to_owned(), d("max_position_pct")?),
                ("stop_pct".to_owned(), d("stop_pct")?),
            ];
            let updated_by: Option<String> = row.try_get("updated_by").map_err(db)?;
            let audits = write_changes(
                tx,
                "vb_config",
                "user_id = ?",
                &[user_id],
                &stored,
                updated_by.as_deref(),
                &changes,
                &changed_by,
                true,
            )
            .await?;
            for a in audits {
                sqlx::query(
                    "INSERT INTO vb_config_audit (user_id, key, old_value, new_value, changed_at, \
                     changed_by, note) VALUES ($1, $2, $3, $4, $5, $6, NULL)",
                )
                .bind(user_id)
                .bind(&a.key)
                .bind(&a.old_value)
                .bind(&a.new_value)
                .bind(now)
                .bind(&changed_by)
                .execute(&mut **tx)
                .await
                .map_err(db)?;
            }
            Ok(())
        })
    })
    .await?;
    super::config_response(&state, user_id).await
}
