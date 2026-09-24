"""FO2: the ``fo_underlying_daily`` seam — read ``fo_contract_daily``, derive, upsert.

The tests below drive :func:`derive_underlying_daily` with a stand-in for the pure function
(FO1's arithmetic is ``packages/core/tests/test_fno_underlying.py``'s). What they assert is the
seam's side of the contract: the window read ends at ``trade_date`` (no look-ahead,
house rule 5), the upsert is idempotent (house rule 7), and it never touches ``in_ban``.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Final

import polars as pl
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from baskfy_core.models import FoContractDaily, FoUnderlyingDaily
from baskfy_worker.fno.partitions import ensure_contract_partition
from baskfy_worker.fno.underlying import DERIVED_COLUMNS, derive_underlying_daily

ENV_VAR: Final = "BASKFY_TEST_DATABASE_URL"
API_DIR: Final = Path(__file__).resolve().parents[2] / "api"
DAY: Final = dt.date(2030, 1, 2)
TOMORROW: Final = dt.date(2030, 1, 3)


def _stand_in(window: pl.DataFrame, trade_date: dt.date) -> pl.DataFrame:
    """Records the window it was given; returns one row per symbol with the derived columns."""
    assert window.get_column("trade_date").max() == trade_date, "no row after trade_date"
    symbols = sorted(set(window.get_column("symbol").to_list()))
    return pl.DataFrame(
        [
            {"symbol": s, **dict.fromkeys(DERIVED_COLUMNS), "ca_flag": False, "level_c": 100.0}
            for s in symbols
        ]
    )


@pytest.fixture(scope="module")
def fo_url() -> str:
    url = os.environ.get(ENV_VAR)
    if url is None:  # pragma: no cover
        pytest.skip(f"{ENV_VAR} is not set")
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=API_DIR,
        env={**os.environ, "BASKFY_DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=True,
    )
    return url


@asynccontextmanager
async def _rolled_back(url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(url)
    session = async_sessionmaker(engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()
        await engine.dispose()


async def _contract(session: AsyncSession, day: dt.date, symbol: str) -> None:
    await ensure_contract_partition(session, day)
    session.add(
        FoContractDaily(
            trade_date=day,
            instrument="FUTSTK",
            symbol=symbol,
            expiry=dt.date(2030, 1, 29),
            strike=Decimal(0),
            option_type="XX",
            close=Decimal(100),
            settle=Decimal(100),
            source_key="k",
        )
    )
    await session.flush()


async def test_derive_reads_no_later_row_keeps_in_ban_and_is_idempotent(fo_url: str) -> None:
    async with _rolled_back(fo_url) as session:
        await _contract(session, DAY, "SBIN")
        await _contract(session, TOMORROW, "SBIN")  # must not reach DAY's derivation
        session.add(FoUnderlyingDaily(trade_date=DAY, symbol="SBIN", in_ban=True))
        await session.flush()
        assert await derive_underlying_daily(session, DAY, _stand_in) == 1
        assert await derive_underlying_daily(session, DAY, _stand_in) == 1
        session.expire_all()
        row = await session.get(FoUnderlyingDaily, (DAY, "SBIN"))
        assert row is not None and row.in_ban is True, "the derivation never touches in_ban"
        count = (
            await session.execute(
                sa.select(sa.func.count()).where(FoUnderlyingDaily.trade_date == DAY)
            )
        ).scalar_one()
        assert count == 1
