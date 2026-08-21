"""Shared setup for the Prompt 20 alert and webhook suites.

Its own module, like ``api_helpers`` and ``billing_helpers``, because pytest gives test files no
package: one test module cannot import another, and the alert dispatch is exercised from both
``test_api_alerts.py`` (the email) and ``test_api_webhooks.py`` (the fan-out).
"""

from __future__ import annotations

import datetime as dt
from typing import Final

import api_helpers
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from decile_api.email import EmailNotSent, Message
from decile_api.settings import Settings
from decile_core.models import Instrument, Screen, ScreenRun

#: Two consecutive trade dates inside the seeded reference export's range.
PREVIOUS: Final = dt.date(2026, 8, 17)
CURRENT: Final = dt.date(2026, 8, 18)

#: A stand-in for a real definition hash. The value never matters; the *equality* between two runs
#: does — a mismatch is what `resolve_runs` treats as "the user edited the screen".
DEFINITION_HASH: Final = "0" * 64


class Recorder:
    """A transport that keeps what it was handed. ``fail`` makes every send bounce."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[Message] = []
        self._fail = fail

    async def send(self, message: Message) -> None:
        if self._fail:
            raise EmailNotSent("transport refused")
        self.sent.append(message)


def settings_for(url: str = "postgresql+asyncpg://unused/unused", **overrides: object) -> Settings:
    """Test settings with a webhook master secret, so signing and unsubscribe links both work."""
    return api_helpers.api_settings(
        url,
        web_origin="http://localhost:3000",
        webhook_signing_secret="a-master-secret-for-tests-only",
        **overrides,
    )


async def make_screen(
    session: AsyncSession, user_id: int, public_id: str, name: str = "Momentum"
) -> Screen:
    screen = Screen(
        public_id=public_id,
        user_id=user_id,
        name=name,
        definition={"index": "nifty-total-market", "sort_by": "avg_sharpe_12_6_3_1"},
        columns=[],
        is_example=False,
    )
    session.add(screen)
    await session.flush()
    return screen


async def make_run(
    session: AsyncSession,
    screen: Screen,
    as_of: dt.date,
    ranks: list[tuple[int, int]],
    *,
    definition_hash: str = DEFINITION_HASH,
) -> ScreenRun:
    """``ranks`` is ``[(rank, instrument_id)]`` — docs/04's ``screen_run.results`` shape."""
    run = ScreenRun(
        screen_id=screen.id,
        as_of=as_of,
        definition_hash=definition_hash,
        result_count=len(ranks),
        results=[
            {"rank": rank, "instrument_id": instrument, "factor_value": "1.00"}
            for rank, instrument in ranks
        ],
    )
    session.add(run)
    await session.flush()
    return run


async def instrument_ids(session: AsyncSession, count: int) -> list[int]:
    """Real instrument ids from the seeded reference export, so an email names real symbols."""
    rows = (
        (await session.execute(select(Instrument.id).order_by(Instrument.id).limit(count)))
        .scalars()
        .all()
    )
    assert len(rows) == count, "the seeded export should hold at least this many instruments"
    return list(rows)
