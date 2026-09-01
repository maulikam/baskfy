"""The resync detector — "what data is pending, and why", for one button (leaf 3.1).

    "Give a button to resync so it resyncs everything if any data is pending, so I don't have to
     come to this machine."

Today repairing a data gap means an AWS SSO login, ``tools/deploy/box.sh``, and knowing which of
four backfills to run. This module is the half of that button which *finds* the work.
``baskfy_worker.tasks.resync`` is the half that does it.

WHERE THIS LIVES, AND WHY IT IS NOT IN ``packages/core``
========================================================
CLAUDE.md law #1: "``packages/core`` touches nothing. DataFrames in, DataFrames out. No database,
no network, no disk, no clock." Every sentence of that is something the detector must do — it
reads three tables, asks NSE a question, reads a token file, and needs to know what today is in
IST to avoid reporting the whole seeded future calendar as missing. So it is service code, which
is exactly where docs/04 §2 puts I/O.

Of the two services it could live in, it lives in the API, for two reasons:

* ``baskfy-worker`` depends on ``baskfy-api`` and not the other way round (see
  ``baskfy_api.queue``), so code here can be called from both sides. Code in the worker could
  only be called from one.
* G1 wants a **dry inspection** served synchronously: the operator is on a phone and should read
  what is about to happen before it happens. A 202-and-poll would answer a different question.

The worker therefore imports :func:`inspect_pending` for its own before/after picture, so
the button's preview and the repair itself can never disagree about what "pending" means.

WHY A PRESENCE CHECK IS NOT ENOUGH — the two production incidents this is built from
====================================================================================
**2026-02-01, the Budget special session (a trading Sunday).** The box held 322 bars. The
neighbouring session, 2026-01-30, held 2,310. NSE had published a full 3,229-row bhavcopy the
whole time; Kite's deep-history pass had silently skipped the day. "Does the day have bars?"
answers *yes* and moves on. It was 87% missing, and it corrupted every 9M and 12M factor window
that crossed it until a manual bhavcopy backfill took it to 2,304.

**2026-08-28, a real trading Friday.** A failed fetch wrote no bars, and ``reconcile_calendar``
concluded the exchange had been shut: ``source='bhavcopy'``,
``holiday_name='inferred: no instrument traded'``. Once marked closed the day was excluded from
every backfill — they all iterate trading days — so it could never heal itself. One session lost,
silently, to an ingestion error. M62 stopped the inference from firing when NSE published a file;
nothing looked for days *already* in that state. This does.

So the detector answers four questions, and :class:`ResyncKind` names them:

    (a) MISSING_RUN    a trading day with no succeeded ``pipeline_run``
    (b) THIN_BARS      a trading day whose bar count is far below its neighbours   (1 Feb)
    (c) WRONG_HOLIDAY  a weekday inferred a holiday that NSE did publish for       (28 Aug)
    (d) KITE_SESSION   no usable Kite access token on this host

IT CANNOT PLACE AN ORDER
========================
Nothing here imports ``packages/execution``, reaches the order gateway, or touches a GTT. The
only broker credential it goes near is the Kite *access token's issue date*, read from the
encrypted store as arithmetic — never sent anywhere, never printed.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.metrics import IST, PUBLISH_SLO_IST
from baskfy_api.settings import Settings
from baskfy_core.models import AdminAction, OhlcvDaily, PipelineRun, TradingDay
from baskfy_core.models.base import JsonObject
from baskfy_core.models.reference import INFERRED_HOLIDAY_PREFIX
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_providers.errors import ProviderError
from baskfy_providers.publication import PublicationCheck

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_LOOKBACK_DAYS",
    "MAX_LOOKBACK_DAYS",
    "RESYNC_COMPLETED_ACTION",
    "RESYNC_REQUESTED_ACTION",
    "RESYNC_TASK_NAME",
    "PublicationCheck",
    "PublicationSource",
    "ResyncFinding",
    "ResyncKind",
    "ResyncPlan",
    "inspect_pending",
    "last_completed_resync",
    "window_for",
]

#: The Celery task the button publishes. Duplicated from the worker's ``@shared_task(name=...)``
#: for the reason ``baskfy_api.queue`` gives — the import cannot go the other way.
#: ``services/worker/tests/test_celery_config.py`` asserts every name the API publishes is a task
#: the worker actually binds.
RESYNC_TASK_NAME: Final = "baskfy.ops.resync"

#: The two ``admin_action`` rows a resync leaves behind. Two and not one, because "who pressed it"
#: and "what it managed to do" are known at different moments and the second may never arrive —
#: a worker killed mid-repair leaves the request row alone, which is the honest record of what
#: happened.
RESYNC_REQUESTED_ACTION: Final = "data_resync_requested"
RESYNC_COMPLETED_ACTION: Final = "data_resync_completed"

#: How far back the inspection looks by default.
#:
#: 400 days, the same number ``baskfy_worker.calendar.CALENDAR_LOOKBACK_DAYS`` uses, and for a
#: related reason: the longest window docs/05 computes is 12 months plus snap slack, so a gap
#: inside 400 days is a gap that still corrupts a factor published *today*. It is also wide enough
#: to have caught 2026-02-01 from here, which a 90-day default would not have.
DEFAULT_LOOKBACK_DAYS: Final = 400

#: The ceiling on an explicit ``?days=``. docs/08 D5 puts the backfill origin at 2011-01-01, so
#: this is "the whole history" rounded up — an operator auditing the deep backfill can ask for it,
#: and nobody can ask for a window that means nothing.
MAX_LOOKBACK_DAYS: Final = 6000

#: A day is "thin" below this fraction of its neighbourhood's median bar count.
#:
#: A *fraction of a median*, not an absolute floor, because the universe grows: the box held 2,550
#: bars a session in mid-July 2026 and 3,014 six weeks later, so any constant would be wrong at
#: one end of the year. The median (rather than the mean) is what survives the other direction —
#: 2026-08-31 carries 9,225 rows, three times its neighbours, and a mean would let a genuinely
#: thin day beside it pass.
#:
#: 0.60 is the widest gap that still cannot be ordinary variation. Session-to-session movement in
#: the observed record is a few per cent; the 1 Feb incident sat at 0.139 of its neighbour, and
#: the repaired day at 0.997. Anything between is a day missing two fifths of the market, which is
#: not something that happens for a benign reason.
THIN_FRACTION: Final = Decimal("0.60")

#: How many neighbouring sessions form the baseline — the nearest five each side where they exist.
#: Ten is enough for the median to ignore two bad days and short enough to track universe growth.
NEIGHBOURHOOD_DAYS: Final = 10

#: Below this median the window is a backfill that has not arrived yet, not a gap. Without it,
#: every day of an empty database reports itself as thin.
MIN_MEDIAN_BARS: Final = 100

#: How many suspect weekdays one inspection will ask NSE about.
#:
#: **Measured, and the number is not cheap.** On the staging box a 400-day window holds nine
#: weekdays recorded as inferred holidays — the lunar-calendar holidays NSE declares by circular
#: and the seed list has never carried. Asking about all nine took 9.8 s against 202 ms for the
#: rest of the inspection, and it does not get faster on a second run: NSE answers 404 for a day
#: it published nothing on, there is no file to archive, so every pass pays the round trip again
#: at the provider's deliberate 1 req/s.
#:
#: Twelve covers a year's worth of them in one pass rather than leaving a permanent "not all
#: checked" note on the page, and the page absorbs the cost by streaming the panel in
#: (``apps/web/src/app/(app)/admin/page.tsx``) instead of blocking on it. Anything past the cap
#: is reported as unresolved rather than as clean.
MAX_PUBLICATION_PROBES: Final = 12

#: ``date.weekday()`` is 0=Monday, so anything below this is a weekday. Saturdays and Sundays
#: are holidays for a reason nobody needs to check with NSE.
SATURDAY: Final = 5

#: How many dates one unresolved note names before it says "and more". A list of forty helps
#: nobody read a phone screen.
NAMED_IN_NOTE: Final = 5

#: How the detector gets hold of the "did NSE publish?" probe.
#:
#: A **factory**, not the probe itself, and the indirection earns its keep twice. Building the
#: provider stack pings Redis and constructs an S3 client, and the overwhelmingly common
#: inspection has no inferred holiday to ask about — so it is called only when there is a
#: candidate. And it may return ``None``, which is how a host with no NSE provider says "I cannot
#: answer" rather than silently answering "nothing was published"; passing a bare callable would
#: have made those two indistinguishable, which is the exact bug (c) exists to catch.
PublicationSource = Callable[[], PublicationCheck | None]


class ResyncKind(StrEnum):
    """The four classes of pending work. A ``StrEnum`` so the value serialises as itself."""

    MISSING_RUN = "missing_run"
    THIN_BARS = "thin_bars"
    WRONG_HOLIDAY = "wrong_holiday"
    KITE_SESSION = "kite_session"


@dataclass(frozen=True, slots=True)
class ResyncFinding:
    """One thing that is pending, why it is pending, and what would be done about it."""

    kind: ResyncKind
    #: ``None`` for :attr:`ResyncKind.KITE_SESSION`, which is about the host and not about a date.
    trade_date: dt.date | None
    #: One sentence an operator can read on a phone, carrying the numbers behind the verdict.
    summary: str
    #: What the repair will run. Named in the preview so pressing the button is not a leap.
    remedy: str
    #: Bars observed on the day, and the neighbourhood median they were judged against. Both
    #: ``None`` for findings where a bar count is not the evidence.
    observed_bars: int | None = None
    expected_bars: int | None = None

    def as_json(self) -> JsonObject:
        return {
            "kind": self.kind.value,
            "trade_date": self.trade_date.isoformat() if self.trade_date else None,
            "summary": self.summary,
            "remedy": self.remedy,
            "observed_bars": self.observed_bars,
            "expected_bars": self.expected_bars,
        }


@dataclass(frozen=True, slots=True)
class ResyncPlan:
    """What one inspection saw. Changes nothing; every field is an observation."""

    window_start: dt.date
    window_end: dt.date
    #: Trading days examined in the window. Zero means the calendar has no sessions there, which
    #: is a different answer from "nothing is pending" and must not be rendered as one.
    trading_days_checked: int
    findings: tuple[ResyncFinding, ...]
    #: Questions this inspection could **not** answer. A plan with an empty ``findings`` and a
    #: non-empty ``unresolved`` is not a clean bill of health, and the UI says so.
    unresolved: tuple[str, ...] = ()

    @property
    def pending(self) -> bool:
        return bool(self.findings)

    def dates(self, *kinds: ResyncKind) -> tuple[dt.date, ...]:
        """The distinct dates carrying any of ``kinds``, oldest first."""
        wanted = set(kinds) if kinds else set(ResyncKind)
        seen = {f.trade_date for f in self.findings if f.kind in wanted and f.trade_date}
        return tuple(sorted(seen))

    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted({f.kind.value for f in self.findings}))

    def as_json(self) -> JsonObject:
        return {
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "trading_days_checked": self.trading_days_checked,
            "findings": [finding.as_json() for finding in self.findings],
            "unresolved": list(self.unresolved),
        }


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


def _publish_deadline(settings: Settings) -> dt.time:
    """``BASKFY_PUBLISH_DEADLINE_IST`` as a time, falling back to docs/11's 20:15.

    A malformed setting is not worth a 500 on the admin page — the deadline only decides whether
    *today* is in scope — so it degrades to the documented default and says so in the log.
    """
    raw = settings.publish_deadline_ist.strip()
    try:
        return dt.time.fromisoformat(raw)
    except ValueError:
        log.warning("unparseable publish deadline, using the docs/11 default", extra={"raw": raw})
        return PUBLISH_SLO_IST


def window_for(
    settings: Settings, *, days: int, now: dt.datetime | None = None
) -> tuple[dt.date, dt.date]:
    """The inclusive date range one inspection covers.

    **The window never reaches into the future, and usually stops before today.** ``trading_day``
    is seeded years ahead — the staging box carries sessions out to 2026-12-31 — so a naive
    "trading days with no bars" query reports every remaining day of the year as a gap. Worse, it
    would report *today* every morning: the nightly chain has not had its evening yet, and a
    button that cries wolf before lunch is a button nobody presses at 6pm.

    So today is in scope only once docs/11's publish deadline (20:15 IST) has passed, which is the
    same instant ``baskfy.ops.check_publish_deadline`` uses to decide the day is late.
    """
    moment = (now or dt.datetime.now(tz=IST)).astimezone(IST)
    end = moment.date()
    if moment.time() < _publish_deadline(settings):
        end -= dt.timedelta(days=1)
    span = max(1, min(days, MAX_LOOKBACK_DAYS))
    return end - dt.timedelta(days=span), end


# ---------------------------------------------------------------------------
# The four checks
# ---------------------------------------------------------------------------


def _median(values: Sequence[int]) -> Decimal:
    """Exact median as a ``Decimal``.

    Hand-rolled rather than ``statistics.median`` because that returns a ``float`` for an even
    sample, and CLAUDE.md house rule 9 keeps floats out of the numbers a decision is made on.
    """
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return Decimal(ordered[middle])
    return (Decimal(ordered[middle - 1]) + Decimal(ordered[middle])) / 2


def _neighbourhood_median(
    days: Sequence[dt.date], counts: dict[dt.date, int], index: int
) -> Decimal | None:
    """The median bar count of the sessions nearest ``days[index]``.

    Days with **no** bars are excluded from the sample. A run of consecutive missing days would
    otherwise drag the baseline to zero and hide every one of them behind the others — which is
    precisely the failure mode of a detector built on averages of whatever is there.
    """
    sample: list[int] = []
    for offset in range(1, len(days)):
        for neighbour in (index - offset, index + offset):
            if 0 <= neighbour < len(days) and neighbour != index:
                count = counts.get(days[neighbour], 0)
                if count > 0:
                    sample.append(count)
        if len(sample) >= NEIGHBOURHOOD_DAYS:
            break
    if not sample:
        return None
    return _median(sample[:NEIGHBOURHOOD_DAYS])


def _calendar_query(start: dt.date, end: dt.date) -> Select[tuple[dt.date, bool, str, str | None]]:
    return (
        select(
            TradingDay.date,
            TradingDay.is_trading_day,
            TradingDay.source,
            TradingDay.holiday_name,
        )
        .where(
            TradingDay.exchange_id == NSE_EXCHANGE_ID,
            TradingDay.date >= start,
            TradingDay.date <= end,
        )
        .order_by(TradingDay.date)
    )


async def _bar_counts(session: AsyncSession, start: dt.date, end: dt.date) -> dict[dt.date, int]:
    rows = (
        await session.execute(
            select(OhlcvDaily.date, func.count())
            .where(OhlcvDaily.date >= start, OhlcvDaily.date <= end)
            .group_by(OhlcvDaily.date)
        )
    ).all()
    return {row[0]: int(row[1]) for row in rows}


async def _succeeded_runs(session: AsyncSession, start: dt.date, end: dt.date) -> set[dt.date]:
    """Trade dates with at least one run that both succeeded **and** published.

    ``status='succeeded'`` alone is not enough: docs/03 step 9 is a hard gate, and a run that
    passed every step but the gate leaves ``data_version`` NULL and the site serving yesterday.
    That day's data is pending whatever the status column says.
    """
    rows = (
        await session.execute(
            select(PipelineRun.trade_date)
            .where(
                PipelineRun.trade_date >= start,
                PipelineRun.trade_date <= end,
                PipelineRun.status == "succeeded",
                PipelineRun.data_version.is_not(None),
            )
            .distinct()
        )
    ).scalars()
    return set(rows)


async def _pipeline_history_start(session: AsyncSession) -> dt.date | None:
    """The earliest trade date the nightly chain has ever opened a run for.

    Class (a) does not apply before it, and the reason is a real number rather than a nicety.
    Staging's ``pipeline_run`` begins on 2026-08-18; its bars go back to 2024 by bhavcopy
    backfill. "No succeeded run" is true of every trading day in between, and every one of them
    is correct data that simply predates the pipeline. Reporting a hundred and fifty of those
    would bury the one day that matters under a wall of days that do not — and the button would
    stop being read, which is the only way this feature actually fails.

    Days *before* the first run are still checked for classes (b) and (c), which is what caught
    2026-02-01: a thin day is thin whether or not a nightly ever ran for it.
    """
    return (await session.execute(select(func.min(PipelineRun.trade_date)))).scalar_one_or_none()


def _kite_session_finding(now: dt.datetime | None) -> tuple[ResyncFinding | None, str | None]:
    """Class (d): is there a usable Kite access token on this host?

    Arithmetic over the stored issue date, never a call to Kite — the same reasoning
    ``baskfy_worker.ops.check_kite_token`` gives: a token's life is bounded by the IST calendar
    day, so a network round trip could only confirm what the clock already knows.

    Returns ``(finding, unresolved)``. A host with no token store configured is local development
    against the fixture provider, not an incident — but it is also not evidence of health, so it
    comes back as an unresolved note rather than as silence.
    """
    from baskfy_providers.settings import get_provider_settings  # noqa: PLC0415
    from baskfy_providers.tokens import AccessTokenStore  # noqa: PLC0415

    settings = get_provider_settings()
    if not (settings.kite_token_path and settings.kite_token_encryption_key):
        return None, (
            "No Kite token store is configured on this host, so the broker session could not be "
            "checked. History before 2024 and holdings sync need one; the daily bhavcopy does not."
        )

    store = AccessTokenStore(settings.kite_token_path, settings.kite_token_encryption_key)
    remedy = "python -m baskfy_worker.kite_session_cli pull (over SSH, from the desk)"
    if not store.exists():
        return (
            ResyncFinding(
                kind=ResyncKind.KITE_SESSION,
                trade_date=None,
                summary=f"No Kite access token is stored at {store.path}.",
                remedy=remedy,
            ),
            None,
        )

    try:
        record = store.load()
    except (ProviderError, OSError) as exc:
        # Not swallowed: an undecryptable blob — or one this process cannot read — is exactly the
        # state a pull repairs, so it becomes a finding with the reason attached rather than a
        # 500 on the admin page. ``OSError`` is not hypothetical: the store is written 0600 by
        # the worker, and the API runs as a different user in some deployments.
        return (
            ResyncFinding(
                kind=ResyncKind.KITE_SESSION,
                trade_date=None,
                summary=f"The stored Kite session could not be read: {exc}",
                remedy=remedy,
            ),
            None,
        )

    if record.is_expired(now=now):
        issued = record.issued_at.astimezone(IST).date().isoformat()
        return (
            ResyncFinding(
                kind=ResyncKind.KITE_SESSION,
                trade_date=None,
                summary=(
                    f"The stored Kite session was issued on {issued} and has expired — a Kite "
                    "access token dies at the next pre-open."
                ),
                remedy=remedy,
            ),
            None,
        )
    return None, None


async def _wrong_holiday_findings(
    calendar: Sequence[tuple[dt.date, bool, str, str | None]],
    publication: PublicationSource | None,
) -> tuple[list[ResyncFinding], list[str]]:
    """Class (c): weekdays marked a holiday by inference that NSE did publish a bhavcopy for.

    Only *inferred* holidays are candidates. A day from the seeded NSE circular is a fact, and
    asking the network to second-guess the exchange's own holiday list would be both wrong and
    two hundred requests a year.

    A ``publication`` that yields ``None`` means nothing on this host can ask NSE. That is
    reported as unresolved, never as "no such days" — treating "cannot check" as "nothing found"
    is the exact shape of the bug this check exists to catch.
    """
    candidates = [
        day
        for day, is_open, _source, name in calendar
        if not is_open
        and day.weekday() < SATURDAY
        and (name or "").startswith(INFERRED_HOLIDAY_PREFIX)
    ]
    if not candidates:
        return [], []

    published = publication() if publication is not None else None
    if published is None:
        return [], [
            f"{len(candidates)} weekday(s) in this window are recorded as inferred holidays "
            f"({', '.join(d.isoformat() for d in candidates[:NAMED_IN_NOTE])}"
            f"{', …' if len(candidates) > NAMED_IN_NOTE else ''}), but no provider on this "
            "host can ask NSE whether it published a bhavcopy for them, so they were neither "
            "cleared nor flagged."
        ]

    findings: list[ResyncFinding] = []
    unresolved: list[str] = []
    for day in candidates[:MAX_PUBLICATION_PROBES]:
        if await published(day):
            findings.append(
                ResyncFinding(
                    kind=ResyncKind.WRONG_HOLIDAY,
                    trade_date=day,
                    summary=(
                        f"{day.isoformat()} is a weekday recorded as a holiday by inference, but "
                        "NSE published a bhavcopy for it — the exchange traded and the ingest "
                        "failed. While it stays marked shut, every backfill skips it."
                    ),
                    remedy="correct trading_day, then re-ingest the bhavcopy for the date",
                )
            )
    if len(candidates) > MAX_PUBLICATION_PROBES:
        deferred = candidates[MAX_PUBLICATION_PROBES:]
        unresolved.append(
            f"{len(deferred)} further inferred holiday(s) were not checked against NSE this "
            f"pass (cap is {MAX_PUBLICATION_PROBES} probes per inspection); the earliest is "
            f"{deferred[0].isoformat()}. Run the inspection again after this one is repaired."
        )
    return findings, unresolved


# ---------------------------------------------------------------------------
# The inspection
# ---------------------------------------------------------------------------


async def inspect_pending(
    session: AsyncSession,
    *,
    settings: Settings,
    publication: PublicationSource | None = None,
    now: dt.datetime | None = None,
    days: int = DEFAULT_LOOKBACK_DAYS,
) -> ResyncPlan:
    """Find every pending gap in the window. **Writes nothing.**

    Four ``SELECT``s, one file read and at most :data:`MAX_PUBLICATION_PROBES` network probes.
    That it changes nothing is what makes the preview safe to render on a page load and what makes
    the repair's idempotence testable: run it twice on a healthy database and both plans are
    empty, run it either side of a repair and the difference is the repair.
    """
    start, end = window_for(settings, now=now, days=days)

    calendar = [
        (row[0], bool(row[1]), str(row[2]), row[3])
        for row in (await session.execute(_calendar_query(start, end))).all()
    ]
    open_days = [day for day, is_open, _source, _name in calendar if is_open]
    counts = await _bar_counts(session, start, end)
    published_runs = await _succeeded_runs(session, start, end)
    history_start = await _pipeline_history_start(session)

    findings: list[ResyncFinding] = []
    unresolved: list[str] = []
    if history_start is None and open_days:
        unresolved.append(
            "The nightly pipeline has never opened a run on this deployment, so no trading day "
            "could be checked for a published snapshot. Bar coverage was still checked."
        )

    for index, day in enumerate(open_days):
        observed = counts.get(day, 0)

        if history_start is not None and day >= history_start and day not in published_runs:
            findings.append(
                ResyncFinding(
                    kind=ResyncKind.MISSING_RUN,
                    trade_date=day,
                    summary=(
                        f"{day.isoformat()} was a trading session with no pipeline run that "
                        "published — nothing bumped data_version for it."
                    ),
                    remedy="re-run the nightly chain for the date",
                    observed_bars=observed,
                )
            )

        median = _neighbourhood_median(open_days, counts, index)
        if median is None or median < MIN_MEDIAN_BARS:
            continue
        if Decimal(observed) >= median * THIN_FRACTION:
            continue
        findings.append(
            ResyncFinding(
                kind=ResyncKind.THIN_BARS,
                trade_date=day,
                summary=(
                    f"{day.isoformat()} holds {observed:,} bars against a neighbouring median "
                    f"of {int(median):,} — below {THIN_FRACTION:.0%} of it. A day this thin "
                    "corrupts every 9M and 12M factor window that crosses it."
                ),
                remedy="re-ingest the NSE bhavcopy for the date",
                observed_bars=observed,
                expected_bars=int(median),
            )
        )

    holiday_findings, holiday_unresolved = await _wrong_holiday_findings(calendar, publication)
    findings.extend(holiday_findings)
    unresolved.extend(holiday_unresolved)

    # Normalised to IST before the token store sees it: `AccessToken.is_expired` compares
    # IST calendar dates, and a naive datetime would be read in the host's local zone.
    kite_finding, kite_unresolved = _kite_session_finding(
        None if now is None else now.astimezone(IST)
    )
    if kite_finding is not None:
        findings.append(kite_finding)
    if kite_unresolved is not None:
        unresolved.append(kite_unresolved)

    findings.sort(key=lambda f: (f.trade_date or dt.date.max, f.kind.value))
    return ResyncPlan(
        window_start=start,
        window_end=end,
        trading_days_checked=len(open_days),
        findings=tuple(findings),
        unresolved=tuple(unresolved),
    )


async def last_completed_resync(session: AsyncSession) -> AdminAction | None:
    """The most recent finished repair, or ``None`` if none has ever finished.

    Read from the staff audit trail rather than from a table of its own: ``admin_action`` is
    already append-only, already carries a JSON payload, and already answers "who did this".
    A second history table would only be able to disagree with it.
    """
    return (
        await session.execute(
            select(AdminAction)
            .where(AdminAction.action == RESYNC_COMPLETED_ACTION)
            .order_by(AdminAction.created_at.desc(), AdminAction.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
