"""Property-based tests for the factor engine (Prompt 19 §2).

    "Property-based tests (hypothesis) for the factor engine: monotonicity (higher prices ->
     higher returns), scale invariance (multiplying an entire price series by a constant leaves
     returns, volatility, RSI and beta unchanged), and NULL propagation through blends."

Why these three and not others
------------------------------
Each is a property docs/05 implies but never states, which is exactly where a golden test is
blind. A golden test pins one input to one output; it cannot notice that the engine has become
sensitive to the *currency unit* of the price series, which is what a scale-invariance failure
would mean. And scale invariance is not academic here: docs/09's adjustment step multiplies whole
price series by ``adj_factor``, so a factor that is not scale invariant would change value on the
day a stock splits — for reasons that have nothing to do with the stock.

Series generation
-----------------
Hypothesis draws the *daily log returns*, and the price path is built from them. Drawing prices
directly would spend most of the budget on paths that jump by 400% between adjacent bars, where
every factor is dominated by one outlier. Drawing returns keeps the shrinker useful: a failing
example shrinks towards a flat series with one interesting day in it.

The short-window configuration ``(1, 3)`` is used wherever the property does not need a year of
history, so a property test costs tens of bars rather than 300. Beta (docs/05 §6 requires 200
paired observations) and the 12-month windows get the long path.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Final

import polars as pl
import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from baskfy_core.blends import BLEND_SHAPES, SHARPE_ONLY_SHAPES, blend_expr, blend_sql
from baskfy_core.factors import (
    DEFAULT_FACTOR_CONFIG,
    FactorConfig,
    compute_factors_unrounded,
)
from baskfy_core.windows import WINDOW_MONTHS

AS_OF: Final = dt.date(2026, 8, 18)

#: A short config, for the properties that do not need a year of bars. docs/05's windows are a
#: parameter of the engine, which is what makes this legitimate rather than a shortcut.
SHORT_CONFIG: Final = FactorConfig(window_months=(1, 3))

#: Relative tolerance for "unchanged". Scaling a price series by ``c`` and dividing back out is
#: two float64 roundings, so exact equality is the wrong assertion; 1e-9 is ~1e7 times looser than
#: one ULP at these magnitudes and ~1e5 times tighter than the last stored digit.
INVARIANCE_TOLERANCE: Final = 1e-9

_SUITE = settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


def weekdays(count: int, end: dt.date = AS_OF) -> list[dt.date]:
    """``count`` weekdays ending on ``end`` — a calendar with no holidays."""
    days: list[dt.date] = []
    day = end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= dt.timedelta(days=1)
    return sorted(days)


def path_from(log_returns: list[float], start: float = 100.0) -> list[float]:
    prices = [start]
    for r in log_returns:
        prices.append(prices[-1] * math.exp(r))
    return prices


def frame_for(
    closes: list[float],
    days: list[dt.date],
    *,
    instrument_id: int = 1,
    scale: float = 1.0,
) -> pl.DataFrame:
    """One instrument's bars, every price column scaled by the same constant.

    ``high``/``low``/``upper_circuit``/``lower_circuit`` are scaled too, because docs/09's
    adjustment scales the whole bar — scaling only ``close`` would be testing something the
    pipeline never does.
    """
    return pl.DataFrame(
        [
            {
                "instrument_id": instrument_id,
                "date": day,
                "close": close * scale,
                "close_raw": close * scale,
                "high": close * scale * 1.01,
                "low": close * scale * 0.99,
                "volume_raw": 10_000.0,
                "turnover": None,
                "upper_circuit": close * scale * 1.2,
                "lower_circuit": close * scale * 0.8,
            }
            for day, close in zip(days, closes, strict=True)
        ],
        strict=False,
    )


def close_enough(a: float | None, b: float | None, tolerance: float = INVARIANCE_TOLERANCE) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return math.isclose(a, b, rel_tol=tolerance, abs_tol=tolerance)


#: A day's log return: either exactly flat, or a move of at least a millionth.
#:
#: THE GAP AROUND ZERO IS A PRECONDITION, NOT A CONVENIENCE. ``pos_days_N`` counts
#: ``daily_return > 0`` (docs/05 §11), and a count of a *strict inequality* is discontinuous at
#: zero — so it cannot be scale invariant for a return within one ULP of zero. Hypothesis found
#: exactly that: a bar whose log return is 2.22e-16 gives ``P_t / P_{t-1} = 1.0000000000000002``
#: and counts as a positive day, and the same bar multiplied by 3.0 divides back to exactly 1.0
#: and does not — 50.00% becomes 45.45% on a 22-bar window. ``TestKnownDiscontinuity`` below pins
#: that behaviour rather than hiding it; this strategy states the precondition under which the
#: invariance claim is true. Exactly-flat days are *included* and are safe: ``P_t`` and
#: ``P_{t-1}`` are then the identical float, so scaling both divides back to exactly 1.0.
daily_log_return = st.one_of(
    st.just(0.0),
    st.floats(min_value=1e-6, max_value=0.12, allow_nan=False, allow_infinity=False),
    st.floats(min_value=-0.12, max_value=-1e-6, allow_nan=False, allow_infinity=False),
)

log_returns = st.lists(daily_log_return, min_size=70, max_size=90)
scales = st.floats(min_value=1e-3, max_value=1e3, allow_nan=False, allow_infinity=False)


# ---------------------------------------------------------------------------
# Property 1 — monotonicity: higher prices -> higher returns
# ---------------------------------------------------------------------------


class TestMonotonicity:
    @given(returns=log_returns, bump=st.floats(min_value=1.001, max_value=3.0))
    @_SUITE
    def test_raising_the_last_close_raises_every_return(
        self, returns: list[float], bump: float
    ) -> None:
        """docs/05 §1: ``ret_N = (P_t / P_{t-N} - 1) x 100``, and ``P_{t-N}`` did not move."""
        days = weekdays(len(returns) + 1)
        closes = path_from(returns)

        base = compute_factors_unrounded(
            frame_for(closes, days), AS_OF, days, config=SHORT_CONFIG
        ).frame
        raised_closes = [*closes[:-1], closes[-1] * bump]
        raised = compute_factors_unrounded(
            frame_for(raised_closes, days), AS_OF, days, config=SHORT_CONFIG
        ).frame

        for months in SHORT_CONFIG.window_months:
            before = base[f"ret_{months}m"][0]
            after = raised[f"ret_{months}m"][0]
            assert before is not None and after is not None
            assert after > before, f"ret_{months}m did not rise when the last close rose"

    @given(returns=log_returns, bump=st.floats(min_value=1.001, max_value=3.0))
    @_SUITE
    def test_raising_the_last_close_cannot_lower_the_all_time_high(
        self, returns: list[float], bump: float
    ) -> None:
        """docs/05 §10: ``high_ath = max(P over the full history)`` — monotone in every price."""
        days = weekdays(len(returns) + 1)
        closes = path_from(returns)
        base = compute_factors_unrounded(
            frame_for(closes, days), AS_OF, days, config=SHORT_CONFIG
        ).frame
        raised = compute_factors_unrounded(
            frame_for([*closes[:-1], closes[-1] * bump], days),
            AS_OF,
            days,
            config=SHORT_CONFIG,
        ).frame
        assert raised["high_ath"][0] >= base["high_ath"][0]
        # And distance from the all-time high can only shrink towards zero.
        #
        # Compared with a floating-point epsilon, not exactly, and the reason is this fixture
        # rather than the property. `frame_for` synthesises `high = close * 1.01`, so once
        # docs/05 §10 was corrected to read the intraday high (2026-08-22) every bar sits exactly
        # 1/1.01 below its own high and `away_high_ath` is a CONSTANT -0.990099…. Scaling the last
        # close scales its high with it, so the two sides differ only in how the same ratio was
        # arrived at — hypothesis found a pair 2e-13 apart. The property is unchanged; what
        # changed is that the quantity is no longer exactly zero at the peak, where float
        # equality was free.
        assert raised["away_high_ath"][0] >= base["away_high_ath"][0] - 1e-9

    @given(returns=log_returns)
    @_SUITE
    def test_a_uniformly_higher_series_has_a_higher_moving_average(
        self, returns: list[float]
    ) -> None:
        """docs/05 §9: ``ma_K`` is a mean, so it is monotone in every term."""
        days = weekdays(len(returns) + 1)
        closes = path_from(returns)
        lifted = [c + 1.0 for c in closes]
        base = compute_factors_unrounded(
            frame_for(closes, days), AS_OF, days, config=SHORT_CONFIG
        ).frame
        higher = compute_factors_unrounded(
            frame_for(lifted, days), AS_OF, days, config=SHORT_CONFIG
        ).frame
        for k in (20, 50):
            if base[f"ma_{k}"][0] is not None:
                assert higher[f"ma_{k}"][0] > base[f"ma_{k}"][0]


# ---------------------------------------------------------------------------
# Property 2 — scale invariance
# ---------------------------------------------------------------------------


class TestScaleInvariance:
    @given(returns=log_returns, scale=scales)
    @_SUITE
    def test_returns_volatility_rsi_are_unchanged_by_a_constant_multiple(
        self, returns: list[float], scale: float
    ) -> None:
        """Multiplying the whole series by ``c`` cancels in every ratio docs/05 defines.

        This is the property docs/09's split adjustment depends on: ``close`` is the raw print
        times ``adj_factor``, so a 10:1 split must not move a single factor.
        """
        days = weekdays(len(returns) + 1)
        closes = path_from(returns)
        unscaled = compute_factors_unrounded(
            frame_for(closes, days), AS_OF, days, config=SHORT_CONFIG
        ).frame
        scaled = compute_factors_unrounded(
            frame_for(closes, days, scale=scale),
            AS_OF,
            days,
            config=SHORT_CONFIG,
        ).frame

        invariant: list[str] = []
        for months in SHORT_CONFIG.window_months:
            invariant += [f"ret_{months}m", f"vol_{months}m", f"rsi_{months}m"]
            invariant += [f"sharpe_{months}m", f"pos_days_{months}m"]
        invariant += ["away_high_1y", "away_high_ath"]

        for column in invariant:
            before, after = unscaled[column][0], scaled[column][0]
            assert close_enough(before, after), (
                f"{column} changed under a x{scale} rescaling: {before} -> {after}"
            )

    @given(returns=log_returns, scale=scales)
    @_SUITE
    def test_prices_scale_and_ratios_do_not(self, returns: list[float], scale: float) -> None:
        """The other half: the *level* factors must scale exactly, or nothing was rescaled."""
        days = weekdays(len(returns) + 1)
        closes = path_from(returns)
        unscaled = compute_factors_unrounded(
            frame_for(closes, days), AS_OF, days, config=SHORT_CONFIG
        ).frame
        scaled = compute_factors_unrounded(
            frame_for(closes, days, scale=scale),
            AS_OF,
            days,
            config=SHORT_CONFIG,
        ).frame
        for column in ("ma_20", "ma_50", "high_ath"):
            before, after = unscaled[column][0], scaled[column][0]
            if before is None:
                assert after is None
                continue
            assert close_enough(after, before * scale), f"{column} did not scale with the series"

    @given(
        returns=st.lists(daily_log_return, min_size=300, max_size=320),
        bench=st.lists(daily_log_return, min_size=320, max_size=320),
        scale=scales,
    )
    @settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    def test_beta_is_unchanged_by_a_constant_multiple(
        self, returns: list[float], bench: list[float], scale: float
    ) -> None:
        """docs/05 §6 — beta is a ratio of moments of *returns*, so the price unit cancels.

        Needs the full config: ``min_beta_observations`` is 200 and the rolling span is 252.
        """
        days = weekdays(len(returns) + 1)
        closes = path_from(returns)
        benchmark = pl.DataFrame(
            {"date": days, "close": path_from(bench[: len(days) - 1], start=24_000.0)}
        )

        unscaled = compute_factors_unrounded(
            frame_for(closes, days),
            AS_OF,
            days,
            benchmark=benchmark,
            config=DEFAULT_FACTOR_CONFIG,
        ).frame
        scaled = compute_factors_unrounded(
            frame_for(closes, days, scale=scale),
            AS_OF,
            days,
            benchmark=benchmark,
            config=DEFAULT_FACTOR_CONFIG,
        ).frame
        assume(unscaled["beta_12m"][0] is not None)
        assert close_enough(unscaled["beta_12m"][0], scaled["beta_12m"][0], tolerance=1e-8)

    @given(returns=log_returns, scale=scales)
    @_SUITE
    def test_rescaling_the_benchmark_alone_does_not_move_beta(
        self, returns: list[float], scale: float
    ) -> None:
        """The same argument from the other side: the index's own level is arbitrary too."""
        days = weekdays(len(returns) + 1)
        closes = path_from(returns)
        bench_path = path_from([r * 0.6 for r in returns], start=24_000.0)
        short_beta = FactorConfig(window_months=(1, 3), min_beta_observations=40)

        one = compute_factors_unrounded(
            frame_for(closes, days),
            AS_OF,
            days,
            benchmark=pl.DataFrame({"date": days, "close": bench_path}),
            config=short_beta,
        ).frame
        other = compute_factors_unrounded(
            frame_for(closes, days),
            AS_OF,
            days,
            benchmark=pl.DataFrame({"date": days, "close": [c * scale for c in bench_path]}),
            config=short_beta,
        ).frame
        assume(one["beta_12m"][0] is not None)
        assert close_enough(one["beta_12m"][0], other["beta_12m"][0], tolerance=1e-8)


# ---------------------------------------------------------------------------
# Property 3 — NULL propagation through blends
# ---------------------------------------------------------------------------

ALL_SHAPES: Final = BLEND_SHAPES + SHARPE_ONLY_SHAPES


class TestBlendNullPropagation:
    @given(
        shape=st.sampled_from(ALL_SHAPES),
        values=st.lists(
            st.one_of(st.none(), st.floats(min_value=-1e4, max_value=1e4, allow_nan=False)),
            min_size=5,
            max_size=5,
        ),
    )
    @_SUITE
    def test_any_null_component_makes_the_blend_null(
        self, shape: tuple[int, ...], values: list[float | None]
    ) -> None:
        """docs/05 §4: "If **any** component is NULL the blend is NULL"."""
        by_window = dict(zip(WINDOW_MONTHS, values, strict=True))
        components = [f"sharpe_{m}m" for m in shape]
        frame = pl.DataFrame(
            {f"sharpe_{m}m": [by_window[m]] for m in WINDOW_MONTHS},
            schema={f"sharpe_{m}m": pl.Float64 for m in WINDOW_MONTHS},
        )
        result = frame.select(blend_expr(components).alias("blend"))["blend"][0]

        used = [by_window[m] for m in shape]
        if any(v is None for v in used):
            assert result is None, f"{shape} averaged over {used} instead of returning NULL"
        else:
            expected = sum(v for v in used if v is not None) / len(used)
            assert result is not None
            assert math.isclose(result, expected, rel_tol=1e-12, abs_tol=1e-12)

    @given(
        shape=st.sampled_from(ALL_SHAPES),
        values=st.lists(
            st.floats(min_value=-1e4, max_value=1e4, allow_nan=False), min_size=5, max_size=5
        ),
    )
    @_SUITE
    def test_a_complete_blend_lies_between_its_extremes(
        self, shape: tuple[int, ...], values: list[float]
    ) -> None:
        """A mean is bounded by its terms. A partial average over fewer terms need not be."""
        by_window = dict(zip(WINDOW_MONTHS, values, strict=True))
        components = [f"sharpe_{m}m" for m in shape]
        frame = pl.DataFrame(
            {f"sharpe_{m}m": [by_window[m]] for m in WINDOW_MONTHS},
            schema={f"sharpe_{m}m": pl.Float64 for m in WINDOW_MONTHS},
        )
        result = frame.select(blend_expr(components).alias("blend"))["blend"][0]
        used = [by_window[m] for m in shape]
        assert min(used) - 1e-9 <= result <= max(used) + 1e-9

    @pytest.mark.parametrize("shape", ALL_SHAPES)
    def test_the_sql_form_carries_no_coalesce(self, shape: tuple[int, ...]) -> None:
        """docs/04 computes blends in SQL; a COALESCE there is the partial average docs/05 bans."""
        sql = blend_sql([f"sharpe_{m}m" for m in shape])
        assert "COALESCE" not in sql.upper()
        assert f"/ {len(shape)}.0" in sql

    @given(
        shape=st.sampled_from(ALL_SHAPES),
        values=st.lists(
            st.one_of(st.none(), st.floats(min_value=-1e4, max_value=1e4, allow_nan=False)),
            min_size=5,
            max_size=5,
        ),
    )
    @_SUITE
    def test_sql_and_polars_blends_agree_including_on_null(
        self, shape: tuple[int, ...], values: list[float | None]
    ) -> None:
        """The two blend implementations must agree: the API uses one, the engine the other."""
        by_window = dict(zip(WINDOW_MONTHS, values, strict=True))
        components = [f"sharpe_{m}m" for m in shape]
        frame = pl.DataFrame(
            {f"sharpe_{m}m": [by_window[m]] for m in WINDOW_MONTHS},
            schema={f"sharpe_{m}m": pl.Float64 for m in WINDOW_MONTHS},
        )
        via_expr = frame.select(blend_expr(components).alias("blend"))["blend"][0]
        query = f"SELECT {blend_sql(components)} AS blend FROM frame"
        via_sql = pl.sql(query, eager=True)["blend"][0]
        assert close_enough(via_expr, via_sql, tolerance=1e-12)


# ---------------------------------------------------------------------------
# The one place scale invariance does not hold, pinned rather than hidden
# ---------------------------------------------------------------------------


class TestKnownDiscontinuity:
    """Two factors are not scale invariant for a return within one ULP of zero.

    Found by the property above rather than by review, and both are properties of the
    *definitions* rather than bugs in the engine:

    * ``pos_days_N`` counts ``r_i > 0`` (docs/05 §11). No strict-inequality count over floats can
      survive a multiplication that moves a 1-ULP tick onto the other side of zero.
    * ``rsi_N`` inherits it through the same tick. With the tick, ``avg_loss == 0`` and docs/05 §5
      says RSI is 100. Without it the series is perfectly flat, ``avg_gain`` and ``avg_loss`` are
      both zero, and this repository answers 50 — a case docs/05 does not cover.

    Asserted here so that (a) nobody "fixes" it by loosening the invariance property, and (b) if
    the engine ever starts counting ``>= 0``, or rounding returns before comparing, or answering
    something other than 50 for a flat RSI, this test says so.

    **It does not matter in production.** docs/13 §4 stores prices at 2 dp, so no real adjusted
    series has a 1e-16 daily move. It would matter to anyone feeding the engine unrounded
    synthetic prices — which is exactly what a backtest fixture is.
    """

    def test_a_sub_ulp_move_can_change_a_positive_day_count_under_rescaling(self) -> None:
        days = weekdays(30)
        # A flat series with a single 1-ULP tick upwards on the last bar.
        closes = [100.0] * (len(days) - 1)
        closes.append(math.nextafter(100.0, math.inf))

        # 0.7 is not arbitrary: it is the smallest one-decimal multiplier for which
        # `(P_t x c) / (P_{t-1} x c)` rounds back to exactly 1.0 for this pair of floats.
        scale = 0.7
        assert (closes[-1] * scale) / (closes[-2] * scale) - 1 == 0.0
        assert closes[-1] / closes[-2] - 1 > 0

        base = compute_factors_unrounded(
            frame_for(closes, days), AS_OF, days, config=SHORT_CONFIG
        ).frame
        scaled = compute_factors_unrounded(
            frame_for(closes, days, scale=scale), AS_OF, days, config=SHORT_CONFIG
        ).frame

        # The tick is a positive day at scale 1 and vanishes at scale 0.7.
        assert base["pos_days_1m"][0] is not None
        assert scaled["pos_days_1m"][0] is not None
        assert base["pos_days_1m"][0] > scaled["pos_days_1m"][0]
        # RSI moves with it: 100 (docs/05 §5's no-losses rule) becomes 50 (this repository's
        # answer for a perfectly flat series, which docs/05 does not specify).
        assert base["rsi_1m"][0] == 100.0
        assert scaled["rsi_1m"][0] == 50.0
        # The factors that are continuous in the price ratio are still invariant.
        for column in ("ret_1m", "vol_1m"):
            assert close_enough(base[column][0], scaled[column][0]), column
