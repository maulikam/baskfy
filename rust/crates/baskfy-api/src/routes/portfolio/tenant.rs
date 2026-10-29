//! Ports `baskfy_api.curated_tenant.scoped_sole_user_id` and
//! `baskfy_api.curated_seed.resolve_sole_user_id` — the sole-tenant guard.

use baskfy_db::OrderedJson;

use crate::problems::{Problem, ProblemType, pipeline_degraded};
use crate::state::AppState;

/// `SOLE_USER_ENV`.
pub(crate) const SOLE_USER_ENV: &str = "BASKFY_SOLE_USER_ID";

/// `baskfy_api.seed.E2E_PUBLIC_ID`.
pub(crate) const E2E_PUBLIC_ID: &str = "e2e000000001";

/// `SOLE_TENANT_REFUSED`.
pub(crate) const SOLE_TENANT_REFUSED: &str = "not-the-sole-tenant";

/// `resolve_sole_user_id`: `BASKFY_SOLE_USER_ID` (stripped; empty or non-numeric is unset), else
/// the seeded development account by public id, else a 503 naming the variable.
///
/// # Errors
/// 503 `pipeline-degraded` when no tenant is configured; 500 for a database failure.
pub(crate) async fn resolve_sole_user_id(state: &AppState) -> Result<i64, Problem> {
    let raw = std::env::var(SOLE_USER_ENV).unwrap_or_default();
    let raw = raw.trim();
    if !raw.is_empty() {
        if let Some(id) = super::params::python_int(raw).and_then(|v| i64::try_from(v).ok()) {
            return Ok(id);
        }
        crate::logging::warning(
            "baskfy_api.curated_seed",
            "BASKFY_SOLE_USER_ID is not a number; reading it as 'no sole tenant configured'",
            &[],
        );
    }
    let found: Option<i64> = sqlx::query_scalar("SELECT id FROM app_user WHERE public_id = $1")
        .bind(E2E_PUBLIC_ID)
        .fetch_optional(state.db.pool())
        .await
        .map_err(|e| crate::db_failure(&e.into()))?;
    found.ok_or_else(|| {
        pipeline_degraded(&format!(
            "The sole-tenant account is not configured on this deployment. Set \
             {SOLE_USER_ENV}, or run `make seed` to create the development account."
        ))
    })
}

/// `scoped_sole_user_id(session, principal_user_id, surface=...)`.
///
/// # Errors
/// As [`resolve_sole_user_id`], and a 404 with `reason: not-the-sole-tenant` for any other caller.
pub(crate) async fn scoped_sole_user_id(
    state: &AppState,
    principal_user_id: Option<i64>,
    surface: Option<&str>,
) -> Result<i64, Problem> {
    let sole = resolve_sole_user_id(state).await?;
    if let Some(user) = principal_user_id
        && user != sole
    {
        let tail = surface.map_or_else(
            || ".".to_owned(),
            |s| format!(", so {s} is not readable here."),
        );
        return Err(Problem::new(
            ProblemType::NotFound,
            format!(
                "This deployment serves a single account and this caller (user {user}) is not \
                 it{tail}"
            ),
        )
        .with("reason", OrderedJson::Str(SOLE_TENANT_REFUSED.to_owned())));
    }
    Ok(sole)
}
