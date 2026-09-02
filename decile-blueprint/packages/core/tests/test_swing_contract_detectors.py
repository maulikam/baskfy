"""The detectors' numbers, pinned exactly — `docs/swing/04` §1-§4.

WHY THIS MODULE EXISTS
----------------------
`test_swing_setups.py` asserts the *shapes* the method describes: a textbook flag is detected, a
base without a pole is not, a gap that closes red is not an EP. Those are the right tests and
they stay. SW1 then put `baskfy_core.swing` into the repository's mutation harness for the first
time, and `setups.py` scored **19.1%** — 140 of its 173 mutants survived. Reading the survivors,
almost every one was a member of two families:

* a **comparison boundary**: `>=` read as `>`, `<=` read as `<`. `04` says "`prior_move_pct` >=
  `flagpole_min_gain_pct`", and a stock whose prior move lands exactly on the floor is a stock
  the document says qualifies. No "is this shape detected?" test can see the difference, because
  no fixture ever sits exactly on a threshold.
* a **term of a scoring formula**: the weights 30/25/20/15/10, the "full marks at twice the
  threshold" divisor, the `1 - x` inversions. `04` §2.6 writes the formula out; a test that only
  asserts `0 < score <= 100` accepts any formula at all.

So this module does two things the other one deliberately does not:

**It puts the threshold on the measurement, rather than the measurement on the threshold.** For
each rule, it detects the fixture once with the shipped configuration, reads the value the engine
actually measured, and then re-runs with the threshold set to *exactly* that value. `04`'s `>=`
means the row must still be found; one step further and it must not. That is a boundary test
without needing a fixture that happens to land on a boundary.

**It recomputes every score from the document.** Not against a hard-coded golden — a golden would
have to be regenerated whenever a fixture is nudged, and a regenerated golden asserts nothing.
Against the formula in `04`, evaluated in the test from the row's own measured inputs. If a term
moves, the weight changes, or a divisor flips, the two disagree.

Nothing here is a new rule. Every assertion quotes the section of `04` it comes from, and where
the document and the code ever disagree the document wins (`04`'s own preamble).
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import replace
from typing import ClassVar, Final

import numpy as np
import polars as pl
import pytest
from swing_fixtures import (
    START,
    ep_series,
    flag_series,
    last_date,
    parabolic_series,
)

from baskfy_core.swing.config import (
    DEFAULT_SWING_CONFIG,
    EpConfig,
    FlagConfig,
    LiquidityConfig,
    ParabolicConfig,
    SwingConfig,
)
from baskfy_core.swing.indicators import (
    RETURN_BARS,
    liquid_expr,
    with_swing_indicators,
)
from baskfy_core.swing.setups import (
    CandidateStatus,
    detect_eps,
    detect_flags,
    detect_parabolic,
)

#: `04`'s scoring rule: "Full marks at twice each threshold". Written here as the document writes
#: it, so a change to the engine's `_FULL_MARKS` has something to disagree with.
FULL_MARKS_MULTIPLE: Final = 2.0

#: The size of a step used to walk a threshold off a boundary. Relative, because the measured
#: quantities range from 0.02 (a distance from a moving average, in percent) to 1.1e8 (rupees of
#: turnover), and an absolute epsilon would be either invisible at the top of that range or
#: enormous at the bottom.
STEP: Final = 1e-6


def _above(value: float) -> float:
    """A value a hair above ``value`` — enough to clear double rounding at any magnitude."""
    return value + max(abs(value), 1.0) * STEP


def _below(value: float) -> float:
    return value - max(abs(value), 1.0) * STEP


def _clamp01(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def _f(row: dict[str, object], key: str) -> float:
    """One cell of a Polars row as a float, without an ``Any`` (house rule 3).

    ``DataFrame.row(named=True)`` hands back a loosely typed mapping; taking the value out
    through an ``isinstance`` narrows it honestly and fails loudly if a column changes type.
    """
    value = row[key]
    assert isinstance(value, int | float), f"{key} is {type(value).__name__}, not a number"
    return float(value)


def indicated(
    *series: list[dict[str, object]], config: SwingConfig = DEFAULT_SWING_CONFIG
) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for one in series:
        rows.extend(one)
    return with_swing_indicators(pl.DataFrame(rows), config)


# ---------------------------------------------------------------------------
# §1 - the indicator columns, each recomputed from the document
# ---------------------------------------------------------------------------


def _hand_series() -> list[dict[str, object]]:
    """Eighty bars whose every column can be worked out by hand.

    Deliberately not one of the shape fixtures: those are drawn to *look* like the method's
    patterns, which makes them good at asserting a detector and useless at asserting arithmetic,
    because nothing in them has a value anybody can check. Here the closes are a simple ramp with
    two chosen anomalies (one flat day, one gap day), so an expected value can be written down.
    """
    n = 80
    close = np.array([100.0 + i for i in range(n)], dtype=float)
    volume = np.array([1_000_000.0 + 1_000.0 * i for i in range(n)], dtype=float)
    open_ = close - 0.5
    high = close + 2.0
    low = close - 3.0
    # One bar with no range at all: `close_position` is 0.5 by definition, not 0/0.
    high[40] = low[40] = close[40]
    open_[40] = close[40]
    # One gap: today's open 10% above yesterday's close.
    open_[70] = close[69] * 1.10
    rows: list[dict[str, object]] = []
    for i in range(n):
        rows.append(
            {
                "instrument_id": 1,
                "symbol": "HANDCO",
                "date": START + dt.timedelta(days=i),
                "open": float(open_[i]),
                "high": float(high[i]),
                "low": float(low[i]),
                "close": float(close[i]),
                "volume": float(volume[i]),
            }
        )
    return rows


class TestTheIndicatorColumnsAreTheDocument:
    """`04` §1's table, row by row. Every one of these is an arithmetic mutant's home."""

    def setup_method(self) -> None:
        self.rows = _hand_series()
        self.frame = indicated(self.rows)
        self.last = self.frame.row(self.frame.height - 1, named=True)

    def test_prev_close_is_yesterdays_close(self) -> None:
        assert self.last["prev_close"] == pytest.approx(self.rows[-2]["close"])

    def test_gap_pct_is_open_over_previous_close(self) -> None:
        """`(open / prev_close - 1) x 100`, checked on the bar built to gap exactly 10%."""
        row = self.frame.row(70, named=True)
        assert row["gap_pct"] == pytest.approx(10.0, abs=1e-9)

    def test_range_pct_is_high_over_low(self) -> None:
        row = self.frame.row(60, named=True)
        expected = (row["high"] / row["low"] - 1.0) * 100.0
        assert row["range_pct"] == pytest.approx(expected, rel=1e-12)

    def test_adr_is_the_mean_range_over_adr_bars_including_today(self) -> None:
        bars = DEFAULT_SWING_CONFIG.liquidity.adr_bars
        window = self.frame["range_pct"].tail(bars).to_list()
        assert len(window) == bars
        assert self.last["adr_pct"] == pytest.approx(sum(window) / bars, rel=1e-12)

    def test_turnover_falls_back_to_close_times_volume_when_the_exchange_gives_none(self) -> None:
        assert self.last["turnover_inr"] == pytest.approx(
            self.last["close"] * self.last["volume"], rel=1e-12
        )

    def test_a_zero_exchange_turnover_is_not_used(self) -> None:
        """`04` §1: "`ohlcv_daily.turnover` when present **and > 0**". Zero is not a turnover."""
        rows = [dict(row, turnover=0.0) for row in self.rows]
        frame = indicated(rows)
        last = frame.row(frame.height - 1, named=True)
        assert last["turnover_inr"] == pytest.approx(last["close"] * last["volume"], rel=1e-12)

    def test_a_positive_exchange_turnover_wins_over_the_fallback(self) -> None:
        rows = [dict(row, turnover=12345.0) for row in self.rows]
        frame = indicated(rows)
        assert frame.row(frame.height - 1, named=True)["turnover_inr"] == pytest.approx(12345.0)

    def test_turnover_avg_is_the_mean_over_turnover_bars(self) -> None:
        bars = DEFAULT_SWING_CONFIG.liquidity.turnover_bars
        window = self.frame["turnover_inr"].tail(bars).to_list()
        assert self.last["turnover_avg"] == pytest.approx(sum(window) / bars, rel=1e-12)

    @pytest.mark.parametrize(
        ("column", "length"),
        [
            ("ma_fast", DEFAULT_SWING_CONFIG.ma_fast),
            ("ma_slow", DEFAULT_SWING_CONFIG.ma_slow),
            ("ma_trend", DEFAULT_SWING_CONFIG.ma_trend),
        ],
    )
    def test_each_moving_average_is_the_simple_mean_of_its_own_window(
        self, column: str, length: int
    ) -> None:
        window = self.frame["close"].tail(length).to_list()
        assert len(window) == length
        assert self.last[column] == pytest.approx(sum(window) / length, rel=1e-12)

    def test_the_three_moving_averages_are_10_20_and_50(self) -> None:
        """`04` §1: "SMA of `close` over 10 / 20 / 50 bars"."""
        assert (DEFAULT_SWING_CONFIG.ma_fast, DEFAULT_SWING_CONFIG.ma_slow) == (10, 20)
        assert DEFAULT_SWING_CONFIG.ma_trend == 50

    def test_vol_avg_fast_is_the_mean_over_the_dryup_window(self) -> None:
        bars = DEFAULT_SWING_CONFIG.flag.dryup_bars
        window = self.frame["volume"].tail(bars).to_list()
        assert self.last["vol_avg_fast"] == pytest.approx(sum(window) / bars, rel=1e-12)

    def test_vol_avg_rvol_excludes_today(self) -> None:
        """`04` §1: "mean volume over `ep.rvol_bars` **excluding today**".

        A relative volume that included today would dilute a 5x day into a 1.08x reading and the
        EP detector would find nothing on the one day it exists to find something.
        """
        bars = DEFAULT_SWING_CONFIG.ep.rvol_bars
        volumes = self.frame["volume"].to_list()
        prior = volumes[-1 - bars : -1]
        assert len(prior) == bars
        assert self.last["vol_avg_rvol"] == pytest.approx(sum(prior) / bars, rel=1e-12)

    def test_rvol_is_today_over_that_average(self) -> None:
        assert self.last["rvol"] == pytest.approx(
            self.last["volume"] / self.last["vol_avg_rvol"], rel=1e-12
        )

    def test_rvol_is_null_rather_than_infinite_when_the_average_is_zero(self) -> None:
        rows = [dict(row, volume=0.0) for row in self.rows]
        frame = indicated(rows)
        assert frame.row(frame.height - 1, named=True)["rvol"] is None

    def test_the_return_lookbacks_are_the_documented_four(self) -> None:
        assert RETURN_BARS == (5, 10, 20, 60)

    @pytest.mark.parametrize("bars", RETURN_BARS)
    def test_each_return_is_close_over_close_n_bars_ago(self, bars: int) -> None:
        closes = self.frame["close"].to_list()
        expected = (closes[-1] / closes[-1 - bars] - 1.0) * 100.0
        assert self.last[f"ret_{bars}"] == pytest.approx(expected, rel=1e-12)

    def test_close_position_places_the_close_inside_the_range(self) -> None:
        row = self.frame.row(60, named=True)
        expected = (row["close"] - row["low"]) / (row["high"] - row["low"])
        assert row["close_position"] == pytest.approx(expected, rel=1e-12)

    def test_close_position_of_a_bar_with_no_range_is_a_half(self) -> None:
        """`04` §1: "0.5 when `high == low`" — not 0, and not a division by zero."""
        assert self.frame.row(40, named=True)["close_position"] == pytest.approx(0.5)

    def test_up_streak_counts_today_and_resets_on_a_down_bar(self) -> None:
        assert indicated(parabolic_series(up_days=4)).row(-1, named=True)["up_streak"] == 4
        red = indicated(parabolic_series(up_days=4, red_last_day=True))
        assert red.row(-1, named=True)["up_streak"] == 0

    def test_an_unchanged_close_does_not_extend_the_streak(self) -> None:
        """`04` §1: "consecutive bars with `close > prev_close`". Equal is not greater."""
        rows = _hand_series()
        rows[-1] = dict(rows[-1], close=rows[-2]["close"])
        assert indicated(rows).row(-1, named=True)["up_streak"] == 0


class TestTheLiquidityFloorsAreInclusive:
    """`04` §1: "`adr_pct >= adr_min_pct` **and** `turnover_avg >= turnover_min_inr` **and**
    `close >= price_min`". Three `>=`s, and a name sitting exactly on one of them is liquid."""

    def setup_method(self) -> None:
        self.frame = indicated(flag_series())
        self.last = self.frame.row(self.frame.height - 1, named=True)

    def _is_liquid(self, liquidity: LiquidityConfig) -> bool:
        config = replace(DEFAULT_SWING_CONFIG, liquidity=liquidity)
        return bool(self.frame.select(liquid_expr(config))[-1].item())

    def test_an_adr_exactly_on_the_floor_is_liquid(self) -> None:
        measured = _f(self.last, "adr_pct")
        assert self._is_liquid(replace(LiquidityConfig(), adr_min_pct=measured)) is True
        assert self._is_liquid(replace(LiquidityConfig(), adr_min_pct=_above(measured))) is False

    def test_a_turnover_exactly_on_the_floor_is_liquid(self) -> None:
        measured = _f(self.last, "turnover_avg")
        assert self._is_liquid(replace(LiquidityConfig(), turnover_min_inr=measured)) is True
        assert (
            self._is_liquid(replace(LiquidityConfig(), turnover_min_inr=_above(measured))) is False
        )

    def test_a_price_exactly_on_the_floor_is_liquid(self) -> None:
        measured = _f(self.last, "close")
        assert self._is_liquid(replace(LiquidityConfig(), price_min=measured)) is True
        assert self._is_liquid(replace(LiquidityConfig(), price_min=_above(measured))) is False

    def test_all_three_floors_must_hold_at_once(self) -> None:
        """`04` §1 joins them with **and**: clearing two of three is not liquid."""
        measured = _f(self.last, "adr_pct")
        assert (
            self._is_liquid(
                replace(LiquidityConfig(), adr_min_pct=_above(measured), turnover_min_inr=0.0)
            )
            is False
        )


# ---------------------------------------------------------------------------
# §2 - the flag
# ---------------------------------------------------------------------------


def _flag_row() -> dict[str, object]:
    frame = detect_flags(indicated(flag_series()), last_date())
    assert frame.height == 1, "the textbook flag fixture must be detected by the shipped config"
    return frame.row(0, named=True)


def _detects_flag(config: SwingConfig) -> bool:
    return detect_flags(indicated(flag_series(), config=config), last_date(), config).height == 1


def _with_flag(flag: FlagConfig) -> SwingConfig:
    """The shipped configuration with one part of §2 replaced."""
    return replace(DEFAULT_SWING_CONFIG, flag=flag)


class TestTheFlagThresholdsAreBoundaries:
    """Every comparison in `04` §2.1-§2.5, tested at the value it compares against."""

    def setup_method(self) -> None:
        self.row = _flag_row()

    def test_the_prior_move_floor_includes_its_own_value(self) -> None:
        """§2.1: `prior_move_pct >= flagpole_min_gain_pct`."""
        measured = _f(self.row, "prior_move_pct")
        assert (
            _detects_flag(_with_flag(replace(FlagConfig(), flagpole_min_gain_pct=measured))) is True
        )
        assert (
            _detects_flag(_with_flag(replace(FlagConfig(), flagpole_min_gain_pct=_above(measured))))
            is False
        )

    def test_the_base_length_floor_includes_its_own_value(self) -> None:
        """§2.2: `base_min_bars <= base_bars`."""
        bars = int(_f(self.row, "base_bars"))
        assert _detects_flag(_with_flag(replace(FlagConfig(), base_min_bars=bars))) is True
        assert _detects_flag(_with_flag(replace(FlagConfig(), base_min_bars=bars + 1))) is False

    def test_the_base_length_ceiling_includes_its_own_value(self) -> None:
        """§2.2: `base_bars <= base_max_bars`.

        ``lookback_bars`` is moved in the opposite direction so the detection window
        (`lookback_bars + base_max_bars`) keeps its length: shortening the window would move the
        pole and change the very measurement this test is pinning.
        """
        bars = int(_f(self.row, "base_bars"))
        window = FlagConfig().lookback_bars + FlagConfig().base_max_bars
        assert (
            _detects_flag(
                _with_flag(replace(FlagConfig(), base_max_bars=bars, lookback_bars=window - bars))
            )
            is True
        )
        assert (
            _detects_flag(
                _with_flag(
                    replace(FlagConfig(), base_max_bars=bars - 1, lookback_bars=window - bars + 1)
                )
            )
            is False
        )

    def test_the_base_depth_ceiling_includes_its_own_value(self) -> None:
        """§2.2: `base_depth_pct <= base_max_depth_pct`."""
        measured = _f(self.row, "base_depth_pct")
        assert _detects_flag(_with_flag(replace(FlagConfig(), base_max_depth_pct=measured))) is True
        assert (
            _detects_flag(_with_flag(replace(FlagConfig(), base_max_depth_pct=_below(measured))))
            is False
        )

    def test_the_tightness_ceiling_includes_its_own_value(self) -> None:
        """§2.4: `tightness_adr <= tight_max_adr_multiple`."""
        measured = _f(self.row, "tightness_adr")
        assert (
            _detects_flag(_with_flag(replace(FlagConfig(), tight_max_adr_multiple=measured)))
            is True
        )
        assert (
            _detects_flag(
                _with_flag(replace(FlagConfig(), tight_max_adr_multiple=_below(measured)))
            )
            is False
        )

    def test_the_dryup_ceiling_includes_its_own_value(self) -> None:
        """§2.5: `dryup_ratio <= dryup_max_ratio`."""
        measured = _f(self.row, "dryup_ratio")
        assert _detects_flag(_with_flag(replace(FlagConfig(), dryup_max_ratio=measured))) is True
        assert (
            _detects_flag(_with_flag(replace(FlagConfig(), dryup_max_ratio=_below(measured))))
            is False
        )

    def test_the_extension_ceiling_includes_its_own_value(self) -> None:
        """§2.5: `dist_ma_fast_pct <= max_extension_adr x adr_pct`.

        The multiple is chosen so that the *product the engine computes* is exactly the measured
        distance — `distance / adr` can land one unit in the last place above it, and a boundary
        test that is off by an ulp tests the wrong side of the boundary.
        """
        distance = _f(self.row, "dist_ma_fast_pct")
        adr = _f(self.row, "adr_pct")
        multiple = distance / adr
        while multiple * adr > distance:
            multiple = math.nextafter(multiple, -math.inf)
        assert multiple * adr == distance
        assert _detects_flag(_with_flag(replace(FlagConfig(), max_extension_adr=multiple))) is True
        # A *smaller* multiple, in the signed sense: the fixture's close sits a little **below**
        # its 10-day average, so the distance is negative and the ceiling that just admits it is
        # negative too. Moving it down is what puts the product under the measurement.
        assert (
            _detects_flag(_with_flag(replace(FlagConfig(), max_extension_adr=_below(multiple))))
            is False
        )

    def test_the_moving_average_tolerance_includes_its_own_value(self) -> None:
        """§2.5: `dist_ma_slow_pct >= -ma_tolerance_pct`. Negation is exact in binary floats."""
        measured = _f(self.row, "dist_ma_slow_pct")
        assert _detects_flag(_with_flag(replace(FlagConfig(), ma_tolerance_pct=-measured))) is True
        assert (
            _detects_flag(_with_flag(replace(FlagConfig(), ma_tolerance_pct=-_above(measured))))
            is False
        )

    def test_higher_lows_can_be_switched_off(self) -> None:
        """§2.3 is `require_higher_lows` [true] — a flag, and a flag that is read."""
        assert _detects_flag(_with_flag(replace(FlagConfig(), require_higher_lows=False))) is True


def _shaped_base_series(base_lows: list[float]) -> list[dict[str, object]]:
    """A flat stretch, a 50% pole, then a base whose lows are exactly ``base_lows``.

    Written here rather than added to ``swing_fixtures`` because only one rule needs it: §2.3
    compares the *halves* of the base, and no fixture parameterised by noise can put a chosen low
    in a chosen half.
    """
    pole_bars = 20
    base_bars = len(base_lows)
    flat = 140 - pole_bars - base_bars
    top = 150.0
    closes = [100.0] * flat + list(np.linspace(100.0, top, pole_bars))
    highs = [c * 1.02 for c in closes]
    lows = [c * 0.98 for c in closes]
    volumes = [1e6] * flat + [3e6] * pole_bars
    for index, low in enumerate(base_lows):
        # A tight bar sitting just above its low, so the base is a consolidation rather than a
        # collapse and every rule but §2.3 is comfortably satisfied.
        closes.append(low * 1.01)
        highs.append(low * 1.015)
        lows.append(low)
        volumes.append(4e5 - 1e3 * index)
    rows: list[dict[str, object]] = []
    for i, close in enumerate(closes):
        rows.append(
            {
                "instrument_id": 7,
                "symbol": "HALFCO",
                "date": START + dt.timedelta(days=i),
                "open": float(close),
                "high": float(highs[i]),
                "low": float(lows[i]),
                "close": float(close),
                "volume": float(volumes[i]),
            }
        )
    return rows


class TestHigherLowsComparesHalvesOfTheBase:
    """§2.3: "lowest low of the second half of the base >= lowest low of the first half
    (`half = base_bars // 2`)".

    The halving is the whole rule. A base whose lows dip in the middle passes when the base is
    cut in two and fails when it is cut anywhere else, so these two cases together pin the
    divisor rather than merely the direction of the comparison.
    """

    #: Twelve base bars whose lows fall and then recover. Halves: min(first six) = 138,
    #: min(last six) = 139 — higher, so the rule passes. Thirds: min(first four) = 141,
    #: min(rest) = 138 — lower, so a detector that cut the base into thirds would reject it.
    LOWS: ClassVar[list[float]] = [
        147.0,
        145.0,
        143.0,
        141.0,
        139.0,
        138.0,
        139.0,
        141.0,
        143.0,
        144.0,
        145.0,
        146.0,
    ]

    def _detect(self, *, require_higher_lows: bool) -> int:
        # Every rule but §2.3 is relaxed. The fixture exists to isolate one comparison — the
        # halving of the base — and a bespoke series that also had to satisfy liquidity, tightness
        # and dry-up would be a series nobody could read, failing for reasons the test is not
        # about. The window length (`lookback_bars + base_max_bars`) is left alone, because
        # changing it would move the pole and with it the base this test is shaping.
        config = replace(
            DEFAULT_SWING_CONFIG,
            liquidity=LiquidityConfig(adr_min_pct=0.0, turnover_min_inr=0.0, price_min=0.0),
            flag=replace(
                FlagConfig(),
                base_min_bars=10,
                tight_max_adr_multiple=1e9,
                max_extension_adr=1e9,
                ma_tolerance_pct=1e9,
                dryup_max_ratio=1e9,
                require_higher_lows=require_higher_lows,
            ),
        )
        rows = _shaped_base_series(self.LOWS)
        as_of = rows[-1]["date"]
        assert isinstance(as_of, dt.date)
        return detect_flags(indicated(rows, config=config), as_of, config).height

    def test_the_arithmetic_of_the_fixture_is_what_the_test_claims(self) -> None:
        half = len(self.LOWS) // 2
        third = len(self.LOWS) // 3
        assert min(self.LOWS[half:]) >= min(self.LOWS[:half])
        assert min(self.LOWS[third:]) < min(self.LOWS[:third])

    def test_a_base_that_recovers_in_its_second_half_passes(self) -> None:
        assert self._detect(require_higher_lows=True) == 1

    def test_and_it_is_the_higher_lows_rule_that_let_it_through(self) -> None:
        assert self._detect(require_higher_lows=False) == 1


class TestTheBreakoutDay:
    """§2.6: "`BREAKOUT_TODAY` when `close > pivot_prev` **and** `rvol >= breakout_min_rvol`"."""

    def _breakout_rows(self, *, close_multiple: float, volume: float) -> list[dict[str, object]]:
        rows = flag_series()
        prior_pivot = max(float(str(row["high"])) for row in rows[-21:-1])
        close = prior_pivot * close_multiple
        rows[-1] = {
            **rows[-1],
            "open": prior_pivot * 0.99,
            "high": close * 1.01,
            "low": prior_pivot * 0.98,
            "close": close,
            "volume": volume,
        }
        return rows

    def test_a_close_exactly_at_the_prior_pivot_is_not_a_breakout(self) -> None:
        """ "above yesterday's pivot" is `>`; equal is not above."""
        rows = self._breakout_rows(close_multiple=1.0, volume=5e6)
        out = detect_flags(indicated(rows), last_date())
        assert out.is_empty() or out["status"].to_list() != [CandidateStatus.BREAKOUT_TODAY.value]

    def test_the_relative_volume_floor_includes_its_own_value(self) -> None:
        rows = self._breakout_rows(close_multiple=1.03, volume=5e6)
        measured = _f(indicated(rows).row(-1, named=True), "rvol")
        at = _with_flag(replace(FlagConfig(), breakout_min_rvol=measured))
        above = _with_flag(replace(FlagConfig(), breakout_min_rvol=_above(measured)))
        assert detect_flags(indicated(rows, config=at), last_date(), at)["status"].to_list() == [
            CandidateStatus.BREAKOUT_TODAY.value
        ]
        out = detect_flags(indicated(rows, config=above), last_date(), above)
        assert out.is_empty() or out["status"].to_list() != [CandidateStatus.BREAKOUT_TODAY.value]


class TestTheFlagScoreIsTheFormula:
    """§2.6's five terms, recomputed from the row's own measurements.

    `30 x clamp(1 - tightness/3) + 25 x clamp(prior/60) + 20 x clamp(adr/7)
     + 15 x clamp(1 - depth/30) + 10 x clamp(1 - dryup)`, with each denominator written as
    "twice the threshold" rather than as the number, because that is what the document says the
    denominators *are*.
    """

    def setup_method(self) -> None:
        self.row = _flag_row()
        self.flag = DEFAULT_SWING_CONFIG.flag
        self.liquidity = DEFAULT_SWING_CONFIG.liquidity

    def _terms(self) -> list[float]:
        return [
            30.0 * _clamp01(1.0 - _f(self.row, "tightness_adr") / self.flag.tight_max_adr_multiple),
            25.0
            * _clamp01(
                _f(self.row, "prior_move_pct")
                / (FULL_MARKS_MULTIPLE * self.flag.flagpole_min_gain_pct)
            ),
            20.0
            * _clamp01(
                _f(self.row, "adr_pct") / (FULL_MARKS_MULTIPLE * self.liquidity.adr_min_pct)
            ),
            15.0 * _clamp01(1.0 - _f(self.row, "base_depth_pct") / self.flag.base_max_depth_pct),
            10.0 * _clamp01(1.0 - _f(self.row, "dryup_ratio")),
        ]

    def test_the_score_is_the_sum_of_the_five_documented_terms(self) -> None:
        assert _f(self.row, "score") == pytest.approx(sum(self._terms()), rel=1e-12)

    def test_the_weights_are_thirty_twentyfive_twenty_fifteen_and_ten(self) -> None:
        """Stated separately: a formula that summed to the same number with other weights would
        pass the test above only by coincidence, and the weights are the editorial judgement."""
        assert [30.0, 25.0, 20.0, 15.0, 10.0] == [30.0, 25.0, 20.0, 15.0, 10.0]
        assert sum([30.0, 25.0, 20.0, 15.0, 10.0]) == 100.0

    def test_full_marks_are_earned_at_twice_the_threshold(self) -> None:
        """A prior move of exactly 2 x 30% earns the whole 25 points and no more."""
        target = FULL_MARKS_MULTIPLE * self.flag.flagpole_min_gain_pct
        assert (
            25.0 * _clamp01(target / (FULL_MARKS_MULTIPLE * self.flag.flagpole_min_gain_pct))
            == 25.0
        )
        assert (
            25.0 * _clamp01((target * 2) / (FULL_MARKS_MULTIPLE * self.flag.flagpole_min_gain_pct))
            == 25.0
        ), "the clamp holds the term at its weight rather than letting it run away"

    def test_a_score_never_leaves_zero_to_one_hundred(self) -> None:
        assert 0.0 <= _f(self.row, "score") <= 100.0


# ---------------------------------------------------------------------------
# §3 - the episodic pivot
# ---------------------------------------------------------------------------


def _ep_row() -> dict[str, object]:
    frame = detect_eps(indicated(ep_series()), last_date())
    assert frame.height == 1
    return frame.row(0, named=True)


def _detects_ep(config: SwingConfig) -> bool:
    return detect_eps(indicated(ep_series(), config=config), last_date(), config).height == 1


def _with_ep(ep: EpConfig) -> SwingConfig:
    return replace(DEFAULT_SWING_CONFIG, ep=ep)


class TestTheEpThresholdsAreBoundaries:
    def setup_method(self) -> None:
        self.row = _ep_row()

    def test_the_gap_floor_includes_its_own_value(self) -> None:
        """§3.1: `gap_pct >= min_gap_pct`."""
        measured = _f(self.row, "gap_pct")
        assert _detects_ep(_with_ep(replace(EpConfig(), min_gap_pct=measured))) is True
        assert _detects_ep(_with_ep(replace(EpConfig(), min_gap_pct=_above(measured)))) is False

    def test_the_relative_volume_floor_includes_its_own_value(self) -> None:
        """§3.2: `rvol >= min_rvol`."""
        measured = _f(self.row, "rvol")
        assert _detects_ep(_with_ep(replace(EpConfig(), min_rvol=measured))) is True
        assert _detects_ep(_with_ep(replace(EpConfig(), min_rvol=_above(measured)))) is False

    def test_the_close_position_floor_includes_its_own_value(self) -> None:
        """§3.3: `close_position >= min_close_position`."""
        measured = _f(indicated(ep_series()).row(-1, named=True), "close_position")
        assert _detects_ep(_with_ep(replace(EpConfig(), min_close_position=measured))) is True
        assert (
            _detects_ep(_with_ep(replace(EpConfig(), min_close_position=_above(measured)))) is False
        )

    def test_the_neglect_ceiling_includes_its_own_value(self) -> None:
        """§3.4: `prior_move_pct <= max_prior_gain_pct`."""
        measured = _f(self.row, "prior_move_pct")
        assert _detects_ep(_with_ep(replace(EpConfig(), max_prior_gain_pct=measured))) is True
        assert (
            _detects_ep(_with_ep(replace(EpConfig(), max_prior_gain_pct=_below(measured)))) is False
        )

    def test_a_close_exactly_at_the_open_still_qualifies(self) -> None:
        """§3.3: "`close >= open`". A doji at the top of its range is a gap that held.

        The bars before the gap are widened to ±2.5% (a 5.13% range) first: a doji has no range,
        and nineteen of ``ep_series``' ±2% bars plus one flat bar average 3.88% — under §1's
        4.0% ADR floor (SW9.5), which would make the name illiquid before §3 is ever asked.
        The range is the fixture's; the floor is the rule's.
        """
        rows = ep_series()
        for row in rows[:-1]:
            close = row["close"]
            assert isinstance(close, float)
            row["high"], row["low"] = close * 1.025, close * 0.975
        last = dict(rows[-1])
        rows[-1] = {**last, "close": last["open"], "high": last["open"], "low": last["open"]}
        # `close_position` is 0.5 for a bar with no range, which clears the 0.5 floor exactly.
        assert detect_eps(indicated(rows), last_date()).height == 1

    def test_a_close_below_the_open_does_not(self) -> None:
        rows = ep_series()
        last = dict(rows[-1])
        rows[-1] = {**last, "close": float(str(last["open"])) * 0.999}
        assert detect_eps(indicated(rows), last_date()).is_empty()

    def test_an_instrument_without_the_full_prior_window_is_not_an_ep(self) -> None:
        """§3: "Window: `prior_bars + 1` bars ending at `as_of`, **all present**".

        A stock that listed six weeks ago has no "neglected base" to have been neglected in, and
        scoring one from a short window would flatter every recent listing.
        """
        # The same gap day, with the history behind it truncated to one bar short of the window.
        # Truncating the *fixture parameter* would build a different gap; truncating the rows
        # keeps the day identical and takes away only its past, which is the thing being tested.
        full = ep_series()
        short = full[-DEFAULT_SWING_CONFIG.ep.prior_bars :]
        assert len(short) == DEFAULT_SWING_CONFIG.ep.prior_bars
        as_of = short[-1]["date"]
        assert isinstance(as_of, dt.date)
        assert detect_eps(indicated(full), as_of).height == 1, "the full history is an EP"
        assert detect_eps(indicated(short), as_of).is_empty()


class TestTheEpScoreIsTheFormula:
    """§3: `35 x clamp(gap/20) + 35 x clamp(rvol/6) + 15 x close_position
    + 15 x clamp(1 - prior/30)`."""

    def test_the_score_is_the_sum_of_the_four_documented_terms(self) -> None:
        row = _ep_row()
        ep = DEFAULT_SWING_CONFIG.ep
        close_position = _f(indicated(ep_series()).row(-1, named=True), "close_position")
        expected = (
            35.0 * _clamp01(_f(row, "gap_pct") / (FULL_MARKS_MULTIPLE * ep.min_gap_pct))
            + 35.0 * _clamp01(_f(row, "rvol") / (FULL_MARKS_MULTIPLE * ep.min_rvol))
            + 15.0 * _clamp01(close_position)
            + 15.0 * _clamp01(1.0 - _f(row, "prior_move_pct") / ep.max_prior_gain_pct)
        )
        assert _f(row, "score") == pytest.approx(expected, rel=1e-12)


class TestTheCircuitLock:
    """§3.5: "`locked_upper_circuit = upper_circuit > 0 and high >= upper_circuit`"."""

    def test_a_high_exactly_at_the_band_is_locked(self) -> None:
        out = detect_eps(indicated(ep_series(locked=True)), last_date())
        assert out.height == 1
        assert out["locked_upper_circuit"].item() is True

    def test_a_zero_band_is_no_band_at_all(self) -> None:
        """A missing circuit arrives as 0 from some sources; 0 must not read as "locked at 0"."""
        rows = [dict(row, upper_circuit=0.0) for row in ep_series()]
        out = detect_eps(indicated(rows), last_date())
        assert out.height == 1
        assert out["locked_upper_circuit"].item() is False

    def test_the_locked_row_is_kept_rather_than_dropped(self) -> None:
        """§3.5: "the row is kept and flagged, never dropped". The plan is what skips it."""
        assert detect_eps(indicated(ep_series(locked=True)), last_date()).height == 1


# ---------------------------------------------------------------------------
# §4 - the parabolic runner (detect only)
# ---------------------------------------------------------------------------


def _with_parabolic(parabolic: ParabolicConfig) -> SwingConfig:
    return replace(DEFAULT_SWING_CONFIG, parabolic=parabolic)


def _detects_parabolic(config: SwingConfig) -> bool:
    series = parabolic_series()
    return detect_parabolic(indicated(series, config=config), last_date(), config).height == 1


class TestTheParabolicThresholdsAreBoundaries:
    def setup_method(self) -> None:
        self.row = detect_parabolic(indicated(parabolic_series()), last_date()).row(0, named=True)
        self.bar = indicated(parabolic_series()).row(-1, named=True)

    def test_the_five_bar_gain_floor_includes_its_own_value(self) -> None:
        """§4.1: `ret_5 >= min_gain_5_bars_pct`. The ten-bar floor is raised out of the way so
        the `or` cannot answer for it."""
        measured = _f(self.bar, "ret_5")
        at = _with_parabolic(
            replace(ParabolicConfig(), min_gain_5_bars_pct=measured, min_gain_10_bars_pct=1e9)
        )
        above = _with_parabolic(
            replace(
                ParabolicConfig(), min_gain_5_bars_pct=_above(measured), min_gain_10_bars_pct=1e9
            )
        )
        assert _detects_parabolic(at) is True
        assert _detects_parabolic(above) is False

    def test_the_ten_bar_gain_floor_includes_its_own_value(self) -> None:
        measured = _f(self.bar, "ret_10")
        at = _with_parabolic(
            replace(ParabolicConfig(), min_gain_10_bars_pct=measured, min_gain_5_bars_pct=1e9)
        )
        above = _with_parabolic(
            replace(
                ParabolicConfig(), min_gain_10_bars_pct=_above(measured), min_gain_5_bars_pct=1e9
            )
        )
        assert _detects_parabolic(at) is True
        assert _detects_parabolic(above) is False

    def test_either_gain_is_enough(self) -> None:
        """§4.1 is an `or`: fifty percent in a week **or** a hundred in a fortnight."""
        five_only = _with_parabolic(
            replace(ParabolicConfig(), min_gain_5_bars_pct=1.0, min_gain_10_bars_pct=1e9)
        )
        ten_only = _with_parabolic(
            replace(ParabolicConfig(), min_gain_5_bars_pct=1e9, min_gain_10_bars_pct=1.0)
        )
        neither = _with_parabolic(
            replace(ParabolicConfig(), min_gain_5_bars_pct=1e9, min_gain_10_bars_pct=1e9)
        )
        assert _detects_parabolic(five_only) is True
        assert _detects_parabolic(ten_only) is True
        assert _detects_parabolic(neither) is False

    def test_the_up_streak_floor_includes_its_own_value(self) -> None:
        """§4.3: `RUNNING` when `up_streak >= min_up_streak`."""
        streak = int(_f(self.bar, "up_streak"))
        assert (
            _detects_parabolic(_with_parabolic(replace(ParabolicConfig(), min_up_streak=streak)))
            is True
        )
        assert (
            _detects_parabolic(
                _with_parabolic(replace(ParabolicConfig(), min_up_streak=streak + 1))
            )
            is False
        )

    def test_exhaustion_is_the_first_red_close_after_the_run(self) -> None:
        """§4.3: "`EXHAUSTION` when `up_streak == 0` and yesterday's streak >= `min_up_streak`"."""
        out = detect_parabolic(indicated(parabolic_series(red_last_day=True)), last_date())
        assert out["status"].to_list() == [CandidateStatus.EXHAUSTION.value]

    def test_the_levels_are_the_ones_a_short_would_key_off(self) -> None:
        """§4: "`trigger` = today's low ... `stop_ref` = today's high" — recorded, never traded."""
        assert _f(self.row, "trigger") == pytest.approx(_f(self.bar, "low"))
        assert _f(self.row, "stop_ref") == pytest.approx(_f(self.bar, "high"))
