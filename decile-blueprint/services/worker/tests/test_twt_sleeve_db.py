"""TW5's acceptance: the sleeve's money, the sleeve's book, and the counter that is not a flag.

Four claims, and three of them are safety properties rather than arithmetic:

1. **The sleeve sizes against its own money.** ``04`` §9.1-9.3: capital plus its own realised
   profit plus its own marked positions, and nothing the account holds. It is a ``MY_STRATEGY``
   capital portfolio in the M34 / ``PORTFOLIO_REDESIGN`` sense, exactly as the swing and VBT
   sleeves are.
2. **It owns only what it bought.** ``tw_position`` is the book; a name held by the weekly book,
   the swing book or VBT-1 is invisible to :func:`sleeve_equity` and can produce no TWT line —
   which is what stops this sleeve ever selling a holding it did not buy (``02`` Track C §5,
   non-negotiable 7's sibling).
3. **A sleeve at ₹0 plans nothing.** Every signal is skipped ``NO_SLEEVE_CAPITAL``. This is the
   rail that keeps the sleeve safe until Maulik enters the capital himself on the first live
   morning, and the run never sets that number.
4. **The countdown is a counter, not a flag.** It moves once per *filled* entry: not on a
   proposed line, not on a ``DRY_RUN`` plan being built, and not twice for the same fill.

The arithmetic itself is tested without a database in ``packages/core/tests/test_twt_sleeve.py``
and ``test_twt_sizing.py``; this file tests the **wiring**, which is where all four claims above
actually live. It sits in the worker tree although the modules it tests are ``baskfy_api``'s,
because that is where a per-test database fixture exists — and because the reader of this loader
who matters most is the evening job, which is the worker's.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from helpers import add_bar, make_instrument, requires_db
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.twt_settings import (
    EDITABLE_FIELDS,
    SYSTEM_OWNED_FIELDS,
    TwtConfigPatch,
    read_config,
)
from baskfy_api.twt_sleeve import (
    FIRST_LIVE_FIELD,
    TWT_PORTFOLIO_KIND,
    TWT_PORTFOLIO_SOURCE,
    book_state,
    cash_available,
    config_for,
    count_first_live_entry,
    held_instrument_ids,
    load_sleeve,
    sleeve_equity,
    slot_multiplier,
)
from baskfy_core.allocation_ledger import PortfolioKind, PortfolioSource
from baskfy_core.models import AppUser, TwConfig, TwConfigAudit, TwPosition, TwSession, VbPosition
from baskfy_core.twt.config import DEFAULT_TWT_CONFIG, Gate
from baskfy_core.twt.plan import (
    Candidate,
    PlanLine,
    Skipped,
    SkipReason,
    build_entries,
    exit_lines,
)
from baskfy_core.twt.sizing import SizeCap, size_entry

pytestmark = requires_db

AS_OF = dt.date(2026, 9, 10)
NOW = dt.datetime(2026, 9, 11, 9, 20, tzinfo=dt.UTC)

#: ``04`` §3.5's own worked example: ten slots at ₹25 lakh is a ₹2.5 lakh line, which is the
#: capital the 1 %-of-turnover cap's argument is made at.
TWENTY_FIVE_LAKH = Decimal("2500000.00")
ONE_CRORE = Decimal("10000000.00")
#: A name turning over ₹2 crore a day — the research's old floor, and the one the cap binds on.
TWO_CRORE_TURNOVER = Decimal("20000000")
#: And one turning over ₹50 crore, where a ₹2.5 lakh line is a fiftieth of the cap.
FIFTY_CRORE_TURNOVER = Decimal("500000000")


async def _user(
    session: AsyncSession,
    tag: str,
    *,
    capital: Decimal = TWENTY_FIVE_LAKH,
    max_position_pct: str = "12.50",
    first_live_entries_left: int = 10,
) -> int:
    """A user with a seeded ``tw_config``. **Capital is a fixture's, never the run's.**"""
    user = AppUser(public_id=f"tw5-{tag}", email=f"tw5-{tag}@example.com")
    session.add(user)
    await session.flush()
    session.add(
        TwConfig(
            user_id=user.id,
            sleeve_capital_inr=capital,
            max_open_positions=10,
            max_position_pct=Decimal(max_position_pct),
            stop_pct=Decimal("20.00"),
            trail_pct=Decimal("20.00"),
            first_live_entries_left=first_live_entries_left,
            dry_run_sessions=0,
            updated_by="test",
        )
    )
    await session.flush()
    return int(user.id)


async def _unseeded_user(session: AsyncSession, tag: str) -> int:
    """A user whose ``tw_config`` row does not exist — a system nobody has run ``make seed`` on."""
    user = AppUser(public_id=f"tw5-{tag}", email=f"tw5-{tag}@example.com")
    session.add(user)
    await session.flush()
    return int(user.id)


async def _position(  # noqa: PLR0913 - a book row is its numbers
    session: AsyncSession,
    user_id: int,
    instrument_id: int,
    *,
    quantity: int = 1_000,
    entry: str = "100.00",
    state: str = "OPEN",
    pnl: str | None = None,
    simulated: bool = False,
    half_size: bool = False,
    gtt_id: str | None = None,
    gtt_trigger: str | None = None,
    next_trigger: str | None = None,
    next_trigger_for: dt.date | None = None,
) -> int:
    entry_avg = Decimal(entry)
    position = TwPosition(
        user_id=user_id,
        instrument_id=instrument_id,
        signal_date=AS_OF,
        entry_date=AS_OF,
        entry_avg=entry_avg,
        entry_adj_factor=Decimal(1),
        quantity_entered=quantity,
        quantity_open=quantity if state == "OPEN" else 0,
        initial_stop=entry_avg * Decimal("0.8"),
        stop_price=entry_avg * Decimal("0.8"),
        high_since=entry_avg,
        high_since_date=AS_OF,
        gtt_id=gtt_id,
        gtt_trigger=None if gtt_trigger is None else Decimal(gtt_trigger),
        next_trigger=None if next_trigger is None else Decimal(next_trigger),
        next_trigger_for=next_trigger_for,
        state=state,
        closed_on=None if state == "OPEN" else AS_OF,
        pnl_inr=None if pnl is None else Decimal(pnl),
        simulated=simulated,
        half_size=half_size,
    )
    session.add(position)
    await session.flush()
    return int(position.id)


def _candidate(
    instrument_id: int,
    symbol: str,
    *,
    turnover: Decimal = FIFTY_CRORE_TURNOVER,
    price: str = "100.00",
) -> Candidate:
    return Candidate(
        instrument_id=instrument_id,
        symbol=symbol,
        signal_date=AS_OF,
        rank_key=turnover,
        turnover_avg_inr=turnover,
        entry_price=Decimal(price),
        stop_reference_price=Decimal(price),
    )


async def _lines_for(
    session: AsyncSession, user_id: int, candidates: list[Candidate]
) -> tuple[list[PlanLine], list[Skipped]]:
    """Size a plan the way TW6 will: the sleeve's own equity, the sleeve's own book, and the
    person's own ``tw_config`` settings reaching the arithmetic through :func:`config_for`."""
    row = await read_config(session, user_id)
    sleeve = await load_sleeve(session, user_id, AS_OF)
    lines, skips = build_entries(
        candidates,
        gate=Gate.OPEN,
        equity=sleeve.equity_inr,
        book=await book_state(session, user_id=user_id, as_of=AS_OF),
        config=config_for(row),
        max_open_positions=row.max_open_positions,
    )
    return list(lines), list(skips)


# ---------------------------------------------------------------------------
# G1 — the sleeve's own cash
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestTheSleeveReadsItsOwnRows:
    async def test_sleeve_equity_of_an_untouched_sleeve_is_its_capital(
        self, session: AsyncSession
    ) -> None:
        user_id = await _user(session, "untouched")
        assert await sleeve_equity(session, user_id, AS_OF) == TWENTY_FIVE_LAKH
        assert await cash_available(session, user_id, AS_OF) == TWENTY_FIVE_LAKH

    async def test_sleeve_equity_is_capital_plus_realised_plus_the_marked_open(
        self, session: AsyncSession
    ) -> None:
        """``04`` §9.1, from rows: a closed position's P&L is cash, an open one is its mark."""
        user_id = await _user(session, "equity")
        held = await make_instrument(session, "TW5HELD")
        sold = await make_instrument(session, "TW5SOLD")
        await add_bar(session, held, AS_OF, "120.00")
        await _position(session, user_id, held, quantity=1_000, entry="100.00")
        await _position(session, user_id, sold, state="CLOSED", pnl="40000.00")

        sleeve = await load_sleeve(session, user_id, AS_OF)
        # cost 1,00,000 out, 1,20,000 back on the mark, plus 40,000 realised.
        assert sleeve.value.cost_of_open_inr == Decimal("100000.00")
        assert sleeve.value.value_of_open_inr == Decimal("120000.00")
        assert sleeve.value.realised_inr == Decimal("40000.00")
        # capital 25,00,000 + realised 40,000 - cost 1,00,000 + mark 1,20,000.
        assert sleeve.equity_inr == Decimal("2560000.00")

    async def test_cash_available_is_equity_less_the_value_of_the_open_positions(
        self, session: AsyncSession
    ) -> None:
        """``04`` §9.2. **There are no working orders in this sleeve to reserve against**
        (``03`` §6), which is the one place its arithmetic differs from VBT-1's."""
        user_id = await _user(session, "cash")
        held = await make_instrument(session, "TW5CASH")
        await add_bar(session, held, AS_OF, "150.00")
        await _position(session, user_id, held, quantity=1_000, entry="100.00")

        sleeve = await load_sleeve(session, user_id, AS_OF)
        assert sleeve.equity_inr == Decimal("2550000.00")
        assert sleeve.cash_available_inr == Decimal("2400000.00")
        assert sleeve.cash_available_inr == sleeve.value.cash_inr

    async def test_cash_available_is_never_negative(self, session: AsyncSession) -> None:
        """A book cannot un-commit money it has already spent."""
        user_id = await _user(session, "underwater", capital=Decimal("100000.00"))
        held = await make_instrument(session, "TW5UNDER")
        await add_bar(session, held, AS_OF, "400.00")
        await _position(session, user_id, held, quantity=1_000, entry="100.00")

        assert await cash_available(session, user_id, AS_OF) == Decimal(0)

    async def test_sleeve_equity_of_an_unseeded_sleeve_is_zero(self, session: AsyncSession) -> None:
        """DECISIONS-TW **TW5.1**: a loader answers ₹0 where a read path raises. The two states
        produce the same plan — one with every signal skipped ``NO_SLEEVE_CAPITAL``."""
        user_id = await _unseeded_user(session, "unseeded")
        assert await sleeve_equity(session, user_id, AS_OF) == Decimal(0)
        assert await cash_available(session, user_id, AS_OF) == Decimal(0)


# ---------------------------------------------------------------------------
# G2 — it owns only what it bought
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestItNeverSellsWhatItDidNotBuy:
    async def test_a_name_the_sleeve_never_bought_is_invisible_to_sleeve_equity(
        self, session: AsyncSession
    ) -> None:
        """The bar exists, the instrument exists, **another book holds it** — and this book's
        equity does not move by a paisa (``02`` Track C §5)."""
        user_id = await _user(session, "not-ours")
        foreign = await make_instrument(session, "TW5FOREIGN")
        await add_bar(session, foreign, AS_OF, "500.00")
        session.add(
            VbPosition(
                user_id=user_id,
                instrument_id=foreign,
                entry_date=AS_OF,
                entry_avg=Decimal("400.00"),
                quantity_entered=1_000,
                quantity_open=1_000,
                initial_stop=Decimal("350.00"),
                stop_price=Decimal("350.00"),
                state="OPEN",
            )
        )
        await session.flush()

        assert await sleeve_equity(session, user_id, AS_OF) == TWENTY_FIVE_LAKH
        assert await held_instrument_ids(session, user_id) == frozenset()

    async def test_a_name_the_sleeve_never_bought_produces_no_line(
        self, session: AsyncSession
    ) -> None:
        """``04`` §10.2 can emit no ``SELL_AT_OPEN`` at all, and it is never even offered one:
        the book it reads is ``tw_position``, so a foreign holding is not in it to be sold."""
        user_id = await _user(session, "no-line")
        foreign = await make_instrument(session, "TW5NOLINE")
        await add_bar(session, foreign, AS_OF, "500.00")
        session.add(
            VbPosition(
                user_id=user_id,
                instrument_id=foreign,
                entry_date=AS_OF,
                entry_avg=Decimal("400.00"),
                quantity_entered=1_000,
                quantity_open=1_000,
                initial_stop=Decimal("350.00"),
                stop_price=Decimal("350.00"),
                state="OPEN",
            )
        )
        await session.flush()

        book = await book_state(session, user_id=user_id, as_of=AS_OF)
        assert book.open_instrument_ids == frozenset()
        assert book.positions_naked_of_gtt == ()
        assert book.ratchets_due == ()
        assert exit_lines(book, AS_OF) == []

    async def test_a_closed_position_is_not_ours_to_mark_but_its_profit_is_kept(
        self, session: AsyncSession
    ) -> None:
        """A ``CLOSED`` row is realised cash and never an open slot."""
        user_id = await _user(session, "closed")
        gone = await make_instrument(session, "TW5GONE")
        await add_bar(session, gone, AS_OF, "900.00")
        await _position(session, user_id, gone, state="CLOSED", pnl="12345.00")

        sleeve = await load_sleeve(session, user_id, AS_OF)
        assert sleeve.value.value_of_open_inr == Decimal(0)
        assert sleeve.equity_inr == TWENTY_FIVE_LAKH + Decimal("12345.00")
        assert await held_instrument_ids(session, user_id) == frozenset()


# ---------------------------------------------------------------------------
# G3 — a sleeve at ₹0 plans nothing
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestASleeveAtZeroPlansNothing:
    async def test_a_sleeve_at_zero_capital_skips_every_signal_no_sleeve_capital(
        self, session: AsyncSession
    ) -> None:
        """``04`` §9.3, and it is the rail the whole sleeve waits behind: ``tw_config`` is seeded
        at ₹0 and **nothing in this repository sets it**."""
        user_id = await _user(session, "zero-capital", capital=Decimal("0.00"))
        names = [await make_instrument(session, f"TW5ZERO{n}") for n in range(3)]
        candidates = [
            _candidate(instrument_id, f"TW5ZERO{n}") for n, instrument_id in enumerate(names)
        ]

        lines, skips = await _lines_for(session, user_id, candidates)
        assert lines == []
        assert [skip.reason for skip in skips] == [SkipReason.NO_SLEEVE_CAPITAL] * 3

    async def test_an_unseeded_sleeve_also_skips_no_sleeve_capital(
        self, session: AsyncSession
    ) -> None:
        """The unseeded sleeve and the seeded-at-zero one must be the same plan, or "not
        configured" becomes a state with its own behaviour nobody specified."""
        user_id = await _unseeded_user(session, "zero-unseeded")
        name = await make_instrument(session, "TW5UNSEED")
        sleeve = await load_sleeve(session, user_id, AS_OF)
        lines, skips = build_entries(
            [_candidate(name, "TW5UNSEED")],
            gate=Gate.OPEN,
            equity=sleeve.equity_inr,
            book=await book_state(session, user_id=user_id, as_of=AS_OF),
            config=DEFAULT_TWT_CONFIG,
        )
        assert lines == []
        assert [skip.reason for skip in skips] == [SkipReason.NO_SLEEVE_CAPITAL]

    async def test_a_zero_capital_sleeve_still_has_no_line_for_a_name_it_holds(
        self, session: AsyncSession
    ) -> None:
        """Zero capital does not make a held position disappear; it makes new money impossible."""
        user_id = await _user(session, "zero-held", capital=Decimal("0.00"))
        held = await make_instrument(session, "TW5ZEROHELD")
        await add_bar(session, held, AS_OF, "100.00")
        await _position(session, user_id, held, quantity=10, entry="100.00")

        lines, skips = await _lines_for(session, user_id, [_candidate(held, "TW5ZEROHELD")])
        assert lines == []
        assert [skip.reason for skip in skips] == [SkipReason.ALREADY_HELD]


# ---------------------------------------------------------------------------
# G4 — the caps, in order
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestTheCapsAreWired:
    async def test_the_turnover_cap_binds_on_a_two_crore_name_at_twenty_five_lakh(
        self, session: AsyncSession
    ) -> None:
        """``04`` §3.5's argument for the ₹5 crore floor, as a test.

        Ten slots at ₹25 lakh is a ₹2.5 lakh line, and 1 % of a ₹2 crore day is ₹2 lakh — so on
        such a name **the cap, not the strategy, decides the size**, and the book would be
        systematically under-sized in exactly the names the research's ₹2 crore floor admitted.

        Both halves are asserted, because together they are the argument: the cap does bind at
        ₹2 crore, **and** the shipped sleeve never lines the name at all, because
        `min_turnover_inr` is ₹5 crore. A floor below the cap is a plan sized by the cap.
        """
        user_id = await _user(session, "cap-binds")
        thin = await make_instrument(session, "TW5THIN")
        sleeve = await load_sleeve(session, user_id, AS_OF)

        sized = size_entry(
            equity=sleeve.equity_inr,
            cash_available=sleeve.cash_available_inr,
            entry_price=Decimal("100.00"),
            stop_price=Decimal("80.00"),
            turnover_avg_inr=TWO_CRORE_TURNOVER,
            config=config_for(await read_config(session, user_id)).sizing,
        )
        assert sized.cap is SizeCap.TURNOVER
        assert sized.turnover_capped is True
        assert sized.value_inr == Decimal("200000.00")
        assert sized.quantity == 2_000

        lines, skips = await _lines_for(
            session, user_id, [_candidate(thin, "TW5THIN", turnover=TWO_CRORE_TURNOVER)]
        )
        assert lines == []
        assert [skip.reason for skip in skips] == [SkipReason.BELOW_LIQUIDITY_FLOOR]

    async def test_the_turnover_cap_does_not_bind_on_a_fifty_crore_name(
        self, session: AsyncSession
    ) -> None:
        """The other half of the same argument: at ₹50 crore a day the line is the slot."""
        user_id = await _user(session, "cap-free")
        deep = await make_instrument(session, "TW5DEEP")

        lines, skips = await _lines_for(
            session, user_id, [_candidate(deep, "TW5DEEP", turnover=FIFTY_CRORE_TURNOVER)]
        )
        assert skips == []
        assert len(lines) == 1
        assert lines[0].value_inr == Decimal("250000.00")
        assert lines[0].quantity == 2_500
        assert lines[0].cap is SizeCap.SLOT

    async def test_the_caps_in_order_are_the_position_ceiling_then_turnover_then_cash(
        self, session: AsyncSession
    ) -> None:
        """``04`` §6.2's order, all three binding at once, on a sleeve whose money is committed.

        The per-position ceiling comes from ``tw_config.max_position_pct`` — the person's own
        setting, reaching the arithmetic through :func:`config_for` — which is what makes this a
        test of the *wiring* and not of ``size_entry``.
        """
        user_id = await _user(session, "caps-order", capital=ONE_CRORE, max_position_pct="8.00")
        held = await make_instrument(session, "TW5COMMIT")
        fresh = await make_instrument(session, "TW5FRESH")
        await add_bar(session, held, AS_OF, "100.00")
        await _position(session, user_id, held, quantity=95_000, entry="100.00")

        lines, skips = await _lines_for(
            session, user_id, [_candidate(fresh, "TW5FRESH", turnover=Decimal("55000000"))]
        )
        assert skips == []
        assert lines[0].cap is SizeCap.CASH
        # The slot is ₹10 lakh; 8 % of equity is ₹8 lakh; 1 % of turnover is ₹5.5 lakh; the cash
        # left after a ₹95 lakh position is ₹5 lakh. Each lowers the one before it.
        assert lines[0].value_inr == Decimal("500000.00")

    async def test_caps_in_order_end_at_the_ten_thousand_floor(self, session: AsyncSession) -> None:
        """``04`` §6.2's last row: below ₹10,000 the brokerage dominates the edge, so a line that
        cannot clear it is skipped rather than shrunk."""
        user_id = await _user(session, "floor")
        held = await make_instrument(session, "TW5NEARLY")
        fresh = await make_instrument(session, "TW5TINY")
        await add_bar(session, held, AS_OF, "100.00")
        await _position(session, user_id, held, quantity=24_950, entry="100.00")

        lines, skips = await _lines_for(session, user_id, [_candidate(fresh, "TW5TINY")])
        assert lines == []
        assert [skip.reason for skip in skips] == [SkipReason.BELOW_MIN_TRADE_VALUE]

    async def test_cash_spent_by_an_earlier_line_of_the_same_plan_is_not_spent_twice(
        self, session: AsyncSession
    ) -> None:
        """``04`` §10.1's closing sentence, from real rows."""
        user_id = await _user(session, "one-purse", capital=Decimal("300000.00"))
        first = await make_instrument(session, "TW5ONEA")
        second = await make_instrument(session, "TW5ONEB")

        lines, skips = await _lines_for(
            session,
            user_id,
            [
                _candidate(first, "TW5ONEA", turnover=Decimal("600000000")),
                _candidate(second, "TW5ONEB", turnover=Decimal("500000000")),
            ],
        )
        # The slot is ₹30,000 and the sleeve has ₹3 lakh, so both fit — and the second is sized
        # against what the first left, which is the property being asserted.
        assert [line.value_inr for line in lines] == [Decimal("30000.00"), Decimal("30000.00")]
        assert skips == []


# ---------------------------------------------------------------------------
# G5 — the counter, and it is a counter
# ---------------------------------------------------------------------------


async def _entries_left(session: AsyncSession, user_id: int) -> int:
    row = await read_config(session, user_id)
    await session.refresh(row)
    return int(row.first_live_entries_left)


async def _counted_on_session(session: AsyncSession, user_id: int) -> int:
    row = (
        await session.execute(
            select(TwSession).where(TwSession.user_id == user_id, TwSession.session_date == AS_OF)
        )
    ).scalar_one_or_none()
    if row is None:
        return 0
    await session.refresh(row)
    return int(row.first_live_entries_counted)


@pytest.mark.db
class TestTheFirstLiveCounter:
    async def test_the_counter_decrements_once_on_a_filled_entry(
        self, session: AsyncSession
    ) -> None:
        """``04`` §6.4: once per **filled** entry, by the session that filled it."""
        user_id = await _user(session, "count-one")
        name = await make_instrument(session, "TW5COUNT")
        position_id = await _position(session, user_id, name, simulated=False)

        outcome = await count_first_live_entry(
            session,
            user_id=user_id,
            position_id=position_id,
            session_date=AS_OF,
            now=NOW,
            execution_enabled=True,
        )
        assert outcome.counted is True
        assert outcome.half_size is True
        assert outcome.entries_left == 9
        assert await _entries_left(session, user_id) == 9
        assert await _counted_on_session(session, user_id) == 1

    async def test_the_counter_records_half_size_on_the_position(
        self, session: AsyncSession
    ) -> None:
        """``03`` §5's ``half_size`` is the record of which lines the countdown applied to."""
        user_id = await _user(session, "count-half")
        name = await make_instrument(session, "TW5HALF")
        position_id = await _position(session, user_id, name)

        await count_first_live_entry(
            session,
            user_id=user_id,
            position_id=position_id,
            session_date=AS_OF,
            now=NOW,
            execution_enabled=True,
        )
        position = (
            await session.execute(select(TwPosition).where(TwPosition.id == position_id))
        ).scalar_one()
        await session.refresh(position)
        assert position.half_size is True

    async def test_the_counter_does_not_move_twice_for_the_same_fill(
        self, session: AsyncSession
    ) -> None:
        """A partial fill that completes later, a retried confirm and a re-run of the morning all
        land on the same answer (house rule 7)."""
        user_id = await _user(session, "count-twice")
        name = await make_instrument(session, "TW5TWICE")
        position_id = await _position(session, user_id, name)

        for _ in range(3):
            outcome = await count_first_live_entry(
                session,
                user_id=user_id,
                position_id=position_id,
                session_date=AS_OF,
                now=NOW,
                execution_enabled=True,
            )
        assert outcome.counted is False
        assert outcome.half_size is True
        assert await _entries_left(session, user_id) == 9
        assert await _counted_on_session(session, user_id) == 1

    async def test_the_counter_does_not_move_on_a_dry_run_fill(self, session: AsyncSession) -> None:
        """``04`` §6.4: a ``DRY_RUN`` plan is **full size**. Half size is a live-money discipline,
        and a paper plan that is not the plan is not a rehearsal."""
        user_id = await _user(session, "count-dry")
        name = await make_instrument(session, "TW5DRY")
        position_id = await _position(session, user_id, name, simulated=True)

        outcome = await count_first_live_entry(
            session,
            user_id=user_id,
            position_id=position_id,
            session_date=AS_OF,
            now=NOW,
            execution_enabled=True,
        )
        assert outcome.counted is False
        assert outcome.half_size is False
        assert await _entries_left(session, user_id) == 10
        assert await _counted_on_session(session, user_id) == 0

    async def test_the_counter_does_not_move_while_execution_is_disabled(
        self, session: AsyncSession
    ) -> None:
        """``BASKFY_TWT_EXECUTION_ENABLED`` is false, and a flag-off fill is not a live entry."""
        user_id = await _user(session, "count-flagoff")
        name = await make_instrument(session, "TW5FLAGOFF")
        position_id = await _position(session, user_id, name, simulated=False)

        outcome = await count_first_live_entry(
            session,
            user_id=user_id,
            position_id=position_id,
            session_date=AS_OF,
            now=NOW,
            execution_enabled=False,
        )
        assert outcome.counted is False
        assert await _entries_left(session, user_id) == 10

    async def test_the_counter_does_not_move_on_a_proposed_line(
        self, session: AsyncSession
    ) -> None:
        """A proposed line has no ``tw_position`` row, so it cannot be counted even by mistake:
        the parameter is a position id, and a proposal has none to give."""
        user_id = await _user(session, "count-proposed")
        with pytest.raises(LookupError, match="a filled entry is a row"):
            await count_first_live_entry(
                session,
                user_id=user_id,
                position_id=10_000_000,
                session_date=AS_OF,
                now=NOW,
                execution_enabled=True,
            )
        assert await _entries_left(session, user_id) == 10

    async def test_the_counter_does_not_move_for_another_users_position(
        self, session: AsyncSession
    ) -> None:
        """P4.1: a row is a tenant's. Another user's fill is not this sleeve's entry."""
        owner = await _user(session, "count-owner")
        stranger = await _user(session, "count-stranger")
        name = await make_instrument(session, "TW5TENANT")
        position_id = await _position(session, owner, name)

        with pytest.raises(LookupError):
            await count_first_live_entry(
                session,
                user_id=stranger,
                position_id=position_id,
                session_date=AS_OF,
                now=NOW,
                execution_enabled=True,
            )
        assert await _entries_left(session, owner) == 10

    async def test_the_counter_stops_at_zero_and_the_next_entry_is_full_size(
        self, session: AsyncSession
    ) -> None:
        """Ten entries, not eleven, and the eleventh is sized at a whole slot."""
        user_id = await _user(session, "count-spent", first_live_entries_left=0)
        name = await make_instrument(session, "TW5SPENT")
        position_id = await _position(session, user_id, name)

        outcome = await count_first_live_entry(
            session,
            user_id=user_id,
            position_id=position_id,
            session_date=AS_OF,
            now=NOW,
            execution_enabled=True,
        )
        assert outcome.counted is False
        assert outcome.half_size is False
        assert await _entries_left(session, user_id) == 0
        assert await slot_multiplier(session, user_id=user_id, execution_enabled=True) == Decimal(1)

    async def test_the_counter_writes_an_audit_row_naming_who_counted_it(
        self, session: AsyncSession
    ) -> None:
        """``03`` §1b: "who counted that entry" has to have an answer."""
        user_id = await _user(session, "count-audit")
        name = await make_instrument(session, "TW5AUDIT")
        position_id = await _position(session, user_id, name)

        await count_first_live_entry(
            session,
            user_id=user_id,
            position_id=position_id,
            session_date=AS_OF,
            now=NOW,
            execution_enabled=True,
        )
        rows = list(
            (
                await session.execute(
                    select(TwConfigAudit).where(
                        TwConfigAudit.user_id == user_id,
                        TwConfigAudit.key == FIRST_LIVE_FIELD,
                    )
                )
            ).scalars()
        )
        assert len(rows) == 1
        assert rows[0].old_value == "10"
        assert rows[0].new_value == "9"
        assert rows[0].changed_by == "twt-fill"

    async def test_first_live_half_size_applies_only_while_live_and_counting(
        self, session: AsyncSession
    ) -> None:
        """``04`` §6.4's multiplier, read from the sleeve's own countdown."""
        user_id = await _user(session, "count-multiplier")
        assert await slot_multiplier(session, user_id=user_id, execution_enabled=True) == Decimal(
            "0.5"
        )
        assert await slot_multiplier(session, user_id=user_id, execution_enabled=False) == Decimal(
            1
        )

    async def test_the_first_live_counter_is_not_a_flag_anyone_can_turn_off(
        self, session: AsyncSession
    ) -> None:
        """The countdown is a counter **in the sleeve**, and there is no switch for it: it is not
        an editable field, it is system-owned, and a patch naming it is refused rather than
        silently dropped — a caller believing they had skipped the discipline by asking is the
        failure this prevents."""
        assert FIRST_LIVE_FIELD not in EDITABLE_FIELDS
        assert FIRST_LIVE_FIELD in SYSTEM_OWNED_FIELDS
        with pytest.raises(ValueError, match=FIRST_LIVE_FIELD):
            TwtConfigPatch(**{FIRST_LIVE_FIELD: 0})


# ---------------------------------------------------------------------------
# G6 — a mark that is not this session's close says so
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestTheMarkSaysWhereItCameFrom:
    async def test_the_mark_is_the_latest_close_on_or_before_the_session(
        self, session: AsyncSession
    ) -> None:
        """Never the latest row: a mark from after the session being reported is a look-ahead in
        the one place it would flatter the book — its own value (house rule 5)."""
        user_id = await _user(session, "mark-onorbefore")
        name = await make_instrument(session, "TW5MARK")
        await add_bar(session, name, AS_OF - dt.timedelta(days=1), "90.00")
        await add_bar(session, name, AS_OF, "110.00")
        await add_bar(session, name, AS_OF + dt.timedelta(days=1), "999.00")
        await _position(session, user_id, name, quantity=100, entry="100.00")

        sleeve = await load_sleeve(session, user_id, AS_OF)
        assert sleeve.value.value_of_open_inr == Decimal("11000.00")
        assert sleeve.stale_marks == ()
        assert sleeve.mark_notes() == {}

    async def test_a_position_with_no_bar_on_the_session_falls_back_to_the_last_close(
        self, session: AsyncSession
    ) -> None:
        """``04`` §9.1: the fallback is a number **and a sentence**. Never a zero, never a silent
        gap — a book that quietly excluded a suspended holding would report an equity it does not
        have, and ``04`` §7.5's five-session write-off is what eventually removes it."""
        user_id = await _user(session, "mark-nobar")
        name = await make_instrument(session, "TW5NOBAR")
        await add_bar(session, name, AS_OF - dt.timedelta(days=4), "80.00")
        await _position(session, user_id, name, quantity=100, entry="100.00")

        sleeve = await load_sleeve(session, user_id, AS_OF)
        assert sleeve.value.value_of_open_inr == Decimal("8000.00")
        assert len(sleeve.stale_marks) == 1
        note = sleeve.mark_notes()[name]
        assert "no bar on 2026-09-10" in note
        assert "2026-09-06" in note
        assert "last known close" in note

    async def test_a_name_that_never_printed_since_the_fill_is_marked_at_the_entry_and_says_so(
        self, session: AsyncSession
    ) -> None:
        """Nothing has printed at all: the entry is the only honest mark, and the note says it."""
        user_id = await _user(session, "mark-entry")
        name = await make_instrument(session, "TW5NEVER")
        await _position(session, user_id, name, quantity=100, entry="100.00")

        sleeve = await load_sleeve(session, user_id, AS_OF)
        assert sleeve.value.value_of_open_inr == Decimal("10000.00")
        assert "nothing has printed since the fill" in sleeve.mark_notes()[name]

    async def test_a_stale_mark_is_never_a_zero(self, session: AsyncSession) -> None:
        """The failure this rule exists to prevent: a missing bar read as a worthless position."""
        user_id = await _user(session, "mark-nonzero")
        name = await make_instrument(session, "TW5NONZERO")
        await _position(session, user_id, name, quantity=100, entry="250.00")

        assert await sleeve_equity(session, user_id, AS_OF) == TWENTY_FIVE_LAKH


# ---------------------------------------------------------------------------
# G7 — the sleeve is a MY_STRATEGY capital portfolio
# ---------------------------------------------------------------------------


@pytest.mark.db
class TestTheSleeveIsAMyStrategyCapitalPortfolio:
    def test_the_sleeve_declares_itself_a_my_strategy_capital_portfolio(self) -> None:
        """``04`` §9.3, in the M34 / ``PORTFOLIO_REDESIGN`` sense, exactly as the swing and VBT
        sleeves are. **Not a ``HOLDING_GROUP``**: that source means "shares you already own,
        grouped after the fact" and would give this sleeve "since grouped" as its headline metric
        when it knows the real entry date of every position it opened."""
        assert TWT_PORTFOLIO_KIND is PortfolioKind.CAPITAL
        assert TWT_PORTFOLIO_SOURCE is PortfolioSource.MY_STRATEGY

    async def test_a_capital_portfolio_of_the_account_cannot_move_the_sleeve(
        self, session: AsyncSession
    ) -> None:
        """The whole of ``04`` §9.3's second sentence: **it never reads the account's total
        holdings.** Three other books hold four names between them and this sleeve's equity is
        still exactly the capital somebody typed into its own settings row."""
        user_id = await _user(session, "portfolio")
        for index in range(4):
            other = await make_instrument(session, f"TW5ACCOUNT{index}")
            await add_bar(session, other, AS_OF, "1000.00")
            session.add(
                VbPosition(
                    user_id=user_id,
                    instrument_id=other,
                    entry_date=AS_OF,
                    entry_avg=Decimal("900.00"),
                    quantity_entered=500,
                    quantity_open=500,
                    initial_stop=Decimal("800.00"),
                    stop_price=Decimal("800.00"),
                    state="OPEN",
                )
            )
        await session.flush()

        assert await sleeve_equity(session, user_id, AS_OF) == TWENTY_FIVE_LAKH
        assert await cash_available(session, user_id, AS_OF) == TWENTY_FIVE_LAKH
        book = await book_state(session, user_id=user_id, as_of=AS_OF)
        assert book.slots_taken == 0
        assert exit_lines(book, AS_OF) == []
