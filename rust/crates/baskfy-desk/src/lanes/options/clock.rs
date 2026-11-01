//! Ports `app.options_clock` (OP9): one loop from start to the close. Drain the tick queues into
//! the strategy, fall back to a throttled quote when the feed is quiet (B10: at most one call
//! per `min_interval`), and call `strategy.check(now)` on **every** pass, ticks or not — a
//! position whose feed has gone silent is exactly the one `FEED_LOST` exists for. `now`,
//! `sleep` and the monotonic clock are seams, so a test ends the loop by handing it a time past
//! the close. New legs are followed on the ticker as they appear (`follow`), and `act(now)` —
//! the executor's sweep of raised exits — runs after every evaluation pass, its error logged and
//! the loop carried on.

use chrono::NaiveDateTime;

use crate::wire::{PyDict, PyValue};

/// No tick from anything for this long → ask the quote endpoint (B10).
pub const QUIET_SECONDS: f64 = 5.0;

/// `is_token_error(exc)`: Kite refused the access token, not a network hiccup.
#[must_use]
pub fn is_token_error(python_type: &str) -> bool {
    python_type == "TokenException"
}

/// What the loop drives (`NiftyOptionsMonitor` or a test's stand-in). An `Err` is the
/// exception Python's strategy would raise: it ends the session (after `on_stop`).
#[async_trait::async_trait]
pub trait Monitor: Send {
    /// The tokens to follow (the index, then every leg).
    fn tokens(&self) -> Vec<i64>;
    /// Exits raised so far.
    fn exits(&self) -> usize;
    /// Load the day.
    async fn on_start(&mut self);
    /// Stop.
    async fn on_stop(&mut self);
    /// One tick.
    async fn on_tick(&mut self, tick: &PyDict) -> Result<(), String>;
    /// One evaluation pass.
    async fn check(&mut self, now: NaiveDateTime) -> Result<(), String>;
    /// Past the close?
    fn session_over(&self, at: NaiveDateTime) -> bool;
    /// The token was refused: raise every exit.
    async fn token_lost(&mut self, now: NaiveDateTime) -> Result<(), String>;
}

/// `act(now)`: what runs after every evaluation pass — the executor's sweep of pending exits.
#[async_trait::async_trait]
pub trait Act: Send {
    /// One sweep; an `Err` is logged and the loop carries on.
    async fn act(&mut self, now: NaiveDateTime) -> Result<(), String>;
}

/// `follow(tokens)`: subscribe new tokens on the live ticker; an `Err` is logged and the bus
/// still gets the quote fallback for them.
pub type Follow<'f> = &'f mut (dyn FnMut(&[i64]) -> Result<(), String> + Send);

/// The quote endpoint the fallback asks (`kite.quote_raw(keys)`); `Err(python_type)` on a
/// failure.
#[async_trait::async_trait]
pub trait QuoteRaw: Send + Sync {
    /// `quote_raw(keys)`.
    async fn quote_raw(&self, keys: &[String]) -> Result<PyDict, String>;
}

/// `LegQuotes`: B10 for options — one `quote` call for every followed token when quiet.
pub struct LegQuotes<'k> {
    kite: &'k dyn QuoteRaw,
    min_interval: f64,
    last: Option<f64>,
    /// Calls made.
    pub calls: i64,
    /// Failures.
    pub failures: i64,
    /// The last poll was refused for the token (OP14).
    pub token_dead: bool,
}

impl<'k> LegQuotes<'k> {
    /// `LegQuotes(kite, min_interval=5.0)`.
    #[must_use]
    pub fn new(kite: &'k dyn QuoteRaw, min_interval: f64) -> Self {
        Self {
            kite,
            min_interval,
            last: None,
            calls: 0,
            failures: 0,
            token_dead: false,
        }
    }

    /// `poll(tokens, now)`: `None` inside the interval.
    pub async fn poll(
        &mut self,
        tokens: &[i64],
        now: NaiveDateTime,
        moment: f64,
    ) -> Option<Vec<PyDict>> {
        if let Some(last) = self.last
            && moment - last < self.min_interval
        {
            return None;
        }
        self.last = Some(moment);
        let keys: Vec<String> = tokens.iter().map(ToString::to_string).collect();
        if keys.is_empty() {
            return Some(Vec::new());
        }
        self.calls += 1;
        match self.kite.quote_raw(&keys).await {
            Err(kind) => {
                self.failures += 1;
                if is_token_error(&kind) {
                    self.token_dead = true;
                }
                Some(Vec::new())
            }
            Ok(payload) => {
                self.token_dead = false;
                Some(
                    payload
                        .iter()
                        .filter_map(|(k, q)| match q {
                            PyValue::Map(m) => Some(tick_of(k, m, now)),
                            _ => None,
                        })
                        .collect(),
                )
            }
        }
    }
}

/// `_tick_of(key, quote, now)`.
#[must_use]
pub fn tick_of(key: &str, quote: &PyDict, now: NaiveDateTime) -> PyDict {
    let token = match quote.get("instrument_token") {
        Some(PyValue::Int(i)) => PyValue::Int(*i),
        _ => {
            if !key.is_empty() && key.chars().all(|c| c.is_ascii_digit()) {
                key.parse().map_or(PyValue::None, PyValue::Int)
            } else {
                PyValue::None
            }
        }
    };
    let stamp = [quote.get("timestamp"), quote.get("last_trade_time")]
        .into_iter()
        .flatten()
        .find(|v| v.truthy())
        .cloned()
        .unwrap_or(PyValue::DateTime(
            baskfy_providers::records::PyDateTime::Naive(now),
        ));
    let depth = match quote.get("depth") {
        Some(v) if v.truthy() => v.clone(),
        _ => PyValue::Map(PyDict::new()),
    };
    crate::wire::dict([
        ("instrument_token", token),
        (
            "last_price",
            quote.get("last_price").cloned().unwrap_or(PyValue::None),
        ),
        ("depth", depth),
        ("exchange_timestamp", stamp),
    ])
}

/// The loop's seams: `now()`, `sleep(seconds)`, `clock()` (monotonic).
#[async_trait::async_trait]
pub trait Seams: Send {
    /// IST, naive.
    fn now(&mut self) -> NaiveDateTime;
    /// Monotonic seconds.
    fn clock(&mut self) -> f64;
    /// Sleep.
    async fn sleep(&mut self, seconds: f64);
}

/// The tick source per token (`bus.subscribe(token)` queues, drained without waiting).
pub trait Bus: Send {
    /// Subscribe a token.
    fn subscribe(&mut self, token: i64);
    /// Drain whatever is queued for `token`.
    fn drain(&mut self, token: i64) -> Vec<PyDict>;
}

/// `run_session(strategy, bus, follow=, quotes=, act=, now=, sleep=, poll_seconds=, clock=)`:
/// drive the strategy until `session_over(now())`; the number of exits it raised.
///
/// # Errors
/// The strategy's exception (`on_tick`, `check`, `token_lost`), after `on_stop`.
pub async fn run_session(
    strategy: &mut dyn Monitor,
    bus: &mut dyn Bus,
    mut follow: Option<Follow<'_>>,
    mut quotes: Option<&mut LegQuotes<'_>>,
    mut act: Option<&mut dyn Act>,
    seams: &mut dyn Seams,
    poll_seconds: f64,
) -> Result<usize, String> {
    let mut order: Vec<i64> = Vec::new();
    let mut sync = |strategy: &dyn Monitor, bus: &mut dyn Bus, order: &mut Vec<i64>| {
        let mut new: Vec<i64> = Vec::new();
        for t in strategy.tokens() {
            if !order.contains(&t) && !new.contains(&t) {
                new.push(t);
            }
        }
        for t in &new {
            bus.subscribe(*t);
            order.push(*t);
        }
        if !new.is_empty()
            && let Some(f) = follow.as_deref_mut()
            && let Err(e) = f(&new)
        {
            super::monitor::emit(&format!(
                "ERROR desk.options_clock options monitor: could not subscribe {new:?} on the ticker: {e}"
            ));
        }
    };
    strategy.on_start().await;
    sync(strategy, bus, &mut order);
    let mut last_tick = seams.clock();
    let outcome: Result<(), String> = async {
        loop {
            let now = seams.now();
            if strategy.session_over(now) {
                return Ok(());
            }
            let mut drained = 0;
            for token in order.clone() {
                for tick in bus.drain(token) {
                    strategy.on_tick(&tick).await?;
                    drained += 1;
                }
            }
            if drained > 0 {
                last_tick = seams.clock();
            } else if let Some(q) = quotes.as_deref_mut()
                && seams.clock() - last_tick >= QUIET_SECONDS
            {
                let moment = seams.clock();
                let now = seams.now();
                for tick in q.poll(&order, now, moment).await.unwrap_or_default() {
                    strategy.on_tick(&tick).await?;
                }
                if q.token_dead {
                    let now = seams.now();
                    strategy.token_lost(now).await?;
                }
            }
            let now = seams.now();
            strategy.check(now).await?;
            if let Some(a) = act.as_deref_mut() {
                let now = seams.now();
                if let Err(e) = a.act(now).await {
                    super::monitor::emit(&format!(
                        "ERROR desk.options_clock options monitor: the exit sweep failed: {e}"
                    ));
                }
            }
            sync(strategy, bus, &mut order);
            seams.sleep(poll_seconds).await;
        }
    }
    .await;
    strategy.on_stop().await;
    outcome.map(|()| strategy.exits())
}
