#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
//! TWT lane A against the goldens (`go/testdata/golden/L1/twt`): `config` (through every
//! case's config input), `indicators`, `calendar`, `breadth`, `signals`, `sizing`, `sleeve`,
//! `drift` and `published`.
//!
//! Each case is decoded exactly as `tools/parity/golden_twt.py`'s `call_*` decodes it, the Rust
//! port is called, and the result is encoded the way the dumper encodes the Python result (a
//! raise as `{"raises": {"type", "message"}}`). Every case of every function is graded strictly
//! and the per-function case count is asserted, so a dropped case fails.

use baskfy_core::CoreError;
use baskfy_core::twt::breadth::{
    as_shared_config as breadth_shared, breadth_above_dma, breadth_series, gate_for,
};
use baskfy_core::twt::calendar::{
    SessionCalendar, as_shared_config as calendar_shared, build_calendar, drop_thin_sessions,
    session_counts, thin_sessions,
};
use baskfy_core::twt::config::{BreadthConfig, DataConfig, SizingConfig, TwtConfig};
use baskfy_core::twt::drift::{Points, compare};
use baskfy_core::twt::indicators::{bucket_keys, require_columns, with_twt_indicators};
use baskfy_core::twt::published::{PublishedStudy, published};
use baskfy_core::twt::signals::{
    detect_signals, entry_events, liquidity_floor, month_low_back, signal_mask, tight_state,
    weekly_closes, weekly_column, with_twt_columns,
};
use baskfy_core::twt::sizing::{first_live_multiplier, size_entry};
use baskfy_core::twt::sleeve::{OpenPositionValue, sleeve_value};
use baskfy_testkit::{Golden, cases_for, encode_frame, encode_series, to_golden_value};
use chrono::NaiveDate;
use polars::prelude::{DataFrame, DataType, IdxCa, IdxSize, IntoColumn};
use serde_json::{Value, json};

const MODULE: &str = "baskfy_core.twt.";

fn raises(e: &CoreError) -> Value {
    let (kind, message) = match e {
        CoreError::InvalidInput(m) => ("ValueError", m.clone()),
        other => ("CoreError", other.to_string()),
    };
    json!({"raises": {"type": kind, "message": message}})
}

fn frame_value(df: &DataFrame) -> Value {
    encode_frame(df).unwrap()
}

fn value<T: serde::Serialize>(v: &T) -> Value {
    to_golden_value(v).unwrap()
}

fn twt_config(g: &Golden) -> TwtConfig {
    match g.input("config").unwrap() {
        Value::Null => TwtConfig::default(),
        _ => g.input_as("config").unwrap(),
    }
}

fn opt_date(g: &Golden, key: &str) -> Option<NaiveDate> {
    match g.input(key).unwrap() {
        Value::Null => None,
        _ => Some(g.input_date(key).unwrap()),
    }
}

/// `frame.sort(["instrument_id", "date"])` — the dumper's `_sorted`.
fn sorted(df: &DataFrame) -> DataFrame {
    let ids: Vec<Option<i64>> = df
        .column("instrument_id")
        .unwrap()
        .cast(&DataType::Int64)
        .unwrap()
        .i64()
        .unwrap()
        .iter()
        .collect();
    let days: Vec<Option<i32>> = df
        .column("date")
        .unwrap()
        .date()
        .unwrap()
        .physical()
        .iter()
        .collect();
    let mut order: Vec<usize> = (0..df.height()).collect();
    order.sort_by(|&a, &b| ids[a].cmp(&ids[b]).then(days[a].cmp(&days[b])));
    let idx: Vec<IdxSize> = order
        .iter()
        .map(|&i| IdxSize::try_from(i).unwrap())
        .collect();
    df.take(&IdxCa::from_vec("".into(), idx)).unwrap()
}

fn grader(fn_name: &str) -> (usize, Grader) {
    TABLE
        .iter()
        .find(|(name, _, _)| *name == fn_name)
        .map(|(_, n, f)| (*n, *f))
        .unwrap_or_else(|| panic!("no grader for {fn_name}"))
}

fn grade_all(cases: &[Golden], grade: Grader) -> Vec<String> {
    let mut failures = Vec::new();
    for g in cases {
        let actual = grade(g);
        if let Err(m) = g.check_value(&actual) {
            failures.push(format!(
                "{} ({}): {} mismatch(es)\n{m}",
                g.case,
                g.fn_name,
                m.0.len()
            ));
        }
    }
    failures
}

/// Grades every golden case of `fn_name`, asserts the count, and fails with every mismatch at
/// once.
fn run(fn_name: &str) {
    let full = format!("{MODULE}{fn_name}");
    let (expected, grade) = grader(fn_name);
    let cases = cases_for(&full).unwrap();
    assert_eq!(cases.len(), expected, "{full}: case count");
    let failures = grade_all(&cases, grade);
    assert!(failures.is_empty(), "{}", failures.join("\n\n"));
}

/// Differential fuzz against the live Python: `TWT_FUZZ_DIR` holds golden-shaped cases written
/// by a generator that calls the dumper's own `CALLS` (never into `go/testdata`). Ignored by
/// default; run with `TWT_FUZZ_DIR=... cargo test --test golden_twt_a -- --ignored`.
#[test]
#[ignore = "needs TWT_FUZZ_DIR from the differential generator"]
fn differential_fuzz() {
    let dir = std::env::var("TWT_FUZZ_DIR").expect("TWT_FUZZ_DIR");
    let mut paths: Vec<_> = std::fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|x| x == "json"))
        .collect();
    paths.sort();
    let mut failures = Vec::new();
    for p in &paths {
        let g = baskfy_testkit::load(p.to_str().unwrap()).unwrap();
        let name = g.fn_name.strip_prefix(MODULE).unwrap().to_owned();
        let (_, grade) = grader(&name);
        failures.extend(grade_all(std::slice::from_ref(&g), grade));
    }
    assert!(!paths.is_empty(), "no fuzz cases in {dir}");
    assert!(
        failures.is_empty(),
        "{} of {} fuzz cases differ:\n{}",
        failures.len(),
        paths.len(),
        failures.join("\n\n")
    );
}

/// Every graded function: (name under `baskfy_core.twt.`, golden case count, grader).
type Grader = fn(&Golden) -> Value;

const TABLE: &[(&str, usize, Grader)] = &[
    ("calendar.session_counts", 5, |g| {
        match session_counts(&g.input_frame("bars").unwrap()) {
            Ok(df) => frame_value(&df),
            Err(e) => raises(&e),
        }
    }),
    ("calendar.thin_sessions", 10, |g| {
        let cfg: Option<DataConfig> = match g.input("config").unwrap() {
            Value::Null => None,
            _ => Some(g.input_as("config").unwrap()),
        };
        match thin_sessions(&g.input_frame("bars").unwrap(), cfg.as_ref()) {
            Ok(days) => value(&days),
            Err(e) => raises(&e),
        }
    }),
    ("calendar.build_calendar", 6, |g| {
        match build_calendar(&g.input_frame("bars").unwrap(), &twt_config(g)) {
            Ok(c) => value(&c),
            Err(e) => raises(&e),
        }
    }),
    (
        "calendar.drop_thin_sessions",
        5,
        |g| match drop_thin_sessions(&g.input_frame("bars").unwrap(), &twt_config(g)) {
            Ok((bars, calendar)) => {
                json!({"bars": frame_value(&bars), "calendar": value(&calendar)})
            }
            Err(e) => raises(&e),
        },
    ),
    ("calendar.as_shared_config", 2, |g| {
        let cfg: DataConfig = g.input_as("config").unwrap();
        value(&calendar_shared(&cfg))
    }),
    ("indicators.bucket_keys", 4, |g| {
        let frame = g.input_frame("frame").unwrap();
        let (week, month) = bucket_keys(&frame).unwrap();
        let date = frame.column("date").unwrap().clone();
        let out = DataFrame::new(frame.height(), vec![date, week, month]).unwrap();
        frame_value(&out)
    }),
    ("indicators.require_columns", 5, |g| {
        match require_columns(&g.input_frame("bars").unwrap()) {
            Ok(()) => Value::Null,
            Err(e) => raises(&e),
        }
    }),
    (
        "indicators.with_twt_indicators",
        9,
        |g| match with_twt_indicators(
            &g.input_frame("bars").unwrap(),
            &calendar_input(g),
            &twt_config(g),
        ) {
            Ok(df) => frame_value(&df),
            Err(e) => raises(&e),
        },
    ),
    ("signals.weekly_column", 4, |g| {
        let back: i64 = g.input_as("back").unwrap();
        json!(weekly_column(back))
    }),
    ("signals.weekly_closes", 8, |g| {
        let df = weekly_closes(&g.input_frame("indicated").unwrap(), &twt_config(g)).unwrap();
        frame_value(&sorted(&df))
    }),
    ("signals.month_low_back", 5, |g| {
        let df = month_low_back(&g.input_frame("indicated").unwrap(), &twt_config(g)).unwrap();
        frame_value(&sorted(&df))
    }),
    ("signals.tight_state", 5, |g| {
        frame_value(&tight_state(&g.input_frame("framed").unwrap(), &twt_config(g)).unwrap())
    }),
    ("signals.entry_events", 7, |g| {
        frame_value(&entry_events(&g.input_frame("stated").unwrap(), &twt_config(g)).unwrap())
    }),
    ("signals.liquidity_floor", 3, |g| {
        let frame = g.input_frame("frame").unwrap();
        let passes = liquidity_floor(
            &frame,
            &twt_config(g),
            g.input_opt_decimal("floor_inr").unwrap(),
        )
        .unwrap()
        .with_name("passes".into());
        let out = DataFrame::new(
            frame.height(),
            vec![
                frame.column("turnover_avg_20").unwrap().clone(),
                passes.into_column(),
            ],
        )
        .unwrap();
        frame_value(&out)
    }),
    ("signals.signal_mask", 3, |g| {
        let mask = signal_mask(
            &g.input_frame("detected").unwrap(),
            &twt_config(g),
            g.input_opt_decimal("floor_inr").unwrap(),
        )
        .unwrap();
        encode_series(&mask).unwrap()
    }),
    ("signals.detect_signals", 5, |g| {
        let df = detect_signals(
            &g.input_frame("detected").unwrap(),
            opt_date(g, "as_of"),
            &twt_config(g),
            g.input_opt_decimal("floor_inr").unwrap(),
        )
        .unwrap();
        frame_value(&df)
    }),
    ("signals.with_twt_columns", 8, |g| {
        frame_value(
            &with_twt_columns(
                &g.input_frame("bars").unwrap(),
                &calendar_input(g),
                &twt_config(g),
            )
            .unwrap(),
        )
    }),
    ("breadth.gate_for", 7, |g| {
        let cfg: Option<BreadthConfig> = match g.input("config").unwrap() {
            Value::Null => None,
            _ => Some(g.input_as("config").unwrap()),
        };
        value(&gate_for(g.input_decimal("pct_above_dma").unwrap(), cfg.as_ref()).unwrap())
    }),
    ("breadth.breadth_above_dma", 7, |g| {
        let reading = breadth_above_dma(
            &g.input_frame("indicated").unwrap(),
            opt_date(g, "as_of"),
            &twt_config(g),
        )
        .unwrap();
        json!({"reading": value(&reading), "derived": {"is_open": reading.is_open()}})
    }),
    ("breadth.breadth_series", 5, |g| {
        frame_value(&breadth_series(&g.input_frame("indicated").unwrap(), &twt_config(g)).unwrap())
    }),
    ("breadth.as_shared_config", 2, |g| {
        let cfg: BreadthConfig = g.input_as("config").unwrap();
        value(&breadth_shared(&cfg))
    }),
    ("sizing.size_entry", 15, |g| {
        let cfg: SizingConfig = g.input_as("config").unwrap();
        let sized = size_entry(
            g.input_decimal("equity").unwrap(),
            g.input_decimal("cash_available").unwrap(),
            g.input_decimal("entry_price").unwrap(),
            g.input_decimal("stop_price").unwrap(),
            g.input_opt_decimal("turnover_avg_inr").unwrap(),
            &cfg,
            g.input_decimal("slot_multiplier").unwrap(),
        )
        .unwrap();
        json!({
            "sized": value(&sized),
            "derived": {"placed": sized.placed(), "turnover_capped": sized.turnover_capped()},
        })
    }),
    ("sizing.first_live_multiplier", 6, |g| {
        let cfg: SizingConfig = g.input_as("config").unwrap();
        let entries: i64 = g.input_as("entries_left").unwrap();
        let enabled: bool = g.input_as("execution_enabled").unwrap();
        value(&first_live_multiplier(entries, enabled, &cfg))
    }),
    ("sleeve.sleeve_value", 7, |g| {
        let positions: Vec<OpenPositionValue> = g.input_as("open_positions").unwrap();
        let v = sleeve_value(
            g.input_decimal("capital_inr").unwrap(),
            g.input_decimal("realised_inr").unwrap(),
            &positions,
        )
        .unwrap();
        json!({
            "value": value(&v),
            "derived": {
                "cash_inr": value(&v.cash_inr().unwrap()),
                "equity_inr": value(&v.equity_inr().unwrap()),
                "cash_available_inr": value(&v.cash_available_inr().unwrap()),
                "open_exposure_inr": value(&v.open_exposure_inr()),
                "unrealised_inr": value(&v.unrealised_inr().unwrap()),
                "has_capital": v.has_capital(),
                "quantize": value(&v.quantize().unwrap()),
            },
            "positions": positions.iter().map(|p| json!({
                "cost": value(&p.cost().unwrap()),
                "value": value(&p.value().unwrap()),
                "is_stale_mark": p.is_stale_mark(),
            })).collect::<Vec<_>>(),
        })
    }),
    ("drift.compare", 8, |g| {
        let cagr: Points = g.input_as("cagr_pct").unwrap();
        let dd: Points = g.input_as("max_drawdown_pct").unwrap();
        let trades: i64 = g.input_as("trades").unwrap();
        let study: PublishedStudy = g.input_as("study").unwrap();
        let drift = compare(
            cagr,
            dd,
            trades,
            &study,
            g.input_decimal("threshold").unwrap(),
        )
        .unwrap();
        json!({"drift": value(&drift), "to_json": value(&drift.to_json())})
    }),
    ("published.PUBLISHED", 1, |_| {
        let study = published();
        json!({"study": value(&study), "cagr": value(&study.cagr().unwrap())})
    }),
];

// ---------------------------------------------------------------------------------------------
// calendar
// ---------------------------------------------------------------------------------------------

#[test]
fn calendar_session_counts() {
    run("calendar.session_counts");
}

#[test]
fn calendar_thin_sessions() {
    run("calendar.thin_sessions");
}

#[test]
fn calendar_build_calendar() {
    run("calendar.build_calendar");
}

#[test]
fn calendar_drop_thin_sessions() {
    run("calendar.drop_thin_sessions");
}

#[test]
fn calendar_as_shared_config() {
    run("calendar.as_shared_config");
}

// ---------------------------------------------------------------------------------------------
// indicators
// ---------------------------------------------------------------------------------------------

#[test]
fn indicators_bucket_keys() {
    run("indicators.bucket_keys");
}

#[test]
fn indicators_require_columns() {
    run("indicators.require_columns");
}

fn calendar_input(g: &Golden) -> SessionCalendar {
    g.input_as("calendar").unwrap()
}

#[test]
fn indicators_with_twt_indicators() {
    run("indicators.with_twt_indicators");
}

// ---------------------------------------------------------------------------------------------
// signals
// ---------------------------------------------------------------------------------------------

#[test]
fn signals_weekly_column() {
    run("signals.weekly_column");
}

#[test]
fn signals_weekly_closes() {
    run("signals.weekly_closes");
}

#[test]
fn signals_month_low_back() {
    run("signals.month_low_back");
}

#[test]
fn signals_tight_state() {
    run("signals.tight_state");
}

#[test]
fn signals_entry_events() {
    run("signals.entry_events");
}

#[test]
fn signals_liquidity_floor() {
    run("signals.liquidity_floor");
}

#[test]
fn signals_signal_mask() {
    run("signals.signal_mask");
}

#[test]
fn signals_detect_signals() {
    run("signals.detect_signals");
}

#[test]
fn signals_with_twt_columns() {
    run("signals.with_twt_columns");
}

// ---------------------------------------------------------------------------------------------
// breadth
// ---------------------------------------------------------------------------------------------

#[test]
fn breadth_gate_for() {
    run("breadth.gate_for");
}

#[test]
fn breadth_breadth_above_dma() {
    run("breadth.breadth_above_dma");
}

#[test]
fn breadth_breadth_series() {
    run("breadth.breadth_series");
}

#[test]
fn breadth_as_shared_config() {
    run("breadth.as_shared_config");
}

// ---------------------------------------------------------------------------------------------
// sizing, sleeve, drift, published
// ---------------------------------------------------------------------------------------------

#[test]
fn sizing_size_entry() {
    run("sizing.size_entry");
}

#[test]
fn sizing_first_live_multiplier() {
    run("sizing.first_live_multiplier");
}

#[test]
fn sleeve_sleeve_value() {
    run("sleeve.sleeve_value");
}

#[test]
fn drift_compare() {
    run("drift.compare");
}

#[test]
fn published_record() {
    run("published.PUBLISHED");
}
