"""A broker sync must not hand back shares the user has already filed away (0035 / PF3).

THE BUG THIS EXISTS TO CATCH
----------------------------
`replace_holdings` makes the broker's own holding group hold *exactly* what the sync gives it.
That was right while a holding lived in exactly one place: the sync said "you hold 100 ITC", the
group held 100, and there was nowhere else for the shares to be.

0035 let a user file 20 of that ITC into "Long term", which takes 20 out of the broker's group.
The next sync still reads 100 from Kite — correctly; the demat has not changed — and without
`_minus_what_is_filed_elsewhere` it would write all 100 back. The user would then hold 20 in Long
term and 100 in the pile: **120 shares of a 100-share position**, across two rows that each look
perfectly reasonable on their own.

Nothing else in the suite would have found it. The ledger's `validate_against_holdings` compares
slices against the position, and after this bug the position IS 120 — the picture is internally
consistent and simply wrong, which is the exact failure mode `allocation_ledger` opens by warning
about. So the assertion here is on the total, every time, and it is asserted after a *second*
sync, because one sync alone cannot show the drift.

WHAT THE BROKER GROUP MEANS NOW
--------------------------------
The remainder — §6.6's "Unallocated", the part of the pile nobody has sorted yet. It shrinks as
the user files things and disappears entirely when a holding is fully filed, which is the
behaviour that makes the first-run screen a to-do list rather than a static inventory.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from baskfy_execution.broker_ports import HoldingRow
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from baskfy_api.broker_holdings import HoldingsResult
from baskfy_api.broker_holdings_sync import (
    portfolio_for_broker_account,
    sync_holdings_into_portfolio,
)
from baskfy_core.allocation_ledger import PortfolioKind, PortfolioSource
from baskfy_core.models import Portfolio, PortfolioHolding

pytestmark = [pytest.mark.db]

AS_OF = dt.date(2026, 9, 10)
SYMBOL = "SLICEDITC"


@pytest_asyncio.fixture
async def db(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """One transactional session per test, with this module's rows cleared first and after.

    Scoped by symbol rather than by truncation: these tables are shared with every other db-marked
    module, and a TRUNCATE here would make this file's pass or fail depend on execution order.
    """
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        await _wipe(session)
        yield session
        await _wipe(session)
        await session.commit()


async def _wipe(session: AsyncSession) -> None:
    await session.execute(
        text(
            "DELETE FROM portfolio_holding WHERE instrument_id IN "
            "(SELECT id FROM instrument WHERE symbol = :symbol)"
        ),
        {"symbol": SYMBOL},
    )
    await session.execute(
        text(
            "DELETE FROM portfolio WHERE name LIKE 'PF3 %' OR name = 'Zerodha holdings' "
            "OR name = 'Swing'"
        )
    )
    await session.execute(
        text(
            "DELETE FROM sw_position WHERE instrument_id IN "
            "(SELECT id FROM instrument WHERE symbol = :symbol)"
        ),
        {"symbol": SYMBOL},
    )
    await session.execute(text("DELETE FROM broker_account WHERE label = 'PF3'"))
    await session.execute(text("DELETE FROM instrument WHERE symbol = :symbol"), {"symbol": SYMBOL})
    await session.execute(text("DELETE FROM app_user WHERE email = 'pf3@example.test'"))
    await session.commit()


async def _user(session: AsyncSession) -> int:
    row = await session.execute(
        text(
            "INSERT INTO app_user (public_id, email, name) "
            "VALUES ('pub-pf3', 'pf3@example.test', 'pf3') RETURNING id"
        )
    )
    return int(row.scalar_one())


async def _broker(session: AsyncSession, user_id: int) -> int:
    row = await session.execute(
        text(
            "INSERT INTO broker_account (user_id, broker_id, label) "
            "VALUES (:user_id, 'zerodha', 'PF3') RETURNING id"
        ),
        {"user_id": user_id},
    )
    return int(row.scalar_one())


async def _instrument(session: AsyncSession) -> int:
    """Written as SQL rather than through the ORM, the way the db-marked worker tests do it:
    the column set is the schema's, and an ORM insert would omit whatever the model has not
    caught up with."""
    row = await session.execute(
        text(
            "INSERT INTO instrument "
            "(exchange_id, symbol, name, instrument_type, series, is_active, listed_on) "
            "VALUES (1, :symbol, 'Sliced ITC', 'EQ', 'EQ', true, :listed_on) RETURNING id"
        ),
        {"symbol": SYMBOL, "listed_on": AS_OF},
    )
    return int(row.scalar_one())


async def _portfolio(session: AsyncSession, user_id: int, name: str) -> int:
    row = await session.execute(
        text(
            "INSERT INTO portfolio (user_id, name, kind, source, started_on) "
            "VALUES (:user_id, :name, :kind, :source, :started_on) RETURNING id"
        ),
        {
            "user_id": user_id,
            "name": name,
            "kind": PortfolioKind.CAPITAL.value,
            "source": PortfolioSource.MY_STRATEGY.value,
            "started_on": AS_OF,
        },
    )
    return int(row.scalar_one())


def _live(quantity: str) -> HoldingsResult:
    """What Kite reports. Unchanged between syncs — the demat did not move."""
    return HoldingsResult(
        rows=(
            HoldingRow(
                symbol=SYMBOL,
                exchange="NSE",
                quantity=Decimal(quantity),
                t1_quantity=Decimal("0"),
                collateral_quantity=Decimal("0"),
                average_price=Decimal("400.00"),
            ),
        ),
        source="live",
    )


async def _total_held(session: AsyncSession, instrument_id: int) -> Decimal:
    """Every capital slice of this instrument, added up. The number that must stay 100."""
    total = await session.execute(
        select(func.sum(PortfolioHolding.quantity)).where(
            PortfolioHolding.instrument_id == instrument_id,
            PortfolioHolding.portfolio_kind == PortfolioKind.CAPITAL.value,
        )
    )
    return total.scalar_one() or Decimal("0")


async def _sync(session: AsyncSession, user_id: int, broker_account_id: int) -> None:
    await sync_holdings_into_portfolio(
        session,
        _live("100"),
        user_id=user_id,
        broker_account_id=broker_account_id,
        broker_name="Zerodha",
        as_of=AS_OF,
    )
    await session.flush()


@pytest.mark.asyncio
async def test_a_second_sync_does_not_hand_back_shares_the_user_filed_away(
    db: AsyncSession,
) -> None:
    """Maulik's example, run twice: 100 held, 20 filed into Long term, sync again.

    The total must be 100 after the second sync, not 120. This is the whole point of the file.
    """
    user_id = await _user(db)
    broker_account_id = await _broker(db, user_id)
    instrument_id = await _instrument(db)

    await _sync(db, user_id, broker_account_id)
    assert await _total_held(db, instrument_id) == Decimal("100")

    # The user files 20 into Long term, taking them out of the pile — what `_apply_allocation`
    # does, written directly here so this test does not depend on the route.
    long_term = await _portfolio(db, user_id, "PF3 Long term")
    pile = await portfolio_for_broker_account(
        db,
        user_id=user_id,
        broker_account_id=broker_account_id,
        broker_name="Zerodha",
        as_of=AS_OF,
    )
    db.add(
        PortfolioHolding(
            portfolio_id=long_term,
            instrument_id=instrument_id,
            broker_account_id=broker_account_id,
            portfolio_kind=PortfolioKind.CAPITAL.value,
            quantity=Decimal("20"),
            added_on=AS_OF,
        )
    )
    await db.execute(
        text(
            "UPDATE portfolio_holding SET quantity = quantity - 20 "
            "WHERE portfolio_id = :pile AND instrument_id = :instrument"
        ),
        {"pile": int(pile.id), "instrument": instrument_id},
    )
    await db.flush()
    assert await _total_held(db, instrument_id) == Decimal("100")

    await _sync(db, user_id, broker_account_id)

    assert await _total_held(db, instrument_id) == Decimal("100"), (
        "the sync handed back the 20 shares already filed into Long term"
    )


@pytest.mark.asyncio
async def test_the_pile_holds_the_remainder_and_long_term_is_untouched(
    db: AsyncSession,
) -> None:
    """Where the 100 ends up: 80 unsorted, 20 filed. The sync never rewrites the user's row."""
    user_id = await _user(db)
    broker_account_id = await _broker(db, user_id)
    instrument_id = await _instrument(db)
    await _sync(db, user_id, broker_account_id)

    long_term = await _portfolio(db, user_id, "PF3 Long term")
    pile = await portfolio_for_broker_account(
        db,
        user_id=user_id,
        broker_account_id=broker_account_id,
        broker_name="Zerodha",
        as_of=AS_OF,
    )
    db.add(
        PortfolioHolding(
            portfolio_id=long_term,
            instrument_id=instrument_id,
            broker_account_id=broker_account_id,
            portfolio_kind=PortfolioKind.CAPITAL.value,
            quantity=Decimal("20"),
            added_on=AS_OF,
        )
    )
    await db.execute(
        text(
            "UPDATE portfolio_holding SET quantity = quantity - 20 "
            "WHERE portfolio_id = :pile AND instrument_id = :instrument"
        ),
        {"pile": int(pile.id), "instrument": instrument_id},
    )
    await db.flush()

    await _sync(db, user_id, broker_account_id)

    rows = (
        await db.execute(
            select(PortfolioHolding.portfolio_id, PortfolioHolding.quantity).where(
                PortfolioHolding.instrument_id == instrument_id
            )
        )
    ).all()
    by_portfolio = {int(portfolio_id): quantity for portfolio_id, quantity in rows}

    assert by_portfolio[long_term] == Decimal("20"), "the sync rewrote the user's own slice"
    assert by_portfolio[int(pile.id)] == Decimal("80"), "the pile is the unsorted remainder"


@pytest.mark.asyncio
async def test_a_fully_filed_holding_leaves_the_pile_entirely(db: AsyncSession) -> None:
    """§6.6's screen is a to-do list: a holding the user has finished sorting stops appearing.

    A row of zero would keep it on the screen forever, which is the difference between a pile that
    empties as you work and one that only ever changes its numbers.
    """
    user_id = await _user(db)
    broker_account_id = await _broker(db, user_id)
    instrument_id = await _instrument(db)
    await _sync(db, user_id, broker_account_id)

    momentum = await _portfolio(db, user_id, "PF3 Momentum")
    pile = await portfolio_for_broker_account(
        db,
        user_id=user_id,
        broker_account_id=broker_account_id,
        broker_name="Zerodha",
        as_of=AS_OF,
    )
    db.add(
        PortfolioHolding(
            portfolio_id=momentum,
            instrument_id=instrument_id,
            broker_account_id=broker_account_id,
            portfolio_kind=PortfolioKind.CAPITAL.value,
            quantity=Decimal("100"),
            added_on=AS_OF,
        )
    )
    await db.execute(
        text(
            "DELETE FROM portfolio_holding "
            "WHERE portfolio_id = :pile AND instrument_id = :instrument"
        ),
        {"pile": int(pile.id), "instrument": instrument_id},
    )
    await db.flush()

    await _sync(db, user_id, broker_account_id)

    in_pile = (
        await db.execute(
            select(PortfolioHolding.quantity).where(
                PortfolioHolding.portfolio_id == int(pile.id),
                PortfolioHolding.instrument_id == instrument_id,
            )
        )
    ).all()

    assert in_pile == [], "a fully filed holding came back to the unsorted pile"
    assert await _total_held(db, instrument_id) == Decimal("100")


@pytest.mark.asyncio
async def test_the_broker_group_is_still_created_and_named_once(db: AsyncSession) -> None:
    """The pile is looked up by (user, broker, source), so this stays one portfolio across syncs
    even though its contents now shrink."""
    user_id = await _user(db)
    broker_account_id = await _broker(db, user_id)
    await _instrument(db)

    await _sync(db, user_id, broker_account_id)
    await _sync(db, user_id, broker_account_id)

    piles = (
        await db.execute(
            select(Portfolio.id).where(
                Portfolio.user_id == user_id,
                Portfolio.broker_account_id == broker_account_id,
                Portfolio.source == PortfolioSource.HOLDING_GROUP.value,
            )
        )
    ).all()

    assert len(piles) == 1


# ===========================================================================================
# The swing desk files its own positions (PF5, Maulik 10 Sep 2026)
# ===========================================================================================


async def _swing_position(
    session: AsyncSession,
    *,
    user_id: int,
    broker_account_id: int,
    instrument_id: int,
    quantity_open: int,
    state: str = "OPEN",
) -> None:
    """One open position on the desk, written the way `swing_execute` writes it."""
    await session.execute(
        text(
            "INSERT INTO sw_position (user_id, broker_account_id, instrument_id, setup, "
            "entry_date, entry_avg, quantity_entered, initial_stop, stop, trail, "
            "quantity_open, state) "
            "VALUES (:user_id, :broker, :instrument, 'FLAG', :entry_date, 400, "
            ":entered, 380, 380, 'MA20', :open, :state)"
        ),
        {
            "user_id": user_id,
            "broker": broker_account_id,
            "instrument": instrument_id,
            "entry_date": AS_OF,
            "entered": quantity_open,
            "open": quantity_open,
            "state": state,
        },
    )
    await session.flush()


async def _slice_in(session: AsyncSession, name: str, instrument_id: int) -> Decimal | None:
    row = await session.execute(
        select(PortfolioHolding.quantity)
        .join(Portfolio, Portfolio.id == PortfolioHolding.portfolio_id)
        .where(Portfolio.name == name, PortfolioHolding.instrument_id == instrument_id)
    )
    return row.scalar_one_or_none()


@pytest.mark.asyncio
async def test_an_open_swing_position_files_itself_into_a_swing_portfolio(
    db: AsyncSession,
) -> None:
    """Maulik: "few from swing". The desk already knows which shares those are."""
    user_id = await _user(db)
    broker_account_id = await _broker(db, user_id)
    instrument_id = await _instrument(db)
    await _swing_position(
        db,
        user_id=user_id,
        broker_account_id=broker_account_id,
        instrument_id=instrument_id,
        quantity_open=30,
    )

    await _sync(db, user_id, broker_account_id)

    assert await _slice_in(db, "Swing", instrument_id) == Decimal("30")
    assert await _total_held(db, instrument_id) == Decimal("100"), "the 30 were double-counted"


@pytest.mark.asyncio
async def test_a_partial_exit_shrinks_the_slice_rather_than_adding_to_it(
    db: AsyncSession,
) -> None:
    """Idempotence, and the reason this SETS rather than adds: a second sync of the same day must
    not double the position, and a partial exit must shrink it."""
    user_id = await _user(db)
    broker_account_id = await _broker(db, user_id)
    instrument_id = await _instrument(db)
    await _swing_position(
        db,
        user_id=user_id,
        broker_account_id=broker_account_id,
        instrument_id=instrument_id,
        quantity_open=30,
    )
    await _sync(db, user_id, broker_account_id)
    await _sync(db, user_id, broker_account_id)

    assert await _slice_in(db, "Swing", instrument_id) == Decimal("30"), "a re-sync added again"

    await db.execute(
        text("UPDATE sw_position SET quantity_open = 12, state = 'PARTIAL'"),
    )
    await db.flush()
    await _sync(db, user_id, broker_account_id)

    assert await _slice_in(db, "Swing", instrument_id) == Decimal("12")
    assert await _total_held(db, instrument_id) == Decimal("100")


@pytest.mark.asyncio
async def test_a_closed_position_releases_its_shares_back_to_the_pile(
    db: AsyncSession,
) -> None:
    user_id = await _user(db)
    broker_account_id = await _broker(db, user_id)
    instrument_id = await _instrument(db)
    await _swing_position(
        db,
        user_id=user_id,
        broker_account_id=broker_account_id,
        instrument_id=instrument_id,
        quantity_open=30,
    )
    await _sync(db, user_id, broker_account_id)

    await db.execute(text("UPDATE sw_position SET quantity_open = 0, state = 'CLOSED'"))
    await db.flush()
    await _sync(db, user_id, broker_account_id)

    assert await _slice_in(db, "Swing", instrument_id) is None
    assert await _total_held(db, instrument_id) == Decimal("100")


@pytest.mark.asyncio
async def test_the_desk_never_takes_shares_the_user_filed_by_hand(db: AsyncSession) -> None:
    """The cap that stops auto-filing from over-allocating.

    90 of the 100 are in the user's own "Long term", so only 10 are free. The desk's 30-share
    position is filed as 10 — visibly short, which the user can see and correct — rather than
    taking 30 and putting the position 20 shares over what is held.
    """
    user_id = await _user(db)
    broker_account_id = await _broker(db, user_id)
    instrument_id = await _instrument(db)
    await _sync(db, user_id, broker_account_id)

    long_term = await _portfolio(db, user_id, "PF3 Long term")
    pile = await portfolio_for_broker_account(
        db,
        user_id=user_id,
        broker_account_id=broker_account_id,
        broker_name="Zerodha",
        as_of=AS_OF,
    )
    db.add(
        PortfolioHolding(
            portfolio_id=long_term,
            instrument_id=instrument_id,
            broker_account_id=broker_account_id,
            portfolio_kind=PortfolioKind.CAPITAL.value,
            quantity=Decimal("90"),
            added_on=AS_OF,
        )
    )
    await db.execute(
        text(
            "UPDATE portfolio_holding SET quantity = 10 "
            "WHERE portfolio_id = :pile AND instrument_id = :instrument"
        ),
        {"pile": int(pile.id), "instrument": instrument_id},
    )
    await _swing_position(
        db,
        user_id=user_id,
        broker_account_id=broker_account_id,
        instrument_id=instrument_id,
        quantity_open=30,
    )
    await db.flush()

    await _sync(db, user_id, broker_account_id)

    assert await _slice_in(db, "PF3 Long term", instrument_id) == Decimal("90")
    assert await _slice_in(db, "Swing", instrument_id) == Decimal("10")
    assert await _total_held(db, instrument_id) == Decimal("100")
