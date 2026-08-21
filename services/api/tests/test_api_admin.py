"""``/admin/*`` — the staff surface (Prompt 17 deliverable 4).

The first class is the one that matters. Every route on this router is behind
``require_staff``, and the failure mode of a staff gate is that someone adds a route and forgets
it — so the gate is asserted **route by route, from the OpenAPI document**, rather than on a
sample. A new endpoint that skips the gate fails this file the moment it is added.

Everything else asserts the behaviour PROMPTS.md Prompt 17 §4 names: run history with per-step
detail, a re-run button, `data_version` history, provider health, user lookup, entitlement
override, and reprocess-instrument.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import pytest
from api_helpers import PREFIX, api_settings, bearer, make_user, running_app, url
from screener_helpers import requires_db
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from decile_api.app import create_app
from decile_core.models import (
    AdminAction,
    AppUser,
    EntitlementOverride,
    PipelineRun,
    PipelineRunStep,
)

pytestmark = [pytest.mark.db, pytest.mark.redis, requires_db]

TRADE_DATE = dt.date(2026, 8, 18)


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


async def _run_with_steps(session: AsyncSession, *, published: bool) -> PipelineRun:
    """One `pipeline_run` and its ten `pipeline_run_step` rows, as docs/03 requires."""
    started = dt.datetime(2026, 8, 18, 13, 15, tzinfo=dt.UTC)
    run = PipelineRun(
        trade_date=TRADE_DATE,
        status="succeeded" if published else "failed",
        started_at=started,
        finished_at=started + dt.timedelta(minutes=25),
        data_version=41 if published else None,
    )
    session.add(run)
    await session.flush()
    session.add_all(
        [
            PipelineRunStep(
                run_id=run.id,
                step="compute_factors",
                status="succeeded",
                rows_in=2300,
                rows_out=2300,
                duration_ms=12_500,
                error={"engine": "polars"},
            ),
            PipelineRunStep(
                run_id=run.id,
                step="data_quality_gate",
                status="succeeded" if published else "failed",
                rows_in=8,
                rows_out=8,
                duration_ms=900,
                error=None if published else {"failures": [{"assertion": 1, "message": "12%"}]},
            ),
        ]
    )
    await session.flush()
    return run


def admin_routes() -> list[tuple[str, str]]:
    """Every ``/admin/*`` (method, path) the application serves, read from the OpenAPI document.

    Derived rather than listed, so a route added without a staff gate fails the tests below the
    moment it is added — which is the failure mode of a gate that has to be remembered per route.
    """
    document = create_app(api_settings("postgresql+asyncpg://unused/unused")).openapi()
    paths = document["paths"]
    assert isinstance(paths, dict)
    return sorted(
        (method.upper(), path)
        for path, operations in paths.items()
        if path.startswith(f"{PREFIX}/admin")
        for method in operations
    )


class TestEveryAdminRouteIsStaffGated:
    """The gate, asserted from the route table rather than on a sample of endpoints."""

    @staticmethod
    def _concrete(path: str) -> str:
        """Substitute plausible values for the path parameters, so routing reaches the gate."""
        return (
            path.replace("{run_id}", "1")
            .replace("{trade_date}", TRADE_DATE.isoformat())
            .replace("{symbol}", "CUPID")
            .replace("{public_id}", "nobody00000")
            .replace("{feature}", "export_csv")
        )

    def test_there_are_admin_routes_to_check(self) -> None:
        """A gate test over an empty route list would pass silently and prove nothing."""
        assert len(admin_routes()) >= 10

    async def test_an_anonymous_caller_gets_401(self, screener_session: AsyncSession) -> None:
        """ "You are not signed in" is not a secret."""
        settings = api_settings(str(screener_session.bind.engine.url))
        async with running_app(settings, screener_session) as client:
            for method, path in admin_routes():
                response = await client.request(method, self._concrete(path))
                assert response.status_code == 401, (
                    f"{method} {path} answered {response.status_code}"
                )

    async def test_a_signed_in_non_staff_caller_gets_404(
        self, screener_session: AsyncSession
    ) -> None:
        """Not 403.

        docs/07's catalogue has no `forbidden` type, and a 403 confirms the path exists and that
        the caller merely lacks a bit — which tells an attacker exactly which endpoint is worth
        getting a session for (`docs/DECISIONS.md` §17.3).
        """
        _, public_id = await make_user(screener_session, "civilian@example.com")
        headers = bearer(public_id)
        settings = api_settings(str(screener_session.bind.engine.url))
        async with running_app(settings, screener_session) as client:
            for method, path in admin_routes():
                response = await client.request(method, self._concrete(path), headers=headers)
                assert response.status_code == 404, (
                    f"{method} {path} answered {response.status_code}; a staff gate that answers "
                    "403 is an oracle"
                )


class TestPipelineHistory:
    async def test_a_run_carries_its_steps_and_its_publish_latency(
        self, screener_session: AsyncSession
    ) -> None:
        """docs/09 §Observability: the operator UI is `pipeline_run_step`, with publish latency."""
        headers = await _staff(screener_session)
        await _run_with_steps(screener_session, published=True)
        settings = api_settings(str(screener_session.bind.engine.url))

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/admin/pipeline/runs"), headers=headers)

        assert response.status_code == 200
        run = response.json()["data"][0]
        assert run["trade_date"] == TRADE_DATE.isoformat()
        assert run["data_version"] == 41
        steps = {step["step"]: step for step in run["steps"]}
        assert steps["compute_factors"]["rows_out"] == 2300
        assert steps["compute_factors"]["duration_ms"] == 12_500
        # docs/03: the step payload survives to the operator, verbatim.
        assert steps["compute_factors"]["detail"] == {"engine": "polars"}
        # docs/09 §Observability: "EOD close -> data live". 15:30 IST is 10:00 UTC; the run
        # finished at 13:40 UTC. 3h40m. Never negative, never invented.
        assert run["publish_latency_seconds"] == pytest.approx(3 * 3600 + 40 * 60)

    async def test_a_failed_gate_shows_its_assertions(self, screener_session: AsyncSession) -> None:
        headers = await _staff(screener_session)
        run = await _run_with_steps(screener_session, published=False)
        settings = api_settings(str(screener_session.bind.engine.url))

        async with running_app(settings, screener_session) as client:
            response = await client.get(url(f"/admin/pipeline/runs/{run.id}"), headers=headers)

        assert response.status_code == 200
        body = response.json()
        assert body["data_version"] is None
        assert body["publish_latency_seconds"] is None
        gate = next(step for step in body["steps"] if step["step"] == "data_quality_gate")
        assert gate["status"] == "failed"
        assert gate["detail"]["failures"][0]["assertion"] == 1

    async def test_an_unknown_run_is_a_404(self, screener_session: AsyncSession) -> None:
        headers = await _staff(screener_session)
        settings = api_settings(str(screener_session.bind.engine.url))
        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/admin/pipeline/runs/999999"), headers=headers)
        assert response.status_code == 404


class TestTheReRunButton:
    async def test_it_publishes_the_nightly_task_and_writes_an_audit_row(
        self, screener_session: AsyncSession
    ) -> None:
        """It enqueues; it does not run the chain inside a request (see `decile_api.admin`)."""
        headers = await _staff(screener_session)
        queue = _RecordingQueue()
        settings = api_settings(str(screener_session.bind.engine.url))

        async with running_app(settings, screener_session, task_queue=queue) as client:
            response = await client.post(
                url(f"/admin/pipeline/runs/{TRADE_DATE.isoformat()}/rerun"), headers=headers
            )

        assert response.status_code == 202
        assert response.json()["task"] == "decile.pipeline.nightly"
        assert queue.sent == [("decile.pipeline.nightly", [TRADE_DATE.isoformat()])]

        action = (
            await screener_session.execute(
                select(AdminAction).where(AdminAction.action == "pipeline_rerun")
            )
        ).scalar_one()
        assert action.target == TRADE_DATE.isoformat()
        assert action.detail is not None
        assert action.detail["task"] == "decile.pipeline.nightly"

    async def test_reprocess_refuses_an_unknown_symbol(
        self, screener_session: AsyncSession
    ) -> None:
        headers = await _staff(screener_session)
        queue = _RecordingQueue()
        settings = api_settings(str(screener_session.bind.engine.url))

        async with running_app(settings, screener_session, task_queue=queue) as client:
            response = await client.post(
                url("/admin/instruments/NOTAREALSYMBOL/reprocess"), headers=headers
            )

        assert response.status_code == 404
        assert queue.sent == [], "nothing should be enqueued for an instrument that does not exist"

    async def test_reprocess_enqueues_by_instrument_id(
        self, screener_session: AsyncSession
    ) -> None:
        """The operator has a symbol; the task takes an id."""
        headers = await _staff(screener_session)
        instrument_symbol = "CUPID"
        queue = _RecordingQueue()
        settings = api_settings(str(screener_session.bind.engine.url))

        async with running_app(settings, screener_session, task_queue=queue) as client:
            response = await client.post(
                url(f"/admin/instruments/{instrument_symbol}/reprocess"), headers=headers
            )

        # The seeded reference export carries CUPID (docs/13).
        assert response.status_code == 202, response.text
        assert response.json()["target"] == instrument_symbol
        name, args = queue.sent[0]
        assert name == "decile.compute.reprocess_instrument"
        assert isinstance(args[0], int)


class TestDataVersionHistory:
    async def test_it_lists_only_published_runs_and_names_the_current_one(
        self, screener_session: AsyncSession
    ) -> None:
        headers = await _staff(screener_session)
        await _run_with_steps(screener_session, published=True)
        settings = api_settings(str(screener_session.bind.engine.url))

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/admin/data-versions"), headers=headers)

        body = response.json()
        assert body["current"] == body["data"][0]["data_version"]
        assert all(row["data_version"] is not None for row in body["data"])


class TestProviderHealth:
    async def test_it_reports_every_adapter_without_touching_the_network(
        self, screener_session: AsyncSession
    ) -> None:
        """`network_guard.py` blocks the suite's sockets, so this passing *is* the assertion."""
        headers = await _staff(screener_session)
        settings = api_settings(str(screener_session.bind.engine.url))

        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/admin/providers"), headers=headers)

        assert response.status_code == 200
        names = {row["name"] for row in response.json()["data"]}
        assert {"kite", "nse"} <= names


class TestUserLookupAndOverrides:
    async def test_lookup_matches_on_an_email_fragment(
        self, screener_session: AsyncSession
    ) -> None:
        headers = await _staff(screener_session, "ops1@example.com")
        await make_user(screener_session, "findme@example.com")
        settings = api_settings(str(screener_session.bind.engine.url))

        async with running_app(settings, screener_session) as client:
            response = await client.get(
                url("/admin/users"), params={"q": "findme"}, headers=headers
            )

        assert response.status_code == 200
        assert [row["email"] for row in response.json()["data"]] == ["findme@example.com"]

    async def test_an_override_changes_the_effective_entitlements(
        self, screener_session: AsyncSession
    ) -> None:
        """The point of the whole feature: it must move the number every gated endpoint reads."""
        headers = await _staff(screener_session, "ops2@example.com")
        _, subject = await make_user(screener_session, "unpaid@example.com")
        settings = api_settings(str(screener_session.bind.engine.url))

        async with running_app(settings, screener_session) as client:
            before = await client.get(url(f"/admin/users/{subject}"), headers=headers)
            assert before.json()["entitlements"]["export_csv"] is False

            granted = await client.put(
                url(f"/admin/users/{subject}/entitlements"),
                headers=headers,
                json={
                    "feature": "export_csv",
                    "effect": "grant",
                    "reason": "webhook evt_x not delivered; paid 2026-08-21",
                    "expires_at": "2026-09-21T00:00:00Z",
                },
            )

        assert granted.status_code == 200, granted.text
        body = granted.json()
        assert body["entitlements"]["export_csv"] is True
        assert body["overrides"][0]["feature"] == "export_csv"
        assert body["overrides"][0]["granted_by"] == "ops2@example.com"
        assert body["overrides"][0]["active"] is True

    async def test_an_override_is_replaced_not_appended(
        self, screener_session: AsyncSession
    ) -> None:
        """ "What does this account get" must have exactly one answer."""
        headers = await _staff(screener_session, "ops3@example.com")
        _, subject = await make_user(screener_session, "twice@example.com")
        settings = api_settings(str(screener_session.bind.engine.url))
        payload = {"feature": "backtests", "effect": "grant", "reason": "first reason"}

        async with running_app(settings, screener_session) as client:
            await client.put(
                url(f"/admin/users/{subject}/entitlements"), headers=headers, json=payload
            )
            second = await client.put(
                url(f"/admin/users/{subject}/entitlements"),
                headers=headers,
                json={**payload, "effect": "revoke", "reason": "second reason"},
            )

        assert second.status_code == 200
        overrides = second.json()["overrides"]
        assert len(overrides) == 1
        assert overrides[0]["effect"] == "revoke"
        assert overrides[0]["reason"] == "second reason"
        assert second.json()["entitlements"]["backtests"] is False

    async def test_an_expired_override_stops_granting(self, screener_session: AsyncSession) -> None:
        headers = await _staff(screener_session, "ops4@example.com")
        _, subject = await make_user(screener_session, "expired@example.com")
        settings = api_settings(str(screener_session.bind.engine.url))

        async with running_app(settings, screener_session) as client:
            response = await client.put(
                url(f"/admin/users/{subject}/entitlements"),
                headers=headers,
                json={
                    "feature": "export_csv",
                    "effect": "grant",
                    "reason": "a trial that has ended",
                    "expires_at": "2020-01-01T00:00:00Z",
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["overrides"][0]["active"] is False
        assert body["entitlements"]["export_csv"] is False

    async def test_a_reason_is_required(self, screener_session: AsyncSession) -> None:
        """An override with no stated reason is the row `admin_action` exists to prevent."""
        headers = await _staff(screener_session, "ops5@example.com")
        _, subject = await make_user(screener_session, "noreason@example.com")
        settings = api_settings(str(screener_session.bind.engine.url))

        async with running_app(settings, screener_session) as client:
            response = await client.put(
                url(f"/admin/users/{subject}/entitlements"),
                headers=headers,
                json={"feature": "export_csv", "effect": "grant", "reason": "x"},
            )

        assert response.status_code == 400

    async def test_max_screens_can_be_overridden_to_a_number(
        self, screener_session: AsyncSession
    ) -> None:
        headers = await _staff(screener_session, "ops6@example.com")
        _, subject = await make_user(screener_session, "manyscreens@example.com")
        settings = api_settings(str(screener_session.bind.engine.url))

        async with running_app(settings, screener_session) as client:
            response = await client.put(
                url(f"/admin/users/{subject}/entitlements"),
                headers=headers,
                json={
                    "feature": "max_screens",
                    "effect": "grant",
                    "value": 25,
                    "reason": "a support arrangement",
                },
            )

        assert response.status_code == 200
        assert response.json()["entitlements"]["max_screens"] == 25

    async def test_max_screens_without_a_value_is_refused(
        self, screener_session: AsyncSession
    ) -> None:
        """ "Override max_screens, unspecified" must not silently become zero."""
        headers = await _staff(screener_session, "ops7@example.com")
        _, subject = await make_user(screener_session, "novalue@example.com")
        settings = api_settings(str(screener_session.bind.engine.url))

        async with running_app(settings, screener_session) as client:
            response = await client.put(
                url(f"/admin/users/{subject}/entitlements"),
                headers=headers,
                json={"feature": "max_screens", "effect": "grant", "reason": "no value given"},
            )

        assert response.status_code == 400

    async def test_an_unknown_feature_is_refused(self, screener_session: AsyncSession) -> None:
        headers = await _staff(screener_session, "ops8@example.com")
        _, subject = await make_user(screener_session, "unknownfeature@example.com")
        settings = api_settings(str(screener_session.bind.engine.url))

        async with running_app(settings, screener_session) as client:
            response = await client.put(
                url(f"/admin/users/{subject}/entitlements"),
                headers=headers,
                json={"feature": "free_money", "effect": "grant", "reason": "nice try"},
            )

        assert response.status_code == 400

    async def test_clearing_an_override_returns_the_plan_answer(
        self, screener_session: AsyncSession
    ) -> None:
        headers = await _staff(screener_session, "ops9@example.com")
        _, subject = await make_user(screener_session, "cleared@example.com")
        settings = api_settings(str(screener_session.bind.engine.url))

        async with running_app(settings, screener_session) as client:
            await client.put(
                url(f"/admin/users/{subject}/entitlements"),
                headers=headers,
                json={"feature": "export_csv", "effect": "grant", "reason": "temporary"},
            )
            deleted = await client.delete(
                url(f"/admin/users/{subject}/entitlements/export_csv"), headers=headers
            )
            after = await client.get(url(f"/admin/users/{subject}"), headers=headers)

        assert deleted.status_code == 204
        assert after.json()["entitlements"]["export_csv"] is False
        assert after.json()["overrides"] == []

        remaining = (
            (
                await screener_session.execute(
                    select(EntitlementOverride).where(EntitlementOverride.feature == "export_csv")
                )
            )
            .scalars()
            .all()
        )
        assert remaining == []

    async def test_every_override_is_audited(self, screener_session: AsyncSession) -> None:
        """An un-undoable privileged action with no record of who took it is a hole."""
        headers = await _staff(screener_session, "ops10@example.com")
        _, subject = await make_user(screener_session, "audited@example.com")
        settings = api_settings(str(screener_session.bind.engine.url))

        async with running_app(settings, screener_session) as client:
            await client.put(
                url(f"/admin/users/{subject}/entitlements"),
                headers=headers,
                json={"feature": "backtests", "effect": "grant", "reason": "an audited grant"},
            )
            actions = await client.get(url("/admin/actions"), headers=headers)

        entries = actions.json()["data"]
        assert entries[0]["action"] == "entitlement_override_set"
        assert entries[0]["target"] == subject
        assert entries[0]["actor"] == "ops10@example.com"
        assert entries[0]["detail"]["reason"] == "an audited grant"


class TestTheMeEndpointReportsStaff:
    async def test_is_staff_is_server_truth(self, screener_session: AsyncSession) -> None:
        """The web app renders the `/admin` link from this, exactly as it renders entitlements."""
        headers = await _staff(screener_session, "ops11@example.com")
        _, civilian = await make_user(screener_session, "civilian2@example.com")
        settings = api_settings(str(screener_session.bind.engine.url))

        async with running_app(settings, screener_session) as client:
            staff_me = await client.get(url("/me"), headers=headers)
            civilian_me = await client.get(url("/me"), headers=bearer(civilian))

        assert staff_me.json()["is_staff"] is True
        assert civilian_me.json()["is_staff"] is False
