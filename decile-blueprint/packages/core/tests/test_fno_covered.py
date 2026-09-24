"""``docs/fno/02`` §2.1 / ``04`` §2 — the covered-overnight predicate, property-tested.

The property (FO1's acceptance): over **every prefix and every partial fill** of the entry
sequence (long put → long call → short put → short call) and the exit sequence (short call →
short put → long call → long put), with randomised strikes and quantities, the book is covered;
a step the guard would refuse is refused before it can uncover anything; and a reversed,
shorts-first sequence is refused at its first step. ``is_covered`` is also checked against an
independent oracle (Hall's condition as thresholds) on arbitrary random books.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from baskfy_core.fno.condor import (
    ENTRY_SEQUENCE,
    EXIT_SEQUENCE,
    CondorStrikes,
    LegRole,
    option_type_of,
    sign_of,
)
from baskfy_core.fno.covered import OptionPosition, assess_step, is_covered, uncovered
from baskfy_core.options.config import OptionType

EXPIRY = dt.date(2026, 11, 24)
U = "NIFTY"


def pos(kind: OptionType, strike: int, qty: int, expiry: dt.date = EXPIRY) -> OptionPosition:
    return OptionPosition(U, expiry, kind, Decimal(strike), qty)


CE, PE = OptionType.CE, OptionType.PE


class TestExamples:
    def test_empty_and_longs_only_are_covered(self) -> None:
        assert is_covered([])
        assert is_covered([pos(CE, 25000, 75), pos(PE, 24000, 75)])

    def test_naked_short_is_not(self) -> None:
        assert not is_covered([pos(CE, 25000, -75)])
        assert uncovered([pos(CE, 25000, -75)])[0].startswith("NIFTY 2026-11-24 25000 CE")

    def test_call_cover_must_be_a_higher_strike(self) -> None:
        assert is_covered([pos(CE, 25000, -75), pos(CE, 25300, 75)])
        assert not is_covered([pos(CE, 25000, -75), pos(CE, 24700, 75)])

    def test_put_cover_must_be_a_lower_strike(self) -> None:
        assert is_covered([pos(PE, 24000, -75), pos(PE, 23700, 75)])
        assert not is_covered([pos(PE, 24000, -75), pos(PE, 24300, 75)])

    def test_quantity_must_be_at_least_the_short(self) -> None:
        assert not is_covered([pos(CE, 25000, -150), pos(CE, 25300, 75)])

    def test_another_type_or_expiry_or_underlying_never_covers(self) -> None:
        assert not is_covered([pos(CE, 25000, -75), pos(PE, 25300, 75)])
        assert not is_covered([pos(CE, 25000, -75), pos(CE, 25300, 75, dt.date(2026, 12, 29))])
        other = OptionPosition("BANKNIFTY", EXPIRY, CE, Decimal(25300), 75)
        assert not is_covered([pos(CE, 25000, -75), other])

    def test_one_long_does_not_cover_two_shorts(self) -> None:
        assert not is_covered([pos(CE, 25000, -75), pos(CE, 25100, -75), pos(CE, 25300, 75)])

    def test_a_long_between_two_shorts_covers_only_the_lower(self) -> None:
        book = [pos(CE, 25000, -75), pos(CE, 25100, 75), pos(CE, 25200, -75)]
        assert not is_covered(book)


# ------------------------------------------------------------------------------------------
# The oracle: Hall's condition, written as thresholds (no walk, no running sum).
# ------------------------------------------------------------------------------------------


def oracle(book: Sequence[OptionPosition]) -> bool:
    sides: dict[tuple[str, dt.date, OptionType], dict[Decimal, int]] = {}
    for p in book:
        side = sides.setdefault((p.underlying, p.expiry, p.option_type), {})
        side[p.strike] = side.get(p.strike, 0) + p.quantity
    for (_, _, kind), net in sides.items():
        for t in net:
            if kind is OptionType.CE:
                shorts = sum(-q for k, q in net.items() if k >= t and q < 0)
                longs = sum(q for k, q in net.items() if k > t and q > 0)
            else:
                shorts = sum(-q for k, q in net.items() if k <= t and q < 0)
                longs = sum(q for k, q in net.items() if k < t and q > 0)
            if shorts > longs:
                return False
    return True


books = st.lists(
    st.builds(
        pos,
        st.sampled_from([CE, PE]),
        st.integers(min_value=240, max_value=260).map(lambda k: k * 100),
        st.integers(min_value=-4, max_value=4).map(lambda q: q * 25),
    ),
    max_size=8,
)


@given(books)
@settings(max_examples=400, deadline=None)
def test_is_covered_agrees_with_the_threshold_oracle(book: list[OptionPosition]) -> None:
    assert is_covered(book) == oracle(book)


# ------------------------------------------------------------------------------------------
# Every prefix and every partial fill of both sequences.
# ------------------------------------------------------------------------------------------


@st.composite
def condors(draw: st.DrawFn) -> tuple[CondorStrikes, int]:
    step = draw(st.sampled_from([50, 100, 500]))
    atm = draw(st.integers(min_value=200, max_value=600)) * step
    short_out = draw(st.integers(min_value=0, max_value=10))
    wing_out = draw(st.integers(min_value=1, max_value=10))
    put_short_out = draw(st.integers(min_value=0, max_value=10))
    put_wing_out = draw(st.integers(min_value=1, max_value=10))
    strikes = CondorStrikes(
        long_call=Decimal(atm + (short_out + wing_out) * step),
        short_call=Decimal(atm + short_out * step),
        short_put=Decimal(atm - put_short_out * step),
        long_put=Decimal(atm - (put_short_out + put_wing_out) * step),
    )
    lot = draw(st.sampled_from([15, 25, 30, 65, 75]))
    lots = draw(st.integers(min_value=1, max_value=10))
    return strikes, lot * lots


def leg(strikes: CondorStrikes, role: LegRole, qty: int) -> OptionPosition:
    return OptionPosition(U, EXPIRY, option_type_of(role), strikes.strike(role), qty)


@dataclass
class Book:
    """The broker's book outside the plan, and the plan's filled legs."""

    outside: list[OptionPosition]
    filled: list[OptionPosition]

    def all(self) -> list[OptionPosition]:
        return [*self.outside, *self.filled]


def run_sequence(  # noqa: PLR0913, PLR0917 - the sequence under test, whole
    strikes: CondorStrikes,
    qty: int,
    sequence: Sequence[LegRole],
    fills: Sequence[int],
    book: Book,
    opening: bool,
) -> list[bool]:
    """Send each leg of ``sequence`` through the guard; fill it ``fills[i]`` units (≤ its
    order). A refused step stops the sequence (entry: ``ABANDONED_PARTIAL``). Returns each
    step's verdict; asserts the book is covered after every order and every partial fill."""
    verdicts: list[bool] = []
    for role, filled in zip(sequence, fills, strict=True):
        direction = sign_of(role) if opening else -sign_of(role)
        order = leg(strikes, role, direction * qty)
        verdict = assess_step(order, broker_positions=book.outside, plan_filled=book.filled)
        verdicts.append(verdict.covered)
        if not verdict.covered:
            break
        # Every partial fill of the admitted order leaves a covered book.
        for part in range(filled + 1):
            assert is_covered([*book.all(), leg(strikes, role, direction * part)])
        book.filled.append(leg(strikes, role, direction * filled))
        assert is_covered(book.all())
    return verdicts


fill_fracs = st.lists(st.integers(min_value=0, max_value=100), min_size=4, max_size=4)


@given(condors())
@settings(max_examples=300, deadline=None)
def test_every_prefix_of_the_full_entry_and_exit_is_admitted_and_covered(
    condor: tuple[CondorStrikes, int],
) -> None:
    strikes, qty = condor
    book = Book([], [])
    assert all(run_sequence(strikes, qty, ENTRY_SEQUENCE, [qty] * 4, book, opening=True))
    assert all(run_sequence(strikes, qty, EXIT_SEQUENCE, [qty] * 4, book, opening=False))
    net: dict[tuple[OptionType, Decimal], int] = {}
    for p in book.filled:
        net[(p.option_type, p.strike)] = net.get((p.option_type, p.strike), 0) + p.quantity
    assert set(net.values()) == {0}  # flat after the exit


@given(condors(), fill_fracs)
@settings(max_examples=400, deadline=None)
def test_partial_entry_fills_never_uncover_and_an_underfilled_wing_stops_its_short(
    condor: tuple[CondorStrikes, int], fracs: list[int]
) -> None:
    strikes, qty = condor
    fills = [qty * f // 100 for f in fracs]
    book = Book([], [])
    verdicts = run_sequence(strikes, qty, ENTRY_SEQUENCE, fills, book, opening=True)
    # Longs are always admitted.
    assert verdicts[:2] == [True, True]
    # The short put goes only if the long put filled in full, the short call only if the long
    # call did (and only if the sequence got that far).
    put_ok = fills[0] >= qty
    assert verdicts[2] is put_ok
    if put_ok:
        assert verdicts[3] is (fills[1] >= qty)
    # Refused at the first short: only longs are behind, and ABANDONED_PARTIAL closes them at
    # once. Refused later, the filled short put is covered by its own wing (asserted per step).
    if not put_ok:
        assert all(p.quantity >= 0 for p in book.filled)
    assert is_covered(book.all())


@given(condors(), fill_fracs)
@settings(max_examples=400, deadline=None)
def test_partial_exit_fills_never_uncover(
    condor: tuple[CondorStrikes, int], fracs: list[int]
) -> None:
    strikes, qty = condor
    book = Book([], [])
    run_sequence(strikes, qty, ENTRY_SEQUENCE, [qty] * 4, book, opening=True)
    fills = [qty * f // 100 for f in fracs]
    verdicts = run_sequence(strikes, qty, EXIT_SEQUENCE, fills, book, opening=False)
    # Buying back shorts is always admitted; selling a wing only once its short is bought back.
    assert verdicts[:2] == [True, True]
    assert verdicts[2] is (fills[0] >= qty)
    if verdicts[2]:
        assert verdicts[3] is (fills[1] >= qty)


@given(condors())
@settings(max_examples=200, deadline=None)
def test_a_reversed_shorts_first_entry_is_refused_at_its_first_step(
    condor: tuple[CondorStrikes, int],
) -> None:
    strikes, qty = condor
    reversed_entry = tuple(reversed(ENTRY_SEQUENCE))
    verdicts = run_sequence(strikes, qty, reversed_entry, [qty] * 4, Book([], []), opening=True)
    assert verdicts == [False]


@given(condors())
@settings(max_examples=200, deadline=None)
def test_a_reversed_longs_first_exit_is_refused_at_its_first_step(
    condor: tuple[CondorStrikes, int],
) -> None:
    strikes, qty = condor
    book = Book([], [])
    run_sequence(strikes, qty, ENTRY_SEQUENCE, [qty] * 4, book, opening=True)
    reversed_exit = tuple(reversed(EXIT_SEQUENCE))
    assert run_sequence(strikes, qty, reversed_exit, [qty] * 4, book, opening=False) == [False]


@given(condors(), books)
@settings(max_examples=200, deadline=None)
def test_an_uncovered_outside_book_is_never_made_to_look_covered(
    condor: tuple[CondorStrikes, int], outside: list[OptionPosition]
) -> None:
    """Whatever the broker's other positions, every admitted step leaves a covered book."""
    strikes, qty = condor
    if not is_covered(outside):
        order = leg(strikes, ENTRY_SEQUENCE[0], qty)
        # A long put can repair a put side, never a call side; the guard judges the whole book.
        verdict = assess_step(order, broker_positions=outside, plan_filled=[])
        assert verdict.covered == is_covered([*outside, order])
        return
    book = Book(list(outside), [])
    run_sequence(strikes, qty, ENTRY_SEQUENCE, [qty] * 4, book, opening=True)
