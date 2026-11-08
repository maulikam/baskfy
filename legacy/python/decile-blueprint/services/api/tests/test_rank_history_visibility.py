"""``GET /instruments/{symbol}/rank-history`` follows the screen's visibility rule.

DECISIONS-RS R5.28 (30 Oct 2026): the route had no principal at all, so an anonymous caller who
knew a private screen's ``public_id`` could read what that screen ranked, date by date. The spec
is the one ``routers/screens.py`` states for every read of a screen: a system screen
(``user_id IS NULL``) is everyone's, including anonymous callers; a private screen is its owner's
only; and not-yours answers exactly as no-such-screen does (``404``), so the route does not
confirm that the id exists.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
from api_helpers import assert_problem, bearer, make_user, url
from screener_helpers import requires_db
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import Instrument, Screen, ScreenRun
from baskfy_core.seed_data import EXAMPLE_SCREENS

pytestmark = [pytest.mark.db, pytest.mark.redis, requires_db]

ON = dt.date(2026, 9, 11)


async def _stock(session: AsyncSession) -> Instrument:
    return (await session.scalars(select(Instrument).order_by(Instrument.id).limit(1))).one()


async def _ranked(session: AsyncSession, screen: Screen, stock: Instrument) -> None:
    session.add(
        ScreenRun(
            screen_id=screen.id,
            as_of=ON,
            definition_hash="h",
            result_count=2,
            results=[
                {"rank": 1, "instrument_id": 1_000_001, "factor_value": None},
                {"rank": 2, "instrument_id": stock.id, "factor_value": None},
            ],
        )
    )
    await session.flush()


async def _private_screen(session: AsyncSession, owner_id: int) -> Screen:
    template = (
        await session.scalars(
            select(Screen).where(Screen.public_id == EXAMPLE_SCREENS[0].public_id)
        )
    ).one()
    screen = Screen(
        public_id="0123456789abcdef01234567",
        user_id=owner_id,
        name="Mine only",
        definition=template.definition,
        columns=list(template.columns),
        is_example=False,
    )
    session.add(screen)
    await session.flush()
    return screen


async def test_a_private_screens_history_is_its_owners_only(
    api: httpx.AsyncClient, screener_session: AsyncSession
) -> None:
    owner_id, owner = await make_user(screener_session, "rank-owner@example.com")
    _, stranger = await make_user(screener_session, "rank-stranger@example.com")
    stock = await _stock(screener_session)
    screen = await _private_screen(screener_session, owner_id)
    await _ranked(screener_session, screen, stock)
    path = url(f"/instruments/{stock.symbol}/rank-history")
    params = {"screen": screen.public_id}

    mine = await api.get(path, params=params, headers=bearer(owner))
    assert mine.status_code == 200, mine.text
    assert [(p["as_of"], p["rank"]) for p in mine.json()["data"]] == [("2026-09-11", 2)]

    anonymous = await api.get(path, params=params)
    assert_problem(anonymous, 404, "not-found")
    another = await api.get(path, params=params, headers=bearer(stranger))
    assert_problem(another, 404, "not-found")

    # Not-yours is indistinguishable from no-such-screen.
    missing = await api.get(path, params={"screen": "ffffffffffffffffffffffff"})
    assert_problem(missing, 404, "not-found")
    assert anonymous.json()["detail"].replace(screen.public_id, "<id>") == missing.json()[
        "detail"
    ].replace("ffffffffffffffffffffffff", "<id>")


async def test_a_system_screens_history_stays_readable_by_anyone(
    api: httpx.AsyncClient, screener_session: AsyncSession
) -> None:
    _, someone = await make_user(screener_session, "rank-anyone@example.com")
    stock = await _stock(screener_session)
    template = (
        await screener_session.scalars(
            select(Screen).where(Screen.public_id == EXAMPLE_SCREENS[0].public_id)
        )
    ).one()
    await _ranked(screener_session, template, stock)
    path = url(f"/instruments/{stock.symbol}/rank-history")

    for headers in ({}, bearer(someone)):
        response = await api.get(path, params={"screen": template.public_id}, headers=headers)
        assert response.status_code == 200, response.text
        assert [p["rank"] for p in response.json()["data"]] == [2]
