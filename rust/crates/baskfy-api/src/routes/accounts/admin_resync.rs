//! `GET /admin/resync?days=` — leaf 3.1's dry inspection. Ports `baskfy_api.resync`
//! (`window_for`, `inspect_pending`, `last_completed_resync`) and the router's `_last_outcome`.
//!
//! **Writes nothing.** Four `SELECT`s, one token-file read, and — only when the window holds a
//! weekday the calendar *inferred* to be a holiday — at most twelve "did NSE publish a bhavcopy?"
//! probes through the provider stack, exactly as Python asks them. The bed's parity cases use a
//! window with no inferred holiday, so neither side reaches NSE there.
//!
//! It cannot place an order: nothing here reaches `packages/execution`, a broker order or a GTT.

use std::sync::Arc;

use axum::extract::{Extension, RawQuery, State};
use axum::response::Response;
use baskfy_db::{OrderedJson, row};
use baskfy_providers::ports::ReferenceProvider;
use baskfy_providers::publication::bhavcopy_publication_check;
use baskfy_providers::settings::get_provider_settings;
use baskfy_providers::tokens::AccessTokenStore;
use chrono::{DateTime, Datelike, Duration, FixedOffset, NaiveDate, NaiveTime, Utc};
use rust_decimal::Decimal;
use serde::Serialize;
use sqlx::Row;

use crate::auth::{Principal, require_staff};
use crate::entitlements::utc_now;
use crate::problems::{Problem, internal_error};
use crate::query::{QueryParams, Validation};
use crate::routes::screens::accounts::{db, dbe, int_param};
use crate::state::AppState;
use crate::wire;

/// `DEFAULT_LOOKBACK_DAYS`.
pub const DEFAULT_LOOKBACK_DAYS: i64 = 400;
/// `MAX_LOOKBACK_DAYS`.
pub const MAX_LOOKBACK_DAYS: i64 = 6000;
/// `NEIGHBOURHOOD_DAYS`.
pub const NEIGHBOURHOOD_DAYS: usize = 10;
/// `MIN_MEDIAN_BARS`.
pub const MIN_MEDIAN_BARS: i64 = 100;
/// `MAX_PUBLICATION_PROBES`.
pub const MAX_PUBLICATION_PROBES: usize = 12;
/// `NAMED_IN_NOTE`.
pub const NAMED_IN_NOTE: usize = 5;
/// `RESYNC_COMPLETED_ACTION`.
pub const RESYNC_COMPLETED_ACTION: &str = "data_resync_completed";
/// `models.reference.INFERRED_HOLIDAY_PREFIX`.
pub const INFERRED_HOLIDAY_PREFIX: &str = "inferred:";
/// `seed_data.NSE_EXCHANGE_ID`.
pub const NSE_EXCHANGE_ID: i16 = 1;

/// `ResyncFindingOut`.
#[derive(Debug, Clone, Serialize)]
pub struct ResyncFindingOut {
    /// `kind`.
    pub kind: &'static str,
    /// `trade_date`.
    #[serde(serialize_with = "wire::ser::opt_date")]
    pub trade_date: Option<NaiveDate>,
    /// `summary`.
    pub summary: String,
    /// `remedy`.
    pub remedy: String,
    /// `observed_bars`.
    pub observed_bars: Option<i64>,
    /// `expected_bars`.
    pub expected_bars: Option<i64>,
}

/// `ResyncOutcomeOut`.
#[derive(Debug, Clone, Serialize)]
pub struct ResyncOutcomeOut {
    /// `completed_at`.
    #[serde(serialize_with = "wire::ser::datetime")]
    pub completed_at: DateTime<Utc>,
    /// `actor`.
    pub actor: Option<String>,
    /// `repaired`.
    pub repaired: Vec<String>,
    /// `failed`.
    pub failed: Vec<String>,
    /// `deferred`.
    pub deferred: Vec<String>,
    /// `still_pending`.
    pub still_pending: Vec<String>,
    /// `unresolved`.
    pub unresolved: Vec<String>,
    /// `complete`.
    pub complete: bool,
}

/// `ResyncPlanOut`.
#[derive(Debug, Clone, Serialize)]
pub struct ResyncPlanOut {
    /// `window_start`.
    #[serde(serialize_with = "wire::ser::date")]
    pub window_start: NaiveDate,
    /// `window_end`.
    #[serde(serialize_with = "wire::ser::date")]
    pub window_end: NaiveDate,
    /// `trading_days_checked`.
    pub trading_days_checked: i64,
    /// `pending`.
    pub pending: bool,
    /// `findings`.
    pub findings: Vec<ResyncFindingOut>,
    /// `unresolved`.
    pub unresolved: Vec<String>,
    /// `last_resync`.
    pub last_resync: Option<ResyncOutcomeOut>,
}

fn ist() -> FixedOffset {
    baskfy_providers::tokens::ist()
}

/// `dt.time.fromisoformat(raw)` for the forms a deadline setting takes (`HH`, `HH:MM`,
/// `HH:MM:SS`, with optional fraction); `None` where Python raises.
fn parse_iso_time(raw: &str) -> Option<NaiveTime> {
    let two = |s: &str| -> Option<u32> {
        (s.len() == 2 && s.bytes().all(|b| b.is_ascii_digit()))
            .then(|| s.parse().ok())
            .flatten()
    };
    let parts: Vec<&str> = raw.split(':').collect();
    match parts.as_slice() {
        [h] => NaiveTime::from_hms_opt(two(h)?, 0, 0),
        [h, m] => NaiveTime::from_hms_opt(two(h)?, two(m)?, 0),
        [h, m, s] => {
            let (sec, frac) = s.split_once('.').unwrap_or((s, ""));
            let micros = if frac.is_empty() {
                if s.contains('.') {
                    return None;
                }
                0
            } else {
                if !(frac.len() == 3 || frac.len() == 6)
                    || !frac.bytes().all(|b| b.is_ascii_digit())
                {
                    return None;
                }
                let n: u32 = frac.parse().ok()?;
                if frac.len() == 3 { n * 1000 } else { n }
            };
            NaiveTime::from_hms_micro_opt(two(h)?, two(m)?, two(sec)?, micros)
        }
        _ => None,
    }
}

/// `window_for(settings, days=)`: the inclusive window, ending yesterday until the publish
/// deadline (IST) has passed today.
#[must_use]
pub fn window_for(
    deadline_setting: &str,
    days: i64,
    now: DateTime<FixedOffset>,
) -> (NaiveDate, NaiveDate) {
    let raw = crate::routes::screens::accounts::investments::py_strip(deadline_setting);
    let deadline = parse_iso_time(raw).unwrap_or_else(|| {
        crate::logging::warning(
            "baskfy_api.resync",
            "unparseable publish deadline, using the docs/11 default",
            &[("raw", raw)],
        );
        NaiveTime::from_hms_opt(20, 15, 0).unwrap_or_default()
    });
    let moment = now.with_timezone(&ist());
    let mut end = moment.date_naive();
    if moment.time() < deadline {
        end -= Duration::days(1);
    }
    let span = days.clamp(1, MAX_LOOKBACK_DAYS);
    (end - Duration::days(span), end)
}

/// `_median`: exact, as a Decimal.
fn median(values: &[i64]) -> Decimal {
    let mut ordered = values.to_vec();
    ordered.sort_unstable();
    let middle = ordered.len() / 2;
    if ordered.len() % 2 == 1 {
        Decimal::from(ordered[middle])
    } else {
        (Decimal::from(ordered[middle - 1]) + Decimal::from(ordered[middle])) / Decimal::TWO
    }
}

/// `_neighbourhood_median`: the median of the nearest sessions that have bars.
fn neighbourhood_median(
    days: &[NaiveDate],
    counts: &std::collections::HashMap<NaiveDate, i64>,
    index: usize,
) -> Option<Decimal> {
    let mut sample: Vec<i64> = Vec::new();
    let n = days.len();
    for offset in 1..n {
        let candidates = [index.checked_sub(offset), index.checked_add(offset)];
        for neighbour in candidates.into_iter().flatten() {
            if neighbour < n && neighbour != index {
                let count = counts.get(&days[neighbour]).copied().unwrap_or(0);
                if count > 0 {
                    sample.push(count);
                }
            }
        }
        if sample.len() >= NEIGHBOURHOOD_DAYS {
            break;
        }
    }
    if sample.is_empty() {
        return None;
    }
    sample.truncate(NEIGHBOURHOOD_DAYS);
    Some(median(&sample))
}

/// `f"{n:,}"` for an integer.
fn thousands(n: i64) -> String {
    let digits = n.unsigned_abs().to_string();
    let mut out = String::new();
    for (i, c) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(c);
    }
    if n < 0 { format!("-{out}") } else { out }
}

/// `int(Decimal)`: truncation toward zero.
fn decimal_int(d: Decimal) -> i64 {
    i64::try_from(d.trunc().mantissa() / 10_i128.pow(d.trunc().scale())).unwrap_or(i64::MAX)
}

struct Finding {
    kind: &'static str,
    trade_date: Option<NaiveDate>,
    summary: String,
    remedy: String,
    observed_bars: Option<i64>,
    expected_bars: Option<i64>,
}

/// `_kite_session_finding(now)`: arithmetic over the stored issue date; never a call to Kite.
fn kite_session_finding(
    now: DateTime<FixedOffset>,
) -> Result<(Option<Finding>, Option<String>), Problem> {
    let settings = get_provider_settings().map_err(|e| {
        crate::logging::error(
            "baskfy_api.app",
            "unhandled error",
            &[("error", &e.to_string())],
        );
        internal_error()
    })?;
    if settings.kite_token_path.is_empty() || settings.kite_token_encryption_key.expose().is_empty()
    {
        return Ok((
            None,
            Some(
                "No Kite token store is configured on this host, so the broker session could \
                 not be checked. History before 2024 and holdings sync need one; the daily \
                 bhavcopy does not."
                    .to_owned(),
            ),
        ));
    }
    let store = AccessTokenStore::new(
        settings.kite_token_path.clone(),
        settings.kite_token_encryption_key.clone(),
    );
    let remedy = "python -m baskfy_worker.kite_session_cli pull (over SSH, from the desk)";
    let finding = |summary: String| Finding {
        kind: "kite_session",
        trade_date: None,
        summary,
        remedy: remedy.to_owned(),
        observed_bars: None,
        expected_bars: None,
    };
    if !store.exists() {
        return Ok((
            Some(finding(format!(
                "No Kite access token is stored at {}.",
                store.path().display()
            ))),
            None,
        ));
    }
    match store.load() {
        Err(e) => Ok((
            Some(finding(format!(
                "The stored Kite session could not be read: {e}"
            ))),
            None,
        )),
        Ok(record) if record.is_expired(now) => Ok((
            Some(finding(format!(
                "The stored Kite session was issued on {} and has expired — a Kite access token \
                 dies at the next pre-open.",
                wire::py_date(record.issued_on())
            ))),
            None,
        )),
        Ok(_) => Ok((None, None)),
    }
}

/// `_wrong_holiday_findings(calendar, publication)`.
async fn wrong_holiday_findings(
    calendar: &[(NaiveDate, bool, Option<String>)],
) -> Result<(Vec<Finding>, Vec<String>), Problem> {
    let candidates: Vec<NaiveDate> = calendar
        .iter()
        .filter(|(day, is_open, name)| {
            !is_open
                && day.weekday().num_days_from_monday() < 5
                && name
                    .as_deref()
                    .unwrap_or("")
                    .starts_with(INFERRED_HOLIDAY_PREFIX)
        })
        .map(|(day, _, _)| *day)
        .collect();
    if candidates.is_empty() {
        return Ok((Vec::new(), Vec::new()));
    }
    // `admin.publication_source()`: the provider stack, built only now that it is needed.
    let stack = super::providers::production_stack().await?;
    let reference: Arc<dyn ReferenceProvider> = stack;
    let Some(published) = bhavcopy_publication_check(Some(reference)) else {
        let named: Vec<String> = candidates
            .iter()
            .take(NAMED_IN_NOTE)
            .map(|d| wire::py_date(*d))
            .collect();
        let more = if candidates.len() > NAMED_IN_NOTE {
            ", …"
        } else {
            ""
        };
        return Ok((
            Vec::new(),
            vec![format!(
                "{} weekday(s) in this window are recorded as inferred holidays ({}{more}), but \
                 no provider on this host can ask NSE whether it published a bhavcopy for them, \
                 so they were neither cleared nor flagged.",
                candidates.len(),
                named.join(", ")
            )],
        ));
    };
    let mut findings = Vec::new();
    let mut unresolved = Vec::new();
    for day in candidates.iter().take(MAX_PUBLICATION_PROBES) {
        if published(*day).await {
            findings.push(Finding {
                kind: "wrong_holiday",
                trade_date: Some(*day),
                summary: format!(
                    "{} is a weekday recorded as a holiday by inference, but NSE published a \
                     bhavcopy for it — the exchange traded and the ingest failed. While it stays \
                     marked shut, every backfill skips it.",
                    wire::py_date(*day)
                ),
                remedy: "correct trading_day, then re-ingest the bhavcopy for the date".to_owned(),
                observed_bars: None,
                expected_bars: None,
            });
        }
    }
    if candidates.len() > MAX_PUBLICATION_PROBES {
        let deferred = &candidates[MAX_PUBLICATION_PROBES..];
        unresolved.push(format!(
            "{} further inferred holiday(s) were not checked against NSE this pass (cap is \
             {MAX_PUBLICATION_PROBES} probes per inspection); the earliest is {}. Run the \
             inspection again after this one is repaired.",
            deferred.len(),
            wire::py_date(deferred[0])
        ));
    }
    Ok((findings, unresolved))
}

/// Python's `str(value)` for a JSON value read back out of `admin_action.detail`.
fn py_str(value: &OrderedJson) -> String {
    match value {
        OrderedJson::Str(s) => s.clone(),
        other => py_repr(other),
    }
}

fn py_repr(value: &OrderedJson) -> String {
    match value {
        OrderedJson::Null => "None".to_owned(),
        OrderedJson::Bool(true) => "True".to_owned(),
        OrderedJson::Bool(false) => "False".to_owned(),
        OrderedJson::Int(i) => i.to_string(),
        OrderedJson::UInt(u) => u.to_string(),
        OrderedJson::BigInt(big) => big.clone(),
        OrderedJson::Float(f) => baskfy_core::py::float_repr(*f),
        OrderedJson::Str(s) => baskfy_providers::py::repr_str(s),
        OrderedJson::Array(items) => {
            format!(
                "[{}]",
                items.iter().map(py_repr).collect::<Vec<_>>().join(", ")
            )
        }
        OrderedJson::Object(entries) => format!(
            "{{{}}}",
            entries
                .iter()
                .map(|(k, v)| format!("{}: {}", baskfy_providers::py::repr_str(k), py_repr(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

/// `_last_outcome(session)`: the previous repair's own report from the audit trail.
async fn last_outcome(state: &AppState) -> Result<Option<ResyncOutcomeOut>, Problem> {
    let found = sqlx::query(
        "SELECT actor_user_id, detail, created_at FROM admin_action WHERE action = $1 \
         ORDER BY created_at DESC, id DESC LIMIT 1",
    )
    .bind(RESYNC_COMPLETED_ACTION)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    let Some(r) = found else {
        return Ok(None);
    };
    let detail = row::opt_jsonb(&r, "detail").map_err(|e| dbe(&e))?;
    let detail = match detail {
        None | Some(OrderedJson::Null) => OrderedJson::Object(Vec::new()),
        Some(d @ OrderedJson::Object(_)) => d,
        // `row.detail or {}` then `.get`: an empty list/string/0 is falsy ({}), anything else
        // non-dict raises AttributeError out of the handler.
        Some(OrderedJson::Array(a)) if a.is_empty() => OrderedJson::Object(Vec::new()),
        Some(OrderedJson::Str(s)) if s.is_empty() => OrderedJson::Object(Vec::new()),
        Some(OrderedJson::Bool(false) | OrderedJson::Int(0)) => OrderedJson::Object(Vec::new()),
        Some(_) => return Err(internal_error()),
    };
    let actor_id: i64 = r.try_get("actor_user_id").map_err(db)?;
    let actor: Option<String> =
        sqlx::query_scalar("SELECT email::text FROM app_user WHERE id = $1")
            .bind(actor_id)
            .fetch_optional(state.db.pool())
            .await
            .map_err(db)?;
    let strings = |key: &str| match detail.get(key) {
        Some(OrderedJson::Array(items)) => items.iter().map(py_str).collect(),
        _ => Vec::new(),
    };
    Ok(Some(ResyncOutcomeOut {
        completed_at: row::timestamptz(&r, "created_at").map_err(|e| dbe(&e))?,
        actor,
        repaired: strings("repaired"),
        failed: strings("failed"),
        deferred: strings("deferred"),
        still_pending: strings("still_pending"),
        unresolved: strings("unresolved"),
        complete: matches!(detail.get("complete"), Some(OrderedJson::Bool(true))),
    }))
}

/// `GET /admin/resync?days=` (`inspect_resync`).
///
/// # Errors
/// The staff gate; 400 for a bad `days`; 500.
pub async fn inspect_resync(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
    RawQuery(raw): RawQuery,
) -> Result<Response, Problem> {
    require_staff(&principal)?;
    let days = days_param(raw.as_deref())?;
    Ok(wire::json(&build_plan(&state, days).await?))
}

/// The `days` query parameter of `GET`/`POST /admin/resync`: `ge=1, le=MAX_LOOKBACK_DAYS`,
/// default `DEFAULT_LOOKBACK_DAYS`.
///
/// # Errors
/// FastAPI's 400 for a bad value.
pub(crate) fn days_param(raw: Option<&str>) -> Result<i64, Problem> {
    let q = QueryParams::parse(raw);
    let mut v = Validation::new();
    let days = int_param(
        &mut v,
        "days",
        q.get("days"),
        Some(1),
        Some(MAX_LOOKBACK_DAYS),
    );
    v.finish()?;
    Ok(days
        .and_then(|n| i64::try_from(n).ok())
        .unwrap_or(DEFAULT_LOOKBACK_DAYS))
}

/// `inspect_pending(session, settings=, publication=, days=)` plus the last outcome: the plan
/// both the inspection and the acting half (`POST /admin/resync`) are built from.
///
/// # Errors
/// 500.
pub(crate) async fn build_plan(state: &AppState, days: i64) -> Result<ResyncPlanOut, Problem> {
    let now = utc_now().with_timezone(&ist());
    let (start, end) = window_for(&state.settings.publish_deadline_ist, days, now);
    let pool = state.db.pool();

    let rows = sqlx::query(
        "SELECT date, is_trading_day, holiday_name FROM trading_day WHERE exchange_id = $1 \
         AND date >= $2 AND date <= $3 ORDER BY date",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(start)
    .bind(end)
    .fetch_all(pool)
    .await
    .map_err(db)?;
    let mut calendar = Vec::with_capacity(rows.len());
    for r in &rows {
        calendar.push((
            row::date(r, "date").map_err(|e| dbe(&e))?,
            r.try_get::<bool, _>("is_trading_day").map_err(db)?,
            r.try_get::<Option<String>, _>("holiday_name").map_err(db)?,
        ));
    }
    let open_days: Vec<NaiveDate> = calendar.iter().filter(|c| c.1).map(|c| c.0).collect();
    let counts: std::collections::HashMap<NaiveDate, i64> = sqlx::query(
        "SELECT date, count(*) AS n FROM ohlcv_daily WHERE date >= $1 AND date <= $2 GROUP BY date",
    )
    .bind(start)
    .bind(end)
    .fetch_all(pool)
    .await
    .map_err(db)?
    .iter()
    .map(|r| {
        Ok((
            row::date(r, "date").map_err(|e| dbe(&e))?,
            r.try_get::<i64, _>("n").map_err(db)?,
        ))
    })
    .collect::<Result<_, Problem>>()?;
    let published_runs: std::collections::HashSet<NaiveDate> = sqlx::query(
        "SELECT DISTINCT trade_date FROM pipeline_run WHERE trade_date >= $1 AND trade_date <= $2 \
         AND status = 'succeeded' AND data_version IS NOT NULL",
    )
    .bind(start)
    .bind(end)
    .fetch_all(pool)
    .await
    .map_err(db)?
    .iter()
    .map(|r| row::date(r, "trade_date").map_err(|e| dbe(&e)))
    .collect::<Result<_, Problem>>()?;
    let history_start: Option<NaiveDate> =
        sqlx::query_scalar("SELECT min(trade_date) FROM pipeline_run")
            .fetch_one(pool)
            .await
            .map_err(db)?;

    let mut findings: Vec<Finding> = Vec::new();
    let mut unresolved: Vec<String> = Vec::new();
    if history_start.is_none() && !open_days.is_empty() {
        unresolved.push(
            "The nightly pipeline has never opened a run on this deployment, so no trading day \
             could be checked for a published snapshot. Bar coverage was still checked."
                .to_owned(),
        );
    }
    let thin_fraction = Decimal::new(60, 2);
    for (index, day) in open_days.iter().enumerate() {
        let observed = counts.get(day).copied().unwrap_or(0);
        if let Some(h) = history_start
            && *day >= h
            && !published_runs.contains(day)
        {
            findings.push(Finding {
                kind: "missing_run",
                trade_date: Some(*day),
                summary: format!(
                    "{} was a trading session with no pipeline run that published — nothing \
                     bumped data_version for it.",
                    wire::py_date(*day)
                ),
                remedy: "re-run the nightly chain for the date".to_owned(),
                observed_bars: Some(observed),
                expected_bars: None,
            });
        }
        let Some(med) = neighbourhood_median(&open_days, &counts, index) else {
            continue;
        };
        if med < Decimal::from(MIN_MEDIAN_BARS) || Decimal::from(observed) >= med * thin_fraction {
            continue;
        }
        findings.push(Finding {
            kind: "thin_bars",
            trade_date: Some(*day),
            summary: format!(
                "{} holds {} bars against a neighbouring median of {} — below 60% of it. A day \
                 this thin corrupts every 9M and 12M factor window that crosses it.",
                wire::py_date(*day),
                thousands(observed),
                thousands(decimal_int(med))
            ),
            remedy: "re-ingest the NSE bhavcopy for the date".to_owned(),
            observed_bars: Some(observed),
            expected_bars: Some(decimal_int(med)),
        });
    }
    let (holiday_findings, holiday_unresolved) = wrong_holiday_findings(&calendar).await?;
    findings.extend(holiday_findings);
    unresolved.extend(holiday_unresolved);
    let (kite_finding, kite_unresolved) = kite_session_finding(now)?;
    findings.extend(kite_finding);
    unresolved.extend(kite_unresolved);
    findings.sort_by(|a, b| {
        (a.trade_date.unwrap_or(NaiveDate::MAX), a.kind)
            .cmp(&(b.trade_date.unwrap_or(NaiveDate::MAX), b.kind))
    });
    let last_resync = last_outcome(state).await?;
    let findings: Vec<ResyncFindingOut> = findings
        .into_iter()
        .map(|f| ResyncFindingOut {
            kind: f.kind,
            trade_date: f.trade_date,
            summary: f.summary,
            remedy: f.remedy,
            observed_bars: f.observed_bars,
            expected_bars: f.expected_bars,
        })
        .collect();
    Ok(ResyncPlanOut {
        window_start: start,
        window_end: end,
        trading_days_checked: i64::try_from(open_days.len()).unwrap_or(i64::MAX),
        pending: !findings.is_empty(),
        findings,
        unresolved,
        last_resync,
    })
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn the_window_stops_before_today_until_the_deadline() {
        let at = |s: &str| DateTime::parse_from_rfc3339(s).unwrap();
        // 19:00 IST: before 20:15, so the window ends yesterday.
        let (start, end) = window_for("20:15", 30, at("2026-10-30T19:00:00+05:30"));
        assert_eq!(end, NaiveDate::from_ymd_opt(2026, 10, 29).unwrap());
        assert_eq!(start, NaiveDate::from_ymd_opt(2026, 9, 29).unwrap());
        let (_, end) = window_for(" 20:15 ", 30, at("2026-10-30T20:15:00+05:30"));
        assert_eq!(end, NaiveDate::from_ymd_opt(2026, 10, 30).unwrap());
        // A malformed setting falls back to 20:15.
        let (_, end) = window_for("8pm", 0, at("2026-10-30T20:00:00+05:30"));
        assert_eq!(end, NaiveDate::from_ymd_opt(2026, 10, 29).unwrap());
    }

    #[test]
    fn medians_and_numbers_are_pythons() {
        assert_eq!(median(&[3, 1, 2]), Decimal::from(2));
        assert_eq!(
            median(&[4, 1, 2, 3]),
            Decimal::new(25, 1),
            "compared as a number, as Python does"
        );
        assert_eq!(thousands(2310), "2,310");
        assert_eq!(thousands(322), "322");
        assert_eq!(thousands(1_234_567), "1,234,567");
        assert_eq!(decimal_int(Decimal::new(23055, 1)), 2305);
    }
}
