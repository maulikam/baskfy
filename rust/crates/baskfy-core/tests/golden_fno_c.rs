//! Grades the fno research ports against every golden of
//! `baskfy_core.fno.{research, directional_retest, retest}` in `go/testdata/golden/L1/fno`.
//! Each case is called from its stored inputs exactly as `tools/parity/golden_fno.py`'s `call_*`
//! function calls the Python (the compositions its notes name included); a Python raise is
//! stored as `{"raises": {"type", "message"}}` and the port's error is graded the same way.
#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

use std::collections::{BTreeMap, BTreeSet};

use baskfy_core::fno::config::{F3Config, FnoError, FnoResult};
use baskfy_core::fno::directional_retest::{
    self as dr, IndexPanel, choose_expiry, index_panel, monthly_expiries, run_f3_retest,
    run_symbol, sessions_left, strike_step,
};
use baskfy_core::fno::research::{
    self as r, CondorParams, DebitStructure, FuturesFamily, FuturesParams, Summary,
};
use baskfy_core::fno::retest::{
    self as rt, RunKnobs, families, family_keys, family_slippage, panels_from_contracts,
    retest_due, run_family, slice_loader, trim_index_contracts,
};
use baskfy_core::fno::vol::OptionType;
use baskfy_testkit::{Golden, cases_for, encode_frame};
use chrono::NaiveDate;
use polars::prelude::DataFrame;
use rust_decimal::Decimal;
use serde::de::DeserializeOwned;
use serde_json::{Map, Value, json};

const P: &str = "baskfy_core.fno.";

/// Runs every case of `function`, collecting failures so one report names them all; returns
/// the count.
fn grade(function: &str, run: impl Fn(&Golden) -> Result<(), String>) -> (usize, String) {
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
    let report = if failures.is_empty() {
        String::new()
    } else {
        format!(
            "{name}: {} of {} cases failed:\n{}\n",
            failures.len(),
            cases.len(),
            failures.join("\n")
        )
    };
    (cases.len(), report)
}

/// Every function's case count asserted, and every failure reported at once.
fn settle(counts: &[(&str, (usize, String), usize)]) {
    let mut errors = String::new();
    for (name, (got, report), want) in counts {
        errors.push_str(report);
        if got != want {
            errors.push_str(&format!("{name}: {got} cases, expected {want}\n"));
        }
    }
    assert!(errors.is_empty(), "{errors}");
}

fn s<E: std::fmt::Display>(e: E) -> String {
    e.to_string()
}

fn input<T: DeserializeOwned>(g: &Golden, key: &str) -> Result<T, String> {
    g.input_as(key).map_err(s)
}

fn frame_in(g: &Golden, key: &str) -> Result<DataFrame, String> {
    g.input_frame(key).map_err(s)
}

fn frame_of(v: &Value) -> Result<DataFrame, String> {
    baskfy_testkit::decode_frame(v).map_err(s)
}

fn enc(df: &DataFrame) -> Result<Value, String> {
    encode_frame(df).map_err(s)
}

fn raises(e: &FnoError) -> Value {
    json!({"raises": {"type": e.python_type(), "message": e.message()}})
}

fn check(g: &Golden, actual: &Value) -> Result<(), String> {
    g.check_value(actual).map_err(s)
}

fn plain<T>(result: FnoResult<T>) -> Result<T, String> {
    result.map_err(|e| format!("{}: {}", e.python_type(), e.message()))
}

fn date(text: &str) -> Result<NaiveDate, String> {
    NaiveDate::parse_from_str(text, "%Y-%m-%d").map_err(s)
}

fn opt_date(g: &Golden, key: &str) -> Result<Option<NaiveDate>, String> {
    let t: Option<String> = input(g, key)?;
    t.as_deref().map(date).transpose()
}

macro_rules! cfg {
    ($g:expr, $key:expr, $ty:ty) => {{
        let c: $ty = input($g, $key)?;
        c.validate().map_err(|e| e.message())?;
        c
    }};
}

/// Python's `repr(float)` for the short floats the dumper uses as keys (`0.6`, `1.0`, `0.25`).
fn float_key(x: f64) -> String {
    if x.fract() == 0.0 {
        format!("{x:.1}")
    } else {
        format!("{x}")
    }
}

fn summary_value(summary: &Summary) -> Result<Value, String> {
    serde_json::to_value(summary).map_err(s)
}

// =============================================================================================
// research
// =============================================================================================

fn run_research_constants(g: &Golden) -> Result<(), String> {
    let actual = json!({
        "RESEARCH_RATE": r::RESEARCH_RATE,
        "FUTURES_ROUND_TRIP_COST": r::FUTURES_ROUND_TRIP_COST,
        "INDEX_UNDERLYINGS": r::INDEX_UNDERLYINGS,
        "B4": r::b4(),
        "B4_LOSS_CLOSE": r::b4_loss_close(),
        "F2_SPEC": r::f2_spec(),
        "CondorParams()": CondorParams::default(),
    });
    check(g, &actual)
}

fn run_continuous(g: &Golden) -> Result<(), String> {
    let actual = match r::continuous(&frame_in(g, "futures")?) {
        Ok(df) => enc(&df)?,
        Err(e) => raises(&e),
    };
    check(g, &actual)
}

fn run_b76(g: &Golden) -> Result<(), String> {
    let out = plain(r::b76(
        &input::<Vec<f64>>(g, "forward")?,
        &input::<Vec<f64>>(g, "strike")?,
        &input::<Vec<f64>>(g, "years")?,
        &input::<Vec<f64>>(g, "vol")?,
        &input::<Vec<String>>(g, "kind")?,
        input(g, "rate")?,
    ))?;
    check(g, &json!(out))
}

fn run_implied(g: &Golden) -> Result<(), String> {
    let out = plain(r::implied(
        &input::<Vec<f64>>(g, "forward")?,
        &input::<Vec<f64>>(g, "strike")?,
        &input::<Vec<f64>>(g, "years")?,
        &input::<Vec<f64>>(g, "price")?,
        &input::<Vec<String>>(g, "kind")?,
        input(g, "rate")?,
    ))?;
    check(g, &json!(out))
}

fn run_leg_costs(g: &Golden) -> Result<(), String> {
    let params: CondorParams = input(g, "params")?;
    let cases: Vec<Value> = input(g, "cases")?;
    let out: Vec<f64> = cases
        .iter()
        .map(|c| {
            r::leg_costs(
                c["price"].as_f64().unwrap(),
                c["sell"].as_bool().unwrap(),
                c["lot"].as_f64().unwrap(),
                &params,
            )
        })
        .collect();
    check(g, &json!(out))
}

fn run_summarise(g: &Golden) -> Result<(), String> {
    let r_column: String = input(g, "r_column")?;
    let date_column: String = input(g, "date_column")?;
    let actual = match r::summarise(&frame_in(g, "trades")?, &r_column, &date_column) {
        Ok(sm) => summary_value(&sm)?,
        Err(e) => raises(&e),
    };
    check(g, &actual)
}

fn run_futures_panel(g: &Golden) -> Result<(), String> {
    let df = plain(r::futures_panel(&frame_in(g, "cont")?))?;
    check(g, &enc(&df)?)
}

fn run_liquid_base(g: &Golden) -> Result<(), String> {
    let panel = plain(r::futures_panel(&frame_in(g, "cont")?))?;
    let shares: Vec<f64> = input(g, "shares")?;
    let mut out = Map::new();
    for share in shares {
        out.insert(
            float_key(share),
            enc(&plain(r::liquid_base(&panel, share))?)?,
        );
    }
    check(g, &Value::Object(out))
}

fn family_of(name: &str) -> Result<FuturesFamily, String> {
    serde_json::from_value(json!(name)).map_err(s)
}

fn run_futures_signals(g: &Golden) -> Result<(), String> {
    let panel = plain(r::futures_panel(&frame_in(g, "cont")?))?;
    let fams: Vec<String> = input(g, "families")?;
    let mut out = Map::new();
    for f in fams {
        let df = plain(r::futures_signals(&panel, family_of(&f)?))?;
        out.insert(f, enc(&df)?);
    }
    check(g, &Value::Object(out))
}

fn run_simulate_futures(g: &Golden) -> Result<(), String> {
    let panel = plain(r::futures_panel(&frame_in(g, "cont")?))?;
    let runs: Vec<Value> = input(g, "runs")?;
    let mut out = Vec::new();
    for run in runs {
        let signals = match run.get("signals") {
            Some(v) if !v.is_null() => frame_of(v)?,
            _ => plain(r::futures_signals(
                &panel,
                family_of(run["family"].as_str().unwrap())?,
            ))?,
        };
        let params: FuturesParams = serde_json::from_value(run["params"].clone()).map_err(s)?;
        out.push(enc(&plain(r::simulate_futures(
            &panel, &signals, &params,
        ))?)?);
    }
    check(g, &Value::Array(out))
}

fn run_f2_trades(g: &Golden) -> Result<(), String> {
    let df = plain(r::f2_trades(&frame_in(g, "cont")?))?;
    check(g, &enc(&df)?)
}

fn run_liquid_symbols(g: &Golden) -> Result<(), String> {
    let cont = frame_in(g, "cont")?;
    let top_ns: Vec<i64> = input(g, "top_ns")?;
    let mut out = Map::new();
    for n in top_ns {
        out.insert(n.to_string(), json!(plain(r::liquid_symbols(&cont, n))?));
    }
    check(g, &Value::Object(out))
}

fn option_panel(g: &Golden) -> Result<r::OptionPanel, String> {
    let futures = frame_in(g, "futures")?;
    let cont = plain(r::continuous(&futures))?;
    let symbols: BTreeSet<String> = input(g, "symbols")?;
    plain(r::build_option_panel(
        &frame_in(g, "options")?,
        &futures,
        &cont,
        &symbols,
    ))
}

fn run_build_option_panel(g: &Golden) -> Result<(), String> {
    let panel = option_panel(g)?;
    let mut groups = Map::new();
    let mut sorted: Vec<_> = panel.groups.iter().collect();
    sorted.sort_by(|a, b| a.0.cmp(&b.0));
    for ((sym, e), by_day) in sorted {
        let days: Map<String, Value> = by_day
            .iter()
            .map(|(d, ch)| (d.to_string(), json!(ch.len())))
            .collect();
        groups.insert(format!("{sym}|{e}"), Value::Object(days));
    }
    let mut rv = Map::new();
    for ((sym, d), v) in &panel.rv20 {
        if let (Some(v), Some(d)) = (v, d) {
            rv.insert(
                format!("{}|{d}", sym.clone().unwrap_or_else(|| "None".into())),
                json!(v),
            );
        }
    }
    let probes: Vec<Value> = input(g, "probes")?;
    let mut tdb = Map::new();
    for p in probes {
        let e = p["expiry"].as_str().unwrap();
        let n = p["n"].as_i64().unwrap();
        tdb.insert(
            format!("{e}|{n}"),
            json!(panel.trading_days_before(date(e)?, n)),
        );
    }
    let actual = json!({
        "dates": panel.dates,
        "lots": panel.lots,
        "groups": groups,
        "fgroups": panel.fgroups.len(),
        "rv20": rv,
        "trading_days_before": tdb,
    });
    check(g, &actual)
}

fn run_condor_trades(g: &Golden) -> Result<(), String> {
    let panel = option_panel(g)?;
    let params: CondorParams = input(g, "params")?;
    let run = plain(r::condor_trades(&panel, &params))?;
    let skipped: Vec<Value> = run
        .ca_skipped
        .iter()
        .map(|(sym, e)| json!([sym, e]))
        .collect();
    check(
        g,
        &json!({"trades": enc(&run.trades)?, "ca_skipped": skipped}),
    )
}

fn run_debit_trades(g: &Golden) -> Result<(), String> {
    let panel = option_panel(g)?;
    let runs: Vec<Value> = input(g, "runs")?;
    let mut out = Vec::new();
    for run in runs {
        let structure: DebitStructure =
            serde_json::from_value(run["structure"].clone()).map_err(s)?;
        let df = plain(r::debit_trades(
            &panel,
            run["horizon"].as_i64().unwrap(),
            structure,
        ))?;
        out.push(enc(&df)?);
    }
    check(g, &Value::Array(out))
}

fn run_basis_study(g: &Golden) -> Result<(), String> {
    let cost: f64 = input(g, "cycle_cost")?;
    let actual = match r::basis_study(&frame_in(g, "futures")?, cost) {
        Ok(b) => {
            let q: Map<String, Value> = b
                .quantiles
                .iter()
                .map(|(q, v)| (float_key(*q), json!(v)))
                .collect();
            json!({
                "sessions": b.sessions,
                "quantiles": q,
                "share_net_above_7pct": b.share_net_above_7pct,
                "by_month": enc(&b.by_month)?,
            })
        }
        Err(e) => raises(&e),
    };
    check(g, &actual)
}

#[test]
fn research_matches_every_golden() {
    let counts = [
        (
            "research.constants",
            grade("research.constants", run_research_constants),
            1,
        ),
        (
            "research.continuous",
            grade("research.continuous", run_continuous),
            3,
        ),
        ("research.b76", grade("research.b76", run_b76), 2),
        (
            "research.implied",
            grade("research.implied", run_implied),
            2,
        ),
        (
            "research.leg_costs",
            grade("research.leg_costs", run_leg_costs),
            2,
        ),
        (
            "research.summarise",
            grade("research.summarise", run_summarise),
            5,
        ),
        (
            "research.futures_panel",
            grade("research.futures_panel", run_futures_panel),
            1,
        ),
        (
            "research.liquid_base",
            grade("research.liquid_base", run_liquid_base),
            1,
        ),
        (
            "research.futures_signals",
            grade("research.futures_signals", run_futures_signals),
            1,
        ),
        (
            "research.simulate_futures",
            grade("research.simulate_futures", run_simulate_futures),
            2,
        ),
        (
            "research.f2_trades",
            grade("research.f2_trades", run_f2_trades),
            1,
        ),
        (
            "research.liquid_symbols",
            grade("research.liquid_symbols", run_liquid_symbols),
            1,
        ),
        (
            "research.build_option_panel",
            grade("research.build_option_panel", run_build_option_panel),
            1,
        ),
        (
            "research.condor_trades",
            grade("research.condor_trades", run_condor_trades),
            7,
        ),
        (
            "research.debit_trades",
            grade("research.debit_trades", run_debit_trades),
            1,
        ),
        (
            "research.basis_study",
            grade("research.basis_study", run_basis_study),
            3,
        ),
    ];
    settle(&counts);
}

// =============================================================================================
// directional_retest
// =============================================================================================

fn run_dr_constants(g: &Golden) -> Result<(), String> {
    let lots: Map<String, Value> = dr::FALLBACK_LOT
        .iter()
        .map(|(k, v)| ((*k).to_owned(), json!(v)))
        .collect();
    check(
        g,
        &json!({"INDEX_COSTS": dr::index_costs(), "FALLBACK_LOT": lots, "NOT_TESTED": dr::NOT_TESTED}),
    )
}

fn panel_of(g: &Golden) -> Result<IndexPanel, String> {
    let symbol: String = input(g, "symbol")?;
    plain(index_panel(&frame_in(g, "contracts")?, &symbol))
}

fn kind_value(k: OptionType) -> &'static str {
    match k {
        OptionType::Ce => "CE",
        OptionType::Pe => "PE",
    }
}

fn run_index_panel(g: &Golden) -> Result<(), String> {
    let p = panel_of(g)?;
    let key = |k: &(NaiveDate, NaiveDate, Decimal, OptionType)| {
        format!("{}|{}|{}|{}", k.0, k.1, k.2, kind_value(k.3))
    };
    let mut settle: Vec<_> = p.settle.iter().collect();
    settle.sort_by_key(|(k, _)| key(k));
    let mut traded: Vec<_> = p.traded.iter().collect();
    traded.sort_by_key(|(k, _)| key(k));
    let settle: Vec<Value> = settle
        .iter()
        .map(|(k, v)| json!({"date": k.0, "expiry": k.1, "strike": k.2, "option_type": kind_value(k.3), "settle": v}))
        .collect();
    let traded: Vec<Value> = traded
        .iter()
        .map(|(k, v)| json!({"date": k.0, "expiry": k.1, "strike": k.2, "option_type": kind_value(k.3), "traded": v}))
        .collect();
    let strikes: Map<String, Value> = p
        .strikes_by_expiry
        .iter()
        .map(|((d, e), v)| (format!("{d}|{e}"), json!(v)))
        .collect();
    let actual = json!({
        "symbol": p.symbol,
        "bars": p.bars,
        "lot_by_date": p.lot_by_date,
        "settle": settle,
        "traded": traded,
        "expiries_by_date": p.expiries_by_date,
        "strikes_by_expiry": strikes,
    });
    check(g, &actual)
}

fn dates_in(g: &Golden, key: &str) -> Result<Vec<NaiveDate>, String> {
    let v: Vec<String> = input(g, key)?;
    v.iter().map(|t| date(t)).collect()
}

fn run_monthly_expiries(g: &Golden) -> Result<(), String> {
    check(g, &json!(monthly_expiries(&dates_in(g, "listed")?)))
}

fn run_strike_step(g: &Golden) -> Result<(), String> {
    let strikes: Vec<Decimal> = input(g, "strikes")?;
    let near: Decimal = input(g, "near")?;
    check(g, &json!(plain(strike_step(&strikes, near))?))
}

fn run_sessions_left(g: &Golden) -> Result<(), String> {
    let p = panel_of(g)?;
    let cases: Vec<Value> = input(g, "cases")?;
    let mut out = Map::new();
    for c in cases {
        let e = c["entry"].as_str().unwrap();
        let x = c["expiry"].as_str().unwrap();
        out.insert(
            format!("{e}|{x}"),
            json!(sessions_left(&p, date(e)?, date(x)?)),
        );
    }
    check(g, &Value::Object(out))
}

fn run_dr_choose_expiry(g: &Golden) -> Result<(), String> {
    let p = panel_of(g)?;
    let entry: String = input(g, "entry")?;
    let got = choose_expiry(
        &p,
        &dates_in(g, "listed")?,
        date(&entry)?,
        input(g, "min_sessions")?,
    );
    check(g, &json!(got))
}

fn f3_result_value(res: &dr::F3RetestResult) -> Result<Value, String> {
    let mut v = serde_json::to_value(res.headline()).map_err(s)?;
    v["trades"] = enc(&res.trades)?;
    Ok(v)
}

fn run_run_symbol(g: &Golden) -> Result<(), String> {
    let p = panel_of(g)?;
    let f3 = cfg!(g, "f3", F3Config);
    let res = plain(run_symbol(
        &p,
        &f3,
        opt_date(g, "start")?,
        opt_date(g, "end")?,
    ))?;
    check(g, &f3_result_value(&res)?)
}

fn run_run_f3_retest(g: &Golden) -> Result<(), String> {
    let f3 = cfg!(g, "f3", F3Config);
    let res = plain(run_f3_retest(
        &frame_in(g, "contracts")?,
        &f3,
        opt_date(g, "start")?,
        opt_date(g, "end")?,
    ))?;
    let mut out = Map::new();
    for (k, v) in &res {
        out.insert(k.clone(), f3_result_value(v)?);
    }
    check(g, &Value::Object(out))
}

#[test]
fn directional_retest_matches_every_golden() {
    let counts = [
        (
            "directional_retest.constants",
            grade("directional_retest.constants", run_dr_constants),
            1,
        ),
        (
            "directional_retest.index_panel",
            grade("directional_retest.index_panel", run_index_panel),
            2,
        ),
        (
            "directional_retest.monthly_expiries",
            grade("directional_retest.monthly_expiries", run_monthly_expiries),
            2,
        ),
        (
            "directional_retest.strike_step",
            grade("directional_retest.strike_step", run_strike_step),
            4,
        ),
        (
            "directional_retest.sessions_left",
            grade("directional_retest.sessions_left", run_sessions_left),
            1,
        ),
        (
            "directional_retest.choose_expiry",
            grade("directional_retest.choose_expiry", run_dr_choose_expiry),
            3,
        ),
        (
            "directional_retest.run_symbol",
            grade("directional_retest.run_symbol", run_run_symbol),
            9,
        ),
        (
            "directional_retest.run_f3_retest",
            grade("directional_retest.run_f3_retest", run_run_f3_retest),
            2,
        ),
    ];
    settle(&counts);
}

// =============================================================================================
// retest
// =============================================================================================

fn run_retest_constants(g: &Golden) -> Result<(), String> {
    let mut months: Vec<u32> = rt::RETEST_MONTHS.to_vec();
    // `_out` sorts a set by `json.dumps` of each member.
    months.sort_by_key(ToString::to_string);
    let actual = json!({
        "FAMILIES": families(),
        "FAMILY_KEYS": family_keys(),
        "TIER_2E_CAVEAT": rt::TIER_2E_CAVEAT,
        "TIER_2E_CAVEAT_MEASURED": rt::TIER_2E_CAVEAT_MEASURED,
        "TIER_2E_CAVEAT_F3": rt::tier_2e_caveat_f3(),
        "MIN_MEASURED_SESSIONS": rt::MIN_MEASURED_SESSIONS,
        "MIN_MEASURED_NAMES": rt::MIN_MEASURED_NAMES,
        "RETEST_MONTHS": months,
        "DIRECTIONAL_EXPIRY_DAYS": rt::DIRECTIONAL_EXPIRY_DAYS,
        "DIRECTIONAL_STRIKE_BAND": rt::DIRECTIONAL_STRIKE_BAND,
        "SYMBOL_BATCH": rt::SYMBOL_BATCH,
    });
    check(g, &actual)
}

fn run_empty_options(g: &Golden) -> Result<(), String> {
    check(g, &enc(&plain(rt::empty_options())?)?)
}

fn run_trim(g: &Golden) -> Result<(), String> {
    let symbol: String = input(g, "symbol")?;
    let df = plain(trim_index_contracts(&frame_in(g, "contracts")?, &symbol))?;
    check(g, &enc(&df)?)
}

fn run_panels(g: &Golden) -> Result<(), String> {
    let p = plain(panels_from_contracts(&frame_in(g, "contracts")?))?;
    check(
        g,
        &json!({"futures": enc(&p.futures)?, "options": enc(&p.options)?}),
    )
}

fn family_by_key(key: &str) -> Result<rt::Family, String> {
    families()
        .into_iter()
        .find(|f| f.key == key)
        .ok_or_else(|| format!("no family {key}"))
}

fn measured_of(v: &Value) -> Result<BTreeMap<String, (f64, i64)>, String> {
    let m = v.as_object().ok_or("measured is not a map")?;
    m.iter()
        .map(|(k, x)| {
            Ok((
                k.clone(),
                (
                    x[0].as_f64().ok_or("rate")?,
                    x[1].as_i64().ok_or("sessions")?,
                ),
            ))
        })
        .collect()
}

fn run_family_slippage(g: &Golden) -> Result<(), String> {
    let measured = measured_of(g.input("measured").map_err(s)?)?;
    let keys: Vec<String> = input(g, "families")?;
    let mut out = Map::new();
    for k in keys {
        let f = family_by_key(&k)?;
        out.insert(
            k,
            serde_json::to_value(family_slippage(&f, &measured)).map_err(s)?,
        );
    }
    check(g, &Value::Object(out))
}

fn run_retest_due(g: &Golden) -> Result<(), String> {
    let cases: Vec<Value> = input(g, "cases")?;
    let mut out = Vec::new();
    for c in cases {
        let today = date(c["today"].as_str().unwrap())?;
        let last = c["last_run"].as_str().map(date).transpose()?;
        out.push(retest_due(today, last));
    }
    check(g, &json!(out))
}

fn run_run_family(g: &Golden) -> Result<(), String> {
    let futures = frame_in(g, "futures")?;
    let options = frame_in(g, "options")?;
    let index = match g.input("index_contracts").map_err(s)? {
        Value::Null => None,
        v => Some(frame_of(v)?),
    };
    let loader = slice_loader(&options);
    let load_index = |_: &str| -> FnoResult<DataFrame> {
        index
            .clone()
            .ok_or_else(|| FnoError::Value("no index rows".to_owned()))
    };
    let runs: Vec<Value> = input(g, "runs")?;
    let mut out = Vec::new();
    for run in runs {
        let f = family_by_key(run["family"].as_str().unwrap())?;
        let measured = measured_of(&run["measured"])?;
        let knobs = RunKnobs {
            cont: None,
            batch: run["batch"].as_i64().unwrap(),
            load_index: if index.is_some() {
                Some(&load_index)
            } else {
                None
            },
        };
        out.push(match run_family(&f, &futures, &loader, &measured, knobs) {
            Ok(res) => serde_json::to_value(res).map_err(s)?,
            Err(e) => raises(&e),
        });
    }
    check(g, &Value::Array(out))
}

#[test]
fn retest_matches_every_golden() {
    let counts = [
        (
            "retest.constants",
            grade("retest.constants", run_retest_constants),
            1,
        ),
        (
            "retest.empty_options",
            grade("retest.empty_options", run_empty_options),
            1,
        ),
        (
            "retest.trim_index_contracts",
            grade("retest.trim_index_contracts", run_trim),
            2,
        ),
        (
            "retest.panels_from_contracts",
            grade("retest.panels_from_contracts", run_panels),
            2,
        ),
        (
            "retest.family_slippage",
            grade("retest.family_slippage", run_family_slippage),
            6,
        ),
        (
            "retest.retest_due",
            grade("retest.retest_due", run_retest_due),
            1,
        ),
        (
            "retest.run_family",
            grade("retest.run_family", run_run_family),
            6,
        ),
    ];
    settle(&counts);
}

// =============================================================================================
// Differential fuzz (not a golden): cases written in golden format by a script that calls the
// live Python through `golden_fno.py`'s own `CALLS`, graded by the runners above.
// =============================================================================================

type Runner = fn(&Golden) -> Result<(), String>;

fn runner_for(fn_name: &str) -> Option<Runner> {
    let short = fn_name.strip_prefix(P)?;
    Some(match short {
        "research.constants" => run_research_constants,
        "research.continuous" => run_continuous,
        "research.b76" => run_b76,
        "research.implied" => run_implied,
        "research.leg_costs" => run_leg_costs,
        "research.summarise" => run_summarise,
        "research.futures_panel" => run_futures_panel,
        "research.liquid_base" => run_liquid_base,
        "research.futures_signals" => run_futures_signals,
        "research.simulate_futures" => run_simulate_futures,
        "research.f2_trades" => run_f2_trades,
        "research.liquid_symbols" => run_liquid_symbols,
        "research.build_option_panel" => run_build_option_panel,
        "research.condor_trades" => run_condor_trades,
        "research.debit_trades" => run_debit_trades,
        "research.basis_study" => run_basis_study,
        "directional_retest.constants" => run_dr_constants,
        "directional_retest.index_panel" => run_index_panel,
        "directional_retest.monthly_expiries" => run_monthly_expiries,
        "directional_retest.strike_step" => run_strike_step,
        "directional_retest.sessions_left" => run_sessions_left,
        "directional_retest.choose_expiry" => run_dr_choose_expiry,
        "directional_retest.run_symbol" => run_run_symbol,
        "directional_retest.run_f3_retest" => run_run_f3_retest,
        "retest.constants" => run_retest_constants,
        "retest.empty_options" => run_empty_options,
        "retest.trim_index_contracts" => run_trim,
        "retest.panels_from_contracts" => run_panels,
        "retest.family_slippage" => run_family_slippage,
        "retest.retest_due" => run_retest_due,
        "retest.run_family" => run_run_family,
        _ => return None,
    })
}

/// `BASKFY_FNO_C_FUZZ=<dir> cargo test --test golden_fno_c -- --ignored`: grades every
/// `*.json` in `<dir>` (written by the lane's fuzz script against the live Python).
#[test]
#[ignore = "differential fuzz: needs BASKFY_FNO_C_FUZZ pointing at freshly dumped cases"]
fn differential_fuzz() {
    let Ok(dir) = std::env::var("BASKFY_FNO_C_FUZZ") else {
        return;
    };
    let mut paths: Vec<std::path::PathBuf> = std::fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|e| e == "json"))
        .collect();
    paths.sort();
    let mut counts: BTreeMap<String, (usize, usize)> = BTreeMap::new();
    let mut failures = Vec::new();
    for p in &paths {
        let g = baskfy_testkit::load(p.to_str().unwrap()).unwrap();
        let run = runner_for(&g.fn_name).unwrap_or_else(|| panic!("no runner for {}", g.fn_name));
        let slot = counts.entry(g.fn_name.clone()).or_default();
        slot.0 += 1;
        if let Err(e) = run(&g) {
            slot.1 += 1;
            let mut e = e;
            e.truncate(3000);
            failures.push(format!("{}: {e}", p.display()));
        }
    }
    let summary: Vec<String> = counts
        .iter()
        .map(|(k, (n, f))| format!("{k}: {} of {n} pass", n - f))
        .collect();
    assert!(
        failures.is_empty(),
        "{}\n\n{}",
        summary.join("\n"),
        failures.join("\n\n")
    );
    // Passing: the per-function counts are in `summary` (printed only on failure).
    drop(summary);
}
