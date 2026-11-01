#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
//! The backtest helpers against the goldens (`go/testdata/golden/L1/backtest`): `month_key`,
//! `month_end`, `year_fraction`, `rebalance_dates`, `shift_schedule`, `raw_weights`,
//! `apply_position_limits`, `CostSpec.charge`, `PricePanel`, `PointInTimeReader` and
//! `AdjustmentCoverage.caveats` — 71 cases.
//!
//! Each case is decoded exactly as `tools/parity/golden_backtest.py`'s `call_*` function
//! decodes it, the Rust port is called, and the result is encoded the way the dumper encodes
//! the Python result (a raised `BacktestError` is `{"error": {"type", "message"}}`, a
//! `LookAheadError` adds `what`, `offending`, `as_of`). Every case is graded strictly by the
//! testkit and the per-function case count is asserted, so a dropped case fails.

use std::collections::{BTreeMap, HashMap};

use baskfy_core::backtest::helpers::{
    AdjustmentCoverage, BacktestData, BacktestError, CostSpec, PointInTimeReader, PositionLimits,
    PricePanel, RebalanceSpec, Selected, Weighting, WideDecimal, apply_position_limits, month_end,
    month_key, raw_weights, rebalance_dates, shift_schedule, year_fraction,
};
use baskfy_testkit::{Golden, decode_frame, encode_frame};
use chrono::NaiveDate;
use polars::prelude::DataFrame;
use serde_json::{Map, Value, json};

type Run = fn(&Golden) -> Result<Value, String>;

fn s<E: ToString>(e: E) -> String {
    e.to_string()
}

fn error_value(e: &BacktestError) -> Value {
    let mut body = Map::new();
    body.insert("type".into(), json!(e.type_name()));
    body.insert("message".into(), json!(e.to_string()));
    if let BacktestError::LookAhead {
        what,
        offending,
        as_of,
    } = e
    {
        body.insert("what".into(), json!(what));
        body.insert("offending".into(), json!(offending.to_string()));
        body.insert("as_of".into(), json!(as_of.to_string()));
    }
    json!({ "error": Value::Object(body) })
}

fn date(v: &Value) -> Result<NaiveDate, String> {
    baskfy_testkit::date(v).map_err(s)
}

fn opt_date(v: &Value) -> Result<Option<NaiveDate>, String> {
    if v.is_null() {
        Ok(None)
    } else {
        date(v).map(Some)
    }
}

fn dates(v: &Value) -> Result<Vec<NaiveDate>, String> {
    v.as_array()
        .ok_or("expected a list of dates")?
        .iter()
        .map(date)
        .collect()
}

fn opt_frame(v: &Value) -> Result<Option<DataFrame>, String> {
    if v.is_null() {
        Ok(None)
    } else {
        decode_frame(v).map(Some).map_err(s)
    }
}

fn frame_value(df: &DataFrame) -> Result<Value, String> {
    encode_frame(df).map_err(s)
}

fn dec_value(d: Option<rust_decimal::Decimal>) -> Value {
    d.map_or(Value::Null, |d| json!(d.to_string()))
}

fn int_key_map<T>(
    v: &Value,
    f: impl Fn(&Value) -> Result<T, String>,
) -> Result<HashMap<i64, T>, String> {
    v.as_object()
        .ok_or("expected an object")?
        .iter()
        .map(|(k, x)| Ok((k.parse::<i64>().map_err(s)?, f(x)?)))
        .collect()
}

fn decode_data(doc: &Value) -> Result<BacktestData, String> {
    let calendar = dates(&doc["calendar"])?;
    let prices = decode_frame(&doc["prices"]).map_err(s)?;
    let price_only = doc["price_only"].as_bool().ok_or("price_only")?;
    let panel = PricePanel::new(&prices, &calendar, price_only).map_err(s)?;
    let mut screens = BTreeMap::new();
    for (k, v) in doc["screens"].as_object().ok_or("screens")? {
        screens.insert(date(&json!(k))?, decode_frame(v).map_err(s)?);
    }
    let coverage: Option<AdjustmentCoverage> =
        serde_json::from_value(doc["adjustment_coverage"].clone()).map_err(s)?;
    let string = |x: &Value| {
        x.as_str()
            .map(str::to_owned)
            .ok_or_else(|| "not a string".to_owned())
    };
    Ok(BacktestData {
        calendar,
        prices: panel,
        screens,
        benchmark: opt_frame(&doc["benchmark"])?,
        delistings: int_key_map(&doc["delistings"], date)?,
        symbols: int_key_map(&doc["symbols"], string)?,
        names: int_key_map(&doc["names"], string)?,
        dividends: opt_frame(&doc["dividends"])?,
        data_version: doc["data_version"].as_i64(),
        adjustment_coverage: coverage,
    })
}

// ---- one runner per function --------------------------------------------------------------

fn run_month_key(g: &Golden) -> Result<Value, String> {
    Ok(json!(month_key(g.input_date("day").map_err(s)?)))
}

fn run_month_end(g: &Golden) -> Result<Value, String> {
    Ok(json!(
        month_end(g.input_date("day").map_err(s)?).to_string()
    ))
}

fn run_year_fraction(g: &Golden) -> Result<Value, String> {
    let start = g.input_date("start").map_err(s)?;
    let end = g.input_date("end").map_err(s)?;
    Ok(json!(year_fraction(start, end).to_string()))
}

fn run_rebalance_dates(g: &Golden) -> Result<Value, String> {
    let calendar = dates(g.input("calendar").map_err(s)?)?;
    let spec: RebalanceSpec = g.input_as("spec").map_err(s)?;
    let out = rebalance_dates(
        &calendar,
        g.input_date("start").map_err(s)?,
        g.input_date("end").map_err(s)?,
        &spec,
    );
    Ok(json!(
        out.iter().map(ToString::to_string).collect::<Vec<_>>()
    ))
}

fn run_shift_schedule(g: &Golden) -> Result<Value, String> {
    let calendar = dates(g.input("calendar").map_err(s)?)?;
    let schedule = dates(g.input("dates").map_err(s)?)?;
    let offset: i64 = g.input_as("offset").map_err(s)?;
    let out = shift_schedule(&calendar, &schedule, offset);
    Ok(json!(
        out.iter().map(ToString::to_string).collect::<Vec<_>>()
    ))
}

fn run_apply_position_limits(g: &Golden) -> Result<Value, String> {
    let raw = g
        .input("weights")
        .map_err(s)?
        .as_object()
        .ok_or("weights")?;
    let weights: Vec<(i64, WideDecimal)> = raw
        .iter()
        .map(|(k, v)| {
            let w: WideDecimal = serde_json::from_value(v.clone()).map_err(s)?;
            Ok((k.parse::<i64>().map_err(s)?, w))
        })
        .collect::<Result<_, String>>()?;
    let limits: PositionLimits = g.input_as("limits").map_err(s)?;
    let out = apply_position_limits(&weights, &limits).map_err(s)?;
    // The dumper's `sum(out.values(), D(0))`, in Python's context.
    let total = out.values().fold(WideDecimal::zero(), |acc, v| {
        acc.add(&WideDecimal::from(*v))
    });
    let mut map = Map::new();
    for (k, v) in &out {
        map.insert(k.to_string(), json!(v.to_string()));
    }
    Ok(json!({ "weights": Value::Object(map), "sum": total.to_string() }))
}

fn run_raw_weights(g: &Golden) -> Result<Value, String> {
    let rows: Vec<Selected> = g.input_as("rows").map_err(s)?;
    let scheme: Weighting = g.input_as("scheme").map_err(s)?;
    Ok(match raw_weights(scheme, &rows) {
        Ok(out) => {
            let mut map = Map::new();
            for (k, v) in out {
                map.insert(k.to_string(), json!(v.to_string()));
            }
            Value::Object(map)
        }
        Err(e) => error_value(&e),
    })
}

fn run_cost_charge(g: &Golden) -> Result<Value, String> {
    let mut costs: CostSpec = g.input_as("costs").map_err(s)?;
    costs.validate().map_err(s)?;
    if let Some(scale) = g.input_opt_decimal("scale").map_err(s)? {
        costs = costs.scaled(scale).map_err(s)?;
    }
    let notionals: Vec<rust_decimal::Decimal> = g
        .input("notionals")
        .map_err(s)?
        .as_array()
        .ok_or("notionals")?
        .iter()
        .map(|v| baskfy_testkit::decimal(v).map_err(s))
        .collect::<Result<_, _>>()?;
    let charges = notionals
        .iter()
        .map(|n| costs.charge(*n).map(|c| json!(c.to_string())).map_err(s))
        .collect::<Result<Vec<_>, _>>()?;
    Ok(json!({ "total_bps": costs.total_bps().to_string(), "charges": charges }))
}

fn run_caveats(g: &Golden) -> Result<Value, String> {
    let coverage: AdjustmentCoverage = g.input_as("coverage").map_err(s)?;
    let out = coverage.caveats(
        g.input_date("start").map_err(s)?,
        g.input_date("end").map_err(s)?,
    );
    Ok(json!(out))
}

fn run_price_panel(g: &Golden) -> Result<Value, String> {
    let calendar = dates(g.input("calendar").map_err(s)?)?;
    let frame = g.input_frame("prices").map_err(s)?;
    let price_only: bool = g.input_as("price_only").map_err(s)?;
    let panel = match PricePanel::new(&frame, &calendar, price_only) {
        Ok(p) => p,
        Err(e) => return Ok(error_value(&e)),
    };
    let mut results = Vec::new();
    for op in g.input("ops").map_err(s)?.as_array().ok_or("ops")? {
        let id = || op["instrument_id"].as_i64().ok_or("instrument_id");
        let position = || {
            op["position"]
                .as_u64()
                .and_then(|p| usize::try_from(p).ok())
                .ok_or("position")
        };
        let result = match op["op"].as_str().ok_or("op")? {
            "index_of" => match panel.index_of(date(&op["day"])?) {
                Ok(i) => json!(i),
                Err(e) => error_value(&e),
            },
            "close_on" => dec_value(panel.close_on(id()?, position()?).map_err(s)?),
            "open_on" => dec_value(panel.open_on(id()?, position()?).map_err(s)?),
            "last_close_at_or_before" => {
                match panel
                    .last_close_at_or_before(id()?, position()?)
                    .map_err(s)?
                {
                    None => Value::Null,
                    Some((price, at)) => json!({ "price": price.to_string(), "position": at }),
                }
            }
            "liquidation_day" => {
                json!(panel.liquidation_day(id()?, opt_date(&op["delisted_on"])?))
            }
            other => return Err(format!("unknown op {other}")),
        };
        results.push(result);
    }
    Ok(json!({ "instrument_ids": panel.instrument_ids(), "results": results }))
}

fn run_point_in_time_reader(g: &Golden) -> Result<Value, String> {
    let data = decode_data(g.input("data").map_err(s)?)?;
    let mut reader = PointInTimeReader::new(&data);
    let mut results = Vec::new();
    for op in g.input("ops").map_err(s)?.as_array().ok_or("ops")? {
        let day = || date(&op["day"]);
        let id = || op["instrument_id"].as_i64().ok_or("instrument_id");
        let outcome: Result<Value, BacktestError> = match op["op"].as_str().ok_or("op")? {
            "as_of" => reader.as_of().map(|d| json!(d.to_string())),
            "advance_to" => reader.advance_to(day()?).map(|()| Value::Null),
            "screen_on" => match reader.screen_on(day()?) {
                Ok(f) => Ok(frame_value(&f)?),
                Err(e) => Err(e),
            },
            "benchmark_to" => match reader.benchmark_to(day()?) {
                Ok(f) => Ok(frame_value(&f)?),
                Err(e) => Err(e),
            },
            "dividends_on" => match reader.dividends_on(day()?) {
                Ok(f) => Ok(frame_value(&f)?),
                Err(e) => Err(e),
            },
            "price_close" => reader.price_close(id()?, day()?).map(dec_value),
            "price_open" => reader.price_open(id()?, day()?).map(dec_value),
            "last_close" => reader.last_close(id()?, day()?).map(|found| {
                found.map_or(
                    Value::Null,
                    |(price, d)| json!({ "price": price.to_string(), "date": d.to_string() }),
                )
            }),
            other => return Err(format!("unknown op {other}")),
        };
        results.push(outcome.unwrap_or_else(|e| error_value(&e)));
    }
    Ok(Value::Array(results))
}

// ---- grading --------------------------------------------------------------------------------

fn grade(fn_name: &str, run: Run) -> usize {
    let cases: Vec<Golden> = baskfy_testkit::cases_for(fn_name).unwrap();
    let mut failures = Vec::new();
    for g in &cases {
        let outcome = run(g).and_then(|actual| {
            g.check_value(&actual)
                .map_err(|m| format!("{} mismatch(es)\n{m}", m.0.len()))
        });
        if let Err(why) = outcome {
            failures.push(format!(
                "{} (known_bug: {:?}): {why}",
                g.case, g.meta.known_bug
            ));
        }
    }
    assert!(
        failures.is_empty(),
        "{} of {} {fn_name} cases failed:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n\n")
    );
    cases.len()
}

macro_rules! golden_test {
    ($name:ident, $fn_name:literal, $run:ident, $count:literal) => {
        #[test]
        fn $name() {
            let n = grade(concat!("baskfy_core.backtest.", $fn_name), $run);
            assert_eq!(n, $count, concat!($fn_name, " case count changed"));
        }
    };
}

golden_test!(
    month_key_matches_every_golden,
    "month_key",
    run_month_key,
    6
);
golden_test!(
    month_end_matches_every_golden,
    "month_end",
    run_month_end,
    6
);
golden_test!(
    year_fraction_matches_every_golden,
    "year_fraction",
    run_year_fraction,
    5
);
golden_test!(
    rebalance_dates_matches_every_golden,
    "rebalance_dates",
    run_rebalance_dates,
    10
);
golden_test!(
    shift_schedule_matches_every_golden,
    "shift_schedule",
    run_shift_schedule,
    7
);
golden_test!(
    raw_weights_matches_every_golden,
    "raw_weights",
    run_raw_weights,
    8
);
golden_test!(
    apply_position_limits_matches_every_golden,
    "apply_position_limits",
    run_apply_position_limits,
    10
);
golden_test!(
    cost_charge_matches_every_golden,
    "CostSpec.charge",
    run_cost_charge,
    5
);
golden_test!(
    caveats_match_every_golden,
    "AdjustmentCoverage.caveats",
    run_caveats,
    7
);
golden_test!(
    price_panel_matches_every_golden,
    "PricePanel",
    run_price_panel,
    5
);
golden_test!(
    point_in_time_reader_matches_every_golden,
    "PointInTimeReader",
    run_point_in_time_reader,
    2
);

/// The 71 cases this lane owns, counted once more in total.
#[test]
fn helpers_lane_owns_seventy_one_cases() {
    let names = [
        "month_key",
        "month_end",
        "year_fraction",
        "rebalance_dates",
        "shift_schedule",
        "raw_weights",
        "apply_position_limits",
        "CostSpec.charge",
        "AdjustmentCoverage.caveats",
        "PricePanel",
        "PointInTimeReader",
    ];
    let total: usize = names
        .iter()
        .map(|n| {
            baskfy_testkit::cases_for(&format!("baskfy_core.backtest.{n}"))
                .unwrap()
                .len()
        })
        .sum();
    assert_eq!(total, 71);
}

// ---- WideDecimal against Python's decimal, beyond the goldens --------------------------------

fn w(text: &str) -> WideDecimal {
    text.parse().unwrap()
}

#[test]
fn wide_decimal_reproduces_python_context_arithmetic() {
    // Values printed by CPython 3 (`decimal`, default context).
    assert_eq!(w("0").div(&w("365.25")).unwrap().to_sci_string(), "0E+2");
    assert_eq!(w("0").div(&w("365.25")).unwrap().to_string(), "0");
    assert_eq!(
        w("1").div(&w("3")).unwrap().to_string(),
        "0.3333333333333333333333333333"
    );
    assert_eq!(w("10").div(&w("4")).unwrap().to_string(), "2.5");
    assert_eq!(w("1.00").div(&w("4")).unwrap().to_string(), "0.25");
    assert_eq!(
        w("1").div(&w("0.18")).unwrap().to_string(),
        "5.555555555555555555555555556"
    );
    assert_eq!(
        w("-29").div(&w("365.25")).unwrap().to_string(),
        "-0.07939767282683093771389459274"
    );
    assert_eq!(w("0.0000001").to_sci_string(), "1E-7");
    assert_eq!(
        w("-0.001")
            .quantize(-2, baskfy_core::backtest::helpers::Rounding::HalfUp)
            .unwrap()
            .to_string(),
        "-0.00"
    );
    assert_eq!(w("0.10").add(&w("0.2")).to_string(), "0.30");
    assert_eq!(w("1").sub(&w("1.000000")).to_string(), "0.000000");
    assert_eq!(w("2.5").mul(&w("0.10")).to_string(), "0.250");
    assert!(w("0.5") == w("0.50"));
    assert!(w("-0") == w("0"));
    assert!(w("1").div(&w("0")).is_err());
}
