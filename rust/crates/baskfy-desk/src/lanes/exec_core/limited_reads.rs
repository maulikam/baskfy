//! Every Kite read a lane makes takes its family's slot from the desk's one read limiter
//! (`app/kite_client.py`'s `self.limits.slot(...)`, SW20) before it reaches the broker.

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use baskfy_providers::secret::Secret;
use tokio::sync::OnceCell;

use super::kite_client::DeskKite;
use super::kite_limits::{DeskLimits, LimitClock, SharedSpacers, TokioClock, family_for_call};
use crate::kite::{KiteError, KiteReads};
use crate::settings::SharedSettings;
use crate::wire::PyValue;

enum Source {
    Desk(Arc<DeskKite>),
    Fixed(Arc<DeskLimits>),
    Own(Box<OwnLimits>),
}

struct OwnLimits {
    limits: OnceCell<Arc<DeskLimits>>,
    shared: SharedSpacers,
    clock: Arc<dyn LimitClock>,
}

/// A [`KiteReads`] whose every request-carrying read waits for its family's slot first.
pub struct LimitedReads {
    inner: Arc<dyn KiteReads>,
    source: Source,
    taken: Mutex<Vec<&'static str>>,
}

impl std::fmt::Debug for LimitedReads {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("LimitedReads").finish_non_exhaustive()
    }
}

impl LimitedReads {
    /// Share the gateway's Kite wrapper's limiter: one process, one limiter, as Python's `Kite()`.
    #[must_use]
    pub fn sharing(inner: Arc<dyn KiteReads>, desk: Arc<DeskKite>) -> Self {
        Self::from_source(inner, Source::Desk(desk))
    }

    /// Use the given limiter.
    #[must_use]
    pub fn with_limits(inner: Arc<dyn KiteReads>, limits: Arc<DeskLimits>) -> Self {
        Self::from_source(inner, Source::Fixed(limits))
    }

    /// A limiter of its own, built on first use from `DESK_SHARED_READ_LIMITS` and its Redis URL.
    #[must_use]
    pub fn own(inner: Arc<dyn KiteReads>, settings: &SharedSettings) -> Self {
        let (enabled, url) = match settings.read() {
            Ok(s) => (s.shared_read_limits, s.shared_read_limits_redis_url.clone()),
            Err(p) => {
                let s = p.into_inner();
                (s.shared_read_limits, s.shared_read_limits_redis_url.clone())
            }
        };
        let clock: Arc<dyn LimitClock> = Arc::new(TokioClock::default());
        Self::from_source(
            inner,
            Source::Own(Box::new(OwnLimits {
                limits: OnceCell::new(),
                shared: SharedSpacers::new(enabled, &url, clock.clone()),
                clock,
            })),
        )
    }

    fn from_source(inner: Arc<dyn KiteReads>, source: Source) -> Self {
        Self {
            inner,
            source,
            taken: Mutex::new(Vec::new()),
        }
    }

    /// Every family this wrapper has taken a slot from, in order.
    #[must_use]
    pub fn slots_taken(&self) -> Vec<&'static str> {
        self.taken.lock().map(|l| l.clone()).unwrap_or_default()
    }

    async fn slot(&self, call: &str) {
        let family = family_for_call(call).unwrap_or("general");
        if let Ok(mut log) = self.taken.lock() {
            log.push(family);
        }
        match &self.source {
            Source::Desk(desk) => desk.take_read_slot(family).await,
            Source::Fixed(limits) => {
                let _ = limits.slot(family).await;
            }
            Source::Own(own) => {
                let built = own
                    .limits
                    .get_or_init(|| async {
                        Arc::new(DeskLimits::new(own.clock.clone(), own.shared.get().await))
                    })
                    .await;
                let _ = built.slot(family).await;
            }
        }
    }
}

#[async_trait]
impl KiteReads for LimitedReads {
    fn login_url(&self) -> String {
        self.inner.login_url()
    }

    fn set_access_token(&self, token: &Secret) {
        self.inner.set_access_token(token);
    }

    async fn generate_session(
        &self,
        request_token: &str,
        api_secret: &Secret,
    ) -> Result<PyValue, KiteError> {
        self.inner.generate_session(request_token, api_secret).await
    }

    async fn profile(&self) -> Result<PyValue, KiteError> {
        self.slot("profile").await;
        self.inner.profile().await
    }

    async fn holdings(&self) -> Result<PyValue, KiteError> {
        self.slot("holdings").await;
        self.inner.holdings().await
    }

    async fn positions(&self) -> Result<PyValue, KiteError> {
        self.slot("positions").await;
        self.inner.positions().await
    }

    async fn margins(&self, segment: Option<&str>) -> Result<PyValue, KiteError> {
        self.slot("margins").await;
        self.inner.margins(segment).await
    }

    async fn ltp(&self, instruments: &[String]) -> Result<PyValue, KiteError> {
        self.slot("ltp").await;
        self.inner.ltp(instruments).await
    }

    async fn ohlc(&self, instruments: &[String]) -> Result<PyValue, KiteError> {
        self.slot("ohlc").await;
        self.inner.ohlc(instruments).await
    }

    async fn quote(&self, instruments: &[String]) -> Result<PyValue, KiteError> {
        self.slot("quote").await;
        self.inner.quote(instruments).await
    }

    async fn instruments(&self, exchange: Option<&str>) -> Result<PyValue, KiteError> {
        self.slot("instruments").await;
        self.inner.instruments(exchange).await
    }

    async fn historical_data(
        &self,
        instrument_token: i64,
        from: &str,
        to: &str,
        interval: &str,
        continuous: bool,
        oi: bool,
    ) -> Result<PyValue, KiteError> {
        self.slot("historical_data").await;
        self.inner
            .historical_data(instrument_token, from, to, interval, continuous, oi)
            .await
    }

    async fn orders(&self) -> Result<PyValue, KiteError> {
        self.slot("orders").await;
        self.inner.orders().await
    }

    async fn order_history(&self, order_id: &str) -> Result<PyValue, KiteError> {
        self.slot("order_history").await;
        self.inner.order_history(order_id).await
    }

    async fn trades(&self) -> Result<PyValue, KiteError> {
        self.slot("trades").await;
        self.inner.trades().await
    }

    async fn get_gtts(&self) -> Result<PyValue, KiteError> {
        self.slot("get_gtts").await;
        self.inner.get_gtts().await
    }

    async fn basket_order_margins(
        &self,
        params: &[PyValue],
        consider_positions: bool,
        mode: Option<&str>,
    ) -> Result<PyValue, KiteError> {
        self.slot("basket_order_margins").await;
        self.inner
            .basket_order_margins(params, consider_positions, mode)
            .await
    }
}
