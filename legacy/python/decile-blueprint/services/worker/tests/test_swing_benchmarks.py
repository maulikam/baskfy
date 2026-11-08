"""The detect step's budget (SW11, STANDING-ANSWERS B9): < 3 minutes for 2,500 instruments.

Measured on the **compute** the step runs — indicators, the liquidity filter, the detectors, the
score adjustments, the storage rounding — over a synthetic 2,500 x 200-session universe, with no
database read or write. That is the same claim `compute_factors` makes in the table ("the engine
only and not docs/03 step 7 end to end") and it is labelled the same way: the bar load and the
upsert are one indexed query and one batched insert, both of which the DB-backed detect suite
exercises for correctness rather than for time.
"""

from __future__ import annotations

import datetime as dt
import time

import numpy as np
import polars as pl
import pytest
from benchmarks.budgets import BUDGET_BY_KEY, record

from baskfy_core.precision import apply_storage_precision
from baskfy_core.swing.config import DEFAULT_SWING_CONFIG, Setup
from baskfy_core.swing.indicators import liquid_expr, with_swing_indicators
from baskfy_core.swing.setups import detect_setups
from baskfy_worker.tasks.swing import (
    apply_score_adjustments,
    sector_breadth,
    to_exchange_prices,
)

pytestmark = pytest.mark.benchmark

INSTRUMENTS = 2_500
SESSIONS = 200
START = dt.date(2025, 11, 1)


def _universe(seed: int = 11) -> tuple[pl.DataFrame, dt.date]:
    """A random-walk universe in the frame `load_swing_bars` hands the step: adjusted prices,
    a turnover, a circuit band, an adjustment factor of one. A few dozen names are given a
    flag's shape so the detectors have work past the filters."""
    rng = np.random.default_rng(seed)
    dates = [START + dt.timedelta(days=i) for i in range(SESSIONS)]
    frames: list[pl.DataFrame] = []
    for instrument_id in range(1, INSTRUMENTS + 1):
        drift = rng.normal(0.0004, 0.02, SESSIONS)
        close = 100.0 * np.cumprod(1 + drift)
        if instrument_id % 40 == 0:
            # A pole and a tightening base, so a share of the names reach the detectors' rules.
            close[100:120] *= np.linspace(1.0, 1.6, 20)
            close[120:] = close[119] * (1 + rng.normal(0, 0.004, SESSIONS - 120))
        volume = rng.integers(200_000, 2_000_000, SESSIONS).astype(float)
        frames.append(
            pl.DataFrame(
                {
                    "instrument_id": np.full(SESSIONS, instrument_id, dtype=np.int64),
                    "symbol": [f"SYN{instrument_id:04d}"] * SESSIONS,
                    "date": dates,
                    "open": close * (1 + rng.normal(0, 0.003, SESSIONS)),
                    # A 5-6% daily range: above the 4% ADR floor, so the names are liquid.
                    "high": close * (1 + 0.03 + np.abs(rng.normal(0, 0.01, SESSIONS))),
                    "low": close * (1 - 0.03 - np.abs(rng.normal(0, 0.01, SESSIONS))),
                    "close": close,
                    "volume": volume,
                    "turnover": close * volume,
                    "upper_circuit": close * 1.2,
                    "adj_factor": np.ones(SESSIONS),
                }
            )
        )
    bars = pl.concat(frames).with_columns(pl.col("date").cast(pl.Date))
    return bars, dates[-1]


def test_swing_detect_2500_synthetic_instruments_completes_inside_three_minutes() -> None:
    config = DEFAULT_SWING_CONFIG
    bars, trade_date = _universe()
    assert bars.height == INSTRUMENTS * SESSIONS

    started = time.perf_counter()
    indicated = with_swing_indicators(bars, config)
    today = indicated.filter(pl.col("date") == trade_date)
    liquid = today.filter(liquid_expr(config))
    strip = sector_breadth(liquid, {})
    candidates = detect_setups(indicated, trade_date, config)
    if not candidates.is_empty():
        candidates = apply_score_adjustments(
            to_exchange_prices(candidates),
            listed_within_2y={},
            sectors={},
            hot_sectors={slug for slug, _, _ in strip[:3]},
        )
        candidates = apply_storage_precision(candidates)
    elapsed = time.perf_counter() - started

    assert today.height == INSTRUMENTS
    per_setup = {
        setup.value: (
            0 if candidates.is_empty() else candidates.filter(pl.col("setup") == setup.value).height
        )
        for setup in Setup
    }
    limit = BUDGET_BY_KEY["swing_detect_2500"].limit
    assert limit is not None
    assert elapsed < limit, f"detect took {elapsed:.1f} s ≥ {limit} s"
    record(
        "swing_detect_2500",
        round(elapsed, 3),
        unit="s",
        method=(
            f"one pass of with_swing_indicators + liquid_expr + detect_setups + "
            f"apply_score_adjustments + apply_storage_precision over {INSTRUMENTS} instruments x "
            f"{SESSIONS} sessions ({elapsed:.1f} s; {liquid.height} liquid, "
            f"{sum(per_setup.values())} candidates)"
        ),
        dataset=(
            "a synthetic random-walk universe with a flag shape planted on every fortieth name; "
            "NO database read or write, so this is the engine and not the step end to end"
        ),
    )
