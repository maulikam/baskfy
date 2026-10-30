//! An inclusive date window. Ports `baskfy_worker.window.DateWindow`.
//!
//! A value object rather than a pair of loose arguments because the same window is threaded
//! through the bar fetch, the backfill cursor and the adjustment rebuild, and a silently swapped
//! `start`/`end` is the kind of bug that produces an empty result instead of an error.

use chrono::NaiveDate;

use crate::error::WorkerError;

/// `DateWindow(start, end)`, both ends included.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DateWindow {
    start: NaiveDate,
    end: NaiveDate,
}

impl DateWindow {
    /// A window, refusing an end before its start (`ValueError`, Python's message).
    ///
    /// # Errors
    /// `ValueError` when `end < start`.
    pub fn new(start: NaiveDate, end: NaiveDate) -> Result<Self, WorkerError> {
        if end < start {
            return Err(WorkerError::python(
                "ValueError",
                format!("window end {end} precedes start {start}"),
            ));
        }
        Ok(Self { start, end })
    }

    /// `DateWindow.single(day)`.
    #[must_use]
    pub fn single(day: NaiveDate) -> Self {
        Self {
            start: day,
            end: day,
        }
    }

    /// The first day.
    #[must_use]
    pub fn start(&self) -> NaiveDate {
        self.start
    }

    /// The last day.
    #[must_use]
    pub fn end(&self) -> NaiveDate {
        self.end
    }

    /// `days`: the number of calendar days covered.
    #[must_use]
    pub fn days(&self) -> i64 {
        (self.end - self.start).num_days() + 1
    }
}

impl std::fmt::Display for DateWindow {
    /// `start..end`, ISO dates (`__str__`).
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "{}..{}",
            self.start.format("%Y-%m-%d"),
            self.end.format("%Y-%m-%d")
        )
    }
}
