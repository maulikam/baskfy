from __future__ import annotations

import api_helpers
import pytest
from api_helpers import bearer, make_user, running_app, url
from screener_helpers import requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import opportunities_agui as agui
from baskfy_api.settings import Settings

pytestmark = [requires_db, pytest.mark.db]

SCAN = {"universe": "nifty-500"}
OPTIONS = {"symbol": "INFY", "direction": "BULLISH"}


def _run(thread: str) -> dict[str, object]:
    return {
        "threadId": thread,
        "runId": "r1",
        "messages": [{"id": "m1", "role": "user", "content": "hello"}],
    }


@pytest.fixture
def settings(seeded_url: str) -> Settings:
    return api_helpers.api_settings(seeded_url)


async def test_every_route_refuses_an_anonymous_caller(
    settings: Settings, screener_session: AsyncSession
) -> None:
    async with running_app(settings, screener_session) as client:
        responses = [
            await client.get(url("/opportunities/meta")),
            await client.post(url("/opportunities/swing/scan"), json=SCAN),
            await client.post(url("/opportunities/swing/scan/stream"), json=SCAN),
            await client.post(url("/opportunities/options/analyze"), json=OPTIONS),
            await client.post(url("/opportunities/agui"), json=_run("anon")),
        ]
    assert [r.status_code for r in responses] == [401] * len(responses)


async def test_options_refuse_a_principal_who_is_not_the_broker_owner(
    settings: Settings, screener_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, _ = await make_user(screener_session, "opps-owner@example.com")
    monkeypatch.setenv("BASKFY_SOLE_USER_ID", str(owner))
    _, intruder = await make_user(screener_session, "opps-intruder@example.com")
    async with running_app(settings, screener_session) as client:
        response = await client.post(
            url("/opportunities/options/analyze"), json=OPTIONS, headers=bearer(intruder)
        )
    assert response.status_code == 404, response.text


async def test_a_conversation_cannot_be_continued_by_another_user(
    settings: Settings, screener_session: AsyncSession
) -> None:
    agui.reset_threads()
    _, alice = await make_user(screener_session, "opps-alice@example.com")
    _, bob = await make_user(screener_session, "opps-bob@example.com")
    async with running_app(settings, screener_session) as client:
        first = await client.post(
            url("/opportunities/agui"), json=_run("shared-thread"), headers=bearer(alice)
        )
        second = await client.post(
            url("/opportunities/agui"), json=_run("shared-thread"), headers=bearer(bob)
        )
    assert first.status_code == 200
    assert first.headers["content-type"].startswith("text/event-stream")
    assert '"RUN_STARTED"' in first.text
    assert second.status_code == 404


async def test_meta_lists_parameters_and_the_provider_state(
    settings: Settings, screener_session: AsyncSession
) -> None:
    _, user = await make_user(screener_session, "opps-meta@example.com")
    async with running_app(settings, screener_session) as client:
        response = await client.get(url("/opportunities/meta"), headers=bearer(user))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["setups"] == ["BREAKOUT", "PULLBACK", "RELATIVE_STRENGTH"]
    assert set(body["options_provider"]) == {"available", "reason", "dependency"}
