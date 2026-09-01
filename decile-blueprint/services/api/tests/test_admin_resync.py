"""The resync button's detector and its two endpoints (leaf 3.1).

    "Give a button to resync so it resyncs everything if any data is pending, so I don't have to
     come to this machine."

What is under test here is the *detector*, because the detector is the hard part and because a
wrong answer from it is worse than no button at all — a page that says "nothing pending" over an
87%-empty trading day is how 2026-02-01 survived for months.

Every class has a test, and each one is built from the shape of a real incident rather than from
a shape convenient to assert:

    (a) 2026-08-19..26 — trading sessions with bars and no published run
    (b) 2026-02-01     — 322 bars against a neighbouring 2,310; a presence check calls it fine
    (c) 2026-08-28     — a real Friday marked shut by inference after a failed fetch (M62)
    (d) the Kite access token, which dies overnight with no refresh

The negative cases matter as much: a day at 90% of its neighbours is **not** flagged, a genuine
seeded holiday is **not** second-guessed against NSE, and the seeded future calendar (which runs
to 2026-12-31) is never reported as a gap.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Final

import pytest
from api_helpers import PREFIX, api_settings, bearer, make_user, running_app, url
from cryptography.fernet import Fernet
from screener_helpers import requires_db
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import resync
from baskfy_api.metrics import IST
from baskfy_api.resync import (
    RESYNC_COMPLETED_ACTION,
    RESYNC_REQUESTED_ACTION,
    RESYNC_TASK_NAME,
    ResyncKind,
    inspect_pending,
    window_for,
)
from baskfy_api.settings import Settings
from baskfy_core.models import AdminAction, AppUser, PipelineRun
from baskfy_core.models.reference import INFERRED_HOLIDAY_NAME
from baskfy_providers.settings import ProviderSettings
from baskfy_providers.tokens import IST as TOKEN_IST
from baskfy_providers.tokens import AccessTokenStore

pytestmark = [pytest.mark.db, pytest.mark.redis, requires_db]

#: 21:00 IST on Friday 2026-08-28 — past docs/11's 20:15 publish deadline, so that Friday is in
#: scope. Fixed rather than "now" because the whole point of `window_for` is that it depends on
#: the clock, and a test whose window moves with the wall clock proves nothing about it.
NOW: Final = dt.datetime(2026, 8, 28, 21, 0, tzinfo=IST)

#: A twelve-day window: 2026-08-16 to 2026-08-28. Wide enough for a neighbourhood, narrow enough
#: that every day in it can be named in an assertion.
WINDOW: Final = 12

#: Synthetic instruments, enough that a day's bar count clears `MIN_MEDIAN_BARS`.
UNIVERSE: Final = 200

#: The bar count a healthy day carries in these fixtures.
HEALTHY: Final = 180


def settings() -> Settings:
    return api_settings("postgresql+asyncpg://unused/unused")


async def open_days(session: AsyncSession, now: dt.datetime | None = NOW) -> list[dt.date]:
    start, end = window_for(settings(), days=WINDOW, now=now)
    rows = await session.execute(
        text(
            "SELECT date FROM trading_day WHERE exchange_id = 1 AND is_trading_day "
            "AND date BETWEEN :start AND :end ORDER BY date"
        ),
        {"start": start, "end": end},
    )
    return [row[0] for row in rows]


async def make_universe(session: AsyncSession) -> None:
    await session.execute(
        text(
            "INSERT INTO instrument (exchange_id, symbol, name, series, instrument_type, "
            "is_active) SELECT 1, 'RSYNC' || lpad(i::text, 5, '0'), 'RSYNC ' || i, 'EQ', 'EQ', "
            f"true FROM generate_series(1, {UNIVERSE}) AS i"
        )
    )


async def add_bars(session: AsyncSession, day: dt.date, count: int) -> None:
    """``count`` instruments print a bar on ``day``. The prices are irrelevant; the count is not."""
    if count <= 0:
        return
    await session.execute(
        text(
            "INSERT INTO ohlcv_daily (instrument_id, date, open, high, low, close, volume, "
            "close_raw, volume_raw, adj_factor, source) "
            "SELECT id, :day, 100, 100, 100, 100, 1000, 100, 1000, 1, 'nse' FROM ("
            "  SELECT id FROM instrument WHERE symbol LIKE 'RSYNC%' ORDER BY id LIMIT :count"
            ") AS chosen ON CONFLICT DO NOTHING"
        ),
        {"day": day, "count": count},
    )


async def publish_runs(session: AsyncSession, days: Sequence[dt.date]) -> None:
    """A succeeded, published run for each date — docs/03 step 10's ``data_version`` bump."""
    moment = dt.datetime(2026, 8, 28, 14, 0, tzinfo=dt.UTC)
    for offset, day in enumerate(days):
        session.add(
            PipelineRun(
                trade_date=day,
                status="succeeded",
                started_at=moment,
                finished_at=moment,
                data_version=100 + offset,
            )
        )
    await session.flush()


async def healthy_window(session: AsyncSession, now: dt.datetime | None = NOW) -> list[dt.date]:
    """Every trading day in the window with a full bar count and a published run.

    The baseline the class tests break in exactly one place. Building the *healthy* state first
    and then damaging it is what makes each assertion about one class rather than about the
    fixture.

    ``now=None`` builds it against the wall clock instead of :data:`NOW`. The endpoint tests need
    that: the route reads the clock itself — correctly, since the window's whole job is to stop at
    today — so a fixture pinned to a fixed date would leave the days between it and the real today
    genuinely unpublished, and the endpoint would report findings the test never planted.
    """
    await make_universe(session)
    days = await open_days(session, now)
    for day in days:
        await add_bars(session, day, HEALTHY)
    await publish_runs(session, days)
    await session.flush()
    return days


async def plan_for(
    session: AsyncSession,
    *,
    publication: resync.PublicationSource | None = None,
    now: dt.datetime = NOW,
) -> resync.ResyncPlan:
    return await inspect_pending(
        session, settings=settings(), publication=publication, days=WINDOW, now=now
    )


def findings_of(plan: resync.ResyncPlan, kind: ResyncKind) -> list[resync.ResyncFinding]:
    return [f for f in plan.findings if f.kind is kind]


def yes() -> resync.PublicationCheck:
    async def published(day: dt.date) -> bool:
        del day
        return True

    return published


def no() -> resync.PublicationCheck:
    async def published(day: dt.date) -> bool:
        del day
        return False

    return published


@pytest.fixture
def no_token_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A host with no Kite token store — the local-development shape.

    Without this the class-(d) check reads whatever the developer's own `.secrets/` holds, and
    every other test in the file would pass or fail on their machine's broker credential.
    """
    monkeypatch.setattr(
        "baskfy_providers.settings.get_provider_settings",
        lambda: ProviderSettings(_env_file=None, kite_token_path="", kite_token_encryption_key=""),
    )
    yield


@pytest.fixture
def kite_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[AccessTokenStore]:
    """A real encrypted token store in a temporary directory."""
    key = Fernet.generate_key().decode()
    path = tmp_path / "kite-token.enc"
    monkeypatch.setattr(
        "baskfy_providers.settings.get_provider_settings",
        lambda: ProviderSettings(
            _env_file=None, kite_token_path=str(path), kite_token_encryption_key=key
        ),
    )
    yield AccessTokenStore(path, key)


class TestTheWindow:
    """It must never report the seeded future, and must not cry wolf about today before evening."""

    def test_it_stops_at_yesterday_before_the_publish_deadline(self) -> None:
        """docs/11 puts publish at 20:15 IST. At 11am the nightly has not had its evening yet, and
        a button that reports today as missing every morning is a button nobody presses at 6pm."""
        morning = dt.datetime(2026, 8, 28, 11, 0, tzinfo=IST)
        _, end = window_for(settings(), days=WINDOW, now=morning)
        assert end == dt.date(2026, 8, 27)

    def test_it_includes_today_once_the_deadline_has_passed(self) -> None:
        _, end = window_for(settings(), days=WINDOW, now=NOW)
        assert end == dt.date(2026, 8, 28)

    def test_it_never_reaches_into_the_future(self) -> None:
        """`trading_day` is seeded to 2026-12-31. A naive query reports every remaining session of
        the year as a gap, which is the difference between a useful button and a wall of noise."""
        start, end = window_for(settings(), days=resync.MAX_LOOKBACK_DAYS, now=NOW)
        assert end <= NOW.date()
        assert start < end

    async def test_no_future_trading_day_is_ever_a_finding(
        self, screener_session: AsyncSession, no_token_store: None
    ) -> None:
        del no_token_store
        await healthy_window(screener_session)
        plan = await plan_for(screener_session)
        assert all(f.trade_date is None or f.trade_date <= NOW.date() for f in plan.findings)


class TestClassAMissingRun:
    async def test_a_trading_day_with_no_published_run_is_pending(
        self, screener_session: AsyncSession, no_token_store: None
    ) -> None:
        del no_token_store
        days = await healthy_window(screener_session)
        orphan = days[-3]
        await screener_session.execute(
            text("DELETE FROM pipeline_run WHERE trade_date = :day"), {"day": orphan}
        )

        plan = await plan_for(screener_session)

        missing = findings_of(plan, ResyncKind.MISSING_RUN)
        assert [f.trade_date for f in missing] == [orphan]
        assert "no pipeline run that published" in missing[0].summary

    async def test_a_run_that_failed_the_gate_does_not_count_as_published(
        self, screener_session: AsyncSession, no_token_store: None
    ) -> None:
        """docs/03 step 9 is a hard gate: a run can succeed every step, fail the gate, leave
        `data_version` NULL and the site serving yesterday. That day is pending."""
        del no_token_store
        days = await healthy_window(screener_session)
        orphan = days[-2]
        await screener_session.execute(
            text(
                "UPDATE pipeline_run SET data_version = NULL, status = 'failed' "
                "WHERE trade_date = :day"
            ),
            {"day": orphan},
        )

        plan = await plan_for(screener_session)

        assert [f.trade_date for f in findings_of(plan, ResyncKind.MISSING_RUN)] == [orphan]

    async def test_days_before_the_pipelines_first_run_are_not_reported(
        self, screener_session: AsyncSession, no_token_store: None
    ) -> None:
        """Staging's `pipeline_run` starts on 2026-08-18 and its bars reach back to 2024. Every
        trading day in between has no run and is *correct*: it was backfilled, not run. Reporting
        a hundred and fifty of those buries the one that matters."""
        del no_token_store
        days = await healthy_window(screener_session)
        await screener_session.execute(
            text("DELETE FROM pipeline_run WHERE trade_date < :day"), {"day": days[3]}
        )

        plan = await plan_for(screener_session)

        assert findings_of(plan, ResyncKind.MISSING_RUN) == []


class TestClassBThinBars:
    """2026-02-01: 322 bars against a neighbouring 2,310. "Has bars?" says fine."""

    async def test_a_day_far_below_its_neighbours_is_pending_although_it_has_bars(
        self, screener_session: AsyncSession, no_token_store: None
    ) -> None:
        del no_token_store
        days = await healthy_window(screener_session)
        thin = days[len(days) // 2]
        # The real ratio: 322/2310 = 0.139. Scaled to this fixture's universe.
        await screener_session.execute(
            text("DELETE FROM ohlcv_daily WHERE date = :day"), {"day": thin}
        )
        await add_bars(screener_session, thin, 25)
        await screener_session.flush()

        plan = await plan_for(screener_session)

        found = findings_of(plan, ResyncKind.THIN_BARS)
        assert [f.trade_date for f in found] == [thin]
        assert found[0].observed_bars == 25
        assert found[0].expected_bars == HEALTHY
        # The whole point: a presence check would have passed this day.
        assert found[0].observed_bars is not None and found[0].observed_bars > 0

    async def test_ordinary_variation_is_not_a_gap(
        self, screener_session: AsyncSession, no_token_store: None
    ) -> None:
        """The universe grows — 2,550 bars a session in July 2026, 3,014 six weeks later. A
        detector that flags a 10% dip would fire every week and be switched off."""
        del no_token_store
        days = await healthy_window(screener_session)
        dipped = days[len(days) // 2]
        await screener_session.execute(
            text("DELETE FROM ohlcv_daily WHERE date = :day"), {"day": dipped}
        )
        await add_bars(screener_session, dipped, int(HEALTHY * 0.9))
        await screener_session.flush()

        plan = await plan_for(screener_session)

        assert findings_of(plan, ResyncKind.THIN_BARS) == []

    async def test_one_inflated_neighbour_does_not_hide_a_thin_day(
        self, screener_session: AsyncSession, no_token_store: None
    ) -> None:
        """2026-08-31 carries 9,225 rows against neighbours near 3,000. Judged against a *mean*
        the day beside it could be half empty and still pass; the median shrugs the outlier off."""
        del no_token_store
        days = await healthy_window(screener_session)
        thin, inflated = days[len(days) // 2], days[len(days) // 2 + 1]
        await screener_session.execute(
            text("DELETE FROM ohlcv_daily WHERE date = :day"), {"day": thin}
        )
        await add_bars(screener_session, thin, 25)
        await add_bars(screener_session, inflated, UNIVERSE)
        await screener_session.flush()

        plan = await plan_for(screener_session)

        assert thin in [f.trade_date for f in findings_of(plan, ResyncKind.THIN_BARS)]

    async def test_an_empty_window_is_not_reported_as_a_wall_of_thin_days(
        self, screener_session: AsyncSession, no_token_store: None
    ) -> None:
        """A database whose backfill has not arrived is not a database full of gaps."""
        del no_token_store
        await make_universe(screener_session)
        await publish_runs(screener_session, await open_days(screener_session))
        await screener_session.flush()

        plan = await plan_for(screener_session)

        assert findings_of(plan, ResyncKind.THIN_BARS) == []


class TestClassCWrongInferredHoliday:
    """2026-08-28: a real trading Friday marked shut by inference after a failed fetch (M62).

    Once marked, it was excluded from every backfill — they all iterate trading days — so it could
    never heal itself.
    """

    async def _mark_inferred(self, session: AsyncSession, day: dt.date) -> None:
        await session.execute(
            text(
                "UPDATE trading_day SET is_trading_day = false, source = 'bhavcopy', "
                "holiday_name = :name WHERE exchange_id = 1 AND date = :day"
            ),
            {"day": day, "name": INFERRED_HOLIDAY_NAME},
        )
        await session.execute(text("DELETE FROM ohlcv_daily WHERE date = :day"), {"day": day})
        await session.flush()

    async def test_a_weekday_nse_published_for_is_pending(
        self, screener_session: AsyncSession, no_token_store: None
    ) -> None:
        del no_token_store
        days = await healthy_window(screener_session)
        friday = days[-1]
        await self._mark_inferred(screener_session, friday)

        plan = await plan_for(screener_session, publication=yes)

        found = findings_of(plan, ResyncKind.WRONG_HOLIDAY)
        assert [f.trade_date for f in found] == [friday]
        assert "every backfill skips it" in found[0].summary

    async def test_a_day_nse_published_nothing_for_stays_a_holiday(
        self, screener_session: AsyncSession, no_token_store: None
    ) -> None:
        """The inference is usually right — it is what fills in the lunar-calendar holidays the
        seed list has never carried. Only the exchange's own file overrules it."""
        del no_token_store
        days = await healthy_window(screener_session)
        await self._mark_inferred(screener_session, days[-1])

        plan = await plan_for(screener_session, publication=no)

        assert findings_of(plan, ResyncKind.WRONG_HOLIDAY) == []

    async def test_a_seeded_circular_holiday_is_never_second_guessed(
        self, screener_session: AsyncSession, no_token_store: None
    ) -> None:
        """`source='holiday'` came from NSE's own circular. Asking the network to check it would
        be both wrong and two hundred requests a year."""
        del no_token_store
        days = await healthy_window(screener_session)
        await screener_session.execute(
            text(
                "UPDATE trading_day SET is_trading_day = false, source = 'holiday', "
                "holiday_name = 'Independence Day' WHERE exchange_id = 1 AND date = :day"
            ),
            {"day": days[-1]},
        )
        await screener_session.flush()

        plan = await plan_for(screener_session, publication=yes)

        assert findings_of(plan, ResyncKind.WRONG_HOLIDAY) == []

    async def test_being_unable_to_ask_nse_is_reported_not_treated_as_clean(
        self, screener_session: AsyncSession, no_token_store: None
    ) -> None:
        """Treating "cannot check" as "nothing found" is the exact shape of the bug this catches,
        so a host with no NSE provider says so out loud instead of returning a clean plan."""
        del no_token_store
        days = await healthy_window(screener_session)
        friday = days[-1]
        await self._mark_inferred(screener_session, friday)

        plan = await plan_for(screener_session, publication=lambda: None)

        assert findings_of(plan, ResyncKind.WRONG_HOLIDAY) == []
        assert any(
            friday.isoformat() in note and "neither cleared nor flagged" in note
            for note in plan.unresolved
        )


class TestClassDKiteSession:
    async def test_an_absent_token_is_pending(
        self, screener_session: AsyncSession, kite_store: AccessTokenStore
    ) -> None:
        await healthy_window(screener_session)
        assert not kite_store.exists()

        plan = await plan_for(screener_session)

        found = findings_of(plan, ResyncKind.KITE_SESSION)
        assert len(found) == 1
        assert found[0].trade_date is None
        assert "No Kite access token" in found[0].summary

    async def test_a_token_issued_yesterday_is_pending(
        self, screener_session: AsyncSession, kite_store: AccessTokenStore
    ) -> None:
        """docs/09 calls Kite token expiry "the #1 pipeline failure": the token dies at the next
        pre-open, so "a file exists" and "there is a session" are different questions."""
        await healthy_window(screener_session)
        kite_store.save(
            "abcdefghijklmnop", issued_at=dt.datetime(2026, 8, 27, 9, 0, tzinfo=TOKEN_IST)
        )

        plan = await plan_for(screener_session)

        found = findings_of(plan, ResyncKind.KITE_SESSION)
        assert len(found) == 1
        assert "2026-08-27" in found[0].summary

    async def test_a_token_issued_today_is_not_pending(
        self, screener_session: AsyncSession, kite_store: AccessTokenStore
    ) -> None:
        await healthy_window(screener_session)
        kite_store.save("abcdefghijklmnop", issued_at=NOW)

        plan = await plan_for(screener_session)

        assert findings_of(plan, ResyncKind.KITE_SESSION) == []

    async def test_a_host_with_no_token_store_says_so_rather_than_reporting_health(
        self, screener_session: AsyncSession, no_token_store: None
    ) -> None:
        del no_token_store
        await healthy_window(screener_session)

        plan = await plan_for(screener_session)

        assert findings_of(plan, ResyncKind.KITE_SESSION) == []
        assert any("No Kite token store is configured" in note for note in plan.unresolved)


class TestTheInspectionChangesNothing:
    """G1 and half of G3. The preview is a read, and reads are repeatable."""

    async def test_two_inspections_in_a_row_agree(
        self, screener_session: AsyncSession, no_token_store: None
    ) -> None:
        del no_token_store
        days = await healthy_window(screener_session)
        await screener_session.execute(
            text("DELETE FROM pipeline_run WHERE trade_date = :day"), {"day": days[-1]}
        )

        first = await plan_for(screener_session)
        second = await plan_for(screener_session)

        assert first == second
        assert first.pending

    async def test_it_writes_no_row(
        self, screener_session: AsyncSession, no_token_store: None
    ) -> None:
        del no_token_store
        await healthy_window(screener_session)

        async def counts() -> tuple[int, int, int]:
            rows = (
                await screener_session.execute(
                    text(
                        "SELECT (SELECT count(*) FROM ohlcv_daily), "
                        "(SELECT count(*) FROM trading_day), (SELECT count(*) FROM admin_action)"
                    )
                )
            ).one()
            return int(rows[0]), int(rows[1]), int(rows[2])

        before = await counts()
        await plan_for(screener_session)
        assert await counts() == before


class TestTheEndpoints:
    async def test_the_dry_inspection_reports_what_is_pending_and_why(
        self, screener_session: AsyncSession, no_token_store: None
    ) -> None:
        del no_token_store
        headers = await _staff(screener_session)
        days = await healthy_window(screener_session, now=None)
        await screener_session.execute(
            text("DELETE FROM pipeline_run WHERE trade_date = :day"), {"day": days[-1]}
        )
        await screener_session.flush()
        conf = api_settings(str(screener_session.bind.engine.url))

        async with running_app(conf, screener_session) as client:
            response = await client.get(url("/admin/resync"), params={"days": WINDOW})

        assert response.status_code == 401
        async with running_app(conf, screener_session) as client:
            response = await client.get(
                url("/admin/resync"), params={"days": WINDOW}, headers=headers
            )
        assert response.status_code == 200
        body = response.json()
        assert body["pending"] is True
        assert body["trading_days_checked"] > 0
        assert {f["kind"] for f in body["findings"]} == {ResyncKind.MISSING_RUN.value}
        assert body["findings"][0]["remedy"]

    async def test_nothing_pending_is_a_real_answer(
        self, screener_session: AsyncSession, no_token_store: None
    ) -> None:
        """The common case, and the one a spinner-that-resolves-to-silence gets wrong."""
        del no_token_store
        headers = await _staff(screener_session)
        await healthy_window(screener_session, now=None)
        conf = api_settings(str(screener_session.bind.engine.url))

        async with running_app(conf, screener_session) as client:
            response = await client.get(
                url("/admin/resync"), params={"days": WINDOW}, headers=headers
            )

        assert response.status_code == 200
        body = response.json()
        assert body["pending"] is False
        assert body["findings"] == []
        assert body["last_resync"] is None

    async def test_pressing_it_queues_the_task_and_writes_an_audit_row(
        self, screener_session: AsyncSession, no_token_store: None
    ) -> None:
        del no_token_store
        headers = await _staff(screener_session)
        days = await healthy_window(screener_session, now=None)
        await screener_session.execute(
            text("DELETE FROM pipeline_run WHERE trade_date = :day"), {"day": days[-1]}
        )
        await screener_session.flush()
        queue = _RecordingQueue()
        conf = api_settings(str(screener_session.bind.engine.url))

        async with running_app(conf, screener_session, task_queue=queue) as client:
            response = await client.post(
                url("/admin/resync"), params={"days": WINDOW}, headers=headers
            )

        assert response.status_code == 202
        assert response.json()["task"] == RESYNC_TASK_NAME
        assert [name for name, _ in queue.sent] == [RESYNC_TASK_NAME]
        row = (
            await screener_session.execute(
                select(AdminAction).where(AdminAction.action == RESYNC_REQUESTED_ACTION)
            )
        ).scalar_one()
        detail = row.detail
        assert detail is not None
        assert detail["pending_at_request"] == 1
        assert detail["kinds_at_request"] == [ResyncKind.MISSING_RUN.value]

    async def test_the_last_repairs_own_report_is_rendered_beside_the_plan(
        self, screener_session: AsyncSession, no_token_store: None
    ) -> None:
        """G7 reaches the operator only if the API hands the page what the repair could not do."""
        del no_token_store
        headers = await _staff(screener_session)
        await healthy_window(screener_session, now=None)
        staff = (
            await screener_session.execute(
                select(AppUser).where(AppUser.email == "ops@example.com")
            )
        ).scalar_one()
        screener_session.add(
            AdminAction(
                actor_user_id=staff.id,
                action=RESYNC_COMPLETED_ACTION,
                target="2026-08-16..2026-08-28",
                detail={
                    "repaired": ["2026-08-27: re-ingested 2,304 bars from the NSE bhavcopy"],
                    "failed": ["2026-08-26: NSE published no bhavcopy for this date"],
                    "deferred": [],
                    "still_pending": ["2026-08-26 holds 0 bars"],
                    "unresolved": [],
                    "complete": False,
                },
            )
        )
        await screener_session.flush()
        conf = api_settings(str(screener_session.bind.engine.url))

        async with running_app(conf, screener_session) as client:
            response = await client.get(
                url("/admin/resync"), params={"days": WINDOW}, headers=headers
            )

        last = response.json()["last_resync"]
        assert last["complete"] is False
        assert last["failed"] == ["2026-08-26: NSE published no bhavcopy for this date"]
        assert last["still_pending"] == ["2026-08-26 holds 0 bars"]
        assert last["actor"] == "ops@example.com"

    async def test_a_signed_in_non_staff_caller_gets_404_on_both(
        self, screener_session: AsyncSession
    ) -> None:
        """G4. `test_api_admin.py` derives this from the OpenAPI document for every admin route;
        naming the two new ones here as well means a reader of *this* file can see the gate."""
        _, public_id = await make_user(screener_session, "civilian-resync@example.com")
        headers = bearer(public_id)
        conf = api_settings(str(screener_session.bind.engine.url))

        async with running_app(conf, screener_session) as client:
            for method in ("GET", "POST"):
                response = await client.request(method, f"{PREFIX}/admin/resync", headers=headers)
                assert response.status_code == 404, f"{method} answered {response.status_code}"


class TestItCannotPlaceAnOrder:
    """G5, and CLAUDE.md non-negotiable #1: orders fire only from `POST /execute` with a plan id.

    Asserted over the source of all three modules rather than by trying to make one place an
    order, because the guarantee wanted is "there is no such path", and only reading the file can
    say that.
    """

    ROOT = Path(__file__).resolve().parents[3]
    SOURCES = (
        "services/api/src/baskfy_api/routers/admin.py",
        "services/api/src/baskfy_api/resync.py",
        "services/worker/src/baskfy_worker/tasks/resync.py",
    )
    FORBIDDEN = (
        "OrderGateway",
        "place_order",
        "place_gtt",
        "baskfy_execution",
        "packages.execution",
    )

    def test_no_resync_module_can_reach_an_order(self) -> None:
        for relative in self.SOURCES:
            source = (self.ROOT / relative).read_text(encoding="utf-8")
            for token in self.FORBIDDEN:
                assert token not in source, f"{relative} names {token}"

    def test_the_files_exist_so_this_would_not_pass_vacuously(self) -> None:
        for relative in self.SOURCES:
            assert (self.ROOT / relative).is_file(), relative


class _RecordingQueue:
    """Stands in for the Celery producer. Records what would have been published."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, list[object]]] = []

    def send_task(self, name: str, args: Sequence[object]) -> object:
        self.sent.append((name, list(args)))
        return f"task-{len(self.sent)}"


async def _staff(session: AsyncSession, email: str = "ops@example.com") -> dict[str, str]:
    _, public_id = await make_user(session, email)
    user = (
        await session.execute(select(AppUser).where(AppUser.public_id == public_id))
    ).scalar_one()
    user.is_staff = True
    await session.flush()
    return bearer(public_id)
