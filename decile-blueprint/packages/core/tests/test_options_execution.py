"""``docs/options/04`` §8 — sequences, never naked, limits and reprices, the depth ladder, the
shared exit plumbing and the expiry-day slot.

The never-naked property (Track C §2) runs over 500 seeded fill sequences per structure, entry
and exit.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import StrEnum

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from options_fixtures import LOT, TICK

from baskfy_core.options.chain import Level
from baskfy_core.options.config import DEFAULT_OPTIONS_CONFIG, ExecutionConfig, Side, Sleeve
from baskfy_core.options.execution import (
    EntryStep,
    LegRole,
    SlotVerdict,
    advance_entry,
    drop_on_stale,
    entry_sequence,
    exit_sequence,
    feed_lost,
    first_by_precedence,
    never_naked,
    next_attempt,
    next_exit_leg,
    simulate_fill,
    slot_after_confirm,
    slot_after_release,
    slot_verdict,
)

EXE = DEFAULT_OPTIONS_CONFIG.execution
STALE_INDEX = DEFAULT_OPTIONS_CONFIG.chain.stale_index_seconds
LC, LP, SC, SP = LegRole.LONG_CALL, LegRole.LONG_PUT, LegRole.SHORT_CALL, LegRole.SHORT_PUT
CONDOR = (SC, LP, SP, LC)
CALL_SPREAD = (SC, LC)
PUT_SPREAD = (LP, SP)
STRUCTURES = {"condor": CONDOR, "call_spread": CALL_SPREAD, "put_spread": PUT_SPREAD}


class TestSequences:
    def test_condor_entry_is_wings_first_puts_first(self) -> None:
        """Condor ``04`` §4.6."""
        assert entry_sequence(CONDOR) == (LP, LC, SP, SC)

    def test_condor_exit_is_shorts_first_calls_first(self) -> None:
        """Condor ``04`` §4.7."""
        assert exit_sequence(CONDOR) == (SC, SP, LC, LP)

    def test_debit_spread_long_in_first_short_out_first(self) -> None:
        """``04`` §5.4."""
        assert entry_sequence(CALL_SPREAD) == (LC, SC)
        assert exit_sequence(CALL_SPREAD) == (SC, LC)
        assert entry_sequence(PUT_SPREAD) == (LP, SP)

    def test_a_long_option_is_one_leg(self) -> None:
        assert entry_sequence((LC,)) == exit_sequence((LC,)) == (LC,)

    def test_a_naked_short_is_refused(self) -> None:
        with pytest.raises(ValueError, match="naked"):
            entry_sequence((SC, LP, SP))

    def test_a_duplicate_role_is_refused(self) -> None:
        with pytest.raises(ValueError, match="one leg per role"):
            entry_sequence((LC, LC))

    def test_leg_role_sides(self) -> None:
        assert (LC.entry_side, LC.exit_side, SP.entry_side, SP.exit_side) == (
            Side.BUY,
            Side.SELL,
            Side.SELL,
            Side.BUY,
        )
        assert (SC.protector, SP.protector, LC.protector) == (LC, LP, None)


class TestAdvanceEntry:
    SEQ = entry_sequence(CONDOR)

    def test_full_fills_send_each_leg_then_open(self) -> None:
        steps = [advance_entry(self.SEQ, LOT, [LOT] * n) for n in range(5)]
        assert [s.leg for s in steps[:4]] == list(self.SEQ)
        assert steps[4].step is EntryStep.OPEN

    def test_a_partial_wing_abandons_before_any_short(self) -> None:
        action = advance_entry(self.SEQ, LOT, [LOT, 30])
        assert action.step is EntryStep.ABANDON
        assert action.close == ((LC, 30), (LP, LOT))

    def test_a_cancelled_wing_closes_the_filled_one(self) -> None:
        assert advance_entry(self.SEQ, LOT, [LOT, 0]).close == ((LP, LOT),)

    def test_a_partial_short_does_not_send_the_other_short(self) -> None:
        action = advance_entry(self.SEQ, LOT, [LOT, LOT, 20])
        assert action.step is EntryStep.ABANDON
        assert action.close == ((SP, 20), (LC, LOT), (LP, LOT))

    def test_bad_outcomes_raise(self) -> None:
        with pytest.raises(ValueError, match="more outcomes"):
            advance_entry((LC,), LOT, [LOT, LOT])
        with pytest.raises(ValueError, match="filled"):
            advance_entry((LC,), LOT, [LOT + 1])


def _walk_entry(roles: tuple[LegRole, ...], qty: int, fills: list[int]) -> None:
    """Drive the reducer with seeded fills; assert never-naked after every fill and every close."""
    seq = entry_sequence(roles)
    position = dict.fromkeys(seq, 0)
    outcomes: list[int] = []
    draws = iter(fills)
    while True:
        action = advance_entry(seq, qty, outcomes)
        if action.step is EntryStep.OPEN:
            assert all(position[r] == qty for r in seq)
            return
        if action.step is EntryStep.ABANDON:
            for role, filled in action.close:
                assert position[role] == filled
                position[role] = 0
                assert never_naked(position)
            assert all(v == 0 for v in position.values())
            return
        assert action.leg is not None
        if not action.leg.is_long:
            assert all(position[r] == qty for r in seq if r.is_long), (
                "a short before every long filled"
            )
        filled = min(next(draws, qty), qty)
        position[action.leg] = filled
        outcomes.append(filled)
        assert never_naked(position)


def _walk_exit(roles: tuple[LegRole, ...], qty: int, fills: list[int]) -> None:
    position = dict.fromkeys(roles, qty)
    draws = iter(fills)
    for _ in range(200):
        leg = next_exit_leg(position)
        if leg is None:
            assert all(v == 0 for v in position.values())
            return
        if leg.is_long:
            assert all(position[s] == 0 for s in (SC, SP) if s in position and s.protector is leg)
        position[leg] -= min(next(draws, qty), position[leg])
        assert never_naked(position)
    pytest.fail("exit did not reach flat")


fill_sequences = st.lists(st.integers(min_value=0, max_value=LOT), min_size=0, max_size=12)


@pytest.mark.parametrize("name", list(STRUCTURES))
@settings(max_examples=500, deadline=None, derandomize=True)
@given(fills=fill_sequences)
def test_never_naked_through_entry(name: str, fills: list[int]) -> None:
    _walk_entry(STRUCTURES[name], LOT, fills)


@pytest.mark.parametrize("name", list(STRUCTURES))
@settings(max_examples=500, deadline=None, derandomize=True)
@given(fills=st.lists(st.integers(min_value=1, max_value=LOT), min_size=0, max_size=40))
def test_never_naked_through_exit(name: str, fills: list[int]) -> None:
    _walk_exit(STRUCTURES[name], LOT, fills)


def test_next_exit_leg_holds_a_wing_while_its_short_is_open() -> None:
    assert next_exit_leg({SC: 0, SP: 10, LC: LOT, LP: LOT}) == SP
    assert next_exit_leg({SC: 0, SP: 0, LC: LOT, LP: LOT}) == LC
    assert next_exit_leg({LC: 0}) is None


class TestAttempts:
    """``04`` §8.2."""

    BID, ASK = Decimal("100.00"), Decimal("100.50")

    def _at(
        self, n: int, side: Side, *, reduces: bool = False, cfg: ExecutionConfig = EXE
    ) -> Decimal | None:
        attempt = next_attempt(
            n, side, self.BID, self.ASK, TICK, closing_reduces_risk=reduces, config=cfg
        )
        return None if attempt is None else attempt.price

    def test_entry_buy_improves_then_reprices_then_cancels(self) -> None:
        assert self._at(1, Side.BUY) == Decimal("100.55")
        assert self._at(2, Side.BUY) == Decimal("100.60")
        assert self._at(3, Side.BUY) is None

    def test_entry_sell(self) -> None:
        assert self._at(1, Side.SELL) == Decimal("99.95")
        assert self._at(2, Side.SELL) == Decimal("99.90")

    def test_a_risk_reducing_close_gets_a_marketable_limit_at_the_touch(self) -> None:
        attempt = next_attempt(
            3, Side.BUY, self.BID, self.ASK, TICK, closing_reduces_risk=True, config=EXE
        )
        assert attempt is not None
        assert (attempt.price, attempt.marketable) == (self.ASK, True)
        assert self._at(4, Side.BUY, reduces=True) is None

    def test_the_flag_turns_the_third_attempt_off(self) -> None:
        off = ExecutionConfig(exit_final_marketable=False)
        assert self._at(3, Side.BUY, reduces=True, cfg=off) is None

    def test_a_sell_never_prices_below_a_tick(self) -> None:
        attempt = next_attempt(
            2,
            Side.SELL,
            Decimal("0.05"),
            Decimal("0.10"),
            TICK,
            closing_reduces_risk=False,
            config=EXE,
        )
        assert attempt is not None
        assert attempt.price == TICK

    def test_attempts_count_from_one(self) -> None:
        with pytest.raises(ValueError, match="from 1"):
            self._at(0, Side.BUY)


class TestDepthLadder:
    """``04`` §8.4: walk the side taken up to the limit, +1 tick per level beyond the first."""

    ASKS = (
        Level(Decimal("100.00"), 65),
        Level(Decimal("100.05"), 65),
        Level(Decimal("100.10"), 100),
    )
    BIDS = (Level(Decimal("99.95"), 65), Level(Decimal("99.90"), 65))

    def test_two_levels_fill_in_full(self) -> None:
        got = simulate_fill(
            Side.BUY, self.ASKS, 130, limit_price=Decimal("100.20"), tick=TICK, config=EXE
        )
        assert (got.filled, got.avg_price, got.levels_consumed, got.complete) == (
            130,
            Decimal("100.05"),
            2,
            True,
        )

    def test_the_limit_stops_the_walk_and_the_rest_is_partial(self) -> None:
        got = simulate_fill(
            Side.BUY, self.ASKS, 130, limit_price=Decimal("100.05"), tick=TICK, config=EXE
        )
        assert (got.filled, got.avg_price, got.complete) == (65, Decimal("100.00"), False)

    def test_a_sell_walks_the_bids_downward(self) -> None:
        got = simulate_fill(
            Side.SELL, self.BIDS, 130, limit_price=Decimal("99.80"), tick=TICK, config=EXE
        )
        assert got.avg_price == Decimal("99.90")  # 65 @ 99.95 + 65 @ (99.90 - 0.05 = 99.85)

    def test_no_depth_no_fill_and_no_ltp_fallback(self) -> None:
        got = simulate_fill(Side.BUY, (), 65, limit_price=Decimal("999"), tick=TICK, config=EXE)
        assert (got.filled, got.avg_price) == (0, None)

    def test_empty_levels_are_skipped(self) -> None:
        ladder = (Level(Decimal("100"), 0), Level(Decimal("100.05"), 65))
        got = simulate_fill(
            Side.BUY, ladder, 65, limit_price=Decimal("100.05"), tick=TICK, config=EXE
        )
        assert got.avg_price == Decimal("100.05")

    def test_quantity_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            simulate_fill(Side.BUY, self.ASKS, 0, limit_price=Decimal(1), tick=TICK, config=EXE)


class _O2(StrEnum):
    HARD_EXIT = "HARD_EXIT"
    STOP = "STOP"
    INVALIDATED = "INVALIDATED"
    TARGET = "TARGET"
    TIME_STOP = "TIME_STOP"


O2_PRECEDENCE = (_O2.HARD_EXIT, _O2.STOP, _O2.INVALIDATED, _O2.TARGET, _O2.TIME_STOP)


class TestExitPlumbing:
    def test_precedence(self) -> None:
        assert first_by_precedence([_O2.TARGET, _O2.STOP], O2_PRECEDENCE) is _O2.STOP
        assert first_by_precedence([_O2.TIME_STOP, _O2.HARD_EXIT], O2_PRECEDENCE) is _O2.HARD_EXIT
        assert first_by_precedence([], O2_PRECEDENCE) is None

    def test_an_unranked_code_raises(self) -> None:
        with pytest.raises(ValueError, match="precedence"):
            first_by_precedence([_O2.STOP], (_O2.HARD_EXIT,))

    def test_a_stale_quote_never_takes_profit(self) -> None:
        fired = [_O2.TARGET, _O2.STOP]
        assert drop_on_stale(fired, [_O2.TARGET], stale=True) == (_O2.STOP,)
        assert drop_on_stale(fired, [_O2.TARGET], stale=False) == (_O2.TARGET, _O2.STOP)


class TestFeedLost:
    """``04`` §8.5."""

    def _lost(self, sleeve: Sleeve, now: str, tick_ago: int | None, opened_ago: int | None) -> bool:
        at = dt.datetime.fromisoformat(f"2026-10-27T{now}")
        return feed_lost(
            sleeve,
            now=at,
            last_index_tick=None if tick_ago is None else at - dt.timedelta(seconds=tick_ago),
            position_opened_at=None
            if opened_ago is None
            else at - dt.timedelta(seconds=opened_ago),
            stale_index_seconds=STALE_INDEX,
            config=EXE,
        )

    def test_o1_before_1400_is_not_lost(self) -> None:
        assert not self._lost(Sleeve.O1M, "13:59:59", 31, 3600)

    def test_o1_after_1400_silent_is_lost(self) -> None:
        assert self._lost(Sleeve.O1M, "14:00:00", 31, 3600)
        assert self._lost(Sleeve.O3A, "14:10:00", None, 3600)

    def test_a_recent_tick_is_fine(self) -> None:
        assert not self._lost(Sleeve.O1W, "14:10:00", 30, 3600)

    def test_no_position_no_exit(self) -> None:
        assert not self._lost(Sleeve.O1M, "14:10:00", None, None)

    def test_o2_any_time_after_the_grace(self) -> None:
        assert self._lost(Sleeve.O2, "11:00:00", 31, 61)
        assert not self._lost(Sleeve.O2, "11:00:00", 31, 60)


class TestSlot:
    """``04`` §8.6."""

    def test_o2_is_outside(self) -> None:
        assert slot_verdict(Sleeve.O2, Sleeve.O1M) is SlotVerdict.OUTSIDE_SLOT
        assert slot_after_confirm(Sleeve.O2, Sleeve.O1M) is Sleeve.O1M

    def test_first_confirmed_holds_it(self) -> None:
        assert slot_verdict(Sleeve.O3B, None) is SlotVerdict.FREE
        holder = slot_after_confirm(Sleeve.O3B, None)
        assert holder is Sleeve.O3B
        assert slot_verdict(Sleeve.O1W, holder) is SlotVerdict.SLOT_TAKEN
        assert slot_verdict(Sleeve.O3B, holder) is SlotVerdict.HELD_BY_SELF
        with pytest.raises(ValueError, match="held by"):
            slot_after_confirm(Sleeve.O1W, holder)

    def test_release_frees_only_its_own(self) -> None:
        assert slot_after_release(Sleeve.O1M, Sleeve.O1M) is None
        assert slot_after_release(Sleeve.O3A, Sleeve.O1M) is Sleeve.O1M
