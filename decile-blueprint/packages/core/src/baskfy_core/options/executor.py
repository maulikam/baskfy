"""Sending a plan's legs, pure: the order in which things happen, never how (``06`` OP10).

``04`` §8.2-§8.4 say *what* an entry and an exit do: every long before any short, each leg a LIMIT
at the touch plus a tick, repriced once, then cancelled — and on an exit a risk-reducing close gets
a third, marketable attempt; any leg that does not fill in full abandons the entry, and whatever did
fill is closed in exit order. This module is that procedure over a :class:`Venue` the caller
supplies. The desk's venue sends each attempt through ``OrderGateway.place`` and, while the sleeve
is ``PAPER``, fills it from the depth-ladder simulator (``execution.simulate_fill``); a test's venue
fills from a seeded random ladder. Either way this module never touches a broker.

**Never naked, at every step.** Every attempt passes through :func:`_send`, which asserts
``never_naked`` on the position the attempt could leave (the short's full remaining quantity counted
as filled before it is sent). ``06`` OP10 asks for that property "across 500 seeded fill sequences
per structure"; ``test_options_executor.py`` runs them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from baskfy_core.options.config import ExecutionConfig, Side, Sleeve
from baskfy_core.options.execution import (
    Attempt,
    EntryStep,
    LegRole,
    advance_entry,
    entry_sequence,
    never_naked,
    next_attempt,
    next_exit_leg,
)

#: The most attempts an order ever gets (§8.2: two limits, then a marketable close).
MAX_ATTEMPTS = 3


class Outcome(StrEnum):
    OPEN = "OPEN"
    ABANDONED_ENTRY = "ABANDONED_ENTRY"
    FLAT = "FLAT"
    #: An exit left a leg open after its attempts (a wing whose close is not risk-reducing): the
    #: caller tries again on its next pass — the position is not flat yet.
    PARTIAL_EXIT = "PARTIAL_EXIT"


@dataclass(frozen=True, slots=True)
class Book:
    """A leg's quote at the moment of an attempt."""

    bid: Decimal
    ask: Decimal


@dataclass(frozen=True, slots=True)
class Fill:
    quantity: int
    avg_price: Decimal | None


class Venue(Protocol):
    """Where orders go. ``quote`` reads the book; ``send`` places one attempt and waits it out
    (``fill_wait_seconds``), returning what filled — a partial is a smaller ``quantity``."""

    def quote(self, role: LegRole) -> Book: ...

    def send(self, role: LegRole, attempt: Attempt, quantity: int, closing: bool) -> Fill: ...


@dataclass(frozen=True, slots=True)
class Event:
    """One step of the procedure, for the journal and the tests."""

    role: LegRole
    side: Side
    attempt: int
    price: Decimal
    requested: int
    filled: int
    closing: bool
    marketable: bool


@dataclass(slots=True)
class Result:
    outcome: Outcome
    position: dict[LegRole, int]
    fills: dict[LegRole, list[Fill]] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)

    def avg_price(self, role: LegRole) -> Decimal | None:
        done = [f for f in self.fills.get(role, []) if f.quantity and f.avg_price is not None]
        total = sum(f.quantity for f in done)
        if not total:
            return None
        return sum(((f.avg_price or Decimal(0)) * f.quantity for f in done), Decimal(0)) / total


def _reduces_risk(role: LegRole, sleeve: Sleeve) -> bool:
    """§8.2: the marketable third attempt is for "a short buy-back, or O2's sell"."""
    return not role.is_long or sleeve is Sleeve.O2


def _entry_side(role: LegRole) -> Side:
    return Side.BUY if role.is_long else Side.SELL


def _exit_side(role: LegRole) -> Side:
    return Side.SELL if role.is_long else Side.BUY


def _send(  # noqa: PLR0913 - the venue, the leg, its size, the rules and the ledger
    venue: Venue,
    role: LegRole,
    quantity: int,
    *,
    closing: bool,
    sleeve: Sleeve,
    tick: Decimal,
    config: ExecutionConfig,
    result: Result,
) -> int:
    """One leg's attempts until it fills or they run out; returns the filled quantity."""
    side = _exit_side(role) if closing else _entry_side(role)
    reduces = closing and _reduces_risk(role, sleeve)
    remaining = quantity
    for number in range(1, MAX_ATTEMPTS + 1):
        book = venue.quote(role)
        attempt = next_attempt(
            number, side, book.bid, book.ask, tick, closing_reduces_risk=reduces, config=config
        )
        if attempt is None:
            break
        # The position this attempt could leave if it filled in full — checked before it is sent.
        worst = dict(result.position)
        worst[role] = worst.get(role, 0) + (-remaining if closing else remaining)
        if not never_naked(worst):
            raise AssertionError(f"{role.value} would leave the book short more than long")
        fill = venue.send(role, attempt, remaining, closing)
        if not 0 <= fill.quantity <= remaining:
            raise ValueError(f"{role.value}: filled {fill.quantity} of {remaining}")
        result.position[role] = result.position.get(role, 0) + (
            -fill.quantity if closing else fill.quantity
        )
        result.fills.setdefault(role, []).append(fill)
        result.events.append(
            Event(role, side, number, attempt.price, remaining, fill.quantity, closing,
                  attempt.marketable)
        )  # fmt: skip
        remaining -= fill.quantity
        if remaining == 0:
            break
    return quantity - remaining


def run_entry(  # noqa: PLR0913 - the venue, the legs, the size and the rules
    venue: Venue,
    roles: Sequence[LegRole],
    quantity: int,
    *,
    sleeve: Sleeve,
    tick: Decimal,
    config: ExecutionConfig,
) -> Result:
    """§8.3's entry: longs first; the first leg short of ``quantity`` abandons the entry and every
    filled leg is closed in exit order. Returns ``OPEN`` or ``ABANDONED_ENTRY`` (with what the
    abandonment's closes left open, if any)."""
    sequence = entry_sequence(roles)
    result = Result(Outcome.OPEN, {})
    outcomes: list[int] = []
    while True:
        action = advance_entry(sequence, quantity, outcomes)
        if action.step is EntryStep.OPEN:
            return result
        if action.step is EntryStep.ABANDON:
            closed = run_exit(
                venue, dict(result.position), sleeve=sleeve, tick=tick, config=config,
                result=result,
            )  # fmt: skip
            closed.outcome = Outcome.ABANDONED_ENTRY
            return closed
        role = action.leg
        if role is None:  # SEND always names a leg; kept for the type checker
            raise ValueError("an entry step with no leg")
        outcomes.append(
            _send(venue, role, quantity, closing=False, sleeve=sleeve, tick=tick, config=config,
                  result=result)
        )  # fmt: skip


def run_exit(  # noqa: PLR0913 - the venue, the position and the rules
    venue: Venue,
    position: dict[LegRole, int],
    *,
    sleeve: Sleeve,
    tick: Decimal,
    config: ExecutionConfig,
    result: Result | None = None,
) -> Result:
    """§8.3's exit: shorts first, a protecting long never before the short it protects; each leg's
    attempts per §8.2. ``FLAT`` when everything closed, ``PARTIAL_EXIT`` when a leg's attempts ran
    out (the caller retries on its next pass; the book is still never naked)."""
    out = result if result is not None else Result(Outcome.FLAT, dict(position))
    if result is not None:
        out.position = dict(position)
    tried: set[LegRole] = set()
    while True:
        role = next_exit_leg(out.position)
        if role is None:
            out.outcome = Outcome.FLAT
            return out
        if role in tried:  # its attempts ran out on this pass
            out.outcome = Outcome.PARTIAL_EXIT
            return out
        tried.add(role)
        _send(venue, role, out.position[role], closing=True, sleeve=sleeve, tick=tick,
              config=config, result=out)  # fmt: skip


__all__ = [
    "MAX_ATTEMPTS",
    "Book",
    "Event",
    "Fill",
    "Outcome",
    "Result",
    "Venue",
    "run_entry",
    "run_exit",
]
