"""The plan the desk shows and a person confirms (``docs/vbt/04`` §9).

Two things are asserted here that are easy to get wrong and expensive to get wrong:

* **The order of the checks**, because it decides which reason a skipped name carries, and the
  reason is what a person reads on the page.
* **That a working order holds a slot.** The strategy bids and waits; a book that could line
  eleven limits for ten slots would over-commit its cash the day they all filled.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG, Gate, VbtConfig
from baskfy_core.vbt.exits import Action, ExitReason, ManageAction
from baskfy_core.vbt.orders import OrderState, WorkingOrder
from baskfy_core.vbt.plan import (
    BookState,
    Candidate,
    LineKind,
    PlanLine,
    Skipped,
    SkipReason,
    assemble,
    build_entries,
    exit_lines,
    to_tick,
)
from baskfy_core.vbt.sizing import SizeCap

SESSION = dt.date(2026, 3, 2)
LAKH = Decimal("1000000")


def candidate(  # noqa: PLR0913 - a signal is its levels
    *,
    instrument_id: int = 1,
    symbol: str = "ACME",
    close: str = "100.00",
    turnover: str | None = "500000000",
    rank: int = 1_000,
    locked: bool = False,
) -> Candidate:
    return Candidate(
        instrument_id=instrument_id,
        symbol=symbol,
        signal_date=SESSION,
        close_raw=Decimal(close),
        turnover_avg_inr=None if turnover is None else Decimal(turnover),
        rank_key=rank,
        locked_upper_circuit=locked,
    )


def entries(  # noqa: PLR0913 - the same knobs build_entries takes
    candidates: list[Candidate],
    *,
    gate: Gate = Gate.OPEN,
    equity: Decimal = LAKH,
    book: BookState | None = None,
    config: VbtConfig = DEFAULT_VBT_CONFIG,
    max_open_positions: int | None = None,
) -> tuple[list[PlanLine], list[Skipped]]:
    return build_entries(
        candidates,
        gate=gate,
        equity=equity,
        book=book or BookState(cash_available_inr=equity),
        config=config,
        max_open_positions=max_open_positions,
    )


class TestTheOrdinaryPlan:
    def test_a_signal_becomes_a_limit_at_its_close_with_a_twelve_percent_stop(self) -> None:
        lines, skips = entries([candidate()])
        assert skips == []
        line = lines[0]
        assert line.kind is LineKind.PLACE_LIMIT
        assert line.limit_price == Decimal("100.00")
        assert line.stop_price == Decimal("88.00")
        assert line.quantity == 1_000
        assert line.cap is SizeCap.SLOT
        assert "signal close" in line.note

    def test_the_level_is_snapped_to_the_exchange_s_tick(self) -> None:
        lines, _ = entries([candidate(close="137.37")])
        assert lines[0].limit_price == Decimal("137.35")
        assert to_tick(Decimal("137.37")) == Decimal("137.35")

    def test_cash_spent_by_an_earlier_line_is_not_spent_twice(self) -> None:
        book = BookState(cash_available_inr=Decimal("150000"))
        lines, skips = entries(
            [
                candidate(instrument_id=1, symbol="AAA", rank=3),
                candidate(instrument_id=2, symbol="BBB", rank=2),
            ],
            book=book,
        )
        assert lines[0].value_inr == Decimal("100000.00")
        assert lines[1].value_inr == Decimal("50000.00")
        assert lines[1].cap is SizeCap.CASH
        assert skips == []

    def test_candidates_are_taken_in_turnover_order(self) -> None:
        lines, _ = entries(
            [
                candidate(instrument_id=1, symbol="SMALL", rank=10),
                candidate(instrument_id=2, symbol="LARGE", rank=99),
            ]
        )
        assert [line.symbol for line in lines] == ["LARGE", "SMALL"]


class TestTheOrderOfTheChecks:
    def test_no_capital_outranks_everything(self) -> None:
        _, skips = entries([candidate(locked=True)], equity=Decimal(0), gate=Gate.SHUT)
        assert skips[0].reason is SkipReason.NO_SLEEVE_CAPITAL

    def test_a_shut_gate_outranks_the_circuit_flag(self) -> None:
        _, skips = entries([candidate(locked=True)], gate=Gate.SHUT)
        assert skips[0].reason is SkipReason.GATE_SHUT

    def test_a_locked_bar_is_skipped_with_its_own_reason(self) -> None:
        _, skips = entries([candidate(locked=True)])
        assert skips[0].reason is SkipReason.LOCKED_UPPER_CIRCUIT

    def test_a_name_already_held_is_never_added_to(self) -> None:
        """One position per name; never averaged down, in any state."""
        book = BookState(open_instrument_ids=frozenset({1}), cash_available_inr=LAKH)
        _, skips = entries([candidate(instrument_id=1)], book=book)
        assert skips[0].reason is SkipReason.ALREADY_HELD

    def test_a_name_with_a_resting_limit_is_not_bid_for_twice(self) -> None:
        book = BookState(working_instrument_ids=frozenset({1}), cash_available_inr=LAKH)
        _, skips = entries([candidate(instrument_id=1)], book=book)
        assert skips[0].reason is SkipReason.ALREADY_WORKING


class TestTheSessionCap:
    def test_three_lines_and_the_fourth_is_a_skip(self) -> None:
        many = [candidate(instrument_id=i, symbol=f"N{i}", rank=100 - i) for i in range(1, 6)]
        lines, skips = entries(many)
        assert len(lines) == 3
        assert [skip.reason for skip in skips] == [SkipReason.SESSION_CAP] * 2

    def test_entries_already_taken_this_session_count(self) -> None:
        """`04` §5.3 — lines in this plan **plus** what the session has already confirmed, so a
        fourth confirm of an evening is a refusal rather than a fourth order."""
        book = BookState(cash_available_inr=LAKH, entries_already_this_session=2)
        lines, skips = entries(
            [candidate(instrument_id=i, symbol=f"N{i}", rank=100 - i) for i in range(1, 4)],
            book=book,
        )
        assert len(lines) == 1
        assert skips[0].reason is SkipReason.SESSION_CAP


class TestSlots:
    def test_a_working_order_holds_a_slot(self) -> None:
        book = BookState(
            open_instrument_ids=frozenset(range(100, 105)),
            working_instrument_ids=frozenset(range(200, 205)),
            cash_available_inr=LAKH,
        )
        _, skips = entries([candidate()], book=book)
        assert skips[0].reason is SkipReason.SLOTS_FULL
        assert "10 slots" in skips[0].detail

    def test_the_traders_own_cap_can_only_lower_the_book(self) -> None:
        book = BookState(open_instrument_ids=frozenset({100, 101}), cash_available_inr=LAKH)
        _, skips = entries([candidate()], book=book, max_open_positions=2)
        assert skips[0].reason is SkipReason.SLOTS_FULL

    def test_it_can_never_raise_it_past_ten(self) -> None:
        book = BookState(open_instrument_ids=frozenset(range(100, 110)), cash_available_inr=LAKH)
        _, skips = entries([candidate()], book=book, max_open_positions=50)
        assert skips[0].reason is SkipReason.SLOTS_FULL


class TestTheMoneyRefusals:
    def test_a_sleeve_with_no_cash_left_says_so(self) -> None:
        book = BookState(cash_available_inr=Decimal("5000"))
        _, skips = entries([candidate()], book=book)
        assert skips[0].reason is SkipReason.BELOW_MIN_TRADE_VALUE

    def test_a_thin_name_is_a_liquidity_verdict_about_the_name(self) -> None:
        _, skips = entries([candidate(turnover="500000")])
        assert skips[0].reason is SkipReason.TURNOVER_CAP
        assert "20-day turnover" in skips[0].detail

    def test_exposure_beyond_the_sleeve_is_refused(self) -> None:
        book = BookState(cash_available_inr=LAKH, open_exposure_inr=Decimal("950000"))
        _, skips = entries([candidate()], book=book)
        assert skips[0].reason is SkipReason.EXPOSURE_FULL


class TestExitLines:
    def test_a_queued_ema_exit_becomes_a_sell_at_open(self) -> None:
        action = ManageAction(Action.QUEUE_SELL_AT_OPEN, ExitReason.EMA_EXIT, None, "below")
        lines = exit_lines([(1, "ACME", 250, action)], [], BookState())
        assert lines[0].kind is LineKind.SELL_AT_OPEN
        assert lines[0].quantity == 250
        assert lines[0].reason is ExitReason.EMA_EXIT

    def test_a_hold_produces_nothing(self) -> None:
        action = ManageAction(Action.HOLD, None, None, "above the EMA")
        assert exit_lines([(1, "ACME", 250, action)], [], BookState()) == []

    def test_an_expired_order_becomes_a_cancel(self) -> None:
        order = WorkingOrder(
            instrument_id=7,
            signal_date=SESSION,
            limit_price=Decimal("55.00"),
            stop_price=Decimal("48.40"),
            quantity=100,
            state=OrderState.CANCELLED,
        )
        lines = exit_lines([], [(order, "OLDCO")], BookState())
        assert lines[0].kind is LineKind.CANCEL_LIMIT
        assert lines[0].quantity == 100

    def test_a_naked_position_gets_a_gtt_line(self) -> None:
        book = BookState(positions_naked_of_gtt=((3, "BARE", 40, Decimal("70.00")),))
        lines = exit_lines([], [], book)
        assert lines[0].kind is LineKind.ARM_GTT
        assert lines[0].stop_price == Decimal("70.00")
        assert "forbids" in lines[0].note


class TestAssembly:
    def test_exits_come_before_cancels_before_gtts_before_entries(self) -> None:
        action = ManageAction(Action.QUEUE_SELL_AT_OPEN, ExitReason.EMA_EXIT, None, "below")
        order = WorkingOrder(
            instrument_id=7,
            signal_date=SESSION,
            limit_price=Decimal("55.00"),
            stop_price=Decimal("48.40"),
            quantity=100,
            state=OrderState.CANCELLED,
        )
        book = BookState(
            cash_available_inr=LAKH, positions_naked_of_gtt=((3, "BARE", 40, Decimal("70.00")),)
        )
        exits = exit_lines([(1, "SELLME", 250, action)], [(order, "OLDCO")], book)
        lines, skips = entries([candidate(instrument_id=9, symbol="NEWCO")], book=book)
        plan = assemble(SESSION, Gate.OPEN, LAKH, exits, lines, skips)
        assert [line.kind for line in plan.lines] == [
            LineKind.SELL_AT_OPEN,
            LineKind.CANCEL_LIMIT,
            LineKind.ARM_GTT,
            LineKind.PLACE_LIMIT,
        ]

    def test_the_totals_are_over_the_entries_only(self) -> None:
        lines, skips = entries([candidate()])
        plan = assemble(SESSION, Gate.OPEN, LAKH, [], lines, skips)
        assert plan.total_new_exposure_inr == Decimal("100000.00")
        assert len(plan.entries) == 1

    def test_the_same_plan_hashes_the_same(self) -> None:
        lines, skips = entries([candidate()])
        first = assemble(SESSION, Gate.OPEN, LAKH, [], lines, skips)
        second = assemble(SESSION, Gate.OPEN, LAKH, [], lines, skips)
        assert first.plan_hash() == second.plan_hash()

    def test_a_different_quantity_hashes_differently(self) -> None:
        lines, skips = entries([candidate()])
        first = assemble(SESSION, Gate.OPEN, LAKH, [], lines, skips)
        other, _ = entries([candidate()], equity=Decimal("500000"))
        second = assemble(SESSION, Gate.OPEN, LAKH, [], other, skips)
        assert first.plan_hash() != second.plan_hash()

    def test_the_hash_carries_no_floats(self) -> None:
        """Prices go into the hash as strings of their exact decimal, so a plan built on two
        machines cannot disagree about its own identity."""
        lines, _ = entries([candidate()])
        canonical = lines[0].canonical()
        assert canonical["limit_price"] == "100.00"
        assert all(not isinstance(value, float) for value in canonical.values())

    def test_a_plan_keeps_its_skips(self) -> None:
        lines, skips = entries([candidate(locked=True)])
        plan = assemble(SESSION, Gate.OPEN, LAKH, [], lines, skips)
        assert len(plan.skips) == 1
        assert plan.skips[0].reason is SkipReason.LOCKED_UPPER_CIRCUIT


def test_every_skip_reason_is_reachable_from_the_plan() -> None:
    """A reason nothing can produce is a reason a page will never explain."""
    produced = set()
    produced.add(entries([candidate()], equity=Decimal(0))[1][0].reason)
    produced.add(entries([candidate()], gate=Gate.SHUT)[1][0].reason)
    produced.add(entries([candidate(locked=True)])[1][0].reason)
    produced.add(
        entries(
            [candidate()],
            book=BookState(open_instrument_ids=frozenset({1}), cash_available_inr=LAKH),
        )[1][0].reason
    )
    produced.add(
        entries(
            [candidate()],
            book=BookState(working_instrument_ids=frozenset({1}), cash_available_inr=LAKH),
        )[1][0].reason
    )
    produced.add(
        entries([candidate(instrument_id=i, symbol=f"N{i}", rank=100 - i) for i in range(1, 6)])[1][
            0
        ].reason
    )
    produced.add(
        entries(
            [candidate()],
            book=BookState(open_instrument_ids=frozenset(range(100, 110)), cash_available_inr=LAKH),
        )[1][0].reason
    )
    produced.add(
        entries([candidate()], book=BookState(cash_available_inr=Decimal("5000")))[1][0].reason
    )
    produced.add(entries([candidate(turnover="500000")])[1][0].reason)
    produced.add(
        entries(
            [candidate()],
            book=BookState(cash_available_inr=LAKH, open_exposure_inr=Decimal("950000")),
        )[1][0].reason
    )
    unreachable = set(SkipReason) - produced
    assert unreachable == {SkipReason.STOP_NOT_BELOW_ENTRY}, (
        "STOP_NOT_BELOW_ENTRY is the one reason the plan cannot reach from a valid signal: the "
        "stop is derived from the limit by a fixed percentage, so it is below it by construction. "
        "It stays in the vocabulary because `size_entry` is called from the desk's confirm path "
        f"too, where the level arrives from outside. Unexpectedly unreachable: {unreachable}"
    )


def test_no_line_kind_can_short_or_carry_a_product_other_than_delivery() -> None:
    """`02` Track C §1 — four kinds, and none of them is an opening sell."""
    assert {kind.value for kind in LineKind} == {
        "PLACE_LIMIT",
        "SELL_AT_OPEN",
        "CANCEL_LIMIT",
        "ARM_GTT",
    }


@pytest.mark.parametrize("gate", list(Gate))
def test_the_gate_has_exactly_two_values(gate: Gate) -> None:
    """DECISIONS-VB VB0.4 — no amber, because there is no ladder for one to feed."""
    assert gate.value in {"OPEN", "SHUT"}
    assert len(Gate) == 2
