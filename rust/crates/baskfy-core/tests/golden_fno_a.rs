//! Grades the fno money-path and series ports against every golden of
//! `baskfy_core.fno.{config, calendar, costs, gating, vol, series, underlying, sizing, ledger,
//! journal}` in `go/testdata/golden/L1/fno`. Each case is called from its stored inputs exactly
//! as `tools/parity/golden_fno.py`'s `call_*` function calls the Python; a Python raise is
//! stored as `{"raises": {"type", "message"}}` and the port's error is graded the same way.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::BTreeMap;

use baskfy_core::fno::calendar::{
    PyDateTime, entry_session, f2_contract_expiry, hard_exit_at, hard_exit_session, held_expiry,
    in_entry_window, is_entry_day, is_late_exit, monthly_expiries, monthly_expiry, next_entry,
    plan_expires_at, roll_session, sessions_before, sessions_between,
};
use baskfy_core::fno::config::{
    BookConfig, CommonConfig, F1Config, F2Config, F3Config, FnoCeilings, FnoConfig, FnoError,
    FnoResult, FoSleeve, FutureCostRates, SeriesConfig, StopVolConfig, ceiling_violations,
    f1_sleeve_for, f3_sleeve_for, group_of,
};
use baskfy_core::fno::costs::{
    Fill, RolePrices, condor_fills, condor_round_trip, crossing_cost, future_charges, future_roll,
    future_round_trip,
};
use baskfy_core::fno::gating::{
    CARRY_FLAG_ENV, DESK_SWITCH_ENV, EXECUTION_FLAG_ENV, FnoFlags, OPERATIONAL_FLAG_ENV, fno_gates,
};
use baskfy_core::fno::journal::{
    FoFillRecord, PoolKey, future_costs, journal_figures, one_pool, option_costs, r_multiple,
};
use baskfy_core::fno::ledger::{
    ClosedTrade, FoPause, OpenMark, book_pause_limit, build_ledger, column_pause_applies,
    evaluate_pauses, figures, mode_tag, month_end, month_start, pauses_for,
};
use baskfy_core::fno::series::continuous_futures;
use baskfy_core::fno::sizing::{margin_check, risk_budget, size};
use baskfy_core::fno::underlying::derive_underlying;
use baskfy_core::fno::vol::{OptionPrint, atm_iv, iv_atm_frame, iv_expiry};
use baskfy_core::options::costs::CostRates;
use baskfy_core::options::gating::Mode;
use baskfy_testkit::{Golden, Meta, Tolerance, cases_for, encode_frame};
use chrono::{NaiveDate, NaiveDateTime};
use rust_decimal::Decimal;
use serde::Serialize;
use serde::de::DeserializeOwned;
use serde_json::{Value, json};

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

fn check(g: &Golden, actual: &Value) -> Result<(), String> {
    g.check_value(actual).map_err(s)
}

fn dates(g: &Golden, key: &str) -> Result<Vec<NaiveDate>, String> {
    input(g, key)
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

fn decs(g: &Golden, key: &str) -> Result<Vec<Decimal>, String> {
    let texts: Vec<String> = input(g, key)?;
    texts.iter().map(|t| signed(t)).collect()
}

fn sleeves(g: &Golden) -> Result<Vec<FoSleeve>, String> {
    input(g, "sleeves")
}

fn sleeve_key(sleeve: FoSleeve) -> String {
    sleeve.value().to_owned()
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

fn frame_value(df: &polars::prelude::DataFrame) -> Result<Value, String> {
    encode_frame(df).map_err(s)
}

// =============================================================================================
// runners: each calls the port from a case's inputs as the dumper's `call_*` calls Python
// =============================================================================================

fn opt_date_value(d: Option<NaiveDate>) -> Value {
    json!(d)
}

fn pauses(g: &Golden) -> Result<FnoResult<Vec<FoPause>>, String> {
    let trades: Vec<ClosedTrade> = input(g, "trades")?;
    let simulated: bool = input(g, "simulated")?;
    let as_of = g.input_date("as_of").map_err(s)?;
    let lifted: Option<BTreeMap<FoSleeve, NaiveDateTime>> = input(g, "lifted_after")?;
    let config = cfg!(g, "config", FnoConfig);
    let ceilings = cfg!(g, "ceilings", FnoCeilings);
    Ok(evaluate_pauses(
        &trades,
        simulated,
        as_of,
        dec(g, "monthly_pause_inr")?,
        lifted.as_ref(),
        &config,
        &ceilings,
    ))
}

fn run_config_group_of(g: &Golden) -> Result<(), String> {
    let out: BTreeMap<String, String> = sleeves(g)?
        .into_iter()
        .map(|sl| (sleeve_key(sl), group_of(sl).value().to_owned()))
        .collect();
    check(g, &json!(out))
}

fn run_config_f1_sleeve_for(g: &Golden) -> Result<(), String> {
    let u: String = input(g, "underlying")?;
    check(g, &capture(f1_sleeve_for(&u))?)
}

fn run_config_f3_sleeve_for(g: &Golden) -> Result<(), String> {
    let u: String = input(g, "underlying")?;
    check(g, &capture(f3_sleeve_for(&u))?)
}

fn run_config_fnoconfig_risk_per_trade_pct(g: &Golden) -> Result<(), String> {
    let config = cfg!(g, "config", FnoConfig);
    let out: BTreeMap<String, Decimal> = sleeves(g)?
        .into_iter()
        .map(|sl| (sleeve_key(sl), config.risk_per_trade_pct(sl)))
        .collect();
    check(g, &json!(out))
}

fn run_config_ceiling_violations(g: &Golden) -> Result<(), String> {
    let config = cfg!(g, "config", FnoConfig);
    let ceilings = cfg!(g, "ceilings", FnoCeilings);
    check(g, &json!(ceiling_violations(&config, &ceilings)))
}

fn run_config_construct(g: &Golden) -> Result<(), String> {
    let class: String = input(g, "class")?;
    let raw = g.input("overrides").map_err(s)?.clone();
    fn build<T: DeserializeOwned + Serialize>(
        raw: Value,
        validate: impl Fn(&T) -> FnoResult<()>,
    ) -> Result<Value, String> {
        let c: T = serde_json::from_value(raw).map_err(s)?;
        capture(validate(&c).map(|()| c))
    }
    let out = match class.as_str() {
        "FnoConfig" => build::<FnoConfig>(raw, FnoConfig::validate),
        "CommonConfig" => build::<CommonConfig>(raw, CommonConfig::validate),
        "F1Config" => build::<F1Config>(raw, F1Config::validate),
        "F2Config" => build::<F2Config>(raw, F2Config::validate),
        "F3Config" => build::<F3Config>(raw, F3Config::validate),
        "StopVolConfig" => build::<StopVolConfig>(raw, StopVolConfig::validate),
        "SeriesConfig" => build::<SeriesConfig>(raw, SeriesConfig::validate),
        "BookConfig" => build::<BookConfig>(raw, BookConfig::validate),
        "FnoCeilings" => build::<FnoCeilings>(raw, FnoCeilings::validate),
        "FutureCostRates" => build::<FutureCostRates>(raw, |_| Ok(())),
        other => Err(format!("no class {other}")),
    }?;
    check(g, &out)
}

fn run_gating_fno_gates(g: &Golden) -> Result<(), String> {
    let sleeve: FoSleeve = input(g, "sleeve")?;
    let flags: FnoFlags = input(g, "flags")?;
    check(
        g,
        &serde_json::to_value(fno_gates(sleeve, &flags)).map_err(s)?,
    )
}

fn run_gating_fnoflags_execution_enabled(g: &Golden) -> Result<(), String> {
    let flags: FnoFlags = input(g, "flags")?;
    let out: BTreeMap<String, bool> = sleeves(g)?
        .into_iter()
        .map(|sl| (sleeve_key(sl), flags.execution_enabled(sl)))
        .collect();
    check(g, &json!(out))
}

fn run_gating_constants(g: &Golden) -> Result<(), String> {
    let env: BTreeMap<&str, &str> = EXECUTION_FLAG_ENV
        .iter()
        .map(|(group, name)| (group.value(), *name))
        .collect();
    check(
        g,
        &json!({
            "EXECUTION_FLAG_ENV": env,
            "CARRY_FLAG_ENV": CARRY_FLAG_ENV,
            "OPERATIONAL_FLAG_ENV": OPERATIONAL_FLAG_ENV,
            "DESK_SWITCH_ENV": DESK_SWITCH_ENV,
            "default_flags": FnoFlags::default(),
        }),
    )
}

fn run_calendar_monthly_expiries(g: &Golden) -> Result<(), String> {
    check(g, &json!(monthly_expiries(&dates(g, "expiries")?)))
}

fn run_calendar_monthly_expiry(g: &Golden) -> Result<(), String> {
    let year: i32 = input(g, "year")?;
    let month: u32 = input(g, "month")?;
    check(
        g,
        &opt_date_value(monthly_expiry(&dates(g, "expiries")?, year, month)),
    )
}

fn run_calendar_sessions_before(g: &Golden) -> Result<(), String> {
    let anchor = g.input_date("anchor").map_err(s)?;
    let n: i64 = input(g, "n")?;
    check(
        g,
        &capture(sessions_before(&dates(g, "sessions")?, anchor, n))?,
    )
}

fn run_calendar_sessions_between(g: &Golden) -> Result<(), String> {
    let start = g.input_date("start").map_err(s)?;
    let end = g.input_date("end").map_err(s)?;
    check(
        g,
        &json!(sessions_between(&dates(g, "sessions")?, start, end)),
    )
}

fn run_calendar_entry_session(g: &Golden) -> Result<(), String> {
    let expiry = g.input_date("expiry").map_err(s)?;
    let n: i64 = input(g, "n")?;
    check(
        g,
        &capture(entry_session(&dates(g, "sessions")?, expiry, n))?,
    )
}

fn run_calendar_hard_exit_session(g: &Golden) -> Result<(), String> {
    let expiry = g.input_date("expiry").map_err(s)?;
    let n: i64 = input(g, "n")?;
    check(
        g,
        &capture(hard_exit_session(&dates(g, "sessions")?, expiry, n))?,
    )
}

fn run_calendar_hard_exit_at(g: &Golden) -> Result<(), String> {
    let expiry = g.input_date("expiry").map_err(s)?;
    let common = cfg!(g, "common", CommonConfig);
    let at = hard_exit_at(&dates(g, "sessions")?, expiry, &common).map_err(s)?;
    check(g, &json!(at.map(|a| PyDateTime::Naive(a).isoformat())))
}

fn run_calendar_is_late_exit(g: &Golden) -> Result<(), String> {
    let hx = g.input_date("hard_exit_date").map_err(s)?;
    let today = g.input_date("today").map_err(s)?;
    check(g, &json!(is_late_exit(hx, today)))
}

fn run_calendar_next_entry(g: &Golden) -> Result<(), String> {
    let today = g.input_date("today").map_err(s)?;
    let n: i64 = input(g, "n")?;
    let found = next_entry(today, &dates(g, "expiries")?, &dates(g, "sessions")?, n).map_err(s)?;
    check(g, &json!(found.map(|(e, x)| vec![e, x])))
}

fn run_calendar_is_entry_day(g: &Golden) -> Result<(), String> {
    let today = g.input_date("today").map_err(s)?;
    let n: i64 = input(g, "n")?;
    let found =
        is_entry_day(today, &dates(g, "expiries")?, &dates(g, "sessions")?, n).map_err(s)?;
    check(g, &opt_date_value(found))
}

fn run_calendar_plan_expires_at(g: &Golden) -> Result<(), String> {
    let issued: PyDateTime = input(g, "issued")?;
    let f1 = cfg!(g, "f1", F1Config);
    let common = cfg!(g, "common", CommonConfig);
    check(g, &capture(plan_expires_at(issued, &f1, &common))?)
}

fn run_calendar_in_entry_window(g: &Golden) -> Result<(), String> {
    let f1 = cfg!(g, "f1", F1Config);
    let nows: Vec<String> = input(g, "nows")?;
    let mut out = BTreeMap::new();
    for now in nows {
        let at = PyDateTime::parse(&now).map_err(s)?;
        out.insert(now, in_entry_window(at, &f1));
    }
    check(g, &json!(out))
}

fn run_calendar_held_expiry(g: &Golden) -> Result<(), String> {
    let prev = g.input_date("previous_session").map_err(s)?;
    check(
        g,
        &opt_date_value(held_expiry(prev, &dates(g, "expiries")?)),
    )
}

fn run_calendar_f2_contract_expiry(g: &Golden) -> Result<(), String> {
    let day = g.input_date("day").map_err(s)?;
    let min: i64 = input(g, "min_sessions")?;
    check(
        g,
        &opt_date_value(f2_contract_expiry(
            day,
            &dates(g, "expiries")?,
            &dates(g, "sessions")?,
            min,
        )),
    )
}

fn run_calendar_roll_session(g: &Golden) -> Result<(), String> {
    let expiry = g.input_date("expiry").map_err(s)?;
    let n: i64 = input(g, "n")?;
    check(
        g,
        &capture(roll_session(&dates(g, "sessions")?, expiry, n))?,
    )
}

fn run_costs_crossing_cost(g: &Golden) -> Result<(), String> {
    let f1 = cfg!(g, "config", F1Config);
    let premiums = decs(g, "premiums")?;
    let out = premiums
        .into_iter()
        .map(|p| crossing_cost(p, &f1))
        .collect::<FnoResult<Vec<_>>>()
        .map_err(s)?;
    check(g, &json!(out))
}

fn run_costs_condor_fills(g: &Golden) -> Result<(), String> {
    let entry: RolePrices = input(g, "entry")?;
    let exit: RolePrices = input(g, "exit")?;
    let quantity: i64 = input(g, "quantity")?;
    let fills =
        condor_fills(&entry, &exit, quantity).map(|f| f.into_iter().map(Fill).collect::<Vec<_>>());
    check(g, &capture(fills)?)
}

fn run_costs_condor_round_trip(g: &Golden) -> Result<(), String> {
    let entry: RolePrices = input(g, "entry")?;
    let exit: Option<RolePrices> = input(g, "exit")?;
    let quantity: i64 = input(g, "quantity")?;
    let rates: CostRates = input(g, "rates")?;
    let f1 = cfg!(g, "config", F1Config);
    check(
        g,
        &capture(condor_round_trip(
            &entry,
            exit.as_ref(),
            quantity,
            &rates,
            &f1,
        ))?,
    )
}

fn run_costs_future_charges(g: &Golden) -> Result<(), String> {
    let rates: FutureCostRates = input(g, "rates")?;
    let orders: i64 = input(g, "orders")?;
    let out = future_charges(dec(g, "buys_inr")?, dec(g, "sells_inr")?, orders, &rates);
    check(g, &capture(out)?)
}

fn run_costs_future_round_trip(g: &Golden) -> Result<(), String> {
    let rates: FutureCostRates = input(g, "rates")?;
    let q: i64 = input(g, "quantity")?;
    let out = future_round_trip(dec(g, "entry")?, dec(g, "exit")?, q, &rates);
    check(g, &capture(out)?)
}

fn run_costs_future_roll(g: &Golden) -> Result<(), String> {
    let rates: FutureCostRates = input(g, "rates")?;
    let q: i64 = input(g, "quantity")?;
    let out = future_roll(dec(g, "sell_old")?, dec(g, "buy_new")?, q, &rates);
    check(g, &capture(out)?)
}

fn run_sizing_risk_budget(g: &Golden) -> Result<(), String> {
    let ceilings = cfg!(g, "ceilings", FnoCeilings);
    let out = risk_budget(dec(g, "capital_inr")?, dec(g, "risk_pct")?, &ceilings);
    check(g, &capture(out)?)
}

fn run_sizing_size(g: &Golden) -> Result<(), String> {
    let mode: Mode = input(g, "mode")?;
    let lot_size: Option<i64> = input(g, "lot_size")?;
    let common = cfg!(g, "common", CommonConfig);
    let ceilings = cfg!(g, "ceilings", FnoCeilings);
    let out = size(
        mode,
        dec(g, "capital_inr")?,
        dec(g, "risk_pct")?,
        dec(g, "risk_per_unit")?,
        lot_size,
        &common,
        &ceilings,
    );
    check(g, &capture(out)?)
}

fn run_sizing_margin_check(g: &Golden) -> Result<(), String> {
    let out = margin_check(dec(g, "required_inr")?, dec(g, "free_inr")?);
    check(g, &serde_json::to_value(out).map_err(s)?)
}

fn run_journal_r_multiple(g: &Golden) -> Result<(), String> {
    check(
        g,
        &capture(r_multiple(dec(g, "net_pnl_inr")?, dec(g, "r_inr")?))?,
    )
}

fn run_journal_option_costs(g: &Golden) -> Result<(), String> {
    let fills: Vec<FoFillRecord> = input(g, "fills")?;
    let rates: CostRates = input(g, "rates")?;
    check(g, &capture(option_costs(&fills, &rates))?)
}

fn run_journal_future_costs(g: &Golden) -> Result<(), String> {
    let fills: Vec<FoFillRecord> = input(g, "fills")?;
    let rates: FutureCostRates = input(g, "rates")?;
    check(g, &capture(future_costs(&fills, &rates))?)
}

fn run_journal_journal_figures(g: &Golden) -> Result<(), String> {
    let fills: Vec<FoFillRecord> = input(g, "fills")?;
    let marks = decs(g, "marks_inr")?;
    let option_rates: CostRates = input(g, "option_rates")?;
    let future_rates: FutureCostRates = input(g, "future_rates")?;
    let out = journal_figures(
        input(g, "structure")?,
        &fills,
        dec(g, "r_inr")?,
        &marks,
        &option_rates,
        &future_rates,
    )
    .map(|f| json!({"figures": f, "cost_detail": f.cost_detail()}));
    check(g, &capture(out)?)
}

fn run_journal_one_pool(g: &Golden) -> Result<(), String> {
    let keys: Vec<PoolKey> = input(g, "keys")?;
    check(g, &capture(one_pool(&keys))?)
}

fn run_ledger_month_bounds(g: &Golden) -> Result<(), String> {
    let days: Vec<NaiveDate> = input(g, "days")?;
    let out: BTreeMap<String, Value> = days
        .into_iter()
        .map(|d| {
            (
                d.to_string(),
                json!({"start": month_start(d), "end": month_end(d)}),
            )
        })
        .collect();
    check(g, &json!(out))
}

fn run_ledger_figures(g: &Golden) -> Result<(), String> {
    let trades: Vec<ClosedTrade> = input(g, "trades")?;
    let marks: Vec<OpenMark> = input(g, "marks")?;
    let as_of = g.input_date("as_of").map_err(s)?;
    check(g, &capture(figures(&trades, &marks, as_of))?)
}

fn run_ledger_build_ledger(g: &Golden) -> Result<(), String> {
    let trades: Vec<ClosedTrade> = input(g, "trades")?;
    let marks: Vec<OpenMark> = input(g, "marks")?;
    let as_of = g.input_date("as_of").map_err(s)?;
    check(g, &capture(build_ledger(&trades, &marks, as_of))?)
}

fn run_ledger_mode_tag(g: &Golden) -> Result<(), String> {
    let modes: Vec<bool> = input(g, "simulated")?;
    let out: BTreeMap<String, &str> = modes
        .into_iter()
        .map(|m| (m.to_string(), mode_tag(m)))
        .collect();
    check(g, &json!(out))
}

fn run_ledger_book_pause_limit(g: &Golden) -> Result<(), String> {
    let ceilings = cfg!(g, "ceilings", FnoCeilings);
    check(
        g,
        &json!(book_pause_limit(dec(g, "monthly_pause_inr")?, &ceilings)),
    )
}

fn run_ledger_evaluate_pauses(g: &Golden) -> Result<(), String> {
    check(g, &capture(pauses(g)?)?)
}

fn run_ledger_pauses_for(g: &Golden) -> Result<(), String> {
    let found = pauses(g)?.map_err(s)?;
    let out: BTreeMap<String, Value> = sleeves(g)?
        .into_iter()
        .map(|sl| (sleeve_key(sl), json!(pauses_for(&found, sl))))
        .collect();
    check(g, &json!(out))
}

fn run_ledger_column_pause_applies(g: &Golden) -> Result<(), String> {
    let cases: Vec<Value> = input(g, "cases")?;
    let mut out = Vec::new();
    for c in cases {
        let until: Option<NaiveDate> =
            serde_json::from_value(c["paused_until"].clone()).map_err(s)?;
        let reason: Option<String> =
            serde_json::from_value(c["paused_reason"].clone()).map_err(s)?;
        let simulated: bool = serde_json::from_value(c["simulated"].clone()).map_err(s)?;
        let day: NaiveDate = serde_json::from_value(c["day"].clone()).map_err(s)?;
        out.push(column_pause_applies(
            until,
            reason.as_deref(),
            simulated,
            day,
        ));
    }
    check(g, &json!(out))
}

fn run_vol_iv_expiry(g: &Golden) -> Result<(), String> {
    let day = g.input_date("day").map_err(s)?;
    let min_left: i64 = input(g, "min_left")?;
    check(
        g,
        &opt_date_value(iv_expiry(
            day,
            &dates(g, "expiries")?,
            &dates(g, "sessions")?,
            min_left,
        )),
    )
}

fn run_vol_atm_iv(g: &Golden) -> Result<(), String> {
    let day = g.input_date("day").map_err(s)?;
    let options: Vec<OptionPrint> = input(g, "options")?;
    let settle: BTreeMap<NaiveDate, f64> = input(g, "futures_settle")?;
    let config = cfg!(g, "config", SeriesConfig);
    let found = atm_iv(day, &options, &settle, &dates(g, "sessions")?, &config);
    check(g, &serde_json::to_value(found).map_err(s)?)
}

fn run_vol_iv_atm_frame(g: &Golden) -> Result<(), String> {
    let options = g.input_frame("options").map_err(s)?;
    let futures = g.input_frame("futures").map_err(s)?;
    let config = cfg!(g, "config", SeriesConfig);
    let out = iv_atm_frame(&options, &futures, &dates(g, "sessions")?, &config);
    match out {
        Ok(df) => check(g, &frame_value(&df)?),
        Err(e) => check(g, &raises(&e)),
    }
}

fn run_series_continuous_futures(g: &Golden) -> Result<(), String> {
    let futures = g.input_frame("futures").map_err(s)?;
    let config = cfg!(g, "config", SeriesConfig);
    match continuous_futures(&futures, &config) {
        Ok(df) => check(g, &frame_value(&df)?),
        Err(e) => check(g, &raises(&e)),
    }
}

fn run_underlying_derive_underlying(g: &Golden) -> Result<(), String> {
    let window = g.input_frame("window").map_err(s)?;
    let trade_date = g.input_date("trade_date").map_err(s)?;
    let config = cfg!(g, "config", SeriesConfig);
    match derive_underlying(&window, trade_date, &dates(g, "sessions")?, &config) {
        Ok(df) => check(g, &frame_value(&df)?),
        Err(e) => check(g, &raises(&e)),
    }
}

/// Every Rust default is the number `04` prints, as the Python defaults the goldens echo.
#[test]
fn config_defaults_are_pythons() {
    let g = &cases_for(&format!("{P}config.construct")).unwrap()[0];
    let expected = g.output.clone();
    let actual = serde_json::to_value(FnoConfig::default()).unwrap();
    baskfy_testkit::compare(&expected, &actual, &g.meta.tolerance).unwrap();
}

/// Non-negotiable #1's third exception reads this gate: only all four switches on is LIVE,
/// for every sleeve, over every combination (not only the 24 the goldens hold).
#[test]
fn gating_is_live_only_with_all_four_switches() {
    for sleeve in FoSleeve::ALL {
        for bits in 0..64u32 {
            let flags = FnoFlags {
                dry_run: bits & 1 != 0,
                options_enabled: bits & 2 != 0,
                fno_carry_enabled: bits & 4 != 0,
                f1_execution_enabled: bits & 8 != 0,
                f2_execution_enabled: bits & 16 != 0,
                f3_execution_enabled: bits & 32 != 0,
            };
            let gate = fno_gates(sleeve, &flags);
            let all_four = !flags.dry_run
                && flags.options_enabled
                && flags.fno_carry_enabled
                && flags.execution_enabled(sleeve);
            assert_eq!(gate.mode == Mode::Live, all_four, "{sleeve:?} {flags:?}");
            assert_eq!(gate.dry_run, !all_four);
            assert!(!gate.intraday_enabled);
        }
        assert_eq!(fno_gates(sleeve, &FnoFlags::default()).mode, Mode::Paper);
    }
}

// =============================================================================================
// The table: every graded function, its runner and its golden count
// =============================================================================================

type Runner = fn(&Golden) -> Result<(), String>;

/// `(function, runner, goldens)` — the count is asserted, so a case added or lost is noticed.
const RUNNERS: &[(&str, Runner, usize)] = &[
    ("config.group_of", run_config_group_of, 1),
    ("config.f1_sleeve_for", run_config_f1_sleeve_for, 4),
    ("config.f3_sleeve_for", run_config_f3_sleeve_for, 3),
    (
        "config.FnoConfig.risk_per_trade_pct",
        run_config_fnoconfig_risk_per_trade_pct,
        2,
    ),
    (
        "config.ceiling_violations",
        run_config_ceiling_violations,
        5,
    ),
    ("config.construct", run_config_construct, 19),
    ("gating.fno_gates", run_gating_fno_gates, 24),
    (
        "gating.FnoFlags.execution_enabled",
        run_gating_fnoflags_execution_enabled,
        4,
    ),
    ("gating.constants", run_gating_constants, 1),
    (
        "calendar.monthly_expiries",
        run_calendar_monthly_expiries,
        3,
    ),
    ("calendar.monthly_expiry", run_calendar_monthly_expiry, 3),
    ("calendar.sessions_before", run_calendar_sessions_before, 5),
    (
        "calendar.sessions_between",
        run_calendar_sessions_between,
        3,
    ),
    ("calendar.entry_session", run_calendar_entry_session, 3),
    (
        "calendar.hard_exit_session",
        run_calendar_hard_exit_session,
        4,
    ),
    ("calendar.hard_exit_at", run_calendar_hard_exit_at, 3),
    ("calendar.is_late_exit", run_calendar_is_late_exit, 3),
    ("calendar.next_entry", run_calendar_next_entry, 5),
    ("calendar.is_entry_day", run_calendar_is_entry_day, 3),
    ("calendar.plan_expires_at", run_calendar_plan_expires_at, 4),
    ("calendar.in_entry_window", run_calendar_in_entry_window, 1),
    ("calendar.held_expiry", run_calendar_held_expiry, 3),
    (
        "calendar.f2_contract_expiry",
        run_calendar_f2_contract_expiry,
        5,
    ),
    ("calendar.roll_session", run_calendar_roll_session, 3),
    ("costs.crossing_cost", run_costs_crossing_cost, 2),
    ("costs.condor_fills", run_costs_condor_fills, 2),
    ("costs.condor_round_trip", run_costs_condor_round_trip, 7),
    ("costs.future_charges", run_costs_future_charges, 5),
    ("costs.future_round_trip", run_costs_future_round_trip, 3),
    ("costs.future_roll", run_costs_future_roll, 2),
    ("sizing.risk_budget", run_sizing_risk_budget, 6),
    ("sizing.size", run_sizing_size, 14),
    ("sizing.margin_check", run_sizing_margin_check, 4),
    ("journal.r_multiple", run_journal_r_multiple, 6),
    ("journal.option_costs", run_journal_option_costs, 2),
    ("journal.future_costs", run_journal_future_costs, 2),
    ("journal.journal_figures", run_journal_journal_figures, 6),
    ("journal.one_pool", run_journal_one_pool, 4),
    ("ledger.month_bounds", run_ledger_month_bounds, 1),
    ("ledger.figures", run_ledger_figures, 7),
    ("ledger.build_ledger", run_ledger_build_ledger, 3),
    ("ledger.mode_tag", run_ledger_mode_tag, 1),
    ("ledger.book_pause_limit", run_ledger_book_pause_limit, 4),
    ("ledger.evaluate_pauses", run_ledger_evaluate_pauses, 10),
    ("ledger.pauses_for", run_ledger_pauses_for, 2),
    (
        "ledger.column_pause_applies",
        run_ledger_column_pause_applies,
        1,
    ),
    ("vol.iv_expiry", run_vol_iv_expiry, 4),
    ("vol.atm_iv", run_vol_atm_iv, 6),
    ("vol.iv_atm_frame", run_vol_iv_atm_frame, 3),
    (
        "series.continuous_futures",
        run_series_continuous_futures,
        7,
    ),
    (
        "underlying.derive_underlying",
        run_underlying_derive_underlying,
        7,
    ),
];

/// Grades every function of one module (`"config."`, `"calendar."`, ...).
fn grade_module(prefix: &str) {
    let mut seen = 0;
    for (function, run, count) in RUNNERS.iter().filter(|(f, _, _)| f.starts_with(prefix)) {
        assert_eq!(grade(function, run), *count, "{function}: golden count");
        seen += 1;
    }
    assert!(seen > 0, "no runner for {prefix}");
}

#[test]
fn config_goldens() {
    grade_module("config.");
}

#[test]
fn gating_goldens() {
    grade_module("gating.");
}

#[test]
fn calendar_goldens() {
    grade_module("calendar.");
}

#[test]
fn costs_goldens() {
    grade_module("costs.");
}

#[test]
fn sizing_goldens() {
    grade_module("sizing.");
}

#[test]
fn journal_goldens() {
    grade_module("journal.");
}

#[test]
fn ledger_goldens() {
    grade_module("ledger.");
}

#[test]
fn vol_goldens() {
    grade_module("vol.");
}

#[test]
fn series_goldens() {
    grade_module("series.");
}

#[test]
fn underlying_goldens() {
    grade_module("underlying.");
}

/// Every function of the ten modules is in the table, and every one of its goldens exists:
/// the fno lane's own listing (`ls L1/fno | grep config|calendar|...`) is the answer.
#[test]
fn table_covers_every_golden_function() {
    let root = baskfy_testkit::golden_root().join("L1/fno");
    let mut functions = std::collections::BTreeSet::new();
    for entry in std::fs::read_dir(root).unwrap() {
        let name = entry.unwrap().file_name().into_string().unwrap();
        let module = name.split('.').next().unwrap_or("");
        if [
            "config",
            "calendar",
            "costs",
            "gating",
            "vol",
            "series",
            "underlying",
            "sizing",
            "ledger",
            "journal",
        ]
        .contains(&module)
        {
            let stem = name.rsplit_once(".case_").unwrap().0.to_owned();
            functions.insert(stem);
        }
    }
    let table: std::collections::BTreeSet<String> =
        RUNNERS.iter().map(|(f, _, _)| (*f).to_owned()).collect();
    assert_eq!(functions, table);
}

/// Differential check against the live Python: `BASKFY_FNO_FUZZ_DIR` holds cases written by
/// the scratch generator in the golden format from random inputs; each is graded at **zero**
/// float tolerance. Run with `cargo test --test golden_fno_a -- --ignored fuzz`.
#[test]
#[ignore = "needs BASKFY_FNO_FUZZ_DIR from the Python generator"]
fn fuzz_against_python() {
    let Ok(dir) = std::env::var("BASKFY_FNO_FUZZ_DIR") else {
        return;
    };
    let tolerance: f64 = std::env::var("BASKFY_FNO_FUZZ_TOL")
        .ok()
        .and_then(|t| t.parse().ok())
        .unwrap_or(0.0);
    let mut failures = Vec::new();
    let mut per_fn: BTreeMap<String, usize> = BTreeMap::new();
    for entry in std::fs::read_dir(&dir).unwrap() {
        let path = entry.unwrap().path();
        let doc: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        let fn_name = doc["fn"].as_str().unwrap().to_owned();
        let short = fn_name.trim_start_matches(P).to_owned();
        let (_, run, _) = RUNNERS
            .iter()
            .find(|(f, _, _)| *f == short)
            .unwrap_or_else(|| panic!("no runner for {fn_name}"));
        let g = Golden {
            case: path.display().to_string(),
            path: path.clone(),
            fn_name,
            inputs: doc["inputs"].clone(),
            output: doc["output"].clone(),
            meta: Meta {
                known_bug: None,
                tolerance: Tolerance { float: tolerance },
                notes: None,
                stable: true,
            },
        };
        *per_fn.entry(short).or_default() += 1;
        if let Err(e) = run(&g) {
            failures.push(format!("{}: {e}", path.display()));
        }
    }
    let total: usize = per_fn.values().sum();
    assert!(
        failures.is_empty(),
        "{} of {total} fuzz cases differ ({per_fn:?}):\n{}",
        failures.len(),
        failures
            .iter()
            .take(40)
            .cloned()
            .collect::<Vec<_>>()
            .join("\n")
    );
}
