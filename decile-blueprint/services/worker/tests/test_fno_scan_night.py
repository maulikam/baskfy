"""FO4: the nightly scan writes ``fo_scan`` for the next session (``docs/fno/04`` §8, §10).

Against the test Postgres, inside a transaction that is rolled back. Every fixture bhavcopy day
is built here: a calendar of weekdays, a NIFTY monthly whose entry session (15 sessions before
it, ``04`` §1) is the session after the scanned one, ~90 sessions of futures for NIFTY,
BANKNIFTY and nine stock underlyings, and NIFTY's option chain on the scanned session priced by
Black-76 at 15 % so the ATM IV solves back to it. Dates sit in 2030 so nothing collides with
committed rows.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Final

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from baskfy_core.fno.config import ScanState
from baskfy_core.models import (
    AppUser,
    Exchange,
    FoContractDaily,
    FoIngestDay,
    FoJournal,
    FoPosition,
    FoScan,
    FoSleeveConfig,
    IndexDef,
    IndexMemberDaily,
    Instrument,
    OpContract,
    OpEventDay,
    OpExpiry,
    TradingDay,
)
from baskfy_core.options.config import OptionType
from baskfy_core.options.greeks import black76_price
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_worker import fno_cli
from baskfy_worker.fno import nightly
from baskfy_worker.fno.partitions import ensure_contract_partition
from baskfy_worker.fno.scan import SLEEVE_ROW, run_scan
from baskfy_worker.seeds.fno_config import seed_fno
from baskfy_worker.tasks.celery_tasks import fno_scan_task

ENV_VAR: Final = "BASKFY_TEST_DATABASE_URL"
API_DIR: Final = Path(__file__).resolve().parents[2] / "api"
IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _weekdays(start: dt.date, end: dt.date) -> list[dt.date]:
    out: list[dt.date] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            out.append(day)
        day += dt.timedelta(days=1)
    return out


SESSIONS: Final = _weekdays(dt.date(2030, 4, 1), dt.date(2030, 12, 31))


def _last(month: int, weekday: int) -> dt.date:
    return max(d for d in SESSIONS if d.month == month and d.weekday() == weekday)


E1: Final = _last(9, 3)  # NIFTY's September monthly (a Thursday)
E2: Final = _last(10, 3)
BANK_E: Final = _last(10, 1)  # BANKNIFTY lists only an October monthly here
S1: Final = SESSIONS[SESSIONS.index(E1) - 15]  # NIFTY's entry session
T: Final = SESSIONS[SESSIONS.index(S1) - 1]  # the scanned session
HISTORY: Final = SESSIONS[SESSIONS.index(T) - 89 : SESSIONS.index(T) + 1]
NIFTY_LOT: Final = 75
STOCK_LOT: Final = 500


@dataclass(frozen=True)
class Stock:
    symbol: str
    turnover: float
    rising: bool
    industry: str | None


STOCKS: Final = (
    Stock("ZQA", 10e9, True, "nifty-it"),
    Stock("ZQB", 9e9, True, "nifty-bank"),
    Stock("ZQC", 8e9, True, "nifty-it"),
    Stock("ZQD", 7e9, True, None),  # banned for S1
    Stock("ZQE", 6e9, False, None),
    Stock("ZQF", 1.0e9, True, None),
    Stock("ZQG", 0.9e9, True, None),
    Stock("ZQH", 0.8e9, True, None),
    Stock("ZQI", 0.7e9, True, None),
)
OPEN_F2: Final = Stock("ZQL", 0.0, True, "nifty-it")


def _d(value: float) -> Decimal:
    return Decimal(repr(value)).quantize(Decimal("0.05"), rounding=ROUND_HALF_UP)


# --- database -------------------------------------------------------------------------------------


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
async def _rolled_back(url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(url)
    session = async_sessionmaker(engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()
        await engine.dispose()


# --- fixture market -------------------------------------------------------------------------------


def _future_rows(
    symbol: str, instrument: str, closes: Sequence[float], turnover: float, lot: int
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for day, close in zip(HISTORY, closes, strict=True):
        for expiry, carry in ((E1, 1.0), (E2, 1.002)):
            settle = close * carry
            rows.append(
                {
                    "trade_date": day,
                    "instrument": instrument,
                    "symbol": symbol,
                    "expiry": expiry,
                    "strike": Decimal(0),
                    "option_type": "XX",
                    "open": _d(settle),
                    "high": _d(settle * 1.01),
                    "low": _d(settle * 0.99),
                    "close": _d(settle),
                    "settle": _d(settle),
                    "open_interest": 1_000_000,
                    "volume": 10_000,
                    "turnover": Decimal(repr(turnover / 2)),
                    "lot_size": lot,
                    "source_key": "test",
                }
            )
    return rows


def _nifty_closes(up: bool) -> list[float]:
    n = len(HISTORY)
    step = 10.0 if up else -10.0
    return [20000.0 - step * (n - 1 - i) for i in range(n)]


def _chain_rows() -> list[dict[str, object]]:
    years = (E1 - T).days / 365.0
    rows: list[dict[str, object]] = []
    for strike in range(17000, 23100, 100):
        for kind in (OptionType.CE, OptionType.PE):
            price = max(black76_price(20000.0, float(strike), years, 0.0, 0.15, kind), 0.05)
            rows.append(
                {
                    "trade_date": T,
                    "instrument": "OPTIDX",
                    "symbol": "NIFTY",
                    "expiry": E1,
                    "strike": Decimal(strike),
                    "option_type": kind.value,
                    "open": None,
                    "high": None,
                    "low": None,
                    "close": _d(price),
                    "settle": _d(price),
                    "open_interest": 10_000_000,
                    "volume": 1_000,
                    "turnover": None,
                    "lot_size": NIFTY_LOT,
                    "source_key": "test",
                }
            )
    return rows


async def _user(session: AsyncSession) -> int:
    user = AppUser(public_id="fo4scan0001", email="fo4-scan@example.com", name="FO4")
    session.add(user)
    await session.flush()
    await seed_fno(session, user.id)
    return int(user.id)


async def _industries(session: AsyncSession) -> None:
    await session.execute(
        insert(Exchange).values(id=NSE_EXCHANGE_ID, code="NSE").on_conflict_do_nothing()
    )
    slugs: dict[str, int] = {}
    for n, slug in enumerate(("nifty-it", "nifty-bank")):
        found = (
            await session.execute(sa.select(IndexDef.id).where(IndexDef.slug == slug))
        ).scalar_one_or_none()
        if found is None:
            index = IndexDef(id=240 + n, slug=slug, name=slug, is_universe=False)
            session.add(index)
            await session.flush()
            found = index.id
        slugs[slug] = int(found)
    for stock in (*STOCKS, OPEN_F2):
        if stock.industry is None:
            continue
        instrument = Instrument(
            exchange_id=NSE_EXCHANGE_ID,
            symbol=stock.symbol,
            name=stock.symbol,
            instrument_type="EQ",
            is_active=True,
        )
        session.add(instrument)
        await session.flush()
        session.add(
            IndexMemberDaily(
                index_id=slugs[stock.industry],
                date=T,
                instrument_id=instrument.id,
                source="nse_file",
            )
        )
    await session.flush()


async def _market(
    session: AsyncSession, *, nifty_up: bool = True, status: str = "INGESTED"
) -> None:
    await session.execute(
        insert(TradingDay)
        .values(
            [
                {
                    "exchange_id": NSE_EXCHANGE_ID,
                    "date": d,
                    "is_trading_day": True,
                    "source": "derived",
                }
                for d in SESSIONS
            ]
        )
        .on_conflict_do_update(
            index_elements=["exchange_id", "date"], set_={"is_trading_day": True}
        )
    )
    for month_start in sorted({d.replace(day=1) for d in HISTORY}):
        await ensure_contract_partition(session, month_start)
    rows = [
        *_future_rows("NIFTY", "FUTIDX", _nifty_closes(nifty_up), 9e11, NIFTY_LOT),
        *_future_rows("BANKNIFTY", "FUTIDX", [45000.0] * len(HISTORY), 5e11, 30),
        *_chain_rows(),
    ]
    n = len(HISTORY)
    for stock in STOCKS:
        closes = [100.0 + i if stock.rising else 200.0 - i for i in range(n)]
        rows.extend(_future_rows(stock.symbol, "FUTSTK", closes, stock.turnover, STOCK_LOT))
    for start in range(0, len(rows), 1000):
        await session.execute(sa.insert(FoContractDaily).values(rows[start : start + 1000]))
    expiries = [
        ("NIFTY", E1, NIFTY_LOT),
        ("NIFTY", E2, NIFTY_LOT),
        ("BANKNIFTY", BANK_E, 30),
        *[(s.symbol, e, STOCK_LOT) for s in STOCKS for e in (E1, E2)],
    ]
    await session.execute(
        sa.insert(OpExpiry).values(
            [
                {
                    "underlying": u,
                    "expiry_date": e,
                    "kind": "MONTHLY",
                    "lot_size": lot,
                    "first_seen": HISTORY[0],
                    "seen_on": T,
                }
                for u, e, lot in expiries
            ]
        )
    )
    await session.execute(
        sa.insert(OpContract).values(
            [
                {
                    "instrument_token": 9_990_000_000 + i,
                    "tradingsymbol": f"NIFTYZQ{row['strike']}{row['option_type']}",
                    "underlying": "NIFTY",
                    "expiry": E1,
                    "strike": row["strike"],
                    "option_type": row["option_type"],
                    "lot_size": NIFTY_LOT,
                    "tick_size": Decimal("0.05"),
                    "first_seen": HISTORY[0],
                    "last_seen": T,
                }
                for i, row in enumerate(_chain_rows())
            ]
        )
    )
    session.add(
        FoIngestDay(
            trade_date=T,
            status=status,
            ban_for_session=S1,
            ban_symbols=["ZQD"],
        )
    )
    await _industries(session)
    await session.flush()


async def _open_position(session: AsyncSession, user_id: int, sleeve: str, symbol: str) -> int:
    position = FoPosition(
        user_id=user_id,
        sleeve=sleeve,
        symbol=symbol,
        structure="FUTURE" if sleeve == "F2" else "IRON_CONDOR",
        entry_plan_id=f"plan-{symbol}",
        legs={},
        lots=1,
        lot_size=STOCK_LOT,
        opened_at=dt.datetime(2030, 8, 1, 9, 30, tzinfo=IST),
        simulated=True,
    )
    session.add(position)
    await session.flush()
    return int(position.id)


def _loss(  # noqa: PLR0913, PLR0917 - one journal row
    pid: int,
    user: int,
    sleeve: str,
    symbol: str,
    r: str,
    closed_on: dt.date,
    *,
    net: Decimal = Decimal(-1600),
    simulated: bool = True,
) -> FoJournal:
    return FoJournal(
        position_id=pid,
        user_id=user,
        sleeve=sleeve,
        symbol=symbol,
        structure="IRON_CONDOR",
        opened_on=closed_on - dt.timedelta(days=20),
        closed_on=closed_on,
        entry_inr=Decimal(1000),
        exit_inr=Decimal(2500),
        gross_pnl_inr=net,
        costs_inr=Decimal(0),
        net_pnl_inr=net,
        risk_budget_inr=Decimal(25000),
        r_multiple=Decimal(r),
        closed_reason="LOSS_CLOSE",
        sessions_held=10,
        simulated=simulated,
        sizing_mode="BUDGET",
    )


async def _rows(session: AsyncSession, user_id: int, sleeve: str) -> dict[str, FoScan]:
    session.expire_all()
    found = await session.execute(
        sa.select(FoScan).where(
            FoScan.user_id == user_id, FoScan.sleeve == sleeve, FoScan.trade_date == T
        )
    )
    return {row.symbol: row for row in found.scalars()}


async def _set_f1_capital(session: AsyncSession, user_id: int, capital: Decimal) -> None:
    await session.execute(
        sa.update(FoSleeveConfig)
        .where(FoSleeveConfig.user_id == user_id, FoSleeveConfig.sleeve == "F1")
        .values(capital_inr=capital)
    )


# --- F1 -------------------------------------------------------------------------------------------


@requires_db
class TestF1:
    async def test_the_entry_session_proposes_the_condor_from_the_bhavcopy(
        self, fo_url: str
    ) -> None:
        async with _rolled_back(fo_url) as session:
            await _market(session)
            user = await _user(session)
            await _set_f1_capital(session, user, Decimal(2_500_000))
            await run_scan(session, T, user_ids=[user])
            row = (await _rows(session, user, "F1N"))["NIFTY"]
            assert row.state == ScanState.CANDIDATE.value, row.reasons
            detail = row.detail
            assert detail["entry_session"] == S1.isoformat()
            assert detail["expiry"] == E1.isoformat()
            legs = detail["legs"]
            assert isinstance(legs, list)
            assert [(leg["role"], leg["strike"], leg["qty_sign"]) for leg in legs] == [
                ("LONG_PUT", "18900.00", 1),
                ("LONG_CALL", "21100.00", 1),
                ("SHORT_PUT", "19200.00", -1),
                ("SHORT_CALL", "20800.00", -1),
            ]
            assert all(leg["expiry"] == E1.isoformat() for leg in legs)
            assert row.credit is not None and row.credit > 0
            assert row.max_loss_per_lot is not None and row.max_loss_per_lot > 0
            assert row.iv is not None and abs(row.iv - Decimal("0.15")) < Decimal("0.002")
            assert row.rv20 is not None and row.iv_rv is not None
            assert detail["iv_rv_note"] == "recorded, not used"
            assert row.cost_share is not None and row.cost_share < 25
            assert detail["lots"] == 1

    async def test_the_seeded_25_lakh_carries_a_nifty_lot_and_10_lakh_would_not(
        self, fo_url: str
    ) -> None:
        """M.2: at M.1's Rs 10 lakh (Rs 10,000 at 1 %) no NIFTY lot fits and the scan says so by
        name; at the seeded Rs 25 lakh (Rs 25,000) the same condor is a CANDIDATE."""
        async with _rolled_back(fo_url) as session:
            await _market(session)
            user = await _user(session)
            await run_scan(session, T, user_ids=[user])
            row = (await _rows(session, user, "F1N"))["NIFTY"]
            assert row.state == "CANDIDATE", row.reasons
            assert row.detail["lots"] >= 1
            await session.execute(
                sa.update(FoSleeveConfig)
                .where(FoSleeveConfig.user_id == user, FoSleeveConfig.sleeve == "F1")
                .values(capital_inr=Decimal("1000000.00"))
            )
            await run_scan(session, T, user_ids=[user])
            row = (await _rows(session, user, "F1N"))["NIFTY"]
            assert row.state == "REJECTED_SIZE"
            assert any("0 lots" in r for r in row.reasons)

    async def test_not_the_entry_day_names_the_next_one(self, fo_url: str) -> None:
        async with _rolled_back(fo_url) as session:
            await _market(session)
            user = await _user(session)
            await run_scan(session, T, user_ids=[user])
            row = (await _rows(session, user, "F1B"))["BANKNIFTY"]
            assert row.state == ScanState.NOT_ENTRY_DAY.value
            entry = SESSIONS[SESSIONS.index(BANK_E) - 15]
            assert row.detail["next_entry_date"] == entry.isoformat()
            assert entry.isoformat() in row.reasons[0]

    async def test_an_event_inside_the_hold_is_skipped_with_the_proposal_kept(
        self, fo_url: str
    ) -> None:
        async with _rolled_back(fo_url) as session:
            await _market(session)
            user = await _user(session)
            event = SESSIONS[SESSIONS.index(S1) + 5]
            session.add(OpEventDay(user_id=user, date=event, reason="RBI_POLICY", source="USER"))
            await session.flush()
            await run_scan(session, T, user_ids=[user])
            row = (await _rows(session, user, "F1N"))["NIFTY"]
            assert row.state == ScanState.SKIPPED_EVENT.value
            assert "RBI_POLICY" in row.reasons[0] and event.isoformat() in row.reasons[0]
            legs = row.detail["legs"]
            assert isinstance(legs, list) and len(legs) == 4

    async def test_an_open_structure_and_three_loss_closes(self, fo_url: str) -> None:
        async with _rolled_back(fo_url) as session:
            await _market(session)
            user = await _user(session)
            await _open_position(session, user, "F1B", "BANKNIFTY")
            for n, r in enumerate(("-0.73", "-0.65", "-0.61")):
                pid = await _open_position(session, user, "F1N", "NIFTY")
                await session.execute(
                    sa.update(FoPosition)
                    .where(FoPosition.id == pid)
                    .values(closed_at=dt.datetime(2030, 7, 1 + n, 15, tzinfo=IST))
                )
                session.add(
                    FoJournal(
                        position_id=pid,
                        user_id=user,
                        sleeve="F1N",
                        symbol="NIFTY",
                        structure="IRON_CONDOR",
                        opened_on=dt.date(2030, 6, 1),
                        closed_on=dt.date(2030, 7, 1 + n),
                        entry_inr=Decimal(1000),
                        exit_inr=Decimal(2500),
                        gross_pnl_inr=Decimal(-1500),
                        costs_inr=Decimal(100),
                        net_pnl_inr=Decimal(-1600),
                        risk_budget_inr=Decimal(10000),
                        r_multiple=Decimal(r),
                        closed_reason="LOSS_CLOSE",
                        sessions_held=10,
                        simulated=True,
                        sizing_mode="BUDGET",
                    )
                )
            await session.flush()
            await run_scan(session, T, user_ids=[user])
            bank = (await _rows(session, user, "F1B"))["BANKNIFTY"]
            assert bank.state == ScanState.OPEN_POSITION.value
            nifty = (await _rows(session, user, "F1N"))["NIFTY"]
            assert nifty.state == ScanState.PAUSED.value
            assert "last 3 closed F1N trades" in nifty.reasons[0]

    async def test_live_losses_never_pause_the_paper_scan(self, fo_url: str) -> None:
        """FO10.1 (correcting FO4.9): the scan reads the paper journal only; three live loss
        closes are another pool."""
        async with _rolled_back(fo_url) as session:
            await _market(session)
            user = await _user(session)
            for n, r in enumerate(("-0.73", "-0.65", "-0.61")):
                pid = await _open_position(session, user, "F1N", "NIFTY")
                await session.execute(
                    sa.update(FoPosition)
                    .where(FoPosition.id == pid)
                    .values(closed_at=dt.datetime(2030, 7, 1 + n, 15, tzinfo=IST), simulated=False)
                )
                session.add(
                    _loss(pid, user, "F1N", "NIFTY", r, dt.date(2030, 7, 1 + n), simulated=False)
                )
            await session.flush()
            await run_scan(session, T, user_ids=[user])
            nifty = (await _rows(session, user, "F1N"))["NIFTY"]
            assert nifty.state != ScanState.PAUSED.value, nifty.reasons

    async def test_a_book_amount_of_0_is_the_75000_ceiling(self, fo_url: str) -> None:
        """FO10.5: the seed's 0 is "not set", which is the ceiling, not "off"."""
        async with _rolled_back(fo_url) as session:
            await _market(session)
            user = await _user(session)
            pid = await _open_position(session, user, "F1B", "BANKNIFTY")
            month_day = S1.replace(day=1)
            await session.execute(
                sa.update(FoPosition)
                .where(FoPosition.id == pid)
                .values(closed_at=dt.datetime.combine(month_day, dt.time(15), tzinfo=IST))
            )
            session.add(
                _loss(pid, user, "F1B", "BANKNIFTY", "-3", month_day, net=Decimal("-75000"))
            )
            await session.flush()
            await run_scan(session, T, user_ids=[user])
            nifty = (await _rows(session, user, "F1N"))["NIFTY"]
            assert nifty.state == ScanState.PAUSED.value
            assert any("₹75000 book pause" in r for r in nifty.reasons), nifty.reasons


# --- F2 -------------------------------------------------------------------------------------------


@requires_db
class TestF2:
    async def test_candidates_and_the_notable_skips(self, fo_url: str) -> None:
        async with _rolled_back(fo_url) as session:
            await _market(session)
            user = await _user(session)
            await _open_position(session, user, "F2", OPEN_F2.symbol)
            await run_scan(session, T, user_ids=[user])
            rows = await _rows(session, user, "F2")
            assert {s: r.state for s, r in rows.items()} == {
                "ZQL": "OPEN_POSITION",
                "ZQA": "CANDIDATE",
                "ZQB": "CANDIDATE",
                "ZQC": "BLOCKED_CAPACITY",
                "ZQD": "BLOCKED_BAN",
                "ZQE": "NO_SIGNAL",
            }, "names outside the top 60 % by turnover write no row"
            assert "industry nifty-it" in rows["ZQC"].reasons[0]
            assert all(r.reasons and all(x.strip() for x in r.reasons) for r in rows.values())

    async def test_a_candidates_plan_numbers(self, fo_url: str) -> None:
        async with _rolled_back(fo_url) as session:
            await _market(session)
            user = await _user(session)
            await run_scan(session, T, user_ids=[user])
            row = (await _rows(session, user, "F2"))["ZQA"]
            d = row.detail
            entry = Decimal(str(d["entry_reference"]))
            atr = Decimal(str(d["atr14"]))
            stop = Decimal(str(d["stop"]))
            assert d["contract_expiry"] == E1.isoformat(), "near month: 15 sessions out"
            assert entry == _d(189.0)  # the E1 settle on T
            assert stop == entry - 3 * atr
            assert Decimal(str(d["gtt_trigger"])) >= stop
            assert Decimal(str(d["breakout_level"])) < Decimal(str(d["close"]))
            assert d["lots"] == 1 and d["sizing_mode"] == "PAPER_ONE_LOT"
            assert isinstance(d["lots_at_ceiling"], int)
            assert row.max_loss_per_lot == ((entry - stop) * STOCK_LOT).quantize(Decimal("0.01"))
            assert row.cost_share is not None and row.cost_share > 0

    async def test_nifty_below_its_average_blocks_the_signals(self, fo_url: str) -> None:
        async with _rolled_back(fo_url) as session:
            await _market(session, nifty_up=False)
            user = await _user(session)
            await run_scan(session, T, user_ids=[user])
            rows = await _rows(session, user, "F2")
            assert {s for s, r in rows.items() if r.state == "BLOCKED_REGIME"} == {
                "ZQA",
                "ZQB",
                "ZQC",
            }
            assert "NIFTY's future" in rows["ZQA"].reasons[0]

    async def test_a_day_not_ingested_writes_one_sleeve_row_with_its_reason(
        self, fo_url: str
    ) -> None:
        async with _rolled_back(fo_url) as session:
            await _market(session, status="PENDING")
            user = await _user(session)
            await run_scan(session, T, user_ids=[user])
            rows = await _rows(session, user, "F2")
            assert list(rows) == [SLEEVE_ROW]
            assert rows[SLEEVE_ROW].state == "NO_DATA"
            assert "not ingested (PENDING)" in rows[SLEEVE_ROW].reasons[0]
            nifty = (await _rows(session, user, "F1N"))["NIFTY"]
            assert nifty.state == "NO_DATA" and "not ingested" in nifty.reasons[0]


# --- idempotency and the seams --------------------------------------------------------------------


@requires_db
class TestIdempotentAndWired:
    async def test_a_rerun_leaves_the_same_rows_and_drops_stale_ones(self, fo_url: str) -> None:
        async with _rolled_back(fo_url) as session:
            await _market(session)
            user = await _user(session)
            session.add(
                FoScan(
                    user_id=user,
                    sleeve="F2",
                    trade_date=T,
                    symbol="STALE",
                    state="CANDIDATE",
                    reasons=["from an earlier run"],
                )
            )
            await session.flush()
            await run_scan(session, T, user_ids=[user])

            async def snapshot() -> set[tuple[object, ...]]:
                session.expire_all()
                found = await session.execute(
                    sa.select(FoScan).where(FoScan.user_id == user, FoScan.trade_date == T)
                )
                return {
                    (r.id, r.sleeve, r.symbol, r.state, tuple(r.reasons), str(r.detail), r.credit)
                    for r in found.scalars()
                }

            first = await snapshot()
            assert "STALE" not in {row[2] for row in first}
            await run_scan(session, T, user_ids=[user])
            assert await snapshot() == first

    async def test_the_nightly_savepoint_keeps_the_ingest_when_the_scan_fails(
        self, fo_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def boom(session: AsyncSession, trade_date: dt.date) -> dict[str, object]:
            raise RuntimeError("scan exploded")

        async with _rolled_back(fo_url) as session:
            await _market(session)
            ok = await nightly.scan_in_savepoint(session, T)
            assert "users" in ok or "skipped" in ok
            monkeypatch.setattr(nightly, "run_scan", boom)
            out = await nightly.scan_in_savepoint(session, T)
            assert out == {"error": "RuntimeError: scan exploded"}
            assert await session.get(FoIngestDay, T) is not None, "the ingest is kept"


def test_the_cli_has_a_scan_command(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dt.date] = []

    async def fake(day: dt.date) -> dict[str, object]:
        seen.append(day)
        return {"trade_date": day.isoformat()}

    monkeypatch.setattr(fno_cli, "_scan", fake)
    assert fno_cli.main(["scan", "--date", "2030-09-04"]) == 0
    assert seen == [dt.date(2030, 9, 4)]


def test_the_scan_task_refuses_while_the_flag_is_off() -> None:
    out = fno_scan_task.run("2030-09-04")
    assert out == {"trade_date": "2030-09-04", "skipped": "BASKFY_FNO_SCAN_ENABLED is false"}
