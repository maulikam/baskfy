//! Sub-lane **weekly_books** (parent lane `weekly`, DECISIONS-RS R8.5): the books the weekly desk
//! reads — the EOD snapshot, the holdings table, FIFO tax lots, the Console tradebook, the
//! plan-against-broker view and the LV2 reconciler.
//!
//! **Python it ports** (`kite-momentum-rebalancer/app/`):
//! * `analytics/snapshot.py` → [`snapshot`] (collection, `run`, the CLI) over [`snapshot_db`]
//!   (`db.py`'s snapshot and cashflow half, kept here until lane `analytics` ports `db.py`,
//!   DECISIONS-RS R8.WB3)
//! * `analytics/holdings_view.py` → [`holdings_view`]
//! * `analytics/tax_lots.py` → [`tax_lots`]
//! * `analytics/tradebook.py` → [`tradebook`] (with the two `corporate_actions` reads it calls:
//!   `load` and `apply_to_book`, kept private here until lane `analytics` ports that module)
//! * `reconcile.py` → [`reconcile`] (the pass, the issue stores and the guard) and
//!   [`reconcile_hooks`] (`twt_hooks`, `vbt_hooks`, `swing_hooks` over each sleeve lane's own
//!   `on_order_update`, `run_pass_from_desk`, and the supervisor's `ReconcilePass`)
//! * `analytics/reconcile_view.py` → [`reconcile_view`] (the stops section takes the protection
//!   review as an input: `analytics/protection.py` is lane `analytics`'s)
//!
//! **The non-negotiables these modules carry.** Holdings quantity is `quantity + t1_quantity +
//! collateral_quantity` — summed by the desk's `Kite.holdings` wrapper (lane `exec_core`), which
//! every function here receives already summed and never re-derives. Pledged shares are an
//! **info flag only** (`pledged_qty`, `pledged_positions`): nothing here gates, sizes or refuses on
//! them. Untouchable instruments (SGB/G-sec) are classified by the one guard rule
//! ([`super::basket::tradeable`]). Nothing here places an order or a GTT, or names `KiteWrites`:
//! the reconcile hooks hand a broker report to the sleeve's own `on_order_update`, whose every
//! GTT re-size, arm or cancel goes through that sleeve's gateway (law 2).
//!
//! **Routes.** None: the pages that render these books (`/performance`, `/tradebook*`,
//! `/reconcile*`) are registered by the lanes that own their other inputs (wave D2: `session`
//! for `/reconcile*` and `/tradebook*`, `analytics_views` for `/performance*`); the lane's
//! `desk_cases/weekly_books.json` grades them.
//!
//! **Local time.** Python's naive `datetime.fromtimestamp` / `.timestamp()` use the process zone;
//! the desk runs with `TZ=Asia/Kolkata` (the container and `desk_diff.py`), so naive local time is
//! IST here ([`local_from_ts`], [`local_ts`]).
//!
//! **Python's exceptions** where a module lets them escape are reproduced by [`pyconv`] (`int()`,
//! `Decimal(str())`, `d[key]`), with Python's text (DECISIONS-RS R8.WB1).

pub mod holdings_view;
pub mod pyconv;
pub mod reconcile;
pub mod reconcile_hooks;
pub mod reconcile_view;
pub mod snapshot;
pub mod snapshot_db;
pub mod tax_lots;
pub mod tradebook;

mod sha1;

#[cfg(test)]
mod parity;

use axum::Router;
use chrono::{DateTime, Duration, NaiveDateTime, TimeDelta};

use crate::app::AppState;
use crate::clock::ist;
use crate::storage::DbValue;
use crate::wire::{PyDict, PyValue};

/// The sub-lane's routes: none (see the module docs).
pub fn routes() -> Router<AppState> {
    Router::new()
}

/// `baskfy-desk snapshot [args]` (`python -m app.analytics.snapshot`): read-only, never an order.
pub async fn snapshot_cli(state: &AppState, argv: &[String]) -> snapshot::CliOutcome {
    let settings = state.settings_snapshot();
    let Some(kite) = state
        .gateway
        .as_any()
        .and_then(|a| a.downcast_ref::<crate::lanes::exec_core::desk::DeskGateway>())
        .map(crate::lanes::exec_core::desk::DeskGateway::kite)
    else {
        return snapshot::CliOutcome {
            code: 1,
            stderr: vec!["RuntimeError: the desk's Kite wrapper is not wired".to_owned()],
            ..snapshot::CliOutcome::default()
        };
    };
    let mut conn = match state.db.connect().await {
        Ok(c) => c,
        Err(e) => {
            return snapshot::CliOutcome {
                code: 1,
                stderr: vec![e.to_string()],
                ..snapshot::CliOutcome::default()
            };
        }
    };
    let base = snapshot::CollectOptions {
        on_date: None,
        price_source: "holdings",
        cash: None,
        allow_stale_cash: false,
        today: state.clock.now_ist().date_naive(),
        include_untouchable_in_nav: settings.include_untouchable_in_nav,
        root: &state.paths.root,
    };
    snapshot::cli(argv, kite.as_ref(), &mut conn, base, settings.dry_run).await
}

/// `datetime.fromtimestamp(ts)` (naive, the process zone = IST), microseconds rounded half-even
/// as CPython does. `None` for a non-finite or out-of-range stamp (Python raises).
#[must_use]
#[allow(clippy::cast_possible_truncation)]
pub fn local_from_ts(ts: f64) -> Option<NaiveDateTime> {
    if !ts.is_finite() {
        return None;
    }
    let secs = ts.floor();
    let mut micros = ((ts - secs) * 1e6).round_ties_even() as i64;
    let mut whole = secs as i64;
    if micros >= 1_000_000 {
        whole += 1;
        micros -= 1_000_000;
    }
    let utc = DateTime::from_timestamp(whole, 0)?;
    let local = utc.with_timezone(&ist()).naive_local();
    local.checked_add_signed(Duration::microseconds(micros))
}

/// `naive_local_datetime.timestamp()` with the process zone = IST.
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn local_ts(at: NaiveDateTime) -> f64 {
    let whole = at.and_utc().timestamp() - i64::from(ist().local_minus_utc());
    let micros = at.and_utc().timestamp_subsec_micros();
    whole as f64 + f64::from(micros) / 1e6
}

/// `(a - b).days`: floor division of the timedelta by one day.
#[must_use]
pub fn delta_days(delta: TimeDelta) -> i64 {
    let micros = delta
        .num_microseconds()
        .unwrap_or_else(|| delta.num_seconds() * 1_000_000);
    micros.div_euclid(86_400_000_000)
}

/// `datetime.isoformat()` for a naive value (`.ffffff` only when there are microseconds).
#[must_use]
pub fn iso_naive(at: NaiveDateTime) -> String {
    if at.and_utc().timestamp_subsec_micros() == 0 {
        at.format("%Y-%m-%dT%H:%M:%S").to_string()
    } else {
        at.format("%Y-%m-%dT%H:%M:%S%.6f").to_string()
    }
}

/// `float(x or 0.0)` over a dict value: `None`/missing/falsy → 0; a number as itself; a string as
/// `float(str)` (0 when it does not parse — callers that must refuse check first).
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn pyf(v: Option<&PyValue>) -> f64 {
    match v {
        Some(PyValue::Int(i)) => *i as f64,
        Some(PyValue::Float(f)) => *f,
        Some(PyValue::Bool(true)) => 1.0,
        Some(PyValue::Str(s)) => s.trim().parse::<f64>().unwrap_or(0.0),
        _ => 0.0,
    }
}

/// `int(x or 0)` over a dict value (a float truncates, as `int()` does).
#[must_use]
#[allow(clippy::cast_possible_truncation)]
pub fn pyi(v: Option<&PyValue>) -> i64 {
    match v {
        Some(PyValue::Int(i)) => *i,
        Some(PyValue::Float(f)) if f.is_finite() => f.trunc() as i64,
        Some(PyValue::Bool(true)) => 1,
        Some(PyValue::Str(s)) => s.trim().parse::<i64>().unwrap_or(0),
        _ => 0,
    }
}

/// `str(x or "")` over a dict value.
#[must_use]
pub fn pys(v: Option<&PyValue>) -> String {
    match v {
        Some(value) if value.truthy() => value.py_str(),
        _ => String::new(),
    }
}

/// A column as the Python desk's row hands it to a dict (`dict(row)`).
#[must_use]
pub fn db_to_py(value: &DbValue) -> PyValue {
    match value {
        DbValue::Null => PyValue::None,
        DbValue::Int(i) => PyValue::Int(*i),
        DbValue::Float(f) => PyValue::Float(*f),
        DbValue::Text(s) | DbValue::Json(s) => PyValue::Str(s.clone()),
        DbValue::Bool(b) => PyValue::Bool(*b),
        DbValue::Bytes(b) => PyValue::Bytes(b.clone()),
        DbValue::Aware(t) => PyValue::Str(t.to_rfc3339()),
        DbValue::Naive(t) => PyValue::Str(iso_naive(*t)),
    }
}

/// `round(x)` → `int` (half to even); `None` for NaN/inf (Python raises).
#[must_use]
#[allow(clippy::cast_possible_truncation)]
pub fn round_int(x: f64) -> Option<i64> {
    x.is_finite().then(|| x.round_ties_even() as i64)
}

/// `round(x, n)` on a float.
#[must_use]
pub fn round_to(x: f64, ndigits: usize) -> f64 {
    if !x.is_finite() {
        return x;
    }
    // Rust's `{:.n}` is the correctly rounded decimal of the exact binary value, as Python's
    // `round` is; reading it back gives the nearest double.
    format!("{x:.ndigits$}").parse::<f64>().unwrap_or(x)
}

/// A `PyDict` with one key replaced or appended.
pub fn put(map: &mut PyDict, key: &str, value: PyValue) {
    map.insert(key, value);
}

#[cfg(test)]
#[allow(
    clippy::unwrap_used,
    clippy::expect_used,
    clippy::panic,
    clippy::float_cmp
)]
mod tests {
    use super::*;
    use chrono::NaiveDate;

    #[test]
    fn local_time_round_trips_through_ist() {
        let at = NaiveDate::from_ymd_opt(2025, 7, 1)
            .unwrap()
            .and_hms_opt(9, 15, 0)
            .unwrap();
        let ts = local_ts(at);
        // 09:15 IST is 03:45 UTC.
        assert_eq!(ts, 1_751_341_500.0);
        assert_eq!(local_from_ts(ts), Some(at));
    }

    #[test]
    fn days_floor_like_timedelta() {
        assert_eq!(delta_days(TimeDelta::hours(-1)), -1);
        assert_eq!(delta_days(TimeDelta::hours(47)), 1);
    }
}
