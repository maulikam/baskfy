"""VB5's acceptance: the sleeve's money comes from the sleeve's own rows.

Two claims, and the second is a safety property rather than an arithmetic one:

1. **A resting limit commits cash without spending it.** ``cash_available`` subtracts it and
   ``cash`` does not, which is what stops a fourth line being sized against a balance three
   limits have already claimed (``04`` §9.1's ``SLOTS_FULL`` counts them for the same reason).
2. **A holding the sleeve did not buy is invisible.** `02` Track C §5: the book is
   ``vb_position`` and nothing else, so a name the weekly book or the swing book owns cannot
   raise this sleeve's equity — and, from VB6, cannot become a sell line either.

The arithmetic itself is tested without a database in
``packages/core/tests/test_vbt_sleeve.py``; this file tests the **loading**, which is where the
two claims above actually live.

It lives in the worker tree although the module it tests is ``baskfy_api.vbt_sleeve``, because
this is where a per-test database fixture already exists — and because the reader of this loader
that matters most is the evening job, which is the worker's.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import sqlalchemy as sa
from helpers import make_instrument, requires_db
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.vbt_sleeve import (
    held_instrument_ids,
    load_sleeve,
    marks,
    working_instrument_ids,
)
from baskfy_core.models import AppUser, OhlcvDaily, VbConfig, VbOrder, VbPosition

pytestmark = requires_db

AS_OF = dt.date(2026, 8, 18)
LAKH = Decimal("1000000.00")


async def _user(session: AsyncSession, tag: str, capital: Decimal = LAKH) -> int:
    user = AppUser(public_id=f"vb5-{tag}", email=f"vb5-{tag}@example.com")
    session.add(user)
    await session.flush()
    session.add(VbConfig(user_id=user.id, sleeve_capital_inr=capital, updated_by="test"))
    await session.flush()
    return int(user.id)


async def _bar(session: AsyncSession, instrument_id: int, on: dt.date, close: str) -> None:
    session.add(
        OhlcvDaily(
            instrument_id=instrument_id,
            date=on,
            open=Decimal(close),
            high=Decimal(close),
            low=Decimal(close),
            close=Decimal(close),
            close_raw=Decimal(close),
            volume=100_000,
            volume_raw=100_000,
            turnover=Decimal("10000000.00"),
            adj_factor=Decimal(1),
            source="nse",
        )
    )
    await session.flush()


async def _position(  # noqa: PLR0913 - a book row is its numbers
    session: AsyncSession,
    user_id: int,
    instrument_id: int,
    *,
    quantity: int = 1_000,
    entry: str = "100.00",
    state: str = "OPEN",
    pnl: str | None = None,
) -> None:
    session.add(
        VbPosition(
            user_id=user_id,
            instrument_id=instrument_id,
            entry_date=AS_OF,
            entry_avg=Decimal(entry),
            quantity_entered=quantity,
            quantity_open=quantity if state == "OPEN" else 0,
            initial_stop=Decimal("88.00"),
            stop_price=Decimal("88.00"),
            state=state,
            closed_on=None if state == "OPEN" else AS_OF,
            pnl_inr=None if pnl is None else Decimal(pnl),
        )
    )
    await session.flush()


async def _order(  # noqa: PLR0913 - an order row is its numbers
    session: AsyncSession,
    user_id: int,
    instrument_id: int,
    *,
    quantity: int,
    limit: str,
    state: str = "SENT",
) -> None:
    session.add(
        VbOrder(
            user_id=user_id,
            instrument_id=instrument_id,
            signal_date=AS_OF,
            limit_price=Decimal(limit),
            stop_price=Decimal("50.00"),
            quantity=quantity,
            state=state,
            cancel_reason="EXPIRY_SWEEP" if state == "CANCELLED" else None,
        )
    )
    await session.flush()


@pytest.mark.db
class TestTheSleeveReadsItsOwnRows:
    async def test_an_untouched_sleeve_is_worth_its_capital(self, session: AsyncSession) -> None:
        user_id = await _user(session, "untouched")
        value = await load_sleeve(session, user_id, AS_OF)
        assert value.equity_inr == LAKH
        assert value.cash_available_inr == LAKH
        assert value.open_exposure_inr == Decimal(0)

    async def test_a_holding_the_sleeve_did_not_buy_is_invisible(
        self, session: AsyncSession
    ) -> None:
        """`02` Track C §5. The bar exists, the instrument exists, and the sleeve has no row for
        it — so it is not this book's, and this book's equity does not move."""
        user_id = await _user(session, "not-ours")
        other = await make_instrument(session, "NOTOURS")
        await _bar(session, other, AS_OF, "500.00")

        value = await load_sleeve(session, user_id, AS_OF)
        held = await held_instrument_ids(session, user_id)
        assert value.equity_inr == LAKH
        assert held == frozenset()

    async def test_an_open_position_is_marked_at_the_latest_close_on_or_before(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session, "marked")
        instrument_id = await make_instrument(session, "MARKED")
        await _bar(session, instrument_id, AS_OF - dt.timedelta(days=7), "100.00")
        await _bar(session, instrument_id, AS_OF, "130.00")
        # A bar from *after* the session being reported must not reach the mark.
        await _bar(session, instrument_id, AS_OF + dt.timedelta(days=7), "300.00")
        await _position(session, user_id, instrument_id, quantity=1_000, entry="100.00")

        value = await load_sleeve(session, user_id, AS_OF)
        prices = await marks(session, [instrument_id], AS_OF)
        assert prices[instrument_id] == Decimal("130.00")
        assert value.cash_inr == Decimal("900000.00")
        assert value.equity_inr == Decimal("1030000.00")
        assert value.unrealised_inr == Decimal("30000.00")

    async def test_a_name_that_has_not_printed_is_marked_at_its_entry(
        self, session: AsyncSession
    ) -> None:
        """A book that quietly excluded a suspended holding would report an equity the sleeve
        does not have; `04` §6.5's write-off is what eventually removes it."""
        user_id = await _user(session, "silent")
        instrument_id = await make_instrument(session, "SILENT")
        await _position(session, user_id, instrument_id, quantity=500, entry="60.00")

        value = await load_sleeve(session, user_id, AS_OF)
        assert value.value_of_open_inr == Decimal("30000.00")
        assert value.equity_inr == LAKH

    async def test_a_resting_limit_commits_cash_without_spending_it(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session, "committed")
        instrument_id = await make_instrument(session, "BIDFOR")
        await _order(session, user_id, instrument_id, quantity=500, limit="200.00")

        value = await load_sleeve(session, user_id, AS_OF)
        working = await working_instrument_ids(session, user_id)
        assert value.cash_inr == LAKH
        assert value.cash_available_inr == Decimal("900000.00")
        assert value.open_exposure_inr == Decimal("100000.00")
        assert working == frozenset({instrument_id})

    async def test_a_cancelled_order_commits_nothing(self, session: AsyncSession) -> None:
        user_id = await _user(session, "cancelled")
        instrument_id = await make_instrument(session, "GONE")
        await _order(
            session, user_id, instrument_id, quantity=500, limit="200.00", state="CANCELLED"
        )

        value = await load_sleeve(session, user_id, AS_OF)
        assert value.committed_inr == Decimal(0)
        assert value.cash_available_inr == LAKH
        assert await working_instrument_ids(session, user_id) == frozenset()

    async def test_a_partly_filled_order_commits_only_what_is_left(
        self, session: AsyncSession
    ) -> None:
        """`04` §7.5 — the filled half is a position; the remainder is still a bid."""
        user_id = await _user(session, "partial")
        instrument_id = await make_instrument(session, "HALFCO")
        await _order(session, user_id, instrument_id, quantity=500, limit="200.00", state="PARTIAL")
        await session.execute(
            sa.update(VbOrder).where(VbOrder.user_id == user_id).values(filled_quantity=200)
        )
        await session.flush()

        value = await load_sleeve(session, user_id, AS_OF)
        assert value.committed_inr == Decimal("60000.00")

    async def test_realised_profit_is_cash(self, session: AsyncSession) -> None:
        user_id = await _user(session, "realised")
        instrument_id = await make_instrument(session, "CLOSEDCO")
        await _position(
            session, user_id, instrument_id, entry="100.00", state="CLOSED", pnl="45000.00"
        )

        value = await load_sleeve(session, user_id, AS_OF)
        assert value.cash_inr == Decimal("1045000.00")
        assert value.equity_inr == Decimal("1045000.00")

    async def test_a_sleeve_with_no_config_row_has_no_capital(self, session: AsyncSession) -> None:
        """Zero rather than an error: a sleeve nobody has configured plans nothing, which is the
        answer `02` §3.4 wants."""
        value = await load_sleeve(session, 9_999_999, AS_OF)
        assert value.equity_inr == Decimal(0)
