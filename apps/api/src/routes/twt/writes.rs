//! The three-weeks-tight sleeve's one write (lane writes-sleeves): `POST /twt/scan` (TW12) —
//! a port of `routers/twt.post_twt_scan` and `twt_scan.request_scan`. One `tw_scan_run` row and
//! one worker task; a scan moves no money and reaches no broker (law 2).

use axum::extract::{Extension, State};
use axum::response::Response;

use crate::auth::{Principal, require_authenticated};
use crate::problems::Problem;
use crate::routes::sleeves::common::scoped_sole_user_id;
use crate::state::AppState;

/// `twt_scan.SCAN_TASK_NAME`.
pub const SCAN_TASK_NAME: &str = "baskfy.twt.scan";

/// `POST /twt/scan` (TW12): one `tw_scan_run` row (`source = 'web'`) and one task published.
/// A scan moves no money and reaches no broker.
///
/// # Errors
/// 401, 404/503 (the tenant), 409 (a scan in flight), 429 (inside the minute), 500.
pub async fn post_twt_scan(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    write: Option<Extension<crate::session::WritePool>>,
) -> Result<Response, Problem> {
    use crate::routes::sleeves::writes::{
        ScanSpec, has_queue, pool, publish_after_commit, queued_response, request_scan,
    };
    require_authenticated(&principal)?;
    let user_id = scoped_sole_user_id(&state, principal.user_id, None).await?;
    let pool = pool(write)?;
    let spec = ScanSpec {
        table: "tw_scan_run",
        insert_sql: "INSERT INTO tw_scan_run (user_id, requested_at, status, source) \
                     VALUES ($1, $2, 'QUEUED', 'web') RETURNING id",
        task: SCAN_TASK_NAME,
        min_interval_seconds: state.settings.twt_scan_min_interval_seconds,
        stale_after_seconds: state.settings.twt_scan_stale_after_seconds,
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
            "baskfy_api.twt_scan",
            &format!("twt scan {run_id} queued with no broker; the worker's sweep will publish it"),
            &[],
        );
    }
    Ok(queued_response(run_id, requested_at))
}
