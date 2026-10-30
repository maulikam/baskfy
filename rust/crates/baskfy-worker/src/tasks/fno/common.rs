//! What every module of the lane shares: the task's moment (`_options_now`), Python's decimal
//! rounding and `str()`, the JSON shapes the payloads and `fo_scan.detail` take, a polars cell
//! reader, the Kite session check (`kite_session_usable`) and the provider builders with the
//! harness's seams (the REST root and the token store's clock; DECISIONS-RS R8.3).
//!
//! **Law 2.** The Kite builders carry reads only (`daily_bars`, `option_quotes`); nothing here
//! can reach an order or GTT verb.

use std::sync::Arc;

use baskfy_core::fno::config::{FnoError, PlanState, ScanState, py};
use baskfy_db::OrderedJson;
use baskfy_providers::factory::{KiteFamily, KiteLane, RedisSource, build_kite_family_limiter};
use baskfy_providers::kite::{
    ClientFactory, DEFAULT_ROOT_URI, KiteClient, KiteConnect, KiteProvider, KiteRuntime,
};
use baskfy_providers::nse::NseProvider;
use baskfy_providers::ratelimit::RateLimiter;
use baskfy_providers::secret::Secret;
use baskfy_providers::settings::ProviderSettings;
use baskfy_providers::tokens::{AccessTokenStore, ist};
use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime, Timelike};
use polars::prelude::{AnyValue, Column, DataFrame};
use rust_decimal::{Decimal, RoundingStrategy};

use crate::error::WorkerError;
use crate::tasks::{BoundArgs, TaskContext};

/// `date.isoformat()`.
pub(crate) fn iso(day: NaiveDate) -> String {
    day.format("%Y-%m-%d").to_string()
}

/// `None` or `date.isoformat()`.
pub(crate) fn opt_iso(day: Option<NaiveDate>) -> OrderedJson {
    day.map_or(OrderedJson::Null, |d| OrderedJson::Str(iso(d)))
}

/// `datetime.isoformat()` for an aware moment: microseconds only when non-zero, the offset as
/// `+HH:MM`.
pub(crate) fn iso_dt(at: DateTime<FixedOffset>) -> String {
    let micros = at.nanosecond() / 1_000;
    let base = at.format("%Y-%m-%dT%H:%M:%S").to_string();
    let offset = at.format("%:z").to_string();
    if micros == 0 {
        format!("{base}{offset}")
    } else {
        format!("{base}.{micros:06}{offset}")
    }
}

/// `datetime.isoformat()` for a naive moment.
#[cfg(test)]
pub(crate) fn iso_naive(at: NaiveDateTime) -> String {
    let micros = at.nanosecond() / 1_000;
    let base = at.format("%Y-%m-%dT%H:%M:%S").to_string();
    if micros == 0 {
        base
    } else {
        format!("{base}.{micros:06}")
    }
}

fn invalid_isoformat(text: &str) -> WorkerError {
    WorkerError::python(
        "ValueError",
        format!(
            "Invalid isoformat string: {}",
            baskfy_providers::py::repr_str(text)
        ),
    )
}

/// `datetime.fromisoformat(text)` for the forms a task's `at` takes: a date, a date and time
/// (`T` or a space, minutes, seconds, a fraction) and an optional offset (`Z`, `±HH:MM`).
/// Returns the naive wall clock and the offset when one was written.
pub(crate) fn fromisoformat_datetime(
    text: &str,
) -> Result<(NaiveDateTime, Option<FixedOffset>), WorkerError> {
    let bad = || invalid_isoformat(text);
    if text.len() < 10 || !text.is_char_boundary(10) {
        return Err(bad());
    }
    let day = crate::tasks::args::fromisoformat_date(&text[..10]).map_err(|_| bad())?;
    let rest = &text[10..];
    if rest.is_empty() {
        return Ok((day.and_time(NaiveTime::MIN), None));
    }
    let Some(rest) = rest.strip_prefix(['T', ' ']) else {
        return Err(bad());
    };
    let (clock, offset) = match rest.find(['+', '-', 'Z']) {
        Some(i) => (&rest[..i], Some(&rest[i..])),
        None => (rest, None),
    };
    let time = parse_clock(clock).ok_or_else(bad)?;
    let tz = match offset {
        None => None,
        Some("Z") => Some(FixedOffset::east_opt(0).ok_or_else(bad)?),
        Some(o) => {
            let sign = if o.starts_with('-') { -1 } else { 1 };
            let body = &o[1..];
            let parts: Vec<&str> = body.split(':').collect();
            let (h, m) = match parts.as_slice() {
                [h, m] => (*h, *m),
                [hm] if hm.len() == 4 => (&hm[..2], &hm[2..]),
                _ => return Err(bad()),
            };
            let h: i32 = h.parse().map_err(|_| bad())?;
            let m: i32 = m.parse().map_err(|_| bad())?;
            Some(FixedOffset::east_opt(sign * (h * 3600 + m * 60)).ok_or_else(bad)?)
        }
    };
    Ok((day.and_time(time), tz))
}

fn parse_clock(text: &str) -> Option<NaiveTime> {
    let (hms, frac) = match text.split_once(['.', ',']) {
        Some((a, b)) => (a, Some(b)),
        None => (text, None),
    };
    let parts: Vec<&str> = hms.split(':').collect();
    let num = |s: &str| -> Option<u32> {
        (s.len() == 2 && s.bytes().all(|b| b.is_ascii_digit()))
            .then(|| s.parse().ok())
            .flatten()
    };
    let (h, m, s) = match parts.as_slice() {
        [h] => (num(h)?, 0, 0),
        [h, m] => (num(h)?, num(m)?, 0),
        [h, m, s] => (num(h)?, num(m)?, num(s)?),
        _ => return None,
    };
    let micros = match frac {
        None => 0,
        Some(f) if !f.is_empty() && f.len() <= 6 && f.bytes().all(|b| b.is_ascii_digit()) => {
            let padded = format!("{f:0<6}");
            padded.parse::<u32>().ok()?
        }
        Some(_) => return None,
    };
    NaiveTime::from_hms_micro_opt(h, m, s, micros)
}

/// `_options_now(at)`: the IST moment an F&O task acts for — `at` (ISO) when given (a naive
/// value read as IST, an aware one kept in its own zone), else the task clock in IST.
pub(crate) fn options_now(
    ctx: &TaskContext,
    args: &BoundArgs,
) -> Result<DateTime<FixedOffset>, WorkerError> {
    match args.value("at") {
        None | Some(OrderedJson::Null) => Ok(ctx.now_utc().with_timezone(&ist())),
        Some(OrderedJson::Str(text)) if text.is_empty() => Ok(ctx.now_utc().with_timezone(&ist())),
        Some(OrderedJson::Str(text)) => {
            let (naive, tz) = fromisoformat_datetime(text)?;
            let tz = tz.unwrap_or_else(ist);
            naive
                .and_local_timezone(tz)
                .single()
                .ok_or_else(|| invalid_isoformat(text))
        }
        Some(OrderedJson::Bool(false) | OrderedJson::Int(0)) => {
            Ok(ctx.now_utc().with_timezone(&ist()))
        }
        Some(_) => Err(WorkerError::python(
            "TypeError",
            "fromisoformat: argument must be str",
        )),
    }
}

/// `dt.date.fromisoformat(value) if value else <default>` for a `str | None` date parameter.
pub(crate) fn falsy_date(args: &BoundArgs, name: &str) -> Result<Option<NaiveDate>, WorkerError> {
    match args.value(name) {
        None | Some(OrderedJson::Null) => Ok(None),
        Some(OrderedJson::Str(text)) if text.is_empty() => Ok(None),
        Some(OrderedJson::Str(text)) => crate::tasks::args::fromisoformat_date(text).map(Some),
        Some(OrderedJson::Bool(false) | OrderedJson::Int(0)) => Ok(None),
        Some(_) => Err(WorkerError::python(
            "TypeError",
            "fromisoformat: argument must be str",
        )),
    }
}

/// A [`FnoError`] as the Python exception it ports.
pub(crate) fn fno_err(e: FnoError) -> WorkerError {
    WorkerError::python(e.python_type(), e.message())
}

/// `str(Decimal)`.
pub(crate) fn dstr(d: Decimal) -> String {
    py::str(d)
}

/// `str(d)` or `None`.
pub(crate) fn opt_dstr(d: Option<Decimal>) -> OrderedJson {
    d.map_or(OrderedJson::Null, |v| OrderedJson::Str(dstr(v)))
}

/// `value.quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP)`: the exponent exactly
/// `-places`, as Python's quantize gives it.
pub(crate) fn quantize_half_up(d: Decimal, places: u32) -> Decimal {
    let mut v = d.round_dp_with_strategy(places, RoundingStrategy::MidpointAwayFromZero);
    v.rescale(places);
    v
}

/// `Decimal(repr(f))` — `None` for a value `rust_decimal` cannot hold.
pub(crate) fn decimal_of_float(f: f64) -> Option<Decimal> {
    let text = baskfy_core::py::float_repr(f);
    if text.contains(['e', 'E']) {
        Decimal::from_scientific(&text).ok()
    } else {
        text.parse::<Decimal>().ok()
    }
}

/// `scan._q` for a float: `None` for `None` or a non-finite value, else `Decimal(repr(value))`
/// quantized half-up to `places`.
pub(crate) fn q_float(value: Option<f64>, places: u32) -> Option<Decimal> {
    let v = value.filter(|f| f.is_finite())?;
    decimal_of_float(v).map(|d| quantize_half_up(d, places))
}

/// `scan._q` for a decimal.
pub(crate) fn q_dec(value: Option<Decimal>, places: u32) -> Option<Decimal> {
    value.map(|d| quantize_half_up(d, places))
}

/// `float(Decimal)` — correctly rounded, as Python's.
pub(crate) fn dec_to_f64(d: Decimal) -> f64 {
    d.to_string().parse::<f64>().unwrap_or(f64::NAN)
}

/// A JSON float, or `null`.
pub(crate) fn opt_float(f: Option<f64>) -> OrderedJson {
    f.map_or(OrderedJson::Null, OrderedJson::Float)
}

/// A JSON int, or `null`.
pub(crate) fn opt_int(i: Option<i64>) -> OrderedJson {
    i.map_or(OrderedJson::Null, OrderedJson::Int)
}

/// `ScanState.<X>.value`.
pub(crate) const fn scan_state(state: ScanState) -> &'static str {
    match state {
        ScanState::NotEntryDay => "NOT_ENTRY_DAY",
        ScanState::Candidate => "CANDIDATE",
        ScanState::SkippedEvent => "SKIPPED_EVENT",
        ScanState::Paused => "PAUSED",
        ScanState::OpenPosition => "OPEN_POSITION",
        ScanState::NoData => "NO_DATA",
        ScanState::NoSignal => "NO_SIGNAL",
        ScanState::BlockedBan => "BLOCKED_BAN",
        ScanState::BlockedRegime => "BLOCKED_REGIME",
        ScanState::BlockedCapacity => "BLOCKED_CAPACITY",
    }
}

/// `PlanState.<X>.value`.
pub(crate) const fn plan_state(state: PlanState) -> &'static str {
    match state {
        PlanState::Issued => "ISSUED",
        PlanState::Confirmed => "CONFIRMED",
        PlanState::Filling => "FILLING",
        PlanState::Open => "OPEN",
        PlanState::Exiting => "EXITING",
        PlanState::Closed => "CLOSED",
        PlanState::Lapsed => "LAPSED",
        PlanState::RejectedStructure => "REJECTED_STRUCTURE",
        PlanState::RejectedLiquidity => "REJECTED_LIQUIDITY",
        PlanState::RejectedSize => "REJECTED_SIZE",
        PlanState::RejectedMargin => "REJECTED_MARGIN",
        PlanState::RejectedCost => "REJECTED_COST",
        PlanState::NoSleeveCapital => "NO_SLEEVE_CAPITAL",
        PlanState::RejectedPaused => "REJECTED_PAUSED",
        PlanState::AbandonedPartial => "ABANDONED_PARTIAL",
    }
}

// ---------------------------------------------------------------------------------------------
// Frame cells
// ---------------------------------------------------------------------------------------------

fn cell_err(message: String) -> WorkerError {
    WorkerError::python("ColumnNotFoundError", message)
}

/// One column of `frame`, by name.
pub(crate) fn column<'a>(frame: &'a DataFrame, name: &str) -> Result<&'a Column, WorkerError> {
    frame
        .column(name)
        .map_err(|e| cell_err(format!("{name}: {e}")))
}

fn value_at<'a>(col: &'a Column, row: usize) -> Result<AnyValue<'a>, WorkerError> {
    col.get(row)
        .map_err(|e| WorkerError::python("OutOfBoundsError", e.to_string()))
}

/// A decimal (or float) cell as `Decimal(str(value))`.
pub(crate) fn cell_decimal(col: &Column, row: usize) -> Result<Option<Decimal>, WorkerError> {
    match value_at(col, row)? {
        AnyValue::Null => Ok(None),
        AnyValue::Decimal(mantissa, _, scale) => {
            let scale = u32::try_from(scale)
                .map_err(|_| WorkerError::python("ValueError", "negative decimal scale"))?;
            Decimal::try_from_i128_with_scale(mantissa, scale)
                .map(Some)
                .map_err(|e| WorkerError::python("ValueError", e.to_string()))
        }
        AnyValue::Float64(f) => Ok(decimal_of_float(f)),
        AnyValue::Float32(f) => Ok(decimal_of_float(f64::from(f))),
        AnyValue::Int64(v) => Ok(Some(Decimal::from(v))),
        AnyValue::Int32(v) => Ok(Some(Decimal::from(v))),
        other => Err(WorkerError::python(
            "TypeError",
            format!("expected a decimal cell, got {other:?}"),
        )),
    }
}

/// A string cell.
pub(crate) fn cell_string(col: &Column, row: usize) -> Result<Option<String>, WorkerError> {
    match value_at(col, row)? {
        AnyValue::Null => Ok(None),
        AnyValue::String(s) => Ok(Some(s.to_owned())),
        AnyValue::StringOwned(s) => Ok(Some(s.to_string())),
        other => Err(WorkerError::python(
            "TypeError",
            format!("expected a string cell, got {other:?}"),
        )),
    }
}

/// An integer cell.
pub(crate) fn cell_int(col: &Column, row: usize) -> Result<Option<i64>, WorkerError> {
    match value_at(col, row)? {
        AnyValue::Null => Ok(None),
        AnyValue::Int64(v) => Ok(Some(v)),
        AnyValue::Int32(v) => Ok(Some(i64::from(v))),
        AnyValue::UInt32(v) => Ok(Some(i64::from(v))),
        other => Err(WorkerError::python(
            "TypeError",
            format!("expected an integer cell, got {other:?}"),
        )),
    }
}

/// A date cell.
pub(crate) fn cell_date(col: &Column, row: usize) -> Result<Option<NaiveDate>, WorkerError> {
    match value_at(col, row)? {
        AnyValue::Null => Ok(None),
        AnyValue::Date(days) => Ok(baskfy_core::fno::series::cols::from_days(days)),
        other => Err(WorkerError::python(
            "TypeError",
            format!("expected a date cell, got {other:?}"),
        )),
    }
}

// ---------------------------------------------------------------------------------------------
// Kite and NSE
// ---------------------------------------------------------------------------------------------

/// `broker_oauth.token_store_path()`.
pub(crate) fn token_store_path() -> std::path::PathBuf {
    let read = |name: &str| std::env::var(name).unwrap_or_default().trim().to_owned();
    let broker = read("BASKFY_BROKER_TOKEN_PATH");
    let kite = read("BASKFY_KITE_TOKEN_PATH");
    std::path::PathBuf::from(if !broker.is_empty() {
        broker
    } else if !kite.is_empty() {
        kite
    } else {
        ".secrets/broker-token.enc".to_owned()
    })
}

/// `celery_tasks.kite_session_usable()`: a stored token issued today (IST), read on the task
/// clock; any failure to read is "no session".
pub(crate) fn kite_session_usable(ctx: &TaskContext) -> bool {
    let key = std::env::var("BASKFY_KITE_TOKEN_ENCRYPTION_KEY")
        .unwrap_or_default()
        .trim()
        .to_owned();
    let store = AccessTokenStore::with_clock(token_store_path(), Secret::new(key), ctx.clock());
    if !store.exists() {
        return false;
    }
    store
        .load()
        .is_ok_and(|token| !token.is_expired(ctx.now_utc().with_timezone(&ist())))
}

/// `build_kite_family_provider(get_provider_settings(), family, provider_retry_hooks())` on the
/// interactive lane, with the REST root and the token store's clock the harness injects.
pub(crate) async fn kite_family(
    ctx: &TaskContext,
    family: KiteFamily,
) -> Result<KiteProvider, WorkerError> {
    let settings =
        ProviderSettings::from_env().map_err(|e| WorkerError::Settings(e.to_string()))?;
    let limiter = build_kite_family_limiter(
        &RedisSource::from_settings(&settings),
        &settings,
        family,
        KiteLane::Interactive,
    )
    .await
    .map_err(WorkerError::Provider)?;
    let root = ctx.kite_root().unwrap_or(DEFAULT_ROOT_URI).to_owned();
    let timeout = settings.kite_request_timeout_seconds;
    let factory: ClientFactory = Arc::new(move |api_key: &str| {
        let client: Arc<dyn KiteClient> =
            Arc::new(KiteConnect::with_root(api_key, &root, Some(timeout))?);
        Ok(client)
    });
    let store = AccessTokenStore::with_clock(
        settings.kite_token_path.clone(),
        settings.kite_token_encryption_key.clone(),
        ctx.clock(),
    );
    KiteProvider::new(
        settings,
        KiteRuntime {
            rate_limiter: limiter.map(|l| l as Arc<dyn RateLimiter>),
            client_factory: Some(factory),
            token_store: Some(Arc::new(store)),
            ..KiteRuntime::default()
        },
    )
    .map_err(WorkerError::Provider)
}

/// `build_nse_provider(get_provider_settings(), retry_hooks=provider_retry_hooks())` (the hooks
/// are metrics only and not ported).
pub(crate) async fn nse_provider() -> Result<NseProvider, WorkerError> {
    let settings =
        ProviderSettings::from_env().map_err(|e| WorkerError::Settings(e.to_string()))?;
    let source = RedisSource::from_settings(&settings);
    Ok(baskfy_providers::factory::build_nse_provider(&source, &settings, None, None).await?)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn quantize_half_up_keeps_the_exponent() {
        assert_eq!(quantize_half_up(Decimal::from(5), 2).to_string(), "5.00");
        assert_eq!(
            quantize_half_up("1.005".parse().unwrap(), 2).to_string(),
            "1.01"
        );
        assert_eq!(
            quantize_half_up("-1.005".parse().unwrap(), 2).to_string(),
            "-1.01"
        );
    }

    #[test]
    fn q_float_reads_the_repr() {
        assert_eq!(q_float(Some(0.1), 2).unwrap().to_string(), "0.10");
        assert_eq!(q_float(Some(1e-7), 6).unwrap().to_string(), "0.000000");
        assert_eq!(q_float(Some(f64::INFINITY), 2), None);
        assert_eq!(q_float(None, 2), None);
    }

    #[test]
    fn fromisoformat_reads_pythons_forms() {
        let (n, tz) = fromisoformat_datetime("2026-10-29T15:00:00").unwrap();
        assert_eq!(iso_naive(n), "2026-10-29T15:00:00");
        assert!(tz.is_none());
        let (n, tz) = fromisoformat_datetime("2026-10-29 15:00+05:30").unwrap();
        assert_eq!(iso_naive(n), "2026-10-29T15:00:00");
        assert_eq!(tz.unwrap().local_minus_utc(), 19_800);
        let (n, _) = fromisoformat_datetime("2026-10-29").unwrap();
        assert_eq!(iso_naive(n), "2026-10-29T00:00:00");
        assert!(fromisoformat_datetime("2026-10-29X").is_err());
        assert!(fromisoformat_datetime("nope").is_err());
    }

    #[test]
    fn iso_dt_prints_the_offset() {
        let at = NaiveDate::from_ymd_opt(2026, 10, 29)
            .unwrap()
            .and_hms_opt(15, 0, 0)
            .unwrap()
            .and_local_timezone(ist())
            .unwrap();
        assert_eq!(iso_dt(at), "2026-10-29T15:00:00+05:30");
    }
}
