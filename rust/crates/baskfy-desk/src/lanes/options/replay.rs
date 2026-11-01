//! Ports `tools/options/replay.py` (OP9): a recorded session through the options monitor, and
//! what it raised.
//!
//! A fixture (`tools/options/make_fixtures.py` writes them) is one JSON file: the day, one open
//! position as `op_position` and its plan carry it, NIFTY 50's minute bars, and each leg's
//! bid/ask each minute. Each index minute becomes four ticks — open, low, high, close at +0, +15,
//! +30 and +45 seconds — and each leg quote one depth tick at +50 seconds. `feed_stops` drops
//! every index tick from that moment on; `start` replays a restart (the ticks before it are not
//! delivered, the bars before it come back through the store's `index_minutes`).
//!
//! The strategy is driven with no broker, no bus and no database: the store is a list.

use std::str::FromStr;

use async_trait::async_trait;
use baskfy_core::options::bars::{Bar, ist};
use baskfy_core::options::exits::{ExitVerdict, OpenLeg, OpenPosition};
use chrono::{Duration, NaiveDate, NaiveDateTime};
use rust_decimal::Decimal;
use serde_json::Value;

use super::core::{role_of, sleeve_of};
use super::monitor::{
    NIFTY_50_TOKEN, NiftyOptionsMonitor, PositionStore, Raised, TrackedPosition, aware_ist,
};
use crate::wire::{PyDict, PyValue, dict};
use baskfy_providers::records::PyDateTime;

fn text<'v>(v: &'v Value, key: &str) -> Result<&'v str, String> {
    v.get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("KeyError: '{key}'"))
}

fn dec(s: &str) -> Result<Decimal, String> {
    Decimal::from_str(s).map_err(|_| format!("decimal.InvalidOperation: {s:?}"))
}

fn opt_dec(v: &Value, key: &str) -> Result<Option<Decimal>, String> {
    match v.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) if s.is_empty() => Ok(None),
        Some(Value::String(s)) => dec(s).map(Some),
        Some(other) => dec(&other.to_string()).map(Some),
    }
}

fn iso(s: &str) -> Result<NaiveDateTime, String> {
    match PyDateTime::parse(s).map_err(|e| e.to_string())? {
        PyDateTime::Naive(n) => Ok(n),
        PyDateTime::Aware(a) => Ok(baskfy_core::options::bars::ist(&a)),
    }
}

fn int_of(v: &Value) -> Result<i64, String> {
    match v {
        Value::Number(n) => n.as_i64().ok_or_else(|| format!("ValueError: {n}")),
        Value::String(s) => s
            .parse()
            .map_err(|_| format!("ValueError: invalid literal for int() with base 10: '{s}'")),
        other => Err(format!("TypeError: int({other})")),
    }
}

/// `position_of(raw)`: the fixture's position as the store would hand it to the monitor.
///
/// # Errors
/// A field Python's constructors refuse.
pub fn position_of(raw: &Value) -> Result<TrackedPosition, String> {
    let mut legs = Vec::new();
    for lg in raw
        .get("legs")
        .and_then(Value::as_array)
        .ok_or("KeyError: 'legs'")?
    {
        let role = text(lg, "role")?;
        legs.push(OpenLeg {
            role: role_of(role)
                .ok_or_else(|| format!("ValueError: '{role}' is not a valid LegRole"))?,
            strike: dec(text(lg, "strike")?)?,
            quantity: int_of(lg.get("quantity").ok_or("KeyError: 'quantity'")?)?,
            fill_price: dec(text(lg, "fill_price")?)?,
            instrument_token: int_of(
                lg.get("instrument_token")
                    .ok_or("KeyError: 'instrument_token'")?,
            )?,
        });
    }
    let sleeve = text(raw, "sleeve")?;
    let structure = text(raw, "structure")?;
    let direction = match raw.get("direction").and_then(Value::as_str) {
        Some(d) if !d.is_empty() => Some(
            super::core::direction_of(d)
                .ok_or_else(|| format!("ValueError: '{d}' is not a valid Direction"))?,
        ),
        _ => None,
    };
    Ok(TrackedPosition {
        session_id: int_of(raw.get("session_id").ok_or("KeyError: 'session_id'")?)?,
        position: OpenPosition {
            sleeve: sleeve_of(sleeve)
                .ok_or_else(|| format!("ValueError: '{sleeve}' is not a valid Sleeve"))?,
            structure: super::core::structure_of(structure)
                .ok_or_else(|| format!("ValueError: '{structure}' is not a valid Structure"))?,
            legs,
            entry_points: dec(text(raw, "entry_points")?)?,
            opened_at: aware_ist(iso(text(raw, "opened_at")?)?),
            risk_budget_inr: dec(text(raw, "risk_budget_inr")?)?,
            direction,
            range_high: opt_dec(raw, "range_high")?,
            range_low: opt_dec(raw, "range_low")?,
            half_gap: opt_dec(raw, "half_gap")?,
            width_points: opt_dec(raw, "width_points")?,
        },
        exit_plan_id: None,
    })
}

fn rows<'v>(fixture: &'v Value, key: &str) -> Result<&'v Vec<Value>, String> {
    fixture
        .get(key)
        .and_then(Value::as_array)
        .ok_or_else(|| format!("KeyError: '{key}'"))
}

fn cell(row: &Value, i: usize) -> Result<&str, String> {
    row.get(i)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("IndexError: {i}"))
}

/// `bars_of(fixture)`: the index minutes.
///
/// # Errors
/// A malformed row.
pub fn bars_of(fixture: &Value) -> Result<Vec<Bar>, String> {
    rows(fixture, "index")?
        .iter()
        .map(|r| {
            Ok(Bar {
                ts: aware_ist(iso(cell(r, 0)?)?),
                open: dec(cell(r, 1)?)?,
                high: dec(cell(r, 2)?)?,
                low: dec(cell(r, 3)?)?,
                close: dec(cell(r, 4)?)?,
            })
        })
        .collect()
}

fn at_value(at: NaiveDateTime) -> PyValue {
    PyValue::DateTime(PyDateTime::Naive(at))
}

/// `ticks_of(fixture)`: the session as ticks, in delivery order (index before leg within the
/// same second).
///
/// # Errors
/// A malformed row.
pub fn ticks_of(fixture: &Value) -> Result<Vec<PyDict>, String> {
    let stop_at = match fixture.get("feed_stops").and_then(Value::as_str) {
        Some(s) if !s.is_empty() => Some(iso(s)?),
        _ => None,
    };
    let mut out: Vec<(NaiveDateTime, u8, PyDict)> = Vec::new();
    for r in rows(fixture, "index")? {
        let t = iso(cell(r, 0)?)?;
        for (sec, i) in [(0, 1), (15, 3), (30, 2), (45, 4)] {
            let at = t + Duration::seconds(sec);
            if stop_at.is_none_or(|s| at < s) {
                out.push((
                    at,
                    0,
                    dict([
                        ("instrument_token", PyValue::Int(NIFTY_50_TOKEN)),
                        ("last_price", PyValue::Str(cell(r, i)?.to_owned())),
                        ("exchange_timestamp", at_value(at)),
                    ]),
                ));
            }
        }
    }
    for r in rows(fixture, "legs")? {
        let at = iso(cell(r, 0)?)? + Duration::seconds(50);
        let token = int_of(r.get(1).ok_or("IndexError: 1")?)?;
        let level = |price: &str| {
            PyValue::List(vec![PyValue::Map(dict([
                ("price", PyValue::Str(price.to_owned())),
                ("quantity", PyValue::Int(1300)),
            ]))])
        };
        let depth = dict([("buy", level(cell(r, 2)?)), ("sell", level(cell(r, 3)?))]);
        out.push((
            at,
            1,
            dict([
                ("instrument_token", PyValue::Int(token)),
                ("depth", PyValue::Map(depth)),
                ("exchange_timestamp", at_value(at)),
            ]),
        ));
    }
    out.sort_by_key(|(at, kind, _)| (*at, *kind));
    Ok(out.into_iter().map(|(_, _, t)| t).collect())
}

/// `ListStore`: the monitor's store as lists — the positions it holds, every exit and mark it
/// recorded.
#[derive(Debug, Clone, Default)]
pub struct ListStore {
    /// `positions`.
    pub positions: Vec<TrackedPosition>,
    /// `minutes` (what `op_index_minute` would hold).
    pub minutes: Vec<Bar>,
    /// Every exit raised: `(session_id, code, reason, at)`.
    pub exits: Vec<Raised>,
    /// Every mark recorded: `(session_id, value, at)`.
    pub marks: Vec<(i64, Decimal, NaiveDateTime)>,
}

impl ListStore {
    /// `ListStore(positions, minutes)`.
    #[must_use]
    pub const fn new(positions: Vec<TrackedPosition>, minutes: Vec<Bar>) -> Self {
        Self {
            positions,
            minutes,
            exits: Vec::new(),
            marks: Vec::new(),
        }
    }
}

#[async_trait]
impl PositionStore for ListStore {
    async fn open_positions(&mut self, _day: NaiveDate) -> Result<Vec<TrackedPosition>, String> {
        Ok(self.positions.clone())
    }

    async fn raise_exit(
        &mut self,
        tracked: &TrackedPosition,
        verdict: &ExitVerdict,
        at: NaiveDateTime,
    ) -> Result<Option<String>, String> {
        self.exits.push((
            tracked.session_id,
            verdict.code.clone(),
            verdict.reason.clone(),
            at,
        ));
        let plan_id = format!("EXIT-{}", tracked.session_id);
        for p in &mut self.positions {
            if p.session_id == tracked.session_id {
                p.exit_plan_id = Some(plan_id.clone());
            }
        }
        Ok(Some(plan_id))
    }

    async fn record_mark(
        &mut self,
        tracked: &TrackedPosition,
        value: Decimal,
        at: NaiveDateTime,
    ) -> Result<(), String> {
        self.marks.push((tracked.session_id, value, at));
        Ok(())
    }

    async fn index_minutes(
        &mut self,
        _day: NaiveDate,
        until: NaiveDateTime,
    ) -> Result<Vec<Bar>, String> {
        Ok(self
            .minutes
            .iter()
            .filter(|b| ist(&b.ts) + Duration::minutes(1) <= until)
            .cloned()
            .collect())
    }
}

fn day_of(fixture: &Value) -> Result<NaiveDate, String> {
    let d = text(fixture, "day")?;
    NaiveDate::parse_from_str(d, "%Y-%m-%d")
        .map_err(|_| format!("ValueError: Invalid isoformat string: '{d}'"))
}

/// `replay(fixture)`: drive the monitor through the fixture; the monitor (its store is the
/// `ListStore`).
///
/// # Errors
/// A malformed fixture, or the monitor's exception.
pub async fn replay(fixture: &Value) -> Result<NiftyOptionsMonitor<ListStore>, String> {
    let day = day_of(fixture)?;
    let start = match fixture.get("start").and_then(Value::as_str) {
        Some(s) if !s.is_empty() => Some(iso(s)?),
        _ => None,
    };
    let stored: Vec<Bar> = bars_of(fixture)?
        .into_iter()
        .filter(|b| start.is_some_and(|s| ist(&b.ts) < s))
        .collect();
    let position = position_of(fixture.get("position").ok_or("KeyError: 'position'")?)?;
    let store = ListStore::new(vec![position], stored);
    let mut monitor = NiftyOptionsMonitor::new(store, day);
    monitor.start().await;
    for tick in ticks_of(fixture)? {
        if let (Some(s), Some(PyValue::DateTime(PyDateTime::Naive(at)))) =
            (start, tick.get("exchange_timestamp"))
            && *at < s
        {
            continue;
        }
        monitor.tick(&tick).await?;
    }
    Ok(monitor)
}

/// `as_json(exits)`.
#[must_use]
pub fn as_json(exits: &[Raised]) -> Value {
    Value::Array(
        exits
            .iter()
            .map(|(sid, code, reason, at)| {
                serde_json::json!({
                    "session_id": sid,
                    "code": code,
                    "reason": reason,
                    "at": super::py::iso_naive(at, 'T'),
                })
            })
            .collect(),
    )
}

/// `json.dumps(value, indent=1)` for the exits' shape (a list of flat objects).
#[must_use]
pub fn dumps_indent1(value: &Value) -> String {
    let mut buf = Vec::new();
    let formatter = serde_json::ser::PrettyFormatter::with_indent(b" ");
    let mut ser = serde_json::Serializer::with_formatter(&mut buf, formatter);
    if serde::Serialize::serialize(value, &mut ser).is_err() {
        return value.to_string();
    }
    String::from_utf8(buf).unwrap_or_else(|_| value.to_string())
}

/// `main(argv)`: `replay <fixture> [--expect <expected>]` — 0 when the exits match the
/// expectation exactly (or none is given), 1 when they do not. The lines it would print are
/// returned beside the code.
///
/// # Errors
/// An unreadable or malformed file (Python's exception), or the monitor's.
pub async fn main(argv: &[String]) -> Result<(i32, Vec<String>), String> {
    let mut fixture_path: Option<&str> = None;
    let mut expect: Option<&str> = None;
    let mut args = argv.iter();
    while let Some(a) = args.next() {
        if a == "--expect" {
            expect = Some(
                args.next()
                    .ok_or("argument --expect: expected one argument")?,
            );
        } else {
            fixture_path = Some(a);
        }
    }
    let fixture_path = fixture_path.ok_or("the following arguments are required: fixture")?;
    let read = |p: &str| -> Result<Value, String> {
        let raw = std::fs::read_to_string(p).map_err(|e| format!("FileNotFoundError: {e}"))?;
        serde_json::from_str(&raw).map_err(|e| format!("JSONDecodeError: {e}"))
    };
    let monitor = replay(&read(fixture_path)?).await?;
    let got = as_json(&monitor.store.exits);
    let mut lines = vec![dumps_indent1(&got)];
    let Some(expect) = expect else {
        return Ok((0, lines));
    };
    let want = read(expect)?;
    if got == want {
        lines.push("OK: the exits match the expectation".to_owned());
        return Ok((0, lines));
    }
    lines.push(format!("MISMATCH\n  expected: {want}\n  got:      {got}"));
    Ok((1, lines))
}
