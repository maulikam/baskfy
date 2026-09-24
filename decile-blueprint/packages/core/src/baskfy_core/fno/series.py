"""The continuous futures series and its derived columns (``04`` §4, ``03`` §2).

``fo_underlying_daily`` is computed here from ``fo_contract_daily``'s futures rows, as a polars
frame in and a polars frame out:

* ``held_expiry`` — the nearest expiry **strictly after the previous session**, so the series
  never sits in a contract on its own expiry day's close-out;
* ``ret = ln(settle_t / settle_{t-1})`` **of that one contract**, so a roll is never a jump; the
  levels ``level_o/h/l/c`` compound ``ret`` into a roll-free, tradeable line;
* ``ca_flag`` — a session where the held contract's settle moved above ``ca_up_ratio`` (1.4) or
  below ``ca_down_ratio`` (0.7): a split or bonus, not a return. It is **excluded from the
  series** (no level, no ret, and the windows below skip it) and ``ca_recent`` keeps it out of
  signals for ``ca_exclusion_sessions`` (5) sessions, the flagged one included;
* ``atr14`` (true range on the levels, simple mean), ``rv20 = stdev(ret, 20) x √252``,
  ``oi_total`` across expiries, ``fut_turnover_20d`` (20-session median), and
  ``basis_ann = (F ÷ S - 1) x 365 ÷ calendar days`` on UDiFF days only (null before
  8 Jul 2024, when the file carries no underlying price).

The construction is ``docs/fno/evidence/research/cont.py``'s, with the CA session kept as a
flagged row rather than dropped. ``research.continuous`` is the verbatim port the golden needs.
"""

from __future__ import annotations

import math

import polars as pl

from baskfy_core.fno.config import SeriesConfig

#: The columns :func:`continuous_futures` reads (``FO_BHAVCOPY_SCHEMA``'s futures subset).
FUTURES_COLUMNS: tuple[str, ...] = (
    "trade_date",
    "symbol",
    "instrument",
    "expiry",
    "open",
    "high",
    "low",
    "settle",
    "underlying",
    "open_interest",
    "turnover",
    "lot_size",
)


def _daily_aggregates(fut: pl.DataFrame) -> pl.DataFrame:
    return fut.group_by("symbol", "trade_date").agg(
        pl.col("open_interest").sum().alias("oi_total"),
        pl.col("turnover").sum().alias("fut_turnover"),
        pl.col("lot_size").max().alias("lot_size"),
        pl.col("underlying").max().alias("spot"),
        pl.col("instrument").first().alias("instrument"),
    )


def _held_bars(fut: pl.DataFrame, config: SeriesConfig) -> pl.DataFrame:
    """One row per (symbol, session) with the held contract's bar and its ratios."""
    front = (
        fut.filter(pl.col("expiry") > pl.col("trade_date"))
        .sort("expiry")
        .group_by("symbol", "di")
        .agg(
            pl.col("expiry").first().alias("held_expiry"),
            pl.col("settle").first().alias("prev_settle"),
        )
    )
    bars = fut.select(
        "symbol",
        pl.col("di").alias("di_next"),
        pl.col("expiry").alias("held_expiry"),
        "open",
        "high",
        "low",
        "settle",
        "trade_date",
    )
    nxt = front.with_columns((pl.col("di") + 1).alias("di_next")).join(
        bars, on=["symbol", "di_next", "held_expiry"], how="inner"
    )
    nxt = nxt.with_columns(
        (pl.col("settle") / pl.col("prev_settle")).alias("gc"),
        (pl.col("open") / pl.col("prev_settle")).alias("go"),
        (pl.col("high") / pl.col("prev_settle")).alias("gh"),
        (pl.col("low") / pl.col("prev_settle")).alias("gl"),
    ).select(
        "symbol",
        "trade_date",
        pl.col("di_next").alias("di"),
        "held_expiry",
        pl.col("settle").alias("held_settle"),
        "gc",
        "go",
        "gh",
        "gl",
    )
    ca = (pl.col("gc") > config.ca_up_ratio) | (pl.col("gc") < config.ca_down_ratio)
    return nxt.with_columns(ca.alias("ca_flag"))


def _levels(good: pl.DataFrame, config: SeriesConfig) -> pl.DataFrame:
    """Compound the held contract's ratios into levels, then ATR and RV (non-CA rows only)."""
    good = good.with_columns(
        pl.when((pl.col("go") <= 0) | pl.col("go").is_null())
        .then(1.0)
        .otherwise(pl.col("go"))
        .alias("go"),
        pl.when((pl.col("gh") <= 0) | pl.col("gh").is_null())
        .then(pl.max_horizontal("gc", pl.lit(1.0)))
        .otherwise(pl.col("gh"))
        .alias("gh"),
        pl.when((pl.col("gl") <= 0) | pl.col("gl").is_null())
        .then(pl.min_horizontal("gc", pl.lit(1.0)))
        .otherwise(pl.col("gl"))
        .alias("gl"),
    )
    good = (
        good.sort("symbol", "di")
        .with_columns(pl.col("gc").log().cum_sum().over("symbol").exp().alias("level_c"))
        .with_columns((pl.col("level_c") / pl.col("gc")).alias("_prev"))
        .with_columns(
            (pl.col("_prev") * pl.col("go")).alias("level_o"),
            (pl.col("_prev") * pl.col("gh")).alias("level_h"),
            (pl.col("_prev") * pl.col("gl")).alias("level_l"),
            pl.col("gc").log().alias("ret"),
        )
    )
    prev_c = pl.col("level_c").shift(1).over("symbol")
    true_range = pl.max_horizontal(
        pl.col("level_h") - pl.col("level_l"),
        (pl.col("level_h") - prev_c).abs(),
        (pl.col("level_l") - prev_c).abs(),
    )
    return good.with_columns(true_range.alias("_tr")).with_columns(
        pl.col("_tr").rolling_mean(config.atr_sessions).over("symbol").alias("atr14"),
        (
            pl.col("ret").rolling_std(config.rv_sessions).over("symbol")
            * math.sqrt(config.annualisation_sessions)
        ).alias("rv20"),
    )


def continuous_futures(futures: pl.DataFrame, config: SeriesConfig) -> pl.DataFrame:
    """``fo_underlying_daily``'s series columns from the futures rows of the bhavcopy.

    ``futures`` carries :data:`FUTURES_COLUMNS` (one row per contract per session; rows with a
    non-positive settle are ignored). The sessions are the frame's own distinct dates, so the
    caller passes every session it has, not one symbol's. Returns one row per (symbol, session
    the held contract printed), sorted by symbol and date.
    """
    fut = futures.select(FUTURES_COLUMNS).filter(pl.col("settle") > 0)
    dates = fut.select("trade_date").unique().sort("trade_date").with_row_index("di")
    fut = fut.join(dates, on="trade_date")
    held = _held_bars(fut, config)
    good = _levels(held.filter(~pl.col("ca_flag")), config).select(
        "symbol",
        "di",
        "level_o",
        "level_h",
        "level_l",
        "level_c",
        "ret",
        "atr14",
        "rv20",
    )
    out = (
        held.select("symbol", "trade_date", "di", "held_expiry", "held_settle", "ca_flag")
        .join(good, on=["symbol", "di"], how="left")
        .join(_daily_aggregates(fut), on=["symbol", "trade_date"], how="left")
        .sort("symbol", "di")
    )
    days = (pl.col("held_expiry") - pl.col("trade_date")).dt.total_days()
    basis_ok = (
        (pl.col("trade_date") >= config.basis_from)
        & pl.col("spot").is_not_null()
        & (pl.col("spot") > 0)
        & (days > 0)
    )
    out = out.with_columns(
        pl.col("fut_turnover")
        .rolling_median(config.turnover_sessions)
        .over("symbol")
        .alias("fut_turnover_20d"),
        pl.col("ca_flag")
        .cast(pl.Int8)
        .rolling_max(config.ca_exclusion_sessions, min_samples=1)
        .over("symbol")
        .cast(pl.Boolean)
        .alias("ca_recent"),
        pl.when(basis_ok)
        .then((pl.col("held_settle") / pl.col("spot") - 1.0) * 365.0 / days)
        .otherwise(None)
        .alias("basis_ann"),
    )
    return out.select(
        "trade_date",
        "symbol",
        "instrument",
        "held_expiry",
        "held_settle",
        "level_o",
        "level_h",
        "level_l",
        "level_c",
        "ret",
        "atr14",
        "rv20",
        "oi_total",
        "fut_turnover",
        "fut_turnover_20d",
        "lot_size",
        "spot",
        "basis_ann",
        "ca_flag",
        "ca_recent",
    )
