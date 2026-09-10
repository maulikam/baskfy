"""Chartink's five lines and the six filters that make them a strategy (``docs/vbt/04`` §3).

Each test moves exactly one number across exactly one threshold, so a failure names the rule. The
comparison senses are asserted on purpose: ``>`` and ``>=`` are Chartink's own, and a rule read
one tick loose is a rule that fires on days it should not.

The fixture is built rather than perturbed: a long rise into a twenty-session shelf, then one
breakout bar. That is the shape the strategy is looking for — an established uptrend, a
controlled base, and a high-volume day that clears it — so the baseline passes all eleven rules
and every test below breaks exactly one.
"""

from __future__ import annotations

import polars as pl
import pytest
from vbt_fixtures import sessions, with_background

from baskfy_core.vbt.calendar import build_calendar
from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG, SignalState, TrendFilter, VbtConfig
from baskfy_core.vbt.indicators import with_vbt_indicators
from baskfy_core.vbt.signals import detect_signals, signal_mask, with_signal_columns

COUNT = 260
DAYS = sessions(COUNT)
SHELF_FROM = 220
#: The signal session: the last of the frame. Late enough that the 200-day average, the 50-day
#: volume average and the 21-day EMA all exist.
WHEN = DAYS[COUNT - 1]
#: Where a ``shelf_high`` is planted — inside the twenty sessions filter B looks back over.
SHELF_HIGH_AT = COUNT - 11


def bars(  # noqa: PLR0913 - one keyword per rule a test might want to break
    *,
    close: float = 96.0,
    high: float | None = None,
    low: float | None = None,
    volume: float = 1_000_000.0,
    close_raw: float | None = None,
    upper_circuit: float | None = None,
    shelf_close: float = 90.0,
    shelf_high: float | None = None,
    close_20_ago: float | None = None,
    base_volume: float = 250_000.0,
    base_start: float = 20.0,
    base_step: float = 0.3,
) -> pl.DataFrame:
    """One instrument: a rise, a shelf, and a breakout bar on the last session of the frame."""
    closes = [base_start + base_step * i for i in range(SHELF_FROM)]
    closes += [shelf_close] * (COUNT - 1 - SHELF_FROM)
    closes.append(close)
    if close_20_ago is not None:
        closes[COUNT - 1 - 20] = close_20_ago
    highs = list(closes)
    lows = [value * 0.99 for value in closes]
    if shelf_high is not None:
        highs[SHELF_HIGH_AT] = shelf_high
    highs[-1] = high if high is not None else close
    lows[-1] = low if low is not None else shelf_close
    volumes = [base_volume] * (COUNT - 1) + [volume]
    raws = list(closes)
    raws[-1] = close_raw if close_raw is not None else close
    return pl.DataFrame(
        {
            "instrument_id": [1] * COUNT,
            "symbol": ["VBTCO"] * COUNT,
            "date": DAYS,
            "open": lows,
            "high": highs,
            "low": lows,
            "close": closes,
            "close_raw": raws,
            "volume": volumes,
            "upper_circuit": [None] * (COUNT - 1) + [upper_circuit],
            "adj_factor": [1.0] * COUNT,
        },
        schema_overrides={"upper_circuit": pl.Float64, "instrument_id": pl.Int64},
    )


def number(row: dict[str, object], column: str) -> float:
    """One numeric cell of an indicated row.

    A frame cell is ``object`` to the type checker and a float in fact; naming that here keeps
    every comparison below readable and keeps the ``type: ignore`` escape hatch out of the
    tree (house rule 3; the hash is left off deliberately, as the house convention asks).
    """
    value = row[column]
    assert isinstance(value, (int, float)), f"{column} is {value!r}"
    return float(value)


def evaluate(frame: pl.DataFrame, config: VbtConfig = DEFAULT_VBT_CONFIG) -> dict[str, object]:
    full = with_background(frame, count=COUNT)
    indicated = with_vbt_indicators(full, build_calendar(full), config)
    return (
        with_signal_columns(indicated, config)
        .filter((pl.col("date") == WHEN) & (pl.col("instrument_id") == 1))
        .to_dicts()[0]
    )


def test_the_baseline_bar_is_a_signal() -> None:
    row = evaluate(bars())
    assert row["scan_hit"] is True
    assert row["state"] == SignalState.SIGNAL.value
    assert row["failed_filters"] == []


class TestTheFiveChartinkLines:
    def test_volume_must_beat_three_times_its_fifty_day_average(self) -> None:
        assert evaluate(bars(volume=1_000_000.0))["scan_hit"] is True
        assert evaluate(bars(volume=700_000.0))["scan_hit"] is False

    def test_the_price_floor_reads_the_exchange_print(self) -> None:
        """`04` §3.1 line 2: a ₹28 name that a 1:2 split makes ₹56 in the adjusted series did
        not clear Chartink's ``Close > 30``, and the rule is about what the exchange printed."""
        assert evaluate(bars(close_raw=29.0))["scan_hit"] is False
        assert evaluate(bars(close_raw=31.0))["scan_hit"] is True

    def test_the_change_floor_admits_a_bar_just_over_it_and_refuses_one_just_under(self) -> None:
        """Line 3 is ``>=``. A bar *exactly* on 6.5% is a floating-point boundary and is
        deliberately not pinned: ``(95.85 / 90 - 1) x 100`` is 6.499999999999995 in IEEE 754,
        which the research's own arithmetic also produces, so the two agree about the edge case
        whichever way it falls. What is pinned is the sense of the comparison."""
        over, under = evaluate(bars(close=95.95)), evaluate(bars(close=95.75))
        assert number(over, "change_pct") > 6.5
        assert over["scan_hit"] is True
        assert number(under, "change_pct") < 6.5
        assert under["scan_hit"] is False

    def test_a_name_whose_book_is_too_thin_fails_the_volume_average_floor(self) -> None:
        row = evaluate(bars(base_volume=1_000.0, volume=60_000.0))
        assert number(row, "vol_sma") < DEFAULT_VBT_CONFIG.scan.min_vol_sma
        assert row["scan_hit"] is False

    def test_the_absolute_volume_floor_can_never_bind_alone(self) -> None:
        """Line 5 is implied by lines 1 and 4: ``volume > 3 x vol_sma`` and ``vol_sma >= 25,000``
        put the volume above 75,000 before line 5 is consulted. It stays in the config because it
        is one of Chartink's five and this pack reproduces the scan as given, not as tidied."""
        scan = DEFAULT_VBT_CONFIG.scan
        assert scan.min_vol_sma * scan.vol_mult > scan.min_volume


class TestTheSixTrendFilters:
    def test_a_name_below_its_two_hundred_day_average_fails_a(self) -> None:
        """Filter A. Below it the same bar loses money in both halves of the history."""
        row = evaluate(bars(base_start=200.0, base_step=-0.5))
        assert row["scan_hit"] is True
        assert number(row, "close") < number(row, "sma_dma")
        assert row["state"] == SignalState.SCAN_ONLY.value
        assert row["failed_filters"] == [TrendFilter.A_ABOVE_200DMA.value]

    def test_a_bar_under_the_prior_twenty_day_high_fails_b(self) -> None:
        """Filter B — the one the book can least do without: removing it costs 9.2 CAGR points
        and takes the drawdown from -27.9% to -37.2%."""
        row = evaluate(bars(shelf_high=140.0))
        assert row["state"] == SignalState.SCAN_ONLY.value
        assert row["failed_filters"] == [TrendFilter.B_TWENTY_DAY_HIGH.value]

    def test_an_already_extended_name_fails_c(self) -> None:
        """Filter C: 96 against 70 twenty sessions ago is +37%, and the move has already been
        made. The day's own change is still only 6.7%, so this is C and nothing else."""
        row = evaluate(bars(close_20_ago=70.0))
        assert number(row, "ret_lookback_pct") > DEFAULT_VBT_CONFIG.trend.max_ret_20_pct
        assert number(row, "change_pct") < DEFAULT_VBT_CONFIG.trend.max_change_pct
        assert row["failed_filters"] == [TrendFilter.C_NOT_EXTENDED.value]

    def test_a_weak_close_fails_d(self) -> None:
        """Filter D: the close must sit in the top 40% of the day's range."""
        row = evaluate(bars(high=126.0, low=90.0))
        assert number(row, "close_position") == pytest.approx(0.1666, abs=1e-3)
        assert row["failed_filters"] == [TrendFilter.D_STRONG_CLOSE.value]

    def test_a_parabolic_print_fails_e(self) -> None:
        """Filter E excludes the circuit plays: 90 → 110 is +22% and is not this strategy's bar."""
        row = evaluate(bars(close=110.0))
        assert row["failed_filters"] == [TrendFilter.E_CHANGE_CEILING.value]

    def test_a_thin_book_fails_f(self) -> None:
        """Filter F: the thin tail is where the losses live, and it is the tail a ₹10-lakh book
        cannot exit. ₹0.9 crore of average turnover is not ₹2 crore."""
        row = evaluate(bars(base_volume=100_000.0, volume=400_000.0))
        assert number(row, "turnover_avg") < DEFAULT_VBT_CONFIG.trend.min_turnover_avg_inr
        assert row["failed_filters"] == [TrendFilter.F_TURNOVER_FLOOR.value]

    def test_the_baseline_book_clears_f(self) -> None:
        row = evaluate(bars())
        assert number(row, "turnover_avg") >= DEFAULT_VBT_CONFIG.trend.min_turnover_avg_inr


class TestANullIsAFail:
    def test_a_name_without_two_hundred_bars_is_not_a_signal(self) -> None:
        """Polars' three-valued logic would otherwise let a null through a negated comparison,
        and a name that listed last month would clear a trend filter it has no history for."""
        short = bars().tail(60)
        full = with_background(short, count=60, start=DAYS[200])
        indicated = with_vbt_indicators(full, build_calendar(full))
        row = (
            with_signal_columns(indicated)
            .filter((pl.col("date") == WHEN) & (pl.col("instrument_id") == 1))
            .to_dicts()[0]
        )
        assert row["sma_dma"] is None
        assert row["state"] == SignalState.SCAN_ONLY.value
        assert TrendFilter.A_ABOVE_200DMA.value in row["failed_filters"]


class TestTheCircuitFlagAndTheRank:
    def test_a_locked_bar_is_kept_and_flagged(self) -> None:
        """`04` §3.3 — kept and flagged, never dropped; the plan is what skips it, and the page
        can then say why a name it can see is a name it will not buy."""
        row = evaluate(bars(upper_circuit=96.0))
        assert row["locked_upper_circuit"] is True
        assert row["state"] == SignalState.SIGNAL.value

    def test_signals_rank_by_the_signal_day_turnover(self) -> None:
        """`04` §3.4 — a rule, not a tiebreak convenience: ranking by day-change costs 6 CAGR
        points and not ranking at all costs 4.6."""
        big = bars(volume=2_000_000.0).with_columns(
            pl.lit(2, dtype=pl.Int64).alias("instrument_id"), pl.lit("BIGCO").alias("symbol")
        )
        full = with_background(pl.concat([bars(), big]), count=COUNT)
        indicated = with_vbt_indicators(full, build_calendar(full))
        found = detect_signals(indicated, WHEN)
        ranked = [row["symbol"] for row in found.to_dicts() if row["instrument_id"] in (1, 2)]
        assert ranked[0] == "BIGCO"
        assert found["rank_key"].to_list() == sorted(found["rank_key"].to_list(), reverse=True)


class TestTheMask:
    def test_the_mask_is_true_only_for_full_signals(self) -> None:
        full = with_background(bars(), count=COUNT)
        indicated = with_vbt_indicators(full, build_calendar(full))
        mask = signal_mask(indicated)
        assert mask.sum() == 1
        assert indicated.filter(mask)["date"].to_list() == [WHEN]

    def test_a_scan_only_row_is_not_in_the_mask(self) -> None:
        full = with_background(bars(shelf_high=140.0), count=COUNT)
        indicated = with_vbt_indicators(full, build_calendar(full))
        assert signal_mask(indicated).sum() == 0
        assert detect_signals(indicated, WHEN).height >= 1

    def test_as_of_restricts_the_answer_to_one_session(self) -> None:
        full = with_background(bars(), count=COUNT)
        indicated = with_vbt_indicators(full, build_calendar(full))
        assert set(detect_signals(indicated, WHEN)["date"].to_list()) == {WHEN}
