//! Ports `app/swing_clock.py` (SW11, SW26): the session's two order-shaped chores — the 10:45
//! cutoff (cancel every open remainder, free every unclaimed slot) and the 15:15 GTT sweep (no
//! filled quantity without a stop). Both go through the swing book's gateway; every chore is
//! noted on the day's `sw_session.notes`, and every failure is logged and swallowed here — a
//! sweep that raised would end the process before the 15:15 sweep it exists to reach.

use std::collections::BTreeMap;
use std::sync::Arc;

use async_trait::async_trait;

use baskfy_core::swing::setups::SwingConfig;
use chrono::{DateTime, FixedOffset, NaiveDate, TimeZone};
use rust_decimal::Decimal;

use crate::clock::ist;
use crate::gateway::Gateway;
use crate::lanes::swing::execute::{
    CutoffReport, ExecOutcome, OrderSource, cutoff_open_orders, eod_gtt_sweep,
};
use crate::lanes::swing::store::SwingStore;
use crate::settings::Settings;

/// `SweepReport`: what the 15:15 sweep found and did.
#[derive(Debug, Clone, PartialEq)]
pub struct SweepReport {
    /// Naked before.
    pub naked_before: i64,
    /// Armed.
    pub armed: i64,
    /// Still naked after.
    pub still_naked: i64,
    /// Each re-arm's outcome.
    pub outcomes: Vec<ExecOutcome>,
}

/// `sweep_time(day)`: `gtt_sweep_at` on `day`, IST — the config field, not a literal.
#[must_use]
pub fn sweep_time(day: NaiveDate) -> DateTime<FixedOffset> {
    chore_time(day, SwingConfig::default().opening_range.gtt_sweep_at)
}

/// `pending_cutoff_at` on `day`, IST.
#[must_use]
pub fn cutoff_time(day: NaiveDate) -> DateTime<FixedOffset> {
    chore_time(day, SwingConfig::default().opening_range.pending_cutoff_at)
}

fn chore_time(day: NaiveDate, (hour, minute): (i64, i64)) -> DateTime<FixedOffset> {
    let naive = day
        .and_hms_opt(
            u32::try_from(hour).unwrap_or(0),
            u32::try_from(minute).unwrap_or(0),
            0,
        )
        .unwrap_or_default();
    ist()
        .from_local_datetime(&naive)
        .single()
        .unwrap_or_else(|| ist().from_utc_datetime(&naive))
}

/// The metric `desk_swing_sweeps_total{sweep, outcome}` as the chores count it.
pub trait SweepCounter: Send + Sync {
    /// One count; a failing sink is the sink's problem, never the chore's.
    fn count(&self, sweep: &str, outcome: &str);
}

/// A counter that records nothing (the desk's telemetry is the session lane's).
#[derive(Debug, Default, Clone, Copy)]
pub struct NoCounter;

impl SweepCounter for NoCounter {
    fn count(&self, _sweep: &str, _outcome: &str) {}
}

/// `run_cutoff`: the 10:45 chore through `cutoff_open_orders`; counted, never raising.
pub async fn run_cutoff(
    store: &mut dyn SwingStore,
    gw: &dyn Gateway,
    settings: &Settings,
    orders: Option<&dyn OrderSource>,
    now: DateTime<FixedOffset>,
    counter: &dyn SweepCounter,
) -> Option<CutoffReport> {
    let report = match cutoff_open_orders(store, gw, settings, orders, now).await {
        Ok(r) => r,
        Err(e) => {
            log_line!("ERROR swing_clock 10:45 cutoff failed: {e}");
            counter.count("cutoff", "failed");
            return None;
        }
    };
    counter.count("cutoff", "ok");
    if report.cancel_failed > 0 {
        counter.count("cutoff", "cancel_refused");
    }
    let local = now.with_timezone(&ist());
    let note = format!(
        "cutoff {}: {} reconciled, {} cancelled, {} refused, {} slots freed",
        local.format("%H:%M"),
        report.reconciled,
        report.cancelled,
        report.cancel_failed,
        report.slots_freed
    );
    if let Err(e) = store.note_session(local.date_naive(), &note).await {
        log_line!("WARNING swing_clock could not note the cutoff on the session: {e}");
    }
    Some(report)
}

/// `price_of(symbol)`: the last price the desk can read for a symbol (`swing_desk.last_price`,
/// `Kite.ltp`). An error is "no price", and the re-arm says BLOCKED, honestly.
#[async_trait]
pub trait PriceOf: Send + Sync {
    /// The price, `None` when there is none.
    async fn price_of(&self, symbol: &str) -> Result<Option<Decimal>, String>;
}

fn naked_count(rows: &[crate::lanes::swing::store::PositionRow]) -> i64 {
    i64::try_from(
        rows.iter()
            .filter(|p| p.gtt_id.is_none() && p.quantity_open > 0)
            .count(),
    )
    .unwrap_or(0)
}

/// `run_gtt_sweep`: the 15:15 chore (A8) — every open position with shares and no GTT is
/// re-armed through `eod_gtt_sweep` at the last price the desk can read for it. A sweep that
/// fails is logged and counted; a store that cannot list its positions is the caller's error,
/// as Python's raises into `run_chore`'s handler.
///
/// # Errors
/// The store's `open_positions`.
pub async fn run_gtt_sweep(
    store: &mut dyn SwingStore,
    gw: &dyn Gateway,
    settings: &Settings,
    now: DateTime<FixedOffset>,
    price_of: &dyn PriceOf,
    counter: &dyn SweepCounter,
) -> Result<SweepReport, String> {
    let naked: Vec<_> = store
        .open_positions()
        .await
        .map_err(|e| e.to_string())?
        .into_iter()
        .filter(|p| p.gtt_id.is_none() && p.quantity_open > 0)
        .collect();
    let mut prices = BTreeMap::new();
    for pos in &naked {
        match price_of.price_of(&pos.symbol).await {
            Ok(Some(price)) => {
                prices.insert(pos.symbol.clone(), price);
            }
            Ok(None) => {}
            Err(e) => log_line!("WARNING swing_clock no last price for {}: {e}", pos.symbol),
        }
    }
    let outcomes = match eod_gtt_sweep(store, gw, settings, now, &prices).await {
        Ok(o) => o,
        Err(e) => {
            log_line!("ERROR swing_clock 15:15 GTT sweep failed: {e}");
            counter.count("gtt", "failed");
            Vec::new()
        }
    };
    let armed = i64::try_from(
        outcomes
            .iter()
            .filter(|o| o.status == "SIMULATED" || o.status == "FILLED")
            .count(),
    )
    .unwrap_or(0);
    let still = naked_count(&store.open_positions().await.map_err(|e| e.to_string())?);
    counter.count("gtt", if still == 0 { "ok" } else { "still_naked" });
    let local = now.with_timezone(&ist());
    let naked_before = i64::try_from(naked.len()).unwrap_or(0);
    let note = format!(
        "gtt-sweep {}: {naked_before} naked, {armed} armed, {still} still naked",
        local.format("%H:%M")
    );
    if let Err(e) = store.note_session(local.date_naive(), &note).await {
        log_line!("WARNING swing_clock could not note the sweep on the session: {e}");
    }
    if still > 0 {
        log_line!(
            "ERROR swing_clock 15:15 sweep: {still} position(s) still without a GTT — \
             SWING_GTT_MISSING_AT_1515"
        );
    }
    Ok(SweepReport {
        naked_before,
        armed,
        still_naked: still,
        outcomes,
    })
}

/// The desk's collaborators a chore builds when it starts and drops when it is done
/// (`swing_desk.open_store`, `swing_gateway`, `order_source`, `last_price`). A seam that cannot
/// be built answers `Err`, and the chore is logged and counted `unavailable`, never raised.
#[async_trait]
pub trait ChoreSeams: Send + Sync {
    /// `open_store()`: the sole user's swing store.
    async fn open_store(&self) -> Result<Box<dyn SwingStore>, String>;
    /// `swing_gateway()`: the swing book's gateway.
    fn gateway(&self) -> Result<Arc<dyn Gateway>, String>;
    /// `order_source()`: the Kite order book (a READ).
    fn orders(&self) -> Option<Arc<dyn OrderSource>>;
    /// `last_price`.
    fn prices(&self) -> Arc<dyn PriceOf>;
}

/// The two chores by name.
pub const CHORES: [&str; 2] = ["cutoff", "gtt_sweep"];

async fn chore_body(
    name: &str,
    seams: &dyn ChoreSeams,
    settings: &Settings,
    moment: DateTime<FixedOffset>,
    counter: &dyn SweepCounter,
) -> Result<(), String> {
    let mut store = seams.open_store().await?;
    let gw = seams.gateway()?;
    match name {
        "cutoff" => {
            let orders = seams.orders();
            run_cutoff(
                store.as_mut(),
                gw.as_ref(),
                settings,
                orders.as_deref(),
                moment,
                counter,
            )
            .await;
            Ok(())
        }
        // `gtt_sweep` only: Python's `run_chore("gtt")` is a ValueError (the backstop
        // reaches the sweep by this name and counts its failure as `gtt` itself).
        "gtt_sweep" => {
            let prices = seams.prices();
            run_gtt_sweep(
                store.as_mut(),
                gw.as_ref(),
                settings,
                moment,
                prices.as_ref(),
                counter,
            )
            .await
            .map(|_| ())
        }
        other => Err(format!("ValueError: no such chore: '{other}'")),
    }
}

/// `run_chore(name)`: run ONE chore now — `cutoff` or `gtt_sweep` (SW26) — the single entry
/// point the watch and the backstop both use. Never raises: a store or gateway that cannot be
/// built is logged and counted `unavailable`.
pub async fn run_chore(
    name: &str,
    seams: &dyn ChoreSeams,
    settings: &Settings,
    now: DateTime<FixedOffset>,
    counter: &dyn SweepCounter,
) {
    if let Err(e) = chore_body(name, seams, settings, now, counter).await {
        log_line!("ERROR swing_clock clock: the {name} chore could not run: {e}");
        counter.count(name, "unavailable");
    }
}

/// `run_after_close`: the cutoff now, then sleep to `gtt_sweep_at` and sweep; `0`. The backstop
/// since SW26 — the watch normally ran both chores at their hours, and both are idempotent.
/// The 15:15 chore is counted as `gtt` when it cannot run, as Python's backstop names it.
pub async fn run_after_close<N, S, F>(
    day: NaiveDate,
    seams: &dyn ChoreSeams,
    settings: &Settings,
    mut now: N,
    mut sleep: S,
    counter: &dyn SweepCounter,
) -> i32
where
    N: FnMut() -> DateTime<FixedOffset> + Send,
    S: FnMut(f64) -> F + Send,
    F: std::future::Future<Output = ()> + Send,
{
    let moment = now();
    if let Err(e) = chore_body("cutoff", seams, settings, moment, counter).await {
        log_line!("ERROR swing_clock clock: the cutoff chore could not run: {e}");
        counter.count("cutoff", "unavailable");
    }
    let target = sweep_time(day);
    let wait = (target - now()).num_milliseconds();
    if wait > 0 {
        #[allow(clippy::cast_precision_loss)]
        let seconds = wait as f64 / 1000.0;
        sleep(seconds).await;
    }
    let at = now();
    if let Err(e) = chore_body("gtt_sweep", seams, settings, at, counter).await {
        log_line!("ERROR swing_clock clock: the gtt chore could not run: {e}");
        counter.count("gtt", "unavailable");
    }
    0
}
