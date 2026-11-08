"""The factor cache: one core ``compute_factors`` frame per monthly signal date. No formulas here.

Every factor value in the ablation is produced by ``baskfy_core``:

* ``compute_factors`` — every stored column (docs/05 + C1), called with the inputs the nightly
  gives it: the last ``DEFAULT_LOOKBACK_DAYS`` of bars (``baskfy_worker.engine``), the trading
  calendar, NIFTY 50 as ``benchmark``.
* ``factors_ranking.cross_sectional_pctile`` / ``rank_persistence`` — ``mom_pctile`` and
  ``rank_persist_20``, the two cross-sectional/cross-date C1 columns, exactly as the worker fills
  them. ``rank_persist_20`` needs ``mom_pctile`` on the 19 sessions before each signal date, so
  those sessions are computed too, with the Wasserstein regime switched off through its own config
  knob (``RegimeConfig.min_windows``) — the regime is 97% of the engine's time and ``mom_pctile``
  never reads it. ``test_runner_uses_core.py`` checks that the switch leaves the Sharpe columns
  bit-identical.
* ``blends.blend_expr`` over the registry's components — ``avg_sharpe_12_6_3_1``.
* ``factor_registry.sql_for("regime_priority")`` evaluated by Polars SQL — ``regime_priority``.

Point in time: the frame for date ``d`` is computed from bars dated ``<= d`` only, the universe
is decided from the trailing 63-bar median traded value ending at ``d``, and membership flags use
the latest snapshot on or before ``d``.

The universe (C8): eligible instruments with a bar on ``d`` and ``mtv_63 >= Rs 5 cr``. Only
universe rows are computed and cached; it is ``universe_mask <> 0`` for ``mom_pctile``.

Memory: instruments are computed in chunks of ``CHUNK`` (every column used here is per instrument;
the cross-sectional ones are filled after the chunks are joined), one date at a time, and each
date is written to ``cache/<run>/factors/<date>.parquet`` before the next starts — so a run is
resumable and holds one date's frames at a time.
"""

from __future__ import annotations

import datetime as dt
import gc
import time
from collections.abc import Sequence
from pathlib import Path

import pandas as pd
import polars as pl
from baskfy_core.blends import blend_expr
from baskfy_core.factor_registry import FACTORS, sql_for
from baskfy_core.factors import FactorConfig, compute_factors
from baskfy_core.factors_ranking import (
    MOM_PCTILE_SOURCE,
    RANK_PERSIST_DATES,
    cross_sectional_pctile,
    rank_persistence,
)
from baskfy_core.instrument_regime import RegimeConfig
from baskfy_core.ranking_engine import IN_NIFTY_200, IS_FNO
from baskfy_core.ranking_validation import monthly_signal_dates
from baskfy_worker.engine import DEFAULT_LOOKBACK_DAYS

from rvl import data

#: The columns ``compute_factors`` reads (``REQUIRED_COLUMNS`` + the optional ones the export has).
FACTOR_INPUTS = [
    "instrument_id",
    "date",
    "close",
    "close_raw",
    "high",
    "low",
    "volume_raw",
    "upper_circuit",
    "lower_circuit",
]
CHUNK = 500
#: Regime off: ``classify_series`` returns NEUTRAL before building a single reference window.
NO_REGIME = FactorConfig(regime=RegimeConfig(min_windows=10**9))
SHARPE_COMPONENTS = list(FACTORS[MOM_PCTILE_SOURCE].components)

#: C8: 2013-01 -> 2026-08. The export starts 2017-01-02, and the 12-month windows plus nse_mr12's
#: 13-month anchor need a full year of bars, so the first decision is the March 2018 month end.
FIRST_SIGNAL_MONTH = dt.date(2018, 3, 1)
LAST_SIGNAL_DAY = dt.date(2026, 8, 31)


def signal_dates(sessions: Sequence[dt.date]) -> list[dt.date]:
    return [
        d for d in monthly_signal_dates(sessions) if FIRST_SIGNAL_MONTH <= d <= LAST_SIGNAL_DAY
    ]


def universe(bars: pl.DataFrame, day: dt.date, ids: set[int] | None) -> list[int]:
    today = bars.filter(
        (pl.col("date") == day)
        & pl.col("close").is_not_null()
        & (pl.col("mtv_63") >= data.UNIVERSE_MIN_TRADED_VALUE_INR)
    )
    chosen = today["instrument_id"].to_list()
    return sorted(i for i in chosen if ids is None or i in ids)


def factors_on(  # noqa: PLR0913 - the engine's own inputs plus the chunking
    bars: pl.DataFrame,
    series: pl.DataFrame,
    day: dt.date,
    ids: list[int],
    trading_days: Sequence[dt.date],
    benchmark: pl.DataFrame | None,
    market_benchmark: pl.DataFrame | None,
    config: FactorConfig | None = None,
) -> pl.DataFrame:
    """``compute_factors`` at ``day`` for ``ids``, over the worker's lookback, in chunks."""
    start = day - dt.timedelta(days=DEFAULT_LOOKBACK_DAYS)
    window = bars.filter((pl.col("date") >= start) & (pl.col("date") <= day))
    frames: list[pl.DataFrame] = []
    for offset in range(0, len(ids), CHUNK):
        chunk = ids[offset : offset + CHUNK]
        history = (
            window.filter(pl.col("instrument_id").is_in(chunk))
            .select(FACTOR_INPUTS)
            .join(series.select("instrument_id", "series"), on="instrument_id", how="left")
        )
        result = compute_factors(
            history,
            day,
            list(trading_days),
            benchmark,
            config if config is not None else FactorConfig(),
            market_benchmark=market_benchmark,
        ).frame
        frames.append(result)
        del history
    return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()


def _with_universe_mask(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.with_columns(pl.lit(1, dtype=pl.Int32).alias("universe_mask"))


def build_date(  # noqa: PLR0913
    bars: pl.DataFrame,
    series: pl.DataFrame,
    day: dt.date,
    sessions: Sequence[dt.date],
    trading_days: Sequence[dt.date],
    benchmark: pl.DataFrame | None,
    market_benchmark: pl.DataFrame | None,
    memberships: dict[str, dict[dt.date, frozenset[int]]],
    ids: set[int] | None,
) -> pl.DataFrame:
    ids_today = universe(bars, day, ids)
    frame = factors_on(bars, series, day, ids_today, trading_days, benchmark, market_benchmark)
    frame = cross_sectional_pctile(_with_universe_mask(frame))

    # rank_persist_20: mom_pctile on the 19 sessions before `day`, then core's persistence.
    position = list(sessions).index(day)
    history = [frame.select("instrument_id", "date", "mom_pctile")]
    for prior in sessions[max(0, position - (RANK_PERSIST_DATES - 1)) : position]:
        prior_ids = universe(bars, prior, ids)
        if not prior_ids:
            continue
        prior_frame = factors_on(
            bars,
            series,
            prior,
            prior_ids,
            trading_days,
            benchmark,
            market_benchmark,
            NO_REGIME,
        ).select("instrument_id", "date", *SHARPE_COMPONENTS)
        history.append(
            cross_sectional_pctile(_with_universe_mask(prior_frame)).select(
                "instrument_id", "date", "mom_pctile"
            )
        )
    persistence = rank_persistence(pl.concat(history, how="vertical_relaxed"), day)
    frame = frame.drop("rank_persist_20").join(persistence, on="instrument_id", how="left")

    frame = frame.with_columns(
        blend_expr(SHARPE_COMPONENTS).alias(MOM_PCTILE_SOURCE),
        pl.col("instrument_id").is_in(list(data.member_on(memberships["nifty-200"], day))).alias(
            IN_NIFTY_200
        ),
        pl.col("instrument_id").is_in(list(data.member_on(memberships["nifty-fno"], day))).alias(
            IS_FNO
        ),
    )
    priority = (
        pl.SQLContext(frame=frame.select("instrument_id", "regime"))
        .execute(f"SELECT instrument_id, {sql_for('regime_priority')} AS regime_priority FROM frame")
        .collect()
    )
    frame = frame.join(priority, on="instrument_id", how="left")
    mtv = bars.filter(pl.col("date") == day).select("instrument_id", "mtv_63")
    frame = frame.join(mtv, on="instrument_id", how="left").join(
        series.select("instrument_id", "symbol"), on="instrument_id", how="left"
    )
    return frame


def run_dir(limit: int | None) -> Path:
    return data.CACHE / ("full" if limit is None else f"subset-{limit}")


def subset_ids(bars: pl.DataFrame, limit: int) -> set[int]:
    """The ``limit`` instruments that are most often in the universe on a signal date."""
    counts = (
        bars.filter(pl.col("mtv_63") >= data.UNIVERSE_MIN_TRADED_VALUE_INR)
        .group_by("instrument_id")
        .len()
        .sort(["len", "instrument_id"], descending=[True, False])
    )
    return set(counts.head(limit)["instrument_id"].to_list())


def build_cache(limit: int | None = None) -> Path:
    """Compute and cache every signal date's factor frame; dates already cached are skipped."""
    data.bars_parquet()
    out = run_dir(limit) / "factors"
    out.mkdir(parents=True, exist_ok=True)
    trading_days = data.calendar()
    bars = data.load_bars([*FACTOR_INPUTS, "mtv_63"])
    sessions = sorted(bars["date"].unique().to_list())
    ids = subset_ids(bars, limit) if limit is not None else None
    series = data.instruments().select("instrument_id", "symbol", "series")
    benchmark = data.index_levels("NIFTY 50")
    market_benchmark = data.index_levels("NIFTY 500")
    memberships = {slug: data.membership(slug) for slug in ("nifty-200", "nifty-fno")}
    dates = signal_dates(sessions)
    print(
        f"factor cache: {len(dates)} signal dates {dates[0]} -> {dates[-1]}, "
        f"{'all' if ids is None else len(ids)} instruments, NIFTY 500 levels "
        f"{'present' if market_benchmark is not None else 'ABSENT (rs_persist_126 is NULL)'}",
        flush=True,
    )
    started = time.time()
    for n, day in enumerate(dates, 1):
        target = out / f"{day.isoformat()}.parquet"
        if target.exists():
            continue
        t = time.time()
        frame = build_date(
            bars,
            series,
            day,
            sessions,
            trading_days,
            benchmark,
            market_benchmark,
            memberships,
            ids,
        )
        frame.write_parquet(target)
        print(
            f"  [{n}/{len(dates)}] {day} rows={frame.height} {time.time() - t:.0f}s "
            f"(elapsed {time.time() - started:.0f}s)",
            flush=True,
        )
        del frame
        gc.collect()
    del bars
    gc.collect()
    return out


def load_cache(limit: int | None = None, columns: list[str] | None = None) -> pd.DataFrame:
    files = sorted((run_dir(limit) / "factors").glob("*.parquet"))
    scan = pl.scan_parquet(files)
    if columns is not None:
        scan = scan.select(columns)
    return data.to_pandas(scan.collect())
