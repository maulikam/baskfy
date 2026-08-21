"""The 25-instrument cross-validation corpus (Prompt 19 §3).

    "pick 25 real instruments, compute every factor with an independent, deliberately naive
     pandas implementation, and assert agreement with the Polars implementation to 4 decimal
     places."

WHAT "REAL" MEANS HERE, EXACTLY
------------------------------
The 25 symbols, their names, their series and their closing prices on 2026-08-18 are real: they
come from ``tests/fixtures/reference-screen-export-2026-08-18.csv`` by way of the Prompt 2
provider fixtures. **Every bar before 2026-08-18 is a seeded random walk** — see
``tests/fixtures/providers/PROVENANCE.md``. This repository contains no real price history and
the suite is network-blocked, so there is none to be had.

That does not weaken this particular harness. Cross-validation asks whether two independent
implementations of docs/05 agree on the *same* input; it does not ask whether the input is the
market. A synthetic path with realistic volatility exercises every branch — and where it does
not (circuit locks, missing turnover, short histories) the branch is injected deliberately below,
which real data would only supply by luck.

What the synthetic path *cannot* do is tell us whether docs/05 matches the reference product.
That is the reconciliation report's job (Prompt 19 §4), and it is honest about its own limits.
"""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path
from typing import Final

import numpy as np
import pandas as pd
import polars as pl

#: The as-of date the whole bundle is pinned to (docs/13).
AS_OF: Final = dt.date(2026, 8, 18)

#: Prompt 19 §3 — "pick 25 real instruments".
CORPUS_SIZE: Final = 25

#: The synthetic NSE price band the fixture builder writes (PROVENANCE.md: "a flat 20% NSE band
#: around the close"). Reused so the published-band branch of docs/05 §12 is exercised.
BAND_FRACTION: Final = 0.20

_FIXTURE_ROOT: Final = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "providers"


def fixture_bars_path() -> Path:
    return _FIXTURE_ROOT / "daily_bars.parquet"


def corpus_symbols() -> list[str]:
    """The 25 symbols, chosen deterministically: alphabetical, so the set never drifts."""
    bars = pl.read_parquet(fixture_bars_path())
    return sorted(bars["symbol"].unique().to_list())[:CORPUS_SIZE]


def _inject_edge_cases(frame: pl.DataFrame) -> pl.DataFrame:
    """Make the corpus exercise the branches a random walk never reaches on its own.

    Three, each named after the docs/05 clause it turns on:

    * **§12 published band** — every row carries ``upper_circuit`` / ``lower_circuit``, so the
      bhavcopy path is the one taken.
    * **§12 band heuristic** — one instrument (the last by id) has its bands removed and two of
      its bars rewritten as locked days: ``high == low == close_raw`` on a +20% move.
    * **§13 turnover fallback** — turnover is NULL on every bar of the two lowest-id instruments,
      so ``vol_day_val`` falls back to ``close_raw x volume_raw`` for them and uses the exchange
      field for everyone else. A third instrument gets a **zero** turnover on one bar and a
      **sub-rupee** one on another: docs/05 §13 says to fall back when turnover is *unavailable*,
      and a session that printed no value is unavailable. Mutation testing found that gap — with
      only NULL and large turnovers in the corpus, changing ``turnover > 0`` to ``>= 0`` or to
      ``> 1`` was invisible.
    """
    ids = sorted(frame["instrument_id"].unique().to_list())
    heuristic_id = ids[-1]
    fallback_ids = ids[:2]
    degenerate_id = ids[2]

    frame = frame.with_columns(
        pl.when(pl.col("instrument_id").is_in(fallback_ids))
        .then(None)
        .otherwise(pl.col("turnover"))
        .alias("turnover"),
        pl.when(pl.col("instrument_id") == heuristic_id)
        .then(None)
        .otherwise(pl.col("upper_circuit"))
        .alias("upper_circuit"),
        pl.when(pl.col("instrument_id") == heuristic_id)
        .then(None)
        .otherwise(pl.col("lower_circuit"))
        .alias("lower_circuit"),
    )

    # Two locked days for the heuristic instrument, at fixed positions so the corpus is
    # reproducible: bar 300 (inside 12m but outside 6m) and bar 700 (inside 3m).
    subset = frame.filter(pl.col("instrument_id") == heuristic_id).sort("date")
    locked_dates = [subset["date"][300], subset["date"][700]]
    previous = {
        locked_dates[0]: float(subset["close_raw"][299]),
        locked_dates[1]: float(subset["close_raw"][699]),
    }
    locked_price: pl.Expr = (
        pl.when(pl.col("date") == locked_dates[0])
        .then(pl.lit(round(previous[locked_dates[0]] * (1 + BAND_FRACTION), 4)))
        .when(pl.col("date") == locked_dates[1])
        .then(pl.lit(round(previous[locked_dates[1]] * (1 + BAND_FRACTION), 4)))
        .otherwise(None)
    )

    is_locked = (pl.col("instrument_id") == heuristic_id) & pl.col("date").is_in(locked_dates)
    frame = frame.with_columns(
        [
            pl.when(is_locked).then(locked_price).otherwise(pl.col(column)).alias(column)
            for column in ("close", "close_raw", "high", "low")
        ]
    )

    # The bar *before* each locked day is set to the locked price divided by the band, and the bar
    # before *that* back to the locked price. So over one bar the move is exactly +20% (a hit) and
    # over two bars it is 0% (not a hit) — which is what makes `prev_close_raw`'s one-bar shift
    # observable. Without it, shifting by two bars still saw a >20% move and the mutation survived.
    ladder = frame.filter(pl.col("instrument_id") == heuristic_id).sort("date")
    ladder_dates = ladder["date"].to_list()
    steps: dict[dt.date, float] = {}
    for position in (300, 700):
        locked = float(ladder["close_raw"][position])
        steps[ladder_dates[position - 1]] = round(locked / (1 + BAND_FRACTION), 4)
        steps[ladder_dates[position - 2]] = locked
    stepped = pl.coalesce(
        [pl.when(pl.col("date") == day).then(pl.lit(value)) for day, value in steps.items()]
        + [pl.col("close_raw")]
    )
    on_the_ladder = (pl.col("instrument_id") == heuristic_id) & pl.col("date").is_in(list(steps))
    frame = frame.with_columns(
        [
            pl.when(on_the_ladder).then(stepped).otherwise(pl.col(column)).alias(column)
            for column in ("close", "close_raw", "high", "low")
        ]
    )

    # docs/05 §13's "unavailable" turnover, in the two shapes NULL does not cover.
    # Inside the last five bars, so every `vol_avg_*` window and `median_vol_12m` sees them.
    # Placed mid-history first, they fell outside all six windows and the mutant that changes
    # `turnover > 0` to `> 1` survived because nothing downstream could see the difference.
    degenerate = frame.filter(pl.col("instrument_id") == degenerate_id).sort("date")
    zero_turnover_day = degenerate["date"][-3]
    tiny_turnover_day = degenerate["date"][-2]
    is_degenerate = pl.col("instrument_id") == degenerate_id
    return frame.with_columns(
        pl.when(is_degenerate & (pl.col("date") == zero_turnover_day))
        .then(pl.lit(0.0))
        .when(is_degenerate & (pl.col("date") == tiny_turnover_day))
        .then(pl.lit(0.5))
        .otherwise(pl.col("turnover"))
        .alias("turnover")
    )


def corpus_frame() -> pl.DataFrame:
    """The Polars frame the engine consumes: 25 instruments x 765 bars.

    ``close`` and ``close_raw`` are the same series. The fixture bars are unadjusted (docs/09:
    "providers never adjust") and no corporate action is applied here on purpose — the adjustment
    maths has its own tests in ``test_adjustments.py``, and mixing it in would mean a
    cross-validation failure could be an adjustment bug rather than a factor bug.
    """
    symbols = corpus_symbols()
    ids = {symbol: index + 1 for index, symbol in enumerate(symbols)}

    bars = (
        pl.read_parquet(fixture_bars_path())
        .filter(pl.col("symbol").is_in(symbols))
        .sort(["symbol", "date"])
        .with_columns(
            pl.col("symbol").replace_strict(ids, return_dtype=pl.Int64).alias("instrument_id"),
            pl.col("close").cast(pl.Float64),
            pl.col("high").cast(pl.Float64),
            pl.col("low").cast(pl.Float64),
            pl.col("volume").cast(pl.Float64).alias("volume_raw"),
        )
        .with_columns(
            pl.col("close").alias("close_raw"),
            # A deterministic VWAP-ish turnover: docs/05 §13's exchange field, in rupees.
            (pl.col("volume_raw") * (pl.col("high") + pl.col("low") + pl.col("close")) / 3).alias(
                "turnover"
            ),
            (pl.col("close") * (1 + BAND_FRACTION)).round(4).alias("upper_circuit"),
            (pl.col("close") * (1 - BAND_FRACTION)).round(4).alias("lower_circuit"),
        )
        .select(
            "instrument_id",
            "symbol",
            "date",
            "close",
            "close_raw",
            "high",
            "low",
            "volume_raw",
            "turnover",
            "upper_circuit",
            "lower_circuit",
        )
    )
    return _inject_edge_cases(bars)


def corpus_pandas(frame: pl.DataFrame) -> pd.DataFrame:
    """The same rows, handed to the oracle as a pandas frame.

    Via ``to_dicts()`` — plain Python scalars — rather than ``to_pandas()``. Two reasons, and the
    second is the real one: ``to_pandas()`` needs pyarrow, which docs/02 does not lock; and going
    through Arrow would make the two implementations share a conversion layer. A list of dicts
    shares nothing but the numbers.
    """
    return pd.DataFrame(frame.to_dicts())


def corpus_calendar(frame: pl.DataFrame) -> list[dt.date]:
    return sorted(frame["date"].unique().to_list())


def corpus_benchmark(calendar: list[dt.date], seed: int = 20260818) -> pl.DataFrame:
    """A NIFTY 50 proxy for docs/05 §6.

    Seeded, so the corpus is reproducible; the fixture's ``index_snapshots.parquet`` covers only
    the last 30 trading days and beta needs 252. Both implementations are handed the identical
    series, which is what a cross-validation requires.
    """
    rng = np.random.default_rng(seed)
    level = 24_000.0
    closes: list[float] = []
    for _ in calendar:
        level *= math.exp(rng.normal(0.0004, 0.009))
        closes.append(level)
    return pl.DataFrame({"date": calendar, "close": closes})
