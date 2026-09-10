"""The VBT-1 signal: Chartink's five lines, plus the six filters that make them a strategy.

``docs/vbt/04`` §3, and ``docs/vbt/01`` §2 for why the five lines alone are not enough — traded
exactly this way they return 0.8% a year at a 48% drawdown, because a 6.5%+ day on 3x volume is,
more often than not, a spike that gets sold into.

Every predicate is built from a field of :mod:`baskfy_core.vbt.config`. **A null on either side
of a comparison is a fail, never a pass**: a name without 200 bars has no 200-day average and is
not a signal, and Polars' three-valued logic would otherwise let it through a ``~(x < y)``.
"""

from __future__ import annotations

from typing import Final

import polars as pl

from baskfy_core.vbt.config import (
    DEFAULT_VBT_CONFIG,
    ScanConfig,
    SignalState,
    TrendConfig,
    TrendFilter,
    VbtConfig,
)

#: One boolean column per filter, named ``filter_A`` … ``filter_F``.
FILTER_COLUMN_PREFIX: Final[str] = "filter_"

#: The columns :func:`detect_signals` returns beyond the indicated frame's own.
SIGNAL_COLUMNS: Final[tuple[str, ...]] = ("scan_hit", "state", "failed_filters")


def _true(expr: pl.Expr) -> pl.Expr:
    """``expr`` with null read as False — the "a null is a fail" rule, in one place."""
    return expr.fill_null(value=False)


def chartink_scan(config: ScanConfig | None = None) -> pl.Expr:
    """Chartink's five lines, read literally on the closed daily bar (``04`` §3.1).

    The comparison senses are Chartink's own and are part of the contract: lines 1 and 5 are
    strict ``>``, lines 3 and 4 are ``>=``. Line 2 reads ``close_raw`` — an exchange price —
    because a ₹28 name that a 1:2 split makes ₹56 in the adjusted series did not clear it.
    """
    cfg = config or DEFAULT_VBT_CONFIG.scan
    return _true(
        (pl.col("volume") > pl.col("vol_sma") * cfg.vol_mult)
        & (pl.col("close_raw") > cfg.min_close_raw_inr)
        & (pl.col("change_pct") >= cfg.min_change_pct)
        & (pl.col("vol_sma") >= cfg.min_vol_sma)
        & (pl.col("volume") > cfg.min_volume)
    )


def trend_filters(config: TrendConfig | None = None) -> dict[TrendFilter, pl.Expr]:
    """The six filters of ``04`` §3.2, one expression each, by the letters STRATEGY §3 gives them.

    Returned as a mapping rather than one conjunction so a rejected row can say *which* filter
    rejected it (``vb_signal_daily.failed_filters``) — ``docs/vbt/01`` §3's ablation table is the
    whole argument for these six, and a system that stores only what it accepted cannot show a
    person what it passed over.
    """
    cfg = config or DEFAULT_VBT_CONFIG.trend
    return {
        TrendFilter.A_ABOVE_200DMA: _true(pl.col("close") > pl.col("sma_dma")),
        TrendFilter.B_TWENTY_DAY_HIGH: _true(pl.col("close") > pl.col("high_prior")),
        TrendFilter.C_NOT_EXTENDED: _true(pl.col("ret_lookback_pct") < cfg.max_ret_20_pct),
        TrendFilter.D_STRONG_CLOSE: _true(pl.col("close_position") >= cfg.min_close_position),
        TrendFilter.E_CHANGE_CEILING: _true(pl.col("change_pct") <= cfg.max_change_pct),
        TrendFilter.F_TURNOVER_FLOOR: _true(pl.col("turnover_avg") >= cfg.min_turnover_avg_inr),
    }


def with_signal_columns(
    indicated: pl.DataFrame, config: VbtConfig = DEFAULT_VBT_CONFIG
) -> pl.DataFrame:
    """Add ``scan_hit``, the six ``filter_*`` booleans, ``state`` and ``failed_filters``."""
    filters = trend_filters(config.trend)
    frame = indicated.with_columns(
        chartink_scan(config.scan).alias("scan_hit"),
        *[expr.alias(f"{FILTER_COLUMN_PREFIX}{name.value}") for name, expr in filters.items()],
    )
    every_filter = pl.all_horizontal(
        [pl.col(f"{FILTER_COLUMN_PREFIX}{name.value}") for name in filters]
    )
    return frame.with_columns(
        pl.when(pl.col("scan_hit") & every_filter)
        .then(pl.lit(SignalState.SIGNAL.value))
        .when(pl.col("scan_hit"))
        .then(pl.lit(SignalState.SCAN_ONLY.value))
        .otherwise(None)
        .alias("state"),
        pl.concat_list(
            [
                pl.when(pl.col(f"{FILTER_COLUMN_PREFIX}{name.value}"))
                .then(None)
                .otherwise(pl.lit(name.value))
                for name in filters
            ]
        )
        .list.drop_nulls()
        .alias("failed_filters"),
    )


def detect_signals(
    indicated: pl.DataFrame,
    as_of: object | None = None,
    config: VbtConfig = DEFAULT_VBT_CONFIG,
) -> pl.DataFrame:
    """Every scan hit of the frame — ``SIGNAL`` rows and ``SCAN_ONLY`` rows, ranked.

    ``as_of`` restricts the answer to one session, which is what the nightly job wants; the
    backtest passes ``None`` and gets the whole history in one pass. The rank key is the
    signal-day rupee turnover (``04`` §3.4): ranking by day-change instead costs 6 CAGR points
    and not ranking at all costs 4.6, so it is a rule, not a tiebreak convenience.
    """
    frame = with_signal_columns(indicated, config)
    if as_of is not None:
        frame = frame.filter(pl.col("date") == as_of)
    return (
        frame.filter(pl.col("scan_hit"))
        .with_columns(pl.col("turnover_inr").alias("rank_key"))
        .sort(["date", "rank_key", "instrument_id"], descending=[False, True, False])
    )


def signal_mask(indicated: pl.DataFrame, config: VbtConfig = DEFAULT_VBT_CONFIG) -> pl.Series:
    """A boolean column over ``indicated``: is this row a full VBT-1 signal?

    The backtest's own entry point — it wants a mask over the whole panel, not a filtered frame.
    """
    return (
        with_signal_columns(indicated, config)["state"]
        .eq(SignalState.SIGNAL.value)
        .fill_null(value=False)
    )
