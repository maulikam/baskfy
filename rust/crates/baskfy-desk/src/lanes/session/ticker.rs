//! `app/core/ticker.py` — the WebSocket tick bus. The broker's ticker pushes into bounded
//! per-subscriber queues; strategies subscribe. Hot path is in memory only — no DB, no disk.
//!
//! [`TickBus`] is ported whole: `subscribe` hands out a queue of 2,048 ticks, `publish` records
//! the last tick per instrument and offers it to every subscriber of that instrument (a full
//! queue drops the tick, as `put_nowait` raising `QueueFull` inside `call_soon_threadsafe`
//! does — the loop logs it and the tick is lost, nothing blocks). Before a loop is attached
//! (`bus.loop is None`) publishing is a no-op, as in Python.
//!
//! [`start_ticker`] keeps SW20's refusal (more than 3,000 instruments on one connection) and
//! then refuses to connect: **this binary has no live Kite client** (R8.0, R8.3) — the
//! websocket is a cutover item, like the live REST client.

use std::collections::HashMap;
use std::sync::Mutex;

use tokio::sync::mpsc;

use crate::wire::{PyDict, PyValue};

/// One subscriber queue's capacity (`asyncio.Queue(maxsize=2048)`).
pub const QUEUE_SIZE: usize = 2048;

/// One websocket connection carries at most this many instruments (Kite: 3 connections per API
/// key, 3,000 each). SW20 refuses a longer list rather than truncating silently.
pub const MAX_INSTRUMENTS_PER_CONNECTION: usize = 3000;

#[derive(Default)]
struct Inner {
    queues: HashMap<i64, Vec<mpsc::Sender<PyDict>>>,
    last_tick: HashMap<i64, PyDict>,
    attached: bool,
}

/// `TickBus`.
#[derive(Default)]
pub struct TickBus {
    inner: Mutex<Inner>,
}

impl std::fmt::Debug for TickBus {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("TickBus").finish_non_exhaustive()
    }
}

impl TickBus {
    /// An empty bus with no loop attached.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Inner> {
        match self.inner.lock() {
            Ok(g) => g,
            Err(poisoned) => poisoned.into_inner(),
        }
    }

    /// `bus.loop = asyncio.get_running_loop()`: from now on publishing delivers.
    pub fn attach(&self) {
        self.lock().attached = true;
    }

    /// `subscribe(token)`: a new bounded queue for one instrument.
    pub fn subscribe(&self, token: i64) -> mpsc::Receiver<PyDict> {
        let (tx, rx) = mpsc::channel(QUEUE_SIZE);
        self.lock().queues.entry(token).or_default().push(tx);
        rx
    }

    /// The last tick seen for an instrument.
    #[must_use]
    pub fn last_tick(&self, token: i64) -> Option<PyDict> {
        self.lock().last_tick.get(&token).cloned()
    }

    /// `publish_threadsafe(ticks)`: returns how many deliveries were dropped (full or closed
    /// queues). A tick with no integer `instrument_token` is refused (Python's `KeyError`
    /// would end the batch there) and ends the batch.
    ///
    /// # Errors
    /// `KeyError: 'instrument_token'` for a tick without one; the ticks before it were delivered.
    pub fn publish(&self, ticks: &[PyDict]) -> Result<usize, String> {
        let mut inner = self.lock();
        if !inner.attached {
            return Ok(0);
        }
        let mut dropped = 0;
        for t in ticks {
            let Some(PyValue::Int(tok)) = t.get("instrument_token") else {
                return Err("KeyError: 'instrument_token'".to_owned());
            };
            let tok = *tok;
            inner.last_tick.insert(tok, t.clone());
            if let Some(queues) = inner.queues.get(&tok) {
                for q in queues {
                    if q.try_send(t.clone()).is_err() {
                        dropped += 1;
                    }
                }
            }
        }
        Ok(dropped)
    }
}

/// `start_ticker(api_key, access_token, tokens, bus)`: the subscription-size refusal (SW20), then
/// — in this binary — the refusal to open a live websocket.
///
/// # Errors
/// Always: the `ValueError` for an oversize list, or "no live ticker" otherwise.
pub fn start_ticker(tokens: &[i64], bus: &TickBus) -> Result<(), String> {
    if tokens.len() > MAX_INSTRUMENTS_PER_CONNECTION {
        return Err(format!(
            "{} instruments asked for on one websocket; Kite carries \
             {MAX_INSTRUMENTS_PER_CONNECTION}. Split the list across connections, or watch \
             fewer names — a silently truncated subscription is a break nobody sees.",
            tokens.len()
        ));
    }
    let _ = bus;
    Err("no live Kite ticker in this binary (DECISIONS-RS R8.0/R8.3: a cutover item)".to_owned())
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;
    use crate::wire::dict;

    fn tick(token: i64, ltp: f64) -> PyDict {
        dict([
            ("instrument_token", PyValue::Int(token)),
            ("last_price", PyValue::Float(ltp)),
        ])
    }

    #[tokio::test]
    async fn ticks_reach_only_their_instruments_subscribers_and_the_last_is_kept() {
        let bus = TickBus::new();
        let mut a = bus.subscribe(1);
        let mut a2 = bus.subscribe(1);
        let mut b = bus.subscribe(2);
        assert_eq!(
            bus.publish(&[tick(1, 10.0)]).unwrap(),
            0,
            "no loop yet: a no-op"
        );
        assert!(bus.last_tick(1).is_none());
        bus.attach();
        bus.publish(&[tick(1, 10.5), tick(3, 1.0), tick(1, 11.0)])
            .unwrap();
        assert_eq!(a.recv().await.unwrap(), tick(1, 10.5));
        assert_eq!(a.recv().await.unwrap(), tick(1, 11.0));
        assert_eq!(a2.recv().await.unwrap(), tick(1, 10.5));
        assert!(b.try_recv().is_err());
        assert_eq!(bus.last_tick(1).unwrap(), tick(1, 11.0));
        assert_eq!(bus.last_tick(3).unwrap(), tick(3, 1.0));
    }

    #[test]
    fn a_full_queue_drops_rather_than_blocks() {
        let bus = TickBus::new();
        bus.attach();
        let _rx = bus.subscribe(7);
        let batch: Vec<PyDict> = (0..QUEUE_SIZE + 5).map(|_| tick(7, 1.0)).collect();
        assert_eq!(bus.publish(&batch).unwrap(), 5);
    }

    #[test]
    fn a_tick_without_a_token_is_a_key_error() {
        let bus = TickBus::new();
        bus.attach();
        let bad = dict([("last_price", PyValue::Float(1.0))]);
        assert_eq!(
            bus.publish(&[bad]).unwrap_err(),
            "KeyError: 'instrument_token'"
        );
    }

    #[test]
    fn more_than_one_connection_carries_is_refused_and_no_live_socket_opens() {
        let bus = TickBus::new();
        let too_many: Vec<i64> = (0..3001).collect();
        let err = start_ticker(&too_many, &bus).unwrap_err();
        assert!(err.starts_with("3001 instruments asked for on one websocket; Kite carries 3000."));
        let err = start_ticker(&[1, 2], &bus).unwrap_err();
        assert!(err.contains("no live Kite ticker"));
    }
}
