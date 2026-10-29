#![allow(clippy::unwrap_used, clippy::expect_used, clippy::panic)]
//! VBT lane A against the goldens: `baskfy_core.vbt.{config, calendar, indicators, signals,
//! breadth, sizing, sleeve, drift, published, orders}`
//! (`go/testdata/golden/L1/vbt/<module>.<function>.case_NNN.json`).
//!
//! Every case of every function is loaded and graded strictly by the testkit; the case count
//! per function is asserted so a new or vanished golden is noticed. Inputs are decoded the way
//! `tools/parity/golden_vbt.py` decodes them; a Python raise is graded as
//! `{"raises": {"type", "message"}}`.

use baskfy_core::CoreError;
use baskfy_core::vbt::breadth::{breadth_above_dma, breadth_series, gate_for};
use baskfy_core::vbt::calendar::{
    SessionCalendar, build_calendar, drop_thin_sessions, session_counts, thin_sessions,
};
use baskfy_core::vbt::config::{
    BreadthConfig, DataConfig, SizingConfig, TrendConfig, VbtConfig, filter_legend,
};
use baskfy_core::vbt::drift::compare;
use baskfy_core::vbt::indicators::{densify, require_columns, with_vbt_indicators};
use baskfy_core::vbt::orders::{
    CancelReason, Fill, WorkingOrder, advance_session, apply_fill, expire_orders, expires_after,
    fill_if_touched, is_expired, sessions_since,
};
use baskfy_core::vbt::published::{PUBLISHED, PublishedStudy};
use baskfy_core::vbt::signals::{
    chartink_scan, detect_signals, signal_mask, trend_filters, with_signal_columns,
};
use baskfy_core::vbt::sizing::{first_live_multiplier, size_entry};
use baskfy_core::vbt::sleeve::{OpenPositionValue, WorkingCommitment, sleeve_value};
use baskfy_testkit::{Golden, encode_frame, encode_series, to_golden_value};
use chrono::NaiveDate;
use serde::Deserialize;
use serde_json::{Value, json};

type R = Result<(), String>;

const P: &str = "baskfy_core.vbt.";

fn s<E: std::fmt::Display>(e: E) -> String {
    e.to_string()
}

/// A Python raise as golden_vbt.py stores it.
fn raises(e: &CoreError) -> Value {
    let (ty, msg) = match e {
        CoreError::InvalidInput(m) | CoreError::MissingColumn(m) => ("ValueError", m.clone()),
        CoreError::Arithmetic(m) => ("ArithmeticError", m.clone()),
        CoreError::Polars(p) => ("PolarsError", p.to_string()),
    };
    json!({"raises": {"type": ty, "message": msg}})
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

fn calendar(g: &Golden) -> Result<SessionCalendar, String> {
    g.input_as("calendar").map_err(s)
}

fn opt_date(g: &Golden, key: &str) -> Result<Option<NaiveDate>, String> {
    g.input_as(key).map_err(s)
}

fn calendar_out(cal: &SessionCalendar) -> Result<Value, String> {
    val(cal)
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

// ------------------------------------------------------------------------------------- config

#[test]
fn config_min_samples() {
    grade("config.DataConfig.min_samples", 11, |g| {
        let data: DataConfig = g.input_as("data").map_err(s)?;
        let w: i64 = g.input_as("window_bars").map_err(s)?;
        check(g, json!(data.min_samples(w).map_err(s)?))
    });
}

#[test]
fn config_bars_required() {
    grade("config.VbtConfig.bars_required", 5, |g| {
        check(g, json!(config(g)?.bars_required()))
    });
}

#[test]
fn config_filter_legend() {
    grade("config.filter_legend", 4, |g| {
        let trend: Option<TrendConfig> = g.input_as("trend").map_err(s)?;
        check(g, val(&filter_legend(trend.as_ref()))?)
    });
}

// ----------------------------------------------------------------------------------- calendar

#[test]
fn calendar_session_counts() {
    grade("calendar.session_counts", 5, |g| {
        let bars = g.input_frame("bars").map_err(s)?;
        let out = match session_counts(&bars) {
            Ok(df) => encode_frame(&df).map_err(s)?,
            Err(e) => raises(&e),
        };
        check(g, out)
    });
}

#[test]
fn calendar_thin_sessions() {
    grade("calendar.thin_sessions", 8, |g| {
        let bars = g.input_frame("bars").map_err(s)?;
        let data: Option<DataConfig> = g.input_as("data").map_err(s)?;
        let out = match thin_sessions(&bars, data.as_ref()) {
            Ok(v) => val(&v)?,
            Err(e) => raises(&e),
        };
        check(g, out)
    });
}

#[test]
fn calendar_build_calendar() {
    grade("calendar.build_calendar", 5, |g| {
        let bars = g.input_frame("bars").map_err(s)?;
        let out = match build_calendar(&bars, &config(g)?) {
            Ok(c) => calendar_out(&c)?,
            Err(e) => raises(&e),
        };
        check(g, out)
    });
}

#[test]
fn calendar_drop_thin_sessions() {
    grade("calendar.drop_thin_sessions", 4, |g| {
        let bars = g.input_frame("bars").map_err(s)?;
        let out = match drop_thin_sessions(&bars, &config(g)?) {
            Ok((df, c)) => {
                json!({"bars": encode_frame(&df).map_err(s)?, "calendar": calendar_out(&c)?})
            }
            Err(e) => raises(&e),
        };
        check(g, out)
    });
}

#[test]
fn calendar_index_of() {
    grade("calendar.SessionCalendar.index_of", 6, |g| {
        let session = g.input_date("session").map_err(s)?;
        check(g, json!(calendar(g)?.index_of(session)))
    });
}

#[test]
fn calendar_sessions_between() {
    grade("calendar.SessionCalendar.sessions_between", 5, |g| {
        let (first, last) = (
            g.input_date("first").map_err(s)?,
            g.input_date("last").map_err(s)?,
        );
        check(g, json!(calendar(g)?.sessions_between(first, last)))
    });
}

#[test]
fn calendar_advance() {
    grade("calendar.SessionCalendar.advance", 5, |g| {
        let session = g.input_date("session").map_err(s)?;
        let n: i64 = g.input_as("sessions").map_err(s)?;
        check(g, val(&calendar(g)?.advance(session, n).map_err(s)?)?)
    });
}

// --------------------------------------------------------------------------------- indicators

#[test]
fn indicators_require_columns() {
    grade("indicators.require_columns", 3, |g| {
        let bars = g.input_frame("bars").map_err(s)?;
        let out = match require_columns(&bars) {
            Ok(()) => json!({"ok": true}),
            Err(e) => raises(&e),
        };
        check(g, out)
    });
}

#[test]
fn indicators_densify() {
    grade("indicators.densify", 5, |g| {
        let bars = g.input_frame("bars").map_err(s)?;
        let df = densify(&bars, &calendar(g)?).map_err(s)?;
        g.check_frame(&df)
    });
}

#[test]
fn indicators_with_vbt_indicators() {
    grade("indicators.with_vbt_indicators", 7, |g| {
        let bars = g.input_frame("bars").map_err(s)?;
        let out = match with_vbt_indicators(&bars, &calendar(g)?, &config(g)?) {
            Ok(df) => encode_frame(&df).map_err(s)?,
            Err(e) => raises(&e),
        };
        check(g, out)
    });
}

// ------------------------------------------------------------------------------------ signals

#[test]
fn signals_chartink_scan() {
    grade("signals.chartink_scan", 9, |g| {
        let frame = g.input_frame("frame").map_err(s)?;
        let cfg = config(g)?;
        let series = chartink_scan(&frame, Some(&cfg.scan)).map_err(s)?;
        check(g, encode_series(&series).map_err(s)?)
    });
}

#[test]
fn signals_trend_filters() {
    grade("signals.trend_filters", 7, |g| {
        let frame = g.input_frame("frame").map_err(s)?;
        let cfg = config(g)?;
        let mut out = serde_json::Map::new();
        for (f, series) in trend_filters(&frame, Some(&cfg.trend)).map_err(s)? {
            out.insert(f.value().to_owned(), encode_series(&series).map_err(s)?);
        }
        check(g, Value::Object(out))
    });
}

#[test]
fn signals_with_signal_columns() {
    grade("signals.with_signal_columns", 4, |g| {
        let frame = g.input_frame("frame").map_err(s)?;
        let df = with_signal_columns(&frame, &config(g)?).map_err(s)?;
        g.check_frame(&df)
    });
}

#[test]
fn signals_detect_signals() {
    grade("signals.detect_signals", 6, |g| {
        let frame = g.input_frame("frame").map_err(s)?;
        let df = detect_signals(&frame, opt_date(g, "as_of")?, &config(g)?).map_err(s)?;
        g.check_frame(&df)
    });
}

#[test]
fn signals_signal_mask() {
    grade("signals.signal_mask", 3, |g| {
        let frame = g.input_frame("frame").map_err(s)?;
        let series = signal_mask(&frame, &config(g)?).map_err(s)?;
        check(g, encode_series(&series).map_err(s)?)
    });
}

// ------------------------------------------------------------------------------------ breadth

#[test]
fn breadth_gate_for() {
    grade("breadth.gate_for", 7, |g| {
        let breadth: Option<BreadthConfig> = g.input_as("breadth").map_err(s)?;
        let pct = g.input_decimal("pct_above_dma").map_err(s)?;
        check(g, val(&gate_for(pct, breadth.as_ref()).map_err(s)?)?)
    });
}

#[test]
fn breadth_above_dma_reading() {
    grade("breadth.breadth_above_dma", 8, |g| {
        let frame = g.input_frame("frame").map_err(s)?;
        let reading = breadth_above_dma(&frame, opt_date(g, "as_of")?, &config(g)?).map_err(s)?;
        check(
            g,
            json!({"reading": val(&reading)?, "is_open": reading.is_open()}),
        )
    });
}

#[test]
fn breadth_series_frame() {
    grade("breadth.breadth_series", 4, |g| {
        let frame = g.input_frame("frame").map_err(s)?;
        let df = breadth_series(&frame, &config(g)?).map_err(s)?;
        g.check_frame(&df)
    });
}

// ------------------------------------------------------------------------------------- sizing

#[test]
fn sizing_size_entry() {
    grade("sizing.size_entry", 15, |g| {
        let cfg: SizingConfig = g.input_as("config").map_err(s)?;
        let sized = size_entry(
            g.input_decimal("equity").map_err(s)?,
            g.input_decimal("cash_available").map_err(s)?,
            g.input_decimal("limit_price").map_err(s)?,
            g.input_decimal("stop_price").map_err(s)?,
            g.input_opt_decimal("turnover_avg_inr").map_err(s)?,
            &cfg,
            g.input_decimal("slot_multiplier").map_err(s)?,
        )
        .map_err(s)?;
        check(g, json!({"sized": val(&sized)?, "placed": sized.placed()}))
    });
}

#[test]
fn sizing_first_live_multiplier() {
    grade("sizing.first_live_multiplier", 6, |g| {
        let cfg: SizingConfig = g.input_as("config").map_err(s)?;
        let left: i64 = g.input_as("sessions_left").map_err(s)?;
        let enabled: bool = g.input_as("execution_enabled").map_err(s)?;
        check(
            g,
            val(&first_live_multiplier(left, enabled, &cfg).map_err(s)?)?,
        )
    });
}

// ------------------------------------------------------------------------------------- sleeve

#[test]
fn sleeve_sleeve_value() {
    grade("sleeve.sleeve_value", 7, |g| {
        let positions: Vec<OpenPositionValue> = g.input_as("open_positions").map_err(s)?;
        let orders: Vec<WorkingCommitment> = g.input_as("working_orders").map_err(s)?;
        let v = sleeve_value(
            g.input_decimal("capital_inr").map_err(s)?,
            g.input_decimal("realised_inr").map_err(s)?,
            &positions,
            &orders,
        )
        .map_err(s)?;
        check(
            g,
            json!({
                "value": val(&v)?,
                "cash_inr": val(&v.cash_inr().map_err(s)?)?,
                "cash_available_inr": val(&v.cash_available_inr().map_err(s)?)?,
                "equity_inr": val(&v.equity_inr().map_err(s)?)?,
                "open_exposure_inr": val(&v.open_exposure_inr().map_err(s)?)?,
                "unrealised_inr": val(&v.unrealised_inr().map_err(s)?)?,
                "quantized": val(&v.quantize().map_err(s)?)?,
            }),
        )
    });
}

// ------------------------------------------------------------------------- drift / published

#[test]
fn drift_compare() {
    grade("drift.compare", 7, |g| {
        let study: Option<PublishedStudy> = g.input_as("study").map_err(s)?;
        let threshold: Option<f64> = g.input_as("threshold").map_err(s)?;
        let d = compare(
            g.input_as("cagr_pct").map_err(s)?,
            g.input_as("max_drawdown_pct").map_err(s)?,
            g.input_as("trades").map_err(s)?,
            study.as_ref(),
            threshold,
        );
        check(g, json!({"drift": val(&d)?, "json": val(&d.to_json())?}))
    });
}

#[test]
fn published_record() {
    grade("published.PUBLISHED", 1, |g| {
        check(
            g,
            json!({"study": val(&PUBLISHED)?, "cagr": val(&PUBLISHED.cagr().map_err(s)?)?}),
        )
    });
}

// ------------------------------------------------------------------------------------- orders

fn order(g: &Golden) -> Result<WorkingOrder, String> {
    g.input_as("order").map_err(s)
}

fn order_out(o: &WorkingOrder) -> Result<Value, String> {
    Ok(json!({"order": val(o)?, "is_live": o.is_live(), "remaining": o.remaining()}))
}

#[test]
fn orders_expires_after() {
    grade("orders.expires_after", 7, |g| {
        let day = g.input_date("signal_date").map_err(s)?;
        let out = expires_after(day, &calendar(g)?, &config(g)?.entry).map_err(s)?;
        check(g, val(&out)?)
    });
}

#[test]
fn orders_sessions_since() {
    grade("orders.sessions_since", 5, |g| {
        let (sig, ses) = (
            g.input_date("signal_date").map_err(s)?,
            g.input_date("session").map_err(s)?,
        );
        check(g, json!(sessions_since(sig, ses, &calendar(g)?)))
    });
}

#[test]
fn orders_is_expired() {
    grade("orders.is_expired", 6, |g| {
        let ses = g.input_date("session").map_err(s)?;
        let out = is_expired(&order(g)?, ses, &calendar(g)?, &config(g)?.entry);
        check(g, json!(out))
    });
}

#[test]
fn orders_advance_session() {
    grade("orders.advance_session", 3, |g| {
        check(g, order_out(&advance_session(&order(g)?))?)
    });
}

#[test]
fn orders_expire_orders() {
    grade("orders.expire_orders", 5, |g| {
        let orders: Vec<WorkingOrder> = g.input_as("orders").map_err(s)?;
        let reason: CancelReason = g.input_as("reason").map_err(s)?;
        let ses = g.input_date("session").map_err(s)?;
        let (live, done) = expire_orders(&orders, ses, &calendar(g)?, &config(g)?.entry, reason);
        check(g, json!({"live": val(&live)?, "expired": val(&done)?}))
    });
}

#[test]
fn orders_fill_if_touched() {
    grade("orders.fill_if_touched", 11, |g| {
        let ses = g.input_date("session").map_err(s)?;
        let out = fill_if_touched(
            &order(g)?,
            ses,
            g.input_opt_decimal("open_price").map_err(s)?,
            g.input_opt_decimal("high").map_err(s)?,
            g.input_opt_decimal("low").map_err(s)?,
            &config(g)?.entry,
        )
        .map_err(s)?;
        check(g, val(&out)?)
    });
}

#[derive(Deserialize)]
struct FillDoc {
    quantity: i64,
    price: rust_decimal::Decimal,
    session: NaiveDate,
}

#[test]
fn orders_apply_fill() {
    grade("orders.apply_fill", 4, |g| {
        let o = order(g)?;
        let f: FillDoc = g.input_as("fill").map_err(s)?;
        let fill = Fill {
            order: o.clone(),
            quantity: f.quantity,
            price: f.price,
            session: f.session,
        };
        check(g, order_out(&apply_fill(&o, &fill))?)
    });
}
