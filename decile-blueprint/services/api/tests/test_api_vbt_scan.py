"""The VBT sleeve's "Scan now", over HTTP against a real database.

`PLAN-SCAN-SYNC.md` fixes one shape across the sleeves — **202** queued, **409** one already in
flight, **429** one a minute, and `GET /vbt/scan/{run_id}` for the poll — so a page written
against the swing book's button works here without learning a second vocabulary. These are the
claims that shape is worth nothing without:

* **the run is a row before it is a message.** A broker that is down is still a 202 with a
  `QUEUED` row and no `task_id`, because `vbt-rescan-sweep` publishes exactly those. The desk's
  own button has always depended on it, having no Celery client at all;
* **it queues the detector that already exists.** The published name is `baskfy.vbt.rescan`,
  asserted literally — a second detector behind the same rows would have none of VB12's tests;
* **both refusals are answered from the table**, not from a cache, so they hold on a box with no
  Redis and are testable against the database alone;
* **a dead worker does not wedge the button**: past `vbt_scan_stale_after_seconds`, a `RUNNING`
  row is history;
* **it is one person's**, on both verbs;
* **and it moves no money.** The row it writes is the only row it writes: nothing on the plan,
  the order, the position or the fill tables moves, asserted by counting them after a press.
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

from baskfy_api.settings import Settings
from baskfy_core.models import (
    VbConfig,
    VbOrder,
    VbPlan,
    VbPlanLine,
    VbPosition,
    VbScanRun,
)

pytestmark = [requires_db, pytest.mark.db]

#: The detector's own counts, as the nightly step writes them.
FUNNEL = {"universe": 4358, "scan_hits": 19}


@pytest.fixture
def settings(seeded_url: str) -> Settings:
    return api_helpers.api_settings(seeded_url)


async def _sole_tenant(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> tuple[int, str]:
    user_id, public_id = await make_user(session, "vbt-scan-sole@example.com")
    monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(user_id))
    session.add(
        VbConfig(user_id=user_id, sleeve_capital_inr=Decimal("1000000.00"), updated_by="test")
    )
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
            response = await client.post(url("/vbt/scan"), headers=bearer(public_id))
            assert response.status_code == 202, response.text
            run_id = response.json()["run_id"]
            read = await client.get(url(f"/vbt/scan/{run_id}"), headers=bearer(public_id))

        assert response.json()["status"] == "QUEUED"
        assert queue.sent == [("baskfy.vbt.rescan", [run_id])], "a second detector was queued"
        row = (
            await screener_session.execute(sa.select(VbScanRun).where(VbScanRun.id == run_id))
        ).scalar_one()
        assert row.user_id == user_id
        assert row.task_id == "task-1"
        assert row.detail == {"source": "web"}
        assert read.status_code == 200
        body = read.json()
        assert body["run_id"] == run_id
        assert body["status"] == "QUEUED"
        assert body["source"] == "web"
        assert body["session_date"] is None, "only the worker decides which session"
        assert body["funnel"] is None
        assert body["error"] is None

    async def test_the_status_vocabulary_is_the_contract_s_four(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`QUEUED | RUNNING | DONE | FAILED`, read back verbatim — a page binds to these."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        rows: dict[str, int] = {}
        for index, status in enumerate(("QUEUED", "RUNNING", "DONE", "FAILED")):
            row = VbScanRun(
                user_id=user_id,
                requested_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=index + 1),
                status=status,
                source="desk",
                detail=({"funnel": FUNNEL} if status == "DONE" else None),
                error="ValueError: no session" if status == "FAILED" else None,
            )
            screener_session.add(row)
            await screener_session.flush()
            rows[status] = int(row.id)

        seen: dict[str, dict[str, object]] = {}
        async with running_app(settings, screener_session) as client:
            for status, rid in rows.items():
                read = await client.get(url(f"/vbt/scan/{rid}"), headers=bearer(public_id))
                seen[status] = read.json()

        assert {status: body["status"] for status, body in seen.items()} == {
            s: s for s in ("QUEUED", "RUNNING", "DONE", "FAILED")
        }
        assert seen["DONE"]["funnel"] == FUNNEL
        assert seen["FAILED"]["error"] == "ValueError: no session"
        assert seen["DONE"]["source"] == "desk", "no detail.source falls back to the column"

    async def test_a_second_request_while_one_is_in_flight_is_a_409_naming_it(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, public_id = await _sole_tenant(screener_session, monkeypatch)
        queue = RecordingQueue()

        async with running_app(settings, screener_session, task_queue=queue) as client:
            first = await client.post(url("/vbt/scan"), headers=bearer(public_id))
            second = await client.post(url("/vbt/scan"), headers=bearer(public_id))

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
            VbScanRun(
                user_id=user_id,
                requested_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=20),
                status="DONE",
                source="desk",
            )
        )
        await screener_session.flush()

        async with running_app(settings, screener_session, task_queue=RecordingQueue()) as client:
            response = await client.post(url("/vbt/scan"), headers=bearer(public_id))

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
            VbScanRun(
                user_id=user_id,
                requested_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(minutes=20),
                status="RUNNING",
                source="desk",
            )
        )
        await screener_session.flush()

        async with running_app(settings, screener_session, task_queue=RecordingQueue()) as client:
            response = await client.post(url("/vbt/scan"), headers=bearer(public_id))

        assert response.status_code == 202

    async def test_the_two_thresholds_are_settings_rather_than_literals(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """House rule: a threshold is a setting. Ninety seconds is outside the default minute and
        inside a two-minute one, and the route's answer must move with the number."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        screener_session.add(
            VbScanRun(
                user_id=user_id,
                requested_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=90),
                status="DONE",
                source="desk",
            )
        )
        await screener_session.flush()

        # The refusal first: a 202 would write a QUEUED row and the next press would be the
        # *other* refusal (409, one in flight), which proves nothing about the interval.
        wider = settings.model_copy(update={"vbt_scan_min_interval_seconds": 120})
        async with running_app(wider, screener_session, task_queue=RecordingQueue()) as client:
            refused = await client.post(url("/vbt/scan"), headers=bearer(public_id))

        async with running_app(settings, screener_session, task_queue=RecordingQueue()) as client:
            allowed = await client.post(url("/vbt/scan"), headers=bearer(public_id))

        assert refused.status_code == 429
        assert allowed.status_code == 202

    async def test_a_broker_that_is_down_leaves_the_row_queued_for_the_sweep(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The row is the request; publishing is the fast path. A broker that refuses is a 202
        all the same, with no `task_id`, and `vbt-rescan-sweep` publishes it within the minute."""
        _, public_id = await _sole_tenant(screener_session, monkeypatch)

        class BrokerDown:
            def send_task(self, name: str, args: Sequence[object]) -> object:
                raise ConnectionError("redis: connection refused")

        async with running_app(settings, screener_session, task_queue=BrokerDown()) as client:
            response = await client.post(url("/vbt/scan"), headers=bearer(public_id))

        assert response.status_code == 202
        row = (
            await screener_session.execute(
                sa.select(VbScanRun).where(VbScanRun.id == response.json()["run_id"])
            )
        ).scalar_one()
        assert row.status == "QUEUED" and row.task_id is None


class TestItMovesNoMoney:
    """The non-negotiable, over HTTP: a scan queues a detector and writes one row.

    `PLAN-SCAN-SYNC.md` rule 3 — "nothing new may reach `OrderGateway.place`" — is asserted
    structurally in `test_vbt_readonly.py`. This is the behavioural half: after a press, every
    table that could represent money is still empty.
    """

    async def test_a_press_writes_the_scan_row_and_nothing_else(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)

        async with running_app(settings, screener_session, task_queue=RecordingQueue()) as client:
            response = await client.post(url("/vbt/scan"), headers=bearer(public_id))

        assert response.status_code == 202
        scans = (
            await screener_session.execute(
                sa.select(sa.func.count())
                .select_from(VbScanRun)
                .where(VbScanRun.user_id == user_id)
            )
        ).scalar_one()
        assert scans == 1
        for model in (VbPlan, VbPlanLine, VbOrder, VbPosition):
            count = (
                await screener_session.execute(sa.select(sa.func.count()).select_from(model))
            ).scalar_one()
            assert count == 0, f"a scan wrote to {model.__tablename__}"


class TestTheScanBelongsToOnePerson:
    async def test_a_stranger_is_refused_on_both_verbs_and_cannot_read_the_run(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, public_id = await _sole_tenant(screener_session, monkeypatch)
        _, other = await make_user(screener_session, "vbt-scan-stranger@example.com")

        async with running_app(settings, screener_session, task_queue=RecordingQueue()) as client:
            mine = await client.post(url("/vbt/scan"), headers=bearer(public_id))
            run_id = mine.json()["run_id"]
            refused_post = await client.post(url("/vbt/scan"), headers=bearer(other))
            refused_get = await client.get(url(f"/vbt/scan/{run_id}"), headers=bearer(other))
            missing = await client.get(url("/vbt/scan/999999"), headers=bearer(public_id))

        assert mine.status_code == 202
        assert refused_post.status_code in (403, 404)
        assert refused_get.status_code in (403, 404)
        assert missing.status_code == 404

    async def test_an_anonymous_caller_gets_nothing(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _sole_tenant(screener_session, monkeypatch)

        async with running_app(settings, screener_session, task_queue=RecordingQueue()) as client:
            posted = await client.post(url("/vbt/scan"))
            read = await client.get(url("/vbt/scan/1"))

        assert posted.status_code == 401
        assert read.status_code == 401


class TestThePageCanSayWhenItLastScanned:
    """THE GAP OF 12 SEP 2026, on this sleeve.

    "None of the scan shows when the last scan performed in any strategy." The plumbing was all
    here — the page has had a copy function and a control for the last run since VB12 — and both
    were being handed `None`, because **`GET /vbt/today` did not carry the run**. The contract
    fixes the poll route rather than whether the day's payload inlines the run, and the page was
    written to accept either; what that left in practice was a cold load with no run id to ask
    about, so "when did this last scan?" had no answer at all until somebody pressed the button in
    that same tab.

    Two claims, and the second is the one that turns `DONE` into a sentence: the day's payload
    names the newest run, and the run says **how many signals it produced** — where `0` is a real
    answer (a thin tape) and `None` is "it did not say", and a page must be able to tell them
    apart.
    """

    async def test_the_day_s_payload_names_the_newest_run(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, public_id = await _sole_tenant(screener_session, monkeypatch)

        async with running_app(settings, screener_session, task_queue=RecordingQueue()) as client:
            before = await client.get(url("/vbt/today"), headers=bearer(public_id))
            posted = await client.post(url("/vbt/scan"), headers=bearer(public_id))
            after = await client.get(url("/vbt/today"), headers=bearer(public_id))

        assert before.status_code == 200
        assert before.json()["last_scan"] is None, "no run, and the page says so in words"
        run = after.json()["last_scan"]
        assert run is not None, "the page cannot say when it last scanned"
        assert run["run_id"] == posted.json()["run_id"]
        assert run["status"] == "QUEUED"
        assert run["found"] is None, "it has not looked at anything yet"

    async def test_a_finished_run_says_how_many_signals_it_found(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        now = dt.datetime.now(tz=dt.UTC)
        # The shape `baskfy_worker.tasks.vbt_rescan` writes: the funnel's counts flattened onto
        # the row, so the count is a top-level key and not one nested under `funnel`.
        screener_session.add(
            VbScanRun(
                user_id=user_id,
                requested_at=now - dt.timedelta(minutes=13),
                started_at=now - dt.timedelta(minutes=13),
                finished_at=now - dt.timedelta(minutes=12),
                status="DONE",
                source="desk",
                session_date=dt.date(2026, 9, 11),
                detail={"signals": 4, "status": "ok", **FUNNEL},
            )
        )
        await screener_session.flush()

        async with running_app(settings, screener_session, task_queue=RecordingQueue()) as client:
            today = await client.get(url("/vbt/today"), headers=bearer(public_id))

        run = today.json()["last_scan"]
        assert run["status"] == "DONE"
        assert run["found"] == 4
        assert run["session_date"] == "2026-09-11"
        assert run["finished_at"] is not None, "the page has nothing to date the scan by"

    async def test_found_none_and_did_not_say_are_different_answers(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A quiet tape produces no signals and that is an ordinary day, not a fault — and not
        the same thing as a run that never reported. `0` and `None` must survive the payload."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        now = dt.datetime.now(tz=dt.UTC)
        quiet = VbScanRun(
            user_id=user_id,
            requested_at=now - dt.timedelta(minutes=9),
            finished_at=now - dt.timedelta(minutes=8),
            status="DONE",
            source="desk",
            detail={"signals": 0, "status": "ok", **FUNNEL},
        )
        screener_session.add(quiet)
        await screener_session.flush()

        async with running_app(settings, screener_session, task_queue=RecordingQueue()) as client:
            quiet_body = await client.get(url("/vbt/today"), headers=bearer(public_id))

        assert quiet_body.json()["last_scan"]["found"] == 0

        silent = VbScanRun(
            user_id=user_id,
            requested_at=now - dt.timedelta(minutes=2),
            finished_at=now - dt.timedelta(minutes=1),
            status="DONE",
            source="desk",
            detail={"status": "ok"},
        )
        screener_session.add(silent)
        await screener_session.flush()

        async with running_app(settings, screener_session, task_queue=RecordingQueue()) as client:
            silent_body = await client.get(url("/vbt/today"), headers=bearer(public_id))

        assert silent_body.json()["last_scan"]["found"] is None

    async def test_the_run_on_the_day_s_payload_is_this_user_s_own(
        self, settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Somebody else's press must never date this reader's page."""
        user_id, public_id = await _sole_tenant(screener_session, monkeypatch)
        stranger, _ = await make_user(screener_session, "vbt-scan-other@example.com")
        now = dt.datetime.now(tz=dt.UTC)
        screener_session.add(
            VbScanRun(
                user_id=stranger,
                requested_at=now,
                finished_at=now,
                status="DONE",
                source="desk",
                detail={"signals": 99},
            )
        )
        await screener_session.flush()
        assert stranger != user_id

        async with running_app(settings, screener_session, task_queue=RecordingQueue()) as client:
            today = await client.get(url("/vbt/today"), headers=bearer(public_id))

        assert today.json()["last_scan"] is None
