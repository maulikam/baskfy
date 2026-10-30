//! Ports `baskfy_worker.fno.backfill`: the resumable F&O bhavcopy backfill from 2022-01-03
//! (`docs/fno/06` FO2). A session already `INGESTED` is skipped without a read; each session is
//! checkpointed (committed) as it lands; `--seed-archive DIR` copies raw zips already on disk into
//! the provider's archive first (never overwriting one). No task calls it — `fno_cli backfill`
//! (lane `clis`) does — so it is `pub(crate)` for that lane.

use std::path::Path;

use baskfy_db::OrderedJson;
use baskfy_providers::archive::{RawArchive, archive_key};
use baskfy_providers::nse::KIND_FO_BHAVCOPY;
use chrono::{NaiveDate, NaiveDateTime};
use sqlx::PgConnection;

use super::common::iso;
use super::ingest::{
    FoBhavcopyReader, STATUS_INGESTED, STATUS_MISSING, ingest_day, ingested_days, is_final_attempt,
};
use crate::error::WorkerError;
use crate::pyjson::{object, strings};

/// `BACKFILL_START`.
pub(crate) fn backfill_start() -> NaiveDate {
    NaiveDate::from_ymd_opt(2022, 1, 3).unwrap_or_default()
}

/// `BackfillReport`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct BackfillReport {
    pub(crate) sessions: i64,
    pub(crate) already_present: i64,
    pub(crate) ingested: i64,
    pub(crate) missing: Vec<NaiveDate>,
    pub(crate) pending: Vec<NaiveDate>,
    pub(crate) seeded_from_archive: i64,
    pub(crate) rows_kept: i64,
}

impl BackfillReport {
    /// `as_dict()`.
    pub(crate) fn as_dict(&self) -> OrderedJson {
        object([
            ("sessions", OrderedJson::Int(self.sessions)),
            ("already_present", OrderedJson::Int(self.already_present)),
            ("ingested", OrderedJson::Int(self.ingested)),
            ("missing", strings(self.missing.iter().map(|d| iso(*d)))),
            ("pending", strings(self.pending.iter().map(|d| iso(*d)))),
            (
                "seeded_from_archive",
                OrderedJson::Int(self.seeded_from_archive),
            ),
            ("rows_kept", OrderedJson::Int(self.rows_kept)),
        ])
    }
}

/// `seed_archive_day`: copy `day`'s raw zip from `seed_dir` (the archive root, `nse/`, or the
/// `fo-bhavcopy` directory) into `archive` if it is there and not yet archived.
pub(crate) async fn seed_archive_day(
    archive: &dyn RawArchive,
    seed_dir: &Path,
    day: NaiveDate,
) -> Result<bool, WorkerError> {
    let key = archive_key(KIND_FO_BHAVCOPY, day, "zip")?;
    if archive.exists(&key).await? {
        return Ok(false);
    }
    let name = Path::new(&key)
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    for candidate in [
        seed_dir.join(&key),
        seed_dir.join(KIND_FO_BHAVCOPY).join(&name),
        seed_dir.join(&name),
    ] {
        if candidate.is_file() {
            let bytes = std::fs::read(&candidate)
                .map_err(|e| WorkerError::python("OSError", e.to_string()))?;
            archive.put(&key, &bytes, "application/zip").await?;
            return Ok(true);
        }
    }
    Ok(false)
}

/// `backfill_days`: ingest every session in `days` not already `INGESTED`; `checkpoint` after
/// each (the CLI commits there).
pub(crate) async fn backfill_days<R: FoBhavcopyReader>(
    conn: &mut PgConnection,
    reader: &R,
    days: &[NaiveDate],
    seed: Option<(&dyn RawArchive, &Path)>,
    now_wall: NaiveDateTime,
    mut checkpoint: impl AsyncFnMut(&mut PgConnection) -> Result<(), WorkerError>,
) -> Result<BackfillReport, WorkerError> {
    let mut report = BackfillReport {
        sessions: i64::try_from(days.len()).unwrap_or(i64::MAX),
        ..BackfillReport::default()
    };
    let present = ingested_days(conn, days).await?;
    for day in days {
        if present.contains(day) {
            report.already_present += 1;
            continue;
        }
        if let Some((archive, dir)) = seed {
            report.seeded_from_archive += i64::from(seed_archive_day(archive, dir, *day).await?);
        }
        let outcome = ingest_day(conn, reader, *day, is_final_attempt(*day, now_wall)).await?;
        if outcome.status == STATUS_INGESTED {
            report.ingested += 1;
            report.rows_kept += outcome.rows_kept.unwrap_or(0);
        } else if outcome.status == STATUS_MISSING {
            report.missing.push(*day);
        } else {
            report.pending.push(*day);
        }
        checkpoint(conn).await?;
    }
    Ok(report)
}
