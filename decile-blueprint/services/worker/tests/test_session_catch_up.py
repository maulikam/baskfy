"""M84 — a session that never landed is found and run again, without being asked.

The failure this file pins: on 3 Sep 2026 a deploy killed the nightly chain mid-run. The reaper
marked the run failed and alerted fifteen minutes later, exactly as designed, and then **nothing
re-ran it** — so the product served the 2 Sep session until a person noticed the next night and
asked for a re-run by hand. Every test below is one sentence of "that cannot happen again".

The finder is proved against a real database, because what it asserts is a join between the
trading calendar and the run history and a fake would only prove the fake. The two task bodies
are proved against stubs, because what they assert is ordering and bounding.
"""

from __future__ import annotations

import datetime as dt

import pytest
from helpers import requires_db
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import PipelineRun, TradingDay
from baskfy_worker import catch_up
from baskfy_worker.steps import RunStatus
from baskfy_worker.tasks import celery_tasks

#: The real week of the incident, and `conftest.clean_db` has already seeded the calendar through
#: 2026-12-31 — so these are trading days because the exchange's calendar says so, not because a
#: test asserted it. 5 Sep is the Saturday.
MON = dt.date(2026, 8, 31)
TUE = dt.date(2026, 9, 1)
WED = dt.date(2026, 9, 2)
THU = dt.date(2026, 9, 3)
FRI = dt.date(2026, 9, 4)
SAT = dt.date(2026, 9, 5)
#: Past the end of the seeded calendar: a date it does not carry at all.
UNCHARTED = dt.date(2027, 1, 5)

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


async def _close_the_exchange(session: AsyncSession, day: dt.date) -> None:
    """Turn a seeded session into a holiday — 28 Aug 2026 was a Friday and shut."""
    await session.execute(
        update(TradingDay).where(TradingDay.date == day).values(is_trading_day=False)
    )
    await session.flush()


async def _run(
    session: AsyncSession,
    day: dt.date,
    *,
    status: str,
    data_version: int | None,
) -> None:
    session.add(
        PipelineRun(
            trade_date=day,
            status=status,
            started_at=dt.datetime(day.year, day.month, day.day, 13, 15, tzinfo=dt.UTC),
            finished_at=dt.datetime(day.year, day.month, day.day, 15, 0, tzinfo=dt.UTC),
            data_version=data_version,
        )
    )
    await session.flush()


@pytest.mark.db
@requires_db
class TestWhatCountsAsLanded:
    """One thing only: a run for that date carrying a `data_version`."""

    async def test_the_3_sep_shape_is_unlanded(self, session: AsyncSession) -> None:
        """A failed run with no data_version is a session we do not have — the whole point."""
        await _run(session, WED, status=RunStatus.SUCCEEDED, data_version=8)
        await _run(session, THU, status=RunStatus.FAILED, data_version=None)
        await _run(session, FRI, status=RunStatus.SUCCEEDED, data_version=9)

        missing = await catch_up.unlanded_sessions(session, through=FRI, lookback_days=2)
        assert missing == [THU], "the killed run was treated as if the day had landed"

    async def test_a_published_day_is_never_proposed(self, session: AsyncSession) -> None:
        await _run(session, WED, status=RunStatus.SUCCEEDED, data_version=8)
        await _run(session, THU, status=RunStatus.SUCCEEDED, data_version=9)
        await _run(session, FRI, status=RunStatus.SUCCEEDED, data_version=10)

        assert await catch_up.unlanded_sessions(session, through=FRI, lookback_days=2) == []

    async def test_a_run_still_going_does_not_count_as_landed(self, session: AsyncSession) -> None:
        """`running` is not `published`; the column that matters is `data_version`."""
        await _run(session, THU, status=RunStatus.RUNNING, data_version=None)

        assert await catch_up.unlanded_sessions(session, through=THU, lookback_days=0) == [THU]

    async def test_a_day_with_no_run_at_all_is_proposed(self, session: AsyncSession) -> None:
        """The 31 Aug shape: the task never ran for that date, so there is no row to read."""
        await _run(session, WED, status=RunStatus.SUCCEEDED, data_version=8)

        assert await catch_up.unlanded_sessions(session, through=WED, lookback_days=0) == []
        assert await catch_up.unlanded_sessions(session, through=THU, lookback_days=1) == [THU]


@pytest.mark.db
@requires_db
class TestItNeverProposesADayThatCannotHaveData:
    async def test_a_holiday_is_not_a_missing_session(self, session: AsyncSession) -> None:
        """28 Aug 2026 was a Friday and shut. `ops.is_trading_day` carries the reasoning."""
        await _close_the_exchange(session, FRI)

        missing = await catch_up.unlanded_sessions(session, through=FRI, lookback_days=1)
        assert FRI not in missing
        assert missing == [THU]

    async def test_a_date_the_calendar_does_not_carry_is_not_proposed(
        self, session: AsyncSession
    ) -> None:
        """Absent means the far future or a gap. Not running is the safe answer either way."""
        assert await catch_up.unlanded_sessions(session, through=UNCHARTED, lookback_days=3) == []

    async def test_today_is_not_missing_until_its_data_can_exist(
        self, session: AsyncSession
    ) -> None:
        """Before 18:00 IST the day has not published, and calling it missing is how an alert
        becomes noise."""
        morning = dt.datetime(2026, 9, 4, 6, 45, tzinfo=IST)
        assert (
            await catch_up.unlanded_sessions(session, through=FRI, lookback_days=0, now=morning)
            == []
        )

        evening = dt.datetime(2026, 9, 4, 18, 30, tzinfo=IST)
        assert await catch_up.unlanded_sessions(
            session, through=FRI, lookback_days=0, now=evening
        ) == [FRI]

    async def test_yesterday_is_missing_at_any_hour(self, session: AsyncSession) -> None:
        """The 06:45 sweep is the one that has to find last night's hole."""
        morning = dt.datetime(2026, 9, 4, 6, 45, tzinfo=IST)

        assert await catch_up.unlanded_sessions(
            session, through=FRI, lookback_days=1, now=morning
        ) == [THU], "the 06:45 sweep would have walked past last night's hole"


@pytest.mark.db
@requires_db
class TestTheWindow:
    async def test_oldest_first(self, session: AsyncSession) -> None:
        """Sessions are run in the order they happened, so factor windows fill in order."""
        missing = await catch_up.unlanded_sessions(session, through=FRI, lookback_days=4)
        assert missing == [MON, TUE, WED, THU, FRI]
        assert missing == sorted(missing)

    async def test_the_lookback_bounds_it(self, session: AsyncSession) -> None:
        """A week, not a quarter: nobody wants a database restore to propose ninety re-runs."""
        assert await catch_up.unlanded_sessions(session, through=FRI, lookback_days=1) == [THU, FRI]
        assert await catch_up.unlanded_sessions(session, through=FRI, lookback_days=0) == [FRI]

    async def test_a_negative_lookback_is_refused(self, session: AsyncSession) -> None:
        with pytest.raises(ValueError, match="lookback_days"):
            await catch_up.unlanded_sessions(session, through=FRI, lookback_days=-1)


class _Report:
    """What `backfill_bars_from_bhavcopy` returns, in the shape the task reads."""

    def __init__(self, bars: int = 3635, days: int = 1) -> None:
        self.bars_written = bars
        self.days_written = days
        self.unmatched_symbols: set[str] = set()
        self.missing_days: list[dt.date] = []
        self.failures: dict[str, str] = {}

    @property
    def succeeded(self) -> bool:
        return not self.failures


class TestTheCatchUpTaskRunsThemOldestFirst:
    """No database: what these assert is ordering and bounding, not a query."""

    @staticmethod
    def _arrange(monkeypatch: pytest.MonkeyPatch, missing: list[dt.date]) -> list[str]:
        ran: list[str] = []
        monkeypatch.setattr(celery_tasks, "run_in_session", lambda _fn: missing)
        monkeypatch.setattr(
            celery_tasks,
            "nightly_pipeline",
            lambda trade_date: ran.append(trade_date) or {"trade_date": trade_date},
        )
        return ran

    def test_it_runs_the_oldest_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sessions land in the order they happened, so each factor window sees a full history."""
        ran = self._arrange(monkeypatch, [WED, THU])
        result = celery_tasks.session_catch_up(max_sessions=2)

        assert ran == [WED.isoformat(), THU.isoformat()]
        assert result["status"] == "ran"
        assert result["remaining"] == []

    def test_it_is_bounded_and_says_what_it_left(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two hours a chain: an unbounded sweep would hold a worker slot until the open."""
        ran = self._arrange(monkeypatch, [MON, TUE, WED, THU])
        result = celery_tasks.session_catch_up(max_sessions=2)

        assert ran == [MON.isoformat(), TUE.isoformat()]
        assert result["remaining"] == [WED.isoformat(), THU.isoformat()]

    def test_an_ordinary_morning_runs_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The 06:45 sweep and every login fire this; when nothing is missing it must be free."""
        ran = self._arrange(monkeypatch, [])
        result = celery_tasks.session_catch_up()

        assert ran == []
        assert result["status"] == "nothing to do"
        assert result["sessions"] == []


class TestTheScheduledBhavcopyJob:
    def test_it_ingests_the_day_without_kite(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The whole point: NSE's own file, no broker session anywhere in the path."""
        windows: list[object] = []

        async def fake_backfill(provider: object, window: object, **kwargs: object) -> _Report:
            windows.append(window)
            return _Report()

        monkeypatch.setattr(celery_tasks, "build_nse_provider", lambda *a, **k: object())
        monkeypatch.setattr(celery_tasks, "backfill_bars_from_bhavcopy", fake_backfill)

        result = celery_tasks.bhavcopy_ingest(THU.isoformat())

        assert result["status"] == "succeeded"
        assert result["bars_written"] == 3635
        assert [str(w) for w in windows] == [f"{THU.isoformat()}..{THU.isoformat()}"]

    def test_it_skips_a_day_the_exchange_was_shut(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A CRITICAL alert for a day with no session teaches an operator to ignore alerts."""
        monkeypatch.setattr(celery_tasks, "run_in_session", lambda _fn: False)
        monkeypatch.setattr(celery_tasks, "backfill_bars_from_bhavcopy", _never_called)

        result = celery_tasks.bhavcopy_ingest()
        assert result["status"] == "skipped"
        assert result["reason"] == "not a trading day"

    def test_it_waits_until_the_file_can_exist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Before 18:00 IST there is no bhavcopy to fetch — the same rule the chain follows."""
        monkeypatch.setattr(celery_tasks, "run_in_session", lambda _fn: True)
        monkeypatch.setattr(celery_tasks, "backfill_bars_from_bhavcopy", _never_called)

        class _Noon(dt.datetime):
            @classmethod
            def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
                return dt.datetime(2026, 9, 4, 12, 0, tzinfo=tz)

        monkeypatch.setattr(celery_tasks.dt, "datetime", _Noon)
        result = celery_tasks.bhavcopy_ingest()
        assert result["status"] == "skipped"
        assert "not published yet" in result["reason"]


async def _never_called(*args: object, **kwargs: object) -> _Report:  # pragma: no cover
    raise AssertionError("the bhavcopy was fetched for a day that cannot have one")
