"""The regime gate: how much of the tradable universe is above its own 200-day average.

``docs/vbt/04`` §4. This is the rule that turns 2018-19 from a -45% hole into a -28% one and
keeps the book in cash for most of 2018-06 → 2020-06 and most of 2025. Without it the strategy
earns the same CAGR at **twice** the drawdown, and is invested 85% of the time instead of 63%.

It is a breadth reading of **the universe the book trades** — the same lesson ``CLAUDE.md``
records for the swing gate: ask the tape you actually trade. Index-based gates (Midcap 150 over
its 50-DMA, the 10-day over the 20-day) were tested on this strategy and do not help.

Not to be confused with ``market_health_daily.pct_above_200dma``, which measures an **index's
point-in-time membership** on the plant's ordinary calendar. Two different populations, two
different calendars; reusing that column would change the gate that produced every number in
``docs/vbt/01`` §4. DECISIONS-VB **VB0.3**.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

import polars as pl

from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG, BreadthConfig, Gate, VbtConfig

_PCT = Decimal(100)


@dataclass(frozen=True, slots=True)
class BreadthReading:
    """One session's reading, and the gate it implies."""

    session: dt.date | None
    universe_count: int
    measured_count: int
    above_count: int
    pct_above_dma: Decimal
    gate: Gate

    @property
    def is_open(self) -> bool:
        return self.gate is Gate.OPEN


def _latest_session(frame: pl.DataFrame) -> dt.date | None:
    """The last session the frame holds, as a date. ``None`` for an empty frame."""
    if not frame.height:
        return None
    latest = frame["date"].max()
    return latest if isinstance(latest, dt.date) else None


def gate_for(pct_above_dma: Decimal, config: BreadthConfig | None = None) -> Gate:
    """``OPEN`` above the threshold, ``SHUT`` at or below it — strict, as the research read it."""
    cfg = config or DEFAULT_VBT_CONFIG.breadth
    return Gate.OPEN if pct_above_dma > Decimal(str(cfg.min_pct_above_dma)) else Gate.SHUT


def breadth_above_dma(
    indicated: pl.DataFrame,
    as_of: dt.date | None = None,
    config: VbtConfig = DEFAULT_VBT_CONFIG,
) -> BreadthReading:
    """The reading for one session.

    The denominator is the names that both **printed a bar** on the session and **have a valid
    200-day average** under ``04`` §2.2's tolerance. A name whose average is still warming up is
    in neither the numerator nor the denominator — counting it as "not above" would report a
    listing wave as a bear market.
    """
    frame = indicated if as_of is None else indicated.filter(pl.col("date") == as_of)
    session = as_of if as_of is not None else _latest_session(frame)
    with_bar = frame.filter(pl.col("close").is_not_null())
    measured = with_bar.filter(pl.col("sma_dma").is_not_null())
    above = measured.filter(pl.col("close") > pl.col("sma_dma"))
    measured_count, above_count = measured.height, above.height
    pct = (
        (Decimal(above_count) / Decimal(measured_count) * _PCT).quantize(Decimal("0.0001"))
        if measured_count
        else Decimal("0.0000")
    )
    return BreadthReading(
        session=session,
        universe_count=with_bar.height,
        measured_count=measured_count,
        above_count=above_count,
        pct_above_dma=pct,
        gate=gate_for(pct, config.breadth),
    )


def breadth_series(indicated: pl.DataFrame, config: VbtConfig = DEFAULT_VBT_CONFIG) -> pl.DataFrame:
    """``(date, universe_count, measured_count, above_count, pct_above_dma, gate)`` per session.

    One pass over the whole panel — what the backtest reads, and what VB2 compares against the
    research's own ``out/breadth200.csv``.
    """
    threshold = config.breadth.min_pct_above_dma
    return (
        indicated.filter(pl.col("close").is_not_null())
        .group_by("date")
        .agg(
            pl.len().alias("universe_count"),
            pl.col("sma_dma").is_not_null().sum().alias("measured_count"),
            (pl.col("sma_dma").is_not_null() & (pl.col("close") > pl.col("sma_dma")))
            .sum()
            .alias("above_count"),
        )
        .with_columns(
            pl.when(pl.col("measured_count") > 0)
            .then(pl.col("above_count") / pl.col("measured_count") * 100.0)
            .otherwise(0.0)
            .alias("pct_above_dma")
        )
        .with_columns(
            pl.when(pl.col("pct_above_dma") > threshold)
            .then(pl.lit(Gate.OPEN.value))
            .otherwise(pl.lit(Gate.SHUT.value))
            .alias("gate")
        )
        .sort("date")
    )
