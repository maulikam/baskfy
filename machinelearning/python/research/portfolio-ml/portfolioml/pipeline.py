"""The feature frame both build_dataset.py and the nightly mb task produce. Frames in, frames out."""

from __future__ import annotations

from collections.abc import Iterator

import polars as pl

from portfolioml import features as F

PRICE = ["instrument_id", "date", "open", "high", "low", "close", "volume", "close_raw", "turnover"]
KEEP = ["instrument_id", "date", "symbol", "eligible", "close", "close_raw", "open", "high", "low", "liquid", "n_hist"]


def chunks(bars: pl.DataFrame, n: int) -> list[pl.DataFrame]:
    ids = bars["instrument_id"].unique().sort()
    step = (len(ids) + n - 1) // n
    return [bars.filter(pl.col("instrument_id").is_in(ids[i:i + step].implode())) for i in range(0, len(ids), step)]


def with_names(bars: pl.DataFrame, ins: pl.DataFrame) -> pl.DataFrame:
    return bars.select(PRICE).join(ins.select("instrument_id", "eligible", "symbol"), on="instrument_id", how="left").with_columns(
        pl.col("eligible").fill_null(False))


def name_part(chunk: pl.DataFrame) -> pl.DataFrame:
    """Per-name features for one instrument chunk, float32."""
    f = F.name_features(chunk)
    f32 = [c for c in F.NAME_FEATURES if c in f.columns and c not in ("rs21", "rs63", "n_hist")] + ["turnover20"]
    return f.select(*KEEP, *f32).with_columns([pl.col(c).cast(pl.Float32) for c in f32])


def _near_sus_gap() -> pl.Expr:
    # suspicious overnight gaps = unadjusted corporate actions (VBT §1); quarantine the 60 sessions
    # *after* one — backward-looking only, so the universe never knows about a gap still to come
    sus = ((pl.col("r1").abs() > 0.6) | (pl.col("gap").abs() > 0.6)).cast(pl.Int8)
    return (sus.rolling_max(61, min_samples=1).over("instrument_id") > 0).alias("near_sus_gap")


UNIVERSE = (pl.col("liquid") & pl.col("eligible") & ~pl.col("near_sus_gap")).alias("universe")


def _cross(feat: pl.DataFrame) -> pl.DataFrame:
    feat = F.cross_sectional(feat)
    cs = [c for c in F.CS_FEATURES + F.MARKET_FEATURES + ["rs21", "rs63"] if c in feat.columns]
    return feat.with_columns([pl.col(c).cast(pl.Float32) for c in cs])


def finish(feat: pl.DataFrame, idx: pl.DataFrame) -> pl.DataFrame:
    """Market, breadth and cross-sectional features, then the universe flag."""
    feat = feat.join(F.market_features(idx), on="date", how="left")
    feat = feat.join(F.breadth(feat), on="date", how="left")
    return _cross(feat).with_columns(_near_sus_gap()).with_columns(UNIVERSE)


BREADTH_IN = ["instrument_id", "date", "liquid", "eligible", "r1", "gap", "px_sma200", "px_sma50", "r21", "dist_hi20"]


def finish_kept(parts: pl.LazyFrame, idx: pl.DataFrame, keep) -> pl.DataFrame:
    """``finish`` for the sessions ``keep(universe_dates)`` returns, reading the full columns of those rows only
    (the trainer's memory cap). Breadth and the gap quarantine read every row; ranks are per session; so the
    rows returned equal ``finish``'s on the same sessions."""
    slim = parts.select(BREADTH_IN).collect().sort("instrument_id", "date")
    b = F.breadth(slim)
    slim = slim.with_columns(_near_sus_gap())
    dates = keep(slim.filter(pl.col("liquid") & pl.col("eligible") & ~pl.col("near_sus_gap"))["date"].unique().sort())
    near = slim.filter(pl.col("date").is_in(dates.implode())).select("instrument_id", "date", "near_sus_gap")
    del slim
    feat = parts.filter(pl.col("date").is_in(dates.implode())).collect().sort("instrument_id", "date")
    feat = feat.join(F.market_features(idx), on="date", how="left").join(b, on="date", how="left")
    return _cross(feat).join(near, on=["instrument_id", "date"], how="left").with_columns(UNIVERSE)


# breadth200_chg20 needs 21 dates, near_sus_gap 61 rows per name: keep enough sessions for both
TAIL_SESSIONS = 80


def tail_features(bars: pl.DataFrame, ins: pl.DataFrame, idx: pl.DataFrame, n_chunks: int = 8) -> pl.DataFrame:
    """The nightly's frame: full history in, only the last TAIL_SESSIONS sessions out (identical values there)."""
    bars = with_names(bars, ins)
    keep_from = bars["date"].unique().sort()[-TAIL_SESSIONS:][0]
    parts: Iterator[pl.DataFrame] = (name_part(ch).filter(pl.col("date") >= keep_from) for ch in chunks(bars, n_chunks))
    return finish(pl.concat(list(parts)).sort("instrument_id", "date"), idx)
