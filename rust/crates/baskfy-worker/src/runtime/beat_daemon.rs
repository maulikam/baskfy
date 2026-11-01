//! `celery beat`: fire `BEAT_SCHEDULE` at its IST times onto the routed Redis queues.

use std::future::Future;

use baskfy_db::OrderedJson;
use chrono::{DateTime, Duration, DurationRound as _, NaiveDateTime, Utc};

use super::celery::{self, ArgsRepr, Outgoing};
use super::log;
use crate::error::WorkerError;
use crate::pyjson::s;
use crate::tasks::beat::{BEAT_SCHEDULE, BeatEntry, Expanded, Schedule};

/// Bound on how far back a late tick looks (Celery fires a missed entry once; so does this).
const MAX_LOOKBACK_MINUTES: i64 = 7 * 24 * 60;

/// The IST wall-clock minute of an instant.
#[must_use]
pub fn ist_minute(at: DateTime<Utc>) -> NaiveDateTime {
    let local = at.with_timezone(&celery::ist()).naive_local();
    local
        .duration_trunc(Duration::minutes(1))
        .unwrap_or(local)
}

fn expanded_matches(e: &Expanded, at: NaiveDateTime) -> bool {
    use chrono::{Datelike as _, Timelike as _};
    e.minute.contains(&at.minute())
        && e.hour.contains(&at.hour())
        && e.day_of_week.contains(&at.weekday().num_days_from_sunday())
        && e.day_of_month.contains(&at.day())
        && e.month_of_year.contains(&at.month())
}

/// The crontab half of `BEAT_SCHEDULE`, expanded once.
#[derive(Debug, Clone)]
pub struct Crontabs {
    entries: Vec<(&'static BeatEntry, Expanded)>,
}

impl Crontabs {
    /// Expand every crontab entry.
    ///
    /// # Errors
    /// A field Celery would refuse.
    pub fn load() -> Result<Self, String> {
        let mut entries = Vec::new();
        for entry in BEAT_SCHEDULE {
            if let Schedule::Crontab(c) = entry.schedule {
                entries.push((entry, c.expand()?));
            }
        }
        Ok(Self { entries })
    }

    /// Entries due at some IST minute in `(after, through]`, each once, in schedule order.
    #[must_use]
    pub fn due_between(
        &self,
        after: NaiveDateTime,
        through: NaiveDateTime,
    ) -> Vec<&'static BeatEntry> {
        let earliest = through - Duration::minutes(MAX_LOOKBACK_MINUTES);
        let start = if after < earliest { earliest } else { after };
        let mut out = Vec::new();
        for (entry, e) in &self.entries {
            let mut minute = start + Duration::minutes(1);
            while minute <= through {
                if expanded_matches(e, minute) {
                    out.push(*entry);
                    break;
                }
                minute += Duration::minutes(1);
            }
        }
        out
    }
}

/// The message Beat publishes for `entry` at `now` (`task.apply_async((), kwargs, **options)`).
///
/// # Errors
/// The entropy source.
pub fn beat_message(entry: &BeatEntry, now: DateTime<Utc>) -> Result<Outgoing, WorkerError> {
    let kwargs: Vec<(String, OrderedJson)> = entry
        .kwargs
        .iter()
        .map(|(k, v)| ((*k).to_owned(), s(*v)))
        .collect();
    let mut out = Outgoing::new(
        entry.task,
        Vec::new(),
        ArgsRepr::Tuple,
        kwargs,
        entry.options.queue.name(),
    )?;
    let local = now.with_timezone(&celery::ist());
    out.eta = entry
        .options
        .countdown
        .map(|c| local + Duration::seconds(i64::from(c)));
    out.expires = entry
        .options
        .expires
        .map(|e| local + Duration::seconds(i64::from(e)));
    out.expiration_ms = entry.options.expires.map(|e| u64::from(e) * 1000);
    if let Some(limit) = crate::tasks::find(entry.task).and_then(|t| t.time_limit) {
        out.timelimit = [OrderedJson::Int(limit), OrderedJson::Null];
    }
    Ok(out)
}

/// `SADD` the queue's binding and `LPUSH` the message (kombu's `_put`).
///
/// # Errors
/// The broker's.
pub async fn publish<C: redis::aio::ConnectionLike + Send>(
    conn: &mut C,
    message: &Outgoing,
) -> Result<(), String> {
    let text = message.encode()?;
    let (binding_key, member) = celery::binding(&message.queue);
    redis::pipe()
        .cmd("SADD")
        .arg(binding_key)
        .arg(member)
        .ignore()
        .cmd("LPUSH")
        .arg(celery::queue_key(&message.queue, message.priority))
        .arg(text)
        .ignore()
        .query_async::<()>(conn)
        .await
        .map_err(|e| e.to_string())
}

async fn fire(conn: &mut redis::aio::ConnectionManager, entry: &BeatEntry, now: DateTime<Utc>) {
    log(
        "INFO",
        &format!("Scheduler: Sending due task {} ({})", entry.name, entry.task),
    );
    let result = match beat_message(entry, now) {
        Ok(message) => publish(conn, &message).await,
        Err(e) => Err(e.to_string()),
    };
    if let Err(e) = result {
        log(
            "ERROR",
            &format!("Message Error: Couldn't apply scheduled task {}: {e}", entry.name),
        );
    }
}

/// Run Beat until `shutdown` resolves: crontabs at their IST minute (none for the minute it
/// starts in), interval entries every N seconds from start.
///
/// # Errors
/// The schedule does not expand, or the broker cannot be reached at start.
pub async fn run(broker_url: &str, shutdown: impl Future<Output = ()>) -> Result<(), String> {
    let crontabs = Crontabs::load()?;
    let client = redis::Client::open(broker_url).map_err(|e| e.to_string())?;
    let mut conn = client
        .get_connection_manager()
        .await
        .map_err(|e| e.to_string())?;
    let start = Utc::now();
    let mut last_minute = ist_minute(start);
    let mut intervals: Vec<(&'static BeatEntry, Duration, DateTime<Utc>)> = BEAT_SCHEDULE
        .iter()
        .filter_map(|e| match e.schedule {
            Schedule::Every { seconds } => {
                let every = Duration::seconds(i64::from(seconds));
                Some((e, every, start + every))
            }
            Schedule::Crontab(_) => None,
        })
        .collect();
    log(
        "INFO",
        &format!("beat: Starting... ({} entries)", BEAT_SCHEDULE.len()),
    );
    tokio::pin!(shutdown);
    loop {
        let now = Utc::now();
        let next_minute = {
            let local = now.with_timezone(&celery::ist());
            let trunc = local.duration_trunc(Duration::minutes(1)).unwrap_or(local);
            (trunc + Duration::minutes(1)).with_timezone(&Utc)
        };
        let wake = intervals
            .iter()
            .map(|(_, _, next)| *next)
            .fold(next_minute, |a, b| if b < a { b } else { a });
        let wait = (wake - now).to_std().unwrap_or_default();
        tokio::select! {
            () = &mut shutdown => break,
            () = tokio::time::sleep(wait) => {}
        }
        let now = Utc::now();
        let minute = ist_minute(now);
        if minute > last_minute {
            for entry in crontabs.due_between(last_minute, minute) {
                fire(&mut conn, entry, now).await;
            }
            last_minute = minute;
        }
        for (entry, every, next) in &mut intervals {
            if *next <= now {
                fire(&mut conn, entry, now).await;
                *next = now + *every;
            }
        }
    }
    log("INFO", "beat: Shutting down");
    Ok(())
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;
    use chrono::NaiveDate;

    fn ist(y: i32, m: u32, d: u32, h: u32, min: u32) -> NaiveDateTime {
        NaiveDate::from_ymd_opt(y, m, d)
            .unwrap()
            .and_hms_opt(h, min, 0)
            .unwrap()
    }

    fn names(v: &[&BeatEntry]) -> Vec<&'static str> {
        v.iter().map(|e| e.name).collect()
    }

    #[test]
    fn the_ist_minute_is_utc_plus_five_thirty() {
        let at = DateTime::parse_from_rfc3339("2026-10-30T14:20:42.5Z")
            .unwrap()
            .with_timezone(&Utc);
        assert_eq!(ist_minute(at), ist(2026, 10, 30, 19, 50));
        let jan = DateTime::parse_from_rfc3339("2026-01-15T14:20:00Z")
            .unwrap()
            .with_timezone(&Utc);
        assert_eq!(ist_minute(jan), ist(2026, 1, 15, 19, 50));
    }

    #[test]
    fn one_tick_fires_exactly_what_due_at_lists() {
        let c = Crontabs::load().unwrap();
        for at in [
            ist(2026, 10, 30, 15, 50),
            ist(2026, 10, 30, 19, 30),
            ist(2026, 10, 30, 9, 15),
            ist(2026, 10, 31, 9, 15),
            ist(2026, 11, 2, 8, 45),
        ] {
            let tick = c.due_between(at - Duration::minutes(1), at);
            let want = crate::tasks::beat::due_at(at).unwrap();
            assert_eq!(names(&tick), names(&want), "{at}");
        }
    }

    #[test]
    fn the_start_minute_never_fires_and_the_next_does() {
        let c = Crontabs::load().unwrap();
        let start = ist(2026, 10, 30, 15, 50);
        assert!(c.due_between(start, start).is_empty());
        let next = c.due_between(start, start + Duration::minutes(1));
        assert!(!names(&next).contains(&"capture-kite-trades"));
        assert!(names(&next).contains(&"options-collect-chain"));
    }

    #[test]
    fn a_late_tick_fires_each_missed_entry_once() {
        let c = Crontabs::load().unwrap();
        let after = ist(2026, 10, 30, 9, 10);
        let fired = c.due_between(after, ist(2026, 10, 30, 9, 40));
        let mut seen = std::collections::BTreeSet::new();
        for e in &fired {
            assert!(seen.insert(e.name), "{} fired twice", e.name);
        }
        assert!(seen.contains("options-collect-chain"));
        assert!(seen.contains("cb-rebalance-notify"));
    }

    #[test]
    fn weekends_are_quiet_for_weekday_entries() {
        let c = Crontabs::load().unwrap();
        let sat = ist(2026, 10, 31, 15, 50);
        assert!(!names(&c.due_between(sat - Duration::minutes(1), sat)).contains(&"capture-kite-trades"));
    }

    #[test]
    fn a_beat_message_carries_expires_eta_and_kwargs() {
        let entry = BEAT_SCHEDULE.iter().find(|e| e.name == "options-scan").unwrap();
        let now = DateTime::parse_from_rfc3339("2026-10-30T04:00:00Z")
            .unwrap()
            .with_timezone(&Utc);
        let m = beat_message(entry, now).unwrap();
        let back = celery::decode(&m.encode().unwrap()).unwrap();
        assert_eq!(back.task, "baskfy.options.scan");
        assert_eq!(back.routing_key, "default");
        assert_eq!(
            celery::py_isoformat(back.eta.unwrap()),
            "2026-10-30T09:30:20+05:30"
        );
        assert_eq!(
            celery::py_isoformat(back.expires.unwrap()),
            "2026-10-30T09:30:55+05:30"
        );
        assert_eq!(m.expiration_ms, Some(55_000));

        let nudge = BEAT_SCHEDULE
            .iter()
            .find(|e| e.name == "kite-login-nudge")
            .unwrap();
        let n = celery::decode(&beat_message(nudge, now).unwrap().encode().unwrap()).unwrap();
        assert_eq!(n.kwargs, vec![("window".to_owned(), s("first"))]);
        assert_eq!(n.routing_key, "compute");
        assert!(n.eta.is_none() && n.expires.is_none());

        let probe = BEAT_SCHEDULE
            .iter()
            .find(|e| e.task == "baskfy.swing.timing_probe")
            .unwrap();
        let p = celery::decode(&beat_message(probe, now).unwrap().encode().unwrap()).unwrap();
        assert_eq!(p.hard_time_limit(), Some(1500.0));
    }
}
