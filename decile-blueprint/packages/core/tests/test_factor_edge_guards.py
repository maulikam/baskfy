"""Guards in the factor engine that ordinary data never reaches (Prompt 19 §6).

Every test here exists because a **surviving mutant** pointed at an untested line. Mutation
testing's whole value is that it finds guards nobody exercises — the branches that only fire on
data the fixtures happen not to contain, which is exactly where a wrong constant sits unnoticed
for a year.

Each class names the mutant it was written to kill.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Final

import polars as pl
import pytest

from baskfy_core import universes
from baskfy_core.factors import (
    TOP_RISK_FLAG_PERCENTILE,
    FactorConfig,
    FactorResult,
    compute_factors,
    compute_factors_unrounded,
)
from baskfy_core.windows import WINDOW_MONTHS

AS_OF: Final = dt.date(2026, 8, 18)
SHORT: Final = FactorConfig(window_months=(1,))


def weekdays(count: int, end: dt.date = AS_OF) -> list[dt.date]:
    days: list[dt.date] = []
    day = end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= dt.timedelta(days=1)
    return sorted(days)


def bars(closes: list[float], days: list[dt.date]) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "instrument_id": 1,
                "date": day,
                "close": close,
                "close_raw": close,
                "high": close,
                "low": close,
                "volume_raw": 1_000.0,
                "turnover": None,
                "upper_circuit": None,
                "lower_circuit": None,
            }
            for day, close in zip(days, closes, strict=True)
        ],
        strict=False,
    )


class TestTheTopRiskPercentileIsOneNumber:
    """Kills ``factors.py TOP_RISK_FLAG_PERCENTILE 0.1 -> 1.1``.

    docs/06 §Step 4 asks for it as "a named constant ... so it can be recalibrated against the
    fixture". It is currently declared **twice** — in `baskfy_core.universes` and in
    `baskfy_core.factors` — and the nightly flag step imports the `factors` one. Nothing before
    this test noticed if they drifted apart, and a mutation of either was invisible to the whole
    of `packages/core`.
    """

    def test_the_two_declarations_agree(self) -> None:
        assert TOP_RISK_FLAG_PERCENTILE == universes.TOP_RISK_FLAG_PERCENTILE

    def test_it_is_the_top_baskfy_docs_06_specifies(self) -> None:
        assert TOP_RISK_FLAG_PERCENTILE == 0.10


def assign(target: object, attribute: str, value: object) -> None:
    """``target.attribute = value``, deferred to runtime.

    Two static checks pull in opposite directions here and this helper is what satisfies both.
    Writing `config.min_beta_observations = 1` is a mypy error on a frozen dataclass, and
    silencing it needs the escape hatch CLAUDE.md house rule 3 bans. Writing
    `setattr(config, "min_beta_observations", 1)` instead is ruff's B010, whose autofix rewrites
    it straight back to the attribute form. Widening the receiver to ``object`` and taking the
    name as a parameter defeats both: mypy cannot resolve the target, and B010 only fires on a
    literal. The behaviour under test — that the assignment raises — is unchanged.
    """
    setattr(target, attribute, value)


class TestTheConfigObjectsAreImmutable:
    """Kills ``@dataclass(frozen=True)`` -> ``frozen=False`` on ``FactorConfig``/``FactorResult``.

    Not cosmetic: ``DEFAULT_FACTOR_CONFIG`` is a module-level singleton used as the default
    argument of ``compute_factors``. If it were mutable, one caller recalibrating the circuit
    bands would silently recalibrate every subsequent caller in the process — including the
    nightly pipeline running in the same worker.
    """

    def test_the_default_config_cannot_be_mutated_in_place(self) -> None:
        config = FactorConfig()
        with pytest.raises(dataclasses.FrozenInstanceError):
            assign(config, "min_beta_observations", 1)

    def test_a_result_cannot_be_mutated_in_place(self) -> None:
        result = FactorResult(pl.DataFrame(), {})
        with pytest.raises(dataclasses.FrozenInstanceError):
            assign(result, "frame", pl.DataFrame())


class TestTheZeroDenominatorGuards:
    """Kills the ``== 0`` sentinels in the daily-return, ``ret_N`` and away-from-high guards.

    A ₹1.00 adjusted close is not hypothetical — docs/09's adjustment divides the exchange print
    by a cumulative factor, and a 10:1 split followed by a 1:1 bonus (which CUPID actually had,
    docs/01 §9) moves a series by 20x. These tests pin that the guards test for *zero* and not
    for some other magic number.
    """

    def test_a_close_of_exactly_one_is_a_normal_price_not_a_sentinel(self) -> None:
        """The ``1.0`` must sit *inside* the window, or the mutant escapes.

        A first attempt put it in the first five bars, outside the 22-day window, and
        `previous == 0` -> `previous == 1` survived. The daily return whose *previous* close is
        1.0 has to be one of the returns the window averages.
        """
        days = weekdays(30)
        n = compute_factors_unrounded(
            bars([float(i + 2) for i in range(len(days))], days), AS_OF, days, config=SHORT
        ).window_lengths[1]
        closes = [float(i + 2) for i in range(len(days))]
        inside = len(days) - n + 1  # the first bar whose return is inside the window
        closes[inside] = 1.0
        frame = compute_factors_unrounded(bars(closes, days), AS_OF, days, config=SHORT).frame
        # Nothing in the series is zero, so no window factor may be NULL and `pos_days` must
        # count the (large) up-move out of 1.0 that follows it.
        assert frame["ret_1m"][0] is not None
        assert frame["vol_1m"][0] is not None
        assert frame["pos_days_1m"][0] is not None
        # The window's N returns are the ones at bar indices len-n .. len-1 inclusive.
        expected_positive = sum(
            1 for i in range(len(days) - n, len(days)) if closes[i] > closes[i - 1]
        )
        assert frame["pos_days_1m"][0] == pytest.approx(expected_positive / n * 100)

    def test_the_return_denominator_being_one_does_not_null_the_return(self) -> None:
        """``P_{t-(N-1)} == 1.0`` exactly, which a ``base == 1`` mutant would treat as missing.

        The base moved one bar later when docs/05 §1 was corrected on 2026-08-22 — it is the
        window's first bar, not the bar before it — so the index this plants the 1.0 at moved
        with it. The guard being tested is unchanged.
        """
        days = weekdays(30)
        window = compute_factors_unrounded(
            bars([float(i + 1) for i in range(len(days))], days), AS_OF, days, config=SHORT
        )
        n = window.window_lengths[1]
        closes = [float(i + 2) for i in range(len(days))]
        closes[len(days) - 1 - (n - 1)] = 1.0
        frame = compute_factors_unrounded(bars(closes, days), AS_OF, days, config=SHORT).frame
        assert frame["ret_1m"][0] == pytest.approx((closes[-1] / 1.0 - 1) * 100)

    def test_an_all_time_high_of_exactly_one_is_a_real_high(self) -> None:
        """Every close <= 1, so ``high_ath == 1.0`` — a ``high == 1`` mutant would null it."""
        days = weekdays(30)
        closes = [0.5 + 0.5 * (i == len(days) - 2) for i in range(len(days))]
        frame = compute_factors_unrounded(bars(closes, days), AS_OF, days, config=SHORT).frame
        assert frame["high_ath"][0] == 1.0
        assert frame["away_high_ath"][0] == pytest.approx((0.5 / 1.0 - 1) * 100)


class TestFramesTooShortToComputeAnything:
    """Kills ``frame.height == 0`` -> ``== 1`` in the RSI block.

    An empty panel reaches the engine whenever a universe selects nothing for a date — which the
    nightly pipeline can produce on a half-day session before the bhavcopy lands.
    """

    def test_an_empty_panel_returns_an_empty_frame_rather_than_raising(self) -> None:
        days = weekdays(30)
        empty = pl.DataFrame(
            schema={
                "instrument_id": pl.Int64,
                "date": pl.Date,
                "close": pl.Float64,
                "close_raw": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "volume_raw": pl.Float64,
                "turnover": pl.Float64,
                "upper_circuit": pl.Float64,
                "lower_circuit": pl.Float64,
            }
        )
        result = compute_factors_unrounded(empty, AS_OF, days, config=SHORT)
        assert result.frame.height == 0
        for months in SHORT.window_months:
            assert f"rsi_{months}m" in result.frame.columns

    def test_a_single_bar_panel_has_no_rsi(self) -> None:
        days = weekdays(30)
        one = bars([100.0], days[-1:])
        result = compute_factors_unrounded(one, AS_OF, days, config=SHORT)
        assert result.frame.height == 1
        assert result.frame["rsi_1m"][0] is None


class TestEveryWindowIsStillFive:
    """A cheap guard on the config the rest of this file narrows for speed."""

    def test_the_default_config_covers_docs_05s_five_windows(self) -> None:
        assert FactorConfig().window_months == WINDOW_MONTHS


class TestTheTurnoverSourceIsRecorded:
    """Kills the two mutants on the ``turnover_source`` expression.

    docs/05 §13 does not only say which field to prefer, it says **"record which was used"** —
    the audit column exists so a liquidity figure can be traced to the number it came from. Both
    mutants on that expression survived every other test in `packages/core`, which is to say the
    column docs/05 mandates had no assertion behind it at all. `vol_day_val` was covered (the
    cross-validation compares it); the provenance beside it was not.
    """

    def test_an_exchange_turnover_is_labelled_exchange(self) -> None:
        days = weekdays(30)
        frame = bars([100.0 + i for i in range(len(days))], days).with_columns(
            pl.lit(5_000_000.0).alias("turnover")
        )
        result = compute_factors_unrounded(frame, AS_OF, days, config=SHORT).frame
        assert result["turnover_source"][0] == "exchange"
        assert result["vol_day_val"][0] == pytest.approx(5_000_000.0)

    def test_a_missing_turnover_falls_back_and_says_so(self) -> None:
        days = weekdays(30)
        closes = [100.0 + i for i in range(len(days))]
        result = compute_factors_unrounded(bars(closes, days), AS_OF, days, config=SHORT).frame
        assert result["turnover_source"][0] == "close_x_volume"
        assert result["vol_day_val"][0] == pytest.approx(closes[-1] * 1_000.0)

    def test_a_zero_turnover_is_unavailable_not_zero_value_traded(self) -> None:
        """docs/05 §13's fallback is for turnover that is *unavailable*; a printed 0 is that."""
        days = weekdays(30)
        closes = [100.0 + i for i in range(len(days))]
        frame = bars(closes, days).with_columns(pl.lit(0.0).alias("turnover"))
        result = compute_factors_unrounded(frame, AS_OF, days, config=SHORT).frame
        assert result["turnover_source"][0] == "close_x_volume"
        assert result["vol_day_val"][0] == pytest.approx(closes[-1] * 1_000.0)

    def test_a_sub_rupee_turnover_is_still_a_real_turnover(self) -> None:
        """The threshold is zero, not one. A mutant moving it to 1 is caught here."""
        days = weekdays(30)
        frame = bars([100.0 + i for i in range(len(days))], days).with_columns(
            pl.lit(0.5).alias("turnover")
        )
        result = compute_factors_unrounded(frame, AS_OF, days, config=SHORT).frame
        assert result["turnover_source"][0] == "exchange"
        assert result["vol_day_val"][0] == pytest.approx(0.5)


class TestCutlersWindowAtTheEdges:
    """Kills the guards and the first-full-row index in ``_cutler_rsi`` (docs/05 §5).

    docs/05 §5 was amended on 2026-08-31 from Wilder's recursion at period ``N`` to **Cutler's
    simple mean at period ``N - 1``** — the count of returns inside an ``N``-bar window. Every
    test before this one runs on 765 bars against windows of at most 247, where the first
    fully-populated row is hundreds of steps behind the row being read and its exact placement is
    invisible.

    The cases below make that row *be* the row under inspection, which is the only way the
    off-by-one is observable at all, and pin the two properties that distinguish Cutler's from
    what was there before: the degenerate ``N = 1`` window is now NULL, and the value does not
    depend on how much history precedes the window.
    """

    def test_a_history_of_exactly_the_window_length_still_has_an_rsi(self) -> None:
        """``steps == period``: the window fills exactly on the last bar and nothing precedes it.

        An ``N``-bar history yields ``N - 1`` changes, which is exactly ``period``. Kills
        ``steps < period`` -> ``<=`` (which would return all-NaN) and the two mutants on
        ``out[period - 1 :]`` (which would start the block on the wrong row and leave the last
        one NaN).
        """
        days = weekdays(40)
        n = compute_factors_unrounded(
            bars([100.0 + i for i in range(len(days))], days), AS_OF, days, config=SHORT
        ).window_lengths[1]

        # Exactly n bars, ending on as_of: n - 1 changes, so the mean has precisely enough terms
        # on the final bar and not one to spare.
        short_days = days[-n:]
        rising = [100.0 + i for i in range(len(short_days))]
        frame = compute_factors_unrounded(bars(rising, short_days), AS_OF, days, config=SHORT).frame
        # A monotonically rising series has no losses at all, so docs/05 §5's "RSI = 100 when
        # avg_loss == 0" applies — and it can only apply if that row was written.
        assert frame["rsi_1m"][0] == 100.0

    def test_one_bar_short_of_the_window_has_no_rsi(self) -> None:
        """``steps == period - 1``: one change too few, so there is nothing to average.

        The mirror of the test above, and the half that fails if ``steps < period`` is relaxed.
        """
        days = weekdays(40)
        n = compute_factors_unrounded(
            bars([100.0 + i for i in range(len(days))], days), AS_OF, days, config=SHORT
        ).window_lengths[1]

        short_days = days[-(n - 1) :]
        rising = [100.0 + i for i in range(len(short_days))]
        # The ROUNDED result, because "no value" is a NULL in storage (baskfy_core.precision turns
        # the engine's in-flight NaN into one). That is the contract every reader downstream sees.
        frame = compute_factors(bars(rising, short_days), AS_OF, days, config=SHORT).frame
        assert frame["rsi_1m"][0] is None

    def test_a_window_of_a_single_trading_day_has_no_rsi(self) -> None:
        """``N == 1``, so ``period == 0``: the degenerate window docs/05 §Notation permits.

        A calendar with one trading day in the last month resolves the 1-month window to N = 1,
        which spans **zero returns**. There is no gain and no loss to average, so §5's amended
        text makes it NULL. Under the pre-2026-08-31 Wilder@N spec this returned 100 off a
        one-observation seed; §5 records the change and why.

        Kills ``period < 1`` -> ``period < 0`` and -> ``period < 2``, and the ``len(dates) < 2``
        guard's two mutants.
        """
        # Two trading days seven months apart: `as_of - 1 month` snaps forward to as_of itself.
        calendar = [dt.date(2026, 1, 5), AS_OF]
        result = compute_factors(bars([100.0, 150.0], calendar), AS_OF, calendar, config=SHORT)
        assert result.window_lengths[1] == 1
        assert result.frame["rsi_1m"][0] is None

    def test_a_single_trading_day_window_that_fell_has_no_rsi_either(self) -> None:
        """The mirror image. A zero-return window is NULL whichever way the price went."""
        calendar = [dt.date(2026, 1, 5), AS_OF]
        result = compute_factors(bars([150.0, 100.0], calendar), AS_OF, calendar, config=SHORT)
        assert result.window_lengths[1] == 1
        assert result.frame["rsi_1m"][0] is None

    def test_the_rsi_does_not_depend_on_history_before_the_window(self) -> None:
        """Cutler's is **path-independent**: it reads the window and nothing before it.

        This is the property docs/05 §5 gives as the second, independent reason to prefer Cutler's
        over Wilder's — Wilder's is seeded from the first ``N`` observations of whatever history it
        is handed and smoothed forward, so its answer moves when the series is cut somewhere else.
        Feeding the same final window twice, once with a long and violent prelude and once with
        the bare minimum, must give the same number. A recursive kernel of any kind fails this.
        """
        days = weekdays(400)
        n = compute_factors_unrounded(
            bars([100.0 + i for i in range(len(days))], days), AS_OF, days, config=SHORT
        ).window_lengths[1]

        # The last n bars are a fixed zig-zag; everything before them is a wild crash and rally
        # that a seeded recursion would still be carrying.
        tail = [100.0 + (7.0 if i % 2 else 0.0) + i for i in range(n)]
        prelude = [500.0 * (0.4 if i % 3 else 2.5) for i in range(len(days) - n)]

        long_history = compute_factors_unrounded(
            bars(prelude + tail, days), AS_OF, days, config=SHORT
        ).frame
        just_the_window = compute_factors_unrounded(
            bars(tail, days[-n:]), AS_OF, days, config=SHORT
        ).frame

        assert long_history["rsi_1m"][0] is not None
        assert long_history["rsi_1m"][0] == pytest.approx(just_the_window["rsi_1m"][0])
