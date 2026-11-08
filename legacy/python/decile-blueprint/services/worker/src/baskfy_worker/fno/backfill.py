"""The resumable F&O bhavcopy backfill from 2022-01-03 (``docs/fno/06`` FO2).

One NSE request per session at the NSE limiter (about an hour and a half on the Mac, measured
23 Sep 2026), each session committed as it lands so an interrupted run keeps what it wrote.
**Resumable**: a session already ``INGESTED`` in ``fo_ingest_day`` is skipped without a read, so
a re-run picks up where the last one stopped and a finished range re-runs as a no-op.

``--seed-archive DIR`` copies raw zips already downloaded (the research's
``~/baskfy-research/fno/archive``, laid out exactly as the provider's archive,
``nse/fo-bhavcopy/YYYY-MM-DD.zip``) into the provider's archive before a session is read, so NSE is
not asked again for a file already on disk. A copied file is parsed through the same provider path
as a fetched one — the wrong-date refusal included — so a mislabelled zip is refused, not written.
Nothing already in the provider's archive is overwritten (docs/09: the archive is the record).
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models.base import JsonObject
from baskfy_providers.archive import RawArchive, archive_key
from baskfy_providers.nse import KIND_FO_BHAVCOPY
from baskfy_worker.celery_app import IST
from baskfy_worker.fno.ingest import (
    STATUS_INGESTED,
    STATUS_MISSING,
    FoBhavcopyReader,
    ingest_day,
    ingested_days,
    is_final_attempt,
)

log = logging.getLogger("baskfy_worker.fno.backfill")

#: ``06`` FO2: "The resumable CLI backfill from 2022-01-03".
BACKFILL_START = dt.date(2022, 1, 3)


@dataclass(slots=True)
class BackfillReport:
    sessions: int = 0
    already_present: int = 0
    ingested: int = 0
    missing: list[dt.date] = field(default_factory=list)
    pending: list[dt.date] = field(default_factory=list)
    seeded_from_archive: int = 0
    rows_kept: int = 0

    def as_dict(self) -> JsonObject:
        return {
            "sessions": self.sessions,
            "already_present": self.already_present,
            "ingested": self.ingested,
            "missing": [d.isoformat() for d in self.missing],
            "pending": [d.isoformat() for d in self.pending],
            "seeded_from_archive": self.seeded_from_archive,
            "rows_kept": self.rows_kept,
        }


def seed_archive_day(archive: RawArchive, seed_dir: Path, day: dt.date) -> bool:
    """Copy ``day``'s raw zip from ``seed_dir`` into ``archive`` if it is there and not yet
    archived. Returns whether a file was copied.

    ``seed_dir`` may be the archive root (holding ``nse/``), ``nse/`` itself, or the
    ``fo-bhavcopy`` directory.
    """
    key = archive_key(KIND_FO_BHAVCOPY, day, "zip")
    if archive.exists(key):
        return False
    name = Path(key).name
    for candidate in (seed_dir / key, seed_dir / KIND_FO_BHAVCOPY / name, seed_dir / name):
        if candidate.is_file():
            archive.put(key, candidate.read_bytes(), content_type="application/zip")
            return True
    return False


async def backfill_days(  # noqa: PLR0913 - the session, the reader, the days and three options
    session: AsyncSession,
    reader: FoBhavcopyReader,
    days: Sequence[dt.date],
    *,
    archive: RawArchive | None = None,
    seed_dir: Path | None = None,
    checkpoint: Callable[[], Awaitable[None]] | None = None,
    now_ist: dt.datetime | None = None,
) -> BackfillReport:
    """Ingest every session in ``days`` not already ``INGESTED``; ``checkpoint`` after each."""
    now = now_ist or dt.datetime.now(tz=IST)
    report = BackfillReport(sessions=len(days))
    present = await ingested_days(session, days)
    for index, day in enumerate(days, start=1):
        if day in present:
            report.already_present += 1
            continue
        if archive is not None and seed_dir is not None:
            report.seeded_from_archive += int(seed_archive_day(archive, seed_dir, day))
        outcome = await ingest_day(session, reader, day, final_attempt=is_final_attempt(day, now))
        if outcome.status == STATUS_INGESTED:
            report.ingested += 1
            report.rows_kept += outcome.rows_kept or 0
        elif outcome.status == STATUS_MISSING:
            report.missing.append(day)
        else:
            report.pending.append(day)
        if checkpoint is not None:
            await checkpoint()
        if index % 50 == 0:
            log.info("fno backfill: %d/%d sessions (%s)", index, len(days), day.isoformat())
    return report
