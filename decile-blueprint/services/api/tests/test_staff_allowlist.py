"""Durable staff grant via ``BASKFY_STAFF_ALLOWLIST``.

Settings tests here have no database mark so they always run. Promotion on
``GET /me`` and Google identity resolve needs the seeded database, same as
``test_api_auth.py``.
"""

from __future__ import annotations

from typing import Final

import api_helpers
import httpx
import pytest
from api_helpers import StubGoogle, bearer, google_token, make_user, running_app, url
from screener_helpers import requires_db
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.settings import Settings
from baskfy_core.models import AppUser

FOUNDER: Final = "mdave.5191@gmail.com"
FOUNDER_ADDRESSES: Final = frozenset(
    {
        "mdave.5191@gmail.com",
        "learnwithalacrity@gmail.com",
        "maulikdave05@gmail.com",
    }
)


class TestTheStaffAllowlist:
    """The founder is staff without a one-off SQL — the default is the grant."""

    def test_the_default_allowlist_includes_the_founder(self) -> None:
        default = Settings.model_fields["staff_allowlist"].default
        settings = api_helpers.api_settings(
            "postgresql+asyncpg://x/y", staff_allowlist=str(default)
        )
        assert settings.allowed_staff == FOUNDER_ADDRESSES
        assert settings.is_staff_email("MDAVE.5191@gmail.com")
        assert settings.is_staff_email("learnwithalacrity@gmail.com")
        assert settings.is_staff_email("maulikdave05@gmail.com")
        assert not settings.is_staff_email("anyone@example.com")

    def test_extra_addresses_are_parsed_like_the_login_allowlist(self) -> None:
        settings = api_helpers.api_settings(
            "postgresql+asyncpg://x/y",
            staff_allowlist="  Owner@Example.COM , second@example.com ",
        )
        assert settings.allowed_staff == {"owner@example.com", "second@example.com"}
        assert settings.is_staff_email("OWNER@example.com")
        assert settings.is_staff_email("second@example.com")
        assert not settings.is_staff_email("third@example.com")
        assert not settings.is_staff_email(FOUNDER)


def _with_stub_google(client: httpx.AsyncClient) -> httpx.AsyncClient:
    app = getattr(client._transport, "app", None)
    assert app is not None, "running_app must be built on an ASGITransport"
    app.state.google_verifier = StubGoogle()
    return client


@pytest.mark.db
@pytest.mark.redis
@requires_db
class TestStaffAllowlistPromotion:
    """An allowlisted address becomes staff; the list never demotes."""

    async def test_an_allowlisted_address_becomes_staff_on_me(
        self, seeded_url: str, screener_session: AsyncSession
    ) -> None:
        user_id, public_id = await make_user(screener_session, FOUNDER)
        user = (
            await screener_session.execute(select(AppUser).where(AppUser.id == user_id))
        ).scalar_one()
        assert user.is_staff is False
        settings = api_helpers.api_settings(seeded_url, staff_allowlist=FOUNDER)
        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/me"), headers=bearer(public_id))
        assert response.status_code == 200, response.text
        assert response.json()["is_staff"] is True
        await screener_session.refresh(user)
        assert user.is_staff is True

    async def test_an_unrelated_address_stays_civilian_on_me(
        self, seeded_url: str, screener_session: AsyncSession
    ) -> None:
        _, public_id = await make_user(screener_session, "civilian@example.com")
        settings = api_helpers.api_settings(seeded_url, staff_allowlist=FOUNDER)
        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/me"), headers=bearer(public_id))
        assert response.status_code == 200, response.text
        assert response.json()["is_staff"] is False
        user = (
            await screener_session.execute(
                select(AppUser).where(AppUser.email == "civilian@example.com")
            )
        ).scalar_one()
        assert user.is_staff is False

    async def test_an_already_staff_unrelated_address_stays_staff(
        self, seeded_url: str, screener_session: AsyncSession
    ) -> None:
        user_id, public_id = await make_user(screener_session, "ops-already@example.com")
        user = (
            await screener_session.execute(select(AppUser).where(AppUser.id == user_id))
        ).scalar_one()
        user.is_staff = True
        await screener_session.flush()
        settings = api_helpers.api_settings(seeded_url, staff_allowlist=FOUNDER)
        async with running_app(settings, screener_session) as client:
            response = await client.get(url("/me"), headers=bearer(public_id))
        assert response.status_code == 200, response.text
        assert response.json()["is_staff"] is True
        await screener_session.refresh(user)
        assert user.is_staff is True

    async def test_an_allowlisted_address_becomes_staff_on_google_sign_in(
        self, seeded_url: str, screener_session: AsyncSession
    ) -> None:
        settings = api_helpers.api_settings(seeded_url, staff_allowlist=FOUNDER)
        async with running_app(settings, screener_session) as client:
            response = await _with_stub_google(client).post(
                url("/auth/google"),
                json={"id_token": google_token(subject="staff-sub-1", email=FOUNDER)},
            )
        assert response.status_code == 200, response.text
        user = (
            await screener_session.execute(select(AppUser).where(AppUser.email == FOUNDER))
        ).scalar_one()
        assert user.is_staff is True
