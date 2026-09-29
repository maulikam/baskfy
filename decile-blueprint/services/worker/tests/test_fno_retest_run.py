"""FO9: the re-test's I/O — ``fo_contract_daily`` in, one ``fo_backtest_run`` row per family per
tenant out, with the tier, the caveat verbatim and the slippage source (``docs/fno/04`` §6).

The arithmetic is ``packages/core/tests/test_fno_retest.py``'s (and its golden's). These tests
assert what only the database can: the rows appended, the refusals, and that a sparse table (the
first quarter after a partial backfill) writes honest zero-trade rows instead of failing.
"""

from __future__ import annotations

import datetime as dt
import math
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

from baskfy_core.fno.retest import DIRECTIONAL_EXPIRY_DAYS, TIER_2E_CAVEAT, TIER_2E_CAVEAT_F3
from baskfy_core.models import AppUser, FoBacktestRun, FoContractDaily
from baskfy_worker.fno.partitions import ensure_contract_partition
from baskfy_worker.fno.retest import load_futures, load_index_rows, load_options, run_retest
from baskfy_worker.fno.scan import scan_users
from baskfy_worker.seeds.fno_config import seed_fno

ENV_VAR: Final = "BASKFY_TEST_DATABASE_URL"
API_DIR: Final = Path(__file__).resolve().parents[2] / "api"
#: Far from any real row: the retest reads 2022-01-03 onward up to ``end``.
START: Final = dt.date(2031, 1, 6)
EXPIRY: Final = dt.date(2031, 6, 26)
TODAY: Final = dt.date(2031, 7, 5)  # a Saturday in July: due


def _sessions(n: int) -> list[dt.date]:
    out: list[dt.date] = []
    day = START
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


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


async def _user(session: AsyncSession, tag: str) -> int:
    user = AppUser(public_id=f"fo9retest{tag}", email=f"fo9-{tag}@example.com", name="FO9")
    session.add(user)
    await session.flush()
    await seed_fno(session, user.id)
    return int(user.id)


async def _futures(session: AsyncSession, days: list[dt.date]) -> None:
    for i, day in enumerate(days):
        await ensure_contract_partition(session, day)
        for symbol, instrument, base in (("SBIN", "FUTSTK", 800.0), ("NIFTY", "FUTIDX", 23000.0)):
            price = Decimal(str(round(base * math.exp(0.002 * i + 0.01 * math.sin(i)), 2)))
            session.add(
                FoContractDaily(
                    trade_date=day,
                    instrument=instrument,
                    symbol=symbol,
                    expiry=EXPIRY,
                    strike=Decimal(0),
                    option_type="XX",
                    open=price,
                    high=price * Decimal("1.01"),
                    low=price * Decimal("0.99"),
                    close=price,
                    settle=price,
                    open_interest=1000 + i,
                    oi_change=1,
                    volume=100,
                    turnover=Decimal("10000000.00"),
                    lot_size=750 if symbol == "SBIN" else 65,
                    source_key="k",
                )
            )
    await session.flush()


async def _rows(session: AsyncSession, user_id: int) -> list[FoBacktestRun]:
    got = await session.execute(sa.select(FoBacktestRun).where(FoBacktestRun.user_id == user_id))
    return list(got.scalars())


async def test_one_row_per_family_per_tenant_with_its_tier_and_caveat(fo_url: str) -> None:
    async with _rolled_back(fo_url) as session:
        await session.execute(sa.delete(FoBacktestRun))
        users = [await _user(session, "a"), await _user(session, "b")]
        await _futures(session, _sessions(80))
        tenants = await scan_users(session)
        assert set(users) <= set(tenants)
        out = await run_retest(session, today=TODAY, families=["F2", "B4", "E"], end=TODAY)
        assert out["rows"] == 3 * len(tenants), out
        for user_id in users:
            rows = await _rows(session, user_id)
            assert sorted(r.family for r in rows) == ["B4", "E", "F2"]
            for row in rows:
                assert row.tier == "2E"
                assert row.caveat == TIER_2E_CAVEAT
                assert row.slippage_source == "ASSUMED"
                assert row.sample_from == START
            b4 = next(r for r in rows if r.family == "B4")
            assert b4.n == 0, "no option rows: an honest zero, not a failure"
            assert b4.net_r is None, "no trades is no expectancy, not 0.0R"


async def test_a_second_run_in_the_month_is_refused_unless_forced(fo_url: str) -> None:
    async with _rolled_back(fo_url) as session:
        await session.execute(sa.delete(FoBacktestRun))
        user = await _user(session, "c")
        await _futures(session, _sessions(30))
        await run_retest(session, today=TODAY, families=["E"], end=TODAY)
        await session.execute(
            sa.update(FoBacktestRun)
            .where(FoBacktestRun.user_id == user)
            .values(run_at=dt.datetime(2031, 7, 5, 6, tzinfo=dt.UTC))
        )
        again = await run_retest(session, today=TODAY + dt.timedelta(days=7), families=["E"])
        assert "not due" in str(again["skipped"])
        forced = await run_retest(
            session, today=TODAY + dt.timedelta(days=7), families=["E"], force=True
        )
        assert forced["rows"] == len(await scan_users(session))
        assert len([r for r in await _rows(session, user) if r.family == "E"]) == 2


async def test_outside_the_quarter_months_nothing_runs(fo_url: str) -> None:
    async with _rolled_back(fo_url) as session:
        await session.execute(sa.delete(FoBacktestRun))
        out = await run_retest(session, today=dt.date(2031, 8, 2))
        assert "not due" in str(out["skipped"])


async def test_an_unknown_family_is_refused_by_name(fo_url: str) -> None:
    async with _rolled_back(fo_url) as session:
        out = await run_retest(session, today=TODAY, families=["B9"], force=True)
        assert "B9" in str(out["refused"])


async def _index_option(
    session: AsyncSession, day: dt.date, expiry: dt.date, strike: int, kind: str
) -> None:
    await ensure_contract_partition(session, day)
    session.add(
        FoContractDaily(
            trade_date=day,
            instrument="OPTIDX",
            symbol="NIFTY",
            expiry=expiry,
            strike=Decimal(strike),
            option_type=kind,
            open=Decimal("20.00"),
            high=Decimal("20.00"),
            low=Decimal("20.00"),
            close=Decimal("20.00"),
            settle=Decimal("20.00"),
            open_interest=500,
            oi_change=1,
            volume=50,
            turnover=Decimal("100000.00"),
            lot_size=65,
            source_key="k",
        )
    )


async def test_f3_reads_the_weeklies_the_option_panel_drops(fo_url: str) -> None:
    """F3-7: NIFTY's weekly has no future, so the research panel drops it; F3's loader keeps it
    (and trims an expiry beyond ``DIRECTIONAL_EXPIRY_DAYS``)."""
    async with _rolled_back(fo_url) as session:
        days = _sessions(10)
        await _futures(session, days)
        weekly = days[-1] + dt.timedelta(days=4)  # no FUTIDX at this expiry
        far = days[-1] + dt.timedelta(days=DIRECTIONAL_EXPIRY_DAYS + 30)
        for expiry in (weekly, far):
            await _index_option(session, days[-1], expiry, 23000, "PE")
        await session.flush()
        futures = await load_futures(session, START, TODAY)
        panel = await load_options(session, futures, frozenset({"NIFTY"}), START, TODAY)
        assert weekly not in set(panel["expiry"].to_list()), "the research panel drops weeklies"
        raw = await load_index_rows(session, futures, "NIFTY", START, TODAY)
        opts = raw.filter(raw["instrument"] == "OPTIDX")
        assert set(opts["expiry"].to_list()) == {weekly}
        assert raw.filter(raw["instrument"] == "FUTIDX").height == len(days)


async def test_f3_families_write_rows_with_their_own_caveat(fo_url: str) -> None:
    async with _rolled_back(fo_url) as session:
        await session.execute(sa.delete(FoBacktestRun))
        user = await _user(session, "f3")
        await _futures(session, _sessions(80))
        out = await run_retest(session, today=TODAY, families=["F3N", "F3B"], end=TODAY)
        assert out["rows"] == 2 * len(await scan_users(session)), out
        rows = {r.family: r for r in await _rows(session, user)}
        assert set(rows) == {"F3N", "F3B"}
        for row in rows.values():
            assert row.caveat == TIER_2E_CAVEAT_F3
            assert row.slippage_source == "ASSUMED"
            assert row.n == 0 and row.net_r is None, "no index options: an honest zero"
        assert rows["F3N"].params["expiry_kind"] == "weekly"
        assert rows["F3B"].params["expiry_kind"] == "monthly"
