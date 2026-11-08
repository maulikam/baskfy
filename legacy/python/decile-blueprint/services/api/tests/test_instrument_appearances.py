"""Where a stock appears, read from stored results only (Maulik, 14 Sep 2026).

"When I open any stock, if it is in any screens or strategy came in results or scan, do mention
that" — and "persist it and reuse it based on the last date result". So: the newest stored
``screen_run`` per screen, and each strategy's own table at its latest session. Nothing re-runs.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import httpx
import pytest
from api_helpers import bearer, make_user, url
from screener_helpers import requires_db
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.instrument_appearances import AppearanceKind, appearances
from baskfy_core.models import Instrument, Screen, ScreenRun, VbBreadthDaily, VbSignalDaily
from baskfy_core.seed_data import EXAMPLE_SCREENS

pytestmark = [pytest.mark.db, pytest.mark.redis, requires_db]

OLDER = dt.date(2026, 9, 10)
NEWER = dt.date(2026, 9, 11)


async def _instrument(session: AsyncSession) -> Instrument:
    return (await session.scalars(select(Instrument).order_by(Instrument.id).limit(1))).one()


async def _screen(session: AsyncSession, public_id: str) -> Screen:
    return (await session.scalars(select(Screen).where(Screen.public_id == public_id))).one()


async def _run(
    session: AsyncSession, screen: Screen, on: dt.date, ids: list[int], *, hash_: str
) -> None:
    session.add(
        ScreenRun(
            screen_id=screen.id,
            as_of=on,
            definition_hash=hash_,
            result_count=len(ids),
            results=[
                {"rank": i + 1, "instrument_id": iid, "factor_value": None}
                for i, iid in enumerate(ids)
            ],
        )
    )
    await session.flush()


async def test_the_newest_stored_run_decides_and_nothing_is_rerun(
    screener_session: AsyncSession,
) -> None:
    from baskfy_core.screen_definition import ScreenDefinition  # noqa: PLC0415

    user_id, _ = await make_user(screener_session, "appear@example.com")
    stock = await _instrument(screener_session)
    investing = await _screen(screener_session, EXAMPLE_SCREENS[0].public_id)
    current = ScreenDefinition.model_validate(investing.definition).definition_hash()

    # Older session: the stock was 1st. Newest session: it is 3rd of 4. The newest wins.
    await _run(screener_session, investing, OLDER, [stock.id], hash_=current)
    await _run(
        screener_session,
        investing,
        NEWER,
        [1_000_001, 1_000_002, stock.id, 1_000_003],
        hash_=current,
    )
    # A second template whose newest run was of an earlier definition.
    second = await _screen(screener_session, EXAMPLE_SCREENS[1].public_id)
    await _run(screener_session, second, NEWER, [stock.id], hash_="an-older-definition")

    view = await appearances(
        screener_session, user_id=user_id, instrument_id=stock.id, strategies_user_id=None
    )

    by_name = {item.name: item for item in view.appearances}
    first = by_name[investing.name]
    assert (first.kind, first.as_of, first.rank, first.of) == (
        AppearanceKind.TEMPLATE,
        NEWER,
        3,
        4,
    )
    assert first.definition_changed is False
    assert by_name[second.name].definition_changed is True
    assert view.screens_checked == len(EXAMPLE_SCREENS)
    assert set(view.screens_never_run) == {s.name for s in EXAMPLE_SCREENS[2:]}
    assert view.strategies_checked == ()


async def test_a_strategy_is_read_at_its_own_latest_session_only(
    screener_session: AsyncSession,
) -> None:
    """A volume-breakout signal from the session before the latest is not "in the scan" now."""
    user_id, _ = await make_user(screener_session, "appear-vbt@example.com")
    stock = await _instrument(screener_session)
    for day in (OLDER, NEWER):
        screener_session.add(
            VbBreadthDaily(
                user_id=user_id,
                date=day,
                universe_count=10,
                measured_count=10,
                above_count=5,
                pct_above_dma=Decimal("50"),
                gate="OPEN",
            )
        )
    screener_session.add(
        VbSignalDaily(
            user_id=user_id,
            date=OLDER,
            instrument_id=stock.id,
            state="SIGNAL",
            close=Decimal("100"),
            close_raw=Decimal("100"),
            limit_price=Decimal("101"),
            stop_price=Decimal("95"),
        )
    )
    await screener_session.flush()

    stale = await appearances(
        screener_session, user_id=user_id, instrument_id=stock.id, strategies_user_id=user_id
    )
    assert not [a for a in stale.appearances if a.kind is AppearanceKind.VOLUME_BREAKOUT]

    screener_session.add(
        VbSignalDaily(
            user_id=user_id,
            date=NEWER,
            instrument_id=stock.id,
            state="SIGNAL",
            close=Decimal("100"),
            close_raw=Decimal("100"),
            limit_price=Decimal("101"),
            stop_price=Decimal("95"),
        )
    )
    await screener_session.flush()
    fresh = await appearances(
        screener_session, user_id=user_id, instrument_id=stock.id, strategies_user_id=user_id
    )
    [breakout] = [a for a in fresh.appearances if a.kind is AppearanceKind.VOLUME_BREAKOUT]
    assert (breakout.as_of, breakout.detail, breakout.ref) == (NEWER, "signal", "/vbt")
    assert fresh.strategies_checked == ("Swing", "Volume breakout", "Three weeks tight")


async def test_the_route_needs_a_signed_in_caller(
    api: httpx.AsyncClient, screener_session: AsyncSession
) -> None:
    stock = await _instrument(screener_session)
    anonymous = await api.get(url(f"/instruments/{stock.symbol}/appearances"))
    assert anonymous.status_code == 401

    _, public_id = await make_user(screener_session, "appear-route@example.com")
    signed = await api.get(
        url(f"/instruments/{stock.symbol}/appearances"), headers=bearer(public_id)
    )
    assert signed.status_code == 200, signed.text
    assert signed.json()["symbol"] == stock.symbol
