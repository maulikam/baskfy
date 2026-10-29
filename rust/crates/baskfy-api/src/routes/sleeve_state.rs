//! ``GET /sleeves/state`` — what each of the three books is doing right now, and why (LV4).
//!
//! Ports `baskfy_api.routers.sleeve_state` and the service `baskfy_api.sleeve_state`. Python
//! declares `APIRouter(prefix="/sleeves", tags=["sleeves"])`.
//!
//! Read-only: the answer is derived from stored rows (the calendar, each sleeve's newest scan run
//! and plan, the desk processes' heartbeats, open protection issues, the swing watchlist), the
//! local Kite token blob (no network call) and the clock. [`derive_state`] is pure. Nothing here
//! queues, scans, plans or places — **no route may place, modify or cancel an order** (law 2).

use axum::Router;
use axum::extract::{Extension, State};
use axum::response::Response;
use axum::routing::get;
use chrono::{DateTime, Datelike, FixedOffset, NaiveDate, NaiveTime, Timelike, Utc};
use sqlx::postgres::PgRow;

use crate::auth::{Principal, require_authenticated};
use crate::problems::Problem;
use crate::routes::meta::NSE_EXCHANGE_ID;
use crate::routes::sleeves::common::{
    J, Obj, as_dict, db, get as col, ist, json_int, now_utc, ojson, pydantic_response,
    scoped_sole_user_id,
};
use crate::state::AppState;

/// `SLEEVES`.
pub const SLEEVES: [&str; 3] = ["swing", "twt", "vbt"];

/// `HEARTBEAT_FRESH_SECONDS`.
pub const HEARTBEAT_FRESH_SECONDS: i64 = 120;

fn market_open_time() -> NaiveTime {
    NaiveTime::from_hms_opt(9, 15, 0).unwrap_or_default()
}

fn market_close_time() -> NaiveTime {
    NaiveTime::from_hms_opt(15, 30, 0).unwrap_or_default()
}

fn twt_entry_window_end() -> NaiveTime {
    NaiveTime::from_hms_opt(9, 35, 0).unwrap_or_default()
}

/// `SCAN_MEANS[sleeve]`.
#[must_use]
pub fn scan_means(sleeve: &str) -> &'static str {
    match sleeve {
        "swing" => "Scan reads today so far from Kite quotes (provisional bars) during the session",
        _ => {
            "Scan reads today so far from Kite quotes (provisional bars) during the session and \
             proposes buys now, at market; outside it, the last published session"
        }
    }
}

/// `SleeveFacts`: everything [`derive_state`] reads.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct SleeveFacts {
    /// `sleeve`.
    pub sleeve: String,
    /// `session_day`.
    pub session_day: bool,
    /// `market_open`.
    pub market_open: bool,
    /// `broker_session`.
    pub broker_session: bool,
    /// `scan_status`.
    pub scan_status: Option<String>,
    /// `scan_found`: the count and its Python `str()` (a stored `true` prints `True`).
    pub scan_found: Option<(i64, String)>,
    /// `scan_finished_at`.
    pub scan_finished_at: Option<DateTime<Utc>>,
    /// `scan_provisional`.
    pub scan_provisional: bool,
    /// `plan_source`.
    pub plan_source: Option<String>,
    /// `plan_session_date`.
    pub plan_session_date: Option<NaiveDate>,
    /// `plan_expires_at`.
    pub plan_expires_at: Option<DateTime<Utc>>,
    /// `heartbeat_at`.
    pub heartbeat_at: Option<DateTime<Utc>>,
    /// `open_issues`.
    pub open_issues: i64,
    /// `issue_kinds`, sorted.
    pub issue_kinds: Vec<String>,
    /// `watching`.
    pub watching: i64,
}

/// `SleeveStateOut`.
#[derive(Debug, Clone, PartialEq)]
pub struct SleeveStateOut {
    /// `sleeve`.
    pub sleeve: String,
    /// `state`.
    pub state: &'static str,
    /// `reason`.
    pub reason: String,
    /// `scan_means`.
    pub scan_means: &'static str,
    /// `as_of`.
    pub as_of: Option<NaiveDate>,
    /// `next`.
    pub next: String,
    /// `updated_at`.
    pub updated_at: DateTime<FixedOffset>,
}

impl SleeveStateOut {
    fn to_j(&self) -> J {
        Obj::new()
            .put("sleeve", J::s(&self.sleeve))
            .put("state", J::s(self.state))
            .put("reason", J::s(&self.reason))
            .put("scan_means", J::s(self.scan_means))
            .put("as_of", J::opt_date(self.as_of))
            .put("next", J::s(&self.next))
            .put("updated_at", J::DtFixed(self.updated_at))
            .done()
    }
}

fn fresh(stamp: Option<DateTime<Utc>>, now: DateTime<FixedOffset>) -> bool {
    stamp.is_some_and(|s| {
        let delta = now.with_timezone(&Utc) - s;
        // `total_seconds() <= 120`, microseconds included.
        delta.num_microseconds().unwrap_or(i64::MAX) <= HEARTBEAT_FRESH_SECONDS * 1_000_000
    })
}

fn hhmm(t: DateTime<Utc>) -> String {
    let local = t.with_timezone(&ist());
    format!("{:02}:{:02}", local.hour(), local.minute())
}

/// Ports `derive_state(facts, now)`: one state and one reason, in precedence order (blocked >
/// closed > waiting_for_login > scanning > monitoring > plan_ready > missed_window >
/// signal_ready > idle).
#[must_use]
pub fn derive_state(facts: &SleeveFacts, now: DateTime<FixedOffset>) -> SleeveStateOut {
    let local = now.with_timezone(&ist());
    let today = local.date_naive();
    let clock = local.time();
    let sleeve = facts.sleeve.as_str();
    let out = |state: &'static str, reason: String, next: &str| SleeveStateOut {
        sleeve: sleeve.to_owned(),
        state,
        reason,
        scan_means: scan_means(sleeve),
        as_of: facts.plan_session_date,
        next: next.to_owned(),
        updated_at: now,
    };

    if facts.open_issues != 0 {
        let joined = facts.issue_kinds.join(", ");
        let kinds = if joined.is_empty() {
            "protection".to_owned()
        } else {
            joined
        };
        return out(
            "blocked",
            format!(
                "{} open protection issue(s): {kinds}. Every {sleeve} entry is refused until the \
                 reconciler sees the book whole again",
                facts.open_issues
            ),
            "resolve the issue on the desk's lifecycle page",
        );
    }
    if !facts.session_day {
        return out(
            "closed",
            "not an NSE session today".to_owned(),
            "the next session's login",
        );
    }
    if let Some(status) = facts.scan_status.as_deref()
        && (status == "QUEUED" || status == "RUNNING")
    {
        return out(
            "scanning",
            format!("a scan is {}", status.to_lowercase()),
            "its result",
        );
    }
    let after_close = clock >= market_close_time();
    if !facts.broker_session && !after_close {
        let what = match sleeve {
            "swing" => "the live scan, the monitor and the desk's entries",
            "twt" => "the 09:15 entry run and the desk's entries",
            _ => "the desk's confirms",
        };
        return out(
            "waiting_for_login",
            format!("no Kite session — {what} wait for a login"),
            "log in to Kite from the brokers page",
        );
    }
    if sleeve == "swing" && facts.market_open && fresh(facts.heartbeat_at, now) {
        return out(
            "monitoring",
            format!(
                "the monitor is watching {} name(s) and confirms its own triggers until 15:30",
                facts.watching
            ),
            "a trigger",
        );
    }
    let plan_today = facts.plan_session_date == Some(today)
        || (sleeve == "swing" && facts.plan_session_date.is_some());
    let plan_live = plan_today
        && facts
            .plan_expires_at
            .is_some_and(|e| e > now.with_timezone(&Utc));
    if plan_live && let Some(expires) = facts.plan_expires_at {
        let next = if sleeve == "twt" {
            "confirm on the desk (twt-auto confirms it within a minute)"
        } else {
            "confirm on the desk"
        };
        return out(
            "plan_ready",
            format!(
                "a {} plan is ready; it expires at {}",
                facts.plan_source.as_deref().unwrap_or(""),
                hhmm(expires)
            ),
            next,
        );
    }
    if sleeve == "twt" && facts.market_open && clock >= twt_entry_window_end() {
        let had = facts.plan_source.as_deref() == Some("MORNING")
            && facts.plan_session_date == Some(today);
        let head = if had {
            "the 09:05 MORNING plan expired at 09:35"
        } else {
            "no MORNING plan was built for today"
        };
        return out(
            "missed_window",
            format!(
                "{head} — press Scan for a live read of today; a signal is bought now, at market"
            ),
            "Scan now, or tomorrow's plan, built at 21:20 and re-sized at 09:05",
        );
    }
    if sleeve == "vbt" && facts.market_open && plan_today && !plan_live {
        return out(
            "missed_window",
            "today's plan has expired; VBT's limit at the signal close is placed from a plan"
                .to_owned(),
            "this evening's plan, built at 21:15",
        );
    }
    let found = facts.scan_found.as_ref();
    if facts.scan_status.as_deref() == Some("DONE") && found.is_some_and(|(n, _)| *n > 0) {
        let when = facts
            .scan_finished_at
            .map_or_else(String::new, |t| format!(" at {}", hhmm(t)));
        let prov = if facts.scan_provisional {
            " (provisional, today so far)"
        } else {
            ""
        };
        let shown = found.map_or("", |(_, s)| s.as_str());
        return out(
            "signal_ready",
            format!(
                "{shown} candidate(s) from the last scan{when}{prov}; a plan is built in the evening"
            ),
            "the evening plan, or the desk",
        );
    }
    if after_close {
        return out(
            "idle",
            "the session has closed".to_owned(),
            "tonight's detect and plan",
        );
    }
    match facts.scan_status.as_deref() {
        Some("DONE") => out(
            "idle",
            "the last scan found nothing".to_owned(),
            "the next scan",
        ),
        Some("FAILED") => out(
            "idle",
            "the last scan failed; see the run".to_owned(),
            "press Scan again",
        ),
        _ => out(
            "idle",
            "nothing is queued, planned or watched".to_owned(),
            "Scan, or the evening jobs",
        ),
    }
}

/// `is_nse_session_open(now, trading_dates)`: a trading day, and 09:15 <= the IST clock <= 15:30.
#[must_use]
pub fn is_nse_session_open(now: DateTime<FixedOffset>, session_day: bool) -> bool {
    if !session_day {
        return false;
    }
    let clock = now.with_timezone(&ist()).time();
    market_open_time() <= clock && clock <= market_close_time()
}

/// `broker_session_exists()`: a real, unexpired, non-simulated Kite token on disk — the local
/// checks only, no network call.
///
/// # Errors
/// A token blob that decrypts but does not parse escapes in Python as the 500 catch-all.
pub fn broker_session_exists(now: DateTime<FixedOffset>) -> Result<bool, Problem> {
    let env = |k: &str| std::env::var(k).unwrap_or_default().trim().to_owned();
    if env("BASKFY_KITE_API_KEY").is_empty() {
        return Ok(false);
    }
    let path = [
        env("BASKFY_BROKER_TOKEN_PATH"),
        env("BASKFY_KITE_TOKEN_PATH"),
    ]
    .into_iter()
    .find(|p| !p.is_empty())
    .unwrap_or_else(|| ".secrets/broker-token.enc".to_owned());
    let key = env("BASKFY_KITE_TOKEN_ENCRYPTION_KEY");
    let store = baskfy_providers::tokens::AccessTokenStore::new(
        path,
        baskfy_providers::secret::Secret::new(key),
    );
    match store.load() {
        Ok(token) => Ok(!(token.value.expose().starts_with("sim_") || token.is_expired(now))),
        Err(e) if e.python_type() == "CredentialsMissing" => Ok(false),
        Err(e) => {
            crate::logging::error(
                "baskfy_api.app",
                "unhandled error",
                &[("error", e.message())],
            );
            Err(crate::problems::internal_error())
        }
    }
}

/// `_found_in(detail)`: the first of `found`, `signals`, `setups`, `candidates`, `detected` that
/// is an `int` (Python's `bool` included).
fn found_in(detail: Option<&baskfy_db::OrderedJson>) -> Option<(i64, String)> {
    let detail = detail?;
    if !matches!(detail, baskfy_db::OrderedJson::Object(_)) {
        return None;
    }
    for key in ["found", "signals", "setups", "candidates", "detected"] {
        match detail.get(key) {
            Some(baskfy_db::OrderedJson::Bool(b)) => {
                return Some((i64::from(*b), if *b { "True" } else { "False" }.to_owned()));
            }
            v @ Some(baskfy_db::OrderedJson::Int(_)) => {
                let n = json_int(v)?;
                return Some((n, n.to_string()));
            }
            _ => {}
        }
    }
    None
}

async fn one(state: &AppState, sql: &str, user_id: i64) -> Result<Option<PgRow>, Problem> {
    sqlx::query(sql)
        .bind(user_id)
        .fetch_optional(state.db.pool())
        .await
        .map_err(db)
}

async fn facts_for(
    state: &AppState,
    sleeve: &str,
    user_id: i64,
    session_day: bool,
    market_open: bool,
    broker_session: bool,
) -> Result<SleeveFacts, Problem> {
    let (scan_table, plan_table, plan_date) = match sleeve {
        "swing" => ("sw_scan_run", "sw_plan", "as_of"),
        "twt" => ("tw_scan_run", "tw_plan", "session_date"),
        _ => ("vb_scan_run", "vb_plan", "session_date"),
    };
    let scan = one(
        state,
        &format!(
            "SELECT status, detail, finished_at, provisional FROM {scan_table} \
             WHERE user_id = $1 ORDER BY requested_at DESC, id DESC LIMIT 1"
        ),
        user_id,
    )
    .await?;
    let plan = one(
        state,
        &format!(
            "SELECT source, {plan_date} AS plan_date, expires_at FROM {plan_table} \
             WHERE user_id = $1 ORDER BY built_at DESC, id DESC LIMIT 1"
        ),
        user_id,
    )
    .await?;
    let process = match sleeve {
        "swing" => Some("swing_monitor"),
        "twt" => Some("twt_auto"),
        _ => None,
    };
    let mut heartbeat_at = None;
    if let Some(process) = process {
        heartbeat_at = sqlx::query_scalar::<_, DateTime<Utc>>(
            "SELECT at FROM lv_heartbeat WHERE user_id = $1 AND process = $2",
        )
        .bind(user_id)
        .bind(process)
        .fetch_optional(state.db.pool())
        .await
        .map_err(db)?;
    }
    let issues: Vec<(String, i64)> = sqlx::query_as(
        "SELECT kind, count(*) FROM lv_protection_issue WHERE user_id = $1 AND sleeve = $2 \
         AND resolved_at IS NULL GROUP BY kind",
    )
    .bind(user_id)
    .bind(sleeve)
    .fetch_all(state.db.pool())
    .await
    .map_err(db)?;
    let mut watching = 0;
    if sleeve == "swing" {
        watching = sqlx::query_scalar::<_, i64>(
            "SELECT count(*) FROM sw_watch WHERE user_id = $1 AND state = 'WATCHING'",
        )
        .bind(user_id)
        .fetch_one(state.db.pool())
        .await
        .map_err(db)?;
    }
    let mut kinds: Vec<String> = issues.iter().map(|(k, _)| k.clone()).collect();
    kinds.sort();
    let mut facts = SleeveFacts {
        sleeve: sleeve.to_owned(),
        session_day,
        market_open,
        broker_session,
        open_issues: issues.iter().map(|(_, n)| *n).sum(),
        issue_kinds: kinds,
        watching,
        heartbeat_at,
        ..SleeveFacts::default()
    };
    if let Some(row) = scan {
        facts.scan_status = Some(col::<String>(&row, "status")?);
        facts.scan_found = found_in(as_dict(ojson(&row, "detail")?).as_ref());
        facts.scan_finished_at = col(&row, "finished_at")?;
        facts.scan_provisional = col(&row, "provisional")?;
    }
    if let Some(row) = plan {
        facts.plan_source = Some(col::<String>(&row, "source")?);
        facts.plan_session_date = Some(col::<NaiveDate>(&row, "plan_date")?);
        facts.plan_expires_at = Some(col::<DateTime<Utc>>(&row, "expires_at")?);
    }
    Ok(facts)
}

/// `is_session_day(session, day)`: a day the calendar does not carry is a session; a named
/// holiday is not.
async fn is_session_day(state: &AppState, day: NaiveDate) -> Result<bool, Problem> {
    let known: Option<bool> = sqlx::query_scalar(
        "SELECT is_trading_day FROM trading_day WHERE exchange_id = $1 AND date = $2",
    )
    .bind(NSE_EXCHANGE_ID)
    .bind(day)
    .fetch_optional(state.db.pool())
    .await
    .map_err(db)?;
    Ok(known.unwrap_or(true))
}

/// `sleeve_states(session, user_id=, now=None)`.
///
/// # Errors
/// A database failure, as the 500 catch-all.
pub async fn sleeve_states(
    state: &AppState,
    user_id: i64,
    now: DateTime<FixedOffset>,
) -> Result<Vec<SleeveStateOut>, Problem> {
    let today = now.with_timezone(&ist()).date_naive();
    let session_day =
        today.weekday().num_days_from_monday() < 5 && is_session_day(state, today).await?;
    let market_open = is_nse_session_open(now, session_day);
    let broker = broker_session_exists(now)?;
    let mut out = Vec::new();
    for sleeve in SLEEVES {
        let facts = facts_for(state, sleeve, user_id, session_day, market_open, broker).await?;
        out.push(derive_state(&facts, now));
    }
    Ok(out)
}

/// `GET /sleeves/state`.
///
/// # Errors
/// 401 anonymous; 404 for a caller who is not the sole tenant; 503 with no sole tenant; 500 on a
/// database failure.
pub async fn get_sleeve_states(
    State(state): State<AppState>,
    Extension(principal): Extension<Principal>,
) -> Result<Response, Problem> {
    require_authenticated(&principal)?;
    let user_id = scoped_sole_user_id(&state, principal.user_id, None).await?;
    let now = now_utc().with_timezone(&ist());
    let rows = sleeve_states(&state, user_id, now).await?;
    Ok(pydantic_response(&J::Arr(
        rows.iter().map(SleeveStateOut::to_j).collect(),
    )))
}

/// The routes of `baskfy_api.routers.sleeve_state`, relative to `/api/v1`.
pub fn router() -> Router<AppState> {
    Router::new().route("/sleeves/state", get(get_sleeve_states))
}

#[cfg(test)]
mod tests {
    //! Ports `services/api/tests/test_sleeve_state.py`: `derive_state` is pure, so every branch
    //! is a table.
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn monday() -> NaiveDate {
        NaiveDate::from_ymd_opt(2026, 9, 28).unwrap()
    }

    fn at(hhmm: &str) -> DateTime<FixedOffset> {
        let (h, m) = hhmm.split_once(':').unwrap();
        monday()
            .and_hms_opt(h.parse().unwrap(), m.parse().unwrap(), 0)
            .unwrap()
            .and_local_timezone(ist())
            .unwrap()
    }

    fn utc(t: DateTime<FixedOffset>) -> DateTime<Utc> {
        t.with_timezone(&Utc)
    }

    fn facts(sleeve: &str) -> SleeveFacts {
        SleeveFacts {
            sleeve: sleeve.to_owned(),
            session_day: true,
            market_open: true,
            broker_session: true,
            ..SleeveFacts::default()
        }
    }

    #[test]
    fn no_session_before_the_open_is_waiting_for_login() {
        let f = SleeveFacts {
            market_open: false,
            broker_session: false,
            ..facts("twt")
        };
        let out = derive_state(&f, at("08:50"));
        assert_eq!(out.state, "waiting_for_login");
        assert!(out.reason.contains("no Kite session") && out.next.contains("log in"));
    }

    #[test]
    fn no_session_after_the_close_is_idle_not_waiting() {
        let f = SleeveFacts {
            market_open: false,
            broker_session: false,
            ..facts("twt")
        };
        let out = derive_state(&f, at("16:00"));
        assert_eq!(out.state, "idle");
        assert!(out.reason.contains("closed"));
    }

    #[test]
    fn a_holiday_is_closed_whatever_else_is_true() {
        let f = SleeveFacts {
            session_day: false,
            market_open: false,
            broker_session: false,
            ..facts("twt")
        };
        assert_eq!(derive_state(&f, at("10:30")).state, "closed");
    }

    #[test]
    fn an_open_protection_issue_blocks_first() {
        let f = SleeveFacts {
            open_issues: 2,
            issue_kinds: vec!["GTT_MISSING".into(), "NAKED".into()],
            plan_source: Some("MORNING".into()),
            ..facts("twt")
        };
        let out = derive_state(&f, at("10:30"));
        assert_eq!(out.state, "blocked");
        assert!(out.reason.contains("GTT_MISSING, NAKED") && out.next.contains("lifecycle"));
    }

    #[test]
    fn a_queued_or_running_scan_is_scanning() {
        for s in ["QUEUED", "RUNNING"] {
            let f = SleeveFacts {
                scan_status: Some(s.into()),
                ..facts("twt")
            };
            assert_eq!(derive_state(&f, at("10:30")).state, "scanning");
        }
    }

    #[test]
    fn twt_with_the_morning_plan_expired_is_missed_window() {
        let f = SleeveFacts {
            plan_source: Some("MORNING".into()),
            plan_session_date: Some(monday()),
            plan_expires_at: Some(utc(at("09:35"))),
            ..facts("twt")
        };
        let out = derive_state(&f, at("10:30"));
        assert_eq!(out.state, "missed_window");
        assert!(out.reason.contains("expired at 09:35") && out.reason.contains("at market"));
        assert!(out.next.contains("Scan now") && out.next.contains("tomorrow"));
    }

    #[test]
    fn twt_with_no_morning_plan_is_missed_window_too() {
        let out = derive_state(&facts("twt"), at("10:30"));
        assert_eq!(out.state, "missed_window");
        assert!(out.reason.contains("no MORNING plan"));
    }

    #[test]
    fn twt_before_the_window_closes_with_a_live_plan_is_plan_ready() {
        let f = SleeveFacts {
            plan_source: Some("MORNING".into()),
            plan_session_date: Some(monday()),
            plan_expires_at: Some(utc(at("09:35"))),
            ..facts("twt")
        };
        let out = derive_state(&f, at("09:20"));
        assert_eq!(out.state, "plan_ready");
        assert!(out.next.contains("twt-auto"));
        assert!(out.reason.ends_with("expires at 09:35"), "{}", out.reason);
    }

    #[test]
    fn vbt_with_an_expired_plan_is_missed_window() {
        let f = SleeveFacts {
            plan_source: Some("MORNING".into()),
            plan_session_date: Some(monday()),
            plan_expires_at: Some(utc(at("09:30"))),
            ..facts("vbt")
        };
        let out = derive_state(&f, at("10:30"));
        assert_eq!(out.state, "missed_window");
        assert!(out.reason.contains("signal close"));
    }

    #[test]
    fn swing_heartbeat_fresh_is_monitoring_and_stale_is_not() {
        let f = SleeveFacts {
            heartbeat_at: Some(utc(at("10:29"))),
            watching: 5,
            ..facts("swing")
        };
        let out = derive_state(&f, at("10:30"));
        assert_eq!(out.state, "monitoring");
        assert!(out.reason.contains("5 name(s)"));
        let stale = utc(at("10:30")) - chrono::Duration::seconds(HEARTBEAT_FRESH_SECONDS + 1);
        let f = SleeveFacts {
            heartbeat_at: Some(stale),
            watching: 5,
            ..facts("swing")
        };
        assert_ne!(derive_state(&f, at("10:30")).state, "monitoring");
    }

    #[test]
    fn signals_and_idle() {
        let f = SleeveFacts {
            scan_status: Some("DONE".into()),
            scan_found: Some((3, "3".into())),
            scan_provisional: true,
            scan_finished_at: Some(utc(at("10:20"))),
            ..facts("swing")
        };
        let out = derive_state(&f, at("10:30"));
        assert_eq!(out.state, "signal_ready");
        assert!(
            out.reason.contains("3 candidate(s)")
                && out.reason.contains("provisional")
                && out.reason.contains("10:20")
        );
        let f = SleeveFacts {
            scan_status: Some("DONE".into()),
            scan_found: Some((0, "0".into())),
            ..facts("vbt")
        };
        let out = derive_state(&f, at("10:30"));
        assert_eq!(out.state, "idle");
        assert!(out.reason.contains("found nothing"));
        let f = SleeveFacts {
            scan_status: Some("FAILED".into()),
            ..facts("swing")
        };
        let out = derive_state(&f, at("10:30"));
        assert_eq!(out.state, "idle");
        assert!(out.reason.contains("failed"));
    }

    #[test]
    fn every_answer_says_what_scan_means_for_its_sleeve() {
        for sleeve in SLEEVES {
            let out = derive_state(&facts(sleeve), at("10:30"));
            assert_eq!(out.scan_means, scan_means(sleeve));
            assert_eq!(out.sleeve, sleeve);
            assert_eq!(out.updated_at, at("10:30"));
        }
        for sleeve in ["twt", "vbt"] {
            let m = scan_means(sleeve);
            assert!(
                m.contains("today so far")
                    && m.contains("last published session")
                    && m.contains("at market")
            );
        }
        assert!(scan_means("swing").contains("today so far"));
    }

    #[test]
    fn the_session_window_is_inclusive_of_both_ends() {
        assert!(is_nse_session_open(at("09:15"), true));
        assert!(is_nse_session_open(at("15:30"), true));
        assert!(!is_nse_session_open(at("09:14"), true));
        assert!(!is_nse_session_open(at("10:00"), false));
    }

    #[test]
    fn found_reads_the_first_int_key() {
        let v = baskfy_db::OrderedJson::parse(r#"{"x":1,"signals":2,"found":"3"}"#).unwrap();
        assert_eq!(found_in(Some(&v)), Some((2, "2".into())));
        let v = baskfy_db::OrderedJson::parse(r#"{"found":true}"#).unwrap();
        assert_eq!(found_in(Some(&v)), Some((1, "True".into())));
        assert_eq!(found_in(None), None);
    }
}
