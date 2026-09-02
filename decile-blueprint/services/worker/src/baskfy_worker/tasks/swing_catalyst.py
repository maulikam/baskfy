"""SW11B: the catalyst feed — a headline, a stamp and a link per watched name (A3).

`docs/swing/STANDING-ANSWERS.md` A3, in its own words: "free NSE corporate-announcement feed,
watchlist + EP-candidate symbols only, via the existing NSE provider and its limiter; store
headline + timestamp + filing URL; auto-fill `sw_watch.catalyst`; link out, do not reproduce
text; add an earnings-date flag from the results calendar; **fail soft** (empty catalyst
allowed, a crashed pre-open scan not)".

WHAT RUNS AT 09:10
------------------
1. The symbols: every WATCHING `sw_watch` row (the funnel's top 20 flags + every EP, the live
   gaps the 09:09 scan just added, and Maulik's MANUAL names) plus the last session's EP rows
   in `sw_setup_daily` — the day's EP candidates, whether or not they were watched. Nothing
   wider: the limiter is 1 req/s and the monitor starts at 09:16.
2. Per symbol, `announcements` and `results_calendar` through the NSE provider — the cookie
   prime, the limiter, archive-then-parse. **Per symbol, not per batch**, so one name NSE
   refuses does not cost the other twenty their links.
3. `sw_catalyst` upserted on `(user_id, instrument_id, url)`; an announcement is a row with a
   stamp, a result meeting is a row with `earnings_date` linking to the exchange's calendar.
4. `sw_watch.catalyst` set to the newest headline **only while it is empty** — typed text is
   the person's and is never overwritten; `sw_watch.earnings_date` refreshed every run.

FAIL SOFT
---------
A provider error on one symbol is a count in the step's note and a skipped symbol; a provider
error on every symbol is a SUCCEEDED step with zero rows and the error's text in the note.
Nothing here raises a `ProviderError` into the morning, and nothing here catches anything
else: a bug is still a bug, and house rule 3 says it must be seen.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import Instrument
from baskfy_core.models.swing import SwCatalyst, SwSetupDaily, SwWatch
from baskfy_core.swing.config import Setup
from baskfy_providers.errors import ProviderError
from baskfy_providers.nse import CATALYST_SOURCE_ANNOUNCEMENT, CATALYST_SOURCE_EVENT_CALENDAR
from baskfy_providers.records import CatalystRecord, EarningsDateRecord
from baskfy_worker.steps import StepOutcome
from baskfy_worker.tasks.swing import recent_trading_days

log = logging.getLogger(__name__)

#: `sw_watch.state` the feed serves. TRIGGERED names are positions now and the book's; EXPIRED
#: and DISMISSED ones are history.
WATCHING: Final = "WATCHING"


class CatalystSource(Protocol):
    """Where the filings come from. Production: ``NSEProvider``; tests: a fake."""

    def announcements(self, symbols: Sequence[str], *, on: dt.date) -> list[CatalystRecord]: ...

    def results_calendar(
        self, symbols: Sequence[str], *, on: dt.date
    ) -> list[EarningsDateRecord]: ...


@dataclass
class CatalystReport:
    """What the morning's feed did — the step payload."""

    session_date: dt.date
    symbols: int = 0
    announcements: int = 0
    earnings_dates: int = 0
    rows_written: int = 0
    catalysts_filled: int = 0
    earnings_flagged: int = 0
    #: Symbols whose read raised the provider's own error — skipped, counted, named.
    failed: list[str] = field(default_factory=list)
    provider_error: str | None = None
    skipped_reason: str | None = None

    def as_detail(self) -> dict[str, object]:
        return {
            "session_date": self.session_date.isoformat(),
            "symbols": self.symbols,
            "announcements": self.announcements,
            "earnings_dates": self.earnings_dates,
            "rows_written": self.rows_written,
            "catalysts_filled": self.catalysts_filled,
            "earnings_flagged": self.earnings_flagged,
            "failed": list(self.failed),
            "provider_error": self.provider_error,
            "skipped_reason": self.skipped_reason,
        }


async def feed_symbols(
    session: AsyncSession, *, user_id: int, session_date: dt.date
) -> dict[str, int]:
    """``symbol -> instrument_id`` for the WATCHING rows and the last session's EP candidates."""
    watched = await session.execute(
        select(Instrument.symbol, Instrument.id)
        .join(SwWatch, SwWatch.instrument_id == Instrument.id)
        .where(SwWatch.user_id == user_id, SwWatch.state == WATCHING)
    )
    symbols: dict[str, int] = {str(symbol): int(instrument_id) for symbol, instrument_id in watched}

    sessions = await recent_trading_days(session, session_date - dt.timedelta(days=1), 1)
    if sessions:
        candidates = await session.execute(
            select(Instrument.symbol, Instrument.id)
            .join(SwSetupDaily, SwSetupDaily.instrument_id == Instrument.id)
            .where(
                SwSetupDaily.user_id == user_id,
                SwSetupDaily.date == sessions[-1],
                SwSetupDaily.setup == Setup.EP.value,
            )
        )
        for symbol, instrument_id in candidates:
            symbols.setdefault(str(symbol), int(instrument_id))
    return dict(sorted(symbols.items()))


def nearest_result(
    records: Sequence[EarningsDateRecord], *, on_or_after: dt.date
) -> EarningsDateRecord | None:
    """The soonest result meeting on or after the session — the flag. Past meetings are not."""
    upcoming = [record for record in records if record.event_date >= on_or_after]
    return min(upcoming, key=lambda record: record.event_date) if upcoming else None


async def upsert_catalysts(
    session: AsyncSession,
    *,
    user_id: int,
    instrument_id: int,
    announcements: Sequence[CatalystRecord],
    result: EarningsDateRecord | None,
) -> int:
    """One symbol's rows, idempotently. A URL already stored is refreshed, never duplicated."""
    payload: list[dict[str, object]] = [
        {
            "user_id": user_id,
            "instrument_id": instrument_id,
            "headline": record.headline,
            "published_at": record.published_at,
            "url": record.url,
            "source": CATALYST_SOURCE_ANNOUNCEMENT,
            "earnings_date": None,
        }
        for record in announcements
    ]
    if result is not None:
        payload.append(
            {
                "user_id": user_id,
                "instrument_id": instrument_id,
                "headline": result.purpose,
                "published_at": None,
                "url": result.url,
                "source": CATALYST_SOURCE_EVENT_CALENDAR,
                "earnings_date": result.event_date,
            }
        )
    # The same URL twice in one answer (NSE does re-list a filing) must collapse before the
    # statement, or Postgres refuses to touch one row twice in one INSERT ... ON CONFLICT.
    by_url: dict[str, dict[str, object]] = {}
    for row in payload:
        by_url.setdefault(str(row["url"]), row)
    if result is None:
        # No result meeting ahead: a calendar row from an earlier morning would be a stale
        # flag, and a link to a calendar with no date on it is not a catalyst.
        await session.execute(
            delete(SwCatalyst).where(
                SwCatalyst.user_id == user_id,
                SwCatalyst.instrument_id == instrument_id,
                SwCatalyst.source == CATALYST_SOURCE_EVENT_CALENDAR,
            )
        )
    if not by_url:
        return 0
    statement = insert(SwCatalyst).values(list(by_url.values()))
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[SwCatalyst.user_id, SwCatalyst.instrument_id, SwCatalyst.url],
            set_={
                "headline": statement.excluded.headline,
                "published_at": statement.excluded.published_at,
                "source": statement.excluded.source,
                "earnings_date": statement.excluded.earnings_date,
            },
        )
    )
    return len(by_url)


async def fill_watch(
    session: AsyncSession,
    *,
    user_id: int,
    instrument_id: int,
    newest: CatalystRecord | None,
    result: EarningsDateRecord | None,
) -> tuple[int, int]:
    """Auto-fill the empty catalyst fields and refresh the earnings flag on the watch rows.

    ``catalyst`` is written only where it is NULL or blank — a typed note is the person's.
    ``earnings_date`` is a flag, not a note, and is set (or cleared) on every run.
    """
    # ``RETURNING id`` rather than ``rowcount``: the async result exposes no reliable count.
    filled = 0
    if newest is not None:
        outcome = await session.execute(
            update(SwWatch)
            .where(
                SwWatch.user_id == user_id,
                SwWatch.instrument_id == instrument_id,
                SwWatch.state == WATCHING,
                (SwWatch.catalyst.is_(None)) | (SwWatch.catalyst == ""),
            )
            .values(catalyst=newest.headline)
            .returning(SwWatch.id)
        )
        filled = len(outcome.all())
    flagged = await session.execute(
        update(SwWatch)
        .where(
            SwWatch.user_id == user_id,
            SwWatch.instrument_id == instrument_id,
            SwWatch.state == WATCHING,
        )
        .values(earnings_date=result.event_date if result is not None else None)
        .returning(SwWatch.id)
    )
    return filled, len(flagged.all()) if result is not None else 0


async def run_swing_catalyst(  # noqa: PLR0913 - one keyword per input the feed depends on
    session: AsyncSession,
    outcome: StepOutcome,
    session_date: dt.date,
    *,
    user_id: int,
    provider: CatalystSource | None,
    now: dt.datetime | None = None,
) -> CatalystReport:
    """The 09:10 job. Never raises a provider error; never places anything anywhere."""
    del now  # the archive is keyed by `session_date`; the clock is not consulted
    report = CatalystReport(session_date=session_date)
    symbols = await feed_symbols(session, user_id=user_id, session_date=session_date)
    report.symbols = len(symbols)
    if not symbols:
        report.skipped_reason = "nothing watched and no EP candidate for the last session"
        outcome.note(**report.as_detail())
        return report
    if provider is None:
        report.skipped_reason = "no catalyst provider configured"
        outcome.note(**report.as_detail())
        return report

    for symbol, instrument_id in symbols.items():
        try:
            announcements = provider.announcements([symbol], on=session_date)
            meetings = provider.results_calendar([symbol], on=session_date)
        except ProviderError as exc:
            # Fail soft (A3): the name keeps whatever it had, the morning goes on.
            log.warning("catalyst feed: %s skipped: %s", symbol, exc)
            report.failed.append(symbol)
            report.provider_error = str(exc)
            continue
        result = nearest_result(meetings, on_or_after=session_date)
        report.announcements += len(announcements)
        report.earnings_dates += 1 if result is not None else 0
        report.rows_written += await upsert_catalysts(
            session,
            user_id=user_id,
            instrument_id=instrument_id,
            announcements=announcements,
            result=result,
        )
        # The provider sorts newest first, undated last; an undated headline still beats none.
        newest = announcements[0] if announcements else None
        filled, flagged = await fill_watch(
            session, user_id=user_id, instrument_id=instrument_id, newest=newest, result=result
        )
        report.catalysts_filled += filled
        report.earnings_flagged += flagged

    outcome.rows_in = report.symbols
    outcome.rows_out = report.rows_written
    outcome.note(**report.as_detail())
    return report


__all__ = [
    "CatalystReport",
    "CatalystSource",
    "feed_symbols",
    "fill_watch",
    "nearest_result",
    "run_swing_catalyst",
    "upsert_catalysts",
]
