//! Grades the fno decision ports against every golden of
//! `baskfy_core.fno.{condor, covered, exits, checklist, scan, monitor, directional}` in
//! `go/testdata/golden/L1/fno`. Each case is called from its stored inputs exactly as
//! `tools/parity/golden_fno.py`'s `call_*` function calls the Python; a Python raise is stored
//! as `{"raises": {"type", "message"}}` and the port's error is graded the same way.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::BTreeMap;

use baskfy_core::fno::calendar::PyDateTime;
use baskfy_core::fno::checklist::{
    F1Cycle, F2Close, LegFill, Violation, into_expiry, journal_gap, mark_gaps, missed_cycles,
    paper_checklist, uncovered_steps,
};
use baskfy_core::fno::condor::{
    CondorStrikes, ENTRY_SEQUENCE, EXIT_SEQUENCE, LegQuote, LegRole, RoleMap, cost_to_close,
    credit, liquidity_refusals, loss_close_hit, loss_close_level, max_loss_per_unit, one_sd,
    option_type_of, price_structure, profit_take_hit, profit_take_level, select_strikes, sign_of,
};
use baskfy_core::fno::config::{
    CommonConfig, F1Config, F2Config, F3Config, FnoCeilings, FnoConfig, FnoError, FnoResult,
    FoSleeve, FoSleeveGroup, SeriesConfig, StopVolConfig,
};
use baskfy_core::fno::covered::{
    OptionPosition, assess_step, book_after, is_covered, net_of_plan, uncovered,
};
use baskfy_core::fno::directional::{
    Bar75, DailyBar, Direction, Levels, MinuteBar, OpenSpread, SpreadStrikes, StrikePrices,
    add_decision, bars_needed, choose_expiry, confirm_75, credit_per_unit, daily_direction,
    decay_target_mark, direction_for, f3_exit, intraday_aligned, key_level, level_broken, levels,
    loss_cut_mark, pivots, pnl_inr, propose_spread, seventy_five_minute_bars, size_entry,
    spread_strikes,
};
use baskfy_core::fno::exits::{
    book_paused, f1_exit, f1_paused, f2_exit, f2_initial_stop, f2_month_paused,
    f2_time_exit_session, f2_trail, gtt_trigger, stop_from_vol,
};
use baskfy_core::fno::monitor::{
    f1_close_cost, f1_pnl_inr, f2_action, f2_pnl_inr, f2_trail_step, reprice_condor, reprice_future,
};
use baskfy_core::fno::scan::{
    LegPrint, LegPrints, allocate_capacity, f2_signals, propose_condor, propose_future,
};
use baskfy_core::fno::vol::OptionType;
use baskfy_core::options::costs::CostRates;
use baskfy_core::options::gating::Mode;
use baskfy_testkit::{Golden, cases_for, encode_frame};
use chrono::NaiveDate;
use rust_decimal::Decimal;
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

const P: &str = "baskfy_core.fno.";

/// Runs every case of `function`, collecting failures so one report names them all; returns
/// the count.
fn grade(function: &str, run: impl Fn(&Golden) -> Result<(), String>) -> usize {
    let name = format!("{P}{function}");
    let cases = cases_for(&name).unwrap();
    let failures: Vec<String> = cases
        .iter()
        .filter_map(|g| {
            run(g).err().map(|e| {
                let bug = g
                    .meta
                    .known_bug
                    .as_deref()
                    .map(|b| format!(" (known_bug: {b})"))
                    .unwrap_or_default();
                format!("{}{bug}: {e}", g.case)
            })
        })
        .collect();
    assert!(
        failures.is_empty(),
        "{name}: {} of {} cases failed:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n")
    );
    cases.len()
}

fn s<E: std::fmt::Display>(e: E) -> String {
    e.to_string()
}

fn input<T: DeserializeOwned>(g: &Golden, key: &str) -> Result<T, String> {
    g.input_as(key).map_err(s)
}

fn raises(e: &FnoError) -> Value {
    json!({"raises": {"type": e.python_type(), "message": e.message()}})
}

/// The output of a call that may raise, as the dumper's `_capture` stores it.
fn capture<T: Serialize>(result: FnoResult<T>) -> Result<Value, String> {
    match result {
        Ok(v) => serde_json::to_value(v).map_err(s),
        Err(e) => Ok(raises(&e)),
    }
}

/// The output of a call the dumper does not capture: an error fails the case.
fn plain<T: Serialize>(result: FnoResult<T>) -> Result<Value, String> {
    serde_json::to_value(result.map_err(|e| e.message())?).map_err(s)
}

fn check(g: &Golden, actual: &Value) -> Result<(), String> {
    g.check_value(actual).map_err(s)
}

/// Python's `Decimal(str)` keeps the sign of a zero (`"-0.00"`); rust_decimal's parser drops it.
fn signed(text: &str) -> Result<Decimal, String> {
    let mut v: Decimal = text.trim().parse().map_err(s)?;
    if v.is_zero() && text.trim_start().starts_with('-') {
        v.set_sign_negative(true);
    }
    Ok(v)
}

fn dec(g: &Golden, key: &str) -> Result<Decimal, String> {
    let text: String = input(g, key)?;
    signed(&text)
}

fn opt_dec(g: &Golden, key: &str) -> Result<Option<Decimal>, String> {
    let text: Option<String> = input(g, key)?;
    text.as_deref().map(signed).transpose()
}

fn decs(g: &Golden, key: &str) -> Result<Vec<Decimal>, String> {
    let texts: Vec<String> = input(g, key)?;
    texts.iter().map(|t| signed(t)).collect()
}

fn date(g: &Golden, key: &str) -> Result<NaiveDate, String> {
    g.input_date(key).map_err(s)
}

fn now(g: &Golden) -> Result<PyDateTime, String> {
    input(g, "now")
}

/// A config decoded field by field (defaults for missing fields) and validated, as the
/// dumper's `cfg(cls, raw)` constructs it.
macro_rules! cfg {
    ($g:expr, $key:expr, $ty:ty) => {{
        let c: $ty = input($g, $key)?;
        c.validate().map_err(|e| e.message())?;
        c
    }};
}

fn strikes(g: &Golden) -> Result<CondorStrikes, String> {
    input(g, "strikes")
}

// =============================================================================================
// condor
// =============================================================================================

fn run_condor_roles(g: &Golden) -> Result<(), String> {
    let types: BTreeMap<&str, OptionType> = LegRole::ALL
        .iter()
        .map(|r| (r.value(), option_type_of(*r)))
        .collect();
    let signs: BTreeMap<&str, i64> = LegRole::ALL
        .iter()
        .map(|r| (r.value(), sign_of(*r)))
        .collect();
    check(
        g,
        &json!({
            "ENTRY_SEQUENCE": ENTRY_SEQUENCE,
            "EXIT_SEQUENCE": EXIT_SEQUENCE,
            "option_type_of": types,
            "sign_of": signs,
        }),
    )
}

fn run_condor_one_sd(g: &Golden) -> Result<(), String> {
    let out = one_sd(
        dec(g, "forward")?,
        input(g, "iv")?,
        input(g, "days_to_expiry")?,
    );
    check(g, &capture(out)?)
}

fn run_condor_select_strikes(g: &Golden) -> Result<(), String> {
    let f1 = cfg!(g, "config", F1Config);
    let out = select_strikes(
        dec(g, "forward")?,
        input(g, "iv")?,
        input(g, "days_to_expiry")?,
        &decs(g, "call_strikes")?,
        &decs(g, "put_strikes")?,
        &f1,
    );
    check(g, &capture(out)?)
}

fn run_condor_credit(g: &Golden) -> Result<(), String> {
    let mids: RoleMap = input(g, "mids")?;
    check(g, &capture(credit(&mids))?)
}

fn run_condor_max_loss_per_unit(g: &Golden) -> Result<(), String> {
    check(
        g,
        &plain(max_loss_per_unit(&strikes(g)?, dec(g, "credit")?))?,
    )
}

fn run_condor_price_structure(g: &Golden) -> Result<(), String> {
    let mids: RoleMap = input(g, "mids")?;
    check(g, &capture(price_structure(&strikes(g)?, &mids))?)
}

fn run_condor_liquidity_refusals(g: &Golden) -> Result<(), String> {
    let quotes: BTreeMap<LegRole, LegQuote> = input(g, "quotes")?;
    let f1 = cfg!(g, "config", F1Config);
    check(g, &plain(liquidity_refusals(&strikes(g)?, &quotes, &f1))?)
}

fn run_condor_legquote(g: &Golden) -> Result<(), String> {
    let quotes: Vec<LegQuote> = input(g, "quotes")?;
    check(g, &serde_json::to_value(quotes).map_err(s)?)
}

fn run_condor_cost_to_close(g: &Golden) -> Result<(), String> {
    let marks: RoleMap = input(g, "marks")?;
    check(g, &capture(cost_to_close(&strikes(g)?, &marks))?)
}

fn run_condor_levels(g: &Golden) -> Result<(), String> {
    let f1 = cfg!(g, "config", F1Config);
    let credit_ = dec(g, "entry_credit")?;
    let st = strikes(g)?;
    let cost = dec(g, "close_cost")?;
    let e = |e: FnoError| e.message();
    check(
        g,
        &json!({
            "profit_take_level": profit_take_level(credit_, &f1).map_err(e)?,
            "loss_close_level": loss_close_level(credit_, &st, &f1).map_err(e)?,
            "profit_take_hit": profit_take_hit(cost, credit_, &f1).map_err(e)?,
            "loss_close_hit": loss_close_hit(cost, credit_, &st, &f1).map_err(e)?,
        }),
    )
}

// =============================================================================================
// exits
// =============================================================================================

fn run_exits_f1_exit(g: &Golden) -> Result<(), String> {
    let f1 = cfg!(g, "f1", F1Config);
    let common = cfg!(g, "common", CommonConfig);
    let out = f1_exit(
        now(g)?,
        date(g, "hard_exit_date")?,
        opt_dec(g, "close_cost")?,
        dec(g, "entry_credit")?,
        &strikes(g)?,
        &f1,
        &common,
    );
    check(g, &plain(out)?)
}

#[derive(Deserialize)]
struct VolCase {
    price: String,
    ann_vol: f64,
}

fn run_exits_stop_from_vol(g: &Golden) -> Result<(), String> {
    let config = cfg!(g, "config", StopVolConfig);
    let cases: Vec<VolCase> = input(g, "cases")?;
    let out = cases
        .iter()
        .map(|c| stop_from_vol(signed(&c.price)?, c.ann_vol, &config).map_err(|e| e.message()))
        .collect::<Result<Vec<_>, String>>()?;
    check(g, &json!(out))
}

fn run_exits_f2_initial_stop(g: &Golden) -> Result<(), String> {
    let f2 = cfg!(g, "f2", F2Config);
    check(
        g,
        &plain(f2_initial_stop(
            dec(g, "entry")?,
            dec(g, "atr_at_signal")?,
            &f2,
        ))?,
    )
}

fn run_exits_f2_trail(g: &Golden) -> Result<(), String> {
    let f2 = cfg!(g, "f2", F2Config);
    check(
        g,
        &plain(f2_trail(
            dec(g, "stop")?,
            dec(g, "highest_close")?,
            dec(g, "atr_at_entry")?,
            &f2,
        ))?,
    )
}

fn run_exits_gtt_trigger(g: &Golden) -> Result<(), String> {
    let config = cfg!(g, "config", StopVolConfig);
    check(
        g,
        &plain(gtt_trigger(
            dec(g, "stop")?,
            dec(g, "entry")?,
            input(g, "ann_vol")?,
            &config,
        ))?,
    )
}

fn run_exits_f2_time_exit_session(g: &Golden) -> Result<(), String> {
    let f2 = cfg!(g, "f2", F2Config);
    let sessions: Vec<NaiveDate> = input(g, "sessions")?;
    check(
        g,
        &plain(f2_time_exit_session(
            &sessions,
            date(g, "entry_session")?,
            &f2,
        ))?,
    )
}

fn run_exits_f2_exit(g: &Golden) -> Result<(), String> {
    let common = cfg!(g, "common", CommonConfig);
    let out = f2_exit(
        now(g)?,
        dec(g, "price")?,
        dec(g, "stop")?,
        input(g, "time_exit_date")?,
        input(g, "roll_date")?,
        &common,
    );
    check(g, &serde_json::to_value(out).map_err(s)?)
}

fn run_exits_f1_paused(g: &Golden) -> Result<(), String> {
    let f1 = cfg!(g, "f1", F1Config);
    check(g, &json!(f1_paused(&decs(g, "closed_r")?, &f1)))
}

fn run_exits_f2_month_paused(g: &Golden) -> Result<(), String> {
    let f2 = cfg!(g, "f2", F2Config);
    check(
        g,
        &plain(f2_month_paused(&decs(g, "month_closed_r")?, &f2))?,
    )
}

fn run_exits_book_paused(g: &Golden) -> Result<(), String> {
    check(
        g,
        &json!(book_paused(
            dec(g, "month_realised_inr")?,
            dec(g, "monthly_pause_inr")?
        )),
    )
}

// =============================================================================================
// covered
// =============================================================================================

fn positions(g: &Golden, key: &str) -> Result<Vec<OptionPosition>, String> {
    input(g, key)
}

fn run_covered_uncovered(g: &Golden) -> Result<(), String> {
    let book = positions(g, "book")?;
    check(
        g,
        &json!({"uncovered": uncovered(&book), "is_covered": is_covered(&book)}),
    )
}

fn run_covered_book_after(g: &Golden) -> Result<(), String> {
    let step: OptionPosition = input(g, "step")?;
    let out = book_after(
        &positions(g, "broker_positions")?,
        &positions(g, "plan_filled")?,
        &step,
    );
    check(g, &serde_json::to_value(out).map_err(s)?)
}

fn run_covered_net_of_plan(g: &Golden) -> Result<(), String> {
    let out = net_of_plan(&positions(g, "broker_raw")?, &positions(g, "plan_filled")?);
    check(g, &serde_json::to_value(out).map_err(s)?)
}

fn run_covered_assess_step(g: &Golden) -> Result<(), String> {
    let step: OptionPosition = input(g, "step")?;
    let out = assess_step(
        &step,
        &positions(g, "broker_positions")?,
        &positions(g, "plan_filled")?,
    );
    check(g, &serde_json::to_value(out).map_err(s)?)
}

// =============================================================================================
// checklist
// =============================================================================================

fn run_checklist_uncovered_steps(g: &Golden) -> Result<(), String> {
    let sleeve: FoSleeve = input(g, "sleeve")?;
    let fills: Vec<LegFill> = input(g, "fills")?;
    check(
        g,
        &serde_json::to_value(uncovered_steps(sleeve, &fills)).map_err(s)?,
    )
}

fn run_checklist_into_expiry(g: &Golden) -> Result<(), String> {
    let sleeve: FoSleeve = input(g, "sleeve")?;
    let reference: String = input(g, "ref")?;
    let out = into_expiry(
        sleeve,
        &reference,
        input(g, "expiry")?,
        input(g, "closed_on")?,
        date(g, "as_of")?,
    );
    check(g, &serde_json::to_value(out).map_err(s)?)
}

fn run_checklist_journal_gap(g: &Golden) -> Result<(), String> {
    let sleeve: FoSleeve = input(g, "sleeve")?;
    let reference: String = input(g, "ref")?;
    let out = journal_gap(
        sleeve,
        &reference,
        date(g, "closed_on")?,
        input(g, "journalled")?,
    );
    check(g, &serde_json::to_value(out).map_err(s)?)
}

fn run_checklist_mark_gaps(g: &Golden) -> Result<(), String> {
    let sleeve: FoSleeve = input(g, "sleeve")?;
    let reference: String = input(g, "ref")?;
    let held: Vec<NaiveDate> = input(g, "held_sessions")?;
    let marked: Vec<NaiveDate> = input(g, "marked")?;
    check(
        g,
        &serde_json::to_value(mark_gaps(sleeve, &reference, &held, &marked)).map_err(s)?,
    )
}

fn run_checklist_missed_cycles(g: &Golden) -> Result<(), String> {
    let cycles: Vec<F1Cycle> = input(g, "cycles")?;
    check(g, &serde_json::to_value(missed_cycles(&cycles)).map_err(s)?)
}

fn run_checklist_paper_checklist(g: &Golden) -> Result<(), String> {
    let cycles: Vec<F1Cycle> = input(g, "f1_cycles")?;
    let closes: Vec<F2Close> = input(g, "f2_closes")?;
    let sessions: Vec<NaiveDate> = input(g, "sessions")?;
    let violations: Vec<Violation> = input(g, "violations")?;
    let result = paper_checklist(
        date(g, "as_of")?,
        &cycles,
        input(g, "f2_start")?,
        &closes,
        &sessions,
        &violations,
    );
    check(
        g,
        &json!({
            "checklist": result,
            "violations_by_kind": result.violations_by_kind(None),
            "violations_by_kind_f1": result.violations_by_kind(Some(FoSleeveGroup::F1)),
            "violations_by_kind_f2": result.violations_by_kind(Some(FoSleeveGroup::F2)),
        }),
    )
}

// =============================================================================================
// monitor
// =============================================================================================

fn run_monitor_reprice_condor(g: &Golden) -> Result<(), String> {
    let quotes: BTreeMap<LegRole, LegQuote> = input(g, "quotes")?;
    let mode: Mode = input(g, "mode")?;
    let config = cfg!(g, "config", FnoConfig);
    let rates: CostRates = input(g, "rates")?;
    let ceilings = cfg!(g, "ceilings", FnoCeilings);
    let out = reprice_condor(
        &strikes(g)?,
        &quotes,
        input(g, "lot_size")?,
        dec(g, "capital_inr")?,
        mode,
        &config,
        &rates,
        &ceilings,
    );
    check(g, &capture(out)?)
}

fn run_monitor_f1_close_cost(g: &Golden) -> Result<(), String> {
    let marks: RoleMap = input(g, "marks")?;
    check(g, &plain(f1_close_cost(&strikes(g)?, &marks))?)
}

fn run_monitor_f1_pnl_inr(g: &Golden) -> Result<(), String> {
    check(
        g,
        &plain(f1_pnl_inr(
            dec(g, "entry_credit")?,
            dec(g, "close_cost")?,
            input(g, "quantity")?,
        ))?,
    )
}

fn run_monitor_reprice_future(g: &Golden) -> Result<(), String> {
    let mode: Mode = input(g, "mode")?;
    let config = cfg!(g, "config", FnoConfig);
    let ceilings = cfg!(g, "ceilings", FnoCeilings);
    let out = reprice_future(
        opt_dec(g, "bid")?,
        opt_dec(g, "ask")?,
        dec(g, "atr")?,
        input(g, "ann_vol")?,
        input(g, "lot_size")?,
        dec(g, "capital_inr")?,
        mode,
        &config,
        &ceilings,
    );
    check(g, &capture(out)?)
}

fn run_monitor_f2_pnl_inr(g: &Golden) -> Result<(), String> {
    check(
        g,
        &plain(f2_pnl_inr(
            dec(g, "entry")?,
            dec(g, "mark")?,
            input(g, "quantity")?,
        ))?,
    )
}

fn run_monitor_f2_trail_step(g: &Golden) -> Result<(), String> {
    let config = cfg!(g, "config", FnoConfig);
    let out = f2_trail_step(
        dec(g, "stop")?,
        dec(g, "trigger")?,
        dec(g, "highest_close")?,
        dec(g, "close")?,
        dec(g, "entry")?,
        dec(g, "atr_at_entry")?,
        input(g, "ann_vol")?,
        &config,
    );
    check(g, &plain(out)?)
}

fn run_monitor_f2_action(g: &Golden) -> Result<(), String> {
    let common = cfg!(g, "common", CommonConfig);
    let out = f2_action(
        now(g)?,
        opt_dec(g, "price")?,
        dec(g, "trigger")?,
        input(g, "time_exit_date")?,
        input(g, "roll_date")?,
        input(g, "naked")?,
        &common,
    );
    check(g, &plain(out)?)
}

// =============================================================================================
// directional
// =============================================================================================

fn lv(g: &Golden) -> Result<Levels, String> {
    input(g, "levels")
}

fn run_dir_seventy_five(g: &Golden) -> Result<(), String> {
    let minutes: Vec<MinuteBar> = input(g, "minutes")?;
    check(g, &plain(seventy_five_minute_bars(&minutes))?)
}

fn run_dir_pivots(g: &Golden) -> Result<(), String> {
    let bars: Vec<DailyBar> = input(g, "bars")?;
    check(
        g,
        &serde_json::to_value(pivots(&bars, input(g, "width")?)).map_err(s)?,
    )
}

fn run_dir_bars_needed(g: &Golden) -> Result<(), String> {
    let f3 = cfg!(g, "f3", F3Config);
    check(g, &json!(bars_needed(&f3)))
}

fn run_dir_levels(g: &Golden) -> Result<(), String> {
    let bars: Vec<DailyBar> = input(g, "bars")?;
    let f3 = cfg!(g, "f3", F3Config);
    let found = levels(&bars, &f3).map_err(|e| e.message())?;
    let direction = found.as_ref().map(daily_direction);
    check(g, &json!({"levels": found, "daily_direction": direction}))
}

fn run_dir_daily_direction(g: &Golden) -> Result<(), String> {
    check(g, &json!(daily_direction(&lv(g)?)))
}

fn run_dir_confirm_75(g: &Golden) -> Result<(), String> {
    let bars: Vec<Bar75> = input(g, "bars")?;
    let daily: Direction = input(g, "daily")?;
    let f3 = cfg!(g, "f3", F3Config);
    check(g, &plain(confirm_75(&bars, daily, &f3))?)
}

fn run_dir_direction_for(g: &Golden) -> Result<(), String> {
    let bars: Vec<Bar75> = input(g, "bars")?;
    let f3 = cfg!(g, "f3", F3Config);
    check(g, &plain(direction_for(&lv(g)?, &bars, &f3))?)
}

fn run_dir_key_level(g: &Golden) -> Result<(), String> {
    let levels_ = lv(g)?;
    let out: BTreeMap<&str, Option<Decimal>> = [Direction::Up, Direction::Down, Direction::None]
        .into_iter()
        .map(|d| (d.value(), key_level(d, &levels_)))
        .collect();
    check(g, &json!(out))
}

#[derive(Deserialize)]
struct AlignCase {
    direction: Direction,
    last_price: String,
    session_open: String,
}

fn run_dir_intraday_aligned(g: &Golden) -> Result<(), String> {
    let cases: Vec<AlignCase> = input(g, "cases")?;
    let out = cases
        .iter()
        .map(|c| {
            Ok(intraday_aligned(
                c.direction,
                signed(&c.last_price)?,
                signed(&c.session_open)?,
            ))
        })
        .collect::<Result<Vec<bool>, String>>()?;
    check(g, &json!(out))
}

#[derive(Deserialize)]
struct BrokenCase {
    direction: Direction,
    level: String,
    last_price: String,
}

fn run_dir_level_broken(g: &Golden) -> Result<(), String> {
    let f3 = cfg!(g, "f3", F3Config);
    let cases: Vec<BrokenCase> = input(g, "cases")?;
    let out = cases
        .iter()
        .map(|c| {
            level_broken(c.direction, signed(&c.level)?, signed(&c.last_price)?, &f3)
                .map_err(|e| e.message())
        })
        .collect::<Result<Vec<bool>, String>>()?;
    check(g, &json!(out))
}

fn run_dir_spread_strikes(g: &Golden) -> Result<(), String> {
    let f3 = cfg!(g, "f3", F3Config);
    let direction: Direction = input(g, "direction")?;
    let out = spread_strikes(direction, &lv(g)?, dec(g, "step")?, &f3);
    check(g, &capture(out)?)
}

fn run_dir_choose_expiry(g: &Golden) -> Result<(), String> {
    let expiries: Vec<NaiveDate> = input(g, "expiries")?;
    let sessions: Vec<NaiveDate> = input(g, "sessions")?;
    let out = choose_expiry(
        &expiries,
        &sessions,
        date(g, "entry")?,
        input(g, "min_sessions")?,
    );
    check(g, &json!(out))
}

fn run_dir_spread_arith(g: &Golden) -> Result<(), String> {
    let spread: SpreadStrikes = input(g, "strikes")?;
    let e = |e: FnoError| e.message();
    let c = credit_per_unit(dec(g, "short_price")?, dec(g, "wing_price")?).map_err(e)?;
    check(
        g,
        &json!({
            "credit_per_unit": c,
            "max_loss_per_unit": baskfy_core::fno::directional::max_loss_per_unit(&spread, c).map_err(e)?,
            "pnl_inr": pnl_inr(c, dec(g, "mark")?, input(g, "quantity")?).map_err(e)?,
        }),
    )
}

fn run_dir_size_entry(g: &Golden) -> Result<(), String> {
    let mode: Mode = input(g, "mode")?;
    let f3 = cfg!(g, "f3", F3Config);
    let common = cfg!(g, "common", CommonConfig);
    let ceilings = cfg!(g, "ceilings", FnoCeilings);
    let out = size_entry(
        mode,
        dec(g, "capital_inr")?,
        dec(g, "max_loss_per_unit")?,
        input(g, "lot_size")?,
        &f3,
        &common,
        &ceilings,
    );
    check(g, &capture(out)?)
}

#[derive(Deserialize)]
struct PriceRow {
    strike: String,
    option_type: OptionType,
    price: String,
}

fn run_dir_propose_spread(g: &Golden) -> Result<(), String> {
    let mode: Mode = input(g, "mode")?;
    let direction: Direction = input(g, "direction")?;
    let f3 = cfg!(g, "f3", F3Config);
    let common = cfg!(g, "common", CommonConfig);
    let ceilings = cfg!(g, "ceilings", FnoCeilings);
    let rows: Vec<PriceRow> = input(g, "prices")?;
    let prices: StrikePrices = rows
        .iter()
        .map(|r| Ok(((signed(&r.strike)?, r.option_type), signed(&r.price)?)))
        .collect::<Result<_, String>>()?;
    let out = propose_spread(
        mode,
        direction,
        &lv(g)?,
        dec(g, "step")?,
        &prices,
        input(g, "lot_size")?,
        dec(g, "capital_inr")?,
        &f3,
        &common,
        &ceilings,
    );
    check(g, &capture(out)?)
}

fn run_dir_add_decision(g: &Golden) -> Result<(), String> {
    let position: OpenSpread = input(g, "position")?;
    let f3 = cfg!(g, "f3", F3Config);
    let common = cfg!(g, "common", CommonConfig);
    let out = add_decision(
        &position,
        date(g, "today")?,
        input(g, "direction_now")?,
        input(g, "level_intact")?,
        opt_dec(g, "mark")?,
        dec(g, "capital_inr")?,
        &f3,
        &common,
    );
    check(g, &plain(out)?)
}

fn run_dir_f3_marks(g: &Golden) -> Result<(), String> {
    let f3 = cfg!(g, "f3", F3Config);
    let credits: Vec<String> = input(g, "credits")?;
    let mut out = Map::new();
    for c in credits {
        let d = signed(&c)?;
        let e = |e: FnoError| e.message();
        out.insert(
            c,
            json!({
                "decay_target_mark": decay_target_mark(d, &f3).map_err(e)?,
                "loss_cut_mark": loss_cut_mark(d, &f3).map_err(e)?,
            }),
        );
    }
    check(g, &Value::Object(out))
}

fn run_dir_f3_exit(g: &Golden) -> Result<(), String> {
    let position: OpenSpread = input(g, "position")?;
    let f3 = cfg!(g, "f3", F3Config);
    let common = cfg!(g, "common", CommonConfig);
    let out = f3_exit(
        now(g)?,
        &position,
        opt_dec(g, "last_price")?,
        opt_dec(g, "mark")?,
        &f3,
        &common,
    );
    check(g, &plain(out)?)
}

// =============================================================================================
// scan
// =============================================================================================

#[derive(Deserialize)]
struct PrintRow {
    strike: String,
    option_type: OptionType,
    settle: String,
    open_interest: Option<i64>,
    volume: Option<i64>,
}

fn run_scan_propose_condor(g: &Golden) -> Result<(), String> {
    let rows: Vec<PrintRow> = input(g, "prints")?;
    let prints: LegPrints = rows
        .iter()
        .map(|r| {
            Ok((
                (signed(&r.strike)?, r.option_type),
                LegPrint {
                    settle: signed(&r.settle)?,
                    open_interest: r.open_interest,
                    volume: r.volume,
                },
            ))
        })
        .collect::<Result<_, String>>()?;
    let config = cfg!(g, "config", FnoConfig);
    let rates: CostRates = input(g, "rates")?;
    let ceilings = cfg!(g, "ceilings", FnoCeilings);
    let out = propose_condor(
        dec(g, "forward")?,
        input(g, "iv")?,
        input(g, "days_to_expiry")?,
        &decs(g, "call_strikes")?,
        &decs(g, "put_strikes")?,
        &prints,
        input(g, "lot_size")?,
        dec(g, "capital_inr")?,
        &config,
        &rates,
        &ceilings,
    );
    check(g, &capture(out)?)
}

fn run_scan_f2_signals(g: &Golden) -> Result<(), String> {
    let futures = g.input_frame("futures").map_err(s)?;
    let f2 = cfg!(g, "f2", F2Config);
    let series = cfg!(g, "series", SeriesConfig);
    match f2_signals(&futures, date(g, "trade_date")?, &f2, &series) {
        Ok(df) => check(g, &encode_frame(&df).map_err(s)?),
        Err(e) => check(g, &raises(&e)),
    }
}

fn run_scan_propose_future(g: &Golden) -> Result<(), String> {
    let config = cfg!(g, "config", FnoConfig);
    let ceilings = cfg!(g, "ceilings", FnoCeilings);
    let out = propose_future(
        dec(g, "entry")?,
        dec(g, "atr")?,
        input(g, "ann_vol")?,
        input(g, "lot_size")?,
        dec(g, "capital_inr")?,
        &config,
        &ceilings,
    );
    check(g, &capture(out)?)
}

fn run_scan_allocate_capacity(g: &Golden) -> Result<(), String> {
    let ordered: Vec<(String, Option<String>)> = input(g, "ordered")?;
    let open: Vec<Option<String>> = input(g, "open_industries")?;
    let f2 = cfg!(g, "f2", F2Config);
    let out: Map<String, Value> = allocate_capacity(&ordered, &open, &f2)
        .into_iter()
        .map(|(k, v)| (k, json!(v)))
        .collect();
    check(g, &Value::Object(out))
}

// =============================================================================================
// The table: every graded function, its runner and its golden count
// =============================================================================================

type Runner = fn(&Golden) -> Result<(), String>;

/// `(function, runner, goldens)` — the count is asserted, so a case added or lost is noticed.
const RUNNERS: &[(&str, Runner, usize)] = &[
    ("condor.roles", run_condor_roles, 1),
    ("condor.one_sd", run_condor_one_sd, 6),
    ("condor.select_strikes", run_condor_select_strikes, 8),
    ("condor.credit", run_condor_credit, 4),
    ("condor.max_loss_per_unit", run_condor_max_loss_per_unit, 3),
    ("condor.price_structure", run_condor_price_structure, 5),
    (
        "condor.liquidity_refusals",
        run_condor_liquidity_refusals,
        6,
    ),
    ("condor.LegQuote", run_condor_legquote, 1),
    ("condor.cost_to_close", run_condor_cost_to_close, 5),
    ("condor.levels", run_condor_levels, 7),
    ("exits.f1_exit", run_exits_f1_exit, 10),
    ("exits.stop_from_vol", run_exits_stop_from_vol, 2),
    ("exits.f2_initial_stop", run_exits_f2_initial_stop, 3),
    ("exits.f2_trail", run_exits_f2_trail, 4),
    ("exits.gtt_trigger", run_exits_gtt_trigger, 3),
    (
        "exits.f2_time_exit_session",
        run_exits_f2_time_exit_session,
        5,
    ),
    ("exits.f2_exit", run_exits_f2_exit, 11),
    ("exits.f1_paused", run_exits_f1_paused, 7),
    ("exits.f2_month_paused", run_exits_f2_month_paused, 4),
    ("exits.book_paused", run_exits_book_paused, 4),
    ("covered.uncovered", run_covered_uncovered, 12),
    ("covered.book_after", run_covered_book_after, 1),
    ("covered.net_of_plan", run_covered_net_of_plan, 4),
    ("covered.assess_step", run_covered_assess_step, 5),
    (
        "checklist.uncovered_steps",
        run_checklist_uncovered_steps,
        5,
    ),
    ("checklist.into_expiry", run_checklist_into_expiry, 5),
    ("checklist.journal_gap", run_checklist_journal_gap, 2),
    ("checklist.mark_gaps", run_checklist_mark_gaps, 2),
    ("checklist.missed_cycles", run_checklist_missed_cycles, 1),
    (
        "checklist.paper_checklist",
        run_checklist_paper_checklist,
        7,
    ),
    ("monitor.reprice_condor", run_monitor_reprice_condor, 9),
    ("monitor.f1_close_cost", run_monitor_f1_close_cost, 2),
    ("monitor.f1_pnl_inr", run_monitor_f1_pnl_inr, 3),
    ("monitor.reprice_future", run_monitor_reprice_future, 10),
    ("monitor.f2_pnl_inr", run_monitor_f2_pnl_inr, 2),
    ("monitor.f2_trail_step", run_monitor_f2_trail_step, 5),
    ("monitor.f2_action", run_monitor_f2_action, 8),
    (
        "directional.seventy_five_minute_bars",
        run_dir_seventy_five,
        5,
    ),
    ("directional.pivots", run_dir_pivots, 5),
    ("directional.bars_needed", run_dir_bars_needed, 2),
    ("directional.levels", run_dir_levels, 6),
    ("directional.daily_direction", run_dir_daily_direction, 6),
    ("directional.confirm_75", run_dir_confirm_75, 6),
    ("directional.direction_for", run_dir_direction_for, 3),
    ("directional.key_level", run_dir_key_level, 1),
    ("directional.intraday_aligned", run_dir_intraday_aligned, 1),
    ("directional.level_broken", run_dir_level_broken, 2),
    ("directional.spread_strikes", run_dir_spread_strikes, 7),
    ("directional.choose_expiry", run_dir_choose_expiry, 6),
    ("directional.spread_arith", run_dir_spread_arith, 3),
    ("directional.size_entry", run_dir_size_entry, 6),
    ("directional.propose_spread", run_dir_propose_spread, 10),
    ("directional.add_decision", run_dir_add_decision, 11),
    ("directional.f3_marks", run_dir_f3_marks, 1),
    ("directional.f3_exit", run_dir_f3_exit, 10),
    ("scan.propose_condor", run_scan_propose_condor, 10),
    ("scan.f2_signals", run_scan_f2_signals, 7),
    ("scan.propose_future", run_scan_propose_future, 6),
    ("scan.allocate_capacity", run_scan_allocate_capacity, 7),
];

/// Grades every function of one module (`"condor."`, `"exits."`, ...).
fn grade_module(prefix: &str) {
    let mut seen = 0;
    for (function, run, count) in RUNNERS.iter().filter(|(f, _, _)| f.starts_with(prefix)) {
        assert_eq!(grade(function, run), *count, "{function}: golden count");
        seen += 1;
    }
    assert!(seen > 0, "no runner for {prefix}");
}

#[test]
fn condor_goldens() {
    grade_module("condor.");
}

#[test]
fn exits_goldens() {
    grade_module("exits.");
}

#[test]
fn covered_goldens() {
    grade_module("covered.");
}

#[test]
fn checklist_goldens() {
    grade_module("checklist.");
}

#[test]
fn monitor_goldens() {
    grade_module("monitor.");
}

#[test]
fn directional_goldens() {
    grade_module("directional.");
}

#[test]
fn scan_goldens() {
    grade_module("scan.");
}

/// Every function of the seven modules is in the table: the lane's own listing
/// (`ls L1/fno | grep -E '^(condor|covered|...)\.'`) is the answer.
#[test]
fn table_covers_every_golden_function() {
    let root = baskfy_testkit::golden_root().join("L1/fno");
    let modules = [
        "condor",
        "covered",
        "exits",
        "checklist",
        "scan",
        "monitor",
        "directional",
    ];
    let mut functions = std::collections::BTreeSet::new();
    for entry in std::fs::read_dir(root).unwrap() {
        let name = entry.unwrap().file_name().into_string().unwrap();
        let module = name.split('.').next().unwrap_or("");
        if modules.contains(&module) {
            let function = name.split(".case_").next().unwrap().to_owned();
            functions.insert(function);
        }
    }
    let table: std::collections::BTreeSet<String> =
        RUNNERS.iter().map(|(f, _, _)| (*f).to_owned()).collect();
    assert_eq!(functions, table);
}

/// The DOWN spread, the paper add path and the F3 hard exit past expiry — branches the goldens
/// reach only partly — hold Python's arithmetic on hand-checked numbers.
#[test]
fn directional_edges() {
    let f3 = F3Config::default();
    let common = CommonConfig::default();
    let lv = Levels {
        as_of: NaiveDate::from_ymd_opt(2026, 10, 16).unwrap(),
        close: Decimal::new(24300, 0),
        support: None,
        resistance: Some(Decimal::new(25100, 0)),
        weekly_high: Decimal::new(245107, 1),
        weekly_low: Decimal::new(24250, 0),
        trend_avg: Decimal::new(24600, 0),
        pivot_highs: vec![],
        pivot_lows: vec![],
    };
    assert_eq!(daily_direction(&lv), Direction::Down);
    let sp = spread_strikes(Direction::Down, &lv, Decimal::new(100, 0), &f3).unwrap();
    assert_eq!(sp.option_type, OptionType::Ce);
    assert!(sp.wing > sp.short);
    let position = OpenSpread {
        direction: Direction::Down,
        level: Decimal::new(25100, 0),
        strikes: sp,
        expiry: NaiveDate::from_ymd_opt(2026, 10, 27).unwrap(),
        entry_session: NaiveDate::from_ymd_opt(2026, 10, 16).unwrap(),
        entry_credit: Decimal::new(30, 0),
        lots: 1,
        lot_size: 65,
        max_loss_per_lot_inr: Decimal::new(30000, 0),
    };
    let late = PyDateTime::parse("2026-10-28T09:20:00").unwrap();
    let exit = f3_exit(late, &position, None, None, &f3, &common)
        .unwrap()
        .unwrap();
    assert_eq!(exit.message, "expiry 2026-10-27 at 15:00");
}

/// Differential check against the live Python: `FNO_B_FUZZ=<file.jsonl>` names golden-shaped
/// docs (one per line) written by a throwaway generator from `golden_fno.py`'s `render_case`;
/// each is graded with the same runner as the committed goldens. Ignored by default.
#[test]
#[ignore = "needs FNO_B_FUZZ, a generated file of Python outputs"]
fn differential_fuzz() {
    let path = std::env::var("FNO_B_FUZZ").unwrap();
    let text = std::fs::read_to_string(&path).unwrap();
    let mut failures = Vec::new();
    let mut graded = 0;
    for (i, line) in text.lines().enumerate() {
        let doc: Value = serde_json::from_str(line).unwrap();
        let fn_name = doc["fn"].as_str().unwrap().to_owned();
        let function = fn_name.strip_prefix(P).unwrap().to_owned();
        let (_, run, _) = RUNNERS
            .iter()
            .find(|(f, _, _)| *f == function.as_str())
            .unwrap_or_else(|| panic!("no runner for {function}"));
        let g = Golden {
            case: format!("fuzz#{i}"),
            path: std::path::PathBuf::from(&path),
            fn_name: fn_name.clone(),
            inputs: doc["inputs"].clone(),
            output: doc["output"].clone(),
            meta: baskfy_testkit::Meta {
                known_bug: None,
                tolerance: baskfy_testkit::Tolerance::default(),
                notes: None,
                stable: true,
            },
        };
        graded += 1;
        if let Err(e) = run(&g) {
            failures.push(format!("{function} #{i}: {e}\n  inputs: {}", g.inputs));
        }
    }
    assert!(
        failures.is_empty(),
        "{} of {graded} fuzz cases failed:\n{}",
        failures.len(),
        failures
            .iter()
            .take(15)
            .cloned()
            .collect::<Vec<_>>()
            .join("\n")
    );
}
