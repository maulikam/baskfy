//! Ports `app.fno_ledger` (`docs/fno/06` FO10): the FO journal and `04` §7's pauses on the desk.
//!
//! "Every close writes the record, and the record governs tomorrow."
//!
//! * [`write_journal`] — one `fo_journal` row per closed position, from its own fills
//!   (`baskfy_core::fno::journal::journal_figures`); idempotent on `position_id`.
//!   [`journal_abandoned`] records an entry abandoned after some legs filled (FO10.4).
//! * [`apply_pauses`] — `04` §7 over the closed trades of **the same mode only** (FO10.1); F2's
//!   and the book's pauses written to their rows (never shortened, never over an active pause)
//!   and audited; F1's per underlying recorded as an audit row (FO10.2).
//! * [`entry_block`] — the reasons, in words, that stop a new entry. A pause stops new entries
//!   only; nothing here closes or sends anything.

use std::collections::BTreeMap;

use async_trait::async_trait;

use chrono::{DateTime, Datelike, Duration, FixedOffset, NaiveDate, NaiveDateTime};
use rust_decimal::Decimal;

use baskfy_core::fno::config::{FnoCeilings, FnoConfig, FoSleeve, PlanKind, Structure, group_of};
use baskfy_core::fno::journal::{
    CloseKind, CostLines, FoFillRecord, FoJournalFigures, journal_figures,
};
use baskfy_core::fno::ledger::{
    ClosedTrade, FoPause, column_pause_applies, evaluate_pauses, pauses_for,
};
use baskfy_core::options::costs::{CostRates, Side};

use super::fno_exec::execute::{Alert, ExecError, FoLedger};
use super::fno_exec::store::{FoPlanRow, FoStore, Val, sleeve_of};
use crate::lanes::options::py::{self, col, int, text};
use crate::lanes::options::store::StoreError;
use crate::storage::{Param, Row};
use crate::wire::{PyDict, PyValue, dict};

/// `LIFT_KEY`.
pub const LIFT_KEY: &str = "lift:";
/// `PAUSE_KEY`.
pub const PAUSE_KEY: &str = "pause:";
const F1: [FoSleeve; 2] = [FoSleeve::F1N, FoSleeve::F1B];

fn data(msg: impl Into<String>) -> StoreError {
    StoreError::Data(msg.into())
}

fn naive_ist(at: DateTime<FixedOffset>) -> NaiveDateTime {
    at.with_timezone(&py::ist()).naive_local()
}

fn plan_kind(s: &str) -> Result<PlanKind, StoreError> {
    serde_json::from_value(serde_json::Value::String(s.to_owned()))
        .map_err(|_| data(format!("ValueError: '{s}' is not a valid PlanKind")))
}

fn structure_of(s: &str) -> Result<Structure, StoreError> {
    serde_json::from_value(serde_json::Value::String(s.to_owned()))
        .map_err(|_| data(format!("ValueError: '{s}' is not a valid Structure")))
}

fn side_of(s: &str) -> Result<Side, StoreError> {
    match s {
        "BUY" => Ok(Side::Buy),
        "SELL" => Ok(Side::Sell),
        _ => Err(data(format!("ValueError: '{s}' is not a valid Side"))),
    }
}

fn need_dec(v: &crate::storage::DbValue, what: &str) -> Result<Decimal, StoreError> {
    py::dec(v).ok_or_else(|| data(format!("InvalidOperation: {what}")))
}

// --- the journal ---------------------------------------------------------------------------

async fn fills(store: &mut FoStore<'_>, position_id: i64) -> Result<Vec<FoFillRecord>, StoreError> {
    let sql = format!(
        "SELECT p.plan_id, p.kind, f.side, f.price, f.quantity, f.filled_at \
         FROM {} f JOIN {} l ON l.id = f.leg_id \
         JOIN {} p ON p.id = l.plan_id \
         WHERE f.user_id = ? AND f.position_id = ? ORDER BY f.filled_at, f.id",
        store.t("fo_fill"),
        store.t("fo_leg"),
        store.t("fo_plan")
    );
    let uid = store.user_id;
    let rows = store
        .rows(&sql, &[Param::Int(uid), Param::Int(position_id)])
        .await?;
    let mut out = Vec::with_capacity(rows.len());
    for r in &rows {
        let at = py::aware(col(r, "filled_at"))
            .ok_or_else(|| data("TypeError: filled_at is not a datetime"))?;
        out.push(FoFillRecord {
            plan_id: text(col(r, "plan_id")),
            kind: plan_kind(&text(col(r, "kind")))?,
            side: side_of(&text(col(r, "side")))?,
            price: need_dec(col(r, "price"), "price")?,
            quantity: int(col(r, "quantity")),
            filled_at: naive_ist(at),
        });
    }
    Ok(out)
}

async fn marks(store: &mut FoStore<'_>, position_id: i64) -> Result<Vec<Decimal>, StoreError> {
    let sql = format!(
        "SELECT pnl_inr FROM {} WHERE user_id = ? AND position_id = ? ORDER BY trade_date",
        store.t("fo_mark")
    );
    let uid = store.user_id;
    let rows = store
        .rows(&sql, &[Param::Int(uid), Param::Int(position_id)])
        .await?;
    rows.iter()
        .map(|r| need_dec(col(r, "pnl_inr"), "pnl_inr"))
        .collect()
}

/// `sessions_between(store, first, last)`: exchange sessions inclusive (`trading_day`; weekdays
/// when the calendar has no rows).
///
/// # Errors
/// The database.
pub async fn sessions_between(
    store: &mut FoStore<'_>,
    first: NaiveDate,
    last: NaiveDate,
) -> Result<i64, StoreError> {
    let sql = format!(
        "SELECT count(*) AS n FROM {} WHERE is_trading_day AND date BETWEEN ? AND ?",
        store.t("trading_day")
    );
    let n = store
        .one(&sql, &[Param::Date(first), Param::Date(last)])
        .await?
        .map_or(0, |r| int(col(&r, "n")));
    if n != 0 {
        return Ok(n);
    }
    let days = (last - first).num_days() + 1;
    Ok((0..days.max(0))
        .filter(|i| {
            (first + Duration::days(*i))
                .weekday()
                .num_days_from_monday()
                < 5
        })
        .count()
        .try_into()
        .unwrap_or(0))
}

async fn plan_max_loss(
    store: &mut FoStore<'_>,
    plan: &FoPlanRow,
) -> Result<Option<Decimal>, StoreError> {
    let sql = format!(
        "SELECT max_loss_inr, risk_per_lot_inr, lots FROM {} WHERE id = ?",
        store.t("fo_plan")
    );
    let Some(row) = store.one(&sql, &[Param::Int(plan.pk)]).await? else {
        return Ok(None);
    };
    if !col(&row, "max_loss_inr").is_null() {
        return Ok(py::dec(col(&row, "max_loss_inr")));
    }
    if !col(&row, "risk_per_lot_inr").is_null() {
        let per = need_dec(col(&row, "risk_per_lot_inr"), "risk_per_lot_inr")?;
        return Ok(per.checked_mul(Decimal::from(int(col(&row, "lots")))));
    }
    Ok(None)
}

async fn risk(
    store: &mut FoStore<'_>,
    position: &Row,
    plan: Option<&FoPlanRow>,
) -> Result<Option<Decimal>, StoreError> {
    let planned = match plan {
        None => None,
        Some(p) => plan_max_loss(store, p).await?,
    };
    for value in [py::dec(col(position, "max_loss_inr")), planned]
        .into_iter()
        .flatten()
    {
        if value > Decimal::ZERO {
            return Ok(Some(value));
        }
    }
    Ok(None)
}

async fn sizing_mode(
    store: &mut FoStore<'_>,
    plan: Option<&FoPlanRow>,
) -> Result<String, StoreError> {
    let Some(plan) = plan else {
        return Ok("BUDGET".to_owned());
    };
    let sql = format!(
        "SELECT sizing_mode FROM {} WHERE id = ?",
        store.t("fo_plan")
    );
    Ok(store
        .one(&sql, &[Param::Int(plan.pk)])
        .await?
        .map_or_else(|| "BUDGET".to_owned(), |r| text(col(&r, "sizing_mode"))))
}

fn cost_lines_json(c: &CostLines) -> PyValue {
    let total = c
        .total()
        .map_or_else(|_| PyValue::None, |t| PyValue::Str(py::dstr(t)));
    PyValue::Map(dict([
        ("orders", PyValue::Int(c.orders)),
        ("brokerage", PyValue::Str(py::dstr(c.brokerage))),
        ("stt", PyValue::Str(py::dstr(c.stt))),
        ("exchange_txn", PyValue::Str(py::dstr(c.exchange_txn))),
        ("sebi", PyValue::Str(py::dstr(c.sebi))),
        ("ipft", PyValue::Str(py::dstr(c.ipft))),
        ("stamp", PyValue::Str(py::dstr(c.stamp))),
        ("gst", PyValue::Str(py::dstr(c.gst))),
        ("total", total),
    ]))
}

/// `figures.cost_detail()` in Python's key order.
#[must_use]
pub fn cost_detail_json(f: &FoJournalFigures) -> PyValue {
    let d = f.cost_detail();
    PyValue::Map(dict([
        ("entry", cost_lines_json(&d.entry)),
        ("exit", cost_lines_json(&d.exit)),
        (
            "rolls",
            PyValue::List(
                d.rolls
                    .iter()
                    .map(|r| {
                        PyValue::Map(dict([
                            ("plan_id", PyValue::Str(r.plan_id.clone())),
                            ("on", PyValue::Str(r.on.format("%Y-%m-%d").to_string())),
                            ("sold_inr", PyValue::Str(py::dstr(r.sold_inr))),
                            ("bought_inr", PyValue::Str(py::dstr(r.bought_inr))),
                            ("costs", cost_lines_json(&r.costs)),
                        ]))
                    })
                    .collect(),
            ),
        ),
        ("total", PyValue::Str(py::dstr(d.total))),
    ]))
}

/// `write_journal(store, position_id, alert=, option_rates=, config=)`: one `fo_journal` row for
/// a closed position; `None` (and an alert) when it cannot be written.
///
/// # Errors
/// The database, or the figures (Python raises them into [`after_close`]).
pub async fn write_journal(
    store: &mut FoStore<'_>,
    position_id: i64,
    alert: &Alert,
    option_rates: Option<&CostRates>,
    config: &FnoConfig,
) -> Result<Option<FoJournalFigures>, StoreError> {
    let sql = format!(
        "SELECT * FROM {} WHERE user_id = ? AND id = ?",
        store.t("fo_position")
    );
    let uid = store.user_id;
    let Some(pos) = store
        .one(&sql, &[Param::Int(uid), Param::Int(position_id)])
        .await?
    else {
        return Ok(None);
    };
    if col(&pos, "closed_at").is_null() {
        return Ok(None);
    }
    let plan = store.plan(&text(col(&pos, "entry_plan_id"))).await?;
    let Some(r_inr) = risk(store, &pos, plan.as_ref()).await? else {
        alert(&format!(
            "FO position {position_id}: no planned max loss to measure R by; not journalled"
        ));
        return Ok(None);
    };
    let fills = fills(store, position_id).await?;
    let structure = structure_of(&text(col(&pos, "structure")))?;
    let marks = marks(store, position_id).await?;
    let default_rates = CostRates::default();
    let figures = journal_figures(
        structure,
        &fills,
        r_inr,
        &marks,
        option_rates.unwrap_or(&default_rates),
        &config.future_costs,
    )
    .map_err(|e| data(e.to_string()))?;
    let ist = py::ist();
    let opened_on = py::aware(col(&pos, "opened_at"))
        .ok_or_else(|| data("TypeError: opened_at is not a datetime"))?
        .with_timezone(&ist)
        .date_naive();
    let closed_on = py::aware(col(&pos, "closed_at"))
        .ok_or_else(|| data("TypeError: closed_at is not a datetime"))?
        .with_timezone(&ist)
        .date_naive();
    let legs = py::json_obj(col(&pos, "legs"));
    let carry_rolls = match legs.get("carry") {
        Some(PyValue::Map(c)) => py::py_int(c.get("rolls"), 0),
        _ => 0,
    };
    let rolls = if figures.rolls.is_empty() {
        carry_rolls
    } else {
        i64::try_from(figures.rolls.len()).unwrap_or(0)
    };
    let violations = match plan.as_ref().and_then(|p| p.detail.get("violations")) {
        Some(v @ PyValue::List(items)) if !items.is_empty() => v.clone(),
        _ => PyValue::List(Vec::new()),
    };
    let detail = dict([
        ("costs", cost_detail_json(&figures)),
        ("entry_plan_id", py::db_to_py(col(&pos, "entry_plan_id"))),
        ("lots", PyValue::Int(int(col(&pos, "lots")))),
        ("lot_size", PyValue::Int(int(col(&pos, "lot_size")))),
        ("violations", violations),
        (
            "r_source",
            PyValue::Str("fo_position.max_loss_inr".to_owned()),
        ),
    ]);
    let reason = match py::opt_text(col(&pos, "closed_reason")) {
        Some(r) if !r.is_empty() => r,
        _ => "EXIT".to_owned(),
    };
    let reason: String = reason.chars().take(32).collect();
    let sessions = sessions_between(store, opened_on, closed_on).await?;
    let sizing = sizing_mode(store, plan.as_ref()).await?;
    let sql = format!(
        "INSERT INTO {} (position_id, user_id, sleeve, symbol, structure, \
         opened_on, closed_on, entry_inr, exit_inr, gross_pnl_inr, costs_inr, net_pnl_inr, \
         risk_budget_inr, r_multiple, closed_reason, sessions_held, rolls, mae_r, mfe_r, \
         simulated, sizing_mode, detail) VALUES \
         (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) \
         ON CONFLICT (position_id) DO NOTHING RETURNING position_id",
        store.t("fo_journal")
    );
    let params = [
        Param::Int(position_id),
        Param::Int(uid),
        Param::Text(text(col(&pos, "sleeve"))),
        Param::Text(text(col(&pos, "symbol"))),
        Param::Text(text(col(&pos, "structure"))),
        Param::Date(opened_on),
        Param::Date(closed_on),
        Param::Decimal(figures.entry_inr),
        Param::Decimal(figures.exit_inr),
        Param::Decimal(figures.gross_pnl_inr),
        Param::Decimal(figures.costs_inr),
        Param::Decimal(figures.net_pnl_inr),
        Param::Decimal(figures.r_inr),
        Param::Decimal(figures.r_multiple),
        Param::Text(reason),
        Param::Int(sessions),
        Param::Int(rolls),
        py::pdec(figures.mae_r),
        py::pdec(figures.mfe_r),
        Param::Bool(py::truthy(col(&pos, "simulated"))),
        Param::Text(sizing),
        Param::Text(py::dumps(&PyValue::Map(detail))),
    ];
    let row = store.one(&sql, &params).await?;
    Ok(row.map(|_| figures))
}

/// `journal_abandoned(store, plan, at)` (FO10.4): a position row opened at the first unattached
/// fill and closed at `at` (`ABANDONED_PARTIAL`), its fills attached. `None` when nothing filled.
///
/// # Errors
/// The database.
pub async fn journal_abandoned(
    store: &mut FoStore<'_>,
    plan: &FoPlanRow,
    at: DateTime<FixedOffset>,
) -> Result<Option<i64>, StoreError> {
    let sql = format!(
        "SELECT min(f.filled_at) AS first FROM {} f \
         JOIN {} l ON l.id = f.leg_id WHERE l.plan_id = ? \
         AND f.position_id IS NULL",
        store.t("fo_fill"),
        store.t("fo_leg")
    );
    let first = store.one(&sql, &[Param::Int(plan.pk)]).await?;
    let Some(first) = first.and_then(|r| py::aware(col(&r, "first"))) else {
        return Ok(None);
    };
    let max_loss = plan_max_loss(store, plan).await?;
    let legs = PyValue::Map(dict([
        ("legs", PyValue::List(Vec::new())),
        (
            "carry",
            PyValue::Map(dict([("abandoned", PyValue::Bool(true))])),
        ),
    ]));
    let position_id = store
        .open_position(&[
            ("sleeve", Val::Str(plan.sleeve.value().to_owned())),
            ("symbol", Val::Str(plan.symbol.clone())),
            ("structure", Val::Str(plan.structure.clone())),
            ("entry_plan_id", Val::Str(plan.plan_id.clone())),
            ("legs", Val::Json(legs)),
            ("lots", Val::Int(plan.lots)),
            ("lot_size", Val::Int(plan.lot_size)),
            ("max_loss_inr", Val::dec(max_loss)),
            ("opened_at", Val::At(first)),
            ("simulated", Val::Bool(true)),
        ])
        .await?;
    store.attach_fills(plan, position_id).await?;
    store
        .close_position(position_id, CloseKind::AbandonedPartial.value(), at)
        .await?;
    Ok(Some(position_id))
}

// --- the pauses ----------------------------------------------------------------------------

/// `load_trades(store)`.
///
/// # Errors
/// The database, or a row Python would refuse.
pub async fn load_trades(store: &mut FoStore<'_>) -> Result<Vec<ClosedTrade>, StoreError> {
    let sql = format!(
        "SELECT j.position_id, j.sleeve, j.symbol, j.simulated, j.closed_on, p.closed_at, \
         j.net_pnl_inr, j.r_multiple, j.closed_reason, j.rolls \
         FROM {} j JOIN {} p ON p.id = j.position_id \
         WHERE j.user_id = ? ORDER BY j.closed_on, j.position_id",
        store.t("fo_journal"),
        store.t("fo_position")
    );
    let uid = store.user_id;
    let rows = store.rows(&sql, &[Param::Int(uid)]).await?;
    let mut out = Vec::with_capacity(rows.len());
    for r in &rows {
        let sleeve_text = text(col(r, "sleeve"));
        let sleeve = sleeve_of(&sleeve_text).ok_or_else(|| {
            data(format!(
                "ValueError: '{sleeve_text}' is not a valid FoSleeve"
            ))
        })?;
        out.push(ClosedTrade {
            position_id: int(col(r, "position_id")),
            sleeve,
            symbol: text(col(r, "symbol")),
            simulated: py::truthy(col(r, "simulated")),
            closed_on: py::opt_day(col(r, "closed_on"))
                .ok_or_else(|| data("TypeError: closed_on is not a date"))?,
            closed_at: py::aware(col(r, "closed_at")).map(naive_ist),
            net_pnl_inr: need_dec(col(r, "net_pnl_inr"), "net_pnl_inr")?,
            r_multiple: need_dec(col(r, "r_multiple"), "r_multiple")?,
            closed_reason: text(col(r, "closed_reason")),
            rolls: int(col(r, "rolls")),
        });
    }
    Ok(out)
}

/// `load_lifts(store)`: the last lift of each F1 sleeve's pause.
///
/// # Errors
/// The database.
pub async fn load_lifts(
    store: &mut FoStore<'_>,
) -> Result<BTreeMap<FoSleeve, DateTime<FixedOffset>>, StoreError> {
    let mut found = BTreeMap::new();
    for sleeve in F1 {
        let sql = format!(
            "SELECT max(changed_at) AS at FROM {} WHERE user_id = ? AND key = ?",
            store.t("fo_config_audit")
        );
        let uid = store.user_id;
        let key = format!("{LIFT_KEY}{}", sleeve.value());
        if let Some(r) = store
            .one(&sql, &[Param::Int(uid), Param::Text(key)])
            .await?
            && let Some(at) = py::aware(col(&r, "at"))
        {
            found.insert(sleeve, at);
        }
    }
    Ok(found)
}

async fn book_amount(store: &mut FoStore<'_>) -> Result<Decimal, StoreError> {
    let sql = format!(
        "SELECT monthly_pause_inr FROM {} WHERE user_id = ?",
        store.t("fo_book_config")
    );
    let uid = store.user_id;
    match store.one(&sql, &[Param::Int(uid)]).await? {
        None => Ok(Decimal::ZERO),
        Some(r) => need_dec(col(&r, "monthly_pause_inr"), "monthly_pause_inr"),
    }
}

/// `evaluate(store, simulated=, day=)`: `04` §7 over this user's journal, one mode.
///
/// # Errors
/// The database.
pub async fn evaluate(
    store: &mut FoStore<'_>,
    simulated: bool,
    day: NaiveDate,
    config: &FnoConfig,
    ceilings: &FnoCeilings,
) -> Result<Vec<FoPause>, StoreError> {
    let trades = load_trades(store).await?;
    let amount = book_amount(store).await?;
    let lifts: BTreeMap<FoSleeve, NaiveDateTime> = load_lifts(store)
        .await?
        .into_iter()
        .map(|(k, v)| (k, naive_ist(v)))
        .collect();
    evaluate_pauses(
        &trades,
        simulated,
        day,
        amount,
        Some(&lifts),
        config,
        ceilings,
    )
    .map_err(|e| data(e.to_string()))
}

async fn audit(
    store: &mut FoStore<'_>,
    scope: &str,
    key: &str,
    old: Option<String>,
    new: Option<String>,
    note: &str,
) -> Result<(), StoreError> {
    let sql = format!(
        "INSERT INTO {} (user_id, scope, key, old_value, new_value, \
         changed_by, note) VALUES (?, ?, ?, ?, ?, 'ledger', ?)",
        store.t("fo_config_audit")
    );
    let uid = store.user_id;
    let opt = |v: Option<String>| v.map_or(Param::Null, Param::Text);
    store
        .conn
        .execute(
            &sql,
            &[
                Param::Int(uid),
                Param::Text(scope.to_owned()),
                Param::Text(key.to_owned()),
                opt(old),
                opt(new),
                Param::Text(note.to_owned()),
            ],
        )
        .await?;
    Ok(())
}

async fn write_column(
    store: &mut FoStore<'_>,
    pause: &FoPause,
    today: NaiveDate,
) -> Result<bool, StoreError> {
    let book = pause.scope == "BOOK";
    let table = if book {
        "fo_book_config"
    } else {
        "fo_sleeve_config"
    };
    let where_ = if book {
        "user_id = ?"
    } else {
        "user_id = ? AND sleeve = ?"
    };
    let uid = store.user_id;
    let mut key = vec![Param::Int(uid)];
    if !book {
        key.push(Param::Text(pause.scope.clone()));
    }
    let Some(until) = pause.paused_until else {
        return Ok(false);
    };
    let sql = format!(
        "SELECT paused_until, paused_reason FROM {} WHERE {where_}",
        store.t(table)
    );
    let row = store.one(&sql, &key).await?;
    let old = row
        .as_ref()
        .and_then(|r| py::opt_day(col(r, "paused_until")));
    let old_reason = row
        .as_ref()
        .and_then(|r| py::opt_text(col(r, "paused_reason")));
    let reason = pause.reason();
    if let Some(o) = old
        && o >= today
        && (o >= until || old_reason.as_deref() != Some(reason.as_str()))
    {
        return Ok(false);
    }
    if row.is_none() {
        let cols = if book {
            "user_id, paused_until, paused_reason, updated_by"
        } else {
            "user_id, sleeve, paused_until, paused_reason, updated_by"
        };
        let marks = vec!["?"; key.len() + 3].join(", ");
        let sql = format!("INSERT INTO {} ({cols}) VALUES ({marks})", store.t(table));
        let mut params = key.clone();
        params.push(Param::Date(until));
        params.push(Param::Text(reason.clone()));
        params.push(Param::Text("ledger".to_owned()));
        store.conn.execute(&sql, &params).await?;
    } else {
        let sql = format!(
            "UPDATE {} SET paused_until = ?, paused_reason = ?, \
             updated_by = 'ledger', updated_at = now() WHERE {where_}",
            store.t(table)
        );
        let mut params = vec![Param::Date(until), Param::Text(reason.clone())];
        params.extend(key.iter().cloned());
        store.conn.execute(&sql, &params).await?;
    }
    audit(
        store,
        &pause.scope,
        "paused_until",
        old.map(|o| o.format("%Y-%m-%d").to_string()),
        Some(until.format("%Y-%m-%d").to_string()),
        &format!("{reason}: {}", pause.message),
    )
    .await?;
    Ok(true)
}

async fn record_f1(
    store: &mut FoStore<'_>,
    pause: &FoPause,
    lifts: &BTreeMap<FoSleeve, DateTime<FixedOffset>>,
) -> Result<bool, StoreError> {
    let key = format!("{PAUSE_KEY}{}", pause.scope);
    let sleeve = sleeve_of(&pause.scope).ok_or_else(|| {
        data(format!(
            "ValueError: '{}' is not a valid FoSleeve",
            pause.scope
        ))
    })?;
    let since = lifts.get(&sleeve).copied();
    let sql = format!(
        "SELECT count(*) AS n FROM {} WHERE user_id = ? AND key = ? \
         AND new_value = ? AND (?::timestamptz IS NULL OR changed_at > ?::timestamptz)",
        store.t("fo_config_audit")
    );
    let since_p = since.map_or(Param::Null, Param::Aware);
    let uid = store.user_id;
    let row = store
        .one(
            &sql,
            &[
                Param::Int(uid),
                Param::Text(key.clone()),
                Param::Text(pause.reason()),
                since_p.clone(),
                since_p,
            ],
        )
        .await?;
    if row.is_some_and(|r| int(col(&r, "n")) > 0) {
        return Ok(false);
    }
    audit(
        store,
        group_of(sleeve).value(),
        &key,
        None,
        Some(pause.reason()),
        &pause.message,
    )
    .await?;
    Ok(true)
}

/// `apply_pauses(store, simulated=, today=)`: `04` §7 for one mode, recorded. Every pause now in
/// force.
///
/// # Errors
/// The database.
pub async fn apply_pauses(
    store: &mut FoStore<'_>,
    simulated: bool,
    today: NaiveDate,
    config: &FnoConfig,
    ceilings: &FnoCeilings,
) -> Result<Vec<FoPause>, StoreError> {
    let found = evaluate(store, simulated, today, config, ceilings).await?;
    let lifts = load_lifts(store).await?;
    for pause in &found {
        if F1.iter().any(|s| s.value() == pause.scope) {
            record_f1(store, pause, &lifts).await?;
        } else {
            write_column(store, pause, today).await?;
        }
    }
    Ok(found)
}

/// `after_close(store, position_id, now=, alert=)`: the close's record, then `04` §7 in the
/// position's own mode. A failure is alerted, never raised into the exit already done.
pub async fn after_close(
    store: &mut FoStore<'_>,
    position_id: i64,
    now: DateTime<FixedOffset>,
    alert: &Alert,
) -> Vec<FoPause> {
    let config = FnoConfig::default();
    let ceilings = FnoCeilings::default();
    let result: Result<Vec<FoPause>, StoreError> = async {
        write_journal(store, position_id, alert, None, &config).await?;
        let sql = format!(
            "SELECT simulated FROM {} WHERE id = ?",
            store.t("fo_position")
        );
        let simulated = store
            .one(&sql, &[Param::Int(position_id)])
            .await?
            .is_none_or(|r| py::truthy(col(&r, "simulated")));
        let today = now.with_timezone(&py::ist()).date_naive();
        apply_pauses(store, simulated, today, &config, &ceilings).await
    }
    .await;
    match result {
        Ok(p) => p,
        Err(e) => {
            alert(&format!(
                "FO position {position_id}: the journal or the pauses failed after the close: \
                 {}; the paper checklist counts a journal gap",
                py_exc_repr(&e.py_text())
            ));
            Vec::new()
        }
    }
}

/// `repr(exc)` from `"Type: message"` (Python's `{exc!r}` in `after_close`'s alert):
/// `Type('message')`, quoted as `repr(str)` quotes.
fn py_exc_repr(text: &str) -> String {
    let (kind, message) = text.split_once(": ").unwrap_or((text, ""));
    if kind == "KeyError" {
        // `str(KeyError(k))` is already `repr(k)`.
        return format!("{kind}({message})");
    }
    let quote = if message.contains('\'') && !message.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut body = String::with_capacity(message.len());
    for ch in message.chars() {
        match ch {
            '\\' => body.push_str("\\\\"),
            '\n' => body.push_str("\\n"),
            '\r' => body.push_str("\\r"),
            '\t' => body.push_str("\\t"),
            c if c == quote => {
                body.push('\\');
                body.push(c);
            }
            c => body.push(c),
        }
    }
    format!("{kind}({quote}{body}{quote})")
}

/// `nightly_pauses(store, today=)`: both modes' rules, each over its own rows.
///
/// # Errors
/// The database.
pub async fn nightly_pauses(
    store: &mut FoStore<'_>,
    today: NaiveDate,
) -> Result<Vec<FoPause>, StoreError> {
    let config = FnoConfig::default();
    let ceilings = FnoCeilings::default();
    let mut out = apply_pauses(store, true, today, &config, &ceilings).await?;
    out.extend(apply_pauses(store, false, today, &config, &ceilings).await?);
    Ok(out)
}

/// The desk's ledger as the executor calls it at a close (`fno_execute`'s `FoLedger`).
#[derive(Debug, Clone, Copy, Default)]
pub struct DeskLedger;

#[async_trait]
impl FoLedger for DeskLedger {
    async fn after_close(
        &self,
        store: &mut FoStore<'_>,
        position_id: i64,
        at: DateTime<FixedOffset>,
        alert: &Alert,
    ) -> Result<(), ExecError> {
        after_close(store, position_id, at, alert).await;
        Ok(())
    }

    async fn journal_abandoned(
        &self,
        store: &mut FoStore<'_>,
        plan: &FoPlanRow,
        at: DateTime<FixedOffset>,
    ) -> Result<Option<i64>, ExecError> {
        journal_abandoned(store, plan, at)
            .await
            .map_err(|e| ExecError::Other(e.py_text()))
    }
}

// --- the gate the monitor asks -------------------------------------------------------------

/// `entry_block(store, sleeve, day=, simulated=)`: why a new `sleeve` entry in this mode may not
/// be raised on `day` (empty: it may).
///
/// # Errors
/// The database.
pub async fn entry_block(
    store: &mut FoStore<'_>,
    sleeve: FoSleeve,
    day: NaiveDate,
    simulated: bool,
    config: &FnoConfig,
    ceilings: &FnoCeilings,
) -> Result<Vec<String>, StoreError> {
    let mut reasons = Vec::new();
    let group = group_of(sleeve).value();
    let uid = store.user_id;
    let sql = format!(
        "SELECT paused_until, paused_reason FROM {} WHERE user_id = ?",
        store.t("fo_book_config")
    );
    let book = store.one(&sql, &[Param::Int(uid)]).await?;
    let sql = format!(
        "SELECT paused_until, paused_reason FROM {} WHERE user_id = ? AND sleeve = ?",
        store.t("fo_sleeve_config")
    );
    let own = store
        .one(&sql, &[Param::Int(uid), Param::Text(group.to_owned())])
        .await?;
    for (name, row) in [
        ("the FO book".to_owned(), book),
        (format!("sleeve {group}"), own),
    ] {
        let Some(row) = row else { continue };
        let until = py::opt_day(col(&row, "paused_until"));
        let reason = py::opt_text(col(&row, "paused_reason"));
        if column_pause_applies(until, reason.as_deref(), simulated, day) {
            let until_s =
                until.map_or_else(|| "None".to_owned(), |d| d.format("%Y-%m-%d").to_string());
            let why = match reason {
                Some(r) if !r.is_empty() => r,
                _ => "no reason recorded".to_owned(),
            };
            reasons.push(format!("{name} is paused until {until_s} ({why})"));
        }
    }
    let found = evaluate(store, simulated, day, config, ceilings).await?;
    reasons.extend(pauses_for(&found, sleeve).into_iter().map(|p| p.message));
    Ok(reasons)
}

/// A `PyDict` of a pause, as a page shows it (`dataclasses.asdict` order plus `reason`).
#[must_use]
pub fn pause_json(p: &FoPause) -> PyDict {
    dict([
        ("scope", PyValue::Str(p.scope.clone())),
        ("code", PyValue::Str(p.code.value().to_owned())),
        ("simulated", PyValue::Bool(p.simulated)),
        (
            "paused_until",
            p.paused_until.map_or(PyValue::None, |d| {
                PyValue::Str(d.format("%Y-%m-%d").to_string())
            }),
        ),
        ("message", PyValue::Str(p.message.clone())),
        ("reason", PyValue::Str(p.reason())),
    ])
}

#[cfg(test)]
mod tests {
    //! The pure halves of `tests/test_fno_ledger.py`: the journal's `detail.costs` shape and
    //! never-pooled pauses (the DB-backed world is graded by `desk_diff.py` and the monitor lane).
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use baskfy_core::fno::ledger::PauseCode;

    use super::*;

    #[test]
    fn cost_lines_keep_as_jsons_order_and_strings() {
        let c = CostLines {
            orders: 2,
            brokerage: Decimal::new(4000, 2),
            stt: Decimal::new(1234, 2),
            exchange_txn: Decimal::new(100, 2),
            sebi: Decimal::new(1, 2),
            ipft: Decimal::new(0, 2),
            stamp: Decimal::new(3, 2),
            gst: Decimal::new(738, 2),
        };
        let out = py::dumps(&cost_lines_json(&c));
        assert_eq!(
            out,
            "{\"orders\": 2, \"brokerage\": \"40.00\", \"stt\": \"12.34\", \"exchange_txn\": \
             \"1.00\", \"sebi\": \"0.01\", \"ipft\": \"0.00\", \"stamp\": \"0.03\", \"gst\": \
             \"7.38\", \"total\": \"60.76\"}"
        );
    }

    #[test]
    fn a_pause_reason_carries_its_mode_and_never_binds_the_other() {
        let day = NaiveDate::from_ymd_opt(2026, 11, 18).unwrap();
        let p = FoPause {
            scope: "F2".to_owned(),
            code: PauseCode::F2MonthR,
            simulated: true,
            paused_until: Some(NaiveDate::from_ymd_opt(2026, 11, 30).unwrap()),
            message: "m".to_owned(),
        };
        assert_eq!(p.reason(), "PAPER:F2_MONTH_R");
        let reason = p.reason();
        assert!(column_pause_applies(
            p.paused_until,
            Some(&reason),
            true,
            day
        ));
        assert!(!column_pause_applies(
            p.paused_until,
            Some(&reason),
            false,
            day
        ));
        // A hand-set reason binds both modes.
        assert!(column_pause_applies(
            p.paused_until,
            Some("manual"),
            false,
            day
        ));
        assert_eq!(
            py::dumps(&PyValue::Map(pause_json(&p))),
            "{\"scope\": \"F2\", \"code\": \"F2_MONTH_R\", \"simulated\": true, \"paused_until\": \
             \"2026-11-30\", \"message\": \"m\", \"reason\": \"PAPER:F2_MONTH_R\"}"
        );
    }

    #[test]
    fn the_after_close_alert_carries_the_exceptions_repr() {
        assert_eq!(py_exc_repr("ValueError: bad row"), "ValueError('bad row')");
        assert_eq!(py_exc_repr("KeyError: 'x'"), "KeyError('x')");
        assert_eq!(py_exc_repr("ValueError: it's"), "ValueError(\"it's\")");
        assert_eq!(py_exc_repr("RuntimeError: a\\b"), "RuntimeError('a\\\\b')");
    }

    #[test]
    fn the_keys_are_pythons() {
        assert_eq!(format!("{LIFT_KEY}{}", FoSleeve::F1N.value()), "lift:F1N");
        assert_eq!(format!("{PAUSE_KEY}{}", FoSleeve::F1B.value()), "pause:F1B");
    }
}
