"""``/keys`` and the credential behind the public read API — Prompt 20 deliverable 1.

The headline is :class:`TestRevocationIsImmediate`, which is Prompt 20's **first acceptance
criterion**, asserted twice: once against the service (which is where the absence of a cache
actually lives) and once end to end over HTTP against the public API.
"""

from __future__ import annotations

import datetime as dt
import time

import api_helpers
import httpx
import pytest
import screener_helpers
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from decile_api import api_keys as service
from decile_core.api_keys import KEY_TOKEN_MARKER, MAX_KEYS_PER_ACCOUNT, Scope
from decile_core.models import ApiKey, ApiKeyUsageDaily
from decile_core.public_api import PUBLIC_API_PREFIX

pytestmark = [screener_helpers.requires_db, pytest.mark.db, pytest.mark.redis]

KEYS = api_helpers.url("/keys")

#: Prompt 20's first acceptance criterion, in seconds.
REVOCATION_BUDGET_SECONDS = 1.0


async def _account(session: AsyncSession, email: str = "keys@example.com") -> tuple[int, str]:
    return await api_helpers.make_user(session, email, subscribed=True)


class IssuedKey:
    """The ``ApiKeyIssuedOut`` body, typed.

    ``response.json()`` is ``object``, and indexing it twice would need the escape hatch CLAUDE.md
    house rule 3 forbids outright — so the narrowing happens once, here.
    """

    def __init__(self, body: object) -> None:
        assert isinstance(body, dict)
        key = body["key"]
        assert isinstance(key, dict)
        self.body: dict[str, object] = body
        self.key: dict[str, object] = key

    @property
    def secret(self) -> str:
        return str(self.body["secret"])

    @property
    def public_id(self) -> str:
        return str(self.key["public_id"])

    @property
    def display(self) -> str:
        return str(self.key["display"])

    @property
    def scopes(self) -> set[str]:
        scopes = self.key["scopes"]
        assert isinstance(scopes, list)
        return {str(scope) for scope in scopes}


async def _create(client: httpx.AsyncClient, public_id: str, **body: object) -> IssuedKey:
    payload: dict[str, object] = {"name": "ci", **body}
    response = await client.post(KEYS, json=payload, headers=api_helpers.bearer(public_id))
    assert response.status_code == 201, response.text
    return IssuedKey(response.json())


class TestCreation:
    async def test_the_secret_is_returned_once_and_never_again(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, public_id = await _account(screener_session)
        created = await _create(api, public_id, name="my laptop")
        secret = created.secret
        assert secret.startswith(f"{KEY_TOKEN_MARKER}_")

        listed = await api.get(KEYS, headers=api_helpers.bearer(public_id))
        assert listed.status_code == 200
        body = listed.text
        assert secret not in body, "the plaintext key must never appear again"

    async def test_the_row_stores_a_digest_and_not_the_secret(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, public_id = await _account(screener_session)
        created = await _create(api, public_id)
        secret = created.secret
        rows = (await screener_session.execute(select(ApiKey))).scalars().all()
        stored = [row for row in rows if secret.split("_")[1] == row.prefix]
        assert len(stored) == 1
        assert secret.split("_")[2] not in stored[0].token_hash
        assert len(stored[0].token_hash) == 64  # sha256 hex

    async def test_the_display_shows_the_prefix_so_two_keys_are_tellable_apart(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, public_id = await _account(screener_session)
        first = await _create(api, public_id, name="one")
        second = await _create(api, public_id, name="two")
        assert first.display != second.display

    async def test_a_key_defaults_to_every_read_scope(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, public_id = await _account(screener_session)
        created = await _create(api, public_id)
        assert created.scopes == {scope.value for scope in Scope}

    async def test_an_unknown_scope_is_refused(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, public_id = await _account(screener_session)
        response = await api.post(
            KEYS,
            json={"name": "bad", "scopes": ["screens:write"]},
            headers=api_helpers.bearer(public_id),
        )
        api_helpers.assert_problem(response, 400, "invalid-screen-definition")

    async def test_a_rate_limit_above_the_ceiling_is_refused_not_clamped(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, public_id = await _account(screener_session)
        response = await api.post(
            KEYS,
            json={"name": "greedy", "rate_limit_per_minute": 100_000},
            headers=api_helpers.bearer(public_id),
        )
        body = api_helpers.assert_problem(response, 400, "invalid-screen-definition")
        assert "may not exceed" in str(body["detail"])

    async def test_the_account_cap_is_enforced(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, public_id = await _account(screener_session)
        for index in range(MAX_KEYS_PER_ACCOUNT):
            await _create(api, public_id, name=f"key-{index}")
        response = await api.post(
            KEYS, json={"name": "one too many"}, headers=api_helpers.bearer(public_id)
        )
        api_helpers.assert_problem(response, 400, "invalid-screen-definition")

    async def test_an_anonymous_caller_cannot_create_a_key(self, api: httpx.AsyncClient) -> None:
        response = await api.post(KEYS, json={"name": "nope"})
        api_helpers.assert_problem(response, 401, "unauthenticated")


class TestRevocationIsImmediate:
    """Prompt 20: "a revoked key is rejected within one second (no cached-auth window)"."""

    async def test_the_service_rejects_a_revoked_key_with_no_cached_window(
        self, screener_session: AsyncSession
    ) -> None:
        user_id, _ = await _account(screener_session, "revoke-service@example.com")
        settings = api_helpers.api_settings("postgresql+asyncpg://unused/unused")
        issued = await service.create_key(
            screener_session,
            user_id=user_id,
            name="ci",
            scopes=list(Scope),
            settings=settings,
        )
        assert await service.authenticate(screener_session, issued.plaintext) is not None

        revoked_at = time.monotonic()
        await service.revoke_key(screener_session, issued.key, reason="leaked")
        assert await service.authenticate(screener_session, issued.plaintext) is None
        assert time.monotonic() - revoked_at < REVOCATION_BUDGET_SECONDS

    async def test_the_public_api_rejects_a_revoked_key_within_one_second(
        self, seeded_url: str, screener_session: AsyncSession, clean_redis_namespaces: None
    ) -> None:
        """End to end: 200 with the key, revoke, then 401 — and the clock is checked."""
        del clean_redis_namespaces
        _, public_id = await _account(screener_session, "revoke-http@example.com")
        settings = api_helpers.api_settings(seeded_url)
        async with api_helpers.running_public_app(settings, screener_session) as client:
            created = await _create(client, public_id, name="ci")
            secret = created.secret
            key_public_id = created.public_id

            headers = {"X-API-Key": secret}
            first = await client.get(f"{PUBLIC_API_PREFIX}/status", headers=headers)
            assert first.status_code == 200, first.text

            revoked_at = time.monotonic()
            revoke = await client.post(
                f"{KEYS}/{key_public_id}/revoke",
                json={"reason": "leaked"},
                headers=api_helpers.bearer(public_id),
            )
            assert revoke.status_code == 200, revoke.text

            after = await client.get(f"{PUBLIC_API_PREFIX}/status", headers=headers)
            elapsed = time.monotonic() - revoked_at
            api_helpers.assert_problem(after, 401, "unauthenticated")
            assert elapsed < REVOCATION_BUDGET_SECONDS, (
                f"a revoked key was still refused, but only after {elapsed:.3f}s"
            )

    async def test_revoking_twice_keeps_the_first_timestamp(
        self, screener_session: AsyncSession
    ) -> None:
        user_id, _ = await _account(screener_session, "revoke-twice@example.com")
        settings = api_helpers.api_settings("postgresql+asyncpg://unused/unused")
        issued = await service.create_key(
            screener_session, user_id=user_id, name="ci", scopes=list(Scope), settings=settings
        )
        await service.revoke_key(screener_session, issued.key, reason="first")
        first = issued.key.revoked_at
        await service.revoke_key(screener_session, issued.key, reason="second")
        assert issued.key.revoked_at == first
        assert issued.key.revoked_reason == "first"


class TestRotation:
    async def test_rotation_issues_a_new_secret_and_kills_the_old_one(
        self, seeded_url: str, screener_session: AsyncSession, clean_redis_namespaces: None
    ) -> None:
        del clean_redis_namespaces
        _, public_id = await _account(screener_session, "rotate@example.com")
        settings = api_helpers.api_settings(seeded_url)
        async with api_helpers.running_public_app(settings, screener_session) as client:
            created = await _create(client, public_id, name="ci")
            old_secret = created.secret
            key_public_id = created.public_id

            rotated = await client.post(
                f"{KEYS}/{key_public_id}/rotate", headers=api_helpers.bearer(public_id)
            )
            assert rotated.status_code == 200, rotated.text
            new_secret = str(rotated.json()["secret"])
            assert new_secret != old_secret

            old = await client.get(f"{PUBLIC_API_PREFIX}/status", headers={"X-API-Key": old_secret})
            api_helpers.assert_problem(old, 401, "unauthenticated")
            new = await client.get(f"{PUBLIC_API_PREFIX}/status", headers={"X-API-Key": new_secret})
            assert new.status_code == 200, new.text

    async def test_the_replacement_records_what_it_replaced(
        self, screener_session: AsyncSession
    ) -> None:
        user_id, _ = await _account(screener_session, "rotate-link@example.com")
        settings = api_helpers.api_settings("postgresql+asyncpg://unused/unused")
        first = await service.create_key(
            screener_session, user_id=user_id, name="ci", scopes=list(Scope), settings=settings
        )
        second = await service.rotate_key(screener_session, first.key, settings=settings)
        assert second.key.rotated_from_id == first.key.id
        assert first.key.revoked_reason == "rotated"


class TestDeletion:
    async def test_a_live_key_cannot_be_deleted(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, public_id = await _account(screener_session)
        created = await _create(api, public_id)
        key_public_id = created.public_id
        response = await api.delete(
            f"{KEYS}/{key_public_id}", headers=api_helpers.bearer(public_id)
        )
        api_helpers.assert_problem(response, 400, "invalid-screen-definition")

    async def test_a_revoked_key_can_be(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, public_id = await _account(screener_session)
        created = await _create(api, public_id)
        key_public_id = created.public_id
        await api.post(
            f"{KEYS}/{key_public_id}/revoke", json={}, headers=api_helpers.bearer(public_id)
        )
        response = await api.delete(
            f"{KEYS}/{key_public_id}", headers=api_helpers.bearer(public_id)
        )
        assert response.status_code == 204


class TestIsolation:
    async def test_one_account_cannot_see_or_touch_another_accounts_key(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        _, owner = await _account(screener_session, "owner@example.com")
        _, stranger = await _account(screener_session, "stranger@example.com")
        created = await _create(api, owner)
        key_public_id = created.public_id

        listed = await api.get(KEYS, headers=api_helpers.bearer(stranger))
        assert listed.json()["keys"] == []

        response = await api.post(
            f"{KEYS}/{key_public_id}/revoke", json={}, headers=api_helpers.bearer(stranger)
        )
        api_helpers.assert_problem(response, 404, "not-found")


class TestUsage:
    async def test_a_public_request_is_counted_against_the_key(
        self, seeded_url: str, screener_session: AsyncSession, clean_redis_namespaces: None
    ) -> None:
        del clean_redis_namespaces
        _, public_id = await _account(screener_session, "usage@example.com")
        settings = api_helpers.api_settings(seeded_url)
        async with api_helpers.running_public_app(settings, screener_session) as client:
            created = await _create(client, public_id, name="ci")
            secret = created.secret
            key_public_id = created.public_id
            for _ in range(3):
                await client.get(f"{PUBLIC_API_PREFIX}/status", headers={"X-API-Key": secret})

            usage = await client.get(
                f"{KEYS}/{key_public_id}/usage", headers=api_helpers.bearer(public_id)
            )
            assert usage.status_code == 200, usage.text
            body = usage.json()
            assert body["total_requests"] >= 3
            assert body["from"] <= dt.datetime.now(tz=dt.UTC).date().isoformat()

    async def test_the_counter_is_incremented_not_overwritten(
        self, screener_session: AsyncSession
    ) -> None:
        user_id, _ = await _account(screener_session, "counter@example.com")
        settings = api_helpers.api_settings("postgresql+asyncpg://unused/unused")
        issued = await service.create_key(
            screener_session, user_id=user_id, name="ci", scopes=list(Scope), settings=settings
        )
        day = dt.date(2026, 8, 19)
        await service.record_usage(screener_session, issued.key.id, requests=1, day=day)
        await service.record_usage(screener_session, issued.key.id, requests=1, day=day)
        await service.record_usage(screener_session, issued.key.id, throttled=1, day=day)
        row = (
            await screener_session.execute(
                select(ApiKeyUsageDaily).where(
                    ApiKeyUsageDaily.api_key_id == issued.key.id, ApiKeyUsageDaily.date == day
                )
            )
        ).scalar_one()
        assert row.requests == 2
        assert row.throttled == 1
