//! The quote source the swing session jobs read: Python's `build_kite_provider(
//! get_provider_settings(), provider_retry_hooks(), lane=KiteLane.INTERACTIVE)`.
//!
//! The same [`KiteProvider`] the rest of the stack uses, on the shared Redis read limiter (the
//! interactive lane: a person pressed a button and is waiting), with two seams the harness needs
//! (DECISIONS-RS R8.3): the client's REST root is [`TaskContext::kite_root`] (the replay upstream
//! under the harness, Kite's own root otherwise) and the token store reads the task's clock, so
//! a token issued at the case's moment is not expired by the real date.
//!
//! **Law 2.** [`KiteProvider`] carries reads only (`quote`, `historical_data`); nothing here can
//! reach an order or a GTT verb.

use std::sync::Arc;

use baskfy_providers::factory::{KiteLane, RedisSource, build_kite_read_limiter};
use baskfy_providers::kite::{
    ClientFactory, DEFAULT_ROOT_URI, KiteClient, KiteConnect, KiteProvider, KiteRuntime,
};
use baskfy_providers::settings::ProviderSettings;
use baskfy_providers::tokens::AccessTokenStore;

use super::TaskContext;
use crate::error::WorkerError;

/// `build_kite_provider(get_provider_settings(), provider_retry_hooks(), lane=lane)`.
///
/// # Errors
/// A provider setting that does not parse, or a limiter that cannot be built.
pub(super) async fn kite_provider(
    ctx: &TaskContext,
    lane: KiteLane,
) -> Result<KiteProvider, WorkerError> {
    let settings =
        ProviderSettings::from_env().map_err(|e| WorkerError::Settings(e.to_string()))?;
    let limiter = build_kite_read_limiter(&RedisSource::from_settings(&settings), &settings, lane)
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
            rate_limiter: limiter.map(baskfy_providers::factory::KiteReadLimiter::into_limiter),
            client_factory: Some(factory),
            token_store: Some(Arc::new(store)),
            ..KiteRuntime::default()
        },
    )
    .map_err(WorkerError::Provider)
}
