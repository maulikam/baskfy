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
    ema,
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


#: NIFTY SMLCAP 250's published closes, 2026-02-19 → 2026-09-29 (150 sessions of
#: `index_snapshot_daily` on the box). SW31: Maulik's chart reads EMA(10, close) 18,086.77 and
#: EMA(20, close) 18,173.17 on 29 Sep; the simple averages the gate read until then were
#: 18,103.59 and 18,234.55.
SMLCAP_250_TO_29_SEP = [
    15988,
    15958.8,
    16027.95,
    15933.05,
    16035.7,
    16047.45,
    15881.05,
    15580.4,
    15250.6,
    15465.75,
    15422.2,
    15063.5,
    15385.9,
    15326.6,
    15266.45,
    14857.5,
    14804.7,
    14889.5,
    15153.55,
    14753.25,
    14791.95,
    14232.85,
    14574.25,
    14937.2,
    14655.35,
    14288.45,
    14749.1,
    14724.45,
    14881.85,
    14900.95,
    15483.5,
    15503.75,
    15753.05,
    15687.95,
    16051.4,
    16199.25,
    16439.25,
    16379.5,
    16513.2,
    16683.45,
    16592.45,
    16414.65,
    16713.55,
    16721.85,
    16804.4,
    16731.2,
    16895.95,
    16928.2,
    17233.85,
    17409,
    17437.5,
    17253.5,
    16735.8,
    16784,
    16789,
    16722.9,
    16473.65,
    16664.6,
    16678.05,
    16796.3,
    16775.3,
    16976.4,
    17009.55,
    17103.45,
    16992.1,
    16857.1,
    16946.6,
    16954.45,
    17052.7,
    17054.5,
    16748.9,
    16977.45,
    16782.95,
    16642.7,
    17079.1,
    17292.7,
    17369.25,
    17495.75,
    17612.6,
    17713.8,
    17849.75,
    17747.65,
    17784.65,
    17704.7,
    17619.6,
    17720.3,
    17801.55,
    17983.35,
    17996.25,
    18078.6,
    17953.1,
    17597.9,
    17875.7,
    18118.9,
    18171.85,
    17974.9,
    18097.85,
    18109.05,
    18003.5,
    18023.9,
    18101,
    17863.75,
    17651.25,
    17601.2,
    17824.5,
    17741.45,
    17978.05,
    17845.6,
    17919.9,
    18166.65,
    18194.3,
    18321.95,
    18358.65,
    18356.55,
    18345.55,
    18381.85,
    18335.65,
    18365.9,
    18278.8,
    18312.2,
    18346.75,
    18232,
    18338.6,
    18414.45,
    18375.45,
    18347.15,
    18455.75,
    18432.75,
    18496.15,
    18372.1,
    18363.1,
    18256.3,
    18444.95,
    18481.4,
    18484.15,
    18527.2,
    18431.95,
    18433.4,
    18339.9,
    17892.8,
    17863.3,
    18009.25,
    18287.4,
    18279.05,
    18260.6,
    18411.4,
    18155.5,
    18158.9,
    17863.2,
    17747.25,
]


def test_the_reading_is_his_charts_ema_ten_and_twenty() -> None:
    reading = index_reading(SMLCAP_250_TO_29_SEP, CONFIG)
    assert reading is not None
    assert round(reading.close, 2) == 17747.25
    assert round(reading.ma_fast, 2) == 18086.77
    assert round(reading.ma_slow, 2) == 18173.17
    assert reading.bearish


def test_ema_is_seeded_with_the_simple_average_and_weights_two_over_n_plus_one() -> None:
    assert ema([1.0, 2.0, 3.0], 3) == 2.0
    assert ema([1.0, 2.0, 3.0, 7.0], 3) == 2.0 + 0.5 * (7.0 - 2.0)
    assert ema([4.0, 6.0], 3) == 5.0, "fewer values than the length: their simple average"


def test_index_reading_refuses_fewer_levels_than_the_slow_average_names() -> None:
    assert index_reading([100.0] * 19, CONFIG) is None


def test_a_live_level_above_a_flat_tape_crosses_the_ten_day_over_the_twenty() -> None:
    """Nineteen closes at 100 and today at 101: EMA(10) is 100 + 2/11, EMA(20) 100 + 2/21 — the
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
