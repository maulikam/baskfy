"""TW12 — the worker half of the TWT page's **Scan now** button.

The desk writes a ``QUEUED`` row and has no Celery client; this is everything that happens next.

* **the sweep publishes what nobody has published** — rows with no ``task_id``, which is exactly
  what the desk leaves behind, and it leaves the API's own rows alone;
* **the task claims before it works**, so a beat that overlaps a slow broker cannot detect the
  same session twice;
* **it detects a published session**, never today, because this strategy's signal is read off
  closed weekly bars (`docs/twt/04` §2) — the same rule VB12 learned on the box (VB13.4) and the
  reason there is no provisional path here at all;
* **it calls the detector that already exists** rather than carrying one (DECISIONS-TW TW12.2);
* **a failure lands on the row**, not in the worker's logs alone, because the button has to be
  able to show what went wrong;
* **it is idempotent** — pressing twice overwrites the same rows and moves no counter
  (house rule 7).

And the one that matters most on this sleeve: **it cannot reach an order**, whatever
``BASKFY_TWT_EXECUTION_ENABLED`` is set to. `docs/twt/02` Track C §3.
"""

from __future__ import annotations

import datetime as dt
import inspect
import re
from decimal import Decimal

import pytest
import sqlalchemy as sa
from helpers import requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import AppUser, PipelineRun, TwBreadthDaily, TwConfig, TwScanRun
from baskfy_worker.tasks import twt_scan as S
from baskfy_worker.tasks.twt_scan import (
    claim_run,
    latest_published_session,
    newest_run,
    run_twt_scan,
    unpublished_runs,
)

pytestmark = [requires_db, pytest.mark.db]

AS_OF = dt.date(2026, 8, 18)
NOW = dt.datetime(2026, 8, 18, 21, 40, tzinfo=dt.UTC)


async def _user(session: AsyncSession, public_id: str = "tw12") -> int:
    user = AppUser(public_id=public_id, email=f"{public_id}@example.com")
    session.add(user)
    await session.flush()
    # `sleeve_capital_inr` is left at the column default, which is 0. THE RUN NEVER SETS IT:
    # root CLAUDE.md's safety rail and `docs/twt/02` §3. A scan does not need capital — it
    # detects, and detection is what a sleeve with no money still does.
    session.add(TwConfig(user_id=user.id, updated_by="test"))
    await session.flush()
    return int(user.id)


async def _published(session: AsyncSession, day: dt.date, version: int) -> PipelineRun:
    """A pipeline run that **published** — the only kind :func:`latest_published_session` counts.

    A run with no ``data_version`` has not put its bars on the page, and detecting a session whose
    bars are not there is what VB12's first live press did by mistake.
    """
    run = PipelineRun(
        trade_date=day,
        status="succeeded",
        data_version=version,
        started_at=dt.datetime.now(tz=dt.UTC),
    )
    session.add(run)
    await session.flush()
    return run


async def _queued(session: AsyncSession, user_id: int, **overrides: object) -> TwScanRun:
    row = TwScanRun(user_id=user_id, requested_at=NOW, status="QUEUED", source="desk")
    for key, value in overrides.items():
        setattr(row, key, value)
    session.add(row)
    await session.flush()
    return row


class TestTheSweep:
    async def test_it_publishes_a_row_the_desk_left_behind(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        row = await _queued(session, user_id)

        assert [one.id for one in await unpublished_runs(session)] == [row.id]

    async def test_it_leaves_alone_a_row_that_already_has_a_task(
        self, session: AsyncSession
    ) -> None:
        """The API publishes its own rows. Publishing them twice would detect twice."""
        user_id = await _user(session)
        await _queued(session, user_id, task_id="already-published")

        assert await unpublished_runs(session) == []

    @pytest.mark.parametrize("status", ["RUNNING", "DONE", "FAILED"])
    async def test_it_only_looks_at_queued_rows(self, session: AsyncSession, status: str) -> None:
        user_id = await _user(session)
        await _queued(session, user_id, status=status)

        assert await unpublished_runs(session) == []

    async def test_it_takes_the_oldest_first(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        second = await _queued(session, user_id, requested_at=NOW)
        first = await _queued(session, user_id, requested_at=NOW - dt.timedelta(minutes=5))

        assert [one.id for one in await unpublished_runs(session)] == [first.id, second.id]


class TestClaiming:
    async def test_a_queued_row_is_claimed_and_marked_running(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        row = await _queued(session, user_id)

        claimed = await claim_run(session, row.id)

        assert claimed is not None
        assert claimed.status == "RUNNING"
        assert claimed.started_at is not None

    async def test_a_second_claim_answers_none(self, session: AsyncSession) -> None:
        """A beat that overlaps a slow broker can publish the same row twice; the second copy must
        do nothing rather than detect the same session again."""
        user_id = await _user(session)
        row = await _queued(session, user_id)

        assert await claim_run(session, row.id) is not None
        assert await claim_run(session, row.id) is None

    async def test_an_unknown_id_answers_none(self, session: AsyncSession) -> None:
        assert await claim_run(session, 999_999) is None


class TestWhichSessionItDetects:
    async def test_it_is_the_newest_published_run(self, session: AsyncSession) -> None:
        await _published(session, AS_OF - dt.timedelta(days=1), version=41)
        await _published(session, AS_OF, version=42)

        assert await latest_published_session(session, AS_OF) == AS_OF

    async def test_today_is_ignored_until_its_bars_are_published(
        self, session: AsyncSession
    ) -> None:
        """The exchange calendar calls Friday a trading day from midnight; Friday's bars do not
        exist until the chain publishes that evening. A scan pressed at two in the afternoon must
        pick yesterday, not today — VB13.4's lesson, inherited rather than re-learned."""
        yesterday = AS_OF - dt.timedelta(days=1)
        await _published(session, yesterday, version=41)
        session.add(
            PipelineRun(trade_date=AS_OF, status="running", started_at=dt.datetime.now(tz=dt.UTC))
        )
        await session.flush()

        assert await latest_published_session(session, AS_OF) == yesterday

    async def test_a_box_that_has_never_published_answers_none(self, session: AsyncSession) -> None:
        """Not a failure — it is the state a fresh box is in before the chain has ever run."""
        assert await latest_published_session(session, dt.date(2009, 1, 1)) is None


class TestTheRun:
    async def test_a_run_with_nothing_published_fails_on_the_row_rather_than_raising(
        self, session: AsyncSession
    ) -> None:
        """The button has to be able to show what went wrong."""
        user_id = await _user(session)
        row = await _queued(session, user_id)
        before_the_calendar = dt.datetime(2009, 1, 1, 21, 40, tzinfo=dt.UTC)

        result = await run_twt_scan(session, row.id, now=before_the_calendar)

        assert result["status"] == "FAILED"
        await session.refresh(row)
        assert row.status == "FAILED"
        assert row.error is not None and "published no session" in row.error
        assert row.finished_at is not None

    async def test_a_run_over_a_quiet_session_finishes_done_with_its_detail(
        self, session: AsyncSession
    ) -> None:
        """No bars is a legitimate answer — the detector returns rather than raises — and the row
        should say DONE with the counts, not FAILED. On this strategy "no signals" is the
        *ordinary* state of a session that ran perfectly (DECISIONS-TW TW4.3)."""
        user_id = await _user(session)
        await _published(session, AS_OF, version=42)
        row = await _queued(session, user_id)

        result = await run_twt_scan(session, row.id, now=NOW)

        assert result["status"] == "DONE"
        await session.refresh(row)
        assert row.session_date == AS_OF
        assert row.detail is not None
        assert row.detail["date"] == AS_OF.isoformat()
        assert row.finished_at is not None and row.started_at is not None

    async def test_a_row_that_is_not_queued_is_skipped(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        row = await _queued(session, user_id, status="DONE")

        result = await run_twt_scan(session, row.id, now=NOW)

        assert "skipped" in result

    async def test_it_forces_a_redetect_of_a_session_that_already_ran(
        self, session: AsyncSession
    ) -> None:
        """**The whole point of the button.**

        The nightly's rule is "skip a session that already has a breadth row"; a person pressing
        Scan now is asking for exactly that session — the night a threshold changed, or the night
        the quality gate refused the day. So the scan passes ``force=True`` and a second run is a
        real re-detect rather than the string ``already detected``.
        """
        user_id = await _user(session)
        await _published(session, AS_OF, version=42)

        first = await run_twt_scan(session, (await _queued(session, user_id)).id, now=NOW)
        second = await run_twt_scan(session, (await _queued(session, user_id)).id, now=NOW)

        for result in (first, second):
            assert result["status"] == "DONE"
            detail = result["detail"]
            assert isinstance(detail, dict)
            assert "skipped" not in detail, (
                "the scan inherited the nightly's already-detected skip — force=True is what "
                "makes this button do anything on a session that has run"
            )

    async def test_pressing_twice_writes_two_rows_and_detects_the_same_session(
        self, session: AsyncSession
    ) -> None:
        """House rule 7: detection is idempotent per (user, date). Two requests are two audit rows
        and one set of detection rows."""
        user_id = await _user(session)
        await _published(session, AS_OF, version=42)
        first = await _queued(session, user_id)
        second = await _queued(session, user_id)

        one = await run_twt_scan(session, first.id, now=NOW)
        two = await run_twt_scan(session, second.id, now=NOW + dt.timedelta(minutes=2))

        assert one["session_date"] == two["session_date"] == AS_OF.isoformat()
        assert (
            await session.execute(sa.select(sa.func.count()).select_from(TwScanRun))
        ).scalar_one() == 2
        breadth = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(TwBreadthDaily)
                .where(TwBreadthDaily.user_id == user_id, TwBreadthDaily.date == AS_OF)
            )
        ).scalar_one()
        assert breadth <= 1, "a second press wrote a second breadth row for the same session"

    async def test_another_users_row_detects_for_that_user(self, session: AsyncSession) -> None:
        """`02` Track C §6: the row carries the owner and the detection is written for them."""
        mine = await _user(session, "tw12-a")
        theirs = await _user(session, "tw12-b")
        await _published(session, AS_OF, version=42)
        row = await _queued(session, theirs)

        await run_twt_scan(session, row.id, now=NOW)

        assert (await newest_run(session, user_id=theirs)) is not None
        assert (await newest_run(session, user_id=mine)) is None


class TestItCannotOrder:
    """`docs/twt/02` Track C §3, over the module a scan actually runs.

    ``packages/core/tests/test_twt_safety_properties.py`` proves the same thing behaviourally, by
    driving the route against a real gateway. This is the cheap static half, kept beside the code
    it is about so somebody editing this file sees it.
    """

    def _code(self) -> str:
        """The source with prose removed — the module *describes* what it must not do, and a
        prohibition must not trip the check."""
        return re.sub(r'("""|\'\'\')(?:.|\n)*?\1', " ", inspect.getsource(S))

    def test_the_module_names_no_broker_and_no_order_path(self) -> None:
        for forbidden in (
            "baskfy_execution",
            "OrderGateway",
            "place_order",
            "place_gtt",
            "kite",
            "execute_line",
            "rearm_gtt",
        ):
            assert forbidden not in self._code(), f"twt_scan names {forbidden}"

    def test_it_writes_only_the_detector_s_tables(self) -> None:
        """It hands off to ``detect_session``, which writes states, signals and breadth. Nothing
        here constructs a plan, an order or a position."""
        for forbidden in ("TwPlan", "TwOrder", "TwPosition", "TwFill", "TwPlanLine"):
            assert forbidden not in self._code(), f"twt_scan constructs {forbidden}"

    def test_it_never_touches_the_sleeve_s_capital_or_its_flag(self) -> None:
        """The two things no agent-written path may write, on the repo's strictest sleeve."""
        for forbidden in ("sleeve_capital_inr", "TWT_EXECUTION_ENABLED", "execution_enabled"):
            assert forbidden not in self._code(), f"twt_scan names {forbidden}"

    def test_it_calls_the_detector_that_already_exists(self) -> None:
        """DECISIONS-TW TW12.2: one detector, not two. A second implementation of the
        tight-state rule would drift from the first the moment a threshold moved."""
        code = self._code()
        assert "detect_session(" in code
        for reimplemented in ("run_detect_twt", "def detect_session", "tight_state"):
            assert reimplemented not in code, f"twt_scan carries a detector: {reimplemented}"

    def test_the_two_windows_are_the_numbers_the_desk_copies(self) -> None:
        """The desk cannot import this module and copies these two. ``test_twt_scan_desk.py``
        asserts the copy from the other side; this pins the original so a change here fails
        there rather than diverging quietly."""
        assert S.STALE_AFTER_SECONDS == 600
        assert S.MIN_INTERVAL_SECONDS == 60
        assert S.IN_FLIGHT == ("QUEUED", "RUNNING")


class TestTheCapitalIsUntouched:
    """The run never sets ``tw_config.sleeve_capital_inr``, and this is the assertion that says so
    against a live database rather than against a grep."""

    async def test_a_scan_leaves_the_sleeve_s_capital_at_zero(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        await _published(session, AS_OF, version=42)
        row = await _queued(session, user_id)

        await run_twt_scan(session, row.id, now=NOW)

        capital = (
            await session.execute(
                sa.select(TwConfig.sleeve_capital_inr).where(TwConfig.user_id == user_id)
            )
        ).scalar_one()
        assert capital == Decimal("0.00"), (
            "a scan changed the sleeve's capital — the one number no agent-written path may write"
        )
