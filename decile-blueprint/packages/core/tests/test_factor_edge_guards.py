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
        """``P_{t-N} == 1.0`` exactly, which a ``base == 1`` mutant would treat as missing."""
        days = weekdays(30)
        window = compute_factors_unrounded(
            bars([float(i + 1) for i in range(len(days))], days), AS_OF, days, config=SHORT
        )
        n = window.window_lengths[1]
        closes = [float(i + 2) for i in range(len(days))]
        closes[len(days) - 1 - n] = 1.0
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


class TestWildersSeedingAtTheEdges:
    """Kills the guards and the seed-row index in ``_wilder_rsi`` (docs/05 §5).

    docs/05 §5 is unusually specific about the seeding — "Seed with a simple mean over the first
    N observations, then avg_x_t = (avg_x_{t-1} x (N-1) + x_t) / N" — and explains in the engine's
    own docstring why Polars' ``ewm_mean`` cannot be used instead (it seeds with the first
    observation, and at N=247 the two answers are still ~36% apart a year later). Every test
    before this one ran on 765 bars against windows of at most 247, where the seed row is
    hundreds of steps behind the row being read and its exact placement is invisible.

    Both cases below make the seed row *be* the row under inspection, which is the only way the
    off-by-one is observable at all.
    """

    def test_a_history_of_exactly_one_more_bar_than_the_window_still_has_an_rsi(self) -> None:
        """``steps == period``: the seed completes on the very last bar and nothing smooths it.

        Kills ``steps < period`` -> ``<=`` (which would return all-NaN) and the two mutants on
        ``out[period - 1]`` (which would write the seed to the wrong row and leave the last one
        NaN).
        """
        days = weekdays(40)
        n = compute_factors_unrounded(
            bars([100.0 + i for i in range(len(days))], days), AS_OF, days, config=SHORT
        ).window_lengths[1]

        # Exactly n + 1 bars, ending on as_of: n daily changes, so Wilder's seed is complete on
        # the final bar and the smoothing loop never runs.
        short_days = days[-(n + 1) :]
        rising = [100.0 + i for i in range(len(short_days))]
        frame = compute_factors_unrounded(bars(rising, short_days), AS_OF, days, config=SHORT).frame
        # A monotonically rising series has no losses at all, so docs/05 §5's "RSI = 100 when
        # avg_loss == 0" applies — and it can only apply if the seed row was written.
        assert frame["rsi_1m"][0] == 100.0

    def test_a_window_of_a_single_trading_day(self) -> None:
        """``period == 1``: the degenerate window docs/05 §Notation permits but never discusses.

        A calendar with one trading day in the last month resolves the 1-month window to N = 1.
        Wilder's period is then 1, the seed is a mean of one observation, and the RSI is defined.
        Kills ``period < 1`` -> ``<= 1``, ``period < 1`` -> ``< 2``, and the ``len(dates) < 2``
        guard's two mutants, all of which would return NULL instead.
        """
        # Two trading days seven months apart: `as_of - 1 month` snaps forward to as_of itself.
        calendar = [dt.date(2026, 1, 5), AS_OF]
        result = compute_factors_unrounded(
            bars([100.0, 150.0], calendar), AS_OF, calendar, config=SHORT
        )
        assert result.window_lengths[1] == 1
        # One change, and it is a gain: no losses, so docs/05 §5 gives 100.
        assert result.frame["rsi_1m"][0] == 100.0

    def test_a_single_trading_day_window_that_fell(self) -> None:
        """The mirror image: one change, and it is a loss, so ``avg_gain == 0`` and RSI is 0."""
        calendar = [dt.date(2026, 1, 5), AS_OF]
        result = compute_factors_unrounded(
            bars([150.0, 100.0], calendar), AS_OF, calendar, config=SHORT
        )
        assert result.window_lengths[1] == 1
        assert result.frame["rsi_1m"][0] == 0.0
