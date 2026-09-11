"""The regime gate: how much of the tradable universe is above its own 200-session average.

``docs/twt/04`` §4. New entries only when **more than 40 %** of the universe is above its own
200-day average: it turns -43 % into -24.7 % for the loss of nothing (17.2 % ungated against 20.9 %
gated), and 35-40 % is the plateau.

**One arithmetic, two rows** (``04`` §4.4, DECISIONS-TW **TW0.4**). VBT-1's gate is the same
measurement over the same universe at the same threshold, and two implementations of one measurement
is the single thing that can make two pages disagree about the same day — so this module *calls*
:mod:`baskfy_core.vbt.breadth` rather than restating it. But a sleeve whose gate lives in another
sleeve's table stops having a gate the day that sleeve's nightly flag is set false, so TWT stores
``tw_breadth_daily`` of its own, and the thresholds handed across are **TWT's own, spelled out** —
a VBT recalibration cannot silently move this sleeve's gate.

Not to be confused with ``market_health_daily.pct_above_200dma``, which measures an **index's**
point-in-time membership: a different population on a different calendar, and reusing it would
change the gate that produced every number in ``01`` §6.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

import polars as pl

from baskfy_core.twt.config import DEFAULT_TWT_CONFIG, BreadthConfig, Gate, TwtConfig
from baskfy_core.vbt.breadth import breadth_above_dma as _vbt_breadth_above_dma
from baskfy_core.vbt.breadth import breadth_series as _vbt_breadth_series
from baskfy_core.vbt.breadth import gate_for as _vbt_gate_for
from baskfy_core.vbt.config import BreadthConfig as VbtBreadthConfig
from baskfy_core.vbt.config import VbtConfig


@dataclass(frozen=True, slots=True)
class BreadthReading:
    """One session's reading, and the gate it implies (``04`` §4.2).

    The denominator is the names that **printed a bar** on the session **and** have a valid
    200-session average under ``04`` §2.2's tolerance; the numerator is those whose adjusted close
    is above it. A name whose average is still warming up is in neither — counting it as "not
    above" would report a listing wave as a bear market.
    """

    session: dt.date | None
    universe_count: int
    measured_count: int
    above_count: int
    pct_above_dma: Decimal
    gate: Gate

    @property
    def is_open(self) -> bool:
        return self.gate is Gate.OPEN


def as_shared_config(config: BreadthConfig) -> VbtConfig:
    """TWT's breadth thresholds, in the shape the shared implementation takes.

    Both numbers are written out rather than inherited, and ``test_twt_docs_parity.py`` pins them
    literally against ``04`` §4.1. That is the whole of DECISIONS-TW TW0.4's first half: share the
    arithmetic, never the calibration.
    """
    return VbtConfig(
        breadth=VbtBreadthConfig(
            dma_bars=config.dma_bars,
            min_pct_above_dma=config.min_pct_above_dma,
        )
    )


def gate_for(pct_above_dma: Decimal, config: BreadthConfig | None = None) -> Gate:
    """``OPEN`` **strictly above** the threshold, ``SHUT`` at or below it (``04`` §4.3).

    Strict is the contract, not a detail: the research's own gate is ``breadth > 0.40`` and a
    session that reads exactly 40 % is a session the book does not enter on. Two values, not three
    — there is no amber and no exposure ladder, which is the swing book's shape, and this book has
    ten equal slots.
    """
    cfg = config or DEFAULT_TWT_CONFIG.breadth
    return Gate(_vbt_gate_for(pct_above_dma, as_shared_config(cfg).breadth).value)


def breadth_above_dma(
    indicated: pl.DataFrame,
    as_of: dt.date | None = None,
    config: TwtConfig = DEFAULT_TWT_CONFIG,
) -> BreadthReading:
    """The reading for one session — the **signal** session, never a later one (``04`` §4.5).

    ``pct_above_dma = above_count / measured_count x 100``, to ``pct_decimals`` [4] places.

    **A zero denominator is ``0.0000`` and a SHUT gate.** A session the panel cannot measure is not
    a session the book enters on: an empty universe must never read as a healthy market, which is
    what an "open when we cannot tell" default would make it.
    """
    reading = _vbt_breadth_above_dma(indicated, as_of, as_shared_config(config.breadth))
    return BreadthReading(
        session=reading.session,
        universe_count=reading.universe_count,
        measured_count=reading.measured_count,
        above_count=reading.above_count,
        pct_above_dma=reading.pct_above_dma,
        gate=gate_for(reading.pct_above_dma, config.breadth),
    )


def breadth_series(indicated: pl.DataFrame, config: TwtConfig = DEFAULT_TWT_CONFIG) -> pl.DataFrame:
    """``(date, universe_count, measured_count, above_count, pct_above_dma, gate)`` per session.

    One pass over the whole panel — what the backtest reads and what the nightly job writes into
    ``tw_breadth_daily``.

    The percentage is rounded to ``pct_decimals`` **before** the gate reads it, so the number a
    page shows and the number the gate decided on are the same number. ``test_twt_breadth.py``
    asserts this series agrees with :func:`breadth_above_dma` on every session, which is the guard
    that keeps "one arithmetic" true after the rounding step.
    """
    cfg = config.breadth
    return (
        _vbt_breadth_series(indicated, as_shared_config(cfg))
        .with_columns(pl.col("pct_above_dma").round(cfg.pct_decimals))
        .with_columns(
            pl.when(pl.col("pct_above_dma") > cfg.min_pct_above_dma)
            .then(pl.lit(Gate.OPEN.value))
            .otherwise(pl.lit(Gate.SHUT.value))
            .alias("gate")
        )
    )
