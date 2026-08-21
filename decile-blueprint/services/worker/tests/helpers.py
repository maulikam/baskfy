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

from decile_core.models import Instrument, OhlcvDaily
from decile_core.seed_data import NSE_EXCHANGE_ID

ENV_VAR: Final = "DECILE_TEST_DATABASE_URL"

requires_db = pytest.mark.skipif(
    os.environ.get(ENV_VAR) is None,
    reason=f"{ENV_VAR} is not set; run `make up` and export it",
)

#: A weekday inside the seeded calendar. docs/13's reference export is dated here.
TRADE_DATE: Final = dt.date(2026, 8, 18)
PRIOR_DATE: Final = dt.date(2026, 8, 17)


def database_url() -> str:
    url = os.environ.get(ENV_VAR)
    if url is None:  # pragma: no cover - guarded by requires_db
        pytest.skip(f"{ENV_VAR} is not set")
    return url


async def make_instrument(
    session: AsyncSession, symbol: str, *, token: int | None = None, series: str = "EQ"
) -> int:
    instrument = Instrument(
        exchange_id=NSE_EXCHANGE_ID,
        symbol=symbol,
        name=f"{symbol} LIMITED",
        series=series,
        instrument_type="EQ",
        kite_token=token,
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
