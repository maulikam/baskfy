//! Ports `app.fno_monitor`'s order-facing half (FO7, M.5): the flag that builds it, the per-user
//! entry plan id, the EXIT/ROLL plans it raises **under the entry's confirm**, and the tick's
//! lapse-and-send pass.
//!
//! **THE FLAG.** [`build_monitor`] returns `None` when `BASKFY_FNO_MONITOR_ENABLED` is false
//! (its default); [`run`] then returns `Ok` without touching the database, Kite or a gateway —
//! Python's `main()` returning 0.
//!
//! **M.5, Maulik's (28 Sep 2026).** An F3 (`CREDIT_SPREAD`) exit is sent by the monitor itself
//! only when `exit_by_monitor(sleeve)` says so — wired to
//! [`crate::lanes::fno::gates::f3_auto_exit_enabled`], `BASKFY_FNO_F3_AUTO_EXIT`, **default false**;
//! otherwise it is written `ISSUED` with `awaits_click=true` and waits for `POST /fno/execute`.
//! The flag gates EXITS only: the monitor never confirms an ENTRY or an ADD (it raises an ADD
//! `ISSUED` for the click), and [`super::execute::run_pending`] sends only EXIT and ROLL plans.
//! Every exit it sends goes through the FO gateway, whose product gates are
//! `fno_gates(sleeve)` with `dry_run` pinned true and read per order (FO7.1): it is simulated,
//! never sent to a broker; `OPTIONS_ENABLED` is the gateway's to enforce.
//!
//! **Not ported here (loud):** the 09:20 plan raising (F1/F2/F3 entries and F3's add), the
//! live-mark decisions of `watch` (F1/F2/F3), and the night's marks and trail. With the flag on,
//! the Rust service refuses (exit 2) rather than run half a monitor; Python keeps the job.

use std::sync::Arc;

use chrono::{DateTime, Duration, FixedOffset, NaiveDate, NaiveTime};

use baskfy_core::fno::config::FoSleeve;
use baskfy_core::fno::exits::{ExitDecision, ExitReason};

use super::execute::{
    ExecError, FUTURE_ROLE, FoCtx, FoOutcome, kind, run_pending, state, structure,
};
use super::store::{FoStore, PositionRow, Val};
use crate::gateway::Gateway;
use crate::lanes::options::py;
use crate::wire::{PyDict, PyValue};

/// `SESSION_OPEN`.
#[must_use]
pub fn session_open() -> NaiveTime {
    NaiveTime::from_hms_opt(9, 15, 0).unwrap_or(NaiveTime::MIN)
}

/// `SESSION_CLOSE`.
#[must_use]
pub fn session_close() -> NaiveTime {
    NaiveTime::from_hms_opt(15, 30, 0).unwrap_or(NaiveTime::MIN)
}

/// `entry_plan_id(sleeve, day, symbol, user_id)`: deterministic and per user (FO7.6).
#[must_use]
pub fn entry_plan_id(sleeve: FoSleeve, day: NaiveDate, symbol: &str, user_id: i64) -> String {
    format!(
        "{}-{}-{symbol}-u{user_id}",
        sleeve.value(),
        day.format("%Y%m%d")
    )
}

/// `ExitReason.value`.
#[must_use]
pub const fn reason_value(reason: ExitReason) -> &'static str {
    match reason {
        ExitReason::LateExit => "LATE_EXIT",
        ExitReason::LossClose => "LOSS_CLOSE",
        ExitReason::ProfitTake => "PROFIT_TAKE",
        ExitReason::HardExit => "HARD_EXIT",
        ExitReason::Stop => "STOP",
        ExitReason::TimeExit => "TIME_EXIT",
        ExitReason::Roll => "ROLL",
        ExitReason::NakedFuture => "NAKED_FUTURE",
        ExitReason::LevelBreak => "LEVEL_BREAK",
        ExitReason::LossCut => "LOSS_CUT",
        ExitReason::DecayTarget => "DECAY_TARGET",
    }
}

/// M.5's rule, the one place it is decided: does this exit wait for the click? Only an F3
/// spread's exit can; it waits unless the monitor may send it (`BASKFY_FNO_F3_AUTO_EXIT`).
/// F1's and F2's exits run under their entry's confirm, as they always have.
#[must_use]
pub fn exit_awaits_click(structure_value: &str, exit_by_monitor: bool) -> bool {
    structure_value == structure::CREDIT_SPREAD && !exit_by_monitor
}

/// The status an exit plan is written in: `ISSUED` for the click, `CONFIRMED` under the entry.
#[must_use]
pub const fn exit_status(by_click: bool) -> &'static str {
    if by_click {
        state::ISSUED
    } else {
        state::CONFIRMED
    }
}

/// `exit_by_monitor`: per sleeve, may the monitor send an F3 exit itself? `main()` wires it to
/// `f3_auto_exit_enabled()` (M.5); the default answers no.
pub type ExitByMonitor = dyn Fn(FoSleeve) -> bool + Send + Sync;

/// `_no_monitor_exit`.
#[must_use]
pub const fn no_monitor_exit(_sleeve: FoSleeve) -> bool {
    false
}

/// The tick's report (`TickReport`): the lapsed, the raised, the actions, the sent.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct TickReport {
    /// Entry plans lapsed.
    pub lapsed: Vec<String>,
    /// Entry plans raised (not ported: always empty here).
    pub raised: Vec<String>,
    /// EXIT/ROLL plans raised.
    pub actions: Vec<String>,
    /// What `run_pending` sent.
    pub done: Vec<FoOutcome>,
}

/// The FO monitor's seams this half needs.
pub struct FnoMonitor<'a> {
    /// The sleeve's gateway.
    pub gateway_for: &'a (dyn Fn(FoSleeve) -> Option<Arc<dyn Gateway>> + Send + Sync),
    /// M.5's per-sleeve answer.
    pub exit_by_monitor: &'a ExitByMonitor,
}

/// `build_monitor(enabled=, **seams)`: the engine, or `None` — with the flag off, not even
/// constructed.
#[must_use]
pub fn build_monitor(enabled: bool, monitor: FnoMonitor<'_>) -> Option<FnoMonitor<'_>> {
    if enabled { Some(monitor) } else { None }
}

fn ist_time(now: DateTime<FixedOffset>) -> NaiveTime {
    now.with_timezone(&py::ist()).time()
}

fn ist_day(now: DateTime<FixedOffset>) -> NaiveDate {
    now.with_timezone(&py::ist()).date_naive()
}

fn leg_text(leg: &PyDict, key: &str) -> String {
    leg.get(key).map(PyValue::py_str).unwrap_or_default()
}

impl FnoMonitor<'_> {
    /// The tick's order-facing part: lapse `ISSUED` entries past their window; inside the
    /// session, send the pending EXIT/ROLL plans (`watch` is not ported: see the module docs).
    ///
    /// # Errors
    /// The database or an exception from a send.
    pub async fn tick_orders(
        &self,
        store: &mut FoStore<'_>,
        ctx: &FoCtx<'_>,
        now: DateTime<FixedOffset>,
    ) -> Result<TickReport, ExecError> {
        let mut report = TickReport {
            lapsed: store.lapse_expired(now).await?,
            ..TickReport::default()
        };
        let t = ist_time(now);
        if session_open() <= t && t < session_close() {
            report.done = run_pending(store, self.gateway_for, ctx).await?;
        }
        Ok(report)
    }

    /// `_raise_exit(position, decision, now)`: the EXIT plan `<entry>-X`, legs in exit order
    /// (shorts first), `CONFIRMED` under the entry's confirm — or, for an F3 spread with the
    /// flag off, `ISSUED` for the click (M.5). `None` when it exists already or nothing is
    /// held.
    ///
    /// # Errors
    /// The database.
    #[allow(clippy::too_many_lines)]
    pub async fn raise_exit(
        &self,
        store: &mut FoStore<'_>,
        ctx: &FoCtx<'_>,
        position: &PositionRow,
        decision: &ExitDecision,
        now: DateTime<FixedOffset>,
    ) -> Result<Option<String>, ExecError> {
        let Some(entry) = store.plan(&position.entry_plan_id).await? else {
            (ctx.alert)(&format!(
                "FO position {}: entry plan {} is gone",
                position.id, position.entry_plan_id
            ));
            return Ok(None);
        };
        let exit_id = format!("{}-X", position.entry_plan_id);
        if store.plan(&exit_id).await?.is_some() {
            return Ok(None);
        }
        let net = store.net_by_symbol(position.id).await?;
        let order: Vec<&str> = baskfy_core::fno::condor::EXIT_SEQUENCE
            .iter()
            .map(|r| r.value())
            .chain(std::iter::once(FUTURE_ROLE))
            .collect();
        // `{tradingsymbol: leg}` (last wins, first position kept), then sorted by role order.
        let mut by_symbol: Vec<(String, PyDict)> = Vec::new();
        for leg in position.leg_list() {
            let sym = leg_text(&leg, "tradingsymbol");
            if let Some(slot) = by_symbol.iter_mut().find(|(s, _)| *s == sym) {
                slot.1 = leg;
            } else {
                by_symbol.push((sym, leg));
            }
        }
        let mut sorted: Vec<PyDict> = Vec::new();
        for (_, leg) in by_symbol {
            let role = leg_text(&leg, "role");
            if !order.contains(&role.as_str()) {
                return Err(ExecError::Other(format!(
                    "ValueError: '{role}' is not in list"
                )));
            }
            sorted.push(leg);
        }
        sorted.sort_by_key(|leg| {
            let role = leg_text(leg, "role");
            order.iter().position(|r| *r == role).unwrap_or(usize::MAX)
        });
        let mut legs: Vec<Vec<(&str, Val)>> = Vec::new();
        for leg in &sorted {
            let sym = leg_text(leg, "tradingsymbol");
            let units = net.iter().find(|(s, _)| *s == sym).map_or(0, |(_, n)| *n);
            if units == 0 {
                continue;
            }
            legs.push(vec![
                ("entry_seq", Val::Int(legs.len() as i64 + 1)),
                ("role", Val::Str(leg_text(leg, "role"))),
                ("tradingsymbol", Val::Str(sym)),
                (
                    "instrument_token",
                    Val::Int(py::py_int(leg.get("instrument_token"), 0)),
                ),
                ("expiry", Val::date(py::opt_day_py(leg.get("expiry")))),
                (
                    "strike",
                    Val::dec(match leg.get("strike") {
                        None | Some(PyValue::None) => None,
                        Some(PyValue::Str(s)) if s.is_empty() => None,
                        Some(v) => py::dec_py(Some(v)),
                    }),
                ),
                ("option_type", Val::Str(leg_text(leg, "option_type"))),
                (
                    "side",
                    Val::Str(if units > 0 { "SELL" } else { "BUY" }.to_owned()),
                ),
                ("quantity", Val::Int(units.abs())),
            ]);
        }
        if legs.is_empty() {
            (ctx.alert)(&format!(
                "FO position {} is open with no units; nothing to close",
                position.id
            ));
            return Ok(None);
        }
        let day = ist_day(now);
        let close = day
            .and_time(session_close())
            .and_local_timezone(py::ist())
            .single()
            .ok_or_else(|| ExecError::Other("ValueError: no 15:30 IST".to_owned()))?;
        let a_minute = now + Duration::minutes(1);
        let expires = if a_minute > close { a_minute } else { close };
        let by_click =
            exit_awaits_click(&position.structure, (self.exit_by_monitor)(position.sleeve));
        let code = reason_value(decision.reason);
        let detail = crate::wire::dict([
            ("code", PyValue::Str(code.to_owned())),
            ("reason", PyValue::Str(decision.message.clone())),
            ("position_id", PyValue::Int(position.id)),
            ("under_confirm_of", PyValue::Str(entry.plan_id.clone())),
            ("awaits_click", PyValue::Bool(by_click)),
        ]);
        let head: Vec<(&str, Val)> = vec![
            ("plan_id", Val::Str(exit_id.clone())),
            ("sleeve", Val::Str(position.sleeve.value().to_owned())),
            ("symbol", Val::Str(position.symbol.clone())),
            ("trade_date", Val::Date(day)),
            ("structure", Val::Str(position.structure.clone())),
            ("kind", Val::Str(kind::EXIT.to_owned())),
            ("sizing_mode", Val::Str("BUDGET".to_owned())),
            ("issued_at", Val::At(now)),
            ("entry_window_end", Val::None),
            ("expires_at", Val::At(expires)),
            ("lots", Val::Int(position.lots)),
            ("lot_size", Val::Int(position.lot_size)),
            ("status", Val::Str(exit_status(by_click).to_owned())),
            ("reason", Val::Str(decision.message.clone())),
            ("detail", Val::Json(PyValue::Map(detail))),
            ("parent_plan_id", Val::Str(entry.plan_id.clone())),
            (
                "confirmed_at",
                if by_click {
                    Val::None
                } else {
                    entry.confirmed_at.map_or(Val::None, Val::At)
                },
            ),
            ("hard_exit_date", Val::date(position.hard_exit_date)),
        ];
        store.insert_plan(&head, &legs).await?;
        if by_click {
            (ctx.alert)(&format!(
                "F3 {}: {code} — {}; the exit {exit_id} waits for the click on /fno (the monitor \
                 may not send it)",
                position.symbol, decision.message
            ));
        }
        if decision.reason == ExitReason::LateExit {
            store
                .add_violation(&entry.plan_id, "LATE_EXIT", &decision.message, now)
                .await?;
            (ctx.alert)(&format!(
                "FO {}: LATE_EXIT — {}",
                entry.plan_id, decision.message
            ));
        }
        Ok(Some(exit_id))
    }
}

/// `python -m app.fno_monitor` as a desk service: `Ok` and nothing touched with
/// `BASKFY_FNO_MONITOR_ENABLED` off (its default); with it on, a refusal — the Rust monitor is
/// half-ported (module docs) and Python keeps the job until it is whole.
///
/// # Errors
/// The flag on.
pub fn run(settings: &crate::settings::Settings) -> Result<(), String> {
    if !settings.fno_monitor_enabled {
        return Ok(());
    }
    Err(
        "fno-monitor: the Rust FO monitor is not whole yet (plan raising, watch and the night \
         are not ported); the Python monitor keeps the job"
            .to_owned(),
    )
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
    use super::*;

    #[test]
    fn entry_plan_ids_are_deterministic_and_per_user() {
        let day = NaiveDate::from_ymd_opt(2026, 11, 12).unwrap_or_default();
        assert_eq!(
            entry_plan_id(FoSleeve::F3N, day, "NIFTY", 7),
            "F3N-20261112-NIFTY-u7"
        );
        assert_ne!(
            entry_plan_id(FoSleeve::F1N, day, "NIFTY", 1),
            entry_plan_id(FoSleeve::F1N, day, "NIFTY", 2)
        );
    }

    #[test]
    fn only_an_f3_exit_can_wait_for_the_click() {
        // Flag off (the default): the F3 exit waits; F1's and F2's run under the entry.
        assert!(exit_awaits_click(structure::CREDIT_SPREAD, false));
        assert!(!exit_awaits_click(structure::IRON_CONDOR, false));
        assert!(!exit_awaits_click(structure::FUTURE, false));
        // Flag on: the monitor sends the F3 exit itself.
        assert!(!exit_awaits_click(structure::CREDIT_SPREAD, true));
        assert_eq!(exit_status(true), "ISSUED");
        assert_eq!(exit_status(false), "CONFIRMED");
    }

    #[test]
    fn the_default_seam_never_lets_the_monitor_exit() {
        for s in FoSleeve::ALL {
            assert!(!no_monitor_exit(s));
        }
    }
}
