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
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import PipelineRun, PipelineRunStep, TradingDay
from baskfy_worker import catch_up
from baskfy_worker.celery_app import BEAT_SCHEDULE
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


async def _gate_verdict(session: AsyncSession, day: dt.date, *, passed: bool) -> None:
    """Record the `data_quality_gate` step for that day's newest run — the verdict itself."""
    run_id = (
        await session.execute(
            select(PipelineRun.id)
            .where(PipelineRun.trade_date == day)
            .order_by(PipelineRun.id.desc())
            .limit(1)
        )
    ).scalar_one()
    session.add(
        PipelineRunStep(
            run_id=run_id,
            step=catch_up.QUALITY_GATE_STEP,
            status="succeeded" if passed else catch_up.STEP_FAILED,
        )
    )
    await session.flush()


@pytest.mark.db
@requires_db
class TestADayTheGateRefusedIsNotReRunForever:
    """5 Sep 2026. The module promised "refused again, loudly, ONCE" and did not deliver it.

    `landed_sessions` reads only `data_version`, and a gate refusal leaves it null exactly as a
    killed run does — so the sweep could not tell "no verdict" from "the verdict was no" and
    re-ran the chain for a refused day every time it fired. It ran four full chains for
    2026-09-04 over eighteen hours; every one of them was refused for the same uningested 1:5
    split, and no amount of re-running could have fixed a missing corporate action.
    """

    async def test_a_gate_refusal_is_not_proposed_again(self, session: AsyncSession) -> None:
        await _run(session, THU, status=RunStatus.FAILED, data_version=None)
        await _gate_verdict(session, THU, passed=False)

        assert await catch_up.unlanded_sessions(session, through=THU, lookback_days=0) == [], (
            "the sweep proposed a day whose data the gate has already judged wrong"
        )

    async def test_a_killed_run_is_still_proposed(self, session: AsyncSession) -> None:
        """The distinction that matters: no gate step at all means no verdict was reached,
        which is the 3 Sep shape this module was written for and must keep healing."""
        await _run(session, THU, status=RunStatus.FAILED, data_version=None)

        assert await catch_up.unlanded_sessions(session, through=THU, lookback_days=0) == [THU]

    async def test_a_refusal_followed_by_a_publish_is_landed_not_refused(
        self, session: AsyncSession
    ) -> None:
        """The recovery path, which is what actually happened: the split was ingested and the
        next run published. A later success must not be masked by the earlier refusal."""
        await _run(session, THU, status=RunStatus.FAILED, data_version=None)
        await _gate_verdict(session, THU, passed=False)
        await _run(session, THU, status=RunStatus.SUCCEEDED, data_version=11)
        await _gate_verdict(session, THU, passed=True)

        assert await catch_up.unlanded_sessions(session, through=THU, lookback_days=0) == []


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
    def _arrange(
        monkeypatch: pytest.MonkeyPatch,
        missing: list[dt.date],
        *,
        plan_raises: Exception | None = None,
        published: bool = True,
    ) -> tuple[list[str], list[str]]:
        """Returns the dates the chain ran and the dates a swing plan was built for.

        ``published`` is what the chain reports back: a `data_version` means it reached
        `publish`, which is the only thing that earns the day a swing plan. Default True
        because the ordinary caught-up session does publish; the refusal has its own test.
        """
        ran: list[str] = []
        planned: list[str] = []
        monkeypatch.setattr(celery_tasks, "run_in_session", lambda _fn: missing)

        def _record(trade_date: str) -> dict[str, object]:
            ran.append(trade_date)
            return {"trade_date": trade_date, "data_version": 42 if published else None}

        def _plan(trade_date: str) -> dict[str, object]:
            if plan_raises is not None:
                raise plan_raises
            planned.append(trade_date)
            return {"date": trade_date}

        monkeypatch.setattr(celery_tasks, "nightly_pipeline", _record)
        monkeypatch.setattr(celery_tasks, "swing_eod_task", _plan)
        return ran, planned

    def test_it_runs_the_oldest_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sessions land in the order they happened, so each factor window sees a full history."""
        ran, _ = self._arrange(monkeypatch, [WED, THU])
        result = celery_tasks.session_catch_up(max_sessions=2)

        assert ran == [WED.isoformat(), THU.isoformat()]
        assert result["status"] == "ran"
        assert result["remaining"] == []

    def test_a_caught_up_session_also_gets_its_swing_plan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """M85, closing M84's own open item — and the half Maulik actually reads.

        The chain's twelfth step detects the day's setups; `baskfy.swing.eod` is what turns them
        into a plan, and it existed only as a 21:05 Beat entry. So a session caught up at 06:45
        or after a 1pm login landed every bar and every setup and no plan, and the swing book
        stayed on the last session that had one. Each plan follows *its own* chain, in order,
        because the plan is built from that day's candidates and that day's gate.
        """
        ran, planned = self._arrange(monkeypatch, [WED, THU])
        result = celery_tasks.session_catch_up(max_sessions=2)

        assert planned == [WED.isoformat(), THU.isoformat()]
        assert planned == ran, "a plan was built for a day whose chain did not run"
        assert result["swing_plans"] == [{"date": WED.isoformat()}, {"date": THU.isoformat()}]

    def test_a_chain_that_did_not_publish_gets_no_swing_plan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """5 Sep 2026, and this was wrong as M85 wrote it.

        The evening ran unconditionally, so on a day the quality gate REFUSED it built a swing
        plan on data the pipeline had just rejected — and counted a LIVE session against
        `first_live_sessions_left`, spending one of the five half-risk sessions on numbers
        nobody should trade. `data_version` is the product's own test for "fit to serve"; a
        chain without one gets no plan.
        """
        ran, planned = self._arrange(monkeypatch, [WED], published=False)
        result = celery_tasks.session_catch_up(max_sessions=1)

        assert ran == [WED.isoformat()], "the chain still ran"
        assert planned == [], "a plan was built on a day the gate refused"
        assert result["swing_plans"] == [
            {"date": WED.isoformat(), "skipped": "the chain did not publish"}
        ]

    def test_a_plan_that_fails_does_not_undo_a_session_that_landed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail soft. The bars are the expensive part and they are already committed."""
        ran, _ = self._arrange(monkeypatch, [WED], plan_raises=RuntimeError("no sleeve"))
        result = celery_tasks.session_catch_up(max_sessions=1)

        assert ran == [WED.isoformat()], "the chain still ran"
        assert result["status"] == "ran"
        assert result["swing_plans"] == [{"date": WED.isoformat(), "error": "RuntimeError"}]

    def test_it_is_bounded_and_says_what_it_left(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two hours a chain: an unbounded sweep would hold a worker slot until the open."""
        ran, planned = self._arrange(monkeypatch, [MON, TUE, WED, THU])
        result = celery_tasks.session_catch_up(max_sessions=2)

        assert ran == [MON.isoformat(), TUE.isoformat()]
        assert planned == [MON.isoformat(), TUE.isoformat()], "the bound covers the plans too"
        assert result["remaining"] == [WED.isoformat(), THU.isoformat()]

    def test_an_ordinary_morning_runs_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The 06:45 sweep and every login fire this; when nothing is missing it must be free."""
        ran, planned = self._arrange(monkeypatch, [])
        result = celery_tasks.session_catch_up()

        assert ran == []
        assert planned == [], "a plan was built for a session nobody was missing"
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

        # `cls(...)` rather than `dt.datetime(...)`: an override may not widen its return type,
        # and `datetime.now` is typed as returning `Self`. Constructing through `cls` keeps the
        # promise and still produces a real datetime, which is what the task goes on to use.
        class _Noon(dt.datetime):
            @classmethod
            def now(cls, tz: dt.tzinfo | None = None) -> _Noon:
                return cls(2026, 9, 4, 12, 0, tzinfo=tz)

        # `dt` here, not `celery_tasks.dt`: it is the same module object, and reaching for it
        # through another module's import is the implicit re-export house rule 3 rules out.
        monkeypatch.setattr(dt, "datetime", _Noon)
        result = celery_tasks.bhavcopy_ingest()
        assert result["status"] == "skipped"
        assert "not published yet" in result["reason"]


async def _never_called(*args: object, **kwargs: object) -> _Report:  # pragma: no cover
    raise AssertionError("the bhavcopy was fetched for a day that cannot have one")


@pytest.mark.db
@requires_db
class TestTheCutoffIsTheBhavcopysHour:
    """M88 moved this cutoff to 15:45 and 8 Sep 2026 proved that wrong.

    Kite returns the day's bars minutes after the close — for the ~3,000 liquid names it
    covers. This universe holds ~4,855 instruments and the rest arrive in NSE's bhavcopy, so an
    early run writes a PARTIAL day: 7 Sep ingested 3,056 bars at 15:37 and the quality gate
    refused it three times (3,056 against a 10-day median of 3,524), where 4 Sep with the
    bhavcopy top-up ingested 4,454 and published.

    The token does make the liquid names available sooner, and the live intraday scan already
    uses them. The published end-of-day series waits for the day to actually be complete.
    """

    def test_the_cutoff_is_18_00(self) -> None:
        assert dt.time(18, 0) == celery_tasks.SESSION_DATA_READY_IST

    def test_there_is_no_earlier_kite_cutoff_any_more(self) -> None:
        """The constant and its Beat entry are gone, not merely unused — a dormant early run
        is one config flip away from writing partial days again."""
        assert not hasattr(celery_tasks, "SESSION_DATA_READY_WITH_KITE_IST")
        assert not hasattr(catch_up, "SESSION_DATA_READY_WITH_KITE_IST")
        assert "refresh-reference-data-early" not in BEAT_SCHEDULE

    def test_a_published_day_is_not_re_run_by_a_redelivered_schedule(self) -> None:
        """Kept from M88, which was right about this half: re-running a landed day is two hours
        of Kite calls to rewrite rows it already wrote. It is also the 31 Aug 2026 shape, where
        redelivered nightlies re-ran a finished session."""
        assert callable(celery_tasks.run_already_published)
