//! What the pipeline needs from outside the database. Ports `baskfy_worker.deps`.

use std::sync::Arc;

use baskfy_providers::composite::CompositeProvider;

use crate::window::DateWindow;

/// `PipelineDependencies`: everything the chain needs from outside the database, bundled so a
/// test can drive the whole pipeline with a replay or fixture provider and no cache.
///
/// The flags are read **once**, when the bundle is built (`providers.build_pipeline_dependencies`),
/// never inside a step: `docs/swing/02` requires a flag to be "read once per process at startup".
#[derive(Clone)]
pub struct PipelineDependencies {
    /// The provider stack (`build_provider_stack()`, or a replay composite under the harness).
    pub provider: Arc<CompositeProvider>,
    /// The publish step's screen cache (`build_cache()`): a Redis client, or `None`.
    pub cache: Option<redis::Client>,
    /// Backfill window for step 2; `None` means the trade date alone.
    pub bars_window: Option<DateWindow>,
    /// SW3: the sole tenant the swing step writes for; `None` skips the step.
    pub swing_user_id: Option<i64>,
    /// The benchmark the swing gate reads (`nifty-smallcap-250`, SW29).
    pub swing_index_slug: String,
    /// PACK.6: which book the swing ladder summarises. Places nothing either way.
    pub swing_execution_enabled: bool,
    /// VB4: the sole tenant; `None` skips the step.
    pub vbt_user_id: Option<i64>,
    /// `docs/vbt/02` Track B: detection on by default.
    pub vbt_nightly_enabled: bool,
    /// Read by the VBT evening job, never by detection.
    pub vbt_execution_enabled: bool,
    /// TW4: the sole tenant; `None` skips the step.
    pub twt_user_id: Option<i64>,
    /// `docs/twt/02` Track B: detection on by default.
    pub twt_nightly_enabled: bool,
    /// Read by TW5/TW6, **never** by the detector.
    pub twt_execution_enabled: bool,
}

impl std::fmt::Debug for PipelineDependencies {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PipelineDependencies")
            .field("provider", &self.provider)
            .field("cache", &self.cache.is_some())
            .field("bars_window", &self.bars_window)
            .field("swing_user_id", &self.swing_user_id)
            .field("vbt_user_id", &self.vbt_user_id)
            .field("twt_user_id", &self.twt_user_id)
            .finish_non_exhaustive()
    }
}

impl PipelineDependencies {
    /// The bundle with Python's dataclass defaults around `provider`.
    #[must_use]
    pub fn new(provider: Arc<CompositeProvider>) -> Self {
        Self {
            provider,
            cache: None,
            bars_window: None,
            swing_user_id: None,
            swing_index_slug: "nifty-smallcap-250".to_owned(),
            swing_execution_enabled: false,
            vbt_user_id: None,
            vbt_nightly_enabled: true,
            vbt_execution_enabled: false,
            twt_user_id: None,
            twt_nightly_enabled: true,
            twt_execution_enabled: false,
        }
    }
}
