#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
//! VBT lane B against the goldens: `baskfy_core.vbt.{exits, plan, backtest}`
//! (`go/testdata/golden/L1/vbt/<module>.<function>.case_NNN.json`).
//!
//! Every case of every function is loaded and graded strictly by the testkit; the case count
//! per function is asserted so a new or vanished golden is noticed. Inputs are decoded the way
//! `tools/parity/golden_vbt.py` decodes them; a Python raise is graded as
//! `{"raises": {"type", "message"}}`.

use baskfy_core::CoreError;
use baskfy_core::vbt::backtest::{
    BacktestParams, BacktestResult, Panel, gate_vector, panel_from_frame, run_backtest, summarise,
    yearly,
};
use baskfy_core::vbt::config::{Gate, VbtConfig};
use baskfy_core::vbt::exits::{
    Bar, ManageAction, OpenPosition, apply_stop, initial_stop, manage, r_multiple, stop_fill,
    tick_floor,
};
use baskfy_core::vbt::orders::WorkingOrder;
use baskfy_core::vbt::plan::{
    BookState, Candidate, ManagedRow, PlanLine, Skipped, assemble, build_entries, exit_lines,
    to_tick,
};
use baskfy_testkit::{Golden, to_golden_value};
use chrono::NaiveDate;
use rust_decimal::Decimal;
use serde_json::{Map, Value, json};

type R = Result<(), String>;

const P: &str = "baskfy_core.vbt.";

fn s<E: std::fmt::Display>(e: E) -> String {
    e.to_string()
}

fn check(g: &Golden, v: Value) -> R {
    g.check_value(&v).map_err(|m| format!("{m}"))
}

fn val<T: serde::Serialize>(x: &T) -> Result<Value, String> {
    to_golden_value(x).map_err(s)
}

fn config(g: &Golden) -> Result<VbtConfig, String> {
    g.input_as("config").map_err(s)
}

fn dec(g: &Golden, key: &str) -> Result<Decimal, String> {
    g.input_decimal(key).map_err(s)
}

fn grade(fn_name: &str, expected: usize, run: fn(&Golden) -> R) {
    let full = format!("{P}{fn_name}");
    let cases: Vec<Golden> = baskfy_testkit::cases_for(&full).unwrap();
    let mut failures = Vec::new();
    for g in &cases {
        if let Err(why) = run(g) {
            failures.push(format!(
                "{} (known_bug: {:?}):\n{why}",
                g.case, g.meta.known_bug
            ));
        }
    }
    assert!(
        failures.is_empty(),
        "{} of {} {full} cases failed:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n\n")
    );
    assert_eq!(cases.len(), expected, "{full}: case count changed");
}

// -------------------------------------------------------------------------------------- exits

#[test]
fn exits_tick_floor() {
    grade("exits.tick_floor", 6, |g| {
        check(
            g,
            val(&tick_floor(dec(g, "price")?, dec(g, "tick")?).map_err(s)?)?,
        )
    });
}

#[test]
fn exits_initial_stop() {
    grade("exits.initial_stop", 6, |g| {
        let out = initial_stop(dec(g, "fill_price")?, &config(g)?.exits, dec(g, "tick")?);
        check(g, val(&out.map_err(s)?)?)
    });
}

#[test]
fn exits_apply_stop() {
    grade("exits.apply_stop", 4, |g| {
        check(
            g,
            val(&apply_stop(dec(g, "current")?, dec(g, "proposed")?))?,
        )
    });
}

#[test]
fn exits_stop_fill() {
    grade("exits.stop_fill", 7, |g| {
        let bar: Bar = g.input_as("bar").map_err(s)?;
        let out = match stop_fill(&bar, dec(g, "stop")?) {
            None => Value::Null,
            Some((reason, price)) => json!({"reason": val(&reason)?, "price": val(&price)?}),
        };
        check(g, out)
    });
}

#[test]
fn exits_manage() {
    grade("exits.manage", 12, |g| {
        let position: OpenPosition = g.input_as("position").map_err(s)?;
        let bar: Bar = g.input_as("bar").map_err(s)?;
        let blank: i64 = g.input_as("blank_sessions").map_err(s)?;
        check(g, val(&manage(&position, &bar, blank, &config(g)?.exits))?)
    });
}

#[test]
fn exits_r_multiple() {
    grade("exits.r_multiple", 6, |g| {
        let out = r_multiple(dec(g, "entry")?, dec(g, "initial")?, dec(g, "exit_price")?);
        check(g, val(&out.map_err(s)?)?)
    });
}

// --------------------------------------------------------------------------------------- plan

#[test]
fn plan_to_tick() {
    grade("plan.to_tick", 8, |g| {
        let tick = g.input_opt_decimal("tick").map_err(s)?;
        check(g, val(&to_tick(dec(g, "price")?, tick).map_err(s)?)?)
    });
}

#[test]
fn plan_book_state() {
    grade("plan.BookState", 5, |g| {
        let book: BookState = g.input_as("book").map_err(s)?;
        let ids: Vec<i64> = g.input_as("instrument_ids").map_err(s)?;
        let mut entries = Map::new();
        for id in ids {
            entries.insert(id.to_string(), json!(book.entries_in(id)));
        }
        check(
            g,
            json!({"entries_in": Value::Object(entries), "slots_taken": book.slots_taken()}),
        )
    });
}

#[test]
fn plan_build_entries() {
    grade("plan.build_entries", 17, |g| {
        let candidates: Vec<Candidate> = g.input_as("candidates").map_err(s)?;
        let gate: Gate = g.input_as("gate").map_err(s)?;
        let book: BookState = g.input_as("book").map_err(s)?;
        let max_open: Option<i64> = g.input_as("max_open_positions").map_err(s)?;
        let tick = g.input_opt_decimal("tick").map_err(s)?;
        let (lines, skipped) = build_entries(
            &candidates,
            gate,
            dec(g, "equity")?,
            &book,
            &config(g)?,
            dec(g, "slot_multiplier")?,
            max_open,
            tick,
        )
        .map_err(s)?;
        check(g, json!({"lines": val(&lines)?, "skipped": val(&skipped)?}))
    });
}

fn managed_rows(g: &Golden) -> Result<Vec<ManagedRow>, String> {
    let rows: Vec<Vec<Value>> = g.input_as("managed").map_err(s)?;
    rows.into_iter()
        .map(|row| {
            let action: ManageAction = serde_json::from_value(row[3].clone()).map_err(s)?;
            let position_id = if row.len() == 5 {
                row[4].as_i64()
            } else {
                None
            };
            Ok(ManagedRow {
                instrument_id: row[0].as_i64().ok_or("instrument_id")?,
                symbol: row[1].as_str().ok_or("symbol")?.to_owned(),
                quantity_open: row[2].as_i64().ok_or("quantity_open")?,
                action,
                position_id,
            })
        })
        .collect()
}

#[test]
fn plan_exit_lines() {
    grade("plan.exit_lines", 10, |g| {
        let managed = managed_rows(g)?;
        let expired: Vec<(WorkingOrder, String)> = g.input_as("expired").map_err(s)?;
        let book: BookState = g.input_as("book").map_err(s)?;
        check(g, val(&exit_lines(&managed, &expired, &book))?)
    });
}

#[test]
fn plan_assemble() {
    grade("plan.assemble", 5, |g| {
        let session: NaiveDate = g.input_date("session").map_err(s)?;
        let gate: Gate = g.input_as("gate").map_err(s)?;
        let exits: Vec<PlanLine> = g.input_as("exits").map_err(s)?;
        let entries: Vec<PlanLine> = g.input_as("entries").map_err(s)?;
        let skips: Vec<Skipped> = g.input_as("skips").map_err(s)?;
        let source: String = g.input_as("source").map_err(s)?;
        let plan = assemble(
            session,
            gate,
            dec(g, "equity")?,
            &exits,
            &entries,
            &skips,
            &source,
        );
        let canonical: Vec<_> = plan.lines.iter().map(PlanLine::canonical).collect();
        check(
            g,
            json!({
                "plan": val(&plan)?,
                "plan_hash": plan.plan_hash(),
                "canonical": val(&canonical)?,
                "total_new_exposure_inr": val(&plan.total_new_exposure_inr().map_err(s)?)?,
                "entries": val(&plan.entries())?,
            }),
        )
    });
}

// ----------------------------------------------------------------------------------- backtest

#[test]
fn backtest_panel_from_frame() {
    grade("backtest.panel_from_frame", 5, |g| {
        let frame = g.input_frame("frame").map_err(s)?;
        let column: String = g.input_as("signal_column").map_err(s)?;
        let panel = panel_from_frame(&frame, &column).map_err(s)?;
        let days: Vec<NaiveDate> = g.input_as("column_of").map_err(s)?;
        let mut column_of = Map::new();
        for d in days {
            column_of.insert(d.to_string(), json!(panel.column_of(d)));
        }
        check(
            g,
            json!({"panel": val(&panel)?, "column_of": Value::Object(column_of)}),
        )
    });
}

#[test]
fn backtest_gate_vector() {
    grade("backtest.gate_vector", 5, |g| {
        let breadth = g.input_frame("breadth").map_err(s)?;
        let sessions: Vec<NaiveDate> = g.input_as("sessions").map_err(s)?;
        check(g, json!(gate_vector(&breadth, &sessions).map_err(s)?))
    });
}

fn result_out(result: &BacktestResult) -> Result<Value, String> {
    Ok(json!({
        "trades": val(&result.trades)?,
        "sessions": val(&result.sessions)?,
        "equity": val(&result.equity)?,
        "open_positions": val(&result.open_positions)?,
        "skipped": val(&result.skipped)?,
        "orders_offered": result.orders_offered,
        "fill_rate_pct": val(&result.fill_rate_pct().map_err(s)?)?,
    }))
}

#[test]
fn backtest_run_backtest() {
    grade("backtest.run_backtest", 14, |g| {
        let panel: Panel = g.input_as("panel").map_err(s)?;
        let gate: Vec<bool> = g.input_as("gate_open").map_err(s)?;
        let params: BacktestParams = g.input_as("params").map_err(s)?;
        let result = run_backtest(&panel, &gate, &params).map_err(s)?;
        check(g, result_out(&result)?)
    });
}

#[test]
fn backtest_summarise() {
    grade("backtest.summarise", 9, |g| {
        let result: BacktestResult = g.input_as("result").map_err(s)?;
        let out = match summarise(&result).map_err(s)? {
            None => Value::Null,
            Some(stats) => json!({"stats": val(&stats)?, "json": val(&stats.to_json())?}),
        };
        check(g, out)
    });
}

#[test]
fn backtest_yearly() {
    grade("backtest.yearly", 5, |g| {
        let result: BacktestResult = g.input_as("result").map_err(s)?;
        let out = match yearly(&result) {
            Ok(rows) => json!({"rows": val(&rows)?, "json": val(&rows)?}),
            // R2.9 KNOWN BUG: Python's IndexError on an empty curve.
            Err(CoreError::InvalidInput(m)) if m == "tuple index out of range" => {
                json!({"raises": {"type": "IndexError", "message": m}})
            }
            Err(e) => return Err(s(e)),
        };
        check(g, out)
    });
}

#[test]
fn backtest_fill_rate_pct() {
    grade("backtest.BacktestResult.fill_rate_pct", 4, |g| {
        let result: BacktestResult = g.input_as("result").map_err(s)?;
        check(g, val(&result.fill_rate_pct().map_err(s)?)?)
    });
}
