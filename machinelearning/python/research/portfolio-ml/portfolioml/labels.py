"""Triple-barrier labels (López de Prado ch. 3) on a long panel sorted by (instrument_id, date).

Entry is the *next* session's open. From that session on, the first of {upper barrier touched by
the high, lower barrier touched by the low} decides; both on one day counts as the loss (the
conservative reading, since we cannot order intraday prints). Untouched after ``horizon``
sessions → exit at that close. Rows without a full forward path get null labels (they are the
rows the model scores but never learns from)."""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl


@dataclass(frozen=True)
class BarrierConfig:
    up: float = 0.05
    down: float = 0.03
    horizon: int = 10  # sessions after entry, inclusive


def triple_barrier(bars: pl.DataFrame, cfg: BarrierConfig = BarrierConfig()) -> pl.DataFrame:
    """Return ``bars`` with entry_px, label (1/0/null), exit_k, exit_ret, fwd_ret_h."""
    g = pl.col("instrument_id")
    entry = pl.col("open").shift(-1).over(g)
    up_px = entry * (1 + cfg.up)
    dn_px = entry * (1 - cfg.down)
    first_up = pl.min_horizontal(
        *[pl.when(pl.col("high").shift(-k).over(g) >= up_px).then(k) for k in range(1, cfg.horizon + 1)]
    )
    first_dn = pl.min_horizontal(
        *[pl.when(pl.col("low").shift(-k).over(g) <= dn_px).then(k) for k in range(1, cfg.horizon + 1)]
    )
    close_h = pl.col("close").shift(-cfg.horizon).over(g)
    complete = close_h.is_not_null() & entry.is_not_null()
    win = first_up.is_not_null() & (first_dn.is_null() | (first_up < first_dn))
    loss = first_dn.is_not_null() & ~win
    exit_k = pl.coalesce(pl.min_horizontal(first_up, first_dn), pl.lit(cfg.horizon))
    # a stop gapped through at the open fills at the open, not at the barrier
    open_at_exit = pl.coalesce(
        *[pl.when(exit_k == k).then(pl.col("open").shift(-k).over(g)) for k in range(1, cfg.horizon + 1)]
    )
    stop_fill = pl.min_horizontal(dn_px, open_at_exit)
    exit_ret = (
        pl.when(win).then(cfg.up).when(loss).then(stop_fill / entry - 1).otherwise(close_h / entry - 1)
    )
    return bars.with_columns(
        entry.alias("entry_px"),
        pl.when(complete).then(win.cast(pl.Int8)).alias("label"),
        pl.when(complete).then(exit_k).alias("exit_k"),
        pl.when(complete).then(exit_ret).alias("exit_ret"),
        pl.when(complete).then(close_h / entry - 1).alias("fwd_ret_h"),
    )


@dataclass(frozen=True)
class MultibaggerConfig:
    up: float = 1.0  # +100% = 2x
    horizon: int = 250  # sessions ≈ 12 months
    max_close_dd: float | None = None  # e.g. -0.40: a 2× that also closed 40% under entry is not a win


def clean_label(df: pl.DataFrame, max_close_dd: float) -> pl.Expr:
    """'touched 2× and never closed below entry×(1+max_close_dd)' — the label that does not reward round-trippers."""
    return ((pl.col("label") == 1) & (pl.col("fwd_min_close_ret") > max_close_dd)).cast(pl.Int8)


def multibagger(bars: pl.DataFrame, cfg: MultibaggerConfig = MultibaggerConfig()) -> pl.DataFrame:
    """Did the name touch ``1+up`` × next-open entry at any point in the next ``horizon`` sessions?

    Also: whether it *closed* there at the horizon (``held``), the horizon return, the best and
    worst point on the way. No lower barrier — the question is 'does it double', the book sim
    decides what to do with the losers."""
    g = pl.col("instrument_id")
    entry = pl.col("open").shift(-1).over(g)
    h = cfg.horizon
    fwd_max = pl.col("high").rolling_max(h).shift(-h).over(g)
    fwd_min = pl.col("low").rolling_min(h).shift(-h).over(g)
    fwd_min_close = pl.col("close").rolling_min(h).shift(-h).over(g)
    close_h = pl.col("close").shift(-h).over(g)
    complete = close_h.is_not_null() & entry.is_not_null()
    return bars.with_columns(
        entry.alias("entry_px"),
        pl.when(complete).then((fwd_max / entry >= 1 + cfg.up).cast(pl.Int8)).alias("label"),
        pl.when(complete).then((close_h / entry >= 1 + cfg.up).cast(pl.Int8)).alias("held"),
        pl.when(complete).then(close_h / entry - 1).alias("fwd_ret_h"),
        pl.when(complete).then(fwd_max / entry - 1).alias("fwd_max_ret"),
        pl.when(complete).then(fwd_min / entry - 1).alias("fwd_min_ret"),
        pl.when(complete).then(fwd_min_close / entry - 1).alias("fwd_min_close_ret"),
    )
