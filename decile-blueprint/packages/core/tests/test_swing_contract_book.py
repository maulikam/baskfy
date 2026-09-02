"""The book's numbers, pinned exactly - `docs/swing/04` §5-§10.

The companion to `test_swing_contract_detectors.py`, and the more important half. A detector that
is a percent out finds a slightly different list of stocks; a stop rule that is a paisa out sells
a position it should have held, or holds one it should have sold.

Every test here is a **boundary**. `04` is written in `>=` and `<=`, and each of those has an
exact value at which the rule is supposed to fire:

* `04` §6.4.1 - "`bar.low` <= `stop` -> `STOPPED_OUT`". A bar whose low is exactly the stop is a
  stopped-out position, because that is where a resting GTT would have triggered.
* `04` §5.1 - "`STOP_TOO_WIDE` (`(entry - stop) / entry x 100` > 10)". A stop exactly 10% away is
  the widest the method tolerates, and it is tolerated.
* `04` §6.4.4 - "`partial_earliest_bar` [3] <= `bars_since_entry` <= `partial_latest_bar` [5]".
  Day three sells; day two does not.

The first mutation run over `baskfy_core.swing` scored `stops.py` at 40% and `opening_range.py`
at 49%, and every survivor in both was one of these equalities. They are not pedantry: "sell a
third on day 3-5" and "sell a third on day 4-5" are different strategies, and only one of them is
the one the document describes.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from decimal import Decimal
from typing import Final

import polars as pl
import pytest

from baskfy_core.swing.config import (
    DEFAULT_SWING_CONFIG,
    EpConfig,
    Setup,
    SizingConfig,
    StopConfig,
)
from baskfy_core.swing.journal import ClosedTrade, exit_average, summarize
from baskfy_core.swing.market import (
    BreadthSnapshot,
    ExposureTier,
    IndexReading,
    MarketGate,
    breadth_snapshot,
    drawdown_locked,
    exposure_tier,
    market_gate,
)
from baskfy_core.swing.opening_range import (
    SESSION_MINUTES,
    Candle,
    LiveGapVerdict,
    OpeningRange,
    TriggerState,
    TriggerVerdict,
    evaluate_trigger,
    live_gap,
    live_gap_score,
    opening_range,
)
from baskfy_core.swing.plan import (
    LineKind,
    SkipReason,
    SwingAccount,
    WatchItem,
    build_entries,
    to_tick,
)
from baskfy_core.swing.sizing import (
    SizeCap,
    SizedPosition,
    SizeRefusal,
    implied_risk_pct,
    r_multiple,
    size_position,
)
from baskfy_core.swing.stops import (
    Action,
    ActionKind,
    ActionReason,
    DailyBar,
    OpenPosition,
    StopMode,
    TrailMa,
    apply,
    choose_trail,
    initial_stop,
    manage,
    partial_quantity,
    widest_stop_pct,
)

SIZING: Final = DEFAULT_SWING_CONFIG.sizing
STOPS: Final = DEFAULT_SWING_CONFIG.stops
MARKET: Final = DEFAULT_SWING_CONFIG.market
RANGE: Final = DEFAULT_SWING_CONFIG.opening_range

DAY: Final = dt.date(2026, 9, 1)
#: One paisa - the exchange's own smallest step, and therefore the smallest difference that can
#: actually exist between two prices in this system.
PAISA: Final = Decimal("0.01")


# ---------------------------------------------------------------------------
# §5 - sizing
# ---------------------------------------------------------------------------


def _size(  # noqa: PLR0913 - the inputs a size depends on, each named
    *,
    equity: str = "1000000",
    cash: str = "1000000",
    entry: str = "100",
    stop: str = "96",
    turnover: str | None = "100000000",
    config: SizingConfig = SIZING,
    max_stop_distance_pct: str = "10",
) -> SizedPosition:
    return size_position(
        equity=Decimal(equity),
        cash_available=Decimal(cash),
        entry=Decimal(entry),
        stop=Decimal(stop),
        avg_turnover_inr=None if turnover is None else Decimal(turnover),
        config=config,
        max_stop_distance_pct=Decimal(max_stop_distance_pct),
    )


class TestTheWorkedExample:
    """`04` §5.2: "stop 4% below, risk 0.5% -> position 12.5% of equity". His own arithmetic."""

    def test_his_own_numbers_come_out(self) -> None:
        sized = _size(equity="1000000", entry="100", stop="96")
        assert sized.quantity == 1250
        assert sized.position_value == Decimal("125000.00")
        assert sized.position_pct == Decimal("12.50")
        assert sized.risk_inr == Decimal("5000.00")
        assert sized.cap is SizeCap.RISK

    def test_the_risk_taken_is_the_risk_budgeted(self) -> None:
        sized = _size(equity="1000000", entry="100", stop="96")
        assert implied_risk_pct(sized, Decimal("1000000")) == Decimal("0.50")


class TestTheRefusalsAndTheirBoundaries:
    """§5.1's four refusals, "in order" - and each one at the exact value it refuses from."""

    def test_no_equity_is_refused_at_zero_not_below_it(self) -> None:
        """ "equity <= 0". A sleeve with nothing in it plans nothing (`sw_config` starts at 0)."""
        assert _size(equity="0").refusal is SizeRefusal.NO_EQUITY
        assert _size(equity="0.01").refusal is not SizeRefusal.NO_EQUITY

    def test_a_stop_equal_to_the_entry_is_not_a_stop(self) -> None:
        """ "`STOP_NOT_BELOW_ENTRY`" - a zero-distance stop is a division by zero, not a trade."""
        assert _size(entry="100", stop="100").refusal is SizeRefusal.STOP_NOT_BELOW_ENTRY
        assert _size(entry="100", stop="99.99").refusal is None

    def test_a_stop_exactly_at_the_width_limit_is_allowed(self) -> None:
        """ "`STOP_TOO_WIDE` ((entry - stop) / entry x 100 **>** 10)". Exactly 10% is not "too"."""
        assert _size(entry="100", stop="90").refusal is None
        assert _size(entry="100", stop="89.99").refusal is SizeRefusal.STOP_TOO_WIDE

    def test_the_size_is_not_shrunk_to_fit_a_bad_stop(self) -> None:
        """§5.1: "the size is **not** shrunk to fit a bad stop"."""
        assert _size(entry="100", stop="80").quantity == 0

    def test_a_trade_exactly_at_the_minimum_value_is_allowed(self) -> None:
        """ "`BELOW_MIN_TRADE_VALUE` (`entry x qty` **<** `min_trade_value_inr`)"."""
        # 100 shares at Rs 100 is exactly Rs 10,000: the floor, and therefore acceptable.
        config = replace(SizingConfig(), max_position_pct=1.0)
        sized = _size(equity="1000000", entry="100", stop="96", config=config)
        assert sized.position_value == Decimal("10000.00")
        assert sized.refusal is None

    def test_a_trade_a_paisa_under_the_minimum_is_refused(self) -> None:
        config = replace(SizingConfig(), min_trade_value_inr=10_000.01)
        sized = _size(
            equity="1000000",
            entry="100",
            stop="96",
            config=replace(config, max_position_pct=1.0),
        )
        assert sized.refusal is SizeRefusal.BELOW_MIN_TRADE_VALUE

    def test_a_refusal_still_reports_the_stop_distance(self) -> None:
        """A refusal that says nothing about why is a refusal a person cannot act on."""
        assert _size(entry="100", stop="80").stop_distance_pct == Decimal("20.00")


class TestTheFourCapsAndWhichOneBound:
    """§5.2: "`qty = min(by_risk, by_position, by_cash, by_turnover)`", and `cap` names it."""

    def test_the_risk_cap_binds_when_it_is_the_smallest(self) -> None:
        assert _size(equity="1000000", entry="100", stop="96").cap is SizeCap.RISK

    def test_the_position_cap_binds_on_a_tight_stop(self) -> None:
        """A stop 1% away lets risk buy 50,000 shares; 20% of equity allows 2,000."""
        sized = _size(equity="1000000", entry="100", stop="99")
        assert sized.cap is SizeCap.POSITION_PCT
        assert sized.quantity == 2000

    def test_the_cash_cap_binds_when_the_sleeve_is_short_of_cash(self) -> None:
        sized = _size(equity="1000000", cash="50000", entry="100", stop="96")
        assert sized.cap is SizeCap.CASH
        assert sized.quantity == 500

    def test_the_turnover_cap_binds_on_a_thin_name(self) -> None:
        """§5.2: "`avg_turnover_inr x max_position_vs_turnover` [0.01] / entry"."""
        sized = _size(equity="1000000", entry="100", stop="96", turnover="6000000")
        assert sized.cap is SizeCap.TURNOVER
        assert sized.quantity == 600

    def test_an_unknown_turnover_simply_does_not_cap(self) -> None:
        """ "(when known)" - a name with no turnover history is sized by the other three."""
        assert _size(turnover=None).cap is SizeCap.RISK

    def test_a_zero_turnover_does_not_cap_either(self) -> None:
        """Zero would otherwise cap every position at zero shares and refuse the whole book."""
        assert _size(turnover="0").cap is SizeCap.RISK

    def test_the_quantity_is_floored_rather_than_rounded(self) -> None:
        """A rounded-up share count risks more than the budget, which §5 does not allow."""
        sized = _size(equity="1000000", entry="100", stop="96.5")
        assert sized.quantity == 1428  # 5000 / 3.5 = 1428.57...
        assert sized.risk_inr <= Decimal("5000")


class TestRMultiple:
    """§5: "`r_multiple(entry, stop, exit) = (exit - entry) / (entry - stop)`, 2 dp"."""

    def test_a_full_stop_out_is_minus_one_r(self) -> None:
        assert r_multiple(entry=Decimal(100), stop=Decimal(96), exit_price=Decimal(96)) == Decimal(
            "-1.00"
        )

    def test_three_times_the_risk_is_three_r(self) -> None:
        assert r_multiple(entry=Decimal(100), stop=Decimal(96), exit_price=Decimal(112)) == Decimal(
            "3.00"
        )

    def test_it_is_two_decimal_places(self) -> None:
        value = r_multiple(entry=Decimal(100), stop=Decimal(97), exit_price=Decimal(101))
        assert value == Decimal("0.33")
        assert value.as_tuple().exponent == -2

    def test_a_stop_at_or_above_the_entry_has_no_r_at_all(self) -> None:
        with pytest.raises(ValueError, match="must be below entry"):
            r_multiple(entry=Decimal(100), stop=Decimal(100), exit_price=Decimal(110))


# ---------------------------------------------------------------------------
# §6 - stops and management
# ---------------------------------------------------------------------------


def _position(  # noqa: PLR0913 - one keyword per field of the position being described
    *,
    entry: Decimal = Decimal("100"),
    initial_stop: Decimal = Decimal("96"),
    stop: Decimal = Decimal("96"),
    quantity: int = 300,
    partial_done: bool = False,
    trail: TrailMa = TrailMa.MA20,
    is_ep_gap_day: bool = False,
) -> OpenPosition:
    """A plain long: 300 shares bought at Rs 100 with the stop Rs 4 below, so one R is Rs 4."""
    return OpenPosition(
        symbol="TESTCO",
        entry_date=DAY,
        entry=entry,
        initial_stop=initial_stop,
        stop=stop,
        quantity=quantity,
        partial_done=partial_done,
        trail=trail,
        is_ep_gap_day=is_ep_gap_day,
    )


def _bar(  # noqa: PLR0913 - one keyword per field of the bar being described
    *,
    open: Decimal = Decimal("100"),
    high: Decimal = Decimal("102"),
    low: Decimal = Decimal("99"),
    close: Decimal = Decimal("101"),
    ma10: Decimal | None = Decimal("98"),
    ma20: Decimal | None = Decimal("97"),
    bars_since_entry: int = 1,
) -> DailyBar:
    """A green day that touches neither the stop nor either trail, so every rule can be asked
    for on its own without another one answering first."""
    return DailyBar(
        date=DAY,
        open=open,
        high=high,
        low=low,
        close=close,
        ma10=ma10,
        ma20=ma20,
        bars_since_entry=bars_since_entry,
    )


class TestTheInitialStop:
    """§6.1."""

    def test_the_default_is_the_low_of_the_day(self) -> None:
        stop = initial_stop(
            entry=Decimal("100"),
            low_of_day=Decimal("97"),
            opening_range_low=Decimal("98"),
            mode=StopMode.LOW_OF_DAY,
        )
        assert stop == Decimal("97")

    def test_the_opening_range_mode_takes_the_tighter_of_the_two(self) -> None:
        """ "`max(range_low, low_of_day)` (the tighter)"."""
        assert initial_stop(
            entry=Decimal("100"),
            low_of_day=Decimal("97"),
            opening_range_low=Decimal("98"),
            mode=StopMode.OPENING_RANGE_LOW,
        ) == Decimal("98")
        assert initial_stop(
            entry=Decimal("100"),
            low_of_day=Decimal("98.5"),
            opening_range_low=Decimal("98"),
            mode=StopMode.OPENING_RANGE_LOW,
        ) == Decimal("98.5")

    def test_the_opening_range_mode_falls_back_when_there_is_no_range(self) -> None:
        assert initial_stop(
            entry=Decimal("100"),
            low_of_day=Decimal("97"),
            opening_range_low=None,
            mode=StopMode.OPENING_RANGE_LOW,
        ) == Decimal("97")

    def test_a_stop_exactly_at_the_entry_is_an_error_not_a_position(self) -> None:
        """§6.1: "A stop >= entry is an error, not a position"."""
        with pytest.raises(ValueError, match="not below entry"):
            initial_stop(
                entry=Decimal("100"),
                low_of_day=Decimal("100"),
                opening_range_low=None,
                mode=StopMode.LOW_OF_DAY,
            )


class TestTheTrailChoice:
    """§6.2: "`MA10` when `adr_pct` >= `fast_trail_min_adr_pct` [6] else `MA20`"."""

    def test_exactly_at_the_threshold_the_fast_trail_is_used(self) -> None:
        assert choose_trail(Decimal("6"), STOPS) is TrailMa.MA10

    def test_a_hair_below_it_the_slow_trail_is_used(self) -> None:
        assert choose_trail(Decimal("5.99"), STOPS) is TrailMa.MA20


class TestThePartialQuantity:
    """§6.3: "`qty x partial_numerator / partial_denominator` [1/3], integer division, never the
    whole position, never 0 unless `qty < 3`"."""

    def test_a_third_of_three_hundred_is_a_hundred(self) -> None:
        assert partial_quantity(300, STOPS) == 100

    def test_integer_division_rounds_down(self) -> None:
        assert partial_quantity(100, STOPS) == 33

    def test_it_is_never_the_whole_position(self) -> None:
        """A "partial" that sold everything would skip the trail and the runner it exists for."""
        whole = replace(StopConfig(), partial_numerator=1, partial_denominator=1)
        assert partial_quantity(300, whole) == 299

    def test_two_shares_cannot_be_thirded(self) -> None:
        assert partial_quantity(2, STOPS) == 0

    def test_three_shares_can(self) -> None:
        assert partial_quantity(3, STOPS) == 1

    def test_one_share_is_left_alone(self) -> None:
        assert partial_quantity(1, STOPS) == 0


class TestManagePrecedence:
    """§6.4's six rules, "evaluated after each close, precedence top-down"."""

    def test_a_low_exactly_at_the_stop_is_a_stop_out(self) -> None:
        """§6.4.1: "`bar.low` <= `stop`". This is where a resting GTT would have fired."""
        actions = manage(_position(), _bar(low=Decimal("96")), STOPS)
        assert [(a.kind, a.reason) for a in actions] == [
            (ActionKind.STOPPED_OUT, ActionReason.HARD_STOP_HIT)
        ]

    def test_a_low_a_paisa_above_the_stop_is_not(self) -> None:
        actions = manage(_position(), _bar(low=Decimal("96") + PAISA), STOPS)
        assert actions[0].kind is not ActionKind.STOPPED_OUT

    def test_the_stop_out_sells_the_whole_position(self) -> None:
        actions = manage(_position(quantity=300), _bar(low=Decimal("90")), STOPS)
        assert actions[0].quantity == 300

    def test_a_stop_out_outranks_everything_below_it(self) -> None:
        """A bar that hit the stop *and* closed green is still a stop-out: the GTT fired first."""
        actions = manage(
            _position(),
            _bar(low=Decimal("95"), close=Decimal("110"), bars_since_entry=4),
            STOPS,
        )
        assert actions[0].kind is ActionKind.STOPPED_OUT

    def test_an_ep_that_closes_red_on_its_gap_day_is_sold(self) -> None:
        """§6.4.2: "EP on its gap day (`bars_since_entry == 0`) with `close < open`"."""
        actions = manage(
            _position(is_ep_gap_day=True),
            _bar(bars_since_entry=0, open=Decimal("101"), close=Decimal("100.99")),
            STOPS,
        )
        assert [(a.kind, a.reason) for a in actions] == [
            (ActionKind.SELL_ALL, ActionReason.EP_FAILED_RED_ON_DAY)
        ]

    def test_an_ep_that_closes_exactly_at_its_open_is_held(self) -> None:
        """ "`close < open`" - unchanged is not red, and an unchanged EP has not failed."""
        actions = manage(
            _position(is_ep_gap_day=True),
            _bar(bars_since_entry=0, open=Decimal("101"), close=Decimal("101")),
            STOPS,
        )
        assert actions[0].kind is not ActionKind.SELL_ALL

    def test_the_ep_rule_applies_only_on_the_gap_day_itself(self) -> None:
        actions = manage(
            _position(is_ep_gap_day=True),
            _bar(bars_since_entry=1, open=Decimal("101"), close=Decimal("100"), ma20=Decimal("90")),
            STOPS,
        )
        assert actions[0].reason is not ActionReason.EP_FAILED_RED_ON_DAY

    def test_a_close_below_the_trail_sells_the_rest(self) -> None:
        """§6.4.3: "`bars_since_entry > 0` and `close < trail MA`"."""
        actions = manage(
            _position(trail=TrailMa.MA20),
            _bar(close=Decimal("96.99"), ma20=Decimal("97"), low=Decimal("96.5")),
            STOPS,
        )
        assert [(a.kind, a.reason) for a in actions] == [
            (ActionKind.SELL_ALL, ActionReason.CLOSE_BELOW_TRAIL_MA)
        ]

    def test_a_close_exactly_on_the_trail_is_held(self) -> None:
        """ "a close **below** it" - sitting on the average is surfing it, which is the setup."""
        actions = manage(
            _position(trail=TrailMa.MA20),
            _bar(close=Decimal("97"), ma20=Decimal("97"), low=Decimal("96.5")),
            STOPS,
        )
        assert actions[0].kind is not ActionKind.SELL_ALL

    def test_the_trail_rule_does_not_fire_on_the_entry_day(self) -> None:
        """ "`bars_since_entry` > 0" - a breakout that closes under its 20-day is still a breakout
        on the day it happened; the stop, not the trail, is what protects day zero."""
        actions = manage(
            _position(),
            _bar(bars_since_entry=0, close=Decimal("96.5"), ma20=Decimal("97")),
            STOPS,
        )
        assert actions[0].kind is not ActionKind.SELL_ALL

    def test_the_position_trails_the_moving_average_its_own_field_names(self) -> None:
        """A position marked MA10 must not be judged against the 20-day, or vice versa."""
        below_ten_above_twenty = _bar(close=Decimal("97.5"), ma10=Decimal("98"), ma20=Decimal("97"))
        assert (
            manage(_position(trail=TrailMa.MA10), below_ten_above_twenty, STOPS)[0].kind
            is ActionKind.SELL_ALL
        )
        assert (
            manage(_position(trail=TrailMa.MA20), below_ten_above_twenty, STOPS)[0].kind
            is not ActionKind.SELL_ALL
        )


class TestThePartialAndTheBreakeven:
    """§6.4.4 and §6.4.5."""

    def test_day_three_green_sells_a_third_and_moves_the_stop_to_breakeven(self) -> None:
        actions = manage(_position(), _bar(bars_since_entry=3, close=Decimal("110")), STOPS)
        assert [(a.kind, a.reason, a.quantity, a.new_stop) for a in actions] == [
            (ActionKind.SELL_PARTIAL, ActionReason.PARTIAL_INTO_STRENGTH, 100, None),
            (ActionKind.RAISE_STOP, ActionReason.BREAKEVEN_AFTER_PARTIAL, 0, Decimal("100")),
        ]

    def test_day_two_is_too_early(self) -> None:
        actions = manage(_position(), _bar(bars_since_entry=2, close=Decimal("110")), STOPS)
        assert all(a.kind is not ActionKind.SELL_PARTIAL for a in actions)

    def test_day_five_is_the_last_day(self) -> None:
        assert (
            manage(_position(), _bar(bars_since_entry=5, close=Decimal("110")), STOPS)[0].kind
            is ActionKind.SELL_PARTIAL
        )

    def test_day_six_is_too_late(self) -> None:
        actions = manage(_position(), _bar(bars_since_entry=6, close=Decimal("110")), STOPS)
        assert all(a.kind is not ActionKind.SELL_PARTIAL for a in actions)

    def test_a_close_exactly_at_the_entry_is_not_strength(self) -> None:
        """ "`close > entry`" - selling into a flat position is not selling into strength."""
        actions = manage(_position(), _bar(bars_since_entry=3, close=Decimal("100")), STOPS)
        assert all(a.kind is not ActionKind.SELL_PARTIAL for a in actions)

    def test_the_partial_happens_once(self) -> None:
        actions = manage(
            _position(partial_done=True), _bar(bars_since_entry=4, close=Decimal("110")), STOPS
        )
        assert all(a.kind is not ActionKind.SELL_PARTIAL for a in actions)

    def test_a_stop_already_at_breakeven_is_not_raised_again_by_the_partial(self) -> None:
        actions = manage(
            _position(stop=Decimal("100")),
            # The bar's low has to clear the raised stop, or rule 1 fires first and correctly.
            _bar(bars_since_entry=3, low=Decimal("101"), close=Decimal("110")),
            STOPS,
        )
        assert [a.kind for a in actions] == [ActionKind.SELL_PARTIAL]

    def test_exactly_one_r_of_profit_moves_the_stop_to_breakeven(self) -> None:
        """§6.4.5: "(close - entry) / (entry - initial_stop) >= `breakeven_after_r` [1.0]".

        Entry 100, initial stop 96, so one R is Rs 4 and the close that earns it is 104.
        """
        actions = manage(_position(), _bar(bars_since_entry=8, close=Decimal("104")), STOPS)
        assert [(a.kind, a.reason, a.new_stop) for a in actions] == [
            (ActionKind.RAISE_STOP, ActionReason.BREAKEVEN_AT_R, Decimal("100"))
        ]

    def test_a_paisa_short_of_one_r_does_not(self) -> None:
        actions = manage(_position(), _bar(bars_since_entry=8, close=Decimal("103.99")), STOPS)
        assert [(a.kind, a.reason) for a in actions] == [
            (ActionKind.HOLD, ActionReason.NOTHING_TO_DO)
        ]

    def test_a_position_with_nothing_to_do_says_so(self) -> None:
        """§6.4.6: "Otherwise `HOLD` / `NOTHING_TO_DO` - said explicitly".

        An empty action list would be indistinguishable from a rule that failed to run.
        """
        actions = manage(_position(), _bar(bars_since_entry=2, close=Decimal("101")), STOPS)
        assert [(a.kind, a.reason) for a in actions] == [
            (ActionKind.HOLD, ActionReason.NOTHING_TO_DO)
        ]


class TestApply:
    """§6.5: "A stop never falls: `apply` takes `max(stop, new_stop)`"."""

    def test_a_raise_moves_the_stop_up(self) -> None:
        after = apply(
            _position(), manage(_position(), _bar(bars_since_entry=8, close=Decimal("104")), STOPS)
        )
        assert after.stop == Decimal("100")

    def test_a_lower_new_stop_is_ignored(self) -> None:
        after = apply(
            _position(stop=Decimal("100")),
            [Action(ActionKind.RAISE_STOP, ActionReason.BREAKEVEN_AT_R, new_stop=Decimal("90"))],
        )
        assert after.stop == Decimal("100")

    def test_a_partial_reduces_the_quantity_and_records_that_it_happened(self) -> None:
        after = apply(
            _position(), manage(_position(), _bar(bars_since_entry=3, close=Decimal("110")), STOPS)
        )
        assert (after.quantity, after.partial_done) == (200, True)

    def test_a_sell_all_empties_the_position(self) -> None:
        after = apply(_position(), manage(_position(), _bar(low=Decimal("90")), STOPS))
        assert after.quantity == 0

    def test_a_hold_changes_nothing(self) -> None:
        before = _position()
        after = apply(before, manage(before, _bar(bars_since_entry=2), STOPS))
        assert after == before


# ---------------------------------------------------------------------------
# §8 - breadth, the gate and the ladder
# ---------------------------------------------------------------------------


def _universe(rows: list[dict[str, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows,
        schema={
            "ret_20": pl.Float64,
            "close": pl.Float64,
            "ma_slow": pl.Float64,
            "high_1y": pl.Float64,
        },
    )


class TestBreadth:
    """§8.1's three shares, each with its own comparison."""

    def test_a_return_exactly_at_the_strong_move_counts(self) -> None:
        """ "share with `ret_20` >= `strong_move_pct` [25]"."""
        frame = _universe(
            [
                {"ret_20": 25.0, "close": 100.0, "ma_slow": 110.0, "high_1y": 200.0},
                {"ret_20": 24.99, "close": 100.0, "ma_slow": 110.0, "high_1y": 200.0},
            ]
        )
        assert breadth_snapshot(frame, MARKET).pct_up_strong_1m == 50.0

    def test_a_close_exactly_at_the_year_high_is_a_new_high(self) -> None:
        """ "share with `close` >= `high_1y`"."""
        frame = _universe(
            [
                {"ret_20": 0.0, "close": 200.0, "ma_slow": 110.0, "high_1y": 200.0},
                {"ret_20": 0.0, "close": 199.99, "ma_slow": 110.0, "high_1y": 200.0},
            ]
        )
        assert breadth_snapshot(frame, MARKET).pct_new_52w_high == 50.0

    def test_a_close_exactly_on_the_slow_average_is_not_above_it(self) -> None:
        """ "share with `close` **>** `ma_slow`" - the one strict comparison of the three."""
        frame = _universe(
            [
                {"ret_20": 0.0, "close": 100.01, "ma_slow": 100.0, "high_1y": 200.0},
                {"ret_20": 0.0, "close": 100.0, "ma_slow": 100.0, "high_1y": 200.0},
            ]
        )
        assert breadth_snapshot(frame, MARKET).pct_above_ma_slow == 50.0

    def test_the_shares_are_two_decimal_places(self) -> None:
        frame = _universe(
            [{"ret_20": 100.0, "close": 1.0, "ma_slow": 2.0, "high_1y": 3.0}] * 1
            + [{"ret_20": 0.0, "close": 1.0, "ma_slow": 2.0, "high_1y": 3.0}] * 2
        )
        assert breadth_snapshot(frame, MARKET).pct_up_strong_1m == 33.33

    def test_an_empty_universe_is_zeroes_rather_than_a_division_by_zero(self) -> None:
        empty = breadth_snapshot(_universe([]), MARKET)
        assert (empty.constituent_count, empty.pct_up_strong_1m) == (0, 0.0)


def _breadth(pct_up: float) -> BreadthSnapshot:
    return BreadthSnapshot(
        constituent_count=100, pct_up_strong_1m=pct_up, pct_new_52w_high=0.0, pct_above_ma_slow=0.0
    )


class TestTheGate:
    """§8.3. "Breadth decides; the index can only make it worse".

    The index rule is his own (`07`, SW9.5): long setups only while the index's 10-day MA is
    above its 20-day. The close itself is not consulted — a pullback to a rising 10-day is
    still a long tape, and a bounce under a falling one is not.
    """

    #: 10-day over 20-day, with the close *under* both: the reading is still long-biased.
    LONG = IndexReading(close=96.0, ma_fast=100.0, ma_slow=99.0)
    #: 10-day under 20-day, with the close *over* both: bearish all the same.
    BEARISH = IndexReading(close=104.0, ma_fast=99.0, ma_slow=100.0)
    #: Equal averages: neither long-biased nor bearish.
    LEVEL = IndexReading(close=100.5, ma_fast=100.0, ma_slow=100.0)

    def test_breadth_exactly_at_the_red_ceiling_is_red(self) -> None:
        """ "`pct_up_strong_1m` <= `red_max_pct_up` [2] -> RED"."""
        assert market_gate(_breadth(2.0), None, MARKET) is MarketGate.RED
        assert market_gate(_breadth(2.01), None, MARKET) is not MarketGate.RED

    def test_breadth_exactly_at_the_green_floor_is_green(self) -> None:
        """ "`pct_up_strong_1m` >= `green_min_pct_up` [5] ... -> GREEN"."""
        assert market_gate(_breadth(5.0), self.LONG, MARKET) is MarketGate.GREEN
        assert market_gate(_breadth(4.99), self.LONG, MARKET) is MarketGate.AMBER

    def test_an_empty_universe_is_red(self) -> None:
        assert market_gate(BreadthSnapshot(0, 99.0, 0.0, 0.0), None, MARKET) is MarketGate.RED

    def test_a_ten_day_below_the_twenty_is_red_whatever_breadth_says(self) -> None:
        """§8.3: "`bearish` (10-day below 20-day) -> RED" — the filter he says would have cut
        his 2022 loss by ~90%."""
        assert market_gate(_breadth(50.0), self.BEARISH, MARKET) is MarketGate.RED

    def test_level_averages_cannot_make_the_gate_green(self) -> None:
        """GREEN needs `long_bias`, which equal averages are not; RED needs `bearish`, which
        they are not either. AMBER."""
        assert market_gate(_breadth(50.0), self.LEVEL, MARKET) is MarketGate.AMBER

    def test_a_missing_index_is_not_a_bear_market(self) -> None:
        """§8.2: "none -> the index is ignored"."""
        assert market_gate(_breadth(50.0), None, MARKET) is MarketGate.GREEN

    def test_the_close_is_not_consulted(self) -> None:
        """A close under both averages with the 10-day over the 20-day is `long_bias`; a close
        over both with the 10-day under is `bearish`."""
        assert self.LONG.long_bias is True and self.LONG.bearish is False
        assert self.BEARISH.bearish is True and self.BEARISH.long_bias is False
        assert self.LEVEL.long_bias is False and self.LEVEL.bearish is False


class TestTheLadder:
    """§8.4. "the ladder never skips a rung", and it "falls down faster than it climbs"."""

    def _tier(self, *, level: int, results: list[str], gate: MarketGate) -> ExposureTier:
        return exposure_tier(
            current_level=level,
            closed_r_multiples=[Decimal(r) for r in results],
            gate=gate,
            config=MARKET,
        )

    def test_red_drops_to_the_bottom_and_forbids_entries(self) -> None:
        tier = self._tier(level=3, results=["1", "1", "1", "1", "1"], gate=MarketGate.RED)
        assert (tier.level, tier.new_entries_allowed) == (0, False)
        assert (tier.max_open_positions, tier.max_exposure_pct) == MARKET.tiers[0]

    def test_five_winners_in_a_green_tape_climb_one_rung(self) -> None:
        assert (
            self._tier(level=0, results=["1", "1", "1", "1", "1"], gate=MarketGate.GREEN).level == 1
        )

    def test_four_winners_are_not_enough(self) -> None:
        """ "GREEN with **>= 5** closed trades and net R > 0"."""
        assert self._tier(level=0, results=["1", "1", "1", "1"], gate=MarketGate.GREEN).level == 0

    def test_a_net_r_of_exactly_zero_does_not_climb(self) -> None:
        """ "net R **>** 0" - breaking even is not evidence that breakouts are working."""
        assert (
            self._tier(level=0, results=["1", "1", "1", "-1", "-2"], gate=MarketGate.GREEN).level
            == 0
        )

    def test_amber_holds_the_rung_and_still_allows_entries(self) -> None:
        """§8.4: "AMBER **entries are allowed at the current rung**"."""
        tier = self._tier(level=2, results=["1", "1", "1", "1", "1"], gate=MarketGate.AMBER)
        assert (tier.level, tier.new_entries_allowed) == (2, True)

    def test_three_losses_in_a_row_step_down(self) -> None:
        assert (
            self._tier(level=2, results=["1", "1", "-1", "-1", "-1"], gate=MarketGate.GREEN).level
            == 1
        )

    def test_two_losses_in_a_row_do_not(self) -> None:
        """Net R is negative here, so the rung neither climbs nor falls: two is not a streak."""
        assert (
            self._tier(level=2, results=["1", "-1", "1", "-1", "-1"], gate=MarketGate.GREEN).level
            == 2
        )

    def test_a_loss_streak_outranks_a_winning_five(self) -> None:
        """A book that has just lost three straight is not a book to press, whatever the sum."""
        assert (
            self._tier(level=2, results=["9", "9", "-1", "-1", "-1"], gate=MarketGate.GREEN).level
            == 1
        )

    def test_only_the_last_five_closes_are_read(self) -> None:
        """ "the last `lookback_trades` [5] closed trades" - a good March is not this week."""
        assert (
            self._tier(
                level=1,
                results=["5", "5", "5", "-1", "-1", "-1"],
                gate=MarketGate.GREEN,
            ).level
            == 0
        )

    def test_the_top_rung_does_not_overflow(self) -> None:
        top = len(MARKET.tiers) - 1
        assert self._tier(level=top, results=["1"] * 5, gate=MarketGate.GREEN).level == top

    def test_the_bottom_rung_does_not_underflow(self) -> None:
        assert self._tier(level=0, results=["-1"] * 5, gate=MarketGate.AMBER).level == 0

    def test_the_four_tiers_are_the_documented_ladder(self) -> None:
        """§8.4 (SW9.5): "(2, 25%), (4, 50%), (6, 75%), (10, 100%)". The top rung is his
        "typically 5-10 positions"; his 15-20 of a great market is the env ceiling, not a rung."""
        assert MARKET.tiers == ((2, 25.0), (4, 50.0), (6, 75.0), (10, 100.0))
        assert MARKET.tiers[-1][0] == DEFAULT_SWING_CONFIG.sizing.max_open_positions == 10


# ---------------------------------------------------------------------------
# §7 - the opening range and the live trigger
# ---------------------------------------------------------------------------


def _candle(minute: int, *, high: str = "105", low: str = "100") -> Candle:
    start = dt.datetime.combine(DAY, dt.time(9, 15)) + dt.timedelta(minutes=minute)
    return Candle(
        start=start,
        open=Decimal(low),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(high),
        volume=1000,
    )


class TestTheOpeningRange:
    """§7.1."""

    def test_the_window_is_half_open_at_its_end(self) -> None:
        """ "candles with `open` <= start < `open + window`" - the 09:20 candle is not in a
        five-minute range that opened at 09:15; it is the evidence the range has closed."""
        candles = [_candle(0, high="105"), _candle(4, high="110"), _candle(5, high="999")]
        opening = opening_range(candles, day=DAY, window_minutes=5, config=RANGE)
        assert opening.high == Decimal("110")
        assert opening.candles == 2
        assert opening.complete is True

    def test_a_range_is_not_a_range_until_the_window_has_closed(self) -> None:
        """ "`complete` only once a candle at or after the window's end exists (no clock)"."""
        opening = opening_range([_candle(0), _candle(4)], day=DAY, window_minutes=5, config=RANGE)
        assert opening.complete is False

    def test_candles_before_the_open_are_ignored(self) -> None:
        pre = Candle(
            start=dt.datetime.combine(DAY, dt.time(9, 10)),
            open=Decimal("50"),
            high=Decimal("50"),
            low=Decimal("50"),
            close=Decimal("50"),
            volume=1,
        )
        opening = opening_range(
            [pre, _candle(0), _candle(6)], day=DAY, window_minutes=5, config=RANGE
        )
        assert opening.low == Decimal("100")

    def test_no_candles_inside_the_window_is_an_empty_incomplete_range(self) -> None:
        opening = opening_range([_candle(30)], day=DAY, window_minutes=5, config=RANGE)
        assert (opening.candles, opening.complete) == (0, False)

    def test_only_the_documented_windows_are_accepted(self) -> None:
        """§7.1: "`windows_minutes` [(1, 5, 60)]". A 15-minute range has no code behind it."""
        with pytest.raises(ValueError, match="not one of"):
            opening_range([_candle(0)], day=DAY, window_minutes=15, config=RANGE)

    def test_the_three_documented_windows_are_one_five_and_sixty(self) -> None:
        assert RANGE.windows_minutes == (1, 5, 60)
        assert RANGE.default_window_minutes == 5


def _verdict(  # noqa: PLR0913 - one keyword per input the verdict depends on
    *,
    last: str,
    high: str = "105",
    low: str = "100",
    pivot: str | None = None,
    low_of_day: str = "99",
    circuit: str | None = None,
    at: dt.time = dt.time(9, 30),
    complete: bool = True,
) -> TriggerVerdict:
    return evaluate_trigger(
        last_price=Decimal(last),
        opening=OpeningRange(
            high=Decimal(high), low=Decimal(low), window_minutes=5, complete=complete, candles=2
        ),
        pivot_high=None if pivot is None else Decimal(pivot),
        low_of_day=Decimal(low_of_day),
        upper_circuit=None if circuit is None else Decimal(circuit),
        at=dt.datetime.combine(DAY, at),
        config=RANGE,
    )


class TestTheTriggerVerdict:
    """§7.2's precedence, top to bottom, each at its boundary."""

    def test_the_monitor_closes_after_the_documented_time(self) -> None:
        """ "after `monitor_close` [10:45] -> `SESSION_OVER`" - 10:45 itself is still open."""
        assert _verdict(last="120", at=dt.time(10, 45)).state is not TriggerState.SESSION_OVER
        assert _verdict(last="120", at=dt.time(10, 45, 1)).state is TriggerState.SESSION_OVER

    def test_an_incomplete_range_says_so(self) -> None:
        assert _verdict(last="120", complete=False).state is TriggerState.RANGE_INCOMPLETE

    def test_a_price_exactly_at_the_circuit_is_locked(self) -> None:
        """ "`last_price` >= `upper_circuit`" - at the band there is no seller, so no fill."""
        assert _verdict(last="120", circuit="120").state is TriggerState.LOCKED_UPPER_CIRCUIT
        assert _verdict(last="120", circuit="120.01").state is not TriggerState.LOCKED_UPPER_CIRCUIT

    def test_a_zero_circuit_is_no_circuit(self) -> None:
        assert _verdict(last="120", circuit="0").state is TriggerState.TRIGGERED

    def test_a_price_exactly_at_the_buffered_range_high_is_still_waiting(self) -> None:
        """ "`last_price` <= `range_high` x (1 + `break_buffer_pct` [0.1] / 100) -> WAITING".

        105 x 1.001 = 105.105, and a tick at exactly that level has not cleared the range.
        """
        assert _verdict(last="105.105", high="105").state is TriggerState.WAITING
        assert _verdict(last="105.106", high="105").state is TriggerState.TRIGGERED

    def test_a_flag_must_also_clear_its_daily_pivot(self) -> None:
        """ "FLAG with `last_price` <= `pivot_high` -> `BELOW_PIVOT`"."""
        assert _verdict(last="120", pivot="120").state is TriggerState.BELOW_PIVOT
        assert _verdict(last="120.01", pivot="120").state is TriggerState.TRIGGERED

    def test_an_ep_has_no_pivot_to_clear(self) -> None:
        """ "pass `pivot_high=None` for an EP, whose pivot is the gap itself"."""
        assert _verdict(last="106", pivot=None).state is TriggerState.TRIGGERED

    def test_the_trigger_carries_the_entry_and_the_lower_of_the_two_lows(self) -> None:
        """§7.2: "`entry = last_price`, `stop = min(range_low, low_of_day)`"."""
        verdict = _verdict(last="120", low="100", low_of_day="99")
        assert (verdict.entry, verdict.stop) == (Decimal("120"), Decimal("99"))
        assert _verdict(last="120", low="98", low_of_day="99").stop == Decimal("98")

    def test_every_verdict_reports_the_range_it_judged_against(self) -> None:
        """A `WAITING` with no numbers on it is a verdict nobody can check afterwards."""
        verdict = _verdict(last="100", high="105", low="100")
        assert (verdict.range_high, verdict.range_low) == (Decimal("105"), Decimal("100"))


class TestTheLiveGap:
    """§7.3."""

    def _gap(self, *, last: str, volume: int, minutes: int = 10) -> LiveGapVerdict:
        return live_gap(
            prev_close=Decimal("100"),
            last_price=Decimal(last),
            volume_so_far=volume,
            avg_daily_volume=Decimal("375000"),
            minutes_elapsed=minutes,
            config=RANGE,
        )

    def test_a_gap_exactly_at_the_floor_with_pace_exactly_at_the_floor_qualifies(self) -> None:
        """ "`gap` >= `live_min_gap_pct` [10] and `volume_pace` >= `live_min_volume_pace` [3.0]".

        375,000 shares a day over 375 minutes is 1,000 a minute; ten minutes at three times that
        pace is 30,000.
        """
        verdict = self._gap(last="110", volume=30_000)
        assert verdict.is_candidate is True
        assert verdict.gap_pct == Decimal("10.00")
        assert verdict.volume_pace == Decimal("3.00")

    def test_a_hair_under_either_floor_does_not(self) -> None:
        assert self._gap(last="109.99", volume=30_000).is_candidate is False
        assert self._gap(last="110", volume=29_999).is_candidate is False

    def test_the_session_is_three_hundred_and_seventy_five_minutes(self) -> None:
        """09:15-15:30. The pace is pro-rated against it, so the constant is load-bearing."""
        assert SESSION_MINUTES == 375

    def test_a_missing_previous_close_is_not_a_candidate_rather_than_a_crash(self) -> None:
        assert (
            live_gap(
                prev_close=Decimal("0"),
                last_price=Decimal("110"),
                volume_so_far=1,
                avg_daily_volume=Decimal("1"),
                minutes_elapsed=1,
                config=RANGE,
            ).is_candidate
            is False
        )

    def test_zero_minutes_elapsed_is_not_a_candidate_either(self) -> None:
        """At 09:15:00 exactly, nothing has traded and the pace is a division by zero."""
        assert self._gap(last="110", volume=30_000, minutes=0).is_candidate is False


# ---------------------------------------------------------------------------
# §9 - the plan
# ---------------------------------------------------------------------------


def _watch(  # noqa: PLR0913 - one keyword per field of the watch row being described
    symbol: str = "AAA",
    *,
    score: str = "80",
    trigger: str = "100",
    stop: str = "96",
    setup: Setup = Setup.FLAG,
    locked: bool = False,
) -> WatchItem:
    return WatchItem(
        symbol=symbol,
        setup=setup,
        trigger=Decimal(trigger),
        stop_ref=Decimal(stop),
        adr_pct=Decimal("5"),
        avg_turnover_inr=Decimal("100000000"),
        score=Decimal(score),
        locked_upper_circuit=locked,
    )


def _account(
    *,
    equity: str = "1000000",
    cash: str = "1000000",
    held: frozenset[str] = frozenset(),
    exposure: str = "0",
) -> SwingAccount:
    return SwingAccount(
        equity=Decimal(equity),
        cash_available=Decimal(cash),
        open_symbols=held,
        open_exposure_inr=Decimal(exposure),
    )


def _tier(level: int = 3) -> ExposureTier:
    positions, exposure = MARKET.tiers[level]
    return ExposureTier(level, positions, exposure, new_entries_allowed=True)


class TestTheTickSnap:
    """§9.1: "`trigger` and `stop` snapped to Rs 0.05"."""

    @pytest.mark.parametrize(
        ("raw", "snapped"),
        [("100.02", "100.00"), ("100.03", "100.05"), ("100.07", "100.05"), ("100.08", "100.10")],
    )
    def test_prices_land_on_the_exchange_grid(self, raw: str, snapped: str) -> None:
        assert to_tick(Decimal(raw)) == Decimal(snapped)

    def test_a_price_already_on_the_grid_does_not_move(self) -> None:
        assert to_tick(Decimal("100.05")) == Decimal("100.05")


class TestTheSkipOrder:
    """§9.1's refusals, "in order". The order is the rule: a locked name that is also already
    held must report the reason that came first, or the page teaches the wrong lesson."""

    def test_a_parabolic_short_is_never_a_line(self) -> None:
        lines, skipped = build_entries(
            as_of=DAY,
            watch=[_watch(setup=Setup.PARABOLIC_SHORT)],
            account=_account(),
            gate=MarketGate.GREEN,
            tier=_tier(),
            config=DEFAULT_SWING_CONFIG,
        )
        assert lines == []
        assert [s.reason for s in skipped] == [SkipReason.NOT_TRADEABLE_SETUP]

    def test_a_red_gate_skips_everything_with_its_own_reason(self) -> None:
        lines, skipped = build_entries(
            as_of=DAY,
            watch=[_watch()],
            account=_account(),
            gate=MarketGate.RED,
            tier=_tier(),
            config=DEFAULT_SWING_CONFIG,
        )
        assert lines == []
        assert [s.reason for s in skipped] == [SkipReason.GATE_RED]

    def test_a_tier_that_forbids_entries_skips_them_too(self) -> None:
        tier = ExposureTier(0, 2, 25.0, new_entries_allowed=False)
        _, skipped = build_entries(
            as_of=DAY,
            watch=[_watch()],
            account=_account(),
            gate=MarketGate.GREEN,
            tier=tier,
            config=DEFAULT_SWING_CONFIG,
        )
        assert [s.reason for s in skipped] == [SkipReason.GATE_RED]

    def test_a_name_already_held_is_never_averaged_into(self) -> None:
        """§6.5: "Never averaged down: `ALREADY_HELD` skips"."""
        _, skipped = build_entries(
            as_of=DAY,
            watch=[_watch("AAA")],
            account=_account(held=frozenset({"AAA"})),
            gate=MarketGate.GREEN,
            tier=_tier(),
            config=DEFAULT_SWING_CONFIG,
        )
        assert [s.reason for s in skipped] == [SkipReason.ALREADY_HELD]

    def test_a_locked_name_is_skipped_with_its_own_reason(self) -> None:
        _, skipped = build_entries(
            as_of=DAY,
            watch=[_watch(locked=True)],
            account=_account(),
            gate=MarketGate.GREEN,
            tier=_tier(),
            config=DEFAULT_SWING_CONFIG,
        )
        assert [s.reason for s in skipped] == [SkipReason.LOCKED_UPPER_CIRCUIT]

    def test_the_tier_caps_how_many_lines_a_plan_can_have(self) -> None:
        watch = [_watch(f"S{i}", score=str(90 - i)) for i in range(5)]
        lines, skipped = build_entries(
            as_of=DAY,
            watch=watch,
            account=_account(),
            gate=MarketGate.GREEN,
            tier=_tier(0),
            config=DEFAULT_SWING_CONFIG,
        )
        assert len(lines) == 2
        assert [s.reason for s in skipped] == [SkipReason.TIER_FULL] * 3

    def test_positions_already_open_count_against_the_tier(self) -> None:
        lines, skipped = build_entries(
            as_of=DAY,
            watch=[_watch("AAA"), _watch("BBB")],
            account=_account(held=frozenset({"ZZZ", "YYY"})),
            gate=MarketGate.GREEN,
            tier=_tier(0),
            config=DEFAULT_SWING_CONFIG,
        )
        assert lines == []
        assert [s.reason for s in skipped] == [SkipReason.TIER_FULL] * 2

    def test_the_exposure_ceiling_is_a_fraction_of_the_sleeve(self) -> None:
        lines, skipped = build_entries(
            as_of=DAY,
            watch=[_watch("AAA")],
            account=_account(equity="1000000", exposure="240000"),
            gate=MarketGate.GREEN,
            tier=_tier(0),
            config=DEFAULT_SWING_CONFIG,
        )
        assert lines == []
        assert [s.reason for s in skipped] == [SkipReason.EXPOSURE_FULL]

    def test_a_size_refusal_names_the_refusal(self) -> None:
        _, skipped = build_entries(
            as_of=DAY,
            watch=[_watch(trigger="100", stop="80")],
            account=_account(),
            gate=MarketGate.GREEN,
            tier=_tier(),
            config=DEFAULT_SWING_CONFIG,
        )
        assert [(s.reason, s.detail) for s in skipped] == [
            (SkipReason.SIZE_REFUSED, SizeRefusal.STOP_TOO_WIDE.value)
        ]


class TestThePlanArithmetic:
    def test_lines_are_ordered_by_score_then_symbol(self) -> None:
        """§9.1: "sorted by `(-score, symbol)`" - the best setup gets the cash first."""
        watch = [_watch("BBB", score="80"), _watch("AAA", score="80"), _watch("CCC", score="90")]
        lines, _ = build_entries(
            as_of=DAY,
            watch=watch,
            account=_account(),
            gate=MarketGate.GREEN,
            tier=_tier(),
            config=DEFAULT_SWING_CONFIG,
        )
        assert [line.symbol for line in lines] == ["CCC", "AAA", "BBB"]

    def test_cash_spent_by_an_earlier_line_is_not_spent_twice(self) -> None:
        """§9.1: "Cash spent by earlier lines is not spent twice"."""
        watch = [_watch("AAA", score="90"), _watch("BBB", score="80")]
        lines, skipped = build_entries(
            as_of=DAY,
            watch=watch,
            account=_account(equity="1000000", cash="130000"),
            gate=MarketGate.GREEN,
            tier=_tier(),
            config=DEFAULT_SWING_CONFIG,
        )
        assert [line.symbol for line in lines] == ["AAA"]
        assert [s.reason for s in skipped] == [SkipReason.SIZE_REFUSED]

    def test_a_line_carries_the_trail_the_stop_rules_will_use(self) -> None:
        lines, _ = build_entries(
            as_of=DAY,
            watch=[_watch()],
            account=_account(),
            gate=MarketGate.GREEN,
            tier=_tier(),
            config=DEFAULT_SWING_CONFIG,
        )
        assert lines[0].kind is LineKind.BUY_ON_TRIGGER
        assert lines[0].trail is TrailMa.MA20  # ADR 5% is under the 6% fast-trail threshold


# ---------------------------------------------------------------------------
# §10 - the journal
# ---------------------------------------------------------------------------


def _trade(
    *, entry: str = "100", stop: str = "96", exit_avg: str = "112", quantity: int = 100
) -> ClosedTrade:
    return ClosedTrade(
        symbol="TESTCO",
        setup="FLAG",
        entry_date=DAY,
        exit_date=DAY + dt.timedelta(days=7),
        entry=Decimal(entry),
        initial_stop=Decimal(stop),
        exit_avg=Decimal(exit_avg),
        quantity=quantity,
    )


class TestTheClosedTrade:
    def test_r_is_measured_against_the_initial_stop_not_the_trailing_one(self) -> None:
        """§10: "(exit_avg - entry) / (entry - initial_stop)". The risk taken was the first one."""
        assert _trade().r_multiple == Decimal("3.00")

    def test_pnl_is_rupees_and_r_is_not(self) -> None:
        assert _trade(quantity=100).pnl_inr == Decimal("1200.00")
        assert _trade(quantity=1).pnl_inr == Decimal("12.00")
        assert _trade(quantity=1).r_multiple == _trade(quantity=100).r_multiple

    def test_a_trade_whose_stop_was_not_below_its_entry_has_no_r(self) -> None:
        with pytest.raises(ValueError, match="not below entry"):
            _ = _trade(entry="100", stop="100").r_multiple


class TestSummarize:
    """§10's ten statistics."""

    def setup_method(self) -> None:
        self.trades = [
            _trade(exit_avg="112"),  # +3R
            _trade(exit_avg="96"),  # -1R
            _trade(exit_avg="104"),  # +1R
            _trade(exit_avg="96"),  # -1R
            _trade(exit_avg="96"),  # -1R
        ]

    def test_the_r_values_are_what_the_test_thinks(self) -> None:
        assert [t.r_multiple for t in self.trades] == [
            Decimal("3.00"),
            Decimal("-1.00"),
            Decimal("1.00"),
            Decimal("-1.00"),
            Decimal("-1.00"),
        ]

    def test_the_statistics(self) -> None:
        stats = summarize(self.trades)
        assert stats.trades == 5
        assert stats.win_rate_pct == Decimal("40.00")
        assert stats.avg_win_r == Decimal("2.00")
        assert stats.avg_loss_r == Decimal("-1.00")
        assert stats.expectancy_r == Decimal("0.20")
        assert stats.profit_factor == Decimal("1.33")
        assert stats.net_r == Decimal("1.00")
        assert stats.largest_win_r == Decimal("3.00")
        assert stats.largest_loss_r == Decimal("-1.00")
        assert stats.current_loss_streak == 2

    def test_a_breakeven_trade_is_not_a_win(self) -> None:
        """ "win rate" counts `r > 0`; a scratch is not a win, and calling it one flatters the
        one statistic the method's 25-35% win rate makes people nervous about."""
        stats = summarize([_trade(exit_avg="100")])
        assert stats.win_rate_pct == Decimal("0.00")
        assert stats.avg_loss_r == Decimal("0.00")

    def test_a_book_with_no_losses_has_no_profit_factor(self) -> None:
        """A division by zero would be infinity; `None` is the honest answer."""
        assert summarize([_trade(exit_avg="112")]).profit_factor is None

    def test_an_empty_journal_is_zeroes_rather_than_an_error(self) -> None:
        """§10: "Empty -> zeros, not an error"."""
        stats = summarize([])
        assert (stats.trades, stats.net_r, stats.current_loss_streak) == (0, Decimal(0), 0)

    def test_the_loss_streak_counts_back_from_the_most_recent_close(self) -> None:
        assert summarize(self.trades[:2]).current_loss_streak == 1
        assert summarize(self.trades[:1]).current_loss_streak == 0


class TestExitAverage:
    """§10: "`exit_avg` share-weighted over fills"."""

    def test_two_fills_are_weighted_by_quantity(self) -> None:
        assert exit_average([(100, Decimal("110")), (200, Decimal("101"))]) == Decimal("104.00")

    def test_one_fill_is_its_own_average(self) -> None:
        assert exit_average([(50, Decimal("107.5"))]) == Decimal("107.50")

    def test_no_quantity_is_an_error_rather_than_a_zero(self) -> None:
        with pytest.raises(ValueError, match="no exit quantity"):
            exit_average([])


# ---------------------------------------------------------------------------
# SW12 - the survivors of the mutation re-run, each a rule `04` states
# ---------------------------------------------------------------------------


class TestTheRulesTheMutationRunFoundUnstated:
    def test_a_zero_quantity_always_carries_a_refusal_even_with_no_minimum_trade_value(
        self,
    ) -> None:
        """§5: "`quantity == 0` always carries a `refusal`". A sleeve too small to buy one share
        at the risk budget is refused, not handed a zero-share position, even when the minimum
        trade value is switched off."""
        sized = size_position(
            equity=Decimal("1000"),
            cash_available=Decimal("1000"),
            entry=Decimal("500"),
            stop=Decimal("480"),
            avg_turnover_inr=None,
            config=replace(SizingConfig(), min_trade_value_inr=0.0),
            max_stop_distance_pct=Decimal("10"),
        )
        assert sized.quantity == 0
        assert sized.refusal is SizeRefusal.BELOW_MIN_TRADE_VALUE

    def test_the_widest_stop_scales_with_the_adr_multiple(self) -> None:
        """§6: `widest = min(max_stop_distance_pct, adr_pct x max_stop_adr_multiple)` - half an
        ADR on an 8% name is 4%; one and a half is capped at the absolute 10."""
        half = replace(StopConfig(), max_stop_adr_multiple=0.5)
        wide = replace(StopConfig(), max_stop_adr_multiple=1.5)
        assert widest_stop_pct(Decimal("8"), half) == Decimal("4.00")
        assert widest_stop_pct(Decimal("8"), wide) == Decimal("10.00")

    def test_a_position_is_not_an_ep_gap_day_position_unless_said_so(self) -> None:
        """§6.3's failed-EP rule is for an EP on its gap day only. A position built without the
        flag (a flag bought at the pivot) that closes red on its entry day is held, not sold."""
        position = OpenPosition(
            symbol="FLAGCO",
            entry_date=dt.date(2026, 9, 1),
            entry=Decimal("100"),
            initial_stop=Decimal("96"),
            stop=Decimal("96"),
            quantity=300,
            partial_done=False,
            trail=TrailMa.MA20,
        )
        assert position.is_ep_gap_day is False
        bar = DailyBar(
            date=dt.date(2026, 9, 1),
            open=Decimal("101"),
            high=Decimal("102"),
            low=Decimal("97"),
            close=Decimal("98"),
            ma10=Decimal("90"),
            ma20=Decimal("88"),
            bars_since_entry=0,
        )
        assert [a.kind for a in manage(position, bar, StopConfig())] == [ActionKind.HOLD]

    def test_the_lockout_releases_once_the_drawdown_is_back_inside_the_resume_line(
        self,
    ) -> None:
        """§8.5: "stays locked until the drawdown is back inside `resume_drawdown_pct` [10]" -
        at 10.00 exactly the sleeve is inside it, and released."""
        assert drawdown_locked(drawdown_pct=10.0, was_locked=True, config=MARKET) is False
        assert drawdown_locked(drawdown_pct=10.01, was_locked=True, config=MARKET) is True


class TestTheLiveGapScore:
    """§7.3: `live_gap_score = 35 x clamp(gap / 20) + 35 x clamp(volume_pace / 6)`, out of 70."""

    def test_the_two_terms_are_the_formula(self) -> None:
        verdict = LiveGapVerdict(True, Decimal("13.00"), Decimal("5.00"))
        expected = Decimal(35) * Decimal(13) / Decimal(20) + Decimal(35) * Decimal(5) / Decimal(6)
        assert live_gap_score(verdict, EpConfig()) == expected.quantize(Decimal("0.01"))
        assert live_gap_score(verdict, EpConfig()) == Decimal("51.92")

    def test_full_marks_at_twice_each_threshold_and_never_more(self) -> None:
        at_twice = LiveGapVerdict(True, Decimal("20.00"), Decimal("6.00"))
        far_beyond = LiveGapVerdict(True, Decimal("60.00"), Decimal("18.00"))
        assert live_gap_score(at_twice, EpConfig()) == Decimal("70.00")
        assert live_gap_score(far_beyond, EpConfig()) == Decimal("70.00")

    def test_the_denominators_are_the_ep_thresholds_not_literals(self) -> None:
        loose = EpConfig(min_gap_pct=5.0, min_rvol=1.0)
        both = LiveGapVerdict(True, Decimal("10.00"), Decimal("2.00"))
        half = LiveGapVerdict(True, Decimal("5.00"), Decimal("1.00"))
        assert live_gap_score(both, loose) == Decimal("70.00")
        assert live_gap_score(half, loose) == Decimal("35.00")


class TestTheLiveGapGuards:
    """§7.3 divides by `prev_close` and by the pro-rated average volume; a name with neither
    cannot gap, and the verdict says zero rather than raising or reporting half a number."""

    def test_no_previous_close_is_no_gap_and_no_numbers(self) -> None:
        verdict = live_gap(
            prev_close=Decimal("0"),
            last_price=Decimal("113"),
            volume_so_far=1,
            avg_daily_volume=Decimal("1000"),
            minutes_elapsed=10,
            config=RANGE,
        )
        assert verdict == LiveGapVerdict(False, Decimal("0"), Decimal("0"))

    def test_no_average_volume_is_no_pace_and_no_gap_reported(self) -> None:
        verdict = live_gap(
            prev_close=Decimal("100"),
            last_price=Decimal("113"),
            volume_so_far=1,
            avg_daily_volume=Decimal("0"),
            minutes_elapsed=10,
            config=RANGE,
        )
        assert verdict == LiveGapVerdict(False, Decimal("0"), Decimal("0"))

    def test_no_minute_elapsed_is_no_pace_and_no_gap_reported(self) -> None:
        verdict = live_gap(
            prev_close=Decimal("100"),
            last_price=Decimal("113"),
            volume_so_far=1,
            avg_daily_volume=Decimal("1000"),
            minutes_elapsed=0,
            config=RANGE,
        )
        assert verdict == LiveGapVerdict(False, Decimal("0"), Decimal("0"))
