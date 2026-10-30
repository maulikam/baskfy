//! The quote source **Scan now** reads during a session: Python's `build_kite_provider(
//! get_provider_settings(), provider_retry_hooks(), lane=KiteLane.INTERACTIVE)`, built only on
//! the provisional path (`twt_scan_task.quote_source`), so a scan outside market hours never
//! touches Kite.
//!
//! Two seams the harness needs (DECISIONS-RS R8.3): the REST root is
//! [`TaskContext::kite_root`] and the token store reads the task's clock. (The `swing_live` lane
//! builds the same source in its own private module; a lane does not reach into another's.)
//!
//! **Law 2.** [`KiteProvider`] carries reads only; nothing here can reach an order or GTT verb.

use std::sync::Arc;

use baskfy_providers::factory::{KiteLane, KiteReadLimiter, RedisSource, build_kite_read_limiter};
use baskfy_providers::kite::{
    ClientFactory, DEFAULT_ROOT_URI, KiteClient, KiteConnect, KiteProvider, KiteRuntime,
};
use baskfy_providers::settings::ProviderSettings;
use baskfy_providers::tokens::AccessTokenStore;

use crate::error::WorkerError;
use crate::tasks::TaskContext;

/// `build_kite_provider(..., lane=KiteLane.INTERACTIVE)`.
///
/// # Errors
/// A provider setting that does not parse, or a limiter that cannot be built.
pub(crate) async fn interactive_kite(ctx: &TaskContext) -> Result<KiteProvider, WorkerError> {
    let settings =
        ProviderSettings::from_env().map_err(|e| WorkerError::Settings(e.to_string()))?;
    let limiter = build_kite_read_limiter(
        &RedisSource::from_settings(&settings),
        &settings,
        KiteLane::Interactive,
    )
    .await
    .map_err(WorkerError::Provider)?;
    let root = ctx.kite_root().unwrap_or(DEFAULT_ROOT_URI).to_owned();
    let timeout = settings.kite_request_timeout_seconds;
    let factory: ClientFactory = Arc::new(move |api_key: &str| {
        let client: Arc<dyn KiteClient> =
            Arc::new(KiteConnect::with_root(api_key, &root, Some(timeout))?);
        Ok(client)
    });
    let store = AccessTokenStore::with_clock(
        settings.kite_token_path.clone(),
        settings.kite_token_encryption_key.clone(),
        ctx.clock(),
    );
    KiteProvider::new(
        settings,
        KiteRuntime {
            rate_limiter: limiter.map(KiteReadLimiter::into_limiter),
            client_factory: Some(factory),
            token_store: Some(Arc::new(store)),
            ..KiteRuntime::default()
        },
    )
    .map_err(WorkerError::Provider)
}
