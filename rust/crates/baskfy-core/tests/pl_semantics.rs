//! polars' NaN semantics in the ports, graded against the live Python (DECISIONS-RS R3.1).
//!
//! The goldens cannot carry a NaN (the encoder writes `null`), so these inputs are built
//! in-process. Two layers:
//!
//! * **Recorded cases** (always run): frames with NaN, `±inf`, `±0.0` and null through twt's
//!   `tight_state`, `month_low_back`, `liquidity_floor`, `breadth_above_dma`, `breadth_series`
//!   and `with_twt_indicators`' `limit_locked`. Every expected value was printed by the live
//!   Python (polars 1.43.2) from the identical frame; each case names the divergence from IEEE
//!   it pins.
//! * **Differential fuzz** (ignored by default): `tests/fuzz/pl_semantics_fuzz.py` writes 5,765
//!   cases from the live Python — `max_horizontal`/`min_horizontal` over 3,000 random frames,
//!   group `min`/`max` over 2,000 value sets, the five comparisons over every pair, and
//!   `tight_state`/`month_low_back`/`liquidity_floor` over random NaN-laden frames — and
//!   [`differential_fuzz`] grades `crate::pl` and the ports against them, bit for bit.
#![allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]

use baskfy_core::pl;
use baskfy_core::twt::breadth::{breadth_above_dma, breadth_series};
use baskfy_core::twt::calendar::build_calendar;
use baskfy_core::twt::config::{Gate, TwtConfig};
use baskfy_core::twt::indicators::with_twt_indicators;
use baskfy_core::twt::signals::{liquidity_floor, month_low_back, tight_state};
use chrono::NaiveDate;
use polars::prelude::{Column, DataFrame, DataType, IntoColumn, NamedFrom, Series};
use serde_json::Value;

const NAN: f64 = f64::NAN;
const INF: f64 = f64::INFINITY;

type F = Option<f64>;

fn fcol(name: &str, values: &[F]) -> Column {
    Series::new(name.into(), values.to_vec()).into_column()
}

fn icol(name: &str, values: &[i64]) -> Column {
    Series::new(name.into(), values.to_vec()).into_column()
}

fn bcol(name: &str, values: &[Option<bool>]) -> Column {
    Series::new(name.into(), values.to_vec()).into_column()
}

fn date_col(name: &str, days: &[NaiveDate]) -> Column {
    let epoch = NaiveDate::from_ymd_opt(1970, 1, 1).unwrap();
    let n: Vec<i32> = days
        .iter()
        .map(|d| i32::try_from(d.signed_duration_since(epoch).num_days()).unwrap())
        .collect();
    Series::new(name.into(), n)
        .cast(&DataType::Date)
        .unwrap()
        .into_column()
}

fn frame(columns: Vec<Column>) -> DataFrame {
    let height = columns.first().map_or(0, Column::len);
    DataFrame::new(height, columns).unwrap()
}

fn floats(df: &DataFrame, name: &str) -> Vec<F> {
    df.column(name)
        .unwrap()
        .as_materialized_series()
        .cast(&DataType::Float64)
        .unwrap()
        .f64()
        .unwrap()
        .iter()
        .collect()
}

fn bools(df: &DataFrame, name: &str) -> Vec<Option<bool>> {
    df.column(name)
        .unwrap()
        .as_materialized_series()
        .bool()
        .unwrap()
        .iter()
        .collect()
}

/// Bit-equal floats, every NaN one value, null only null.
fn same(a: F, b: F) -> bool {
    match (a, b) {
        (None, None) => true,
        (Some(x), Some(y)) => (x.is_nan() && y.is_nan()) || x.to_bits() == y.to_bits(),
        _ => false,
    }
}

fn assert_floats(what: &str, got: &[F], want: &[F]) {
    assert!(
        got.len() == want.len() && got.iter().zip(want).all(|(g, w)| same(*g, *w)),
        "{what}: got {got:?}, want {want:?}"
    );
}

fn day(d: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 10, d).unwrap()
}

// ---------------------------------------------------------------------------------------------
// Recorded cases
// ---------------------------------------------------------------------------------------------

/// The tight-state frame of the recorded case: one row per divergence.
fn tight_frame() -> DataFrame {
    let weeks: [[F; 3]; 11] = [
        [Some(100.0), Some(101.0), Some(102.0)],
        // `max_horizontal(NaN, 101, 102)` with no null in w0: the IEEE kernel, NaN loses -> 102.
        [Some(NAN), Some(101.0), Some(102.0)],
        [Some(100.0), Some(101.0), Some(102.0)],
        [Some(100.0), Some(101.0), Some(102.0)],
        [Some(100.0), Some(101.0), Some(102.0)],
        [Some(100.0), Some(101.0), Some(102.0)],
        // w1 holds a null elsewhere: `w1 op w2` runs the total kernel, NaN wins -> NaN.
        [Some(100.0), Some(NAN), Some(102.0)],
        // Signed zeros: the minimum is -0.0, the range -inf, so `tight` holds.
        [Some(1.0), Some(0.0), Some(-0.0)],
        [Some(1.0), Some(-0.0), Some(0.0)],
        [Some(100.0), None, Some(102.0)],
        [Some(NAN), Some(NAN), Some(NAN)],
    ];
    let col_of = |k: usize| weeks.iter().map(|w| w[k]).collect::<Vec<F>>();
    let mut close = vec![Some(200.0); 11];
    close[2] = Some(NAN); // NaN >= 130 is true in polars.
    let mut mlb = vec![Some(100.0); 11];
    mlb[5] = Some(NAN); // 200 >= NaN x 1.3 is false.
    let mut craw = vec![Some(50.0); 11];
    craw[3] = Some(NAN); // NaN > 30 is true.
    let mut vsma = vec![Some(2e4); 11];
    vsma[4] = Some(NAN); // NaN >= 10,000 is true.
    frame(vec![
        fcol("weekly_close_0", &col_of(0)),
        fcol("weekly_close_1", &col_of(1)),
        fcol("weekly_close_2", &col_of(2)),
        fcol("close", &close),
        fcol("month_low_back", &mlb),
        fcol("close_raw", &craw),
        fcol("vol_sma", &vsma),
        bcol("is_etf", &[Some(false); 11]),
    ])
}

#[test]
fn tight_state_reads_nan_as_polars_does() {
    let out = tight_state(&tight_frame(), &TwtConfig::default()).unwrap();
    let p = Some(2.000_000_000_000_001_8);
    assert_floats(
        "tight_range_pct",
        &floats(&out, "tight_range_pct"),
        &[
            p,
            Some(0.990_099_009_900_991),
            p,
            p,
            p,
            p,
            Some(NAN),
            Some(-INF),
            Some(INF),
            None,
            Some(NAN),
        ],
    );
    let t = Some(true);
    let f = Some(false);
    assert_eq!(bools(&out, "tight"), [t, t, t, t, t, t, f, t, f, f, f]);
    assert_eq!(
        bools(&out, "above_month_low"),
        [t, t, t, t, t, f, t, t, t, t, t]
    );
    assert_eq!(
        bools(&out, "tight_state"),
        [t, t, t, t, t, f, f, t, f, f, f]
    );
}

#[test]
fn month_low_back_skips_nan_like_a_polars_min() {
    let df = frame(vec![
        icol("instrument_id", &[1, 1, 1, 1, 2, 2, 2, 3, 3, 3]),
        icol("month_key", &[10, 10, 10, 13, 10, 10, 13, 10, 10, 13]),
        fcol(
            "low",
            &[
                Some(NAN),
                Some(5.0),
                Some(6.0),
                Some(9.0),
                Some(NAN),
                Some(NAN),
                Some(9.0),
                Some(4.0),
                Some(NAN),
                Some(9.0),
            ],
        ),
    ]);
    let out = month_low_back(&df, &TwtConfig::default()).unwrap();
    assert_floats(
        "month_low_back",
        &floats(&out, "month_low_back"),
        &[
            None,
            None,
            None,
            Some(5.0),
            None,
            None,
            Some(NAN),
            None,
            None,
            Some(4.0),
        ],
    );
}

#[test]
fn liquidity_floor_passes_a_nan_turnover() {
    let df = frame(vec![fcol(
        "turnover_avg_20",
        &[Some(NAN), Some(1e8), Some(1e6), None],
    )]);
    let out = liquidity_floor(&df, &TwtConfig::default(), None).unwrap();
    let got: Vec<Option<bool>> = out.bool().unwrap().iter().collect();
    assert_eq!(got, [Some(true), Some(true), Some(false), Some(false)]);
}

#[test]
fn breadth_counts_a_nan_close_as_above() {
    let df = frame(vec![
        date_col("date", &[day(1); 4]),
        fcol("close", &[Some(NAN), Some(2.0), Some(0.5), Some(1.0)]),
        fcol("sma_dma", &[Some(1.0), Some(1.0), Some(1.0), Some(NAN)]),
    ]);
    let config = TwtConfig::default();
    let reading = breadth_above_dma(&df, Some(day(1)), &config).unwrap();
    assert_eq!(
        (
            reading.universe_count,
            reading.measured_count,
            reading.above_count
        ),
        (4, 4, 2)
    );
    assert_eq!(reading.pct_above_dma.to_string(), "50.0000");
    assert_eq!(reading.gate, Gate::Open);
    let series = breadth_series(&df, &config).unwrap();
    assert_floats(
        "pct_above_dma",
        &floats(&series, "pct_above_dma"),
        &[Some(50.0)],
    );
    assert_floats("above_count", &floats(&series, "above_count"), &[Some(2.0)]);
}

#[test]
fn limit_locked_reads_nan_equal_to_nan() {
    let bars = frame(vec![
        icol("instrument_id", &[1, 1, 2, 2]),
        date_col("date", &[day(1), day(2), day(1), day(2)]),
        fcol("open", &[Some(NAN), Some(5.0), Some(NAN), Some(5.0)]),
        fcol("high", &[Some(NAN), Some(5.0), Some(4.0), Some(NAN)]),
        fcol("low", &[Some(NAN), Some(5.0), Some(4.0), Some(NAN)]),
        fcol("close", &[Some(5.0); 4]),
        fcol("close_raw", &[Some(5.0); 4]),
        fcol("volume", &[Some(1.0); 4]),
    ]);
    let config = TwtConfig::default();
    let calendar = build_calendar(&bars, &config).unwrap();
    let out = with_twt_indicators(&bars, &calendar, &config).unwrap();
    assert_eq!(
        bools(&out, "limit_locked"),
        [Some(true), Some(true), Some(false), Some(false)]
    );
}

// ---------------------------------------------------------------------------------------------
// Differential fuzz
// ---------------------------------------------------------------------------------------------

fn dec(v: &Value) -> F {
    match v {
        Value::Null => None,
        Value::String(s) => Some(s.parse::<f64>().unwrap()),
        other => panic!("not an encoded float: {other}"),
    }
}

fn decs(v: &Value) -> Vec<F> {
    v.as_array().unwrap().iter().map(dec).collect()
}

fn opt_bools(v: &Value) -> Vec<Option<bool>> {
    v.as_array().unwrap().iter().map(Value::as_bool).collect()
}

/// Differential fuzz against the live Python: `BASKFY_PL_FUZZ` names the file
/// `tests/fuzz/pl_semantics_fuzz.py` wrote. Ignored by default; run with
/// `BASKFY_PL_FUZZ=... cargo test -p baskfy-core --test pl_semantics -- --ignored`.
#[test]
#[ignore = "needs BASKFY_PL_FUZZ from tests/fuzz/pl_semantics_fuzz.py"]
fn differential_fuzz() {
    let path = std::env::var("BASKFY_PL_FUZZ").expect("BASKFY_PL_FUZZ");
    let doc: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let mut failures: Vec<String> = Vec::new();
    let mut graded = 0_usize;

    for case in doc["horizontal"].as_array().unwrap() {
        let columns: Vec<Vec<F>> = case["columns"]
            .as_array()
            .unwrap()
            .iter()
            .map(decs)
            .collect();
        let slices: Vec<&[F]> = columns.iter().map(Vec::as_slice).collect();
        for (name, got) in [
            ("max", pl::max_horizontal(&slices)),
            ("min", pl::min_horizontal(&slices)),
        ] {
            let want = decs(&case[name]);
            graded += 1;
            if !(got.len() == want.len() && got.iter().zip(&want).all(|(g, w)| same(*g, *w))) {
                failures.push(format!(
                    "{name}_horizontal {columns:?}: {got:?} != {want:?}"
                ));
            }
        }
    }

    for case in doc["aggregate"].as_array().unwrap() {
        let values = decs(&case["values"]);
        for (name, got) in [
            ("min", pl::nan_min(values.iter().copied())),
            ("max", pl::nan_max(values.iter().copied())),
        ] {
            graded += 1;
            if !same(got, dec(&case[name])) {
                failures.push(format!("group {name} {values:?}: {got:?}"));
            }
        }
    }

    for case in doc["compare"].as_array().unwrap() {
        let (a, b) = (dec(&case["a"]), dec(&case["b"]));
        for (name, accept) in [
            ("gt", pl::gt as fn(f64, f64) -> bool),
            ("ge", pl::ge),
            ("lt", pl::lt),
            ("le", pl::le),
            ("eq", pl::eq),
        ] {
            let got = a.zip(b).map(|(x, y)| accept(x, y));
            graded += 1;
            if got != case[name].as_bool() {
                failures.push(format!("{a:?} {name} {b:?}: {got:?}"));
            }
        }
    }

    let config = TwtConfig::default();
    for case in doc["tight_state"].as_array().unwrap() {
        let f = &case["frame"];
        let names = [
            "weekly_close_0",
            "weekly_close_1",
            "weekly_close_2",
            "close",
            "month_low_back",
            "close_raw",
            "vol_sma",
        ];
        let mut cols: Vec<Column> = names.iter().map(|n| fcol(n, &decs(&f[*n]))).collect();
        cols.push(bcol("is_etf", &opt_bools(&f["is_etf"])));
        let out = tight_state(&frame(cols), &config).unwrap();
        graded += 1;
        let pct_ok = {
            let (got, want) = (
                floats(&out, "tight_range_pct"),
                decs(&case["tight_range_pct"]),
            );
            got.len() == want.len() && got.iter().zip(&want).all(|(g, w)| same(*g, *w))
        };
        let flags_ok = ["tight", "above_month_low", "tight_state"]
            .iter()
            .all(|n| bools(&out, n) == opt_bools(&case[*n]));
        if !(pct_ok && flags_ok) {
            failures.push(format!("tight_state {f}"));
        }
    }

    for case in doc["month_low"].as_array().unwrap() {
        let f = &case["frame"];
        let ints = |n: &str| -> Vec<i64> {
            f[n].as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_i64().unwrap())
                .collect()
        };
        let df = frame(vec![
            icol("instrument_id", &ints("instrument_id")),
            icol("month_key", &ints("month_key")),
            fcol("low", &decs(&f["low"])),
        ]);
        let got = floats(&month_low_back(&df, &config).unwrap(), "month_low_back");
        let want = decs(&case["month_low_back"]);
        graded += 1;
        if !(got.len() == want.len() && got.iter().zip(&want).all(|(g, w)| same(*g, *w))) {
            failures.push(format!("month_low_back {f}: {got:?} != {want:?}"));
        }
    }

    for case in doc["liquidity"].as_array().unwrap() {
        let df = frame(vec![fcol(
            "turnover_avg_20",
            &decs(&case["turnover_avg_20"]),
        )]);
        let out = liquidity_floor(&df, &config, None).unwrap();
        let got: Vec<Option<bool>> = out.bool().unwrap().iter().collect();
        graded += 1;
        if got != opt_bools(&case["passes"]) {
            failures.push(format!("liquidity_floor: {got:?}"));
        }
    }

    assert!(graded > 0, "no fuzz cases");
    assert!(
        failures.is_empty(),
        "{} of {graded} fuzz checks differ:\n{}",
        failures.len(),
        failures.join("\n")
    );
}
