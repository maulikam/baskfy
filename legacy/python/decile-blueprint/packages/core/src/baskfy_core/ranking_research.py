"""Phase 1.3's research candidates, now promoted to stored factors — this module holds no formulas.

docs/ranking/PLAN.md Phase 1.3 put ATR extension, MA slope, efficiency ratio, non-overlapping
acceleration and excess return over a common index here as free-standing NumPy helpers. Phase 2
(contract C1) defined each one exactly and stored it on ``factor_daily``, computed by
``baskfy_core.factors_ranking``. Two implementations of one number is how a research notebook and
the screen end up ranking by different things, so the helpers were deleted and this module now
only *names* the promotion and delegates to the stored implementation.

The research names stay out of ``FACTORS`` — they are aliases, and the dropdown carries the C1
keys. The corrections Phase 1.3 locked in survive in C1 and in the registry:

1. Excess return over a *common* index is a filter / column, not a rank key: subtracting the same
   index return from every name cannot reorder them (``excess_ret_*`` is ``rankable=False``).
2. ATR extension is ``target_range`` — overextension is a risk, not "better".
3. Acceleration compares **non-overlapping** blocks of log returns (``accel_21_105``: the last 21
   sessions against the 105 before them), never ``3m - 12m`` on overlapping horizons.
"""

from __future__ import annotations

import datetime as dt
from typing import Final

import polars as pl

from baskfy_core.factor_registry import FACTORS
from baskfy_core.factors import compute_factors
from baskfy_core.ranking import FactorPreference

#: Phase 1.3 research name -> the C1 stored factor that replaced it.
PROMOTED_TO: Final[dict[str, str]] = {
    "atr_extension": "atr_ext_20",
    "ma_slope": "ma50_slope_20",
    "efficiency_ratio": "eff_ratio_63",
    "acceleration_nonoverlap": "accel_21_105",
    # filter/column only — see the module docstring, correction 1.
    "excess_return_common_index": "excess_ret_12m",
}

#: The Phase 1.3 names. They must never appear in ``FACTORS``: the registry carries the C1 keys.
RESEARCH_CANDIDATE_KEYS: Final[tuple[str, ...]] = tuple(PROMOTED_TO)

#: Preference per research name, read from the registry entry it was promoted to — not restated.
RESEARCH_PREFERENCE: Final[dict[str, FactorPreference]] = {
    name: FACTORS[stored].preference for name, stored in PROMOTED_TO.items()
}


def research_frame(
    bars: pl.DataFrame,
    as_of: dt.date,
    trading_days: list[dt.date] | tuple[dt.date, ...],
    *,
    benchmark: pl.DataFrame | None = None,
    market_benchmark: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """The promoted columns for ``as_of``, under their research names, from the stored engine.

    Exactly ``compute_factors(...)``'s rounded row — the value the nightly writes — with each C1
    column copied to its Phase 1.3 name, so a notebook written against the old names reads the
    production numbers rather than a private re-derivation of them.
    """
    stored = compute_factors(
        bars, as_of, trading_days, benchmark, market_benchmark=market_benchmark
    ).frame
    return stored.select(
        "instrument_id",
        *(pl.col(column).alias(name) for name, column in PROMOTED_TO.items()),
    )
