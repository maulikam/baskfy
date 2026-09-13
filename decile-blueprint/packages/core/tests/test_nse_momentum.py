"""NIFTY200 Momentum 30 score — asserts NSE's methodology text (§16), not current behaviour.

Every expected number below is worked by hand from the text in ``baskfy_core.nse_momentum``'s
docstring; the arithmetic is written out next to it so a reviewer can redo it on paper.
"""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pandas as pd
import pytest

from baskfy_core import nse_momentum
from baskfy_core.nse_momentum import (
    Anchors,
    HorizonStats,
    NsePopulation,
    anchors_for,
    momentum_ratios,
    normalise,
    scores,
)


def _calendar(start: str, end: str, holidays: tuple[str, ...] = ()) -> list[dt.date]:
    days = pd.bdate_range(start, end)
    return [d.date() for d in days if d.strftime("%Y-%m-%d") not in holidays]


class TestMonthEndAnchoring:
    def test_anchors_are_last_trading_days_of_m1_m7_m13(self) -> None:
        # Rebalance month M = Sep 2026. 31 Aug 2026 is a Monday; 28 Feb 2026 a Saturday, so the
        # last session is Fri 27 Feb; 31 Aug 2025 is a Sunday, so Fri 29 Aug 2025.
        calendar = _calendar("2025-01-01", "2026-09-30")
        assert anchors_for(dt.date(2026, 9, 13), calendar) == Anchors(
            end_m1=dt.date(2026, 8, 31),
            end_m7=dt.date(2026, 2, 27),
            end_m13=dt.date(2025, 8, 29),
        )

    def test_a_holiday_on_the_month_end_moves_the_anchor_back(self) -> None:
        calendar = _calendar("2025-01-01", "2026-09-30", holidays=("2026-08-31",))
        anchors = anchors_for(dt.date(2026, 9, 13), calendar)
        assert anchors is not None
        assert anchors.end_m1 == dt.date(2026, 8, 28)

    def test_anchors_are_constant_within_the_rebalance_month(self) -> None:
        calendar = _calendar("2025-01-01", "2026-10-30")
        first = anchors_for(dt.date(2026, 9, 1), calendar)
        assert first == anchors_for(dt.date(2026, 9, 30), calendar)
        october = anchors_for(dt.date(2026, 10, 1), calendar)
        assert october is not None and october.end_m1 == dt.date(2026, 9, 30)

    def test_the_rebalance_months_own_sessions_are_never_read(self) -> None:
        """No look-ahead: adding September sessions cannot move September's anchors."""
        before = _calendar("2025-01-01", "2026-08-31")
        after = _calendar("2025-01-01", "2026-09-30")
        assert anchors_for(dt.date(2026, 9, 2), before) == anchors_for(dt.date(2026, 9, 2), after)

    def test_a_calendar_that_does_not_reach_m13_has_no_anchors(self) -> None:
        assert anchors_for(dt.date(2026, 9, 13), _calendar("2025-09-01", "2026-09-30")) is None


class TestMomentumRatios:
    """Step 2 on a series whose log returns alternate ``drift ± swing``."""

    DRIFT = 0.001
    SWING = 0.01

    def _closes(self, calendar: list[dt.date]) -> pd.Series:
        signs = np.array([1.0 if i % 2 == 0 else -1.0 for i in range(len(calendar))])
        log_returns = self.DRIFT + self.SWING * signs
        log_returns[0] = 0.0
        prices = 100.0 * np.exp(np.cumsum(log_returns))
        return pd.Series(prices, index=pd.DatetimeIndex(calendar))

    def _expected(self, calendar: list[dt.date], start: dt.date, end: dt.date) -> float:
        # Over the 252 returns ending at `end` the swings cancel (126 up, 126 down), so every
        # deviation from the mean is ±SWING: sample variance = 252·SWING² / 251.
        sigma_p = self.SWING * math.sqrt(252 / 251) * math.sqrt(252)
        i, j = calendar.index(start), calendar.index(end)
        swings = sum(1.0 if k % 2 == 0 else -1.0 for k in range(i + 1, j + 1))
        log_return = self.DRIFT * (j - i) + self.SWING * swings
        return (math.exp(log_return) - 1.0) / sigma_p

    def test_mr6_and_mr12_follow_the_text(self) -> None:
        calendar = _calendar("2024-06-03", "2026-09-11")
        anchors = anchors_for(dt.date(2026, 9, 11), calendar)
        assert anchors is not None
        mr6, mr12 = momentum_ratios(self._closes(calendar), anchors)
        assert mr6 == pytest.approx(self._expected(calendar, anchors.end_m7, anchors.end_m1))
        assert mr12 == pytest.approx(self._expected(calendar, anchors.end_m13, anchors.end_m1))

    def test_bars_after_the_m1_anchor_do_not_change_the_ratio(self) -> None:
        calendar = _calendar("2024-06-03", "2026-09-11")
        anchors = anchors_for(dt.date(2026, 9, 11), calendar)
        assert anchors is not None
        closes = self._closes(calendar)
        shocked = closes.copy()
        shocked.loc[pd.Timestamp("2026-09-01") :] *= 3.0
        assert momentum_ratios(closes, anchors) == momentum_ratios(shocked, anchors)

    def test_a_suspended_month_end_uses_the_last_close_in_that_month(self) -> None:
        calendar = _calendar("2024-06-03", "2026-09-11")
        anchors = anchors_for(dt.date(2026, 9, 11), calendar)
        assert anchors is not None
        closes = self._closes(calendar).drop(pd.Timestamp(anchors.end_m13))
        previous = calendar[calendar.index(anchors.end_m13) - 1]
        _, mr12 = momentum_ratios(closes, anchors)
        # The 253-close volatility window ending at M-1 starts in Sep 2025, after the dropped
        # bar, so only the M-13 start price moves: to the previous session's close in Aug 2025.
        expected_price_ratio = float(closes.loc[pd.Timestamp(anchors.end_m1)]) / float(
            closes.loc[pd.Timestamp(previous)]
        )
        window = closes.loc[: pd.Timestamp(anchors.end_m1)].iloc[-253:].to_numpy()
        sigma_p = float(np.std(np.diff(np.log(window)), ddof=1)) * math.sqrt(252)
        assert mr12 == pytest.approx((expected_price_ratio - 1.0) / sigma_p)

    def test_no_bar_in_the_anchor_month_means_no_ratio(self) -> None:
        calendar = _calendar("2024-06-03", "2026-09-11")
        anchors = anchors_for(dt.date(2026, 9, 11), calendar)
        assert anchors is not None
        closes = self._closes(calendar)
        dates = pd.DatetimeIndex(closes.index)
        february = (dates.year == 2026) & (dates.month == 2)
        mr6, mr12 = momentum_ratios(closes[~february], anchors)
        assert mr6 is None
        assert mr12 is not None

    def test_short_history_has_neither_ratio(self) -> None:
        """Step 1's one-year listing rule: 252 sessions of returns ending at M-1 are required."""
        calendar = _calendar("2024-06-03", "2026-09-11")
        anchors = anchors_for(dt.date(2026, 9, 11), calendar)
        assert anchors is not None
        closes = self._closes(calendar)
        end = calendar.index(anchors.end_m1)
        young = closes.iloc[end - 251 :]  # 252 closes = 251 returns up to M-1
        assert momentum_ratios(young, anchors) == (None, None)
        just_enough = closes.iloc[end - 252 :]
        assert momentum_ratios(just_enough, anchors)[0] is not None


class TestNormalise:
    def test_both_branches(self) -> None:
        assert normalise(0.0) == 1.0
        assert normalise(1.5) == 2.5
        assert normalise(-1.0) == 0.5
        assert normalise(-3.0) == 0.25

    def test_continuous_and_positive(self) -> None:
        assert normalise(-1e-12) == pytest.approx(normalise(0.0))
        assert all(normalise(z) > 0 for z in (-100.0, -1.0, 0.0, 1.0, 100.0))


class TestFiveStockExample:
    """Steps 1, 3, 4, 5 on five eligible names plus three ineligible ones.

    MR12 = [2, 1, 0, -1, 3]:   mean 1, population variance (1+0+1+4+4)/5 = 2, sigma = sqrt 2
        Z12 = [1/sqrt2, 0, -1/sqrt2, -sqrt2, sqrt2]
    MR6 = [0.5, 1.5, -0.5, 0.5, 1.0]: mean 0.6, deviations [-0.1, 0.9, -1.1, -0.1, 0.4],
        population variance (0.01+0.81+1.21+0.01+0.16)/5 = 0.44
        Z6 = deviations / sqrt 0.44
    weighted Z = 0.5·Z12 + 0.5·Z6; score = 1+Z (Z ≥ 0) else 1/(1-Z).
    """

    def _frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "symbol": ["A", "B", "C", "D", "E", "NOT200", "NOFNO", "YOUNG"],
                "mr12": [2.0, 1.0, 0.0, -1.0, 3.0, 50.0, 50.0, np.nan],
                "mr6": [0.5, 1.5, -0.5, 0.5, 1.0, 50.0, 50.0, 40.0],
                "in200": [True, True, True, True, True, False, True, True],
                "fno": [True, True, True, True, True, True, False, True],
            }
        )

    def _expected(self) -> list[float]:
        root2 = math.sqrt(2.0)
        z12 = [1 / root2, 0.0, -1 / root2, -root2, root2]
        z6 = [d / math.sqrt(0.44) for d in (-0.1, 0.9, -1.1, -0.1, 0.4)]
        out = []
        for a, b in zip(z12, z6, strict=True):
            z = 0.5 * a + 0.5 * b
            out.append(1 + z if z >= 0 else 1 / (1 - z))
        return out

    def test_scores(self) -> None:
        frame = self._frame()
        result = scores(frame.mr6, frame.mr12, frame.in200, frame.fno)
        assert result.iloc[:5].tolist() == pytest.approx(self._expected())
        # The same numbers to six places, worked by hand, so the closed forms above are checked too.
        assert result.iloc[:5].round(6).tolist() == [
            1.278176,
            1.678401,
            0.458146,
            0.561015,
            2.008618,
        ]

    def test_negative_z_uses_the_reciprocal_branch(self) -> None:
        frame = self._frame()
        result = scores(frame.mr6, frame.mr12, frame.in200, frame.fno)
        c_weighted = 0.5 * (-1 / math.sqrt(2)) + 0.5 * (-1.1 / math.sqrt(0.44))
        assert c_weighted < 0
        assert result.iloc[2] == pytest.approx(1 / (1 - c_weighted))
        assert 0 < result.iloc[2] < 1

    def test_ineligible_rows_are_null_and_do_not_move_the_population(self) -> None:
        frame = self._frame()
        result = scores(frame.mr6, frame.mr12, frame.in200, frame.fno)
        assert result.iloc[5:].isna().all()
        eligible_only = frame.iloc[:5]
        again = scores(
            eligible_only.mr6, eligible_only.mr12, eligible_only.in200, eligible_only.fno
        )
        assert again.tolist() == pytest.approx(result.iloc[:5].tolist())

    def test_ranking_order(self) -> None:
        frame = self._frame()
        frame["score"] = scores(frame.mr6, frame.mr12, frame.in200, frame.fno)
        ranked = frame.dropna(subset=["score"]).sort_values("score", ascending=False)
        assert ranked.symbol.tolist() == ["E", "B", "A", "D", "C"]

    def test_an_explicit_population_is_used_instead_of_the_frame(self) -> None:
        """A narrower frame must standardise against the whole eligible universe."""
        frame = self._frame().iloc[:2]
        population = NsePopulation(
            mr6=HorizonStats(mean=0.6, std=math.sqrt(0.44)),
            mr12=HorizonStats(mean=1.0, std=math.sqrt(2.0)),
        )
        result = scores(frame.mr6, frame.mr12, frame.in200, frame.fno, population=population)
        assert result.tolist() == pytest.approx(self._expected()[:2])

    def test_a_single_eligible_name_has_no_defined_z(self) -> None:
        frame = self._frame().iloc[:1]
        assert scores(frame.mr6, frame.mr12, frame.in200, frame.fno).isna().all()


def test_version_and_universe_constants() -> None:
    assert nse_momentum.NSE_MOMENTUM_VERSION == "nifty200-momentum30-2026.09"
    assert nse_momentum.NSE_MOMENTUM_INDEX == "nifty-200"
    assert nse_momentum.HORIZON_WEIGHTS == {12: 0.5, 6: 0.5}
