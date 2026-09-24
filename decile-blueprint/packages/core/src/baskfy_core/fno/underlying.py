"""One session's ``fo_underlying_daily`` rows from a window of ``fo_contract_daily`` (``03`` §2).

This is the pure half of the nightly derivation (FO2's worker seam does the reading and the
upsert). It joins :func:`baskfy_core.fno.series.continuous_futures` and
:func:`baskfy_core.fno.vol.iv_atm_frame` and keeps the rows of ``trade_date`` only.

**The levels are anchored to the session's settle** (DECISIONS-FO FO2.8).
``continuous_futures`` compounds the held contract's ratios from 1.0 at the window's first
session, so its levels are a unitless index whose base moves with the window. Stored at
``numeric(18,2)`` an ATR of 0.03 would round to zero, and two nights' rows would sit on
different bases. Each night therefore rescales the window so that ``level_c`` on
``trade_date`` equals the held contract's settle: a ratio back-adjusted series, in rupees, whose
last close is a price the book could trade. ``ret``, ``rv20`` and every ratio a signal reads are
scale-free and do not change. A reader comparing levels across nights re-derives the window
rather than mixing stored rows (``RESEARCH.md``'s series is built the same way, from one window).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import polars as pl

from baskfy_core.fno.config import SeriesConfig
from baskfy_core.fno.series import continuous_futures
from baskfy_core.fno.vol import iv_atm_frame

#: The columns :func:`derive_underlying` returns besides ``symbol`` (``03`` §2, ``in_ban`` aside:
#: the nightly writes it from the ban list, not from the bhavcopy).
DERIVED_COLUMNS: tuple[str, ...] = (
    "held_expiry",
    "level_o",
    "level_h",
    "level_l",
    "level_c",
    "ret",
    "atr14",
    "oi_total",
    "fut_turnover_20d",
    "iv_atm",
    "rv20",
    "basis_ann",
    "ca_flag",
)

_FUTURES = ("FUTSTK", "FUTIDX")
_OPTIONS = ("OPTSTK", "OPTIDX")
_PRICE_COLUMNS = ("level_o", "level_h", "level_l", "level_c", "atr14")


def derive_underlying(
    window: pl.DataFrame,
    trade_date: dt.date,
    sessions: Sequence[dt.date],
    config: SeriesConfig,
) -> pl.DataFrame:
    """``fo_underlying_daily`` for ``trade_date``: one row per underlying, rounded for storage.

    ``window`` is ``fo_contract_daily`` rows (the table's column names) for sessions up to and
    including ``trade_date``; a row after it is refused (house rule 5). ``sessions`` is the
    exchange calendar, including sessions after ``trade_date``, because ``iv_atm`` needs the
    sessions left to expiry. Rounding (house rule 8): prices to 2 places, ``ret`` to 10, vols
    and basis to 6.
    """
    if window.is_empty():
        return _empty()
    latest = window.get_column("trade_date").max()
    if isinstance(latest, dt.date) and latest > trade_date:
        raise ValueError(f"the window holds {latest}, after {trade_date}: look-ahead")
    futures = window.filter(pl.col("instrument").is_in(_FUTURES))
    if futures.is_empty():
        return _empty()
    series = continuous_futures(futures, config).filter(pl.col("trade_date") == trade_date)
    if series.is_empty():
        return _empty()
    options = window.filter(
        pl.col("instrument").is_in(_OPTIONS) & (pl.col("trade_date") == trade_date)
    )
    iv = iv_atm_frame(
        options,
        futures.filter(pl.col("trade_date") == trade_date),
        sessions,
        config,
    ).select("symbol", "iv_atm")
    scale = (
        pl.when(pl.col("level_c").is_not_null() & (pl.col("level_c") > 0))
        .then(pl.col("held_settle") / pl.col("level_c"))
        .otherwise(None)
    )
    return (
        series.join(iv, on="symbol", how="left")
        .with_columns(scale.alias("_scale"))
        .with_columns((pl.col(c) * pl.col("_scale")).round(2).alias(c) for c in _PRICE_COLUMNS)
        .with_columns(
            pl.col("ret").round(10),
            pl.col("rv20").round(6),
            pl.col("iv_atm").round(6),
            pl.col("basis_ann").round(6),
            pl.col("fut_turnover_20d").round(2),
            pl.col("oi_total").cast(pl.Int64),
            pl.col("ca_flag").fill_null(False),
        )
        .select("symbol", *DERIVED_COLUMNS)
        .sort("symbol")
    )


def _empty() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "symbol": pl.String,
            "held_expiry": pl.Date,
            **{c: pl.Float64 for c in _PRICE_COLUMNS},
            "ret": pl.Float64,
            "oi_total": pl.Int64,
            "fut_turnover_20d": pl.Float64,
            "iv_atm": pl.Float64,
            "rv20": pl.Float64,
            "basis_ann": pl.Float64,
            "ca_flag": pl.Boolean,
        }
    ).select("symbol", *DERIVED_COLUMNS)
