"""The covered-overnight predicate (``02`` §2.1, ``04`` §2).

**A short option is never open without a long option of the same underlying, type and expiry,
further from the money, in at least the same quantity.** For calls "further" is a higher strike,
for puts a lower one. :func:`is_covered` decides that for a book; the gateway's
``assert_overnight_option_is_covered`` (FO6) asks it before every NRML option order, on the book
**after** the order.

The decision per ``(underlying, expiry, option_type)``: net the quantities per strike, then walk
from the furthest-out strike toward the money (calls: descending, puts: ascending) keeping the
running sum; a short strike is covered only by longs already passed (strictly further out), so the
book is covered iff the running sum never goes negative. That is Hall's condition for matching
every short unit to a long unit further out, and the property test checks it against an
independent threshold oracle.

The book after a step is the broker's positions **plus** the plan's already-filled legs **plus**
the order itself (``04`` §2). The caller's contract (DECISIONS-FO FO1): ``broker_positions`` is
the broker's book **net of this plan's own fills**, so a fill is counted once — counting a long
twice would overstate cover, which is the one mistake this module exists to prevent.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from baskfy_core.options.config import OptionType


@dataclass(frozen=True, slots=True)
class OptionPosition:
    """A signed option quantity in units: positive long, negative short."""

    underlying: str
    expiry: dt.date
    option_type: OptionType
    strike: Decimal
    quantity: int


_Side = tuple[str, dt.date, OptionType]


def _net_by_side(book: Iterable[OptionPosition]) -> dict[_Side, dict[Decimal, int]]:
    sides: dict[_Side, dict[Decimal, int]] = defaultdict(lambda: defaultdict(int))
    for row in book:
        sides[(row.underlying, row.expiry, row.option_type)][row.strike] += row.quantity
    return sides


def uncovered(book: Iterable[OptionPosition]) -> tuple[str, ...]:
    """Every uncovered short, in words. Empty means the book is covered."""
    found: list[str] = []
    for (underlying, expiry, kind), by_strike in sorted(
        _net_by_side(book).items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2].value)
    ):
        outward = sorted(by_strike, reverse=kind is OptionType.CE)
        running = 0
        for strike in outward:
            net = by_strike[strike]
            running += net
            if net < 0 and running < 0:
                found.append(
                    f"{underlying} {expiry.isoformat()} {strike} {kind.value}: short "
                    f"{-net} with only {running - net} long further out"
                )
                running = 0
    return tuple(found)


def is_covered(book_after: Iterable[OptionPosition]) -> bool:
    """``02`` §2.1 on the book after an order."""
    return not uncovered(book_after)


def book_after(
    broker_positions: Iterable[OptionPosition],
    plan_filled: Iterable[OptionPosition],
    step: OptionPosition,
) -> tuple[OptionPosition, ...]:
    """Broker book (net of this plan's fills) + this plan's filled legs + the order."""
    return (*broker_positions, *plan_filled, step)


def net_of_plan(
    broker_raw: Iterable[OptionPosition], plan_filled: Iterable[OptionPosition]
) -> tuple[OptionPosition, ...]:
    """The broker's book with this plan's filled legs taken out (FO1.2's caller contract, FO6).

    Kite reports a net quantity per contract, not per plan, so a book read after a leg filled
    already holds that leg. Subtracting it here is what lets :func:`book_after` add it back
    exactly once. If the broker has not yet reflected a fill, the subtraction leaves a negative
    (short) residue, which can only make the guard stricter — the safe direction.
    """
    net: dict[tuple[str, dt.date, OptionType, Decimal], int] = defaultdict(int)
    for row in broker_raw:
        net[(row.underlying, row.expiry, row.option_type, row.strike)] += row.quantity
    for row in plan_filled:
        net[(row.underlying, row.expiry, row.option_type, row.strike)] -= row.quantity
    return tuple(
        OptionPosition(underlying, expiry, kind, strike, qty)
        for (underlying, expiry, kind, strike), qty in net.items()
        if qty != 0
    )


@dataclass(frozen=True, slots=True)
class StepVerdict:
    covered: bool
    reasons: tuple[str, ...]


def assess_step(
    step: OptionPosition,
    *,
    broker_positions: Iterable[OptionPosition],
    plan_filled: Iterable[OptionPosition],
) -> StepVerdict:
    """The guard's answer for one order of a plan (``04`` §2). A refused entry step abandons
    the entry (``ABANDONED_PARTIAL``); the legs already filled are longs and close at once."""
    reasons = uncovered(book_after(broker_positions, plan_filled, step))
    return StepVerdict(not reasons, reasons)
