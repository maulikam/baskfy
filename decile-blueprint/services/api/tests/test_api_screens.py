"""Contract tests for ``/screens`` CRUD — docs/07 §Screens (Prompt 7 acceptance criterion 1)."""

from __future__ import annotations

import httpx
import pytest
from api_helpers import assert_problem, bearer, errors_of, make_user, url
from screener_helpers import requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from decile_core.seed_data import EXAMPLE_SCREENS

pytestmark = [pytest.mark.db, pytest.mark.redis, requires_db]

EXAMPLE_ID = EXAMPLE_SCREENS[0].public_id

MINIMAL = {"index": "nifty-500", "sort_by": "ret_12m"}


async def owner(session: AsyncSession, email: str = "owner@example.com") -> dict[str, str]:
    _, public_id = await make_user(session, email)
    return bearer(public_id)


class TestListing:
    async def test_anonymous_sees_only_the_example_screens(self, api: httpx.AsyncClient) -> None:
        body = (await api.get(url("/screens"))).json()
        assert len(body["data"]) == len(EXAMPLE_SCREENS)
        assert all(row["is_example"] for row in body["data"])
        assert all(row["editable"] is False for row in body["data"])

    async def test_a_user_sees_examples_plus_their_own(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """docs/07: "GET /screens -> user screens + example screens"."""
        headers = await owner(screener_session)
        created = await api.post(
            url("/screens"), json={"name": "Mine", "definition": MINIMAL}, headers=headers
        )
        assert created.status_code == 201

        body = (await api.get(url("/screens"), headers=headers)).json()
        names = {row["name"] for row in body["data"]}
        assert "Mine" in names
        assert "Investing 001" in names
        mine = next(row for row in body["data"] if row["name"] == "Mine")
        assert mine["editable"] is True

    async def test_one_user_never_sees_another_users_screens(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        alice = await owner(screener_session, "alice@example.com")
        bob = await owner(screener_session, "bob@example.com")
        await api.post(
            url("/screens"), json={"name": "Alice only", "definition": MINIMAL}, headers=alice
        )
        body = (await api.get(url("/screens"), headers=bob)).json()
        assert "Alice only" not in {row["name"] for row in body["data"]}


class TestCreate:
    async def test_it_requires_authentication(self, api: httpx.AsyncClient) -> None:
        response = await api.post(url("/screens"), json={"name": "x", "definition": MINIMAL})
        body = assert_problem(response, 401, "unauthenticated")
        assert body["title"] == "Authentication required"
        assert response.headers["www-authenticate"] == "Bearer"

    async def test_it_returns_the_created_screen(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await owner(screener_session)
        response = await api.post(
            url("/screens"),
            json={"name": "Momentum", "definition": MINIMAL, "columns": ["ret_12m", "beta_12m"]},
            headers=headers,
        )
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "Momentum"
        assert body["definition"]["sort_by"] == "ret_12m"
        assert body["columns"] == ["ret_12m", "beta_12m"]
        assert body["is_example"] is False
        assert body["editable"] is True
        assert len(body["public_id"]) == 12

    async def test_an_unknown_factor_key_is_a_400_with_field_paths(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """docs/07: 400 `invalid-screen-definition`, with `errors[]` listing field paths."""
        headers = await owner(screener_session)
        response = await api.post(
            url("/screens"),
            json={"name": "Bad", "definition": {"index": "nifty-500", "sort_by": "nope"}},
            headers=headers,
        )
        body = assert_problem(response, 400, "invalid-screen-definition")
        assert any(error["field"] == "definition.sort_by" for error in errors_of(body)), body

    async def test_an_unknown_key_in_the_definition_is_rejected(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """docs/07 §Screens: "Unknown keys are rejected (`extra="forbid"`)"."""
        headers = await owner(screener_session)
        response = await api.post(
            url("/screens"),
            json={"name": "Bad", "definition": {**MINIMAL, "not_a_field": 1}},
            headers=headers,
        )
        assert_problem(response, 400, "invalid-screen-definition")

    async def test_an_unknown_column_is_rejected(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await owner(screener_session)
        response = await api.post(
            url("/screens"),
            json={"name": "Bad", "definition": MINIMAL, "columns": ["not_a_column"]},
            headers=headers,
        )
        assert_problem(response, 400, "invalid-screen-definition")

    async def test_an_idempotency_key_creates_one_screen(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """docs/07 §Conventions: "`Idempotency-Key` header honoured on all POSTs that create"."""
        headers = {**await owner(screener_session), "Idempotency-Key": "abc-123"}
        first = await api.post(
            url("/screens"), json={"name": "Once", "definition": MINIMAL}, headers=headers
        )
        second = await api.post(
            url("/screens"), json={"name": "Once", "definition": MINIMAL}, headers=headers
        )
        assert first.status_code == 201
        assert second.json()["public_id"] == first.json()["public_id"]

        listing = (await api.get(url("/screens"), headers=headers)).json()
        assert [row["name"] for row in listing["data"]].count("Once") == 1


class TestRead:
    async def test_an_example_screen_is_readable_anonymously(self, api: httpx.AsyncClient) -> None:
        body = (await api.get(url(f"/screens/{EXAMPLE_ID}"))).json()
        assert body["name"] == "Investing 001"
        assert body["editable"] is False

    async def test_an_unknown_id_is_404(self, api: httpx.AsyncClient) -> None:
        assert_problem(await api.get(url("/screens/doesnotexist")), 404, "not-found")

    async def test_another_users_screen_is_404_not_403(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """A 403 would confirm the id exists. A 404 tells the caller nothing they did not know."""
        alice = await owner(screener_session, "alice2@example.com")
        bob = await owner(screener_session, "bob2@example.com")
        created = await api.post(
            url("/screens"), json={"name": "Hers", "definition": MINIMAL}, headers=alice
        )
        public_id = created.json()["public_id"]
        assert_problem(await api.get(url(f"/screens/{public_id}"), headers=bob), 404, "not-found")


class TestUpdate:
    async def test_it_patches_what_was_sent_and_leaves_the_rest(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await owner(screener_session)
        created = (
            await api.post(
                url("/screens"),
                json={"name": "Before", "definition": MINIMAL, "columns": ["ret_12m"]},
                headers=headers,
            )
        ).json()

        response = await api.patch(
            url(f"/screens/{created['public_id']}"), json={"name": "After"}, headers=headers
        )
        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "After"
        assert body["columns"] == ["ret_12m"]
        assert body["definition"]["sort_by"] == "ret_12m"

    async def test_an_example_screen_cannot_be_edited(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        """docs/01 §1: the six templates are read-only. Duplicate is the supported path."""
        headers = await owner(screener_session)
        response = await api.patch(
            url(f"/screens/{EXAMPLE_ID}"), json={"name": "Hijacked"}, headers=headers
        )
        body = assert_problem(response, 404, "not-found")
        assert "duplicate" in str(body["detail"]).lower()

    async def test_editing_needs_authentication(self, api: httpx.AsyncClient) -> None:
        assert_problem(
            await api.patch(url(f"/screens/{EXAMPLE_ID}"), json={"name": "x"}),
            401,
            "unauthenticated",
        )


class TestDelete:
    async def test_it_removes_the_screen(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await owner(screener_session)
        created = (
            await api.post(
                url("/screens"), json={"name": "Doomed", "definition": MINIMAL}, headers=headers
            )
        ).json()
        response = await api.delete(url(f"/screens/{created['public_id']}"), headers=headers)
        assert response.status_code == 204
        assert response.content == b""
        assert_problem(
            await api.get(url(f"/screens/{created['public_id']}"), headers=headers),
            404,
            "not-found",
        )

    async def test_an_example_screen_cannot_be_deleted(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await owner(screener_session)
        assert_problem(
            await api.delete(url(f"/screens/{EXAMPLE_ID}"), headers=headers), 404, "not-found"
        )


class TestDuplicate:
    async def test_it_copies_an_example_into_an_editable_screen(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await owner(screener_session)
        response = await api.post(url(f"/screens/{EXAMPLE_ID}/duplicate"), headers=headers)
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "Investing 001 (copy)"
        assert body["editable"] is True
        assert body["is_example"] is False
        assert body["public_id"] != EXAMPLE_ID

        original = (await api.get(url(f"/screens/{EXAMPLE_ID}"))).json()
        assert body["definition"] == original["definition"]
        assert body["columns"] == original["columns"]

    async def test_a_name_can_be_supplied(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = await owner(screener_session)
        response = await api.post(
            url(f"/screens/{EXAMPLE_ID}/duplicate"), json={"name": "My take"}, headers=headers
        )
        assert response.json()["name"] == "My take"

    async def test_it_is_idempotent(
        self, api: httpx.AsyncClient, screener_session: AsyncSession
    ) -> None:
        headers = {**await owner(screener_session), "Idempotency-Key": "dup-1"}
        first = await api.post(url(f"/screens/{EXAMPLE_ID}/duplicate"), headers=headers)
        second = await api.post(url(f"/screens/{EXAMPLE_ID}/duplicate"), headers=headers)
        assert first.json()["public_id"] == second.json()["public_id"]

    async def test_it_requires_authentication(self, api: httpx.AsyncClient) -> None:
        assert_problem(
            await api.post(url(f"/screens/{EXAMPLE_ID}/duplicate")), 401, "unauthenticated"
        )
