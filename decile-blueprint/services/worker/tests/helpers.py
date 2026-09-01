"""Shared helpers for the pipeline tests.

Separate from ``conftest.py`` so test modules can import them: pytest gives test files no package,
so one test module cannot import another, and two ``conftest`` modules in different test trees
collide for mypy.
"""

from __future__ import annotations

import datetime as dt
import os
from decimal import Decimal
from typing import Final

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import Instrument, OhlcvDaily
from baskfy_core.seed_data import NSE_EXCHANGE_ID


class _Unset:
    """Distinguishes "caller said nothing" from "caller said None"."""


UNSET = _Unset()

ENV_VAR: Final = "BASKFY_TEST_DATABASE_URL"

requires_db = pytest.mark.skipif(
    os.environ.get(ENV_VAR) is None,
    reason=f"{ENV_VAR} is not set; run `make up` and export it",
)

#: A weekday inside the seeded calendar. docs/13's reference export is dated here.
#: Long before any test's trade date, deliberately. The first version of this defaulted to
#: `today`, which made every test instrument list AFTER the 18 Aug session the pipeline runs —
#: point-in-time membership then correctly excluded them and three acceptance tests failed with
#: "nifty-allcap has no members". A fixture must not manufacture a look-ahead (house rule 5).
#: D5's backfill floor, so it precedes anything a test can ask for.
DEFAULT_LISTED_ON: Final = dt.date(2011, 1, 1)

TRADE_DATE: Final = dt.date(2026, 8, 18)
PRIOR_DATE: Final = dt.date(2026, 8, 17)


def database_url() -> str:
    url = os.environ.get(ENV_VAR)
    if url is None:  # pragma: no cover - guarded by requires_db
        pytest.skip(f"{ENV_VAR} is not set")
    return url


async def make_instrument(
    session: AsyncSession,
    symbol: str,
    *,
    token: int | None = None,
    # `str | None`, because a seriesless instrument is a real thing: Kite's dump carries
    # 38,542 of them and M78 turns on telling them apart from cash names.
    series: str | None = "EQ",
    listed_on: dt.date | _Unset | None = UNSET,
) -> int:
    """A live NSE instrument.

    `listed_on` defaults to today because M78 made `active_instruments` skip instruments that have
    neither a bar nor a listing date — Kite's dump carries 31,603 such rows and asking about them
    cost three hours a night. A test instrument with no history is exactly a new listing, so that
    is what it is given. A sentinel, not `None`, so that passing `listed_on=None` explicitly
    means "no listing date" and builds an instrument the filter should skip — the two cases
    are different and collapsing them made the skip test pass a row dated today.
    """
    instrument = Instrument(
        exchange_id=NSE_EXCHANGE_ID,
        symbol=symbol,
        name=f"{symbol} LIMITED",
        series=series,
        instrument_type="EQ",
        kite_token=token,
        listed_on=DEFAULT_LISTED_ON if isinstance(listed_on, _Unset) else listed_on,
        is_active=True,
    )
    session.add(instrument)
    await session.flush()
    return instrument.id


async def add_bar(  # noqa: PLR0913 - one parameter per bar column a test may set
    session: AsyncSession,
    instrument_id: int,
    on: dt.date,
    close: str,
    *,
    volume: int = 1000,
    upper_circuit: str | None = None,
    lower_circuit: str | None = None,
) -> None:
    session.add(
        OhlcvDaily(
            instrument_id=instrument_id,
            date=on,
            open=Decimal(close),
            high=Decimal(close),
            low=Decimal(close),
            close=Decimal(close),
            volume=volume,
            close_raw=Decimal(close),
            volume_raw=volume,
            adj_factor=Decimal(1),
            upper_circuit=Decimal(upper_circuit) if upper_circuit else None,
            lower_circuit=Decimal(lower_circuit) if lower_circuit else None,
            source="nse",
        )
    )
    await session.flush()
