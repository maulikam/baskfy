"""OP10 — sending a plan's legs (``04`` §8.2-§8.4, ``06`` OP10).

``06`` OP10's pure acceptance criteria:

* "never-naked holds across 500 seeded fill sequences per structure" (:class:`TestNeverNaked`);
* "thin long-call depth → ``ABANDONED_ENTRY``, no short ever sent" and "O3 partial long → abandoned,
  no short sent" (:class:`TestAbandonment`);
* "an exit produces closes in sequence with the final attempt marketable" (:class:`TestTheExit`).

The desk's half (the gateway, the simulator's ladder, the rows) is
``kite-momentum-rebalancer/tests/test_options_execute.py``.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from baskfy_core.options.config import ExecutionConfig, Side, Sleeve
from baskfy_core.options.execution import Attempt, LegRole, never_naked
from baskfy_core.options.executor import (
    Book,
    Fill,
    Outcome,
    run_entry,
    run_exit,
)

CFG = ExecutionConfig()
TICK = Decimal("0.05")
QTY = 65
CONDOR = (LegRole.SHORT_CALL, LegRole.SHORT_PUT, LegRole.LONG_CALL, LegRole.LONG_PUT)
SPREAD = (LegRole.LONG_CALL, LegRole.SHORT_CALL)
LONG = (LegRole.LONG_CALL,)


@dataclass
class Venue:
    """A venue that fills each attempt with ``fill(role, attempt, remaining)`` and records every
    order, and asserts the book is never short more than long after any fill."""

    fill: Callable[[LegRole, Attempt, int], int]
    sent: list[tuple[LegRole, Side, int, int, bool, bool]] = field(default_factory=list)
    book: dict[LegRole, int] = field(default_factory=dict)

    def quote(self, role: LegRole) -> Book:
        del role
        return Book(Decimal("10.00"), Decimal("10.20"))

    def send(self, role: LegRole, attempt: Attempt, quantity: int, closing: bool) -> Fill:
        filled = self.fill(role, attempt, quantity)
        self.sent.append((role, attempt.side, attempt.number, filled, closing, attempt.marketable))
        self.book[role] = self.book.get(role, 0) + (-filled if closing else filled)
        assert never_naked(self.book), (role, self.book)
        return Fill(filled, attempt.price if filled else None)


def full(role: LegRole, attempt: Attempt, quantity: int) -> int:
    del role, attempt
    return quantity


class TestTheHappyPath:
    def test_a_condor_opens_wings_first(self) -> None:
        venue = Venue(full)
        result = run_entry(venue, CONDOR, QTY, sleeve=Sleeve.O1M, tick=TICK, config=CFG)
        assert result.outcome is Outcome.OPEN
        sent = [s[0] for s in venue.sent]
        # Both wings before either short (§8.3); the order among the wings is the core's own.
        assert {sent[0], sent[1]} == {LegRole.LONG_CALL, LegRole.LONG_PUT}
        assert {sent[2], sent[3]} == {LegRole.SHORT_CALL, LegRole.SHORT_PUT}
        assert result.position == dict.fromkeys(CONDOR, QTY)

    def test_attempt_one_is_the_touch_plus_a_tick(self) -> None:
        venue = Venue(full)
        result = run_entry(venue, SPREAD, QTY, sleeve=Sleeve.O3A, tick=TICK, config=CFG)
        long_event, short_event = result.events
        assert long_event.price == Decimal("10.25")  # ask 10.20 + 1 tick
        assert short_event.price == Decimal("9.95")  # bid 10.00 - 1 tick
        assert result.avg_price(LegRole.LONG_CALL) == Decimal("10.25")


class TestAbandonment:
    def test_thin_long_call_depth_abandons_and_no_short_is_ever_sent(self) -> None:
        """A long call that cannot fill: after the reprice it is cancelled; the long put that did
        fill is sold back; neither short is sent."""

        def thin(role: LegRole, attempt: Attempt, quantity: int) -> int:
            del attempt
            return 0 if role is LegRole.LONG_CALL else quantity

        venue = Venue(thin)
        result = run_entry(venue, CONDOR, QTY, sleeve=Sleeve.O1M, tick=TICK, config=CFG)
        assert result.outcome is Outcome.ABANDONED_ENTRY
        assert not any(role in (LegRole.SHORT_CALL, LegRole.SHORT_PUT) for role, *_ in venue.sent)
        assert result.position.get(LegRole.LONG_PUT, 0) == 0  # the filled wing, sold back

    def test_an_o3_partial_long_is_abandoned_and_its_fill_closed(self) -> None:
        def partial(role: LegRole, attempt: Attempt, quantity: int) -> int:
            if role is LegRole.LONG_CALL and attempt.side is Side.BUY:
                return min(quantity, 25)  # 25 on each of its two attempts, then cancelled
            return quantity

        venue = Venue(partial)
        result = run_entry(venue, SPREAD, QTY, sleeve=Sleeve.O3A, tick=TICK, config=CFG)
        assert result.outcome is Outcome.ABANDONED_ENTRY
        assert LegRole.SHORT_CALL not in {role for role, *_ in venue.sent}
        # 25 + 25 filled on the two attempts, then cancelled; the 50 sold back in full.
        bought = sum(
            f for r, s, _, f, closing, _ in venue.sent if r is LegRole.LONG_CALL and not closing
        )
        sold = sum(f for r, s, _, f, closing, _ in venue.sent if r is LegRole.LONG_CALL and closing)
        assert bought == sold == 50
        assert result.position[LegRole.LONG_CALL] == 0


class TestTheExit:
    def test_closes_run_shorts_first_and_the_third_attempt_is_marketable(self) -> None:
        """A short buy-back unfilled twice is sent a third time, marketable at the touch."""

        def stubborn(role: LegRole, attempt: Attempt, quantity: int) -> int:
            if role is LegRole.SHORT_CALL and attempt.number < 3:
                return 0
            return quantity

        venue = Venue(stubborn, book=dict.fromkeys(SPREAD, QTY))
        result = run_exit(
            venue, dict.fromkeys(SPREAD, QTY), sleeve=Sleeve.O3A, tick=TICK, config=CFG
        )
        assert result.outcome is Outcome.FLAT
        order = [(role, number, marketable) for role, _, number, _, _, marketable in venue.sent]
        assert order == [
            (LegRole.SHORT_CALL, 1, False),
            (LegRole.SHORT_CALL, 2, False),
            (LegRole.SHORT_CALL, 3, True),
            (LegRole.LONG_CALL, 1, False),
        ]

    def test_a_wing_gets_no_marketable_attempt_and_the_exit_is_partial(self) -> None:
        """§8.2: the third attempt is for a short buy-back or O2's sell — not an O1/O3 wing."""

        def wing_stuck(role: LegRole, attempt: Attempt, quantity: int) -> int:
            del attempt
            return 0 if role is LegRole.LONG_CALL else quantity

        venue = Venue(wing_stuck, book=dict.fromkeys(SPREAD, QTY))
        result = run_exit(
            venue, dict.fromkeys(SPREAD, QTY), sleeve=Sleeve.O3A, tick=TICK, config=CFG
        )
        assert result.outcome is Outcome.PARTIAL_EXIT
        assert result.position == {LegRole.SHORT_CALL: 0, LegRole.LONG_CALL: QTY}
        assert [n for r, _, n, *_ in venue.sent if r is LegRole.LONG_CALL] == [1, 2]

    def test_o2s_sell_is_risk_reducing(self) -> None:
        def slow(role: LegRole, attempt: Attempt, quantity: int) -> int:
            del role
            return quantity if attempt.number == 3 else 0

        venue = Venue(slow, book={LegRole.LONG_CALL: QTY})
        result = run_exit(venue, {LegRole.LONG_CALL: QTY}, sleeve=Sleeve.O2, tick=TICK, config=CFG)
        assert result.outcome is Outcome.FLAT
        assert venue.sent[-1][5] is True  # marketable


class TestNeverNaked:
    @pytest.mark.parametrize(
        ("roles", "sleeve"),
        [(CONDOR, Sleeve.O1M), (SPREAD, Sleeve.O3A), (LONG, Sleeve.O2)],
        ids=["condor", "spread", "long"],
    )
    def test_500_seeded_fill_sequences_per_structure(
        self, roles: tuple[LegRole, ...], sleeve: Sleeve
    ) -> None:
        """Every attempt fills a random part of its order (often nothing, sometimes all): the
        venue asserts the book after every fill, and the executor asserts it before every send."""
        outcomes: set[Outcome] = set()
        for seed in range(500):
            rng = random.Random(seed)

            def chaotic(
                role: LegRole, attempt: Attempt, quantity: int, rng: random.Random = rng
            ) -> int:
                del role, attempt
                return rng.choice([0, quantity, rng.randint(0, quantity)])

            venue = Venue(chaotic)
            result = run_entry(venue, roles, QTY, sleeve=sleeve, tick=TICK, config=CFG)
            outcomes.add(result.outcome)
            if result.outcome is Outcome.OPEN:
                closed = run_exit(
                    venue, dict(result.position), sleeve=sleeve, tick=TICK, config=CFG
                )
                outcomes.add(closed.outcome)
            assert never_naked(result.position)
        # The seeds really exercised every branch, not only the happy path.
        assert {Outcome.OPEN, Outcome.ABANDONED_ENTRY} <= outcomes
