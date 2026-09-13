"""Research candidates — corrections from docs/ranking/PLAN.md, not current behaviour."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from baskfy_core.factor_registry import FACTORS
from baskfy_core import ranking_research as rr
from baskfy_core.ranking import FactorPreference


def test_research_keys_are_not_sort_by_factors() -> None:
    for key in rr.RESEARCH_CANDIDATE_KEYS:
        assert key not in FACTORS, f"{key} must not be in FACTORS until promoted"


def test_excess_return_is_eligibility_not_rank() -> None:
    assert rr.RESEARCH_PREFERENCE["excess_return_common_index"] == FactorPreference.ELIGIBILITY


def test_atr_extension_is_target_range() -> None:
    assert rr.RESEARCH_PREFERENCE["atr_extension"] == FactorPreference.TARGET_RANGE
    close = np.array([110.0, 100.0])
    atr = np.array([5.0, 5.0])
    ma = np.array([100.0, 100.0])
    ext = rr.atr_extension(close, atr, ma)
    assert ext[0] == pytest.approx(2.0)
    assert ext[1] == pytest.approx(0.0)


def test_acceleration_is_non_overlapping() -> None:
    """Last short block vs prior short block — not overlapping 3m−12m."""
    # Synthetic: flat, then jump, then flat — recent rate > prior rate.
    n = 252
    close = np.ones(n, dtype=float)
    close[126:] = 1.2  # step halfway; recent 63 sessions are elevated vs prior 63
    close = np.cumprod(np.concatenate([[100.0], np.diff(np.log(close * 100)) * 0 + 1.001]))
    # Cleaner construction: geometric growth that accelerates.
    t = np.arange(n, dtype=float)
    px = 100.0 * np.exp(0.0001 * t + 0.0005 * np.maximum(t - (n - 63), 0))
    acc = rr.acceleration_nonoverlap(px, short=63, long=252)
    assert np.isfinite(acc[-1])
    # Recent block has higher drift → positive acceleration at the end.
    assert acc[-1] > 0


def test_efficiency_ratio_bounds() -> None:
    # Perfect trend: efficiency → 1
    close = np.linspace(100, 120, 40)
    eff = rr.efficiency_ratio(close, window=20)
    assert eff[-1] == pytest.approx(1.0, abs=1e-9)
    # Pure oscillation: efficiency near 0
    osc = np.array([100.0, 101.0] * 20)
    eff_osc = rr.efficiency_ratio(osc, window=20)
    assert eff_osc[-1] == pytest.approx(0.0, abs=1e-9)


def test_excess_return_common_index_does_not_reorder_when_index_constant() -> None:
    """Identical index subtraction preserves order — hence not a rank key."""
    asset = np.array([10.0, 20.0, 5.0])
    index = np.array([3.0, 3.0, 3.0])
    excess = rr.excess_return_common_index(asset, index)
    assert list(np.argsort(-excess)) == list(np.argsort(-asset))


def test_research_frame_attaches_columns() -> None:
    close = pd.Series(np.linspace(100, 130, 300))
    frame = pd.DataFrame(
        {
            "close": close,
            "atr_14": 2.0,
            "ret_12m": 15.0,
            "index_return": 10.0,
        }
    )
    out = rr.research_frame(frame)
    assert "atr_extension" in out.columns
    assert "ma_slope" in out.columns
    assert "efficiency_ratio" in out.columns
    assert "acceleration_nonoverlap" in out.columns
    assert "excess_return_common_index" in out.columns
