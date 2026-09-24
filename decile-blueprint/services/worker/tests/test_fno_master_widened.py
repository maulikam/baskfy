"""FO2: the NFO master widened to every F&O underlying, and the O-sleeves still reading NIFTY only.

``docs/fno/06`` FO2: "``op_contract``'s nightly refresh widened from NIFTY to every underlying in
``instruments("NFO")``, **with the O-sleeves' reads still filtering to NIFTY** (their tests prove
it)." Asserted here, with BANKNIFTY and stock rows in the master beside NIFTY's:

* ``refresh_master_all`` writes every underlying's contracts and its own ``op_expiry`` calendar
  (a holiday-shifted BANKNIFTY monthly read from the master), keeps IDEA's 71,475 lot, refuses a
  dump with no NIFTY whole, marks a dropped underlying's contracts expired (never deleted), and a
  re-run of the same night changes nothing;
* **every O-sleeve read returns NIFTY rows only**: ``load_contracts`` (the collector and the
  health checks), ``scan.load_market`` (the scan and the O1/O2/O3 planners), the backtest's
  ``market_day``, and the API's ``op_expiry`` read behind the calendar page.

Database tests run in rolled-back transactions against ``BASKFY_TEST_DATABASE_URL``.
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

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from baskfy_api.options_read import _expiry_rows
from baskfy_core.models import OpContract, OpExpiry
from baskfy_core.options.config import OptionsConfig
from baskfy_providers.records import OptionContractRecord
from baskfy_worker.options.backtest_run import market_day
from baskfy_worker.options.master import EmptyMaster, load_contracts, refresh_master_all
from baskfy_worker.options.scan import load_market

ENV_VAR: Final = "BASKFY_TEST_DATABASE_URL"
API_DIR: Final = Path(__file__).resolve().parents[2] / "api"
#: A token base far from any real Kite token, and from test_options_master's.
BASE: Final = 8_000_000_000
AS_OF: Final = dt.date(2026, 9, 24)

NIFTY_EXPIRIES: Final = (dt.date(2026, 9, 29), dt.date(2026, 10, 6), dt.date(2026, 10, 27))
#: FO0 (b): BANKNIFTY's November monthly is a Monday, the Tuesday being a holiday.
BANKNIFTY_EXPIRIES: Final = (dt.date(2026, 10, 27), dt.date(2026, 11, 23))
STOCK_EXPIRY: Final = dt.date(2026, 10, 27)


def _records(
    underlying: str, expiries: tuple[dt.date, ...], *, lot: int, offset: int
) -> list[OptionContractRecord]:
    out: list[OptionContractRecord] = []
    for i, expiry in enumerate(expiries):
        for j, strike in enumerate((100, 200)):
            for k, kind in enumerate(("CE", "PE")):
                token = BASE + offset + i * 100 + j * 10 + k
                out.append(
                    OptionContractRecord(
                        instrument_token=token,
                        tradingsymbol=f"FO2{underlying}{token}{kind}",
                        underlying=underlying,
                        expiry=expiry,
                        strike=Decimal(strike),
                        option_type="CE" if kind == "CE" else "PE",
                        lot_size=lot,
                        tick_size=Decimal("0.05"),
                    )
                )
    return out


NIFTY: Final = _records("NIFTY", NIFTY_EXPIRIES, lot=65, offset=0)
BANKNIFTY: Final = _records("BANKNIFTY", BANKNIFTY_EXPIRIES, lot=30, offset=10_000)
IDEA: Final = _records("IDEA", (STOCK_EXPIRY,), lot=71_475, offset=20_000)
RELIANCE: Final = _records("RELIANCE", (STOCK_EXPIRY,), lot=500, offset=30_000)
EVERYTHING: Final = [*NIFTY, *BANKNIFTY, *IDEA, *RELIANCE]
NIFTY_TOKENS: Final = frozenset(r.instrument_token for r in NIFTY)
OTHER_TOKENS: Final = frozenset(r.instrument_token for r in EVERYTHING) - NIFTY_TOKENS


def _database_url() -> str | None:
    return os.environ.get(ENV_VAR)


requires_db = pytest.mark.db(
    pytest.mark.skipif(_database_url() is None, reason=f"{ENV_VAR} is not set")
)


@pytest.fixture(scope="module")
def fo_url() -> str:
    url = _database_url()
    if url is None:  # pragma: no cover - guarded by requires_db
        pytest.skip(f"{ENV_VAR} is not set")
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=API_DIR,
        env={
            **{k: v for k, v in os.environ.items() if k != "BASKFY_DATABASE_URL"},
            "BASKFY_DATABASE_URL": url,
        },
        capture_output=True,
        text=True,
        check=True,
    )
    return url


@asynccontextmanager
async def _widened(url: str) -> AsyncIterator[AsyncSession]:
    """A rolled-back session whose master holds NIFTY, BANKNIFTY, IDEA and RELIANCE."""
    engine = create_async_engine(url)
    session = async_sessionmaker(engine, expire_on_commit=False)()
    try:
        await refresh_master_all(session, EVERYTHING, as_of=AS_OF)
        yield session
    finally:
        await session.rollback()
        await session.close()
        await engine.dispose()


async def _underlyings(session: AsyncSession, tokens: frozenset[int]) -> dict[int, str]:
    rows = await session.execute(
        sa.select(OpContract.instrument_token, OpContract.underlying).where(
            OpContract.instrument_token.in_(tokens)
        )
    )
    return {int(token): str(name) for token, name in rows.all()}


@requires_db
class TestTheWidenedMaster:
    async def test_every_underlying_lands_with_its_own_calendar(self, fo_url: str) -> None:
        async with _widened(fo_url) as session:
            stored = await _underlyings(session, NIFTY_TOKENS | OTHER_TOKENS)
            assert set(stored.values()) == {"NIFTY", "BANKNIFTY", "IDEA", "RELIANCE"}
            assert len(stored) == len(EVERYTHING)
            idea = await session.get(OpContract, IDEA[0].instrument_token)
            assert idea is not None and idea.lot_size == 71_475, "a lot above a smallint"
            banknifty = {
                row.expiry_date: row.kind
                for row in (
                    await session.execute(
                        sa.select(OpExpiry).where(
                            OpExpiry.underlying == "BANKNIFTY",
                            OpExpiry.expiry_date.in_(BANKNIFTY_EXPIRIES),
                        )
                    )
                ).scalars()
            }
            # The Monday is November's monthly because it is the month's last master expiry.
            assert banknifty == {d: "MONTHLY" for d in BANKNIFTY_EXPIRIES}

    async def test_the_same_night_twice_changes_nothing(self, fo_url: str) -> None:
        async with _widened(fo_url) as session:
            again = await refresh_master_all(session, EVERYTHING, as_of=AS_OF)
            assert sum(r.contracts_new for r in again.by_underlying.values()) == 0
            assert all(not r.changes for r in again.by_underlying.values())
            assert again.delisted == []

    async def test_a_dump_without_nifty_is_refused_whole(self, fo_url: str) -> None:
        async with _widened(fo_url) as session:
            with pytest.raises(EmptyMaster):
                await refresh_master_all(
                    session, [*BANKNIFTY, *IDEA], as_of=AS_OF + dt.timedelta(days=1)
                )
            expired = (
                await session.execute(
                    sa.select(sa.func.count()).where(
                        OpContract.instrument_token.in_(NIFTY_TOKENS | OTHER_TOKENS),
                        OpContract.expired.is_(True),
                    )
                )
            ).scalar_one()
            assert expired == 0, "a refused dump expires nothing"

    async def test_an_underlying_dropped_from_fno_is_expired_never_deleted(
        self, fo_url: str
    ) -> None:
        async with _widened(fo_url) as session:
            tonight = [*NIFTY, *BANKNIFTY, *IDEA]  # RELIANCE gone from the dump
            reports = await refresh_master_all(session, tonight, as_of=AS_OF + dt.timedelta(1))
            assert "RELIANCE" in reports.delisted
            row = await session.get(OpContract, RELIANCE[0].instrument_token)
            assert row is not None and row.expired is True


@requires_db
class TestTheOSleevesStillReadNiftyOnly:
    async def test_load_contracts_the_collector_and_checks_read(self, fo_url: str) -> None:
        async with _widened(fo_url) as session:
            contracts = await load_contracts(session)
            assert contracts and {c.underlying for c in contracts} == {"NIFTY"}
            tokens = {c.instrument_token for c in contracts}
            assert tokens >= NIFTY_TOKENS and not tokens & OTHER_TOKENS

    async def test_the_scan_and_planners_market_day(self, fo_url: str) -> None:
        """``load_market`` is what ``scan``, ``plan`` (O1), ``plan_o2`` and ``plan_o3`` read."""
        async with _widened(fo_url) as session:
            market = await load_market(session, AS_OF, True, OptionsConfig())
            assert market.contracts and {c.underlying for c in market.contracts} == {"NIFTY"}
            assert not {c.instrument_token for c in market.contracts} & OTHER_TOKENS

    async def test_the_backtests_master_as_the_day_saw_it(self, fo_url: str) -> None:
        async with _widened(fo_url) as session:
            day = await market_day(session, AS_OF, frozenset(), OptionsConfig())
            assert day.contracts and {c.underlying for c in day.contracts} == {"NIFTY"}

    async def test_the_api_calendar_reads_niftys_expiries_only(self, fo_url: str) -> None:
        async with _widened(fo_url) as session:
            rows = await _expiry_rows(session, start=AS_OF)
            assert rows and {r.underlying for r in rows} == {"NIFTY"}
            assert set(NIFTY_EXPIRIES) <= {r.expiry_date for r in rows}
