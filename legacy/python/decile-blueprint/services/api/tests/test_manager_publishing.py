"""``/managers/*`` — onboarding, review, publishing, and the dark revenue-share surface.

Asserted as a specification. The tests that matter most are the ones that would still fail if
somebody made the product friendlier in the wrong direction: a suspended manager regaining
publish rights, another manager's basket becoming addressable, or the revenue-share surface
answering while fee collection is off.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
from api_helpers import assert_problem, bearer, make_user, url
from screener_helpers import requires_db
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import AppUser, Base, CbBasket, CbManager
from baskfy_core.models.curated_baskets import CbManagerRevenueShare

pytestmark = [pytest.mark.db, pytest.mark.redis, requires_db]


@pytest.fixture
def session(screener_session: AsyncSession) -> AsyncSession:
    """Readable alias for the module-scoped seeded session the ``api`` fixture also uses.

    Both must be the same transaction: a row this test writes has to be visible to the request
    the client makes, and the ``api`` fixture overrides ``get_session`` with ``screener_session``.
    """
    return screener_session


APPLICATION = {"name": "Asha Rao", "bio": "Long-only quality.", "sebi_reg_type": "NONE"}


async def _account(session: AsyncSession, tag: str) -> tuple[int, dict[str, str]]:
    """An account with an address that cannot collide.

    ``make_user`` derives ``public_id`` from the local part with dots removed, truncated to 12
    characters — so two readable addresses like ``manager.one`` and ``manager.two`` both become
    ``managerone``/``managertwo`` only by luck, and longer ones collide outright on the unique
    index. Opaque tags avoid the trap entirely.
    """
    email = f"{tag}@example.com"
    user_id, public_id = await make_user(session, email)
    return user_id, bearer(public_id)


async def _staff(session: AsyncSession, tag: str) -> dict[str, str]:
    user_id, headers = await _account(session, tag)
    user = await session.get(AppUser, user_id)
    assert user is not None
    user.is_staff = True
    await session.flush()
    return headers


async def _apply(api: httpx.AsyncClient, headers: dict[str, str], **over: object) -> httpx.Response:
    return await api.post(url("/managers/me"), json={**APPLICATION, **over}, headers=headers)


async def _approve(api: httpx.AsyncClient, staff: dict[str, str], slug: str) -> httpx.Response:
    return await api.patch(url(f"/managers/{slug}"), json={"state": "APPROVED"}, headers=staff)


async def _basket_for(session: AsyncSession, user_id: int, slug: str) -> CbBasket:
    manager = (
        await session.execute(select(CbManager).where(CbManager.user_id == user_id))
    ).scalar_one()
    basket = CbBasket(
        slug=slug,
        name=slug,
        manager_id=manager.id,
        type="STOCK",
        access="FREE",
        visibility="PRIVATE",
        categories=[],
        rebalance_frequency="QUARTERLY",
        source="MANUAL",
    )
    session.add(basket)
    await session.flush()
    return basket


class TestTheSeededManagers:
    async def test_curated_seed_managers_are_approved_and_belong_to_nobody(
        self, session: AsyncSession
    ) -> None:
        """0020 backfilled the two seed rows rather than dropping them into DRAFT.

        They were publishing before a lifecycle existed; DRAFT would have un-published live
        baskets to satisfy a state machine that arrived afterwards. Neither is a person.
        """
        rows = (await session.execute(select(CbManager).order_by(CbManager.id))).scalars().all()
        seeded = [m for m in rows if m.slug in {"baskfy-engine", "maulik"}]
        assert len(seeded) == 2, "the two seed managers must still exist"
        for manager in seeded:
            assert manager.state == "APPROVED"
            assert manager.user_id is None
            assert manager.sebi_reg_type == "NONE"


class TestApplying:
    async def test_an_authenticated_user_can_apply(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _, headers = await _account(session, "mgrapply1")
        response = await _apply(api, headers)
        assert response.status_code == 201
        body = response.json()
        assert body["state"] == "SUBMITTED"
        assert body["may_publish"] is False
        assert body["kind"] == "EXTERNAL"

    async def test_a_fresh_application_may_not_publish_yet(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _, headers = await _account(session, "mgrapply2")
        assert (await _apply(api, headers)).json()["may_publish"] is False

    async def test_applying_twice_resubmits_rather_than_creating_a_second_identity(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        user_id, headers = await _account(session, "mgrapply3")
        await _apply(api, headers)
        await _apply(api, headers, name="Asha Rao II")
        rows = (
            (await session.execute(select(CbManager).where(CbManager.user_id == user_id)))
            .scalars()
            .all()
        )
        assert len(rows) == 1, "one account, one manager identity"
        assert rows[0].name == "Asha Rao II"

    async def test_a_malformed_registration_is_kept_for_review_not_refused(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """A shape we do not recognise must not stop somebody applying."""
        _, headers = await _account(session, "mgrapply4")
        response = await _apply(
            api, headers, sebi_reg_type="RESEARCH_ANALYST", sebi_reg_no="brand-new-shape"
        )
        assert response.status_code == 201
        assert response.json()["sebi_reg_type"] == "UNKNOWN"

    async def test_an_incoherent_registration_is_refused_with_a_sentence(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _, headers = await _account(session, "mgrapply5")
        response = await _apply(api, headers, sebi_reg_type="NONE", sebi_reg_no="INH000001234")
        assert_problem(response, 400, "invalid-screen-definition")

    async def test_a_well_formed_registration_is_stored_normalised_and_unverified(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _, headers = await _account(session, "mgrapply6")
        body = (
            await _apply(
                api, headers, sebi_reg_type="RESEARCH_ANALYST", sebi_reg_no=" inh 000001234 "
            )
        ).json()
        assert body["sebi_reg_no"] == "INH000001234"
        assert body["sebi_reg_verified_at"] is None
        assert "not verified by Baskfy" in body["registration_disclaimer"]


class TestReview:
    async def test_staff_can_approve_and_approval_grants_publish_rights(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _, headers = await _account(session, "mgrrev1")
        slug = (await _apply(api, headers)).json()["slug"]
        staff = await _staff(session, "mgrstaff1")
        body = (await _approve(api, staff, slug)).json()
        assert body["state"] == "APPROVED"
        assert body["may_publish"] is True

    async def test_a_non_staff_caller_cannot_review(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _, headers = await _account(session, "mgrrev2")
        slug = (await _apply(api, headers)).json()["slug"]
        response = await api.patch(
            url(f"/managers/{slug}"), json={"state": "APPROVED"}, headers=headers
        )
        assert response.status_code == 404, "a non-staff caller is told the surface is not there"

    async def test_a_suspended_manager_cannot_be_restored_in_one_step(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """The decision the state machine exists to hold. Not relaxable for convenience."""
        _, headers = await _account(session, "mgrrev3")
        slug = (await _apply(api, headers)).json()["slug"]
        staff = await _staff(session, "mgrstaff2")
        await _approve(api, staff, slug)
        await api.patch(url(f"/managers/{slug}"), json={"state": "SUSPENDED"}, headers=staff)
        refused = await api.patch(
            url(f"/managers/{slug}"), json={"state": "APPROVED"}, headers=staff
        )
        assert_problem(refused, 400, "invalid-screen-definition")
        assert "SUBMITTED" in refused.text, "the refusal must name the way back"

    async def test_reviewing_a_manager_that_does_not_exist_is_not_found(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        staff = await _staff(session, "mgrstaff3")
        response = await api.patch(
            url("/managers/no-such-manager"), json={"state": "APPROVED"}, headers=staff
        )
        assert response.status_code == 404


class TestPublishing:
    async def test_an_approved_manager_publishes_their_own_basket(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        user_id, headers = await _account(session, "mgrpub1")
        slug = (await _apply(api, headers)).json()["slug"]
        staff = await _staff(session, "mgrstaff4")
        await _approve(api, staff, slug)
        await _basket_for(session, user_id, "pub-own-1")
        response = await api.post(url("/managers/me/baskets/pub-own-1/publish"), headers=headers)
        assert response.status_code == 200
        assert response.json()["visibility"] == "PUBLISHED"
        assert response.json()["placed_an_order"] is False

    async def test_an_unapproved_manager_may_not_publish(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        user_id, headers = await _account(session, "mgrpub2")
        await _apply(api, headers)
        await _basket_for(session, user_id, "pub-unapproved")
        response = await api.post(
            url("/managers/me/baskets/pub-unapproved/publish"), headers=headers
        )
        assert_problem(response, 400, "invalid-screen-definition")

    async def test_a_suspended_manager_loses_publish_rights(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        user_id, headers = await _account(session, "mgrpub3")
        slug = (await _apply(api, headers)).json()["slug"]
        staff = await _staff(session, "mgrstaff5")
        await _approve(api, staff, slug)
        await _basket_for(session, user_id, "pub-suspended")
        await api.patch(url(f"/managers/{slug}"), json={"state": "SUSPENDED"}, headers=staff)
        response = await api.post(
            url("/managers/me/baskets/pub-suspended/publish"), headers=headers
        )
        assert_problem(response, 400, "invalid-screen-definition")

    async def test_a_suspended_manager_may_still_unpublish_their_own_listing(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """Refusing this would trap a basket in public view exactly when it should not be."""
        user_id, headers = await _account(session, "mgrpub4")
        slug = (await _apply(api, headers)).json()["slug"]
        staff = await _staff(session, "mgrstaff6")
        await _approve(api, staff, slug)
        await _basket_for(session, user_id, "pub-takedown")
        await api.post(url("/managers/me/baskets/pub-takedown/publish"), headers=headers)
        await api.patch(url(f"/managers/{slug}"), json={"state": "SUSPENDED"}, headers=staff)
        response = await api.delete(
            url("/managers/me/baskets/pub-takedown/publish"), headers=headers
        )
        assert response.status_code == 200
        assert response.json()["visibility"] == "PRIVATE"

    async def test_publishing_another_managers_basket_is_not_found(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """NOT_FOUND, never FORBIDDEN: the surface must not confirm the slug exists."""
        owner_id, owner_headers = await _account(session, "mgrpub5a")
        owner_slug = (await _apply(api, owner_headers)).json()["slug"]
        staff = await _staff(session, "mgrstaff7")
        await _approve(api, staff, owner_slug)
        await _basket_for(session, owner_id, "someone-elses-basket")

        _, other_headers = await _account(session, "mgrpub5b")
        other_slug = (await _apply(api, other_headers)).json()["slug"]
        await _approve(api, staff, other_slug)

        response = await api.post(
            url("/managers/me/baskets/someone-elses-basket/publish"), headers=other_headers
        )
        assert response.status_code == 404

    async def test_a_basket_that_does_not_exist_is_indistinguishable_from_one_you_cannot_see(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _, headers = await _account(session, "mgrpub6")
        slug = (await _apply(api, headers)).json()["slug"]
        staff = await _staff(session, "mgrstaff8")
        await _approve(api, staff, slug)
        missing = await api.post(
            url("/managers/me/baskets/no-such-basket/publish"), headers=headers
        )
        assert missing.status_code == 404

    async def test_a_caller_with_no_manager_identity_is_not_found(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        _, headers = await _account(session, "mgrpub7")
        assert (await api.get(url("/managers/me"), headers=headers)).status_code == 404


class TestRevenueShareIsDark:
    async def test_the_revenue_share_surface_404s_while_fee_collection_is_off(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """Track B stays dark. This is the flag's job, not the router's opinion."""
        _, headers = await _account(session, "mgrrev0")
        await _apply(api, headers)
        response = await api.get(url("/managers/me/revenue-share"), headers=headers)
        assert response.status_code == 404

    async def test_a_recorded_agreement_is_still_dark_while_the_flag_is_off(
        self, api: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        """Data existing must not open the surface — only the flag may."""
        user_id, headers = await _account(session, "mgrrev9")
        await _apply(api, headers)
        manager = (
            await session.execute(select(CbManager).where(CbManager.user_id == user_id))
        ).scalar_one()
        session.add(
            CbManagerRevenueShare(
                manager_id=manager.id, rate_bps=2000, effective_from=dt.date(2026, 1, 1)
            )
        )
        await session.flush()
        response = await api.get(url("/managers/me/revenue-share"), headers=headers)
        assert response.status_code == 404

    async def test_the_rate_column_has_no_default_so_no_amount_is_ever_invented(
        self, session: AsyncSession
    ) -> None:
        """D7 is human-track: a default rate is a guess that never appears in a diff again."""
        column = Base.metadata.tables["cb_manager_revenue_share"].c["rate_bps"]
        assert column.server_default is None
        assert column.nullable is False, "a row must state a rate, not omit one"
