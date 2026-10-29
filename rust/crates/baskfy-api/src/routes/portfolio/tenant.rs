//! Ports `baskfy_api.curated_tenant.scoped_sole_user_id` and
//! `baskfy_api.curated_seed.resolve_sole_user_id` — the sole-tenant guard, now held once in
//! [`crate::routes::common`] (R5.10's collapse); these keep the lane's `&AppState` signatures.

use crate::problems::Problem;
use crate::state::AppState;

/// `scoped_sole_user_id(session, principal_user_id, surface=...)`, as
/// [`crate::routes::common::scoped_sole_user_id`].
///
/// # Errors
/// As [`crate::routes::common::resolve_sole_tenant`], and a 404 with `reason: not-the-sole-tenant` for any other caller.
pub(crate) async fn scoped_sole_user_id(
    state: &AppState,
    principal_user_id: Option<i64>,
    surface: Option<&str>,
) -> Result<i64, Problem> {
    crate::routes::common::scoped_sole_user_id(&state.db, principal_user_id, surface).await
}
