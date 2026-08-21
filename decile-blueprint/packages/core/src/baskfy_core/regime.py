"""Wasserstein regime classifier — **INFERRED design** (docs/05 §15).

docs/01 §5 observed the label on the instrument page: "Market Quality -> Wasserstein Regime: BULL".
docs/05 §15 specifies it:

    1. Take the instrument's last 63 daily returns -> empirical distribution Q.
    2. Take two reference distributions from that instrument's own history: B_bull = returns from
       the best-performing 20% of rolling 63-day windows, B_bear = worst 20%.
    3. Compute the 1-Wasserstein (earth-mover) distance W1(Q, B_bull) and W1(Q, B_bear)
       (for 1-D this is the mean absolute difference of sorted quantiles).
    4. BULL if W1(Q,B_bull) < W1(Q,B_bear) x (1 - margin), BEAR if the reverse, else NEUTRAL.
       margin = 0.1.

    "Expose the two distances in the API so the label is explainable rather than magic."

Prompt 5 deliverable 2 repeats that requirement: "the Wasserstein regime classifier (return the
label AND both distances)". So both distances are returned as columns, not just the verdict.

The references are drawn from the instrument's *own* history rather than from a cross-sectional
pool, which is what makes the label mean "this stock is behaving like it does in its own good
periods" rather than "this stock looks like the market". A stock with too little history to build
references gets NEUTRAL and NULL distances — the honest answer, rather than a default of BULL.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import numpy as np
import polars as pl

#: docs/05 §15 step 1.
REGIME_SAMPLE_BARS: Final = 63
#: docs/05 §15 step 2 — "the best-performing 20% ... worst 20%".
REGIME_TAIL_FRACTION: Final = 0.20
#: docs/05 §15 step 4.
REGIME_MARGIN: Final = 0.10

#: How much history is needed before the references mean anything: enough rolling windows that a
#: 20% tail is more than a couple of samples.
MIN_REGIME_WINDOWS: Final = 10


class Regime(StrEnum):
    """docs/04: ``factor_daily.regime`` is 'BULL' | 'BEAR' | 'NEUTRAL'."""

    BULL = "BULL"
    BEAR = "BEAR"
    NEUTRAL = "NEUTRAL"


@dataclass(frozen=True, slots=True)
class RegimeConfig:
    sample_bars: int = REGIME_SAMPLE_BARS
    tail_fraction: float = REGIME_TAIL_FRACTION
    margin: float = REGIME_MARGIN
    min_windows: int = MIN_REGIME_WINDOWS

    def __post_init__(self) -> None:
        if self.sample_bars < 2:  # noqa: PLR2004 - a distance needs at least two observations
            raise ValueError(f"sample_bars must be at least 2; got {self.sample_bars}")
        if not 0 < self.tail_fraction < 1:
            raise ValueError(f"tail_fraction must be in (0, 1); got {self.tail_fraction}")
        if not 0 <= self.margin < 1:
            raise ValueError(f"margin must be in [0, 1); got {self.margin}")


def wasserstein_1d(left: np.ndarray, right: np.ndarray) -> float:
    """The 1-Wasserstein distance between two 1-D samples.

    docs/05 §15: "for 1-D this is the mean absolute difference of sorted quantiles". Samples of
    different lengths are compared on a common quantile grid, which is the same statistic and
    avoids discarding observations from the longer one.
    """
    if left.size == 0 or right.size == 0:
        return float("nan")
    grid = np.linspace(0.0, 1.0, num=max(left.size, right.size))
    return float(np.mean(np.abs(np.quantile(left, grid) - np.quantile(right, grid))))


def classify_series(returns: np.ndarray, config: RegimeConfig) -> tuple[str, float, float]:
    """The label and both distances for one instrument's return history.

    Returns ``(regime, distance_to_bull, distance_to_bear)``; the distances are NaN when there is
    not enough history to build references.
    """
    clean = returns[~np.isnan(returns)]
    n = config.sample_bars
    if clean.size < n:
        return Regime.NEUTRAL.value, float("nan"), float("nan")

    current = clean[-n:]

    # Step 2: rolling 63-day windows over the instrument's own history, ranked by performance.
    starts = np.arange(0, clean.size - n + 1)
    if starts.size < config.min_windows:
        return Regime.NEUTRAL.value, float("nan"), float("nan")
    windows = np.stack([clean[s : s + n] for s in starts])
    performance = windows.sum(axis=1)
    order = np.argsort(performance)
    tail = max(1, round(starts.size * config.tail_fraction))

    bear_reference = windows[order[:tail]].reshape(-1)
    bull_reference = windows[order[-tail:]].reshape(-1)

    to_bull = wasserstein_1d(current, bull_reference)
    to_bear = wasserstein_1d(current, bear_reference)
    if not np.isfinite(to_bull) or not np.isfinite(to_bear):
        return Regime.NEUTRAL.value, to_bull, to_bear

    # Step 4. The margin is what stops a label flipping on noise: being marginally closer to the
    # bull reference is not evidence of a bull regime.
    threshold = 1.0 - config.margin
    if to_bull < to_bear * threshold:
        return Regime.BULL.value, to_bull, to_bear
    if to_bear < to_bull * threshold:
        return Regime.BEAR.value, to_bull, to_bear
    return Regime.NEUTRAL.value, to_bull, to_bear


def classify_regimes(frame: pl.DataFrame, config: RegimeConfig) -> pl.DataFrame:
    """Attach ``regime``, ``regime_distance_bull`` and ``regime_distance_bear`` to the last bar.

    The classification is only meaningful at the end of a history, so it is computed once per
    instrument and broadcast; every earlier row carries NULL rather than a stale label.
    """
    if frame.height == 0:
        return frame.with_columns(
            pl.lit(None, dtype=pl.String).alias("regime"),
            pl.lit(None, dtype=pl.Float64).alias("regime_distance_bull"),
            pl.lit(None, dtype=pl.Float64).alias("regime_distance_bear"),
        )

    labels: list[dict[str, object]] = []
    for (instrument_id,), group in frame.group_by(["instrument_id"], maintain_order=True):
        returns = group["daily_return"].to_numpy().astype(float)
        regime, to_bull, to_bear = classify_series(returns, config)
        labels.append(
            {
                "instrument_id": instrument_id,
                "regime": regime,
                "regime_distance_bull": None if np.isnan(to_bull) else to_bull,
                "regime_distance_bear": None if np.isnan(to_bear) else to_bear,
            }
        )

    summary = pl.DataFrame(labels, strict=False).with_columns(
        pl.col("instrument_id").cast(frame.schema["instrument_id"])
    )
    last_date = frame["date"].max()
    return frame.join(summary, on="instrument_id", how="left").with_columns(
        [
            pl.when(pl.col("date") == last_date).then(pl.col(column)).otherwise(None).alias(column)
            for column in ("regime", "regime_distance_bull", "regime_distance_bear")
        ]
    )
