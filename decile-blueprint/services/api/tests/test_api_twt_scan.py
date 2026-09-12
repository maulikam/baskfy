"""The TWT sleeve's "Scan now", over HTTP against a real database (TW12).

`PLAN-SCAN-SYNC.md` fixes one shape across the sleeves — **202** queued, **409** one already in
flight, **429** one a minute, and `GET /twt/scan/{run_id}` for the poll — so a page written
against the swing book's button works here without learning a second vocabulary. These are the
claims that shape is worth nothing without:

* **the run is a row before it is a message.** A broker that is down is still a 202 with a
  `QUEUED` row and no `task_id`, because `twt-scan-publish` publishes exactly those. The desk's own
  button has always depended on it, having no Celery client at all;
* **it queues the detector that already exists.** The published name is `baskfy.twt.scan`, whose
  worker calls `twt.detect_session` — the function `baskfy.twt.detect` calls. Asserted literally,
  because a second detector behind the same rows would have none of TW4's tests;
* **both refusals are answered from the table**, not from a cache, so they hold on a box with no
  Redis and are testable against the database alone;
* **a dead worker does not wedge the button**: past `twt_scan_stale_after_seconds`, a `RUNNING`
  row is history;
* **it is one person's**, on both verbs;
* **and it moves no money.** On this sleeve that is the whole point: the row it writes is the only
  row it writes, nothing on the plan, the order, the position or the fill tables moves, and
  `tw_config.sleeve_capital_inr` is still whatever it was — asserted after a press rather than
  reasoned about.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal

import api_helpers
import pytest
import sqlalchemy as sa
from api_helpers import bearer, make_user, running_app, url
from screener_helpers import requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.deferred_publish import drain_deferred_publishes
from baskfy_api.settings import Settings
from baskfy_core.models import (
    TwConfig,
    TwOrder,
    TwPlan,
    TwPlanLine,
    TwPosition,
    TwScanRun,
)

pytestmark = [requires_db, pytest.mark.db]

#: The detector's own answer, as `twt.detect_session` returns it and the task copies it onto the
#: row. The shape a page binds to on DONE.
DETAIL = {"date": "2026-09-11", "signals": 0, "status": "OK"}

#: What the sleeve's capital is seeded at here, and what it must still be afterwards. **Not zero
#: on purpose**: a test that seeded 0 and asserted 0 would pass against a route that set it to 0.
CAPITAL = Decimal("1000000.00")


@pytest.fixture
def settings(seeded_url: str) -> Settings:
    return api_helpers.api_settings(seeded_url)


async def _sole_tenant(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> tuple[int, str]:
    user_id, public_id = await make_user(session, "twt-scan-sole@example.com")
    monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
    session.add(TwConfig(user_id=user_id, sleeve_capital_inr=CAPITAL, updated_by="test"))
    await session.flush()
    return user_id, public_id


class RecordingQueue:
    """The Celery producer, recording. The contract is *that the name was published*."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, list[object]]] = []

    def send_task(self, name: str, args: Sequence[object]) -> object:
        self.sent.append((name, list(args)))
        return f"task-{len(self.sent)}"


class TestScanNow:
    async def test_a_request_is_a_202_with_the_run_to_poll_and_the_task_published(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        queue = RecordingQueue()

        async with running_app(settings, screener_session, task_queue=queue) as client:
            response = await client.post(url("/twt/scan"), headers=bearer(public_id))
            assert response.status_code == 202, response.text
            run_id = response.json()["run_id"]
            assert queue.sent == [], "publish waits for commit (audit 4.13)"
            read = await client.get(url(f"/twt/scan/{run_id}"), headers=bearer(public_id))

        drain_deferred_publishes(screener_session)
        await screener_session.flush()
        assert response.json()["status"] == "QUEUED"
        assert queue.sent == [("baskfy.twt.scan", [run_id])], "a second detector was queued"
        row = (
            await screener_session.execute(sa.select(TwScanRun).where(TwScanRun.id == run_id))
        ).scalar_one()
        assert row.user_id == user_id
        assert row.task_id == "task-1"
        assert row.source == "web", "the row must say which button was pressed"
        assert read.status_code == 200
        body = read.json()
        assert body["run_id"] == run_id
        assert body["status"] == "QUEUED"
        assert body["session_date"] is None, "only the worker decides which session"
        assert body["detail"] is None
        assert body["error"] is None

    async def test_the_answer_has_no_provisional_field(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DECISIONS-TW TW12.2, as a payload assertion.

        The swing book's run carries `provisional`, because its setups can be read off a bar still
        forming. This strategy's cannot — `04` §2 measures three *closed* weekly ranges — so a
        page that saw the field would be offered a freshness the strategy has no use for. Absence
        is cheaper to keep honest than a field that is always false.
        """
        _, public_id = await _sole_tenant(screener_session, monkeypatch)

        async with running_app(settings, screener_session, task_queue=RecordingQueue()) as client:
            posted = await client.post(url("/twt/scan"), headers=bearer(public_id))
            read = await client.get(
                url(f"/twt/scan/{posted.json()['run_id']}"), headers=bearer(public_id)
            )

        assert "provisional" not in read.json()
        # An exact set, so a field added to this payload is reviewed rather than noticed. ``found``
        # joined it on 12 Sep 2026: the page could say a run had finished but not what it found,
        # and "DONE" is not a sentence anybody wants to read about their own money.
        assert set(read.json()) == {
            "run_id",
            "status",
            "requested_at",
            "started_at",
            "finished_at",
            "session_date",
            "detail",
            "error",
            "found",
        }
        assert read.json()["found"] is None, "a queued run has not looked at anything yet"

    async def test_the_status_vocabulary_is_the_contract_s_four(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`QUEUED | RUNNING | DONE | FAILED`, read back verbatim — a page binds to these."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        rows: dict[str, int] = {}
        for index, status in enumerate(("QUEUED", "RUNNING", "DONE", "FAILED")):
            row = TwScanRun(
                user_id=user_id,
                requested_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=index + 1),
                status=status,
                source="desk",
                detail=(DETAIL if status == "DONE" else None),
                error="ValueError: no session" if status == "FAILED" else None,
            )
            screener_session.add(row)
            await screener_session.flush()
            rows[status] = int(row.id)

        seen: dict[str, dict[str, object]] = {}
        async with running_app(settings, screener_session) as client:
            for status, rid in rows.items():
                read = await client.get(url(f"/twt/scan/{rid}"), headers=bearer(public_id))
                seen[status] = read.json()

        assert {status: body["status"] for status, body in seen.items()} == {
            s: s for s in ("QUEUED", "RUNNING", "DONE", "FAILED")
        }
        assert seen["DONE"]["detail"] == DETAIL
        assert seen["FAILED"]["error"] == "ValueError: no session"

    async def test_a_second_request_while_one_is_in_flight_is_a_409_naming_it(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, public_id = await _sole_tenant(screener_session, monkeypatch)
        queue = RecordingQueue()

        async with running_app(settings, screener_session, task_queue=queue) as client:
            first = await client.post(url("/twt/scan"), headers=bearer(public_id))
            second = await client.post(url("/twt/scan"), headers=bearer(public_id))

        drain_deferred_publishes(screener_session)
        assert first.status_code == 202
        assert second.status_code == 409
        body = second.json()
        assert body["type"] == "scan-in-flight"
        assert body["run_id"] == first.json()["run_id"]
        assert body["status_of_run"] == "QUEUED"
        assert len(queue.sent) == 1, "the second press published nothing"

    async def test_a_request_inside_a_minute_of_a_finished_one_is_a_429_with_retry_after(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        screener_session.add(
            TwScanRun(
                user_id=user_id,
                requested_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=20),
                status="DONE",
                source="desk",
            )
        )
        await screener_session.flush()

        async with running_app(settings, screener_session, task_queue=RecordingQueue()) as client:
            response = await client.post(url("/twt/scan"), headers=bearer(public_id))

        assert response.status_code == 429
        assert response.json()["type"] == "rate-limited"
        assert 30 <= response.json()["retry_after"] <= 41
        assert response.headers["Retry-After"] == str(response.json()["retry_after"])
        assert "one a minute" in response.json()["detail"]

    async def test_a_stale_run_no_longer_blocks_the_button(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A worker that died mid-scan must not lock the button for an afternoon."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        screener_session.add(
            TwScanRun(
                user_id=user_id,
                requested_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(minutes=20),
                status="RUNNING",
                source="desk",
            )
        )
        await screener_session.flush()

        async with running_app(settings, screener_session, task_queue=RecordingQueue()) as client:
            response = await client.post(url("/twt/scan"), headers=bearer(public_id))

        assert response.status_code == 202

    async def test_the_two_thresholds_are_settings_rather_than_literals(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """House rule: a threshold is a setting. Ninety seconds is outside the default minute and
        inside a two-minute one, and the route's answer must move with the number."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        screener_session.add(
            TwScanRun(
                user_id=user_id,
                requested_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=90),
                status="DONE",
                source="desk",
            )
        )
        await screener_session.flush()

        # The refusal first: a 202 would write a QUEUED row and the next press would be the
        # *other* refusal (409, one in flight), which proves nothing about the interval.
        wider = settings.model_copy(update={"twt_scan_min_interval_seconds": 120})
        async with running_app(wider, screener_session, task_queue=RecordingQueue()) as client:
            refused = await client.post(url("/twt/scan"), headers=bearer(public_id))

        async with running_app(settings, screener_session, task_queue=RecordingQueue()) as client:
            allowed = await client.post(url("/twt/scan"), headers=bearer(public_id))

        assert refused.status_code == 429
        assert allowed.status_code == 202

    async def test_a_broker_that_is_down_leaves_the_row_queued_for_the_sweep(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The row is the request; publishing is the fast path. A broker that refuses is a 202 all
        the same, with no `task_id`, and `twt-scan-publish` publishes it within the minute."""
        _, public_id = await _sole_tenant(screener_session, monkeypatch)

        class BrokerDown:
            def send_task(self, name: str, args: Sequence[object]) -> object:
                raise ConnectionError("redis: connection refused")

        async with running_app(settings, screener_session, task_queue=BrokerDown()) as client:
            response = await client.post(url("/twt/scan"), headers=bearer(public_id))

        assert response.status_code == 202
        row = (
            await screener_session.execute(
                sa.select(TwScanRun).where(TwScanRun.id == response.json()["run_id"])
            )
        ).scalar_one()
        assert row.status == "QUEUED" and row.task_id is None


class TestItMovesNoMoney:
    """The non-negotiable, over HTTP: a scan queues a detector and writes one row.

    `PLAN-SCAN-SYNC.md` rule 3 — "nothing new may reach `OrderGateway.place`" — is asserted
    structurally in `test_twt_readonly.py` and behaviourally against the desk's routes in
    `packages/core/tests/test_twt_safety_properties.py`. This is the API's half: after a press,
    every table that could represent money is still empty, and the sleeve's capital has not moved.
    """

    async def test_a_press_writes_the_scan_row_and_nothing_else(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)

        async with running_app(settings, screener_session, task_queue=RecordingQueue()) as client:
            response = await client.post(url("/twt/scan"), headers=bearer(public_id))

        assert response.status_code == 202
        scans = (
            await screener_session.execute(
                sa.select(sa.func.count())
                .select_from(TwScanRun)
                .where(TwScanRun.user_id == user_id)
            )
        ).scalar_one()
        assert scans == 1
        for model in (TwPlan, TwPlanLine, TwOrder, TwPosition):
            count = (
                await screener_session.execute(sa.select(sa.func.count()).select_from(model))
            ).scalar_one()
            assert count == 0, f"a scan wrote to {model.__tablename__}"

    async def test_a_press_does_not_touch_the_sleeve_s_capital(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one number on this sleeve no agent-written path may write, and the rail
        `PLAN-SCAN-SYNC.md` names twice. Seeded non-zero so the assertion cannot pass by
        coincidence."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)

        async with running_app(settings, screener_session, task_queue=RecordingQueue()) as client:
            assert (
                await client.post(url("/twt/scan"), headers=bearer(public_id))
            ).status_code == 202

        capital = (
            await screener_session.execute(
                sa.select(TwConfig.sleeve_capital_inr).where(TwConfig.user_id == user_id)
            )
        ).scalar_one()
        assert capital == CAPITAL, "a scan changed the sleeve's capital"


class TestTheScanBelongsToOnePerson:
    async def test_a_stranger_is_refused_on_both_verbs_and_cannot_read_the_run(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, public_id = await _sole_tenant(screener_session, monkeypatch)
        _, other = await make_user(screener_session, "twt-scan-stranger@example.com")

        async with running_app(settings, screener_session, task_queue=RecordingQueue()) as client:
            mine = await client.post(url("/twt/scan"), headers=bearer(public_id))
            run_id = mine.json()["run_id"]
            refused_post = await client.post(url("/twt/scan"), headers=bearer(other))
            refused_get = await client.get(url(f"/twt/scan/{run_id}"), headers=bearer(other))
            missing = await client.get(url("/twt/scan/999999"), headers=bearer(public_id))

        assert mine.status_code == 202
        assert refused_post.status_code in (403, 404)
        assert refused_get.status_code in (403, 404)
        assert missing.status_code == 404

    async def test_an_anonymous_caller_gets_nothing(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _sole_tenant(screener_session, monkeypatch)

        async with running_app(settings, screener_session, task_queue=RecordingQueue()) as client:
            posted = await client.post(url("/twt/scan"))
            read = await client.get(url("/twt/scan/1"))

        assert posted.status_code == 401
        assert read.status_code == 401


class TestThePageCanSayWhenItLastScanned:
    """THE GAP OF 12 SEP 2026, on this sleeve.

    "None of the scan shows when the last scan performed in any strategy." The hub has had a
    control and a copy function for the last run since TW12, and both were being handed `None`.
    The box's own ``tw_scan_run`` held two finished runs at the time — 16:45 and 16:59 IST on
    12 Sep, about nineteen seconds each — and the page said nothing about either.

    What a reader wants is three facts: **when it ran, whether it finished, and whether it found
    anything.** The first two the row has always carried. The third is ``found``, lifted out of
    the detail the worker wrote, where `0` is a real answer — this strategy signals about
    eighteen times a year, so most sessions have none — and `None` is "the run did not say".
    A page that could not tell those apart would read a quiet week as a broken job.
    """

    async def test_the_day_s_payload_names_the_newest_run(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, public_id = await _sole_tenant(screener_session, monkeypatch)

        async with running_app(settings, screener_session, task_queue=RecordingQueue()) as client:
            before = await client.get(url("/twt/today"), headers=bearer(public_id))
            posted = await client.post(url("/twt/scan"), headers=bearer(public_id))
            after = await client.get(url("/twt/today"), headers=bearer(public_id))

        assert before.status_code == 200
        assert before.json()["last_scan"] is None, "no run, and the page says so in words"
        run = after.json()["last_scan"]
        assert run is not None, "the page cannot say when it last scanned"
        assert run["id"] == posted.json()["run_id"]
        assert run["status"] == "QUEUED"
        assert run["found"] is None, "it has not looked at anything yet"
        assert run["session_date"] is None, "only the worker decides which session"

    async def test_a_finished_run_says_when_it_finished_and_what_it_found(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        now = dt.datetime.now(tz=dt.UTC)
        screener_session.add(
            TwScanRun(
                user_id=user_id,
                requested_at=now - dt.timedelta(minutes=13),
                started_at=now - dt.timedelta(minutes=13),
                finished_at=now - dt.timedelta(minutes=12),
                status="DONE",
                source="web",
                session_date=dt.date(2026, 9, 11),
                # What `twt.detect_session` returns and the task copies onto the row.
                detail={"date": "2026-09-11", "signals": 3, "status": "OK", "detail": {}},
            )
        )
        await screener_session.flush()

        async with running_app(settings, screener_session, task_queue=RecordingQueue()) as client:
            today = await client.get(url("/twt/today"), headers=bearer(public_id))

        run = today.json()["last_scan"]
        assert run["status"] == "DONE"
        assert run["found"] == 3
        assert run["session_date"] == "2026-09-11"
        assert run["finished_at"] is not None, "the page has nothing to date the scan by"

    async def test_found_none_and_did_not_say_are_different_answers(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Eighteen entries a year means most sessions signal nothing. That is the ordinary
        result and must not be served as the same thing as a run that never reported."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        now = dt.datetime.now(tz=dt.UTC)
        screener_session.add(
            TwScanRun(
                user_id=user_id,
                requested_at=now - dt.timedelta(minutes=9),
                finished_at=now - dt.timedelta(minutes=8),
                status="DONE",
                source="web",
                detail=dict(DETAIL),
            )
        )
        await screener_session.flush()

        async with running_app(settings, screener_session, task_queue=RecordingQueue()) as client:
            quiet = await client.get(url("/twt/today"), headers=bearer(public_id))

        assert DETAIL["signals"] == 0, "the fixture is the quiet session"
        assert quiet.json()["last_scan"]["found"] == 0

        screener_session.add(
            TwScanRun(
                user_id=user_id,
                requested_at=now - dt.timedelta(minutes=2),
                finished_at=now - dt.timedelta(minutes=1),
                status="FAILED",
                source="web",
                error="ScanNotRunnable: nothing published to detect",
            )
        )
        await screener_session.flush()

        async with running_app(settings, screener_session, task_queue=RecordingQueue()) as client:
            failed = await client.get(url("/twt/today"), headers=bearer(public_id))

        run = failed.json()["last_scan"]
        assert run["status"] == "FAILED"
        assert run["found"] is None, "a run that stopped before detecting has no count to give"

    async def test_the_run_on_the_day_s_payload_is_this_user_s_own(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Somebody else's press must never date this reader's page."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        stranger, _ = await make_user(screener_session, "twt-scan-other@example.com")
        now = dt.datetime.now(tz=dt.UTC)
        screener_session.add(
            TwScanRun(
                user_id=stranger,
                requested_at=now,
                finished_at=now,
                status="DONE",
                source="web",
                detail={"signals": 99},
            )
        )
        await screener_session.flush()
        assert stranger != user_id

        async with running_app(settings, screener_session, task_queue=RecordingQueue()) as client:
            today = await client.get(url("/twt/today"), headers=bearer(public_id))

        assert today.json()["last_scan"] is None
