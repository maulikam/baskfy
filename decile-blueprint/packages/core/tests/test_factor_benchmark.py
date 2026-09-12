"""The nightly factor computation, timed — Prompt 16 deliverable 1.

    "A benchmark suite ... covering: ... and the nightly factor computation."

docs/11 §"Performance budgets" gives no separate figure for step 7 of docs/03's ten. What it gives
is the whole chain: **"Nightly pipeline end-to-end | < 45 min"**. So this benchmark's job is not to
pass a threshold docs/11 does not state — it is to produce the number, and to show that
``compute_factors`` is not the reason the chain would miss.

What it measures
----------------
``baskfy_core.factors.compute_factors`` over a production-sized panel: docs/02 §"Why Postgres"
sizes the market at "~2,300 NSE instruments", and the twelve-month windows need two years of
history behind the as-of date, so the input is ~2,300 x ~500 bars — 1.15M rows, which is the shape
the real step hands the engine.

What it does **not** measure, and this matters when reading the number: the bars are synthetic and
the database is not involved. The nightly step also reads two years of ``ohlcv_daily`` and writes
~2,300 ``factor_daily`` rows, and neither of those is here. Until a real backfill exists there is
nothing to measure them against — the seeded database holds one trading day of *results* and no
price history (CLAUDE.md §"Open items"). Recorded honestly in ``benchmarks/AS-MEASURED.md``.

A ceiling rather than a docs/11 budget
--------------------------------------
:data:`ENGINE_CEILING_SECONDS` is chosen here, not transcribed: 45 minutes is the whole ten-step
chain, of which nine steps are network-bound fetches against Kite and NSE. Giving the one pure-CPU
step a tenth of the chain — 270 s — leaves the fetches the rest and still fails loudly if a change
to the engine makes it quadratic. It is deliberately not tight; a tight bound on a synthetic panel
would fail on a slower CI runner for no reason anyone could act on.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Final

import numpy as np
import polars as pl
import pytest
from benchmarks.budgets import record

from baskfy_core.factors import compute_factors
from _law1_io import seed_holidays

pytestmark = pytest.mark.benchmark

#: docs/02 §"Why Postgres + TimescaleDB, not ClickHouse": "~2,300 NSE instruments".
INSTRUMENTS: Final = 2300

#: Two years of trading days: enough history for the 12-month windows plus the 21-day skip-month
#: shift, which is what the real step loads.
HISTORY_DAYS: Final = 500

#: See the module docstring. Not a docs/11 number.
ENGINE_CEILING_SECONDS: Final = 270.0

AS_OF: Final = dt.date(2026, 8, 18)


def _trading_days(count: int, end: dt.date) -> list[dt.date]:
    """``count`` weekdays ending at ``end``, holidays removed.

    The calendar shape matters to :func:`baskfy_core.windows.resolve_windows`, which resolves the
    twelve-month window by counting backwards through it — a naive "every day" calendar would make
    the windows a third too long and the benchmark would measure a different computation.
    """
    holidays = set(seed_holidays())
    days: list[dt.date] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5 and cursor not in holidays:
            days.append(cursor)
        cursor -= dt.timedelta(days=1)
    return sorted(days)


def _panel(instruments: int, days: list[dt.date]) -> pl.DataFrame:
    """A seeded random walk per instrument, in the frame shape ``compute_factors`` requires.

    Seeded, so the benchmark measures the same arithmetic every run: an unseeded walk can land on
    a series with more NULL windows than the last one and shift the number for no reason.
    """
    rng = np.random.default_rng(20260821)
    n_days = len(days)
    steps = rng.normal(loc=0.0005, scale=0.018, size=(instruments, n_days))
    levels = 100.0 * np.exp(np.cumsum(steps, axis=1))
    close = np.round(levels, 2).ravel()
    volume = rng.integers(10_000, 5_000_000, size=instruments * n_days)

    return pl.DataFrame(
        {
            "instrument_id": np.repeat(np.arange(1, instruments + 1), n_days),
            "date": np.tile(np.array(days, dtype="datetime64[D]"), instruments),
            "close": close,
            "close_raw": close,
            "high": np.round(close * 1.01, 2),
            "low": np.round(close * 0.99, 2),
            "volume_raw": volume,
            "volume": volume,
            "series": np.full(instruments * n_days, "EQ"),
        }
    ).with_columns(pl.col("date").cast(pl.Date))


def _benchmark_series(days: list[dt.date]) -> pl.DataFrame:
    """A NIFTY 50 level series for beta (docs/05 §6)."""
    rng = np.random.default_rng(7)
    levels = 20000.0 * np.exp(np.cumsum(rng.normal(0.0004, 0.009, size=len(days))))
    return pl.DataFrame(
        {"date": np.array(days, dtype="datetime64[D]"), "close": np.round(levels, 2)}
    ).with_columns(pl.col("date").cast(pl.Date))


class TestNightlyFactorComputation:
    def test_a_full_market_panel_computes_in_reasonable_time(self) -> None:
        days = _trading_days(HISTORY_DAYS, AS_OF)
        bars = _panel(INSTRUMENTS, days)
        benchmark = _benchmark_series(days)
        assert bars.height == INSTRUMENTS * HISTORY_DAYS

        started = time.perf_counter()
        result = compute_factors(bars, AS_OF, days, benchmark=benchmark)
        elapsed = time.perf_counter() - started

        assert result.frame.height == INSTRUMENTS, (
            "one row per instrument at the as-of date, or the engine computed something else"
        )
        # docs/13 §3 — the 12-month window is 247 trading days on a full calendar. This is a
        # synthetic calendar, so the assertion is only that the window resolved to something
        # plausible; `test_factors_golden.py` owns the exact lengths.
        assert result.window_lengths[12] > 200
        assert elapsed < ENGINE_CEILING_SECONDS, (
            f"compute_factors took {elapsed:.1f} s over {bars.height} bars, past the "
            f"{ENGINE_CEILING_SECONDS:.0f} s ceiling this module explains"
        )
        record(
            "compute_factors",
            elapsed,
            unit="s",
            method=(
                f"one call to baskfy_core.factors.compute_factors over "
                f"{INSTRUMENTS} instruments x {HISTORY_DAYS} bars"
            ),
            dataset=(
                "a synthetic seeded random walk; NO database read or write, so this is the "
                "engine only and not docs/03 step 7 end to end"
            ),
        )
