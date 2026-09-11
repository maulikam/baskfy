"""The three-weeks-tight sleeve's own cash, its own book, and its half-size counter (TW5).

The arithmetic is ``baskfy_core.twt.sleeve`` and ``baskfy_core.twt.sizing``, stated once there so
the evening job, the desk page and the confirm path cannot each derive a slightly different
equity. **This module is the part that reads rows**, and it lives in the API package because both
sides need it — the same place ``twt_settings`` and ``vbt_sleeve`` sit, for the same reason.

THREE RULES THIS MODULE EXISTS TO KEEP
--------------------------------------
**It sizes against the sleeve's own money.** ``04`` §9.3: the sleeve is a ``MY_STRATEGY`` capital
portfolio in the M34 / ``PORTFOLIO_REDESIGN`` sense (:data:`TWT_PORTFOLIO_SOURCE`,
:data:`TWT_PORTFOLIO_KIND`), and it never reads the account's total holdings. A deposit, a Friday
rebalance, a swing entry or a name the user grouped by hand cannot move a rupee of
:func:`sleeve_equity`.

**It owns only what it bought.** ``tw_position`` is the book and nothing else is (``03`` §5,
``02`` Track C §5). A holding in the broker's account that this sleeve never bought is invisible
here, so it can never be marked, never be counted as a slot, and — because ``exit_lines`` can only
speak about rows this module returns — never become a sell line. This is non-negotiable 7's
sibling, and it is the same discipline that keeps the swing book from selling the weekly book's
shares.

**A sleeve at ₹0 plans nothing.** ``tw_config.sleeve_capital_inr`` is seeded at zero and nothing in
this repository sets it (root ``CLAUDE.md``'s safety rails, ``02`` §3). Zero equity makes
:func:`baskfy_core.twt.plan.build_entries` skip every signal ``NO_SLEEVE_CAPITAL``, which is the
rail that keeps the sleeve safe until Maulik enters the capital himself on the first live morning.

THE COUNTER, AND WHY IT IS A COUNTER
------------------------------------
``04`` §6.4's first ten live **entries** run at half size. :func:`slot_multiplier` is the sizing
half — it reads ``tw_config.first_live_entries_left`` and hands
:func:`baskfy_core.twt.sizing.first_live_multiplier` the number — and
:func:`count_first_live_entry` is the spending half, which moves the countdown **once per filled
entry**: not on a proposed line (the function takes a ``position_id``, and a proposal has no
position row), not on a ``DRY_RUN`` or flag-off fill (half size is a live-money discipline), and
not twice for the same fill (``tw_position.half_size`` is the record of which entries it applied
to, and a row already marked is already counted).

**Nothing about it is a switch.** ``TwtConfigPatch`` has no field for it and
``record_system_change`` is the only door, so the audit trail always names who counted an entry.

WHAT THIS MODULE DOES NOT DO
----------------------------
It places no order and writes no order path: ``BASKFY_TWT_EXECUTION_ENABLED`` is false, TW6 owns
the desk plan and ``POST /twt/execute``, and law #2 means the gateway is the only way to a broker.
:func:`book_state` assembles what ``build_entries`` and ``exit_lines`` read; the plan itself is
pure and stays that way.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.twt_settings import TwtConfigNotSeeded, read_config, record_system_change
from baskfy_core.allocation_ledger import PortfolioKind, PortfolioSource
from baskfy_core.models import Instrument, OhlcvDaily, TwConfig, TwOrder, TwPosition, TwSession
from baskfy_core.twt.config import DEFAULT_TWT_CONFIG, TwtConfig
from baskfy_core.twt.plan import BookState, NakedPosition, RatchetDue
from baskfy_core.twt.sizing import first_live_multiplier
from baskfy_core.twt.sleeve import (
    MarkSource,
    OpenPositionValue,
    SleeveValue,
    sleeve_value,
)

_ZERO = Decimal(0)
_ONE = Decimal(1)

#: ``tw_position.state`` — the two the book has (``03`` §5).
OPEN_STATE: Final = "OPEN"
CLOSED_STATE: Final = "CLOSED"

#: The sleeve's identity in the portfolio model (``04`` §9.3). It is a **capital** portfolio
#: because its value sums into net worth and its holdings are its own, and a ``MY_STRATEGY`` one
#: because the desk trades it — not a ``HOLDING_GROUP``, which means "shares you already own,
#: grouped after the fact" and would give it "since grouped" as its headline metric when this
#: sleeve knows the real entry date of every position it opened. Declared here, beside the money,
#: so the page that renders the sleeve and the loader that values it name one thing.
TWT_PORTFOLIO_NAME: Final = "Three weeks tight"
TWT_PORTFOLIO_KIND: Final = PortfolioKind.CAPITAL
TWT_PORTFOLIO_SOURCE: Final = PortfolioSource.MY_STRATEGY

#: ``04`` §6.3's "the session's already-confirmed or sent orders, whatever plan they came from".
#: A confirmed order has spoken for one of the session's three entries whether or not it has
#: printed yet, so a fourth confirm of an evening is a refusal rather than a surprise.
ENTERED_ORDER_STATES: Final[tuple[str, ...]] = ("CONFIRMED", "SENT", "PARTIAL", "FILLED")

#: The one ``tw_config`` field this module moves, and the only door it moves through
#: (:func:`baskfy_api.twt_settings.record_system_change`).
FIRST_LIVE_FIELD: Final = "first_live_entries_left"

#: Who the audit row says counted the entry. The evening job owns the *field*; the entry is
#: counted by the fill, so the trail says so rather than crediting a job that was not running.
FIRST_LIVE_COUNTED_BY: Final = "twt-fill"

_ALREADY_COUNTED: Final = "this entry was already counted; a fill is counted once"
_NOT_LIVE_MONEY: Final = "a DRY_RUN or flag-off fill is full size, and counts nothing"
_COUNTDOWN_SPENT: Final = "the first live entries are spent; this line was full size"
_COUNTED: Final = "a live entry filled at half size"


@dataclass(frozen=True, slots=True)
class Mark:
    """One instrument's published close, and the session it closed on.

    The date rides along because ``04`` §9.1 does not merely want a number: a position marked on
    a session the name did not trade **falls back to its last close and says so**, and "says so"
    needs the date it fell back to.
    """

    close: Decimal
    on: dt.date


@dataclass(frozen=True, slots=True)
class LoadedSleeve:
    """The sleeve's money as of a session, and where every mark came from."""

    as_of: dt.date
    value: SleeveValue
    positions: tuple[OpenPositionValue, ...]
    #: The session each position's mark was taken from; ``None`` when nothing has printed since
    #: the fill and the entry is the only honest mark.
    marked_on: Mapping[int, dt.date | None]

    @property
    def equity_inr(self) -> Decimal:
        return self.value.equity_inr

    @property
    def cash_available_inr(self) -> Decimal:
        return self.value.cash_available_inr

    @property
    def stale_marks(self) -> tuple[OpenPositionValue, ...]:
        """The positions whose mark is not this session's close — never a zero, never a gap."""
        return tuple(position for position in self.positions if position.is_stale_mark)

    def mark_notes(self) -> dict[int, str]:
        """What the plan's detail says about each stale mark (``04`` §9.1).

        Empty when every position printed on the session, which is the ordinary evening.
        """
        notes: dict[int, str] = {}
        for position in self.stale_marks:
            marked = self.marked_on.get(position.instrument_id)
            if position.mark_source is MarkSource.LAST_KNOWN_CLOSE and marked is not None:
                notes[position.instrument_id] = (
                    f"no bar on {self.as_of.isoformat()}; marked at the last known close of "
                    f"{position.mark}, from {marked.isoformat()}"
                )
            else:
                notes[position.instrument_id] = (
                    f"nothing has printed since the fill; marked at the entry of {position.mark}"
                )
        return notes


@dataclass(frozen=True, slots=True)
class FirstLiveEntry:
    """What :func:`count_first_live_entry` did, and why.

    ``counted`` is the countdown moving. ``half_size`` is what the position row now says about
    the entry — true only when this entry was one of the first ten live ones.
    """

    counted: bool
    half_size: bool
    entries_left: int
    reason: str


async def open_positions(session: AsyncSession, user_id: int) -> list[TwPosition]:
    """The sleeve's book: what it bought and still holds. **Never the account's holdings.**"""
    rows = await session.execute(
        select(TwPosition)
        .where(
            TwPosition.user_id == user_id,
            TwPosition.state == OPEN_STATE,
            TwPosition.quantity_open > 0,
        )
        .order_by(TwPosition.instrument_id, TwPosition.id)
    )
    return list(rows.scalars())


async def held_instrument_ids(session: AsyncSession, user_id: int) -> frozenset[int]:
    """What the sleeve owns — ``04`` §10.1's ``ALREADY_HELD``, and Track C §5's whole content."""
    return frozenset(row.instrument_id for row in await open_positions(session, user_id))


async def realised_pnl(
    session: AsyncSession, user_id: int, as_of: dt.date | None = None
) -> Decimal:
    """Σ ``pnl_inr`` over the sleeve's closed positions, up to and including ``as_of``."""
    statement = select(func.coalesce(func.sum(TwPosition.pnl_inr), 0)).where(
        TwPosition.user_id == user_id, TwPosition.state == CLOSED_STATE
    )
    if as_of is not None:
        statement = statement.where(TwPosition.closed_on <= as_of)
    return Decimal(str((await session.execute(statement)).scalar_one()))


async def marks(
    session: AsyncSession, instrument_ids: Sequence[int], as_of: dt.date
) -> dict[int, Mark]:
    """The latest published close **on or before** ``as_of``, per instrument, with its session.

    On or before, never the latest row: a position marked at a bar from after the session being
    reported would be a look-ahead in the one place it would flatter the book — its own value
    (house rule 5).
    """
    if not instrument_ids:
        return {}
    latest = (
        select(
            OhlcvDaily.instrument_id,
            func.max(OhlcvDaily.date).label("date"),
        )
        .where(OhlcvDaily.instrument_id.in_(instrument_ids), OhlcvDaily.date <= as_of)
        .group_by(OhlcvDaily.instrument_id)
        .subquery()
    )
    rows = await session.execute(
        select(OhlcvDaily.instrument_id, OhlcvDaily.date, OhlcvDaily.close).join(
            latest,
            (OhlcvDaily.instrument_id == latest.c.instrument_id)
            & (OhlcvDaily.date == latest.c.date),
        )
    )
    return {
        int(instrument_id): Mark(close=Decimal(str(close)), on=on)
        for instrument_id, on, close in rows
    }


async def sleeve_capital(session: AsyncSession, user_id: int) -> Decimal:
    """``tw_config.sleeve_capital_inr``, or ₹0 when the row has not been seeded.

    Zero rather than a raised error, although :func:`baskfy_api.twt_settings.read_config` raises:
    a **read path** that must answer a person is right to say "nobody has seeded this yet", and a
    **loader** is right to say ₹0, because the two states produce the same plan — one with every
    signal skipped ``NO_SLEEVE_CAPITAL``. An evening job that crashed on an unseeded row would
    lose the session's stored signals to protect a number that is zero either way.
    DECISIONS-TW **TW5.1**.
    """
    try:
        row = await read_config(session, user_id)
    except TwtConfigNotSeeded:
        return _ZERO
    return Decimal(str(row.sleeve_capital_inr))


async def load_sleeve(session: AsyncSession, user_id: int, as_of: dt.date) -> LoadedSleeve:
    """The sleeve's money as of a session, from its own rows and nothing else (``04`` §9.1).

    A position whose instrument did not print on the session is marked at its **last known
    close** and the fallback is recorded, not hidden; one that has not printed at all since the
    fill is marked at the **entry**. Neither is dropped and neither is zero: a book that quietly
    excluded a suspended holding would report an equity the sleeve does not have, and ``04``
    §7.5's five-session write-off is the rule that eventually removes it.
    """
    positions = await open_positions(session, user_id)
    prices = await marks(session, [row.instrument_id for row in positions], as_of)
    valued: list[OpenPositionValue] = []
    marked_on: dict[int, dt.date | None] = {}
    for row in positions:
        entry = Decimal(str(row.entry_avg))
        mark = prices.get(row.instrument_id)
        if mark is None:
            source, price, on = MarkSource.ENTRY, entry, None
        elif mark.on == as_of:
            source, price, on = MarkSource.SESSION_CLOSE, mark.close, mark.on
        else:
            source, price, on = MarkSource.LAST_KNOWN_CLOSE, mark.close, mark.on
        valued.append(
            OpenPositionValue(
                instrument_id=row.instrument_id,
                quantity_open=row.quantity_open,
                entry_avg=entry,
                mark=price,
                mark_source=source,
            )
        )
        marked_on[row.instrument_id] = on
    return LoadedSleeve(
        as_of=as_of,
        value=sleeve_value(
            capital_inr=await sleeve_capital(session, user_id),
            realised_inr=await realised_pnl(session, user_id, as_of),
            open_positions=valued,
        ),
        positions=tuple(valued),
        marked_on=marked_on,
    )


async def sleeve_equity(session: AsyncSession, user_id: int, as_of: dt.date) -> Decimal:
    """``04`` §9.1: capital + realised P&L of ``CLOSED`` positions + the marked value of ``OPEN``
    ones. The slot one new line is sized against is a tenth of this (``04`` §6.1)."""
    return (await load_sleeve(session, user_id, as_of)).value.equity_inr


async def cash_available(session: AsyncSession, user_id: int, as_of: dt.date) -> Decimal:
    """``04`` §9.2: equity less the value of the open positions.

    **There are no working orders in this sleeve to reserve against** (``03`` §6): TWT-1 buys at
    the next open, at market, so nothing rests. VBT-1's loader subtracts its committed limits and
    this one does not, which is the difference between a book that bids and waits and a book that
    takes the open.
    """
    return (await load_sleeve(session, user_id, as_of)).value.cash_available_inr


def config_for(row: TwConfig, base: TwtConfig = DEFAULT_TWT_CONFIG) -> TwtConfig:
    """The engine's configuration with the person's own settings written into it (``03`` §1).

    Three of ``tw_config``'s five editable fields are ``baskfy_core.twt.config`` fields too, and
    this is where the form's number reaches the arithmetic. ``sleeve_capital_inr`` is not one of
    them — it becomes equity — and ``max_open_positions`` is not either: ``04`` §10.1 hands it to
    :func:`baskfy_core.twt.plan.build_entries` separately, because it caps the slot *count* and
    not the slot *size*.
    """
    return replace(
        base,
        sizing=replace(base.sizing, max_position_pct=Decimal(str(row.max_position_pct))),
        exits=replace(
            base.exits,
            stop_pct=Decimal(str(row.stop_pct)),
            trail_pct=Decimal(str(row.trail_pct)),
        ),
    )


async def entries_already_this_session(
    session: AsyncSession, *, user_id: int, signal_date: dt.date
) -> int:
    """``04`` §6.3's already-spent entries: the signal session's confirmed or sent buy orders.

    Counted by ``signal_date`` rather than by a plan id, because the cap is the *session's* and
    the point of it is that a fourth confirm is a refusal **whatever plan it came from**.
    """
    total = await session.execute(
        select(func.count())
        .select_from(TwOrder)
        .where(
            TwOrder.user_id == user_id,
            TwOrder.signal_date == signal_date,
            TwOrder.side == "BUY",
            TwOrder.state.in_(ENTERED_ORDER_STATES),
        )
    )
    return int(total.scalar_one())


async def book_state(
    session: AsyncSession,
    *,
    user_id: int,
    as_of: dt.date,
    signal_date: dt.date | None = None,
) -> BookState:
    """What the sleeve holds and what it has already done — the input ``04`` §10 reads.

    Every position it reports is a ``tw_position`` row, so the plan built from it can only speak
    about names the sleeve bought. A position with no resting GTT becomes ``04`` §10.2's
    ``ARM_GTT``; one carrying an evening-computed ``next_trigger`` is offered to
    :func:`baskfy_core.twt.plan.exit_lines`, which decides for itself whether the trigger is for
    this session and whether it is above the resting one. **Two judgements, one of them pure**:
    this function reports rows, the plan applies rules.
    """
    sleeve = await load_sleeve(session, user_id, as_of)
    rows = await session.execute(
        select(TwPosition, Instrument.symbol)
        .join(Instrument, Instrument.id == TwPosition.instrument_id)
        .where(
            TwPosition.user_id == user_id,
            TwPosition.state == OPEN_STATE,
            TwPosition.quantity_open > 0,
        )
        .order_by(Instrument.symbol)
    )
    naked: list[NakedPosition] = []
    ratchets: list[RatchetDue] = []
    for position, symbol in rows:
        if position.gtt_id is None or position.gtt_trigger is None:
            naked.append(
                NakedPosition(
                    instrument_id=position.instrument_id,
                    symbol=symbol,
                    quantity=position.quantity_open,
                    stop_price=Decimal(str(position.stop_price)),
                )
            )
            continue
        if position.next_trigger is None or position.next_trigger_for is None:
            continue
        ratchets.append(
            RatchetDue(
                instrument_id=position.instrument_id,
                symbol=symbol,
                quantity=position.quantity_open,
                gtt_trigger=Decimal(str(position.gtt_trigger)),
                next_trigger=Decimal(str(position.next_trigger)),
                next_trigger_for=position.next_trigger_for,
                high_since=Decimal(str(position.high_since)),
            )
        )
    return BookState(
        open_instrument_ids=frozenset(position.instrument_id for position in sleeve.positions),
        open_exposure_inr=sleeve.value.open_exposure_inr,
        cash_available_inr=sleeve.value.cash_available_inr,
        entries_already_this_session=await entries_already_this_session(
            session, user_id=user_id, signal_date=signal_date if signal_date else as_of
        ),
        positions_naked_of_gtt=tuple(naked),
        ratchets_due=tuple(ratchets),
    )


async def slot_multiplier(
    session: AsyncSession,
    *,
    user_id: int,
    execution_enabled: bool,
    config: TwtConfig = DEFAULT_TWT_CONFIG,
) -> Decimal:
    """``04`` §6.4's multiplier for the next line, read from the sleeve's own countdown.

    Half while ``first_live_entries_left`` is above zero **and** execution is enabled; one
    otherwise. A ``DRY_RUN`` plan is full size — half size is a live-money discipline, and a
    paper plan that is not the plan is not a rehearsal.

    An unseeded sleeve gets one, which is arithmetically irrelevant: its equity is ₹0 and every
    signal is skipped ``NO_SLEEVE_CAPITAL`` before a multiplier is applied to anything.
    """
    try:
        row = await read_config(session, user_id)
    except TwtConfigNotSeeded:
        return _ONE
    return first_live_multiplier(
        entries_left=row.first_live_entries_left,
        execution_enabled=execution_enabled,
        config=config.sizing,
    )


async def count_first_live_entry(  # noqa: PLR0913 - one keyword per input the audit row needs
    session: AsyncSession,
    *,
    user_id: int,
    position_id: int,
    session_date: dt.date,
    now: dt.datetime,
    execution_enabled: bool,
    changed_by: str = FIRST_LIVE_COUNTED_BY,
) -> FirstLiveEntry:
    """Spend one of ``04`` §6.4's first ten live entries, on a **fill**.

    Three things this cannot do, and each is structural rather than remembered:

    * **It cannot be called on a proposed line.** The parameter is a ``position_id``, and a plan
      line nobody confirmed has no ``tw_position`` row to have an id.
    * **It cannot be called by a plan being built.** A ``DRY_RUN`` or flag-off fill is
      ``simulated``, and a simulated fill counts nothing.
    * **It cannot count the same fill twice.** ``tw_position.half_size`` is the record of which
      entries the countdown applied to; a row already marked is already counted, so a partial
      fill that completes later, a retried confirm and a re-run of the morning all land on the
      same answer.

    The countdown itself moves through :func:`baskfy_api.twt_settings.record_system_change`, the
    one door a job has, so ``tw_config_audit`` always says who counted an entry and when. There
    is no field on ``TwtConfigPatch`` for it: a person who could set the counter could delete the
    half-size discipline by asking for it.
    """
    position = (
        await session.execute(
            select(TwPosition).where(TwPosition.id == position_id, TwPosition.user_id == user_id)
        )
    ).scalar_one_or_none()
    if position is None:
        raise LookupError(
            f"no tw_position {position_id} for user {user_id}; the countdown moves on a filled "
            "entry, and a filled entry is a row"
        )
    row = await read_config(session, user_id)
    left = int(row.first_live_entries_left)
    if position.half_size:
        return FirstLiveEntry(False, True, left, _ALREADY_COUNTED)
    if not execution_enabled or position.simulated:
        return FirstLiveEntry(False, False, left, _NOT_LIVE_MONEY)
    if left <= 0:
        return FirstLiveEntry(False, False, left, _COUNTDOWN_SPENT)

    await record_system_change(
        session,
        user_id=user_id,
        field=FIRST_LIVE_FIELD,
        value=left - 1,
        changed_by=changed_by,
        now=now,
        note=(
            f"entry filled on {session_date.isoformat()}: tw_position {position_id} was sized at "
            "half a slot"
        ),
    )
    position.half_size = True
    await _count_on_the_session(session, user_id=user_id, session_date=session_date)
    await session.flush()
    return FirstLiveEntry(True, True, left - 1, _COUNTED)


async def _count_on_the_session(
    session: AsyncSession, *, user_id: int, session_date: dt.date
) -> None:
    """``03`` §8's ``tw_session.first_live_entries_counted`` — how many this session consumed.

    An upsert that increments in the database rather than a read-modify-write, so the count is
    right whether the evening job wrote the session row first or the fill got there first, and
    two fills a minute apart cannot both read the same number. ``mode`` is set only on the
    insert: a session row the evening already wrote keeps what it said it was.
    """
    statement = pg_insert(TwSession).values(
        user_id=user_id,
        session_date=session_date,
        mode="LIVE",
        first_live_entries_counted=1,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["user_id", "session_date"],
            set_={
                "first_live_entries_counted": TwSession.first_live_entries_counted + 1,
            },
        )
    )
