"""The public read API's surface, driven with the compliance gate deliberately opened.

`test_public_api_flag.py` asserts the gate is shut in every committed configuration. This module
opens it for the duration of one app (``api_helpers.running_public_app``) and asserts what the
surface *would* serve, because a feature that is only ever tested switched off is a feature nobody
has tested.

The assertions that matter most are the negative ones: no response carries a price, a volume or a
moving average, and a screen that sorts by a withheld column is refused rather than served.
"""

from __future__ import annotations

import api_helpers
import pytest
import screener_helpers
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import api_keys as service
from baskfy_core.api_keys import Scope
from baskfy_core.models import Screen
from baskfy_core.public_api import PUBLIC_API_PREFIX, PUBLIC_COLUMNS, RAW_BAR_FIELDS
from baskfy_core.seed_data import EXAMPLE_SCREENS

pytestmark = [screener_helpers.requires_db, pytest.mark.db, pytest.mark.redis]


async def _key(
    session: AsyncSession, email: str, *, scopes: list[Scope] | None = None
) -> tuple[str, str]:
    """An account and a live key. Returns ``(user public_id, X-API-Key value)``."""
    user_id, public_id = await api_helpers.make_user(session, email, subscribed=True)
    settings = api_helpers.api_settings("postgresql+asyncpg://unused/unused")
    issued = await service.create_key(
        session,
        user_id=user_id,
        name="ci",
        scopes=scopes if scopes is not None else list(Scope),
        settings=settings,
    )
    return public_id, issued.plaintext


def _leaks(payload: object) -> list[str]:
    """Every withheld field name that appears as a key anywhere in a response body."""
    found: list[str] = []
    if isinstance(payload, dict):
        for name, value in payload.items():
            if name in RAW_BAR_FIELDS:
                found.append(name)
            found.extend(_leaks(value))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(_leaks(item))
    return found


class TestTermsOfUse:
    async def test_it_is_readable_without_a_key(
        self, seeded_url: str, screener_session: AsyncSession, clean_redis_namespaces: None
    ) -> None:
        """An integrator has to read the licence before they hold a credential."""
        del clean_redis_namespaces
        settings = api_helpers.api_settings(seeded_url)
        async with api_helpers.running_public_app(settings, screener_session) as client:
            response = await client.get(f"{PUBLIC_API_PREFIX}/terms")
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["redistribution_permitted"] is False
            assert set(body["served_fields"]) == set(PUBLIC_COLUMNS)
            assert set(body["withheld_fields"]) & RAW_BAR_FIELDS

    async def test_it_is_cacheable_for_a_day(
        self, seeded_url: str, screener_session: AsyncSession, clean_redis_namespaces: None
    ) -> None:
        del clean_redis_namespaces
        settings = api_helpers.api_settings(seeded_url)
        async with api_helpers.running_public_app(settings, screener_session) as client:
            response = await client.get(f"{PUBLIC_API_PREFIX}/terms")
            assert "max-age=86400" in response.headers["cache-control"]


class TestAuthentication:
    async def test_no_key_is_a_401(
        self, seeded_url: str, screener_session: AsyncSession, clean_redis_namespaces: None
    ) -> None:
        del clean_redis_namespaces
        settings = api_helpers.api_settings(seeded_url)
        async with api_helpers.running_public_app(settings, screener_session) as client:
            response = await client.get(f"{PUBLIC_API_PREFIX}/status")
            api_helpers.assert_problem(response, 401, "unauthenticated")

    async def test_a_bearer_token_does_not_authenticate_the_public_api(
        self, seeded_url: str, screener_session: AsyncSession, clean_redis_namespaces: None
    ) -> None:
        """A session is not a key. The two credentials open different doors, deliberately."""
        del clean_redis_namespaces
        public_id, _ = await _key(screener_session, "bearer-only@example.com")
        settings = api_helpers.api_settings(seeded_url)
        async with api_helpers.running_public_app(settings, screener_session) as client:
            response = await client.get(
                f"{PUBLIC_API_PREFIX}/status", headers=api_helpers.bearer(public_id)
            )
            api_helpers.assert_problem(response, 401, "unauthenticated")

    async def test_an_api_key_does_not_authenticate_the_product_api(
        self, seeded_url: str, screener_session: AsyncSession, clean_redis_namespaces: None
    ) -> None:
        """The converse, and the more important half: a read-only key cannot reach `/api/v1`."""
        del clean_redis_namespaces
        _, secret = await _key(screener_session, "key-only@example.com")
        settings = api_helpers.api_settings(seeded_url)
        async with api_helpers.running_public_app(settings, screener_session) as client:
            response = await client.get(api_helpers.url("/keys"), headers={"X-API-Key": secret})
            api_helpers.assert_problem(response, 401, "unauthenticated")

    async def test_a_scope_the_key_does_not_carry_is_a_402(
        self, seeded_url: str, screener_session: AsyncSession, clean_redis_namespaces: None
    ) -> None:
        del clean_redis_namespaces
        _, secret = await _key(screener_session, "narrow@example.com", scopes=[Scope.META_READ])
        settings = api_helpers.api_settings(seeded_url)
        async with api_helpers.running_public_app(settings, screener_session) as client:
            allowed = await client.get(f"{PUBLIC_API_PREFIX}/status", headers={"X-API-Key": secret})
            assert allowed.status_code == 200, allowed.text
            refused = await client.get(
                f"{PUBLIC_API_PREFIX}/breadth", headers={"X-API-Key": secret}
            )
            body = api_helpers.assert_problem(refused, 402, "payment-required")
            assert body["feature"] == "breadth:read"


class TestDerivedAnalyticsOnly:
    """docs/11 §Compliance: "Serve derived analytics; do not expose a raw-bar API"."""

    async def test_a_screen_result_carries_no_price_or_volume(
        self, seeded_url: str, screener_session: AsyncSession, clean_redis_namespaces: None
    ) -> None:
        del clean_redis_namespaces
        _, secret = await _key(screener_session, "screens@example.com")
        example = EXAMPLE_SCREENS[0]
        settings = api_helpers.api_settings(seeded_url)
        async with api_helpers.running_public_app(settings, screener_session) as client:
            response = await client.get(
                f"{PUBLIC_API_PREFIX}/screens/{example.public_id}/results",
                headers={"X-API-Key": secret},
                params={"limit": 5},
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert _leaks(body) == [], f"a withheld field reached the wire: {_leaks(body)}"
            assert body["rows"], "the seeded reference export should produce rows"
            assert "close_raw" not in body["columns"]
            assert body["disclaimer"].startswith("Baskfy is not a SEBI-registered")

    async def test_a_screen_sorted_by_a_withheld_column_is_refused(
        self, seeded_url: str, screener_session: AsyncSession, clean_redis_namespaces: None
    ) -> None:
        """`sorting_factor` is always projected, so the column whitelist alone is not enough."""
        del clean_redis_namespaces
        user_id, _ = await api_helpers.make_user(
            screener_session, "priced-sort@example.com", subscribed=True
        )
        settings = api_helpers.api_settings("postgresql+asyncpg://unused/unused")
        issued = await service.create_key(
            screener_session, user_id=user_id, name="ci", scopes=list(Scope), settings=settings
        )
        screen = Screen(
            public_id="pricedsort01",
            user_id=user_id,
            name="Sorted by the last close",
            definition={"index": "nifty-total-market", "sort_by": "close_raw"},
            columns=[],
            is_example=False,
        )
        screener_session.add(screen)
        await screener_session.flush()

        async with api_helpers.running_public_app(
            api_helpers.api_settings(seeded_url), screener_session
        ) as client:
            response = await client.get(
                f"{PUBLIC_API_PREFIX}/screens/pricedsort01/results",
                headers={"X-API-Key": issued.plaintext},
            )
            body = api_helpers.assert_problem(response, 402, "payment-required")
            assert "close_raw" in str(body["detail"])

    async def test_factor_values_carry_no_price(
        self, seeded_url: str, screener_session: AsyncSession, clean_redis_namespaces: None
    ) -> None:
        del clean_redis_namespaces
        _, secret = await _key(screener_session, "factors@example.com")
        settings = api_helpers.api_settings(seeded_url)
        async with api_helpers.running_public_app(settings, screener_session) as client:
            response = await client.get(
                f"{PUBLIC_API_PREFIX}/instruments/CUPID/factors",
                headers={"X-API-Key": secret},
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert _leaks(body) == []
            assert body["symbol"] == "CUPID"
            assert "ret_12m" in body["factors"]

    async def test_breadth_is_served(
        self, seeded_url: str, screener_session: AsyncSession, clean_redis_namespaces: None
    ) -> None:
        del clean_redis_namespaces
        _, secret = await _key(screener_session, "breadth@example.com")
        settings = api_helpers.api_settings(seeded_url)
        async with api_helpers.running_public_app(settings, screener_session) as client:
            response = await client.get(
                f"{PUBLIC_API_PREFIX}/breadth",
                headers={"X-API-Key": secret},
                params={"universe": "nifty-500"},
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert _leaks(body) == []
            assert body["universe"] == "nifty-500"

    async def test_an_unknown_universe_is_a_404(
        self, seeded_url: str, screener_session: AsyncSession, clean_redis_namespaces: None
    ) -> None:
        del clean_redis_namespaces
        _, secret = await _key(screener_session, "bad-universe@example.com")
        settings = api_helpers.api_settings(seeded_url)
        async with api_helpers.running_public_app(settings, screener_session) as client:
            response = await client.get(
                f"{PUBLIC_API_PREFIX}/breadth",
                headers={"X-API-Key": secret},
                params={"universe": "not-a-universe"},
            )
            api_helpers.assert_problem(response, 404, "not-found")


class TestPerKeyRateLimit:
    async def test_a_key_with_a_tiny_limit_is_throttled_and_the_throttle_is_counted(
        self, seeded_url: str, screener_session: AsyncSession, clean_redis_namespaces: None
    ) -> None:
        """PROMPTS.md Prompt 20 §1: "per-key rate limits"."""
        del clean_redis_namespaces
        user_id, public_id = await api_helpers.make_user(
            screener_session, "throttled@example.com", subscribed=True
        )
        settings = api_helpers.api_settings("postgresql+asyncpg://unused/unused")
        issued = await service.create_key(
            screener_session,
            user_id=user_id,
            name="tiny",
            scopes=list(Scope),
            settings=settings,
            rate_limit_per_minute=1,
        )
        headers = {"X-API-Key": issued.plaintext}
        async with api_helpers.running_public_app(
            api_helpers.api_settings(seeded_url), screener_session
        ) as client:
            statuses = [
                (await client.get(f"{PUBLIC_API_PREFIX}/status", headers=headers)).status_code
                for _ in range(4)
            ]
            assert statuses[0] == 200, statuses
            assert 429 in statuses, statuses

            usage = await client.get(
                api_helpers.url(f"/keys/{issued.key.public_id}/usage"),
                headers=api_helpers.bearer(public_id),
            )
            assert usage.json()["total_throttled"] >= 1
