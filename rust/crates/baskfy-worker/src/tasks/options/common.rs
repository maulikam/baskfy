//! What every module of the lane shares: the task's moment (`celery_tasks._options_now`),
//! Python's `isoformat`, decimal rounding and `str()`, the Kite session check
//! (`celery_tasks.kite_session_usable`), the NSE calendar read (`swing_health.is_session_day`)
//! and the provider builders (`options.reads.build_options_kite`, `build_kite_provider`) with the
//! harness's seams — the REST root and the token store's clock (DECISIONS-RS R8.3).
//!
//! The `fno` lane carries its own copy of the moment parser and the session check; a lane does
//! not reach into another's private module (R8.1), so these are this lane's.
//!
//! **Law 2.** The Kite builders carry reads only (`option_quotes`, `minute_bars`,
//! `fno_option_contracts`, the margin *calculator*); nothing here can reach an order or GTT verb.

use std::sync::Arc;

use baskfy_db::OrderedJson;
use baskfy_providers::factory::{
    KiteFamily, KiteLane, KiteReadLimiter, RedisSource, build_kite_family_limiter,
    build_kite_read_limiter,
};
use baskfy_providers::kite::{
    ClientFactory, DEFAULT_ROOT_URI, KiteClient, KiteConnect, KiteProvider, KiteRuntime,
};
use baskfy_providers::ratelimit::RateLimiter;
use baskfy_providers::secret::Secret;
use baskfy_providers::settings::ProviderSettings;
use baskfy_providers::tokens::{AccessTokenStore, ist};
use chrono::{DateTime, FixedOffset, NaiveDate, NaiveDateTime, NaiveTime, Timelike};
use rust_decimal::{Decimal, RoundingStrategy};
use sqlx::PgConnection;

use crate::calendar::NSE_EXCHANGE_ID;
use crate::error::WorkerError;
use crate::tasks::{BoundArgs, TaskContext};

/// `date.isoformat()`.
pub(crate) fn iso(day: NaiveDate) -> String {
    day.format("%Y-%m-%d").to_string()
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

/// `moment.astimezone(IST)`.
pub(crate) fn in_ist(at: DateTime<FixedOffset>) -> DateTime<FixedOffset> {
    at.with_timezone(&ist())
}

/// `dt.datetime.combine(day, clock, tzinfo=IST)`.
pub(crate) fn ist_at(
    day: NaiveDate,
    clock: NaiveTime,
) -> Result<DateTime<FixedOffset>, WorkerError> {
    day.and_time(clock)
        .and_local_timezone(ist())
        .single()
        .ok_or_else(|| WorkerError::python("ValueError", "an IST moment that does not exist"))
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
fn fromisoformat_datetime(text: &str) -> Result<(NaiveDateTime, Option<FixedOffset>), WorkerError> {
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
            let body = o.get(1..).ok_or_else(bad)?;
            let parts: Vec<&str> = body.split(':').collect();
            let (h, m) = match parts.as_slice() {
                [h, m] => (*h, *m),
                [hm] if hm.len() == 4 => {
                    (hm.get(..2).ok_or_else(bad)?, hm.get(2..).ok_or_else(bad)?)
                }
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
            format!("{f:0<6}").parse::<u32>().ok()?
        }
        Some(_) => return None,
    };
    NaiveTime::from_hms_micro_opt(h, m, s, micros)
}

/// The task clock in IST, aware (`dt.datetime.now(tz=IST)`).
pub(crate) fn now_ist(ctx: &TaskContext) -> DateTime<FixedOffset> {
    ctx.now_utc().with_timezone(&ist())
}

/// `_options_now(at)`: the IST moment an options task acts for — `at` (ISO) when given (a naive
/// value read as IST, an aware one kept in its own zone), else the task clock in IST. Python's
/// `if at:` reads any falsy value as "not given".
pub(crate) fn options_now(
    ctx: &TaskContext,
    args: &BoundArgs,
) -> Result<DateTime<FixedOffset>, WorkerError> {
    match args.value("at") {
        None | Some(OrderedJson::Null | OrderedJson::Bool(false) | OrderedJson::Int(0)) => {
            Ok(now_ist(ctx))
        }
        Some(OrderedJson::Str(text)) if text.is_empty() => Ok(now_ist(ctx)),
        Some(OrderedJson::Str(text)) => {
            let (naive, tz) = fromisoformat_datetime(text)?;
            let tz = tz.unwrap_or_else(ist);
            naive
                .and_local_timezone(tz)
                .single()
                .ok_or_else(|| invalid_isoformat(text))
        }
        Some(_) => Err(WorkerError::python(
            "TypeError",
            "fromisoformat: argument must be str",
        )),
    }
}

/// `dt.date.fromisoformat(value) if value else <None>` for a `str | None` date parameter.
pub(crate) fn falsy_date(args: &BoundArgs, name: &str) -> Result<Option<NaiveDate>, WorkerError> {
    match args.value(name) {
        None | Some(OrderedJson::Null | OrderedJson::Bool(false) | OrderedJson::Int(0)) => Ok(None),
        Some(OrderedJson::Str(text)) if text.is_empty() => Ok(None),
        Some(OrderedJson::Str(text)) => crate::tasks::args::fromisoformat_date(text).map(Some),
        Some(_) => Err(WorkerError::python(
            "TypeError",
            "fromisoformat: argument must be str",
        )),
    }
}

/// `dt.date.fromisoformat(value)` for a required `str` date parameter (no truthiness test).
pub(crate) fn required_date(args: &BoundArgs, name: &str) -> Result<NaiveDate, WorkerError> {
    match args.value(name) {
        Some(OrderedJson::Str(text)) => crate::tasks::args::fromisoformat_date(text),
        _ => Err(WorkerError::python(
            "TypeError",
            "fromisoformat: argument must be str",
        )),
    }
}

/// `value.quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP)`: the exponent exactly
/// `-places`, as Python's quantize gives it.
pub(crate) fn quantize_half_up(d: Decimal, places: u32) -> Decimal {
    let mut v = d.round_dp_with_strategy(places, RoundingStrategy::MidpointAwayFromZero);
    v.rescale(places);
    v
}

/// `str(Decimal)`.
pub(crate) fn dstr(d: Decimal) -> String {
    baskfy_core::fno::config::py::str(d)
}

/// `round(seconds, 3)` as a JSON float.
pub(crate) fn seconds_json(seconds: f64) -> OrderedJson {
    OrderedJson::Float((seconds * 1000.0).round() / 1000.0)
}

/// A count as a JSON int.
pub(crate) fn count(n: usize) -> OrderedJson {
    OrderedJson::Int(i64::try_from(n).unwrap_or(i64::MAX))
}

// ---------------------------------------------------------------------------------------------
// The NSE calendar and the Kite session
// ---------------------------------------------------------------------------------------------

/// `swing_health.is_session_day`: a day the calendar does not carry is a session; a named
/// holiday is not.
pub(crate) async fn is_session_day(
    conn: &mut PgConnection,
    day: NaiveDate,
) -> Result<bool, WorkerError> {
    let row: Option<(bool,)> = sqlx::query_as(
        "SELECT trading_day.is_trading_day FROM trading_day \
         WHERE trading_day.exchange_id = $1 AND trading_day.date = $2",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(day)
    .fetch_optional(&mut *conn)
    .await?;
    Ok(row.is_none_or(|(known,)| known))
}

/// `broker_oauth.token_store_path()`.
fn token_store_path() -> std::path::PathBuf {
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
        .is_ok_and(|token| !token.is_expired(now_ist(ctx)))
}

fn provider_settings() -> Result<ProviderSettings, WorkerError> {
    ProviderSettings::from_env().map_err(|e| WorkerError::Settings(e.to_string()))
}

fn runtime_parts(
    ctx: &TaskContext,
    settings: &ProviderSettings,
) -> (ClientFactory, Arc<AccessTokenStore>) {
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
    (factory, Arc::new(store))
}

/// `build_kite_family_provider(get_provider_settings(), family, retry_hooks, lane=lane)` — one
/// of `build_options_kite`'s three adapters, on the box's shared clocks for `family`.
pub(crate) async fn family_kite(
    ctx: &TaskContext,
    family: KiteFamily,
    lane: KiteLane,
) -> Result<KiteProvider, WorkerError> {
    let settings = provider_settings()?;
    let limiter = build_kite_family_limiter(
        &RedisSource::from_settings(&settings),
        &settings,
        family,
        lane,
    )
    .await
    .map_err(WorkerError::Provider)?;
    let (factory, store) = runtime_parts(ctx, &settings);
    KiteProvider::new(
        settings,
        KiteRuntime {
            rate_limiter: limiter.map(|l| l as Arc<dyn RateLimiter>),
            client_factory: Some(factory),
            token_store: Some(store),
            ..KiteRuntime::default()
        },
    )
    .map_err(WorkerError::Provider)
}

/// `build_kite_provider(get_provider_settings(), provider_retry_hooks(), lane=lane)` — the
/// plain read adapter `options.refresh_master` reads the NFO dump through.
pub(crate) async fn plain_kite(
    ctx: &TaskContext,
    lane: KiteLane,
) -> Result<KiteProvider, WorkerError> {
    let settings = provider_settings()?;
    let limiter = build_kite_read_limiter(&RedisSource::from_settings(&settings), &settings, lane)
        .await
        .map_err(WorkerError::Provider)?;
    let (factory, store) = runtime_parts(ctx, &settings);
    KiteProvider::new(
        settings,
        KiteRuntime {
            rate_limiter: limiter.map(KiteReadLimiter::into_limiter),
            client_factory: Some(factory),
            token_store: Some(store),
            ..KiteRuntime::default()
        },
    )
    .map_err(WorkerError::Provider)
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
            quantize_half_up("-0.0000005".parse().unwrap(), 6).to_string(),
            "-0.000001"
        );
    }

    #[test]
    fn fromisoformat_reads_pythons_forms() {
        let (n, tz) = fromisoformat_datetime("2026-10-29T13:15:20").unwrap();
        assert_eq!(n.format("%H:%M:%S").to_string(), "13:15:20");
        assert!(tz.is_none());
        let (_, tz) = fromisoformat_datetime("2026-10-29 13:15+05:30").unwrap();
        assert_eq!(tz.unwrap().local_minus_utc(), 19_800);
        assert!(fromisoformat_datetime("2026-10-29X").is_err());
        assert!(fromisoformat_datetime("nope").is_err());
    }

    #[test]
    fn iso_dt_prints_the_offset() {
        let at = ist_at(
            NaiveDate::from_ymd_opt(2026, 10, 29).unwrap(),
            NaiveTime::from_hms_opt(13, 15, 0).unwrap(),
        )
        .unwrap();
        assert_eq!(iso_dt(at), "2026-10-29T13:15:00+05:30");
    }
}
