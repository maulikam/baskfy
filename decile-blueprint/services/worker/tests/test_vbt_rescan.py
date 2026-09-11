"""VB12 — the worker half of the desk's **Re-detect** button.

The desk writes a `QUEUED` row and has no Celery client; this is everything that happens next.

* **the sweep publishes what nobody has published** — rows with no `task_id`, which is exactly
  what the desk leaves behind, and leaves the API's own rows alone;
* **the task claims before it works**, so a beat that overlaps a slow broker cannot detect the
  same session twice;
* **it re-detects a published session**, never today, because VBT-1's signal is a closed-day fact
  (`04` §10);
* **a failure lands on the row**, not in the worker's logs alone, because the button has to be
  able to show what went wrong;
* **it is idempotent** — pressing twice overwrites the same rows and moves no counter.
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

from baskfy_core.models import AppUser, TradingDay, VbConfig, VbScanRun
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_worker.tasks import vbt_rescan as R
from baskfy_worker.tasks.vbt_rescan import (
    claim_run,
    latest_published_session,
    newest_run,
    run_vbt_rescan,
    unpublished_runs,
)

pytestmark = [requires_db, pytest.mark.db]

AS_OF = dt.date(2026, 8, 18)
NOW = dt.datetime(2026, 8, 18, 21, 40, tzinfo=dt.UTC)


async def _user(session: AsyncSession, public_id: str = "vb12") -> int:
    user = AppUser(public_id=public_id, email=f"{public_id}@example.com")
    session.add(user)
    await session.flush()
    session.add(VbConfig(user_id=user.id, sleeve_capital_inr=Decimal("2500000.00"), updated_by="t"))
    await session.flush()
    return int(user.id)


async def _is_session(session: AsyncSession, day: dt.date) -> bool:
    """Is this a trading day on the fixture's own calendar?

    The seeded database already carries the exchange calendar (2011-2026, 4,089 days), so these
    tests **read** it rather than inserting their own: inserting a duplicate is an integrity
    error, and inventing a second calendar would test a fixture instead of the query.
    """
    return bool(
        (
            await session.execute(
                sa.select(TradingDay.date).where(
                    TradingDay.exchange_id == NSE_EXCHANGE_ID,
                    TradingDay.date == day,
                    TradingDay.is_trading_day.is_(True),
                )
            )
        ).scalar_one_or_none()
    )


async def _queued(session: AsyncSession, user_id: int, **overrides: object) -> VbScanRun:
    row = VbScanRun(user_id=user_id, requested_at=NOW, status="QUEUED", source="desk")
    for key, value in overrides.items():
        setattr(row, key, value)
    session.add(row)
    await session.flush()
    return row


class TestTheSweep:
    async def test_it_publishes_a_row_the_desk_left_behind(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        row = await _queued(session, user_id)

        pending = await unpublished_runs(session)

        assert [one.id for one in pending] == [row.id]

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
        """A beat that overlaps a slow broker can publish the same row twice; the second copy
        must do nothing rather than detect the same session again."""
        user_id = await _user(session)
        row = await _queued(session, user_id)

        assert await claim_run(session, row.id) is not None
        assert await claim_run(session, row.id) is None

    async def test_an_unknown_id_answers_none(self, session: AsyncSession) -> None:
        assert await claim_run(session, 999_999) is None


class TestWhichSessionItDetects:
    async def test_it_is_the_latest_trading_day_at_or_before_the_date(
        self, session: AsyncSession
    ) -> None:
        assert await _is_session(session, AS_OF), "the fixture calendar changed under this test"

        assert await latest_published_session(session, AS_OF) == AS_OF

    async def test_a_sunday_falls_back_to_the_friday(self, session: AsyncSession) -> None:
        """16 Aug 2026 is a Sunday. The answer must be a session, never a calendar date."""
        sunday = dt.date(2026, 8, 16)
        assert not await _is_session(session, sunday)

        answer = await latest_published_session(session, sunday)

        assert answer is not None
        assert answer < sunday
        assert answer.weekday() < 5
        assert await _is_session(session, answer)

    async def test_a_date_before_the_calendar_begins_answers_none(
        self, session: AsyncSession
    ) -> None:
        """Not a failure — the exchange calendar starts in 2011, and asking before it is the
        state a fresh box is in before the reference-data job has run."""
        assert await latest_published_session(session, dt.date(2009, 1, 1)) is None


class TestTheRun:
    async def test_a_run_with_no_calendar_fails_on_the_row_rather_than_raising(
        self, session: AsyncSession
    ) -> None:
        """The button has to be able to show what went wrong."""
        user_id = await _user(session)
        row = await _queued(session, user_id)
        # A stamp before the exchange calendar begins: there is no session to detect.
        before_the_calendar = dt.datetime(2009, 1, 1, 21, 40, tzinfo=dt.UTC)

        result = await run_vbt_rescan(session, row.id, now=before_the_calendar)

        assert result["status"] == "FAILED"
        await session.refresh(row)
        assert row.status == "FAILED"
        assert row.error is not None and "trading day" in row.error
        assert row.finished_at is not None

    async def test_a_run_over_a_quiet_session_finishes_done_with_its_funnel(
        self, session: AsyncSession
    ) -> None:
        """No bars is a legitimate answer — `run_detect_vbt` returns rather than raises — and the
        row should say DONE with the counts, not FAILED."""
        user_id = await _user(session)
        row = await _queued(session, user_id)

        result = await run_vbt_rescan(session, row.id, now=NOW)

        assert result["status"] == "DONE"
        await session.refresh(row)
        assert row.session_date == AS_OF
        assert row.detail is not None
        assert row.detail["signals"] == 0
        assert row.finished_at is not None and row.started_at is not None

    async def test_a_row_that_is_not_queued_is_skipped(self, session: AsyncSession) -> None:
        user_id = await _user(session)
        row = await _queued(session, user_id, status="DONE")

        result = await run_vbt_rescan(session, row.id, now=NOW)

        assert "skipped" in result

    async def test_pressing_twice_writes_two_rows_and_detects_the_same_session(
        self, session: AsyncSession
    ) -> None:
        """House rule 7: detection is idempotent per (user, date). Two requests are two audit
        rows and one set of signals."""
        user_id = await _user(session)
        first = await _queued(session, user_id)
        second = await _queued(session, user_id)

        one = await run_vbt_rescan(session, first.id, now=NOW)
        two = await run_vbt_rescan(session, second.id, now=NOW + dt.timedelta(minutes=2))

        assert one["session_date"] == two["session_date"] == AS_OF.isoformat()
        assert (
            await session.execute(sa.select(sa.func.count()).select_from(VbScanRun))
        ).scalar_one() == 2

    async def test_another_users_row_detects_for_that_user(self, session: AsyncSession) -> None:
        """`02` Track C §6: the row carries the owner and the detection is written for them."""
        mine = await _user(session, "vb12-a")
        theirs = await _user(session, "vb12-b")
        row = await _queued(session, theirs)

        await run_vbt_rescan(session, row.id, now=NOW)

        assert (await newest_run(session, user_id=theirs)) is not None
        assert (await newest_run(session, user_id=mine)) is None


class TestItCannotOrder:
    def test_the_module_names_no_broker_and_no_order_path(self) -> None:
        source = inspect.getsource(R)
        code = re.sub(r'("""|\'\'\')(?:.|\n)*?\1', " ", source)
        for forbidden in (
            "baskfy_execution",
            "OrderGateway",
            "place_order",
            "place_gtt",
            "kite",
            "execute_line",
        ):
            assert forbidden not in code, f"vbt_rescan names {forbidden}"

    def test_it_writes_only_the_detector_s_tables(self) -> None:
        """It hands off to `run_detect_vbt`, which writes signals and breadth. Nothing here
        constructs a plan, an order or a position."""
        source = inspect.getsource(R)
        code = re.sub(r'("""|\'\'\')(?:.|\n)*?\1', " ", source)
        for forbidden in ("VbPlan", "VbOrder", "VbPosition", "VbFill"):
            assert forbidden not in code, f"vbt_rescan constructs {forbidden}"
