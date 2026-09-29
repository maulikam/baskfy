"""docs/swing/04 §8 — breadth, the gate, and the exposure ladder."""

from __future__ import annotations

from decimal import Decimal

import polars as pl

from baskfy_core.swing.config import MarketConfig
from baskfy_core.swing.market import (
    BreadthSnapshot,
    ExposureTier,
    IndexReading,
    MarketGate,
    breadth_snapshot,
    drawdown_locked,
    exposure_tier,
    index_reading,
    intraday_tier,
    market_gate,
)

CONFIG = MarketConfig()
D = Decimal


def snapshot(pct_up: float) -> BreadthSnapshot:
    return BreadthSnapshot(500, pct_up, 3.0, 50.0)


#: His filter for longs is the index's 10-day MA against its 20-day; the close itself is not
#: consulted, so a pullback to a rising 10-day is still a long tape.
UP = IndexReading(close=96.0, ma_fast=98.0, ma_slow=97.0)
DOWN = IndexReading(close=99.0, ma_fast=96.0, ma_slow=97.0)


def test_breadth_counts_strong_movers_new_highs_and_ma_position() -> None:
    at = pl.DataFrame(
        {
            "ret_20": [30.0, 10.0, -5.0, 26.0],
            "close": [100.0, 50.0, 20.0, 80.0],
            "high_1y": [100.0, 60.0, 30.0, 70.0],
            "ma_slow": [90.0, 55.0, 25.0, 70.0],
        }
    )
    b = breadth_snapshot(at, CONFIG)
    assert b.constituent_count == 4
    assert b.pct_up_strong_1m == 50.0
    assert b.pct_new_52w_high == 50.0
    assert b.pct_above_ma_slow == 50.0


def test_empty_universe_is_red_not_a_division_error() -> None:
    b = breadth_snapshot(
        pl.DataFrame({"ret_20": [], "close": [], "high_1y": [], "ma_slow": []}), CONFIG
    )
    assert b.constituent_count == 0
    assert market_gate(b, UP, CONFIG) is MarketGate.RED


def test_gate_green_needs_breadth_and_the_ten_day_above_the_twenty() -> None:
    assert market_gate(snapshot(8.0), UP, CONFIG) is MarketGate.GREEN
    assert market_gate(snapshot(8.0), None, CONFIG) is MarketGate.GREEN


def test_gate_red_on_thin_breadth_or_the_ten_day_below_the_twenty() -> None:
    """§8.3: "long setups only when the 10-day MA of the index is above the 20-day"."""
    assert market_gate(snapshot(1.5), UP, CONFIG) is MarketGate.RED
    assert market_gate(snapshot(8.0), DOWN, CONFIG) is MarketGate.RED
    assert UP.long_bias and not UP.bearish
    assert DOWN.bearish and not DOWN.long_bias


def test_gate_amber_in_between() -> None:
    assert market_gate(snapshot(3.5), UP, CONFIG) is MarketGate.AMBER


def tier(level: int, rs: list[str], gate: MarketGate = MarketGate.GREEN) -> ExposureTier:
    return exposure_tier(
        current_level=level, closed_r_multiples=[D(r) for r in rs], gate=gate, config=CONFIG
    )


def test_red_gate_drops_to_the_bottom_rung_and_forbids_entries() -> None:
    t = tier(3, ["2", "2", "2", "2", "2"], MarketGate.RED)
    assert t.level == 0
    assert t.new_entries_allowed is False
    assert t.max_open_positions == CONFIG.tiers[0][0]


def test_five_winning_trades_in_a_green_tape_climb_one_rung() -> None:
    assert tier(0, ["1", "-1", "3", "-1", "2"]).level == 1
    assert tier(3, ["1", "1", "1", "1", "1"]).level == 3  # already at the top


def test_the_ladder_never_skips_a_rung() -> None:
    assert tier(0, ["5", "5", "5", "5", "5", "5", "5", "5"]).level == 1


def test_amber_holds_the_rung_even_with_good_results() -> None:
    assert tier(1, ["1", "1", "1", "1", "1"], MarketGate.AMBER).level == 1


def test_three_straight_losses_step_down() -> None:
    assert tier(2, ["3", "2", "-1", "-1", "-1"]).level == 1
    assert tier(0, ["-1", "-1", "-1"]).level == 0


def test_fewer_than_five_closed_trades_cannot_climb() -> None:
    assert tier(0, ["2", "2"]).level == 0


def test_tier_carries_its_limits() -> None:
    t = tier(2, [])
    assert (t.max_open_positions, t.max_exposure_pct) == CONFIG.tiers[2]
    assert t.new_entries_allowed is True
    assert t.drawdown_locked is False


def test_top_rung_is_his_typical_count_not_his_maximum() -> None:
    """§8.4: "typically 5-10 positions"; the 15-20 of a great market is the env ceiling."""
    assert CONFIG.tiers[-1][0] == 10
    assert CONFIG.tiers[-1][1] == 100.0


# --- drawdown containment (§8.5) ---------------------------------------------


def test_fifteen_percent_drawdown_locks_the_sleeve_out_of_new_entries() -> None:
    """ "I try to contain them at 15-20%." A locked sleeve manages exits only."""
    t = exposure_tier(
        current_level=3,
        closed_r_multiples=[],
        gate=MarketGate.GREEN,
        config=CONFIG,
        drawdown_pct=15.0,
    )
    assert t.drawdown_locked is True
    assert t.new_entries_allowed is False
    assert t.level == 0


def test_lockout_persists_until_the_drawdown_is_back_inside_ten_percent() -> None:
    assert drawdown_locked(drawdown_pct=12.0, was_locked=True, config=CONFIG) is True
    assert drawdown_locked(drawdown_pct=9.9, was_locked=True, config=CONFIG) is False
    assert drawdown_locked(drawdown_pct=12.0, was_locked=False, config=CONFIG) is False
    assert drawdown_locked(drawdown_pct=15.0, was_locked=False, config=CONFIG) is True


def test_a_recovered_sleeve_resumes_at_the_bottom_rung() -> None:
    t = exposure_tier(
        current_level=3,
        closed_r_multiples=[],
        gate=MarketGate.GREEN,
        config=CONFIG,
        drawdown_pct=8.0,
        was_drawdown_locked=True,
    )
    assert t.drawdown_locked is False
    assert t.new_entries_allowed is True


# --- SW30: the gate re-read on the live index (Maulik, 29 Sep 2026) --------------------------


def test_index_reading_is_the_ten_and_twenty_day_simple_averages() -> None:
    levels = [float(v) for v in range(1, 21)]  # 1 … 20, oldest first
    reading = index_reading(levels, CONFIG)
    assert reading == IndexReading(close=20.0, ma_fast=15.5, ma_slow=10.5)


def test_index_reading_refuses_fewer_levels_than_the_slow_average_names() -> None:
    assert index_reading([100.0] * 19, CONFIG) is None


def test_a_live_level_above_a_flat_tape_crosses_the_ten_day_over_the_twenty() -> None:
    """Nineteen closes at 100 and today at 101: the 10-day is 100.10, the 20-day 100.05 — the
    cross he asked the gate to open on; at 99 it is the cross back down."""
    up = index_reading([100.0] * 19 + [101.0], CONFIG)
    down = index_reading([100.0] * 19 + [99.0], CONFIG)
    assert up is not None and up.long_bias
    assert down is not None and down.bearish
    assert market_gate(snapshot(10.0), up, CONFIG) is MarketGate.GREEN
    assert market_gate(snapshot(10.0), down, CONFIG) is MarketGate.RED


def test_a_live_green_after_a_closing_red_allows_entries_at_the_bottom_rung() -> None:
    close = exposure_tier(
        current_level=2, closed_r_multiples=[], gate=MarketGate.RED, config=CONFIG
    )
    live = intraday_tier(close, MarketGate.GREEN, CONFIG)
    assert live == ExposureTier(0, 2, 25.0, new_entries_allowed=True)


def test_the_ladder_never_climbs_on_a_quote() -> None:
    close = ExposureTier(1, 4, 50.0, new_entries_allowed=True)
    assert intraday_tier(close, MarketGate.GREEN, CONFIG) == close
    assert intraday_tier(close, MarketGate.AMBER, CONFIG) == close


def test_a_live_red_refuses_entries_and_a_cross_back_restores_the_close_rung() -> None:
    close = ExposureTier(2, 6, 75.0, new_entries_allowed=True)
    red = intraday_tier(close, MarketGate.RED, CONFIG)
    assert red == ExposureTier(0, 2, 25.0, new_entries_allowed=False)
    assert intraday_tier(close, MarketGate.GREEN, CONFIG) == close


def test_a_drawdown_lock_holds_whatever_the_index_does() -> None:
    locked = ExposureTier(0, 2, 25.0, new_entries_allowed=False, drawdown_locked=True)
    assert intraday_tier(locked, MarketGate.GREEN, CONFIG) == locked
