//! `celery_app.BEAT_SCHEDULE` as data: every entry Beat fires, its task, its schedule in **IST**
//! (`timezone = "Asia/Kolkata"`, `enable_utc = False`), its kwargs and its publish options.
//! **Generated from** `tests/fixtures/python_task_registry.json` and held equal to it by
//! `tests/task_registry.rs` (names, the five crontab fields as written *and* as Celery expands
//! them, intervals, kwargs, queues, `expires`, `countdown`).
//!
//! Python's Beat keeps firing these at cutover (R8.1): the table is here so the single binary's own
//! scheduler can replace Beat later without re-deriving it, and so a schedule change in Python
//! fails the Rust build until the port follows. [`Crontab::is_due`] answers "does this entry fire
//! at this IST minute" with Celery's semantics — every field must match (day-of-month **and**
//! day-of-week, unlike cron's either-or), weekday 0 is Sunday.

use std::collections::BTreeSet;

use chrono::{Datelike, NaiveDateTime, Timelike};

use super::Queue;

/// A crontab's five fields as written in `celery_app.py`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Crontab {
    /// `minute`.
    pub minute: &'static str,
    /// `hour`.
    pub hour: &'static str,
    /// `day_of_week` (0 or `sun` is Sunday).
    pub day_of_week: &'static str,
    /// `day_of_month`.
    pub day_of_month: &'static str,
    /// `month_of_year`.
    pub month_of_year: &'static str,
}

/// A crontab's fields as Celery expands them (`crontab.minute` etc. are sets).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Expanded {
    /// Minutes, 0–59.
    pub minute: BTreeSet<u32>,
    /// Hours, 0–23.
    pub hour: BTreeSet<u32>,
    /// Weekdays, 0 (Sunday) – 6.
    pub day_of_week: BTreeSet<u32>,
    /// Days, 1–31.
    pub day_of_month: BTreeSet<u32>,
    /// Months, 1–12.
    pub month_of_year: BTreeSet<u32>,
}

/// How an entry is scheduled.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Schedule {
    /// `crontab(...)`, in IST.
    Crontab(Crontab),
    /// `timedelta(seconds=...)`: every N seconds from Beat's start.
    Every {
        /// The interval.
        seconds: u32,
    },
}

/// An entry's `options`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BeatOptions {
    /// `queue`.
    pub queue: Queue,
    /// `expires`, seconds after publishing.
    pub expires: Option<u32>,
    /// `countdown`, seconds.
    pub countdown: Option<u32>,
}

/// One `BEAT_SCHEDULE` entry.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BeatEntry {
    /// The entry's key (`"refresh-reference-data"`).
    pub name: &'static str,
    /// The task it publishes.
    pub task: &'static str,
    /// When.
    pub schedule: Schedule,
    /// `kwargs` (every value in the table is a string).
    pub kwargs: &'static [(&'static str, &'static str)],
    /// `options`.
    pub options: BeatOptions,
}

const WEEKDAYS: [&str; 7] = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];
const MONTHS: [&str; 12] = [
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
];

/// One field's parser (`celery.schedules.crontab_parser(max_, min_)`).
struct Field {
    max: u32,
    min: u32,
    names: &'static [&'static str],
    name_base: u32,
}

impl Field {
    fn value(&self, token: &str) -> Result<u32, String> {
        if let Ok(n) = token.parse::<u32>() {
            return Ok(n);
        }
        let abbreviation: String = token.chars().take(3).collect::<String>().to_lowercase();
        self.names
            .iter()
            .position(|n| *n == abbreviation)
            .and_then(|i| u32::try_from(i).ok())
            .map(|i| i + self.name_base)
            .ok_or_else(|| format!("Invalid weekday/month literal {token:?}."))
    }

    fn range(&self, from: u32, to: u32) -> Vec<u32> {
        if from <= to {
            (from..=to).collect()
        } else {
            // A wrapping range (`fri-mon`), as Celery's `_expand_range`.
            (from..self.max + self.min).chain(self.min..=to).collect()
        }
    }

    fn part(&self, part: &str) -> Result<Vec<u32>, String> {
        let (body, step) = match part.split_once('/') {
            Some((b, s)) => (
                b,
                Some(s.parse::<usize>().map_err(|_| format!("bad step {s:?}"))?),
            ),
            None => (part, None),
        };
        let values = if body == "*" {
            (self.min..self.max + self.min).collect()
        } else if let Some((a, b)) = body.split_once('-') {
            self.range(self.value(a)?, self.value(b)?)
        } else if step.is_some() {
            return Err(format!("a step needs a range or '*': {part:?}"));
        } else {
            vec![self.value(body)?]
        };
        Ok(match step {
            Some(0) => return Err(format!("zero step in {part:?}")),
            Some(n) => values.into_iter().step_by(n).collect(),
            None => values,
        })
    }

    fn parse(&self, spec: &str) -> Result<BTreeSet<u32>, String> {
        let mut out = BTreeSet::new();
        for part in spec.split(',') {
            for v in self.part(part.trim())? {
                if v < self.min || v >= self.max + self.min {
                    return Err(format!(
                        "Invalid crontab pattern. Valid range is {}-{}. '{v}' was found.",
                        self.min,
                        self.max + self.min - 1
                    ));
                }
                out.insert(v);
            }
        }
        Ok(out)
    }
}

const MINUTE: Field = Field {
    max: 60,
    min: 0,
    names: &[],
    name_base: 0,
};
const HOUR: Field = Field {
    max: 24,
    min: 0,
    names: &[],
    name_base: 0,
};
const DAY_OF_WEEK: Field = Field {
    max: 7,
    min: 0,
    names: &WEEKDAYS,
    name_base: 0,
};
const DAY_OF_MONTH: Field = Field {
    max: 31,
    min: 1,
    names: &[],
    name_base: 0,
};
const MONTH_OF_YEAR: Field = Field {
    max: 12,
    min: 1,
    names: &MONTHS,
    name_base: 1,
};

impl Crontab {
    /// Celery's expansion of the five fields.
    ///
    /// # Errors
    /// A field Celery would refuse.
    pub fn expand(&self) -> Result<Expanded, String> {
        Ok(Expanded {
            minute: MINUTE.parse(self.minute)?,
            hour: HOUR.parse(self.hour)?,
            day_of_week: DAY_OF_WEEK.parse(self.day_of_week)?,
            day_of_month: DAY_OF_MONTH.parse(self.day_of_month)?,
            month_of_year: MONTH_OF_YEAR.parse(self.month_of_year)?,
        })
    }

    /// Does this crontab fire in the IST minute `at`?
    ///
    /// # Errors
    /// A field Celery would refuse.
    pub fn is_due(&self, at: NaiveDateTime) -> Result<bool, String> {
        let e = self.expand()?;
        Ok(e.minute.contains(&at.minute())
            && e.hour.contains(&at.hour())
            && e.day_of_week.contains(&at.weekday().num_days_from_sunday())
            && e.day_of_month.contains(&at.day())
            && e.month_of_year.contains(&at.month()))
    }
}

/// The crontab entries due in the IST minute `at` (interval entries are never listed: they fire
/// relative to Beat's own start).
///
/// # Errors
/// A field Celery would refuse.
pub fn due_at(at: NaiveDateTime) -> Result<Vec<&'static BeatEntry>, String> {
    let mut out = Vec::new();
    for entry in BEAT_SCHEDULE {
        if let Schedule::Crontab(c) = entry.schedule
            && c.is_due(at)?
        {
            out.push(entry);
        }
    }
    Ok(out)
}

/// `BEAT_SCHEDULE`, in Python's order.
pub static BEAT_SCHEDULE: &[BeatEntry] = &[
    BeatEntry {
        name: "capture-kite-trades",
        task: "baskfy.pipeline.capture_kite_trades",
        schedule: Schedule::Crontab(Crontab {
            minute: "50",
            hour: "15",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "options-contract-master",
        task: "baskfy.options.refresh_master",
        schedule: Schedule::Crontab(Crontab {
            minute: "30",
            hour: "19",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "options-collect-chain",
        task: "baskfy.options.collect_chain",
        schedule: Schedule::Crontab(Crontab {
            minute: "*",
            hour: "9-15",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: Some(55),
            countdown: None,
        },
    },
    BeatEntry {
        name: "options-index-bars",
        task: "baskfy.options.index_bars",
        schedule: Schedule::Crontab(Crontab {
            minute: "*",
            hour: "9-15",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: Some(55),
            countdown: None,
        },
    },
    BeatEntry {
        name: "options-scan",
        task: "baskfy.options.scan",
        schedule: Schedule::Crontab(Crontab {
            minute: "*",
            hour: "9-15",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: Some(55),
            countdown: Some(20),
        },
    },
    BeatEntry {
        name: "options-plan-o1",
        task: "baskfy.options.plan_o1",
        schedule: Schedule::Crontab(Crontab {
            minute: "0-16",
            hour: "10",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: Some(55),
            countdown: Some(40),
        },
    },
    BeatEntry {
        name: "options-plan-o2",
        task: "baskfy.options.plan_o2",
        schedule: Schedule::Crontab(Crontab {
            minute: "*",
            hour: "9-13",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: Some(55),
            countdown: Some(45),
        },
    },
    BeatEntry {
        name: "options-plan-o3",
        task: "baskfy.options.plan_o3",
        schedule: Schedule::Crontab(Crontab {
            minute: "*",
            hour: "9-13",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: Some(55),
            countdown: Some(45),
        },
    },
    BeatEntry {
        name: "options-checks",
        task: "baskfy.options.checks",
        schedule: Schedule::Crontab(Crontab {
            minute: "3,20,33,35,48",
            hour: "9-15",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: Some(55),
            countdown: None,
        },
    },
    BeatEntry {
        name: "options-weekly",
        task: "baskfy.options.weekly",
        schedule: Schedule::Crontab(Crontab {
            minute: "30",
            hour: "16",
            day_of_week: "fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "eq-bars-session",
        task: "baskfy.eq_bars.session",
        schedule: Schedule::Crontab(Crontab {
            minute: "45",
            hour: "15",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Ingest,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "options-index-bars-eod",
        task: "baskfy.options.index_bars_eod",
        schedule: Schedule::Crontab(Crontab {
            minute: "45",
            hour: "15",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "fno-bhavcopy",
        task: "baskfy.fno.ingest_bhavcopy",
        schedule: Schedule::Crontab(Crontab {
            minute: "30",
            hour: "18-23",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "fno-index-daily",
        task: "baskfy.fno.index_daily",
        schedule: Schedule::Crontab(Crontab {
            minute: "15",
            hour: "18",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "fno-retest",
        task: "baskfy.fno.retest",
        schedule: Schedule::Crontab(Crontab {
            minute: "0",
            hour: "6",
            day_of_week: "sat",
            day_of_month: "1-7",
            month_of_year: "1,4,7,10",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Compute,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "fno-weekly",
        task: "baskfy.fno.weekly",
        schedule: Schedule::Crontab(Crontab {
            minute: "35",
            hour: "16",
            day_of_week: "fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "fno-alerts",
        task: "baskfy.fno.alerts",
        schedule: Schedule::Crontab(Crontab {
            minute: "*/5",
            hour: "9-23",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: Some(240),
            countdown: None,
        },
    },
    BeatEntry {
        name: "fno-spread-sample",
        task: "baskfy.fno.spread_sample",
        schedule: Schedule::Crontab(Crontab {
            minute: "0",
            hour: "15",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: Some(600),
            countdown: None,
        },
    },
    BeatEntry {
        name: "bhavcopy-eod",
        task: "baskfy.pipeline.bhavcopy_ingest",
        schedule: Schedule::Crontab(Crontab {
            minute: "15",
            hour: "18",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "refresh-reference-data",
        task: "baskfy.pipeline.nightly",
        schedule: Schedule::Crontab(Crontab {
            minute: "45",
            hour: "18",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "session-catch-up",
        task: "baskfy.pipeline.session_catch_up",
        schedule: Schedule::Crontab(Crontab {
            minute: "45",
            hour: "6",
            day_of_week: "mon-sat",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "purge-deleted-accounts",
        task: "baskfy.accounts.purge",
        schedule: Schedule::Crontab(Crontab {
            minute: "0",
            hour: "3",
            day_of_week: "*",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "weekly-integrity-audit",
        task: "baskfy.pipeline.integrity_audit",
        schedule: Schedule::Crontab(Crontab {
            minute: "0",
            hour: "2",
            day_of_week: "sat",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Compute,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "reap-abandoned-pipeline-runs",
        task: "baskfy.ops.reap_abandoned_runs",
        schedule: Schedule::Crontab(Crontab {
            minute: "*/15",
            hour: "*",
            day_of_week: "*",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "publish-deadline-slo",
        task: "baskfy.ops.check_publish_deadline",
        schedule: Schedule::Crontab(Crontab {
            minute: "15",
            hour: "20",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "kite-token-expiry",
        task: "baskfy.ops.check_kite_token",
        schedule: Schedule::Crontab(Crontab {
            minute: "5",
            hour: "*",
            day_of_week: "*",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "queue-backlog",
        task: "baskfy.ops.check_queue_backlog",
        schedule: Schedule::Crontab(Crontab {
            minute: "*/10",
            hour: "*",
            day_of_week: "*",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "dispatch-screen-alerts",
        task: "baskfy.alerts.dispatch",
        schedule: Schedule::Crontab(Crontab {
            minute: "30",
            hour: "20",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "sweep-webhook-deliveries",
        task: "baskfy.alerts.sweep_webhooks",
        schedule: Schedule::Crontab(Crontab {
            minute: "*/2",
            hour: "*",
            day_of_week: "*",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "cb-eod-metrics",
        task: "baskfy.cb.compute_metrics",
        schedule: Schedule::Crontab(Crontab {
            minute: "20",
            hour: "20",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Compute,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "cb-sip-reminders",
        task: "baskfy.cb.sip_reminders",
        schedule: Schedule::Crontab(Crontab {
            minute: "0",
            hour: "9",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Compute,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "cb-dividends",
        task: "baskfy.cb.derive_dividends",
        schedule: Schedule::Crontab(Crontab {
            minute: "25",
            hour: "20",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Compute,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "cb-sync-batches",
        task: "baskfy.cb.sync_batches",
        schedule: Schedule::Crontab(Crontab {
            minute: "*/15",
            hour: "*",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Compute,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "portfolio-eod-nav",
        task: "baskfy.portfolio.eod_nav",
        schedule: Schedule::Crontab(Crontab {
            minute: "35",
            hour: "20",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Compute,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "swing-eod",
        task: "baskfy.swing.detect",
        schedule: Schedule::Crontab(Crontab {
            minute: "0",
            hour: "21",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Compute,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "swing-eod-plan",
        task: "baskfy.swing.eod",
        schedule: Schedule::Crontab(Crontab {
            minute: "5",
            hour: "21",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Compute,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "vbt-detect",
        task: "baskfy.vbt.detect",
        schedule: Schedule::Crontab(Crontab {
            minute: "10",
            hour: "21",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Compute,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "twt-detect",
        task: "baskfy.twt.detect",
        schedule: Schedule::Crontab(Crontab {
            minute: "0",
            hour: "21",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Compute,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "twt-evening",
        task: "baskfy.twt.evening",
        schedule: Schedule::Crontab(Crontab {
            minute: "20",
            hour: "21",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Compute,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "twt-morning",
        task: "baskfy.twt.morning",
        schedule: Schedule::Crontab(Crontab {
            minute: "5",
            hour: "9",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Compute,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "vbt-evening",
        task: "baskfy.vbt.evening",
        schedule: Schedule::Crontab(Crontab {
            minute: "15",
            hour: "21",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Compute,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "vbt-morning",
        task: "baskfy.vbt.morning",
        schedule: Schedule::Crontab(Crontab {
            minute: "0",
            hour: "9",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Compute,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "swing-weekend",
        task: "baskfy.swing.weekend",
        schedule: Schedule::Crontab(Crontab {
            minute: "0",
            hour: "7",
            day_of_week: "sat",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Compute,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "swing-premarket-levels",
        task: "baskfy.swing.premarket",
        schedule: Schedule::Crontab(Crontab {
            minute: "50",
            hour: "8",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[("stage", "LEVELS")],
        options: BeatOptions {
            queue: Queue::Compute,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "swing-premarket-gaps",
        task: "baskfy.swing.premarket",
        schedule: Schedule::Crontab(Crontab {
            minute: "16",
            hour: "9",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[("stage", "GAPS")],
        options: BeatOptions {
            queue: Queue::Compute,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "swing-timing-probe",
        task: "baskfy.swing.timing_probe",
        schedule: Schedule::Crontab(Crontab {
            minute: "4",
            hour: "9",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Compute,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "swing-check-monitor-started",
        task: "baskfy.swing.check_monitor_started",
        schedule: Schedule::Crontab(Crontab {
            minute: "20",
            hour: "9",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "swing-check-orders-after-cutoff",
        task: "baskfy.swing.check_orders_after_cutoff",
        schedule: Schedule::Crontab(Crontab {
            minute: "50",
            hour: "10",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "swing-check-gtt-at-1515",
        task: "baskfy.swing.check_gtt_at_1515",
        schedule: Schedule::Crontab(Crontab {
            minute: "20",
            hour: "15",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "swing-check-detect-fresh",
        task: "baskfy.swing.check_detect_fresh",
        schedule: Schedule::Crontab(Crontab {
            minute: "30",
            hour: "21",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "vbt-rescan-sweep",
        task: "baskfy.vbt.rescan_sweep",
        schedule: Schedule::Crontab(Crontab {
            minute: "*",
            hour: "*",
            day_of_week: "*",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "twt-scan-publish",
        task: "baskfy.twt.scan_publish",
        schedule: Schedule::Every { seconds: 60 },
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "vbt-check-detect-fresh",
        task: "baskfy.vbt.check_detect_fresh",
        schedule: Schedule::Crontab(Crontab {
            minute: "30",
            hour: "21",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "vbt-check-orders-past-expiry",
        task: "baskfy.vbt.check_orders_past_expiry",
        schedule: Schedule::Crontab(Crontab {
            minute: "40",
            hour: "21",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "vbt-check-naked-positions",
        task: "baskfy.vbt.check_naked_positions",
        schedule: Schedule::Crontab(Crontab {
            minute: "40",
            hour: "21",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "vbt-check-positions-without-bars",
        task: "baskfy.vbt.check_positions_without_bars",
        schedule: Schedule::Crontab(Crontab {
            minute: "40",
            hour: "21",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "swing-catalyst",
        task: "baskfy.swing.catalyst",
        schedule: Schedule::Crontab(Crontab {
            minute: "17",
            hour: "9",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Compute,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "swing-scan-sweep",
        task: "baskfy.swing.scan_sweep",
        schedule: Schedule::Every { seconds: 60 },
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Default,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "kite-login-nudge",
        task: "baskfy.kite.login_nudge",
        schedule: Schedule::Crontab(Crontab {
            minute: "45",
            hour: "8",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[("window", "first")],
        options: BeatOptions {
            queue: Queue::Compute,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "kite-login-nudge-second",
        task: "baskfy.kite.login_nudge",
        schedule: Schedule::Crontab(Crontab {
            minute: "5",
            hour: "9",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[("window", "second")],
        options: BeatOptions {
            queue: Queue::Compute,
            expires: None,
            countdown: None,
        },
    },
    BeatEntry {
        name: "cb-rebalance-notify",
        task: "baskfy.cb.rebalance_notify",
        schedule: Schedule::Crontab(Crontab {
            minute: "30",
            hour: "9",
            day_of_week: "mon-fri",
            day_of_month: "*",
            month_of_year: "*",
        }),
        kwargs: &[],
        options: BeatOptions {
            queue: Queue::Compute,
            expires: None,
            countdown: None,
        },
    },
];
