"""A session's plan (``docs/twt/04`` §10).

``04`` §10.1's skips are checked **in order**, and the order decides which reason a row carries: a
name that is both already held and below the liquidity floor is ``ALREADY_HELD``, because that is
the first thing a person needs to know about it. Each test below moves one input and names the
reason it expects.

The other half of this file is about what the plan **cannot** do: ``exit_lines`` emits no
``SELL_AT_OPEN`` (``03`` §7 — the shape this package copied has such a rule and TWT-1 does not), and
no ``RAISE_GTT_STOP`` at or below a resting trigger.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from baskfy_core.twt.config import DEFAULT_TWT_CONFIG, Gate, SizingConfig, TwtConfig
from baskfy_core.twt.plan import (
    LINE_ORDER,
    BookState,
    Candidate,
    EntryBar,
    LineKind,
    NakedPosition,
    PlanLine,
    RatchetDue,
    Skipped,
    SkipReason,
    assemble,
    build_entries,
    client_id_for,
    exit_lines,
    gtt_limit_price,
)
from baskfy_core.twt.sizing import SizeCap

SESSION = dt.date(2024, 6, 3)
NEXT = dt.date(2024, 6, 4)
EQUITY = Decimal("2500000")
DEEP = Decimal("500000000")
PRICE = Decimal("100")


def candidate(  # noqa: PLR0913 - a candidate is its fields
    *,
    instrument_id: int = 1,
    symbol: str = "TWTCO",
    rank_key: Decimal = Decimal("90000000"),
    turnover: Decimal | None = DEEP,
    price: Decimal = PRICE,
    entry_bar: EntryBar | None = None,
) -> Candidate:
    return Candidate(
        instrument_id=instrument_id,
        symbol=symbol,
        signal_date=SESSION,
        rank_key=rank_key,
        turnover_avg_inr=turnover,
        entry_price=price,
        stop_reference_price=price,
        entry_bar=entry_bar,
    )


def book(  # noqa: PLR0913 - the book is what the plan consults, one field at a time
    *,
    open_instrument_ids: frozenset[int] = frozenset(),
    open_exposure_inr: Decimal = Decimal(0),
    cash_available_inr: Decimal = EQUITY,
    entries_already_this_session: int = 0,
    positions_naked_of_gtt: tuple[NakedPosition, ...] = (),
    ratchets_due: tuple[RatchetDue, ...] = (),
) -> BookState:
    return BookState(
        open_instrument_ids=open_instrument_ids,
        open_exposure_inr=open_exposure_inr,
        cash_available_inr=cash_available_inr,
        entries_already_this_session=entries_already_this_session,
        positions_naked_of_gtt=positions_naked_of_gtt,
        ratchets_due=ratchets_due,
    )


def build(  # noqa: PLR0913 - one keyword per gate the plan consults
    candidates: list[Candidate],
    *,
    gate: Gate = Gate.OPEN,
    equity: Decimal = EQUITY,
    state: BookState | None = None,
    config: TwtConfig = DEFAULT_TWT_CONFIG,
    multiplier: Decimal = Decimal(1),
    max_open: int | None = None,
) -> tuple[list[PlanLine], list[Skipped]]:
    return build_entries(
        candidates,
        gate=gate,
        equity=equity,
        book=state if state is not None else book(),
        config=config,
        slot_multiplier=multiplier,
        max_open_positions=max_open,
    )


class TestTheHappyLine:
    def test_a_signal_becomes_a_buy_at_the_next_open(self) -> None:
        lines, skips = build([candidate()])
        assert skips == []
        assert len(lines) == 1
        line = lines[0]
        assert line.kind is LineKind.BUY_AT_OPEN
        assert line.quantity == 2_500
        assert line.stop_price == Decimal("80")
        assert line.value_inr == Decimal("250000")
        assert line.cap is SizeCap.SLOT

    def test_the_stop_previewed_is_twenty_percent_under_the_reference_price(self) -> None:
        lines, _ = build([candidate(price=Decimal("101.23"))])
        assert lines[0].stop_price == Decimal("80.95")


class TestTheSkipsInOrder:
    """``04`` §10.1, top to bottom."""

    def test_a_shut_gate_stops_everything_first(self) -> None:
        lines, skips = build(
            [candidate(turnover=Decimal("1"))],
            gate=Gate.SHUT,
            state=book(open_instrument_ids=frozenset({1})),
        )
        assert lines == []
        assert [skip.reason for skip in skips] == [SkipReason.GATE_SHUT]

    def test_a_name_already_held_is_never_averaged_down(self) -> None:
        _, skips = build(
            [candidate(turnover=Decimal("1"))], state=book(open_instrument_ids=frozenset({1}))
        )
        assert skips[0].reason is SkipReason.ALREADY_HELD

    def test_a_name_under_the_liquidity_floor_is_skipped(self) -> None:
        floor = DEFAULT_TWT_CONFIG.entry.min_turnover_inr
        _, skips = build([candidate(turnover=floor - Decimal(1))])
        assert skips[0].reason is SkipReason.BELOW_LIQUIDITY_FLOOR
        lines, _ = build([candidate(turnover=floor)])
        assert len(lines) == 1

    def test_a_name_with_no_turnover_window_is_under_the_floor(self) -> None:
        _, skips = build([candidate(turnover=None)])
        assert skips[0].reason is SkipReason.BELOW_LIQUIDITY_FLOOR

    def test_no_bar_on_the_entry_session_is_a_skip(self) -> None:
        _, skips = build([candidate(entry_bar=EntryBar(session=NEXT, open=None))])
        assert skips[0].reason is SkipReason.NO_BAR

    def test_a_bar_locked_at_the_open_is_a_skip(self) -> None:
        """``04`` §5.2: ``open == high == low`` means no fill is possible at a price the book would
        accept."""
        locked = EntryBar(session=NEXT, open=Decimal("110"), limit_locked=True)
        _, skips = build([candidate(entry_bar=locked)])
        assert skips[0].reason is SkipReason.LOCKED_UPPER_CIRCUIT

    def test_an_evening_plan_has_no_bar_to_judge_and_does_not_invent_one(self) -> None:
        """The evening plan is built for tomorrow's open and tomorrow has no bar yet. Neither
        bar-shaped skip may fire on an absence of information."""
        lines, skips = build([candidate(entry_bar=None)])
        assert skips == []
        assert len(lines) == 1

    def test_the_session_cap_is_three_new_entries(self) -> None:
        names = [candidate(instrument_id=i, symbol=f"N{i}") for i in range(5)]
        lines, skips = build(names)
        assert len(lines) == DEFAULT_TWT_CONFIG.sizing.max_new_entries_per_session == 3
        assert {skip.reason for skip in skips} == {SkipReason.SESSION_CAP}

    def test_the_cap_counts_orders_already_confirmed_this_session(self) -> None:
        """``04`` §6.3: lines in this plan **plus** the session's already-confirmed or sent orders,
        whatever plan they came from — so a fourth confirm of an evening is a refusal, not a
        surprise."""
        names = [candidate(instrument_id=i, symbol=f"N{i}") for i in range(5)]
        lines, _ = build(
            names, state=book(cash_available_inr=EQUITY, entries_already_this_session=2)
        )
        assert len(lines) == 1

    def test_slots_full_counts_the_positions_the_sleeve_holds(self) -> None:
        held = frozenset(range(100, 110))
        _, skips = build([candidate()], state=book(open_instrument_ids=held))
        assert skips[0].reason is SkipReason.SLOTS_FULL

    def test_the_slot_ceiling_is_the_traders_own_setting(self) -> None:
        held = frozenset(range(100, 110))
        lines, _ = build(
            [candidate()],
            state=book(open_instrument_ids=held, cash_available_inr=EQUITY),
            max_open=12,
        )
        assert len(lines) == 1

    def test_a_sleeve_with_no_capital_plans_nothing(self) -> None:
        _, skips = build([candidate()], equity=Decimal(0))
        assert skips[0].reason is SkipReason.NO_SLEEVE_CAPITAL

    def test_a_line_too_small_to_clear_the_floor_is_skipped(self) -> None:
        tiny = TwtConfig(sizing=SizingConfig(min_trade_value_inr=Decimal("400000")))
        _, skips = build([candidate()], config=tiny)
        assert skips[0].reason is SkipReason.BELOW_MIN_TRADE_VALUE

    def test_the_exposure_ceiling_refuses_a_line_the_sleeve_cannot_carry(self) -> None:
        crowded = book(cash_available_inr=EQUITY, open_exposure_inr=EQUITY - Decimal("100000"))
        _, skips = build([candidate()], state=crowded)
        assert skips[0].reason is SkipReason.EXPOSURE_FULL

    def test_the_turnover_cap_alone_binding_is_not_a_skip(self) -> None:
        """``04`` §10.1, stated as an exception because it reads like one: it is a **smaller line
        with a note**, and a plan that skipped it would silently stop trading the names the floor
        was written to admit.

        It takes a ₹1 crore sleeve to reach the case at all, which is the next paragraph's point.
        """
        lines, skips = build([candidate(turnover=Decimal("60000000"))], equity=Decimal("10000000"))
        assert skips == []
        assert lines[0].cap is SizeCap.TURNOVER
        assert lines[0].value_inr == Decimal("600000")
        assert "turnover" in lines[0].note

    def test_at_twenty_five_lakh_the_five_crore_floor_puts_the_cap_out_of_reach(self) -> None:
        """DECISIONS-TW **TW0.3**'s coherence argument, as a test rather than a paragraph.

        Ten slots at ₹25 lakh is a ₹2.5 lakh line and 1 % of the floor is ₹5 lakh, so **a name that
        clears the floor can never be capped by its own turnover at this capital**. That is the
        whole reason the sleeve ships at ₹5 crore where the research used ₹2 crore: a floor below
        the cap means the plan is routinely sized by the cap rather than by the strategy."""
        floor = DEFAULT_TWT_CONFIG.entry.min_turnover_inr
        slot = EQUITY / Decimal(DEFAULT_TWT_CONFIG.sizing.max_slots)
        assert floor * DEFAULT_TWT_CONFIG.sizing.max_position_vs_turnover > slot
        lines, _ = build([candidate(turnover=floor)])
        assert lines[0].cap is SizeCap.SLOT


class TestTheOrderAndTheMoney:
    def test_signals_are_ranked_by_turnover_descending_then_symbol(self) -> None:
        names = [
            candidate(instrument_id=1, symbol="ZETA", rank_key=Decimal("100")),
            candidate(instrument_id=2, symbol="ALPHA", rank_key=Decimal("100")),
            candidate(instrument_id=3, symbol="MIDCO", rank_key=Decimal("900")),
        ]
        lines, _ = build(names)
        assert [line.symbol for line in lines] == ["MIDCO", "ALPHA", "ZETA"]

    def test_cash_spent_by_an_earlier_line_is_not_spent_twice(self) -> None:
        names = [candidate(instrument_id=i, symbol=f"N{i}") for i in range(3)]
        lines, skips = build(names, state=book(cash_available_inr=Decimal("400000")))
        assert [line.value_inr for line in lines] == [Decimal("250000"), Decimal("150000")]
        assert skips[0].reason is SkipReason.BELOW_MIN_TRADE_VALUE

    def test_half_size_reaches_the_line_that_is_shown(self) -> None:
        lines, _ = build([candidate()], multiplier=Decimal("0.5"))
        assert lines[0].value_inr == Decimal("125000")


class TestTheExitLines:
    """``04`` §10.2, and **nothing else**."""

    def test_a_position_without_a_resting_stop_gets_an_arm_gtt(self) -> None:
        naked = NakedPosition(
            instrument_id=1, symbol="TWTCO", quantity=100, stop_price=Decimal("80")
        )
        lines = exit_lines(book(positions_naked_of_gtt=(naked,)), SESSION)
        assert [line.kind for line in lines] == [LineKind.ARM_GTT]
        assert lines[0].stop_price == Decimal("80")

    def test_a_higher_trigger_computed_for_this_session_is_a_raise(self) -> None:
        due = RatchetDue(
            instrument_id=1,
            symbol="TWTCO",
            quantity=100,
            gtt_trigger=Decimal("80"),
            next_trigger=Decimal("120"),
            next_trigger_for=SESSION,
            high_since=Decimal("150"),
        )
        lines = exit_lines(book(ratchets_due=(due,)), SESSION)
        assert [line.kind for line in lines] == [LineKind.RAISE_GTT_STOP]
        assert lines[0].stop_price == Decimal("120")
        assert lines[0].previous_stop == Decimal("80")
        assert lines[0].high_since == Decimal("150")

    @pytest.mark.parametrize("trigger", [Decimal("80"), Decimal("79.95")])
    def test_a_trigger_at_or_below_the_resting_one_is_not_a_line(self, trigger: Decimal) -> None:
        """**A stop never falls**, and this is the second of the three places that say so."""
        due = RatchetDue(
            instrument_id=1,
            symbol="TWTCO",
            quantity=100,
            gtt_trigger=Decimal("80"),
            next_trigger=trigger,
            next_trigger_for=SESSION,
            high_since=Decimal("150"),
        )
        assert exit_lines(book(ratchets_due=(due,)), SESSION) == []

    def test_a_trigger_computed_for_another_session_is_not_read(self) -> None:
        """``03`` §5: a plan never reads a trigger computed for another session — a stale trigger
        is a stop derived from a price that is no longer the highest high."""
        due = RatchetDue(
            instrument_id=1,
            symbol="TWTCO",
            quantity=100,
            gtt_trigger=Decimal("80"),
            next_trigger=Decimal("120"),
            next_trigger_for=SESSION,
            high_since=Decimal("150"),
        )
        assert exit_lines(book(ratchets_due=(due,)), NEXT) == []

    def test_exit_lines_can_emit_no_sell_at_open(self) -> None:
        """``03`` §7. The strategy has no end-of-day sell rule; the GTT is the exit. The kind
        exists so a person can be given a line for a ``MANUAL`` exit without a migration."""
        naked = NakedPosition(instrument_id=1, symbol="A", quantity=1, stop_price=Decimal("1"))
        due = RatchetDue(
            instrument_id=2,
            symbol="B",
            quantity=1,
            gtt_trigger=Decimal("1"),
            next_trigger=Decimal("2"),
            next_trigger_for=SESSION,
            high_since=Decimal("3"),
        )
        lines = exit_lines(book(positions_naked_of_gtt=(naked,), ratchets_due=(due,)), SESSION)
        assert {line.kind for line in lines} <= {LineKind.ARM_GTT, LineKind.RAISE_GTT_STOP}
        assert LineKind.SELL_AT_OPEN in set(LineKind)


class TestAssembly:
    def test_exits_come_before_entries(self) -> None:
        naked = NakedPosition(instrument_id=9, symbol="AAA", quantity=1, stop_price=Decimal("1"))
        exits = exit_lines(book(positions_naked_of_gtt=(naked,)), SESSION)
        entries, skips = build([candidate()])
        plan = assemble(SESSION, Gate.OPEN, EQUITY, exits, entries, skips)
        assert [line.kind for line in plan.lines] == [LineKind.ARM_GTT, LineKind.BUY_AT_OPEN]
        assert LINE_ORDER.index(LineKind.ARM_GTT) < LINE_ORDER.index(LineKind.BUY_AT_OPEN)

    def test_the_totals_are_over_the_entries(self) -> None:
        entries, skips = build([candidate()])
        plan = assemble(SESSION, Gate.OPEN, EQUITY, [], entries, skips)
        assert plan.total_new_exposure_inr == Decimal("250000")

    def test_the_same_plan_hashes_the_same(self) -> None:
        entries, skips = build([candidate()])
        one = assemble(SESSION, Gate.OPEN, EQUITY, [], entries, skips)
        two = assemble(SESSION, Gate.OPEN, EQUITY, [], *build([candidate()]))
        assert one.plan_hash() == two.plan_hash()

    def test_a_different_quantity_hashes_differently(self) -> None:
        one = assemble(SESSION, Gate.OPEN, EQUITY, [], *build([candidate()]))
        two = assemble(
            SESSION, Gate.OPEN, EQUITY, [], *build([candidate()], multiplier=Decimal("0.5"))
        )
        assert one.plan_hash() != two.plan_hash()

    def test_a_plan_carries_its_skips_because_a_plan_without_them_is_not_honest(self) -> None:
        entries, skips = build([candidate(turnover=None)])
        plan = assemble(SESSION, Gate.OPEN, EQUITY, [], entries, skips)
        assert plan.lines == ()
        assert plan.skips[0].reason is SkipReason.BELOW_LIQUIDITY_FLOOR


class TestTheDesksOwnConventions:
    def test_an_order_is_keyed_by_plan_and_symbol(self) -> None:
        """``04`` §10.4 and non-negotiable 6: a re-posted plan cannot double-send."""
        assert client_id_for("p1", "TWTCO", LineKind.BUY_AT_OPEN) == "p1:TWTCO"

    def test_a_gtt_leg_is_keyed_by_plan_symbol_and_kind(self) -> None:
        assert client_id_for("p1", "TWTCO", LineKind.ARM_GTT) == "p1:TWTCO:ARM_GTT"
        assert client_id_for("p1", "TWTCO", LineKind.RAISE_GTT_STOP) == "p1:TWTCO:RAISE_GTT_STOP"

    def test_the_gtt_limit_sits_three_percent_under_its_trigger(self) -> None:
        """``04`` §10.6: a GTT fires a LIMIT order and Maulik uses market stops."""
        assert DEFAULT_TWT_CONFIG.exits.gtt_limit_fraction == Decimal("0.97")
        assert gtt_limit_price(Decimal("100"), DEFAULT_TWT_CONFIG) == Decimal("97")

    def test_the_stop_band_admits_this_sleeves_own_stop(self) -> None:
        """``04`` §10.7 and DECISIONS-TW **TW0.8**: a band that refused a 20 % stop would make
        non-negotiable 4 unsatisfiable — there would be no legal way to arm the GTT the
        non-negotiable requires."""
        exits = DEFAULT_TWT_CONFIG.exits
        stop_distance = exits.stop_pct / Decimal(100)
        assert exits.gtt_band_min_pct < stop_distance < exits.gtt_band_max_pct
        assert exits.gtt_band_max_pct == Decimal("0.30")
