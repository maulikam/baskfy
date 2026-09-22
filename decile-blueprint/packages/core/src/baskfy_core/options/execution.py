"""The execution mechanics the core decides and the desk sends (``04`` §8).

Pure decisions only — nothing here sends an order (law 2: ``packages/execution`` is the only path
to one). The desk (OP10) asks this module:

* **in which order** the legs go in and come out (§8.3; condor ``04`` §4.6-4.7; ``04`` §5.4) —
  protecting longs first on entry, shorts first on exit, so there is **no instant with short
  quantity above long quantity on either side** (Track C §2);
* **what to do next** after each leg's outcome (``advance_entry`` / ``next_exit_leg``) — a partial
  or cancelled protecting leg abandons the entry before any short is sent;
* **at what limit** each attempt goes (§8.2) — the quoted side improved by a tick, once repriced,
  then cancelled; a risk-reducing close gets a third, marketable LIMIT at the touch (never a
  MARKET order — ``DECISIONS-OP`` OP0.8);
* **what a paper fill is** (§8.4) — the depth-ladder walk, re-implemented from the frozen lab's
  ``strategies/strangle/fills_paper.simulate_fill`` (PACK.2): no LTP, no mid, ever;
* the shared exit plumbing — precedence, the stale-mark rule and ``FEED_LOST`` (§8.5) — and the
  expiry-day slot (§8.6).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from baskfy_core.options.chain import Level
from baskfy_core.options.config import (
    SLOT_SLEEVES,
    ExecutionConfig,
    OptionType,
    Side,
    Sleeve,
    SleeveGroup,
    group_of,
)

#: §8.2: attempt 2 is the one reprice; attempt 3 exists only for a risk-reducing close.
_REPRICED_ATTEMPT = 2
_MARKETABLE_ATTEMPT = 3


class LegRole(StrEnum):
    LONG_CALL = "LONG_CALL"
    LONG_PUT = "LONG_PUT"
    SHORT_CALL = "SHORT_CALL"
    SHORT_PUT = "SHORT_PUT"

    @property
    def is_long(self) -> bool:
        return self in (LegRole.LONG_CALL, LegRole.LONG_PUT)

    @property
    def option_type(self) -> OptionType:
        return OptionType.CE if self in (LegRole.LONG_CALL, LegRole.SHORT_CALL) else OptionType.PE

    @property
    def entry_side(self) -> Side:
        return Side.BUY if self.is_long else Side.SELL

    @property
    def exit_side(self) -> Side:
        return Side.SELL if self.is_long else Side.BUY

    @property
    def protector(self) -> LegRole | None:
        """The long that protects a short (same option type); ``None`` for a long."""
        if self is LegRole.SHORT_CALL:
            return LegRole.LONG_CALL
        if self is LegRole.SHORT_PUT:
            return LegRole.LONG_PUT
        return None


#: Entry: puts before calls within longs and within shorts — condor ``04`` §4.6's
#: ``[LONG_PUT, LONG_CALL, SHORT_PUT, SHORT_CALL]``; a debit spread is ``[LONG, SHORT]`` (§5.4).
_ENTRY_RANK: dict[LegRole, int] = {
    LegRole.LONG_PUT: 0,
    LegRole.LONG_CALL: 1,
    LegRole.SHORT_PUT: 2,
    LegRole.SHORT_CALL: 3,
}
#: Exit: condor ``04`` §4.7's ``[SHORT_CALL, SHORT_PUT, LONG_CALL, LONG_PUT]``; a spread is
#: ``[SHORT, LONG]``.
_EXIT_RANK: dict[LegRole, int] = {
    LegRole.SHORT_CALL: 0,
    LegRole.SHORT_PUT: 1,
    LegRole.LONG_CALL: 2,
    LegRole.LONG_PUT: 3,
}


def entry_sequence(roles: Iterable[LegRole]) -> tuple[LegRole, ...]:
    """Every long before any short (§8.3). Refuses a short with no protecting long (Track C §2)."""
    listed = tuple(roles)
    if len(set(listed)) != len(listed):
        raise ValueError("a structure has at most one leg per role")
    for role in listed:
        protector = role.protector
        if protector is not None and protector not in listed:
            raise ValueError(f"{role.value} has no {protector.value}: a naked short is Track C")
    return tuple(sorted(listed, key=_ENTRY_RANK.__getitem__))


def exit_sequence(roles: Iterable[LegRole]) -> tuple[LegRole, ...]:
    """Every short before any long — a protecting leg is never closed first (§8.3)."""
    return tuple(sorted(set(roles), key=_EXIT_RANK.__getitem__))


def never_naked(position: Mapping[LegRole, int]) -> bool:
    """Short quantity ≤ long quantity on each side, the property Track C §2 demands."""
    return position.get(LegRole.SHORT_CALL, 0) <= position.get(
        LegRole.LONG_CALL, 0
    ) and position.get(LegRole.SHORT_PUT, 0) <= position.get(LegRole.LONG_PUT, 0)


class EntryStep(StrEnum):
    SEND = "SEND"
    OPEN = "OPEN"
    ABANDON = "ABANDON"


@dataclass(frozen=True, slots=True)
class EntryAction:
    """What the desk does next. ``SEND`` names the leg; ``ABANDON`` lists what to close, in exit
    order, with the filled quantity of each."""

    step: EntryStep
    leg: LegRole | None = None
    close: tuple[tuple[LegRole, int], ...] = ()


def advance_entry(
    sequence: Sequence[LegRole], quantity: int, outcomes: Sequence[int]
) -> EntryAction:
    """The entry reducer. ``outcomes[i]`` is the final filled quantity of ``sequence[i]`` (after
    its wait, reprice and cancel); legs not yet sent have no outcome.

    * every outcome full and legs remain → ``SEND`` the next one;
    * every outcome full and none remain → ``OPEN``;
    * any outcome short of ``quantity`` → ``ABANDON``: nothing further is sent, and every filled
      leg is closed in exit order (a filled short is bought back before its wing is sold). Because
      longs precede shorts, a short is only ever sent after every long filled in full.
    """
    if len(outcomes) > len(sequence):
        raise ValueError("more outcomes than legs")
    for role, filled in zip(sequence, outcomes, strict=False):
        if not 0 <= filled <= quantity:
            raise ValueError(f"{role.value} filled {filled} of {quantity}")
    if any(filled < quantity for filled in outcomes):
        filled_legs = {
            role: filled for role, filled in zip(sequence, outcomes, strict=False) if filled > 0
        }
        return EntryAction(
            EntryStep.ABANDON,
            close=tuple((role, filled_legs[role]) for role in exit_sequence(filled_legs)),
        )
    if len(outcomes) == len(sequence):
        return EntryAction(EntryStep.OPEN)
    return EntryAction(EntryStep.SEND, leg=sequence[len(outcomes)])


def next_exit_leg(open_quantity: Mapping[LegRole, int]) -> LegRole | None:
    """The next leg to close: the first leg in exit order still open. A long is never offered while
    its short is open (§8.3); ``None`` when flat."""
    for role in exit_sequence(open_quantity):
        if open_quantity[role] <= 0:
            continue
        protected_open = any(
            open_quantity.get(short, 0) > 0
            for short in (LegRole.SHORT_CALL, LegRole.SHORT_PUT)
            if short.protector is role
        )
        if protected_open:
            continue
        return role
    return None


@dataclass(frozen=True, slots=True)
class Attempt:
    """One LIMIT order attempt. ``marketable`` is the third, risk-reducing close at the touch."""

    number: int
    side: Side
    price: Decimal
    marketable: bool


def next_attempt(  # noqa: PLR0913 - the attempt, the quote, the tick and the config
    number: int,
    side: Side,
    bid: Decimal,
    ask: Decimal,
    tick: Decimal,
    *,
    closing_reduces_risk: bool,
    config: ExecutionConfig,
) -> Attempt | None:
    """§8.2: attempt 1 at the quoted side + ``limit_improve_ticks`` toward the market (a buy at
    ``ask + tick``, a sell at ``bid - tick``); attempt 2 one more tick; after the second wait the
    order is cancelled (``None``) — except a closing leg that reduces risk (a short buy-back, or
    O2's sell), whose attempt 3 is a **marketable LIMIT at the current touch** when
    ``exit_final_marketable``. ``bid``/``ask`` are the quote at the attempt. A sell never prices
    below one tick."""
    if number < 1:
        raise ValueError("attempts count from 1")
    direction = Decimal(1) if side is Side.BUY else Decimal(-1)
    touch = ask if side is Side.BUY else bid
    if number <= _REPRICED_ATTEMPT:
        ticks = config.limit_improve_ticks + (number - 1)
        price = touch + direction * tick * ticks
        return Attempt(number, side, max(price, tick), marketable=False)
    if number == _MARKETABLE_ATTEMPT and closing_reduces_risk and config.exit_final_marketable:
        return Attempt(number, side, max(touch, tick), marketable=True)
    return None


class SimMethod(StrEnum):
    DEPTH_LADDER = "DEPTH_LADDER"


@dataclass(frozen=True, slots=True)
class SimFill:
    """A paper fill: what the ladder gave at or inside the limit. ``filled < requested`` is a
    partial; ``filled == 0`` means nothing was available at the limit."""

    side: Side
    requested: int
    filled: int
    avg_price: Decimal | None
    levels_consumed: int
    method: SimMethod = SimMethod.DEPTH_LADDER

    @property
    def complete(self) -> bool:
        return self.filled == self.requested


def simulate_fill(  # noqa: PLR0913 - the order, the book and the config
    side: Side,
    ladder: Sequence[Level],
    quantity: int,
    *,
    limit_price: Decimal,
    tick: Decimal,
    config: ExecutionConfig,
) -> SimFill:
    """§8.4: walk the side the order takes (``ladder`` = the asks for a buy, the bids for a sell),
    level by level, up to ``limit_price``.

    Each level beyond the first fills ``latency_ticks`` worse than its quoted price (OP1.5); a
    level whose effective price is beyond the limit stops the walk, and what the ladder could not
    fill at the limit is a partial. There is no LTP or mid path: an empty ladder fills nothing.
    """
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    adverse = tick * config.latency_ticks * (1 if side is Side.BUY else -1)
    remaining, cost, consumed = quantity, Decimal(0), 0
    for index, level in enumerate(lv for lv in ladder if lv.price > 0 and lv.quantity > 0):
        price = level.price + (adverse if index > 0 else Decimal(0))
        beyond = price > limit_price if side is Side.BUY else price < limit_price
        if beyond:
            break
        take = min(remaining, level.quantity)
        cost += price * take
        remaining -= take
        consumed += 1
        if remaining == 0:
            break
    filled = quantity - remaining
    return SimFill(
        side=side,
        requested=quantity,
        filled=filled,
        avg_price=cost / filled if filled else None,
        levels_consumed=consumed,
    )


def first_by_precedence[C: StrEnum](fired: Iterable[C], precedence: Sequence[C]) -> C | None:
    """The one exit that wins when several fire on one tick — the first in ``precedence``.

    O1 ``HARD_EXIT > STRIKE_TOUCH > STOP > PROFIT`` (condor §7.6), O2 ``HARD_EXIT > STOP >
    INVALIDATED > TARGET > TIME_STOP`` (§4.5), O3 ``HARD_EXIT > STOP > INVALIDATED > TARGET``
    (§5.3). A fired code missing from ``precedence`` is a caller bug and raises.
    """
    fired_set = set(fired)
    unknown = fired_set - set(precedence)
    if unknown:
        raise ValueError(f"no precedence for {sorted(str(code) for code in unknown)}")
    for code in precedence:
        if code in fired_set:
            return code
    return None


def drop_on_stale[C: StrEnum](
    fired: Iterable[C], profit_codes: Iterable[C], stale: bool
) -> tuple[C, ...]:
    """§8.5: on a stale quote the profit-taking / target rules are not evaluated; stops,
    invalidations and hard exits still are, on the last known mark."""
    profits = set(profit_codes)
    return tuple(code for code in fired if not (stale and code in profits))


def feed_lost(  # noqa: PLR0913 - two clocks, the position and the config
    sleeve: Sleeve,
    *,
    now: dt.datetime,
    last_index_tick: dt.datetime | None,
    position_opened_at: dt.datetime | None,
    stale_index_seconds: int,
    config: ExecutionConfig,
) -> bool:
    """§8.5 — ``HARD_EXIT`` with reason ``FEED_LOST``.

    With a position open: O1 and O3 lose the feed after ``feed_watch_from`` (14:00) once no index
    tick has come for ``stale_index_seconds``; O2 at any time once the position is older than
    ``feed_grace_seconds`` and no tick has come for ``stale_index_seconds``. No position, no exit.
    """
    if position_opened_at is None:
        return False
    silent = (
        last_index_tick is None or (now - last_index_tick).total_seconds() > stale_index_seconds
    )
    if not silent:
        return False
    if group_of(sleeve) is SleeveGroup.O2:
        return (now - position_opened_at).total_seconds() > config.feed_grace_seconds
    return now.time() >= config.feed_watch_from


class SlotVerdict(StrEnum):
    FREE = "FREE"
    HELD_BY_SELF = "HELD_BY_SELF"
    SLOT_TAKEN = "SLOT_TAKEN"
    OUTSIDE_SLOT = "OUTSIDE_SLOT"


def slot_verdict(sleeve: Sleeve, holder: Sleeve | None) -> SlotVerdict:
    """§8.6: one O1-or-O3 position per expiry day; the first **confirmed** plan holds the slot.
    O2 is outside it."""
    if sleeve not in SLOT_SLEEVES:
        return SlotVerdict.OUTSIDE_SLOT
    if holder is None:
        return SlotVerdict.FREE
    if holder is sleeve:
        return SlotVerdict.HELD_BY_SELF
    return SlotVerdict.SLOT_TAKEN


def slot_after_confirm(sleeve: Sleeve, holder: Sleeve | None) -> Sleeve | None:
    """The holder after ``sleeve`` confirms: it takes a free slot; a taken slot refuses."""
    verdict = slot_verdict(sleeve, holder)
    if verdict is SlotVerdict.SLOT_TAKEN:
        raise ValueError(f"the expiry-day slot is held by {holder}")
    if verdict is SlotVerdict.OUTSIDE_SLOT:
        return holder
    return sleeve


def slot_after_release(sleeve: Sleeve, holder: Sleeve | None) -> Sleeve | None:
    """A lapsed or rejected plan frees the slot only if its sleeve held it."""
    return None if holder is sleeve else holder
