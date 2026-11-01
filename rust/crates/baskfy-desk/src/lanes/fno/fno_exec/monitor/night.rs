//! The night (`FnoMonitor.nightly`): one `fo_mark` per open position per session at the
//! settle, F2's trail moved up (never lower) with its GTT modified **through the gateway**, then
//! `04` §7's pauses for each mode. Idempotent: a re-run writes nothing new.

use chrono::NaiveDate;
use rust_decimal::Decimal;

use baskfy_core::fno::condor::{LegRole, RoleMap, loss_close_hit, profit_take_hit};
use baskfy_core::fno::directional as d3;
use baskfy_core::fno::monitor::{f1_close_cost, f1_pnl_inr, f2_pnl_inr, f2_trail_step};

use super::super::execute::{
    ExecError, FUTURE_TYPE, FoCtx, move_future_stop, paper_book, structure,
};
use super::super::store::{FoStore, PositionRow, Val, money};
use super::engine::{
    FnoMonitor, NightReport, fe as fno_err, get_text as text_of, ist_day as day_of,
    or_dec as dec_or, role_of as leg_role, strikes_of as condor_strikes,
};
use super::seams::PrintKey;
use crate::lanes::options::py;
use crate::wire::{PyDict, PyValue, dict};

fn ds(d: Decimal) -> PyValue {
    PyValue::Str(py::dstr(d))
}

impl FnoMonitor {
    /// `nightly(day)`.
    ///
    /// # Errors
    /// The database, a seam, the arithmetic.
    pub async fn nightly(
        &self,
        store: &mut FoStore<'_>,
        day: NaiveDate,
    ) -> Result<NightReport, ExecError> {
        if !self.market.landed(day).await? {
            return Ok(NightReport::default());
        }
        let mut report = NightReport {
            landed: true,
            ..NightReport::default()
        };
        let sessions: Vec<NaiveDate> = self
            .market
            .sessions()
            .await?
            .into_iter()
            .filter(|d| *d <= day)
            .collect();
        for opened_position in store.open_positions().await? {
            let mut position = opened_position;
            let opened = day_of(position.opened_at);
            let done = store.marked_days(position.id).await?;
            for session in sessions
                .iter()
                .copied()
                .filter(|d| *d >= opened && !done.contains(d))
            {
                if !self.market.landed(session).await? {
                    continue;
                }
                let ok = if position.structure == structure::IRON_CONDOR {
                    self.mark_f1(store, &position, session).await?
                } else if position.structure == structure::CREDIT_SPREAD {
                    self.mark_f3(store, &position, session).await?
                } else {
                    let ok = self.mark_f2(store, &position, session, &mut report).await?;
                    if let Some(fresh) = store.position(position.id).await? {
                        position = fresh; // the trail moved
                    }
                    ok
                };
                if ok {
                    report.marked += 1;
                } else {
                    report
                        .missing
                        .push(format!("{}@{}", position.id, session.format("%Y-%m-%d")));
                }
            }
        }
        report.pauses = crate::lanes::fno::ledger::nightly_pauses(store, day)
            .await?
            .iter()
            .map(|p| format!("{}:{}", p.reason(), p.scope))
            .collect();
        Ok(report)
    }

    fn print_key(position: &PositionRow, leg: &PyDict) -> Option<PrintKey> {
        let expiry = py::opt_day_py(leg.get("expiry"))?;
        Some((
            position.symbol.clone(),
            expiry,
            py::dec_py(leg.get("strike")),
            text_of(leg, "option_type"),
        ))
    }

    async fn mark_f1(
        &self,
        store: &mut FoStore<'_>,
        position: &PositionRow,
        day: NaiveDate,
    ) -> Result<bool, ExecError> {
        let (Some(strikes), Some(credit)) =
            (condor_strikes(&position.carry()), position.entry_credit)
        else {
            return Ok(false);
        };
        let mut keys: Vec<(LegRole, PrintKey)> = Vec::new();
        for leg in position.leg_list() {
            let Some(key) = Self::print_key(position, &leg) else {
                return Ok(false);
            };
            let role = leg_role(&text_of(&leg, "role"))?;
            if let Some(slot) = keys.iter_mut().find(|(r, _)| *r == role) {
                slot.1 = key;
            } else {
                keys.push((role, key));
            }
        }
        let wanted: Vec<PrintKey> = keys.iter().map(|(_, k)| k.clone()).collect();
        let prints = self.market.prints(day, &wanted).await?;
        let mut settles = RoleMap::new();
        for (role, k) in &keys {
            if let Some(p) = prints.get(k) {
                settles.insert(*role, p.settle);
            }
        }
        let Some(close_cost) = f1_close_cost(&strikes, &settles).map_err(fno_err)? else {
            return Ok(false);
        };
        let pnl =
            f1_pnl_inr(credit, close_cost, position.lots * position.lot_size).map_err(fno_err)?;
        let f1 = &self.config.f1;
        let mut settles_json = PyDict::new();
        for (role, k) in &keys {
            if let Some(p) = prints.get(k) {
                settles_json.insert(role.value(), ds(p.settle));
            }
        }
        let detail = dict([
            ("settles", PyValue::Map(settles_json)),
            (
                "profit_take_hit",
                PyValue::Bool(profit_take_hit(close_cost, credit, f1).map_err(fno_err)?),
            ),
            (
                "loss_close_hit",
                PyValue::Bool(loss_close_hit(close_cost, credit, &strikes, f1).map_err(fno_err)?),
            ),
            (
                "note",
                PyValue::Str(
                    "the settle is a mark; exits fire on the live check (04 §1)".to_owned(),
                ),
            ),
        ]);
        store
            .write_mark(position.id, day, close_cost, pnl, None, &detail)
            .await?;
        Ok(true)
    }

    async fn mark_f3(
        &self,
        store: &mut FoStore<'_>,
        position: &PositionRow,
        day: NaiveDate,
    ) -> Result<bool, ExecError> {
        let Some(spread) = Self::f3_open(position)? else {
            return Ok(false);
        };
        let mut keys: Vec<(String, PrintKey)> = Vec::new();
        for leg in position.leg_list() {
            let Some(key) = Self::print_key(position, &leg) else {
                return Ok(false);
            };
            let role = text_of(&leg, "role");
            if let Some(slot) = keys.iter_mut().find(|(r, _)| *r == role) {
                slot.1 = key;
            } else {
                keys.push((role, key));
            }
        }
        let wanted: Vec<PrintKey> = keys.iter().map(|(_, k)| k.clone()).collect();
        let prints = self.market.prints(day, &wanted).await?;
        let pick = |prefix: &str| {
            keys.iter()
                .find(|(r, _)| r.starts_with(prefix))
                .and_then(|(_, k)| prints.get(k))
        };
        let Some(short) = pick("SHORT") else {
            return Ok(false);
        };
        let wing = pick("LONG").map_or(Decimal::ZERO, |p| p.settle);
        let mark = short.settle - wing;
        let pnl = d3::pnl_inr(spread.entry_credit, mark, position.lots * position.lot_size)
            .map_err(fno_err)?;
        let f3 = &self.config.f3;
        let mut settles_json = PyDict::new();
        for (role, k) in &keys {
            if let Some(p) = prints.get(k) {
                settles_json.insert(role.clone(), PyValue::Str(py::dstr(p.settle)));
            }
        }
        let detail = dict([
            ("settles", PyValue::Map(settles_json)),
            (
                "decay_target_hit",
                PyValue::Bool(
                    mark <= d3::decay_target_mark(spread.entry_credit, f3).map_err(fno_err)?,
                ),
            ),
            (
                "loss_cut_hit",
                PyValue::Bool(mark >= d3::loss_cut_mark(spread.entry_credit, f3).map_err(fno_err)?),
            ),
            (
                "note",
                PyValue::Str(
                    "the settle is a mark; exits fire on the live check (04 §11)".to_owned(),
                ),
            ),
        ]);
        store
            .write_mark(
                position.id,
                day,
                money(mark),
                pnl,
                position.stop_price,
                &detail,
            )
            .await?;
        Ok(true)
    }

    async fn mark_f2(
        &self,
        store: &mut FoStore<'_>,
        position: &PositionRow,
        day: NaiveDate,
        report: &mut NightReport,
    ) -> Result<bool, ExecError> {
        let legs = position.leg_list();
        let Some(leg) = legs.first() else {
            return Ok(false);
        };
        let expiry = py::opt_day_py(leg.get("expiry"));
        let symbol = text_of(leg, "tradingsymbol");
        let Some(expiry) = expiry else {
            return Ok(false);
        };
        let key: PrintKey = (
            position.symbol.clone(),
            expiry,
            None,
            FUTURE_TYPE.to_owned(),
        );
        let prints = self.market.prints(day, std::slice::from_ref(&key)).await?;
        let Some(found) = prints.get(&key).copied() else {
            return Ok(false);
        };
        let mut carry = position.carry();
        let quantity = py::py_int(leg.get("quantity"), position.lots * position.lot_size);
        let current_entry = dec_or(
            py::dec_py(leg.get("avg_price")),
            dec_or(position.entry_price, Decimal::ZERO),
        );
        let realised = dec_or(py::dec_py(carry.get("realised_inr")), Decimal::ZERO);
        let pnl = realised + f2_pnl_inr(current_entry, found.settle, quantity).map_err(fno_err)?;
        let old_trigger = position.stop_price.unwrap_or(Decimal::ZERO);
        let ann_vol = match carry.get("ann_vol") {
            Some(PyValue::Float(f)) if *f != 0.0 => *f,
            Some(PyValue::Int(i)) if *i != 0 => {
                #[allow(clippy::cast_precision_loss)]
                let f = *i as f64;
                f
            }
            Some(PyValue::Str(s)) if !s.is_empty() => s.trim().parse::<f64>().map_err(|_| {
                ExecError::Other(format!(
                    "ValueError: could not convert string to float: '{s}'"
                ))
            })?,
            _ => 0.0,
        };
        let step = f2_trail_step(
            dec_or(py::dec_py(carry.get("trail_stop")), old_trigger),
            old_trigger,
            dec_or(py::dec_py(carry.get("highest_close")), current_entry),
            found.close,
            dec_or(position.entry_price, current_entry),
            dec_or(py::dec_py(carry.get("atr_at_entry")), Decimal::ZERO),
            ann_vol,
            &self.config,
        )
        .map_err(fno_err)?;
        let detail = dict([
            ("settle", ds(found.settle)),
            ("close", ds(found.close)),
            ("trail_stop", ds(step.stop)),
            ("highest_close", ds(step.highest_close)),
            ("moved", PyValue::Bool(step.moved)),
        ]);
        let fresh = store
            .write_mark(
                position.id,
                day,
                found.settle,
                pnl,
                Some(step.trigger),
                &detail,
            )
            .await?;
        if !fresh {
            return Ok(true);
        }
        carry.insert("trail_stop", ds(step.stop));
        carry.insert("highest_close", ds(step.highest_close));
        let legs_json = PyValue::Map(dict([
            (
                "legs",
                PyValue::List(legs.iter().cloned().map(PyValue::Map).collect()),
            ),
            ("carry", PyValue::Map(carry)),
        ]));
        store
            .update_position(position.id, &[("legs", Val::Json(legs_json))])
            .await?;
        if step.moved && position.gtt_id.is_some() {
            let gateway = self.gateway_or_err(position.sleeve)?;
            let now = py::ist_at(
                day,
                chrono::NaiveTime::from_hms_opt(15, 30, 0).unwrap_or_default(),
            )
            .ok_or_else(|| ExecError::Other("ValueError: no such time".to_owned()))?;
            let now_fn = move || now;
            let book = |filled: &[baskfy_core::fno::covered::OptionPosition]| paper_book(filled);
            let ctx = FoCtx {
                quotes: &*self.quotes,
                now: &now_fn,
                broker_book: &book,
                // `move_future_stop` logs a refused modify (`log.error`); it is not the
                // monitor's alert.
                alert: &super::engine::log_error,
                ledger: &*self.ledger,
                tenant: self.tenant,
            };
            let status = move_future_stop(
                store,
                &gateway,
                position,
                &symbol,
                quantity,
                step.trigger,
                old_trigger,
                found.close,
                &ctx,
            )
            .await?;
            report.trailed.push(format!(
                "{}:{}->{}:{status}",
                position.symbol,
                py::dstr(old_trigger),
                py::dstr(step.trigger)
            ));
        }
        Ok(true)
    }
}
