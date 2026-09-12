"""Trading-day awareness (Prompt 3 deliverable 7).

    "Trading-day awareness everywhere: never attempt to ingest or compute for a non-trading day."

docs/09 §Schedule: "an NSE holiday calendar table (`trading_day(date, is_open)`), refreshed
annually from the exchange circular and **asserted against observed bar dates**."

That last clause is this module's other job. ``docs/04a-trading-day-addendum.md`` records that the
seeded calendar is *provisional*: it contains only the deterministic holidays, because India's
lunar-calendar holidays are declared per year by circular and are not in the bundle. Every weekday
we have not seen data for is therefore marked ``derived`` — a guess. :func:`reconcile_calendar`
is what turns those guesses into facts, and CLAUDE.md carries it as an open item assigned to this
prompt.

Two directions of inference, and only one of them is safe from a single date:

    a bar exists on D               -> D was certainly a trading day.       (always applied)
    no instrument has a bar on D    -> D was *probably* a holiday.          (needs a universe)

The second is only sound with a universe-wide view and a populated backfill, which is why it is
gated behind an explicit minimum: a date with no bars because the backfill has not reached it yet
must not be recorded as an exchange holiday.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import OhlcvDaily, TradingDay
from baskfy_core.models.reference import INFERRED_HOLIDAY_NAME
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_providers.publication import PublicationCheck

#: Below this many instruments with bars, "no bars" means "no backfill", not "holiday".
MIN_INSTRUMENTS_FOR_HOLIDAY_INFERENCE: Final = 20

#: 12-month factor windows (docs/05) need the lunar holidays in the year *behind* ``as_of``,
#: not only the trade date the nightly run just ingested. 400 calendar days covers 12 months
#: plus snap slack; a single-day reconcile left those holidays as ``derived`` and 9M/12M
#: windows resolved long (T9.2).
CALENDAR_LOOKBACK_DAYS: Final = 400


class NotATradingDay(Exception):
    """Raised when work is attempted for a date the exchange was closed.

    Not a failure of the pipeline — a weekend run should stop cleanly, not error — so the
    orchestrator catches this specifically and records the run as skipped.
    """

    def __init__(self, date: dt.date, reason: str) -> None:
        super().__init__(f"{date.isoformat()} is not an NSE trading day: {reason}")
        self.date = date
        self.reason = reason


@dataclass(frozen=True, slots=True)
class CalendarVerdict:
    date: dt.date
    is_trading_day: bool
    source: str
    holiday_name: str | None = None

    @property
    def is_provisional(self) -> bool:
        """True while the answer rests on the seed list rather than on observed market data."""
        return self.source in ("holiday", "derived")


async def classify(session: AsyncSession, date: dt.date) -> CalendarVerdict:
    """What does the calendar say about ``date``?"""
    row = (
        await session.execute(
            select(TradingDay).where(
                TradingDay.exchange_id == NSE_EXCHANGE_ID, TradingDay.date == date
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotATradingDay(date, "outside the loaded calendar range; run `make seed`")
    return CalendarVerdict(date, row.is_trading_day, row.source, row.holiday_name)


async def require_trading_day(session: AsyncSession, date: dt.date) -> CalendarVerdict:
    """The guard every task calls before touching data for ``date``."""
    verdict = await classify(session, date)
    if not verdict.is_trading_day:
        raise NotATradingDay(date, verdict.holiday_name or verdict.source)
    return verdict


async def previous_trading_days(
    session: AsyncSession, before: dt.date, count: int
) -> list[dt.date]:
    """The ``count`` trading days immediately before ``before``, most recent first.

    Used by the quality gate's rolling-window assertions (docs/09 assertions 1 and 8), which are
    defined over "the last 10 trading days" — calendar days would silently include weekends and
    make the baseline wrong every Monday.
    """
    rows = await session.execute(
        select(TradingDay.date)
        .where(
            TradingDay.exchange_id == NSE_EXCHANGE_ID,
            TradingDay.is_trading_day.is_(True),
            TradingDay.date < before,
        )
        .order_by(TradingDay.date.desc())
        .limit(count)
    )
    return [row[0] for row in rows]


#: "Did NSE publish a bhavcopy for this date?" — the question that separates a holiday from a
#: failed ingest. Defined in `baskfy_providers.publication` so the API service can ask it too
#: (leaf 3.1's resync detector) without importing the worker; re-exported here because this is
#: where every existing caller expects to find it.
__all__ = [
    "CALENDAR_LOOKBACK_DAYS",
    "MIN_INSTRUMENTS_FOR_HOLIDAY_INFERENCE",
    "CalendarVerdict",
    "NotATradingDay",
    "PublicationCheck",
    "ReconciliationResult",
    "classify",
    "mark_inferred_holiday",
    "previous_trading_days",
    "reconcile_calendar",
    "require_trading_day",
]


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    confirmed: int
    inferred_holidays: int
    still_provisional: int
    #: Weekdays with no bars that were NOT called holidays, because NSE published a file for
    #: them. Each one is a session we failed to ingest — surfaced so it is loud, not silent.
    missed_sessions: tuple[dt.date, ...] = ()


async def reconcile_calendar(
    session: AsyncSession,
    start: dt.date,
    end: dt.date,
    *,
    infer_holidays: bool = True,
    published: PublicationCheck | None = None,
) -> ReconciliationResult:
    """Assert the calendar against observed bar dates (docs/09 §Schedule).

    Promotes every date with bars to ``source='bhavcopy'`` — an observed fact that outranks the
    seed list and is never demoted by a re-seed.

    When ``infer_holidays`` is set and the range is densely populated, weekdays with no bars at
    all are recorded as inferred holidays. This is what fills in the lunar-calendar holidays the
    seed file is missing, and it is the only mechanism that can: nothing in the bundle lists them.

    **"No bars" has two causes and they must not be conflated (M62).** On 2026-08-28 the nightly's
    Kite fetch failed, no bars were written, and this function inferred a holiday for a Friday NSE
    had traded — a 202,201-byte bhavcopy sat in the archive the whole time. Once marked, the day
    was excluded from every subsequent backfill (they iterate trading days), so it could never
    self-heal: one lost session, silently, from a fetch error.

    ``published`` closes that. It answers "did NSE publish a bhavcopy for this date?", and a day
    it says yes to is **never** inferred a holiday — the exchange traded and we simply failed to
    ingest it, which is an ingestion bug to fix rather than a calendar fact to record. Passing
    ``None`` keeps the pre-M62 behaviour for callers that have no provider (the seed path, and
    tests that assert the inference itself).
    """
    counts = dict(
        (
            await session.execute(
                select(OhlcvDaily.date, func.count())
                .where(OhlcvDaily.date >= start, OhlcvDaily.date <= end)
                .group_by(OhlcvDaily.date)
            )
        )
        .tuples()
        .all()
    )
    observed = {day for day, count in counts.items() if count > 0}
    dense = bool(counts) and max(counts.values()) >= MIN_INSTRUMENTS_FOR_HOLIDAY_INFERENCE

    confirmed = 0
    inferred = 0
    skipped_published: list[dt.date] = []
    for day in observed:
        await _mark(session, day, is_trading_day=True, source="bhavcopy", holiday_name=None)
        confirmed += 1

    if infer_holidays and dense:
        weekdays = (
            await session.execute(
                select(TradingDay.date).where(
                    TradingDay.exchange_id == NSE_EXCHANGE_ID,
                    TradingDay.date >= start,
                    TradingDay.date <= end,
                    TradingDay.is_trading_day.is_(True),
                    TradingDay.source == "derived",
                )
            )
        ).scalars()
        for day in weekdays:
            if day in observed:
                continue
            # The exchange's own answer beats our absence of data. Only when NSE published
            # nothing is "no bars" evidence of a holiday.
            if published is not None and await published(day):
                skipped_published.append(day)
                continue
            await _mark(
                session,
                day,
                is_trading_day=False,
                source="bhavcopy",
                holiday_name=INFERRED_HOLIDAY_NAME,
            )
            inferred += 1

    remaining = (
        await session.execute(
            select(func.count())
            .select_from(TradingDay)
            .where(
                TradingDay.exchange_id == NSE_EXCHANGE_ID,
                TradingDay.date >= start,
                TradingDay.date <= end,
                TradingDay.source.in_(("holiday", "derived")),
            )
        )
    ).scalar_one()

    return ReconciliationResult(
        confirmed, inferred, int(remaining), tuple(sorted(skipped_published))
    )


async def mark_inferred_holiday(session: AsyncSession, day: dt.date) -> None:
    """Record ``day`` as an inferred holiday (AF 3.11 catch-up / reconcile shared write)."""
    await _mark(
        session,
        day,
        is_trading_day=False,
        source="bhavcopy",
        holiday_name=INFERRED_HOLIDAY_NAME,
    )


async def _mark(
    session: AsyncSession,
    day: dt.date,
    *,
    is_trading_day: bool,
    source: str,
    holiday_name: str | None,
) -> None:
    stmt = insert(TradingDay).values(
        exchange_id=NSE_EXCHANGE_ID,
        date=day,
        is_trading_day=is_trading_day,
        holiday_name=holiday_name,
        source=source,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[TradingDay.exchange_id, TradingDay.date],
            set_={
                "is_trading_day": stmt.excluded.is_trading_day,
                "holiday_name": stmt.excluded.holiday_name,
                "source": stmt.excluded.source,
            },
        )
    )
