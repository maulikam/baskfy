"""The edges the first two contract modules left: measurements, defaults and guards.

The three modules together are SW1's answer to a 41% mutation score. The first two pin the
*thresholds* and the *formulas that combine measurements*. This one pins the remaining third:

**The measurements themselves.** A test that recomputes the flag's score from the row's own
`prior_move_pct` cannot notice that `prior_move_pct` was computed wrongly - the error is in both
sides of the comparison. So the detector's outputs are recomputed here from the **raw fixture
bars**, in plain Python, by an implementation that shares nothing with the Polars expressions
under test. `flag_series` builds a known shape (a flat stretch, a twenty-bar pole, a base of a
stated length), so where the pole is and what the base low must be are facts about the fixture
rather than facts read back out of the thing being tested.

**The defaults and the guards.** `fill_null(1.0)` on a missing adjustment factor, `> 0` on an
exchange turnover, `fill_null(False)` on the first bar's up-streak, `frozen=True` on every
dataclass the engine hands out. Each is a single token that changes an answer, and none of them
is reachable from a test that only asks "was this flag detected?".

**Immutability.** `frozen=True` is not decoration on this package. `DEFAULT_SWING_CONFIG` is a
module-level singleton used as a default argument by every detector; a mutable one would let a
caller experimenting with a threshold silently recalibrate the nightly job in the same worker.
The same argument as `test_factor_edge_guards.TestTheConfigObjectsAreImmutable`, which is where
the precedent comes from.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from dataclasses import replace
from decimal import Decimal
from typing import Final

import polars as pl
import pytest
from swing_fixtures import ep_series, flag_series, last_date

from baskfy_core.swing import config as config_module
from baskfy_core.swing import journal as journal_module
from baskfy_core.swing import market as market_module
from baskfy_core.swing import opening_range as opening_range_module
from baskfy_core.swing import plan as plan_module
from baskfy_core.swing import sizing as sizing_module
from baskfy_core.swing import stops as stops_module
from baskfy_core.swing.config import DEFAULT_SWING_CONFIG, FlagConfig, MarketConfig, Setup
from baskfy_core.swing.indicators import with_swing_indicators
from baskfy_core.swing.journal import ClosedTrade, exit_average, summarize
from baskfy_core.swing.market import (
    BreadthSnapshot,
    ExposureTier,
    IndexReading,
    MarketGate,
    breadth_snapshot,
    exposure_tier,
    market_gate,
)
from baskfy_core.swing.opening_range import (
    OpeningRange,
    TriggerState,
    evaluate_trigger,
    live_gap,
)
from baskfy_core.swing.plan import (
    LineKind,
    SwingAccount,
    WatchItem,
    build_entries,
    exit_lines,
)
from baskfy_core.swing.setups import CandidateStatus, detect_eps, detect_flags
from baskfy_core.swing.sizing import SizeRefusal, size_position
from baskfy_core.swing.stops import (
    Action,
    ActionKind,
    ActionReason,
    DailyBar,
    OpenPosition,
    TrailMa,
    apply,
    manage,
    partial_quantity,
)

STOPS: Final = DEFAULT_SWING_CONFIG.stops
MARKET: Final = DEFAULT_SWING_CONFIG.market
RANGE: Final = DEFAULT_SWING_CONFIG.opening_range
FLAG: Final = DEFAULT_SWING_CONFIG.flag
DAY: Final = dt.date(2026, 9, 1)


def _f(row: dict[str, object], key: str) -> float:
    value = row[key]
    assert isinstance(value, int | float), f"{key} is {type(value).__name__}, not a number"
    return float(value)


def _assign(target: object, name: str, value: object) -> None:
    """``target.name = value``, spelled so mypy does not have to be silenced.

    A frozen dataclass raises `FrozenInstanceError` here exactly as it would on a plain
    assignment; writing it as an assignment would need a `type: ignore[misc]`, which house rule 3
    forbids — including in tests.
    """
    setattr(target, name, value)


def _column(rows: list[dict[str, object]], key: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        value = row[key]
        assert isinstance(value, int | float)
        values.append(float(value))
    return values


# ---------------------------------------------------------------------------
# Every frozen dataclass is actually frozen
# ---------------------------------------------------------------------------

MODULES: Final = (
    config_module,
    journal_module,
    market_module,
    opening_range_module,
    plan_module,
    sizing_module,
    stops_module,
)


def _dataclasses() -> list[type]:
    found: list[type] = []
    for module in MODULES:
        for name in dir(module):
            candidate = getattr(module, name)
            if (
                isinstance(candidate, type)
                and dataclasses.is_dataclass(candidate)
                and candidate.__module__ == module.__name__
            ):
                found.append(candidate)
    return found


class TestNothingTheEngineHandsOutCanBeMutated:
    """`frozen=True`, asserted rather than assumed.

    Two of these matter more than the rest and are worth naming. `SwingConfig` is a
    module-level singleton (`DEFAULT_SWING_CONFIG`) that every detector takes as a default
    argument: a mutable one would let a caller trying a different threshold change what the
    nightly job computes, in the same process, with no diff anywhere. `OpenPosition` carries the
    stop; a mutable stop is a stop that can be widened by accident, which is the one thing
    `04` §6.5 forbids outright.
    """

    def test_there_are_dataclasses_to_check(self) -> None:
        assert len(_dataclasses()) >= 15

    @pytest.mark.parametrize("kind", _dataclasses(), ids=lambda k: k.__name__)
    def test_the_dataclass_is_frozen(self, kind: type) -> None:
        # `__dataclass_params__` is not in the type stubs, so it is reached by name. House rule 3
        # forbids the `type: ignore` the attribute access would otherwise need, and `getattr`
        # says the same thing without one.
        params = getattr(kind, "__dataclass_params__", None)
        assert params is not None, f"{kind.__name__} is not a dataclass"
        assert params.frozen is True, f"{kind.__name__} is mutable"

    def test_the_default_config_refuses_assignment_at_runtime(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            _assign(DEFAULT_SWING_CONFIG, "ma_fast", 5)

    def test_a_position_refuses_to_have_its_stop_moved(self) -> None:
        position = OpenPosition(
            symbol="TESTCO",
            entry_date=DAY,
            entry=Decimal("100"),
            initial_stop=Decimal("96"),
            stop=Decimal("96"),
            quantity=100,
            partial_done=False,
            trail=TrailMa.MA20,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            _assign(position, "stop", Decimal("90"))


# ---------------------------------------------------------------------------
# The indicator defaults and guards
# ---------------------------------------------------------------------------


#: Long enough for `vol_avg_rvol` (50 prior bars) to exist, which is the longest window any
#: guard below depends on.
GUARD_BARS: Final = 60


def _bars(**overrides: object) -> pl.DataFrame:
    """Sixty bars whose only moving part is the close, so a guard can be asked about alone."""
    rows: list[dict[str, object]] = []
    for i in range(GUARD_BARS):
        row: dict[str, object] = {
            "instrument_id": 1,
            "symbol": "GUARDCO",
            "date": dt.date(2026, 1, 1) + dt.timedelta(days=i),
            "open": 100.0,
            "high": 102.0,
            "low": 98.0,
            "close": 100.0 + i,
            "volume": 1_000_000.0,
        }
        row.update(overrides)
        rows.append(row)
    return pl.DataFrame(rows)


class TestTheIndicatorDefaultsAndGuards:
    def test_a_missing_adjustment_factor_defaults_to_one(self) -> None:
        """`indicators`: "`adj_factor` rides along so a trigger level can be turned back into an
        exchange price (`raw = adjusted / adj_factor`)". A default of anything but 1.0 would
        silently rescale every level on a name with no corporate action."""
        frame = with_swing_indicators(_bars())
        assert frame["adj_factor"].to_list() == [1.0] * GUARD_BARS

    def test_a_supplied_adjustment_factor_is_kept(self) -> None:
        frame = with_swing_indicators(_bars(adj_factor=0.5))
        assert frame["adj_factor"].to_list() == [0.5] * GUARD_BARS

    def test_a_missing_symbol_column_is_text_rather_than_a_number(self) -> None:
        """The column is filled in when absent so the output schema is stable; filling a text
        column with a float type makes `_conform`'s cast fail on the day a caller omits it."""
        frame = with_swing_indicators(_bars())
        assert frame.schema["symbol"] == pl.String

    def test_a_turnover_of_exactly_one_rupee_is_still_a_turnover(self) -> None:
        """`04` §1: "`turnover` when present **and > 0**". One rupee is present and positive;
        the fallback is for a missing or zero print, not for a small one."""
        frame = with_swing_indicators(_bars(turnover=1.0))
        assert frame["turnover_inr"].to_list() == [1.0] * GUARD_BARS

    def test_a_relative_volume_is_computed_against_an_average_of_exactly_one(self) -> None:
        """`rvol` is null only when the average is **zero**, not when it is merely small."""
        frame = with_swing_indicators(_bars(volume=1.0))
        assert frame["rvol"].tail(1).item() == pytest.approx(1.0)

    def test_the_first_bar_of_a_series_has_no_up_streak(self) -> None:
        """There is no previous close, so the bar is not an up bar. Treating the unknown as an
        up day would start every newly listed stock with a streak it never had."""
        frame = with_swing_indicators(_bars())
        assert frame["up_streak"].head(1).item() == 0


# ---------------------------------------------------------------------------
# The flag's measurements, recomputed from the fixture's own bars
# ---------------------------------------------------------------------------


class TestTheFlagMeasurementsAreRecomputable:
    """`04` §2.1-§2.5's quantities, worked out from the raw bars by a second implementation.

    `flag_series()` is 140 bars: 85 flat at 100, a 20-bar pole to 150, then a 35-bar base. The
    detection window is `lookback_bars + base_max_bars` = 125 bars, so it starts at index 15.
    Everything below follows from those numbers and from `04`, not from the engine.
    """

    WINDOW: Final = FLAG.lookback_bars + FLAG.base_max_bars

    def setup_method(self) -> None:
        self.rows = flag_series()
        self.window = self.rows[-self.WINDOW :]
        self.highs = _column(self.window, "high")
        self.lows = _column(self.window, "low")
        self.closes = _column(self.window, "close")
        # §2.1: "index of the highest `high` among the bars **before** today".
        self.pole_idx = max(range(len(self.highs) - 1), key=lambda i: self.highs[i])
        self.row = detect_flags(with_swing_indicators(pl.DataFrame(self.rows)), last_date()).row(
            0, named=True
        )

    def test_the_window_is_the_documented_length(self) -> None:
        assert self.WINDOW == 125
        assert len(self.window) == 125

    def test_base_bars_counts_the_bars_after_the_pole_including_today(self) -> None:
        """§2.2: "`base_bars` = (bars in window - 1) - `pole_idx`"."""
        expected = (len(self.window) - 1) - self.pole_idx
        assert int(_f(self.row, "base_bars")) == expected

    def test_prior_move_is_the_pole_high_over_the_low_before_it(self) -> None:
        """§2.1: `pole_high` is the highest high before today; `pole_low` the lowest low in
        `[pole_idx - lookback_bars, pole_idx)`."""
        pole_high = max(self.highs[: len(self.highs) - 1])
        start = max(self.pole_idx - FLAG.lookback_bars, 0)
        pole_low = min(self.lows[start : self.pole_idx])
        expected = (pole_high / pole_low - 1.0) * 100.0
        assert _f(self.row, "prior_move_pct") == pytest.approx(expected, rel=1e-12)

    def test_base_depth_is_the_retracement_from_the_pole_high(self) -> None:
        """§2.2: "`base_depth_pct` = (1 - `base_low` / `pole_high`) x 100"."""
        pole_high = max(self.highs[: len(self.highs) - 1])
        base_low = min(self.lows[self.pole_idx + 1 :])
        expected = (1.0 - base_low / pole_high) * 100.0
        assert _f(self.row, "base_depth_pct") == pytest.approx(expected, rel=1e-12)

    def test_tightness_is_the_recent_range_as_a_multiple_of_the_average_day(self) -> None:
        """§2.4: "((max high - min low over the last `tight_bars`) / min low x 100) / `adr_pct`"."""
        tight_high = max(self.highs[-FLAG.tight_bars :])
        tight_low = min(self.lows[-FLAG.tight_bars :])
        expected = ((tight_high / tight_low - 1.0) * 100.0) / _f(self.row, "adr_pct")
        assert _f(self.row, "tightness_adr") == pytest.approx(expected, rel=1e-12)

    def test_the_distance_from_each_moving_average_is_a_percentage_of_it(self) -> None:
        """§2.5: `(close / ma - 1) x 100` - a *percentage above*, not a difference in rupees."""
        fast = sum(self.closes[-DEFAULT_SWING_CONFIG.ma_fast :]) / DEFAULT_SWING_CONFIG.ma_fast
        slow = sum(self.closes[-DEFAULT_SWING_CONFIG.ma_slow :]) / DEFAULT_SWING_CONFIG.ma_slow
        assert _f(self.row, "dist_ma_fast_pct") == pytest.approx(
            (self.closes[-1] / fast - 1.0) * 100.0, rel=1e-9
        )
        assert _f(self.row, "dist_ma_slow_pct") == pytest.approx(
            (self.closes[-1] / slow - 1.0) * 100.0, rel=1e-9
        )

    def test_the_pivot_is_the_highest_high_of_the_last_pivot_bars_including_today(self) -> None:
        """§2.6: "`pivot_high` = highest high of the last `pivot_bars` [20] bars, today
        included" - and for a `SETTING_UP` flag the trigger is that pivot."""
        expected = max(self.highs[-FLAG.pivot_bars :])
        assert _f(self.row, "pivot_high") == pytest.approx(expected, rel=1e-12)
        assert _f(self.row, "trigger") == pytest.approx(expected, rel=1e-12)

    def test_the_stop_reference_is_todays_low(self) -> None:
        assert _f(self.row, "stop_ref") == pytest.approx(self.lows[-1], rel=1e-12)

    def test_the_dryup_ratio_compares_the_recent_volume_to_the_whole_base(self) -> None:
        """§2.5: "`dryup_ratio` = `vol_avg_fast` / mean volume over the base"."""
        volumes = _column(self.window, "volume")
        recent = volumes[-FLAG.dryup_bars :]
        base = volumes[self.pole_idx + 1 :]
        expected = (sum(recent) / len(recent)) / (sum(base) / len(base))
        assert _f(self.row, "dryup_ratio") == pytest.approx(expected, rel=1e-12)


class TestTheEpMeasurementsAreRecomputable:
    """§3.4's neglect measurement, and the two moving-average distances."""

    def setup_method(self) -> None:
        self.rows = ep_series()
        self.window = self.rows[-(DEFAULT_SWING_CONFIG.ep.prior_bars + 1) :]
        self.closes = _column(self.window, "close")
        self.row = detect_eps(with_swing_indicators(pl.DataFrame(self.rows)), last_date()).row(
            0, named=True
        )

    def test_the_window_is_prior_bars_plus_the_gap_day(self) -> None:
        assert len(self.window) == 61

    def test_prior_move_is_yesterdays_close_over_the_close_that_started_the_window(self) -> None:
        """§3.4: "(prev_close / close[prior_bars ago] - 1) x 100" - the run-up **before** the
        catalyst, which is what "neglected" means. Today's gap must not count towards it."""
        expected = (self.closes[-2] / self.closes[0] - 1.0) * 100.0
        assert _f(self.row, "prior_move_pct") == pytest.approx(expected, rel=1e-9)

    def test_the_distances_from_the_moving_averages_are_percentages(self) -> None:
        fast = sum(self.closes[-DEFAULT_SWING_CONFIG.ma_fast :]) / DEFAULT_SWING_CONFIG.ma_fast
        slow = sum(self.closes[-DEFAULT_SWING_CONFIG.ma_slow :]) / DEFAULT_SWING_CONFIG.ma_slow
        assert _f(self.row, "dist_ma_fast_pct") == pytest.approx(
            (self.closes[-1] / fast - 1.0) * 100.0, rel=1e-9
        )
        assert _f(self.row, "dist_ma_slow_pct") == pytest.approx(
            (self.closes[-1] / slow - 1.0) * 100.0, rel=1e-9
        )

    def test_a_band_of_one_rupee_is_still_a_band(self) -> None:
        """§3.5: "`upper_circuit` **> 0** and `high` >= `upper_circuit`".

        One rupee is not a plausible price band; the point is the guard's boundary. Zero means
        "this source told us nothing", and everything above it means "this is the band".
        """
        rows = [dict(row, upper_circuit=1.0) for row in ep_series()]
        out = detect_eps(with_swing_indicators(pl.DataFrame(rows)), last_date())
        assert out["locked_upper_circuit"].item() is True


class TestTheBreakoutDayStillNeedsItsBase:
    """§2.6: "2.1-2.2 hold (2.3-2.5 are waived on the breakout day itself)".

    The waiver is the interesting half: a breakout day is allowed to be wide, extended and
    heavy, because that is what a breakout looks like. What it is *not* allowed to skip is the
    pole and the base - otherwise every stock that closed at a twenty-day high on volume becomes
    a candidate, which is the opposite of a continuation setup.
    """

    def setup_method(self) -> None:
        rows = flag_series()
        prior_pivot = max(_column(rows[-21:-1], "high"))
        close = prior_pivot * 1.03
        rows[-1] = {
            **rows[-1],
            "open": prior_pivot * 0.99,
            "high": close * 1.01,
            "low": prior_pivot * 0.98,
            "close": close,
            "volume": 5e6,
        }
        self.rows = rows
        self.row = detect_flags(with_swing_indicators(pl.DataFrame(rows)), last_date()).row(
            0, named=True
        )
        assert self.row["status"] == CandidateStatus.BREAKOUT_TODAY.value

    def _detects(self, flag: FlagConfig) -> bool:
        config = replace(DEFAULT_SWING_CONFIG, flag=flag)
        out = detect_flags(
            with_swing_indicators(pl.DataFrame(self.rows), config), last_date(), config
        )
        return not out.is_empty()

    def test_the_breakout_path_enforces_the_base_length_floor(self) -> None:
        bars = int(_f(self.row, "base_bars"))
        assert self._detects(replace(FlagConfig(), base_min_bars=bars)) is True
        assert self._detects(replace(FlagConfig(), base_min_bars=bars + 1)) is False

    def test_the_breakout_path_enforces_the_base_length_ceiling(self) -> None:
        bars = int(_f(self.row, "base_bars"))
        assert self._detects(replace(FlagConfig(), base_max_bars=bars)) is True
        assert self._detects(replace(FlagConfig(), base_max_bars=bars - 1)) is False

    def test_the_breakout_path_enforces_the_prior_move_floor(self) -> None:
        measured = _f(self.row, "prior_move_pct")
        assert self._detects(replace(FlagConfig(), flagpole_min_gain_pct=measured)) is True
        assert (
            self._detects(replace(FlagConfig(), flagpole_min_gain_pct=measured * 1.000001)) is False
        )

    def test_the_breakout_path_enforces_the_base_depth_ceiling(self) -> None:
        measured = _f(self.row, "base_depth_pct")
        assert self._detects(replace(FlagConfig(), base_max_depth_pct=measured)) is True
        assert self._detects(replace(FlagConfig(), base_max_depth_pct=measured * 0.999999)) is False

    def test_the_breakout_path_waives_the_tightness_rule(self) -> None:
        """A tightness ceiling far below the measurement would reject a `SETTING_UP` flag and
        must not reject this one: today's wide bar is the breakout."""
        assert self._detects(replace(FlagConfig(), tight_max_adr_multiple=0.01)) is True


class TestAFlatBaseSitsExactlyOnItsRules:
    """§2.3 and §2.5 are inclusive: a base that has gone dead flat for fifty bars has
    `close == ma_trend`, `ma_slow == ma_slow[5 bars ago]` and both halves at the same low -
    three boundaries at once, all of them by equality between two measurements - and it is a
    flag."""

    def setup_method(self) -> None:
        rows = flag_series(base_bars=55)
        for i in range(len(rows) - 55, len(rows)):
            rows[i] = {**rows[i], "open": 144.0, "close": 144.0, "high": 146.88, "low": 141.12}
        self.indicated = with_swing_indicators(pl.DataFrame(rows))

    def test_the_bar_is_exactly_on_every_boundary(self) -> None:
        last = self.indicated.row(-1, named=True)
        assert _f(last, "close") == _f(last, "ma_trend") == _f(last, "ma_slow")
        assert self.indicated["ma_slow"].tail(6).to_list() == [144.0] * 6

    def test_on_the_fifty_day_on_a_flat_twenty_day_with_equal_lows_is_still_a_flag(self) -> None:
        out = detect_flags(self.indicated, last_date())
        assert out["status"].to_list() == [CandidateStatus.SETTING_UP.value]


# ---------------------------------------------------------------------------
# The stop rules' remaining edges
# ---------------------------------------------------------------------------


def _position(
    *,
    entry: Decimal = Decimal("100"),
    initial_stop: Decimal = Decimal("96"),
    stop: Decimal = Decimal("96"),
    quantity: int = 300,
) -> OpenPosition:
    return OpenPosition(
        symbol="TESTCO",
        entry_date=DAY,
        entry=entry,
        initial_stop=initial_stop,
        stop=stop,
        quantity=quantity,
        partial_done=False,
        trail=TrailMa.MA20,
    )


def _bar(
    *, close: Decimal = Decimal("110"), low: Decimal = Decimal("105"), bars: int = 3
) -> DailyBar:
    return DailyBar(
        date=DAY,
        open=Decimal("108"),
        high=Decimal("112"),
        low=low,
        close=close,
        ma10=Decimal("98"),
        ma20=Decimal("97"),
        bars_since_entry=bars,
    )


class TestThePartialsSmallestCases:
    def test_the_share_count_is_an_integer(self) -> None:
        """It is a number of shares. A float would reach the broker as `100.0` and be rejected,
        or worse, be silently truncated somewhere between here and the order."""
        assert isinstance(partial_quantity(300, STOPS), int)

    def test_a_three_share_position_sells_one(self) -> None:
        """`04` §6.3: "never 0 unless `qty < 3`". Three is the smallest position that has a
        third, and the rule has to fire on it or the boundary is one share off."""
        actions = manage(_position(quantity=3), _bar(), STOPS)
        assert next((a.kind, a.quantity) for a in actions) == (ActionKind.SELL_PARTIAL, 1)

    def test_a_two_share_position_sells_nothing_rather_than_zero_shares(self) -> None:
        """A `SELL_PARTIAL` of 0 shares is a plan line that a person would confirm and that would
        then do nothing - worse than no line at all."""
        actions = manage(_position(quantity=2), _bar(), STOPS)
        assert all(a.kind is not ActionKind.SELL_PARTIAL for a in actions)


class TestTheBreakevenEdges:
    def test_a_stop_already_at_the_entry_is_not_raised_to_the_entry_again(self) -> None:
        """§6.4.5 fires only while "the stop is below entry". Re-issuing a `RAISE_GTT_STOP` to a
        level the GTT already rests at would cancel and re-arm a working stop for nothing, which
        is a window in which the position is naked."""
        actions = manage(_position(stop=Decimal("100")), _bar(close=Decimal("110"), bars=8), STOPS)
        assert [(a.kind, a.reason) for a in actions] == [
            (ActionKind.HOLD, ActionReason.NOTHING_TO_DO)
        ]

    def test_a_position_whose_initial_stop_was_the_entry_does_not_divide_by_zero(self) -> None:
        """One R is `entry - initial_stop`. `initial_stop()` refuses to build such a position,
        but a row read back from the database is not built by `initial_stop()`, and the rules
        have to survive one rather than raising inside a nightly job."""
        actions = manage(
            _position(entry=Decimal("100"), initial_stop=Decimal("100"), stop=Decimal("96")),
            _bar(close=Decimal("110"), bars=8),
            STOPS,
        )
        assert [(a.kind, a.reason) for a in actions] == [
            (ActionKind.HOLD, ActionReason.NOTHING_TO_DO)
        ]


class TestApplyIgnoresAnIncompleteRaise:
    def test_a_raise_with_no_level_changes_nothing(self) -> None:
        """`apply` reads `action.new_stop`; an action without one is not a stop to move to.
        Taking `max(stop, None)` would raise inside the EOD job."""
        before = _position()
        after = apply(before, [Action(ActionKind.RAISE_STOP, ActionReason.BREAKEVEN_AT_R)])
        assert after.stop == before.stop

    def test_the_plan_does_not_emit_a_raise_line_without_a_level(self) -> None:
        lines = exit_lines("TESTCO", [Action(ActionKind.RAISE_STOP, ActionReason.BREAKEVEN_AT_R)])
        assert lines == []


# ---------------------------------------------------------------------------
# Market, journal and opening-range edges
# ---------------------------------------------------------------------------


class TestTheIndexReadingEdges:
    """§8.2 (SW9.5). The rule is the 10-day MA against the 20-day, and equal averages are
    neither side of it: `long_bias` is strictly above, `bearish` strictly below."""

    def test_a_fast_average_level_with_the_slow_one_is_not_long_biased(self) -> None:
        assert IndexReading(close=101.0, ma_fast=100.0, ma_slow=100.0).long_bias is False

    def test_a_fast_average_level_with_the_slow_one_is_not_bearish(self) -> None:
        assert IndexReading(close=99.0, ma_fast=100.0, ma_slow=100.0).bearish is False

    def test_a_fast_average_a_tick_above_the_slow_one_is_long_biased(self) -> None:
        """Whatever the close: a pullback under both averages on a rising 10-day is long."""
        assert IndexReading(close=90.0, ma_fast=100.01, ma_slow=100.0).long_bias is True

    def test_a_fast_average_a_tick_below_the_slow_one_is_bearish(self) -> None:
        """Whatever the close: a bounce over both averages under a falling 10-day is bearish."""
        assert IndexReading(close=110.0, ma_fast=99.99, ma_slow=100.0).bearish is True


class TestBreadthEdges:
    def test_an_empty_universe_reports_zero_for_every_share(self) -> None:
        """Not just the first one: a "1%" that came from an empty universe would render on the
        market page as a fact about the market."""
        empty = breadth_snapshot(
            pl.DataFrame(
                schema={
                    "ret_20": pl.Float64,
                    "close": pl.Float64,
                    "ma_slow": pl.Float64,
                    "high_1y": pl.Float64,
                }
            ),
            MARKET,
        )
        assert (
            empty.constituent_count,
            empty.pct_up_strong_1m,
            empty.pct_new_52w_high,
            empty.pct_above_ma_slow,
        ) == (0, 0.0, 0.0, 0.0)

    def test_every_share_is_rounded_to_two_decimals(self) -> None:
        """One in three is 33.33%, not 33.333%. `sw_market_daily` stores `BREADTH` (7,4), so a
        third decimal would be stored and then disagree with the page that rounded it again."""
        rows = [
            {"ret_20": 100.0, "close": 300.0, "ma_slow": 1.0, "high_1y": 200.0},
            {"ret_20": 0.0, "close": 1.0, "ma_slow": 300.0, "high_1y": 200.0},
            {"ret_20": 0.0, "close": 1.0, "ma_slow": 300.0, "high_1y": 200.0},
        ]
        snapshot = breadth_snapshot(pl.DataFrame(rows), MARKET)
        assert snapshot.pct_up_strong_1m == 33.33
        assert snapshot.pct_new_52w_high == 33.33
        assert snapshot.pct_above_ma_slow == 33.33


class TestTheLossStreakCountsLossesOnly:
    def test_a_scratch_trade_ends_a_losing_streak(self) -> None:
        """§8.4 steps down on a "loss streak". A trade that got out at breakeven is not a loss,
        and counting it as one would shrink the book for a trade that cost nothing."""
        tier = exposure_tier(
            current_level=2,
            closed_r_multiples=[Decimal(v) for v in ("1", "1", "-1", "-1", "0")],
            gate=MarketGate.GREEN,
            config=MARKET,
        )
        assert tier.level == 2

    def test_the_journals_streak_agrees(self) -> None:
        trades = [_closed(exit_avg="96"), _closed(exit_avg="100")]
        assert summarize(trades).current_loss_streak == 0


def _closed(*, exit_avg: str = "112") -> ClosedTrade:
    return ClosedTrade(
        symbol="TESTCO",
        setup="FLAG",
        entry_date=DAY,
        exit_date=DAY + dt.timedelta(days=7),
        entry=Decimal("100"),
        initial_stop=Decimal("96"),
        exit_avg=Decimal(exit_avg),
        quantity=100,
    )


class TestTheJournalCountsAScratchAsALoser:
    def test_a_zero_r_trade_is_averaged_into_the_losses(self) -> None:
        """§10: `avg_loss_r` is the mean of `r <= 0`. A scratch belongs there - it is a trade
        that did not work - and excluding it would flatter the average loss."""
        stats = summarize([_closed(exit_avg="88"), _closed(exit_avg="100")])
        assert stats.avg_loss_r == Decimal("-1.50")

    def test_a_single_share_exit_still_has_an_average(self) -> None:
        assert exit_average([(1, Decimal("123.45"))]) == Decimal("123.45")


class TestTheOpeningRangeEdges:
    def test_a_one_candle_range_is_a_range(self) -> None:
        """The 1-minute window is one of the three `04` §7.1 allows, so a complete range of a
        single candle is the normal case for it, not a degenerate one."""
        verdict = evaluate_trigger(
            last_price=Decimal("120"),
            opening=OpeningRange(
                high=Decimal("105"),
                low=Decimal("100"),
                window_minutes=1,
                complete=True,
                candles=1,
            ),
            pivot_high=None,
            low_of_day=Decimal("99"),
            upper_circuit=None,
            at=dt.datetime.combine(DAY, dt.time(9, 30)),
            config=RANGE,
        )
        assert verdict.state is TriggerState.TRIGGERED

    def test_one_minute_into_the_session_is_enough_to_measure_a_pace(self) -> None:
        """§7.3 pro-rates against the minutes elapsed; the first minute is the one an EP is
        found in, so it must not be excluded by the guard that exists for zero."""
        verdict = live_gap(
            prev_close=Decimal("100"),
            last_price=Decimal("110"),
            volume_so_far=3_000,
            avg_daily_volume=Decimal("375000"),
            minutes_elapsed=1,
            config=RANGE,
        )
        assert verdict.is_candidate is True

    def test_a_name_with_no_volume_history_is_not_a_candidate(self) -> None:
        """A pace measured against a zero average is a division by zero, not an infinite pace."""
        verdict = live_gap(
            prev_close=Decimal("100"),
            last_price=Decimal("110"),
            volume_so_far=30_000,
            avg_daily_volume=Decimal("0"),
            minutes_elapsed=10,
            config=RANGE,
        )
        assert verdict.is_candidate is False


class TestTheWatchItemDefault:
    def test_a_watch_item_is_not_locked_unless_it_says_so(self) -> None:
        """`WatchItem.locked_upper_circuit` defaults to False. A default of True would skip every
        manually added name with `LOCKED_UPPER_CIRCUIT` and the plan would be empty for a reason
        nobody could see."""
        item = WatchItem(
            symbol="AAA",
            setup=Setup.FLAG,
            trigger=Decimal("100"),
            stop_ref=Decimal("96"),
            adr_pct=Decimal("5"),
            avg_turnover_inr=Decimal("100000000"),
            score=Decimal("80"),
        )
        assert item.locked_upper_circuit is False
        lines, skipped = build_entries(
            as_of=DAY,
            watch=[item],
            account=SwingAccount(
                equity=Decimal("1000000"),
                cash_available=Decimal("1000000"),
                open_symbols=frozenset(),
                open_exposure_inr=Decimal("0"),
            ),
            gate=MarketGate.GREEN,
            tier=ExposureTier(3, 8, 100.0, new_entries_allowed=True),
            config=DEFAULT_SWING_CONFIG,
        )
        assert [line.kind for line in lines] == [LineKind.BUY_ON_TRIGGER]
        assert skipped == []


class TestSizingsSmallestPositions:
    def test_a_single_share_worth_more_than_the_floor_is_a_position(self) -> None:
        """§5.1's floor is on the trade's **value**, not on its share count. One share of a
        Rs 20,000 stock clears a Rs 10,000 floor, and refusing it would silently exclude every
        high-priced name from the book."""
        sized = size_position(
            equity=Decimal("250000"),
            cash_available=Decimal("250000"),
            entry=Decimal("20000"),
            stop=Decimal("19000"),
            avg_turnover_inr=Decimal("100000000"),
            config=DEFAULT_SWING_CONFIG.sizing,
            max_stop_distance_pct=Decimal("10"),
        )
        assert sized.quantity == 1
        assert sized.refusal is None

    def test_an_entry_price_of_zero_is_refused_rather_than_divided_by(self) -> None:
        sized = size_position(
            equity=Decimal("100000"),
            cash_available=Decimal("100000"),
            entry=Decimal("0"),
            stop=Decimal("-1"),
            avg_turnover_inr=None,
            config=DEFAULT_SWING_CONFIG.sizing,
            max_stop_distance_pct=Decimal("10"),
        )
        assert sized.refusal is SizeRefusal.STOP_NOT_BELOW_ENTRY


class TestTheMarketConfigDefaults:
    def test_the_documented_defaults_are_the_shipped_ones(self) -> None:
        """`04` §8's bracketed numbers, so a recalibration has to edit the document too."""
        assert MarketConfig().strong_move_pct == 25.0
        assert MarketConfig().green_min_pct_up == 5.0
        assert MarketConfig().red_max_pct_up == 2.0
        assert (MarketConfig().index_ma_fast, MarketConfig().index_ma_slow) == (10, 20)
        assert MarketConfig().lookback_trades == 5
        assert MarketConfig().step_down_loss_streak == 3


class TestTheBreadthSnapshotIsWhatTheGateReads:
    def test_the_gate_reads_the_strong_move_share_and_not_another_one(self) -> None:
        """§8.3: "Breadth decides" - and the breadth number it decides on is `pct_up_strong_1m`.
        A gate that read `pct_above_ma_slow` would be green in every drifting market."""
        strong_only = BreadthSnapshot(100, 10.0, 0.0, 0.0)
        weak_but_broad = BreadthSnapshot(100, 1.0, 99.0, 99.0)
        assert market_gate(strong_only, None, MARKET) is MarketGate.GREEN
        assert market_gate(weak_but_broad, None, MARKET) is MarketGate.RED
