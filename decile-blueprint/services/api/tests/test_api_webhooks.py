"""Outbound webhooks — signing, delivery, retry and backoff (Prompt 20 deliverable 4).

Nothing here touches a socket. ``network_guard.py`` blocks the suite outright, so every delivery
runs against an ``httpx.MockTransport`` — which is also the only way to assert what happens when a
receiver answers 500, or 401, or nothing at all.
"""

from __future__ import annotations

import datetime as dt
import json

import api_helpers
import httpx
import pytest
import screener_helpers
from alert_helpers import CURRENT, PREVIOUS, Recorder, instrument_ids, make_run, make_screen
from alert_helpers import settings_for as base_settings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import alerts as alert_service
from baskfy_api import webhooks
from baskfy_api.email import Mailer
from baskfy_api.settings import Settings
from baskfy_core.models import Screen, WebhookDelivery, WebhookEndpoint
from baskfy_worker.tasks.alerts import run_webhook_sweep

pytestmark = [screener_helpers.requires_db, pytest.mark.db, pytest.mark.redis]

ENDPOINTS = api_helpers.url("/webhook-endpoints")
NOW = dt.datetime(2026, 8, 19, 15, 0, tzinfo=dt.UTC)


def settings_for(url_: str = "postgresql+asyncpg://unused/unused") -> Settings:
    return base_settings(url_)


class Receiver:
    """A stand-in for the customer's server. Records what arrived and answers what it is told to."""

    def __init__(self, status: int = 200, *, raises: bool = False) -> None:
        self.status = status
        self.raises = raises
        self.received: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.received.append(request)
            if self.raises:
                raise httpx.ConnectError("connection refused", request=request)
            return httpx.Response(self.status, json={"ok": True})

        return httpx.MockTransport(handler)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport())


class TestSigning:
    def test_a_signature_verifies_against_the_derived_secret(self) -> None:
        settings = settings_for()
        secret = webhooks.signing_secret(settings, "endpoint0001", 1)
        body = b'{"event":"screen.entries"}'
        header = webhooks.signature_for(secret, body, timestamp=1_755_000_000)
        assert webhooks.verify_signature(secret, body, header, now=1_755_000_000)

    def test_a_tampered_body_does_not_verify(self) -> None:
        settings = settings_for()
        secret = webhooks.signing_secret(settings, "endpoint0001", 1)
        header = webhooks.signature_for(secret, b"original", timestamp=1_755_000_000)
        assert not webhooks.verify_signature(secret, b"tampered", header, now=1_755_000_000)

    def test_an_old_signature_is_refused(self) -> None:
        """The timestamp inside the signed payload is what stops a captured delivery replaying."""
        settings = settings_for()
        secret = webhooks.signing_secret(settings, "endpoint0001", 1)
        header = webhooks.signature_for(secret, b"body", timestamp=1_755_000_000)
        stale = 1_755_000_000 + webhooks.SIGNATURE_TOLERANCE_SECONDS + 1
        assert not webhooks.verify_signature(secret, b"body", header, now=stale)

    def test_rotation_changes_the_secret(self) -> None:
        settings = settings_for()
        first = webhooks.signing_secret(settings, "endpoint0001", 1)
        second = webhooks.signing_secret(settings, "endpoint0001", 2)
        assert first != second

    def test_two_endpoints_get_different_secrets(self) -> None:
        settings = settings_for()
        assert webhooks.signing_secret(settings, "aaaa", 1) != webhooks.signing_secret(
            settings, "bbbb", 1
        )

    def test_a_deployment_with_no_master_secret_cannot_sign(self) -> None:
        settings = Settings(environment="local", jwt_secret="", webhook_signing_secret="")
        with pytest.raises(webhooks.WebhookNotConfigured):
            webhooks.signing_secret(settings, "endpoint0001", 1)


class TestBackoff:
    def test_it_grows_exponentially(self) -> None:
        settings = settings_for()
        waits = [webhooks.backoff_seconds(n, settings) for n in (1, 2, 3, 4)]
        assert waits == [30, 120, 480, 1920]

    def test_the_first_attempt_waits_for_nothing(self) -> None:
        assert webhooks.backoff_seconds(0, settings_for()) == 0


async def _endpoint(
    session: AsyncSession, user_id: int, screen: Screen, url: str = "https://example.com/hook"
) -> WebhookEndpoint:
    endpoint = WebhookEndpoint(
        public_id=webhooks.new_public_id(),
        user_id=user_id,
        screen_id=screen.id,
        url=url,
        secret_version=1,
        events=["screen.entries", "screen.exits"],
        is_active=True,
        consecutive_failures=0,
    )
    session.add(endpoint)
    await session.flush()
    return endpoint


async def _queued(
    session: AsyncSession, endpoint: WebhookEndpoint, key: str = "k1"
) -> WebhookDelivery:
    await webhooks.enqueue(
        session,
        endpoint,
        event="screen.entries",
        payload={"event": "screen.entries", "instruments": [{"symbol": "CUPID", "rank": 1}]},
        idempotency_key=key,
        now=NOW,
    )
    await session.flush()
    return (
        await session.execute(
            select(WebhookDelivery).where(
                WebhookDelivery.endpoint_id == endpoint.id,
                WebhookDelivery.idempotency_key == key,
            )
        )
    ).scalar_one()


class TestDelivery:
    async def test_a_2xx_marks_it_delivered_and_signs_the_body(
        self, screener_session: AsyncSession
    ) -> None:
        settings = settings_for()
        user_id, _ = await api_helpers.make_user(screener_session, "hook@example.com")
        screen = await make_screen(screener_session, user_id, "hookscreen01")
        endpoint = await _endpoint(screener_session, user_id, screen)
        delivery = await _queued(screener_session, endpoint)

        receiver = Receiver(200)
        async with receiver.client() as client:
            outcome = await webhooks.attempt_delivery(
                screener_session,
                delivery,
                endpoint,
                settings=settings,
                client=client,
                now=NOW,
            )
        assert outcome.delivered
        assert delivery.status == "delivered"
        assert delivery.attempts == 1

        request = receiver.received[0]
        secret = webhooks.signing_secret(settings, endpoint.public_id, 1)
        header = request.headers[webhooks.SIGNATURE_HEADER]
        assert webhooks.verify_signature(secret, request.content, header, now=int(NOW.timestamp()))
        assert json.loads(request.content)["event"] == "screen.entries"

    async def test_a_5xx_is_retried_with_backoff(self, screener_session: AsyncSession) -> None:
        settings = settings_for()
        user_id, _ = await api_helpers.make_user(screener_session, "hook5xx@example.com")
        screen = await make_screen(screener_session, user_id, "hookscreen02")
        endpoint = await _endpoint(screener_session, user_id, screen)
        delivery = await _queued(screener_session, endpoint)

        async with Receiver(503).client() as client:
            outcome = await webhooks.attempt_delivery(
                screener_session, delivery, endpoint, settings=settings, client=client, now=NOW
            )
        assert outcome.retrying
        assert delivery.status == "pending"
        assert delivery.next_attempt_at == NOW + dt.timedelta(seconds=30)

    async def test_a_401_is_permanent(self, screener_session: AsyncSession) -> None:
        """Retrying a refusal the receiver meant is a self-inflicted denial of service."""
        settings = settings_for()
        user_id, _ = await api_helpers.make_user(screener_session, "hook401@example.com")
        screen = await make_screen(screener_session, user_id, "hookscreen03")
        endpoint = await _endpoint(screener_session, user_id, screen)
        delivery = await _queued(screener_session, endpoint)

        async with Receiver(401).client() as client:
            outcome = await webhooks.attempt_delivery(
                screener_session, delivery, endpoint, settings=settings, client=client, now=NOW
            )
        assert not outcome.retrying
        assert delivery.status == "failed"
        assert delivery.attempts == 1

    async def test_a_429_is_not_permanent(self, screener_session: AsyncSession) -> None:
        settings = settings_for()
        user_id, _ = await api_helpers.make_user(screener_session, "hook429@example.com")
        screen = await make_screen(screener_session, user_id, "hookscreen04")
        endpoint = await _endpoint(screener_session, user_id, screen)
        delivery = await _queued(screener_session, endpoint)

        async with Receiver(429).client() as client:
            outcome = await webhooks.attempt_delivery(
                screener_session, delivery, endpoint, settings=settings, client=client, now=NOW
            )
        assert outcome.retrying

    async def test_a_connection_error_is_retried_and_recorded(
        self, screener_session: AsyncSession
    ) -> None:
        settings = settings_for()
        user_id, _ = await api_helpers.make_user(screener_session, "hookdead@example.com")
        screen = await make_screen(screener_session, user_id, "hookscreen05")
        endpoint = await _endpoint(screener_session, user_id, screen)
        delivery = await _queued(screener_session, endpoint)

        async with Receiver(raises=True).client() as client:
            outcome = await webhooks.attempt_delivery(
                screener_session, delivery, endpoint, settings=settings, client=client, now=NOW
            )
        assert outcome.retrying
        assert delivery.response_status is None
        assert delivery.last_error is not None
        assert "ConnectError" in delivery.last_error

    async def test_attempts_run_out(self, screener_session: AsyncSession) -> None:
        settings = settings_for()
        user_id, _ = await api_helpers.make_user(screener_session, "hookgone@example.com")
        screen = await make_screen(screener_session, user_id, "hookscreen06")
        endpoint = await _endpoint(screener_session, user_id, screen)
        delivery = await _queued(screener_session, endpoint)

        async with Receiver(503).client() as client:
            for _ in range(settings.webhook_max_attempts):
                await webhooks.attempt_delivery(
                    screener_session,
                    delivery,
                    endpoint,
                    settings=settings,
                    client=client,
                    now=NOW,
                )
        assert delivery.attempts == settings.webhook_max_attempts
        assert delivery.status == "failed"

    async def test_an_endpoint_that_keeps_failing_is_switched_off(
        self, screener_session: AsyncSession
    ) -> None:
        settings = settings_for().model_copy(update={"webhook_failure_threshold": 2})
        user_id, _ = await api_helpers.make_user(screener_session, "hookoff@example.com")
        screen = await make_screen(screener_session, user_id, "hookscreen07")
        endpoint = await _endpoint(screener_session, user_id, screen)

        async with Receiver(500).client() as client:
            for index in range(2):
                delivery = await _queued(screener_session, endpoint, key=f"k{index}")
                await webhooks.attempt_delivery(
                    screener_session,
                    delivery,
                    endpoint,
                    settings=settings,
                    client=client,
                    now=NOW,
                )
        assert endpoint.is_active is False
        assert endpoint.disabled_reason is not None


class TestIdempotency:
    async def test_enqueuing_the_same_key_twice_creates_one_delivery(
        self, screener_session: AsyncSession
    ) -> None:
        user_id, _ = await api_helpers.make_user(screener_session, "hookonce@example.com")
        screen = await make_screen(screener_session, user_id, "hookscreen08")
        endpoint = await _endpoint(screener_session, user_id, screen)
        await _queued(screener_session, endpoint, key="same")
        await _queued(screener_session, endpoint, key="same")
        rows = (
            (
                await screener_session.execute(
                    select(WebhookDelivery).where(WebhookDelivery.endpoint_id == endpoint.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1


class TestTheSweep:
    async def test_it_attempts_what_is_due_and_leaves_what_is_not(
        self, screener_session: AsyncSession
    ) -> None:
        settings = settings_for()
        user_id, _ = await api_helpers.make_user(screener_session, "hooksweep@example.com")
        screen = await make_screen(screener_session, user_id, "hookscreen09")
        endpoint = await _endpoint(screener_session, user_id, screen)
        due = await _queued(screener_session, endpoint, key="due")
        later = await _queued(screener_session, endpoint, key="later")
        later.next_attempt_at = NOW + dt.timedelta(hours=1)
        await screener_session.flush()

        receiver = Receiver(200)
        async with receiver.client() as client:
            report = await run_webhook_sweep(
                screener_session, settings=settings, client=client, now=NOW
            )
        assert report == {"attempted": 1, "delivered": 1, "retrying": 0, "failed": 0}
        assert due.status == "delivered"
        assert later.status == "pending"

    async def test_a_disabled_endpoint_is_not_swept(self, screener_session: AsyncSession) -> None:
        settings = settings_for()
        user_id, _ = await api_helpers.make_user(screener_session, "hookdis@example.com")
        screen = await make_screen(screener_session, user_id, "hookscreen10")
        endpoint = await _endpoint(screener_session, user_id, screen)
        await _queued(screener_session, endpoint, key="orphan")
        endpoint.is_active = False
        await screener_session.flush()

        async with Receiver(200).client() as client:
            report = await run_webhook_sweep(
                screener_session, settings=settings, client=client, now=NOW
            )
        assert report["attempted"] == 0


class TestFanOutFromAScreenDiff:
    async def test_the_nightly_dispatch_enqueues_entries_and_exits(
        self, screener_session: AsyncSession
    ) -> None:
        """Prompt 20 §4: "Webhooks for entries/exits on a screen"."""
        settings = settings_for()
        user_id, _ = await api_helpers.make_user(screener_session, "hookfan@example.com")
        screen = await make_screen(screener_session, user_id, "hookscreen11")
        endpoint = await _endpoint(screener_session, user_id, screen)
        ids = await instrument_ids(screener_session, 3)
        await make_run(screener_session, screen, PREVIOUS, [(1, ids[0]), (2, ids[1])])
        await make_run(screener_session, screen, CURRENT, [(1, ids[0]), (2, ids[2])])

        report = await alert_service.dispatch(
            screener_session,
            as_of=CURRENT,
            mailer=Mailer(Recorder()),
            settings=settings,
        )
        assert report.webhooks_enqueued == 2
        rows = (
            (
                await screener_session.execute(
                    select(WebhookDelivery).where(WebhookDelivery.endpoint_id == endpoint.id)
                )
            )
            .scalars()
            .all()
        )
        assert {row.event for row in rows} == {"screen.entries", "screen.exits"}

    async def test_the_payload_carries_symbols_and_ranks_and_no_market_data(
        self, screener_session: AsyncSession
    ) -> None:
        """docs/11 §Compliance applies to a webhook exactly as it applies to the public API."""
        settings = settings_for()
        user_id, _ = await api_helpers.make_user(screener_session, "hookbody@example.com")
        screen = await make_screen(screener_session, user_id, "hookscreen12")
        endpoint = await _endpoint(screener_session, user_id, screen)
        ids = await instrument_ids(screener_session, 3)
        await make_run(screener_session, screen, PREVIOUS, [(1, ids[0]), (2, ids[1])])
        await make_run(screener_session, screen, CURRENT, [(1, ids[0]), (2, ids[2])])
        await alert_service.dispatch(
            screener_session, as_of=CURRENT, mailer=Mailer(Recorder()), settings=settings
        )

        row = (
            await screener_session.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.endpoint_id == endpoint.id,
                    WebhookDelivery.event == "screen.entries",
                )
            )
        ).scalar_one()
        payload = row.payload
        assert set(payload) == {"event", "screen", "as_of", "previous_as_of", "instruments"}
        instruments = payload["instruments"]
        assert isinstance(instruments, list)
        assert set(instruments[0]) == {"symbol", "rank"}


class TestTheEndpointApi:
    async def test_a_url_that_is_not_http_is_refused(
        self, seeded_url: str, screener_session: AsyncSession, clean_redis_namespaces: None
    ) -> None:
        del clean_redis_namespaces
        user_id, public_id = await api_helpers.make_user(
            screener_session, "hookapi@example.com", subscribed=True
        )
        screen = await make_screen(screener_session, user_id, "hookscreen20")
        async with api_helpers.running_app(settings_for(seeded_url), screener_session) as client:
            response = await client.post(
                ENDPOINTS,
                json={"screen_public_id": screen.public_id, "url": "file:///etc/passwd"},
                headers=api_helpers.bearer(public_id),
            )
            api_helpers.assert_problem(response, 400, "invalid-screen-definition")

    async def test_creation_returns_the_signing_secret_and_rotation_changes_it(
        self, seeded_url: str, screener_session: AsyncSession, clean_redis_namespaces: None
    ) -> None:
        del clean_redis_namespaces
        user_id, public_id = await api_helpers.make_user(
            screener_session, "hookapi2@example.com", subscribed=True
        )
        screen = await make_screen(screener_session, user_id, "hookscreen21")
        async with api_helpers.running_app(settings_for(seeded_url), screener_session) as client:
            created = await client.post(
                ENDPOINTS,
                json={"screen_public_id": screen.public_id, "url": "https://example.com/hook"},
                headers=api_helpers.bearer(public_id),
            )
            assert created.status_code == 201, created.text
            first_secret = created.json()["signing_secret"]
            assert first_secret.startswith("whsec_")
            endpoint_id = created.json()["endpoint"]["public_id"]

            rotated = await client.post(
                f"{ENDPOINTS}/{endpoint_id}/rotate-secret",
                headers=api_helpers.bearer(public_id),
            )
            assert rotated.status_code == 200, rotated.text
            assert rotated.json()["signing_secret"] != first_secret
            assert rotated.json()["endpoint"]["secret_version"] == 2

    async def test_one_account_cannot_see_anothers_endpoint(
        self, seeded_url: str, screener_session: AsyncSession, clean_redis_namespaces: None
    ) -> None:
        del clean_redis_namespaces
        owner_id, _ = await api_helpers.make_user(
            screener_session, "hookowner@example.com", subscribed=True
        )
        _, stranger = await api_helpers.make_user(
            screener_session, "hookstranger@example.com", subscribed=True
        )
        screen = await make_screen(screener_session, owner_id, "hookscreen22")
        endpoint = await _endpoint(screener_session, owner_id, screen)
        async with api_helpers.running_app(settings_for(seeded_url), screener_session) as client:
            listed = await client.get(ENDPOINTS, headers=api_helpers.bearer(stranger))
            assert listed.json()["endpoints"] == []
            response = await client.delete(
                f"{ENDPOINTS}/{endpoint.public_id}", headers=api_helpers.bearer(stranger)
            )
            api_helpers.assert_problem(response, 404, "not-found")
