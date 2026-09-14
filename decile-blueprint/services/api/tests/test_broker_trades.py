"""Trade history reaches Postgres and dates the holdings it accounts for (NEEDS-MAULIK §32).

The spec, from `gates/kite-sync.md` §"What would make transaction sync real": a `broker_trade`
table, an importer that refuses to half-parse, and only then `first_bought_on` /
`history_source='BROKER'` — for a holding the trades add up to, and for no other.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from baskfy_api.broker_trades import TradeSource, apply_trade_history, store_fills
from baskfy_core.models import BrokerTrade, PortfolioHolding
from baskfy_core.tradebook import TradeFill, TradeSide

pytestmark = [pytest.mark.db]

HELD = "TRDHELD"
BONUS = "TRDBONUS"
CAS = "TRDCAS"
SYMBOLS = (HELD, BONUS, CAS)


@pytest_asyncio.fixture
async def db(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        await _wipe(session)
        yield session
        await session.rollback()
        await _wipe(session)
        await session.commit()


async def _wipe(session: AsyncSession) -> None:
    params = {"symbols": list(SYMBOLS)}
    await session.execute(text("DELETE FROM broker_trade WHERE symbol = ANY(:symbols)"), params)
    await session.execute(
        text(
            "DELETE FROM portfolio_holding WHERE instrument_id IN "
            "(SELECT id FROM instrument WHERE symbol = ANY(:symbols))"
        ),
        params,
    )
    await session.execute(text("DELETE FROM portfolio WHERE name = 'TRD pile'"))
    await session.execute(text("DELETE FROM broker_account WHERE label = 'TRD'"))
    await session.execute(text("DELETE FROM instrument WHERE symbol = ANY(:symbols)"), params)
    await session.execute(text("DELETE FROM app_user WHERE email = 'trd@example.test'"))
    await session.commit()


async def _scalar(session: AsyncSession, sql: str, **params: object) -> int:
    return int((await session.execute(text(sql), params)).scalar_one())


async def _setup(session: AsyncSession) -> tuple[int, int, dict[str, int]]:
    user_id = await _scalar(
        session,
        "INSERT INTO app_user (public_id, email, name) "
        "VALUES ('pub-trd', 'trd@example.test', 'trd') RETURNING id",
    )
    account_id = await _scalar(
        session,
        "INSERT INTO broker_account (user_id, broker_id, label) "
        "VALUES (:user_id, 'zerodha', 'TRD') RETURNING id",
        user_id=user_id,
    )
    portfolio_id = await _scalar(
        session,
        "INSERT INTO portfolio (user_id, name, kind, source, started_on, broker_account_id) "
        "VALUES (:user_id, 'TRD pile', 'CAPITAL', 'HOLDING_GROUP', '2026-09-01', :account) "
        "RETURNING id",
        user_id=user_id,
        account=account_id,
    )
    instruments: dict[str, int] = {}
    for symbol in SYMBOLS:
        instruments[symbol] = await _scalar(
            session,
            "INSERT INTO instrument (exchange_id, symbol, name, instrument_type, series, "
            "is_active, listed_on) VALUES (1, :symbol, :symbol, 'EQ', 'EQ', true, '2020-01-01') "
            "RETURNING id",
            symbol=symbol,
        )
    for symbol, quantity, source in (
        (HELD, "60", "NONE"),
        (BONUS, "200", "NONE"),
        (CAS, "10", "CAS"),
    ):
        await session.execute(
            text(
                "INSERT INTO portfolio_holding (portfolio_id, instrument_id, quantity, avg_price, "
                "added_on, broker_account_id, portfolio_kind, first_bought_on, history_source) "
                "VALUES (:portfolio, :instrument, :quantity, 100, '2026-09-01', :account, "
                "'CAPITAL', :first, :source)"
            ),
            {
                "portfolio": portfolio_id,
                "instrument": instruments[symbol],
                "quantity": Decimal(quantity),
                "account": account_id,
                "first": dt.date(2019, 5, 5) if source == "CAS" else None,
                "source": source,
            },
        )
    await session.flush()
    return user_id, account_id, instruments


def _fill(symbol: str, side: TradeSide, qty: str, on: dt.date, tid: str) -> TradeFill:
    return TradeFill(
        symbol=symbol,
        exchange="NSE",
        side=side,
        quantity=Decimal(qty),
        price=Decimal("100.50"),
        trade_date=on,
        trade_id=tid,
    )


FILLS = (
    _fill(HELD, TradeSide.BUY, "100", dt.date(2024, 1, 2), "h1"),
    _fill(HELD, TradeSide.BUY, "20", dt.date(2024, 3, 2), "h2"),
    _fill(HELD, TradeSide.SELL, "60", dt.date(2025, 1, 2), "h3"),
    # 100 bought, 200 held: a 1:1 bonus the trades cannot see.
    _fill(BONUS, TradeSide.BUY, "100", dt.date(2023, 1, 2), "b1"),
    _fill(CAS, TradeSide.BUY, "10", dt.date(2024, 7, 1), "c1"),
    _fill("NOSUCHSYMBOLXYZ", TradeSide.BUY, "1", dt.date(2024, 7, 1), "x1"),
)


@pytest.mark.asyncio
async def test_a_reimport_writes_nothing_new(db: AsyncSession) -> None:
    user_id, account_id, _ = await _setup(db)

    first = await store_fills(
        db, FILLS, user_id=user_id, broker_account_id=account_id, source=TradeSource.CONSOLE_CSV
    )
    again = await store_fills(
        db, FILLS, user_id=user_id, broker_account_id=account_id, source=TradeSource.KITE_API
    )

    assert (first.inserted, first.already_present) == (6, 0)
    assert (again.inserted, again.already_present) == (0, 6)
    assert first.unresolved == ("NOSUCHSYMBOLXYZ",)
    stored = await db.scalar(
        select(func.count())
        .select_from(BrokerTrade)
        .where(BrokerTrade.broker_account_id == account_id)
    )
    assert stored == 6


@pytest.mark.asyncio
async def test_only_a_holding_the_trades_add_up_to_is_dated(db: AsyncSession) -> None:
    user_id, account_id, instruments = await _setup(db)
    await store_fills(
        db, FILLS, user_id=user_id, broker_account_id=account_id, source=TradeSource.CONSOLE_CSV
    )

    report = await apply_trade_history(db, user_id=user_id, broker_account_id=account_id)
    db.expire_all()

    rows = {
        row.instrument_id: row
        for row in (
            await db.scalars(
                select(PortfolioHolding).where(PortfolioHolding.broker_account_id == account_id)
            )
        ).all()
    }
    held = rows[instruments[HELD]]
    # FIFO: the 60 sold came out of the first 100, so the open 60 are 40 from Jan and 20 from
    # March — the oldest open lot is still January's.
    assert (held.first_bought_on, held.history_source) == (dt.date(2024, 1, 2), "BROKER")

    bonus = rows[instruments[BONUS]]
    assert (bonus.first_bought_on, bonus.history_source) == (None, "NONE")
    assert [item.symbol for item in report.undated] == [BONUS]

    cas = rows[instruments[CAS]]
    assert (cas.first_bought_on, cas.history_source) == (dt.date(2019, 5, 5), "CAS")
    assert report.dated == 2  # HELD, and CAS reconciles too but its row is not overwritten
