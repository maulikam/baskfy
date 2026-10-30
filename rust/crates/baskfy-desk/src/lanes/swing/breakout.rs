//! Ports `app/strategies/swing_breakout.py` (SW6): the opening-range monitor, a desk strategy
//! that **never places**. It watches the watchlist's names, builds each range from the ticks
//! inside `[open, open + window)` at window close (A4), reconciles it once against the minute
//! candles `range_reconcile_delay_minutes` later, evaluates every tick with the pure
//! `evaluate_trigger`, and on a break RAISES A SIGNAL through its [`SignalStore`].
//!
//! It holds no gateway and no Kite writes — not even an unused one: the type has no field that
//! could carry either, and [`SwingBreakout::generate_targets`] is empty. The person confirms;
//! the monitor raises. (SW25's auto-execute is the runner's, not this type's.)

use std::collections::{BTreeMap, BTreeSet};

use async_trait::async_trait;
use baskfy_core::swing::opening_range::{
    OpeningRange, TriggerState, TriggerVerdict, evaluate_trigger,
};
use baskfy_core::swing::setups::{Setup, SwingConfig};
use chrono::{NaiveDate, NaiveDateTime, NaiveTime};
use rust_decimal::Decimal;

use super::rules::{Candle, RuleError, opening_range, py_dec, py_int_list, session_open};

/// `WatchedName`: one row of the morning's watchlist, as the monitor needs it.
#[derive(Debug, Clone, PartialEq)]
pub struct WatchedName {
    /// `sw_watch.id`.
    pub watch_id: i64,
    /// `instrument.id`.
    pub instrument_id: i64,
    /// The symbol.
    pub symbol: String,
    /// Kite's instrument token.
    pub token: i64,
    /// FLAG or EP.
    pub setup: Setup,
    /// A FLAG's pivot (its trigger); `None` for an EP.
    pub pivot_high: Option<Decimal>,
    /// The day's upper circuit, from the morning quote.
    pub upper_circuit: Option<Decimal>,
    /// In today's daily focus (A14): its signal is pushed.
    pub focus: bool,
}

/// `Signal`: what the monitor raised — one per name per session, at most.
#[derive(Debug, Clone, PartialEq)]
pub struct Signal {
    /// The name.
    pub watch: WatchedName,
    /// When (naive, exchange-local).
    pub at: NaiveDateTime,
    /// The verdict.
    pub verdict: TriggerVerdict,
    /// The tick's price.
    pub last_price: Decimal,
    /// The low of the day so far.
    pub low_of_day: Decimal,
    /// The window.
    pub window_minutes: i64,
}

/// `SignalStore`: where a signal goes.
#[async_trait]
pub trait SignalStore: Send {
    /// Record one signal (a failure is logged by the strategy, never raised).
    async fn raise_signal(&mut self, signal: &Signal) -> Result<(), String>;
}

/// `CandleSource`: minute candles for one token from the open to `until`.
#[async_trait]
pub trait CandleSource: Send + Sync {
    /// The candles; empty means "none yet".
    async fn minute_candles(
        &self,
        token: i64,
        day: NaiveDate,
        until: NaiveDateTime,
    ) -> Result<Vec<Candle>, String>;
}

/// One quote snapshot as the strategy reads it.
#[derive(Debug, Clone, PartialEq)]
pub struct Tick {
    /// `instrument_token`.
    pub instrument_token: i64,
    /// `last_price` (`Decimal(str(x))`).
    pub last_price: Option<Decimal>,
    /// `ohlc.low`, the exchange's running low.
    pub day_low: Option<Decimal>,
    /// The tick's time (`_tick_time`), naive exchange-local.
    pub at: NaiveDateTime,
}

#[derive(Debug, Clone, Default)]
struct NameState {
    low_of_day: Option<Decimal>,
    tick_high: Option<Decimal>,
    tick_low: Option<Decimal>,
    opening: Option<OpeningRange>,
    reconciled: bool,
    reconcile_note: Option<String>,
    triggered: bool,
    last_state: Option<TriggerState>,
    recorded: BTreeSet<String>,
}

/// `SwingBreakout`: watch the list, build each range at window close, raise the break.
pub struct SwingBreakout {
    /// token → name.
    pub watchlist: BTreeMap<i64, WatchedName>,
    order: Vec<i64>,
    day: NaiveDate,
    config: SwingConfig,
    /// The window in force.
    pub window_minutes: i64,
    state: BTreeMap<i64, NameState>,
    /// Every signal raised this session.
    pub signals: Vec<Signal>,
    open: NaiveDateTime,
    window_end: NaiveDateTime,
    reconcile_at: NaiveDateTime,
    /// How long each verdict took, seconds.
    pub verdict_seconds: Vec<f64>,
    observe: Option<VerdictSink>,
}

/// A sink for each verdict's duration (seconds); an `Err` drops it.
pub type VerdictSink = Box<dyn Fn(f64) -> Result<(), String> + Send + Sync>;

fn state_name(state: TriggerState) -> &'static str {
    match state {
        TriggerState::Triggered => "TRIGGERED",
        TriggerState::Waiting => "WAITING",
        TriggerState::RangeIncomplete => "RANGE_INCOMPLETE",
        TriggerState::BelowPivot => "BELOW_PIVOT",
        TriggerState::LockedUpperCircuit => "LOCKED_UPPER_CIRCUIT",
        TriggerState::SessionOver => "SESSION_OVER",
    }
}

/// `TriggerState.value`.
#[must_use]
pub const fn trigger_state_str(state: TriggerState) -> &'static str {
    match state {
        TriggerState::Triggered => "TRIGGERED",
        TriggerState::Waiting => "WAITING",
        TriggerState::RangeIncomplete => "RANGE_INCOMPLETE",
        TriggerState::BelowPivot => "BELOW_PIVOT",
        TriggerState::LockedUpperCircuit => "LOCKED_UPPER_CIRCUIT",
        TriggerState::SessionOver => "SESSION_OVER",
    }
}

impl SwingBreakout {
    /// Build the strategy. The window must be one the config names — refused at start-up
    /// rather than at 09:20.
    ///
    /// # Errors
    /// An unknown window (Python's `ValueError`).
    pub fn new(
        watchlist: Vec<WatchedName>,
        day: NaiveDate,
        window_minutes: Option<i64>,
        config: SwingConfig,
    ) -> Result<Self, RuleError> {
        let window = window_minutes
            .filter(|w| *w != 0)
            .unwrap_or(config.opening_range.default_window_minutes);
        if !config.opening_range.windows_minutes.contains(&window) {
            return Err(RuleError(format!(
                "window {window} is not one of {} (docs/swing/04 §7)",
                py_int_list(&config.opening_range.windows_minutes)
            )));
        }
        let order: Vec<i64> = watchlist.iter().map(|w| w.token).collect();
        let state = order.iter().map(|t| (*t, NameState::default())).collect();
        let open = session_open(day, &config.opening_range);
        let window_end = open + chrono::Duration::minutes(window);
        let reconcile_at = window_end
            + chrono::Duration::minutes(config.opening_range.range_reconcile_delay_minutes);
        Ok(Self {
            watchlist: watchlist.into_iter().map(|w| (w.token, w)).collect(),
            order,
            day,
            config,
            window_minutes: window,
            state,
            signals: Vec::new(),
            open,
            window_end,
            reconcile_at,
            verdict_seconds: Vec::new(),
            observe: None,
        })
    }

    /// `tokens`: the watched tokens, in the order they were added.
    #[must_use]
    pub fn tokens(&self) -> Vec<i64> {
        self.order.clone()
    }

    /// `add_watch` (LV4): false when already watched.
    pub fn add_watch(&mut self, watched: WatchedName) -> bool {
        if self.watchlist.contains_key(&watched.token) {
            return false;
        }
        self.order.push(watched.token);
        self.state.insert(watched.token, NameState::default());
        self.watchlist.insert(watched.token, watched);
        true
    }

    /// `drop_watch`: false when it was not watched. A raised signal stands.
    pub fn drop_watch(&mut self, token: i64) -> bool {
        if self.watchlist.remove(&token).is_none() {
            return false;
        }
        self.order.retain(|t| *t != token);
        self.state.remove(&token);
        true
    }

    /// `observe_with`: hand every verdict's duration to a sink; one that fails is dropped.
    pub fn observe_with(&mut self, sink: Option<VerdictSink>) {
        self.observe = sink;
    }

    /// The reconcile note on a name, for the log and the tests.
    #[must_use]
    pub fn reconcile_note(&self, token: i64) -> Option<String> {
        self.state
            .get(&token)
            .and_then(|s| s.reconcile_note.clone())
    }

    /// The name's current opening range.
    #[must_use]
    pub fn opening(&self, token: i64) -> Option<OpeningRange> {
        self.state.get(&token).and_then(|s| s.opening.clone())
    }

    /// The last verdict state a name reached.
    #[must_use]
    pub fn last_state(&self, token: i64) -> Option<TriggerState> {
        self.state.get(&token).and_then(|s| s.last_state)
    }

    fn took(&mut self, started: std::time::Instant) {
        let elapsed = started.elapsed().as_secs_f64();
        self.verdict_seconds.push(elapsed);
        if let Some(sink) = &self.observe
            && sink(elapsed).is_err()
        {
            self.observe = None;
        }
    }

    /// `on_tick`: evaluate the name the tick belongs to; raise at most one signal.
    pub async fn on_tick(
        &mut self,
        tick: &Tick,
        store: &mut dyn SignalStore,
        candles: &dyn CandleSource,
    ) {
        let started = std::time::Instant::now();
        let token = tick.instrument_token;
        let Some(watched) = self.watchlist.get(&token).cloned() else {
            return;
        };
        let (window_end, reconcile_at, open, window_minutes) = (
            self.window_end,
            self.reconcile_at,
            self.open,
            self.window_minutes,
        );
        let Some(state) = self.state.get_mut(&token) else {
            return;
        };
        if state.triggered {
            return;
        }
        let at = tick.at;
        let Some(price) = tick.last_price else {
            return;
        };
        state.low_of_day = Some(match (tick.day_low, state.low_of_day) {
            (Some(low), _) => low,
            (None, None) => price,
            (None, Some(seen)) => {
                if price < seen {
                    price
                } else {
                    seen
                }
            }
        });
        if open <= at && at < window_end {
            // Python's max()/min() keep the FIRST of equal values; `Ord::max` would keep the
            // second, and a Decimal's scale (100.0 vs 100.00) is part of what a note prints.
            state.tick_high = Some(
                state
                    .tick_high
                    .map_or(price, |h| if price > h { price } else { h }),
            );
            state.tick_low = Some(
                state
                    .tick_low
                    .map_or(price, |l| if price < l { price } else { l }),
            );
        }
        if state.opening.is_none() && at >= window_end {
            state.opening = match (state.tick_high, state.tick_low) {
                (Some(high), Some(low)) => Some(OpeningRange {
                    high,
                    low,
                    window_minutes,
                    complete: true,
                    candles: 1,
                }),
                _ => None,
            };
        }
        let needs_reconcile = !state.reconciled && at >= reconcile_at;
        if needs_reconcile {
            self.reconcile(token, at, candles).await;
        }
        let Some(state) = self.state.get_mut(&token) else {
            return;
        };
        let Some(opening) = state.opening.clone() else {
            self.took(started);
            return;
        };
        let low = state.low_of_day.unwrap_or(price);
        let low = if low.is_zero() { price } else { low };
        let verdict = evaluate_trigger(
            price,
            &opening,
            if watched.setup == Setup::Flag {
                watched.pivot_high
            } else {
                None
            },
            low,
            watched.upper_circuit,
            at,
            &self.config.opening_range,
        );
        let Ok(verdict) = verdict else {
            self.took(started);
            return;
        };
        state.last_state = Some(verdict.state);
        let low_of_day = state.low_of_day.unwrap_or(Decimal::ZERO);
        let mut raise = false;
        match verdict.state {
            TriggerState::Triggered => {
                state.triggered = true;
                raise = true;
            }
            TriggerState::LockedUpperCircuit | TriggerState::BelowPivot
                if state.recorded.insert(state_name(verdict.state).to_owned()) =>
            {
                raise = true;
            }
            _ => {}
        }
        self.took(started);
        if raise {
            let signal = Signal {
                watch: watched,
                at,
                verdict,
                last_price: price,
                low_of_day,
                window_minutes: self.window_minutes,
            };
            self.signals.push(signal.clone());
            if let Err(e) = store.raise_signal(&signal).await {
                log_error(&format!(
                    "ERROR swing_breakout could not record signal for {}: {e}",
                    signal.watch.symbol
                ));
            }
        }
    }

    async fn reconcile(&mut self, token: i64, at: NaiveDateTime, candles: &dyn CandleSource) {
        if let Some(state) = self.state.get_mut(&token) {
            state.reconciled = true;
        }
        let Ok(bars) = candles.minute_candles(token, self.day, at).await else {
            return;
        };
        if bars.is_empty() {
            return;
        }
        let Ok(built) = opening_range(
            &bars,
            self.day,
            self.window_minutes,
            &self.config.opening_range,
        ) else {
            return;
        };
        if !built.complete || built.candles == 0 {
            return;
        }
        let Some(state) = self.state.get_mut(&token) else {
            return;
        };
        let before = state.opening.clone();
        if let Some(b) = &before
            && b.high == built.high
            && b.low == built.low
        {
            return;
        }
        state.reconcile_note = Some(match &before {
            Some(b) => format!(
                "range from ticks {}-{} reconciled to candles {}-{}",
                py_dec(b.low),
                py_dec(b.high),
                py_dec(built.low),
                py_dec(built.high)
            ),
            None => format!(
                "range from candles {}-{} (no tick inside the window)",
                py_dec(built.low),
                py_dec(built.high)
            ),
        });
        state.opening = Some(built);
    }

    /// `session_over`: strictly after `monitor_close`.
    #[must_use]
    pub fn session_over(&self, at: NaiveDateTime) -> bool {
        let (hour, minute) = self.config.opening_range.monitor_close;
        let close = NaiveTime::from_hms_opt(
            u32::try_from(hour).unwrap_or(15),
            u32::try_from(minute).unwrap_or(30),
            0,
        )
        .unwrap_or_default();
        at.time() > close
    }

    /// `generate_targets`: nothing. Ever.
    #[must_use]
    pub fn generate_targets(&self) -> Vec<()> {
        Vec::new()
    }
}

/// `log.exception(...)`: one line on stderr, the desk process's log (symbols and error words
/// only, never a secret).
#[allow(clippy::print_stderr)]
fn log_error(line: &str) {
    eprintln!("{line}");
}

#[cfg(test)]
mod tests;
