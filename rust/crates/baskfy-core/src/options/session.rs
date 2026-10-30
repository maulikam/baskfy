//! Port of `baskfy_core.options.session`: the session state machine per sleeve per date (`04` §11).

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime, TimeDelta};
use serde::{Deserialize, Serialize};

use crate::error::{CoreError, CoreResult};
use crate::options::config::Sleeve;
use crate::options::num::combine;

/// A session's state.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SessionState {
    /// Watching the gate.
    Observing,
    /// No trade today.
    Skipped,
    /// A plan is live.
    Planned,
    /// The plan expired unconfirmed.
    Lapsed,
    /// The plan was confirmed.
    Confirmed,
    /// Every entry leg filled.
    Open,
    /// Done.
    Closed,
}

impl SessionState {
    /// The enum's value.
    pub fn value(self) -> &'static str {
        match self {
            SessionState::Observing => "OBSERVING",
            SessionState::Skipped => "SKIPPED",
            SessionState::Planned => "PLANNED",
            SessionState::Lapsed => "LAPSED",
            SessionState::Confirmed => "CONFIRMED",
            SessionState::Open => "OPEN",
            SessionState::Closed => "CLOSED",
        }
    }
}

/// The reason a `CONFIRMED` session closes without opening.
pub const NEVER_OPENED: &str = "NEVER_OPENED";

/// The machine's seven edges.
pub const EDGES: [(SessionState, SessionState); 7] = [
    (SessionState::Observing, SessionState::Skipped),
    (SessionState::Observing, SessionState::Planned),
    (SessionState::Planned, SessionState::Lapsed),
    (SessionState::Planned, SessionState::Confirmed),
    (SessionState::Confirmed, SessionState::Open),
    (SessionState::Confirmed, SessionState::Closed),
    (SessionState::Open, SessionState::Closed),
];

/// The terminal states.
pub const TERMINAL: [SessionState; 3] = [
    SessionState::Skipped,
    SessionState::Lapsed,
    SessionState::Closed,
];

/// One sleeve's session on one date.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Session {
    /// The sleeve.
    pub sleeve: Sleeve,
    /// The trade date.
    pub trade_date: NaiveDate,
    /// Its state.
    pub state: SessionState,
    /// Why it is in that state.
    pub reason: Option<String>,
}

/// Open today's session for `sleeve`; refuses a second one (`DuplicateSession`).
pub fn new_session(
    sleeve: Sleeve,
    trade_date: NaiveDate,
    existing: &[Session],
) -> CoreResult<Session> {
    if existing
        .iter()
        .any(|o| o.sleeve == sleeve && o.trade_date == trade_date)
    {
        return Err(CoreError::InvalidInput(format!(
            "{} already has a session on {trade_date}",
            sleeve.value()
        )));
    }
    Ok(Session {
        sleeve,
        trade_date,
        state: SessionState::Observing,
        reason: None,
    })
}

/// Move along one of the seven edges, or an `IllegalTransition` error.
pub fn transition(
    session: &Session,
    to: SessionState,
    reason: Option<&str>,
) -> CoreResult<Session> {
    let edge = (session.state, to);
    let illegal = |m: String| Err(CoreError::InvalidInput(m));
    if !EDGES.contains(&edge) {
        return illegal(format!(
            "{} → {} is not an edge",
            session.state.value(),
            to.value()
        ));
    }
    let empty = reason.is_none_or(str::is_empty);
    if to == SessionState::Skipped && empty {
        return illegal("a skipped session carries its reason".to_owned());
    }
    if edge == (SessionState::Confirmed, SessionState::Closed) && reason != Some(NEVER_OPENED) {
        return illegal("a confirmed session that never opened closes NEVER_OPENED".to_owned());
    }
    if edge == (SessionState::Open, SessionState::Closed) && (empty || reason == Some(NEVER_OPENED))
    {
        return illegal("an open session closes with its exit code".to_owned());
    }
    Ok(Session {
        state: to,
        reason: reason.map(str::to_owned),
        ..session.clone()
    })
}

/// `expires_at = min(issued_at + ttl, entry_window_end)` (`03` §9).
pub fn plan_expires_at(
    issued_at: &DateTime<FixedOffset>,
    entry_window_end: NaiveTime,
    ttl_minutes: i64,
) -> DateTime<FixedOffset> {
    let window_end = combine(
        issued_at.date_naive(),
        entry_window_end,
        *issued_at.offset(),
    );
    let ttl = *issued_at + TimeDelta::minutes(ttl_minutes);
    if window_end < ttl { window_end } else { ttl }
}

/// A plan may be confirmed only strictly before it expires.
pub fn plan_is_live(expires_at: &DateTime<FixedOffset>, now: &DateTime<FixedOffset>) -> bool {
    now < expires_at
}
