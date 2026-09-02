"""The three detectors (docs/swing/04 §2-§4). One as-of date in, one row per candidate out.

Shape of the computation
------------------------
Every detector takes the frame :func:`baskfy_core.swing.indicators.with_swing_indicators`
produced, keeps the last ``window`` bars of each instrument up to ``as_of``, and evaluates its
conditions as ``group_by(instrument_id).agg(...)`` expressions — so 2,500 instruments are one
pass, not a loop. The flag detector's base-relative statistics ("the lowest low *after* the
pole's high") use ``Expr.slice`` with a per-group offset, which is what lets a window-relative
rule stay an expression.

Output contract
---------------
All three return the same columns (:data:`CANDIDATE_COLUMNS`), nulls where a column is not
meaningful for the setup, so the worker can write one table and the API can serve one shape.
``trigger`` and ``stop_ref`` are **adjusted** prices; ``adj_factor`` rides along so the plan
builder can produce the exchange price (``adjusted / adj_factor``) — see ``indicators``.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Final

import polars as pl

from baskfy_core.swing.config import DEFAULT_SWING_CONFIG, FlagConfig, Setup, SwingConfig
from baskfy_core.swing.indicators import liquid_expr

_OVER: Final = "instrument_id"
_PCT: Final = 100.0
_HALF: Final = 2
#: A reading at this multiple of its threshold earns full marks in a score component.
_FULL_MARKS: Final = 2


class CandidateStatus(StrEnum):
    """Where the candidate is in its life, the day it was detected."""

    #: A flag whose conditions hold and whose close is still below the pivot: watch the pivot.
    SETTING_UP = "SETTING_UP"
    #: A flag that closed above yesterday's pivot on volume today: the breakout happened.
    BREAKOUT_TODAY = "BREAKOUT_TODAY"
    #: The EP's gap day itself.
    GAP_DAY = "GAP_DAY"
    #: A parabolic name still making green closes.
    RUNNING = "RUNNING"
    #: A parabolic name that printed its first red close after the streak.
    EXHAUSTION = "EXHAUSTION"


#: The uniform output schema, in order.
CANDIDATE_COLUMNS: Final[tuple[str, ...]] = (
    "instrument_id",
    "symbol",
    "date",
    "setup",
    "status",
    "score",
    "close",
    "adj_factor",
    "adr_pct",
    "turnover_avg",
    "trigger",
    "stop_ref",
    "pivot_high",
    "prior_move_pct",
    "base_bars",
    "base_depth_pct",
    "tightness_adr",
    "dryup_ratio",
    "dist_ma_fast_pct",
    "dist_ma_slow_pct",
    "rvol",
    "gap_pct",
    "up_streak",
    "locked_upper_circuit",
)

_SCHEMA: Final[dict[str, pl.DataType]] = {
    "instrument_id": pl.Int64(),
    "symbol": pl.String(),
    "date": pl.Date(),
    "setup": pl.String(),
    "status": pl.String(),
    "score": pl.Float64(),
    "close": pl.Float64(),
    "adj_factor": pl.Float64(),
    "adr_pct": pl.Float64(),
    "turnover_avg": pl.Float64(),
    "trigger": pl.Float64(),
    "stop_ref": pl.Float64(),
    "pivot_high": pl.Float64(),
    "prior_move_pct": pl.Float64(),
    "base_bars": pl.Int64(),
    "base_depth_pct": pl.Float64(),
    "tightness_adr": pl.Float64(),
    "dryup_ratio": pl.Float64(),
    "dist_ma_fast_pct": pl.Float64(),
    "dist_ma_slow_pct": pl.Float64(),
    "rvol": pl.Float64(),
    "gap_pct": pl.Float64(),
    "up_streak": pl.Int32(),
    "locked_upper_circuit": pl.Boolean(),
}


def empty_candidates() -> pl.DataFrame:
    return pl.DataFrame(schema=_SCHEMA)


def _conform(frame: pl.DataFrame) -> pl.DataFrame:
    """Every output column present, typed, in :data:`CANDIDATE_COLUMNS` order."""
    for column, dtype in _SCHEMA.items():
        if column not in frame.columns:
            frame = frame.with_columns(pl.lit(None, dtype=dtype).alias(column))
    return frame.select([pl.col(c).cast(_SCHEMA[c]) for c in CANDIDATE_COLUMNS])


def _window(
    indicated: pl.DataFrame, as_of: dt.date, bars: int, *, min_bars: int = 1
) -> pl.DataFrame:
    """The last ``bars`` rows per instrument ending exactly at ``as_of``.

    ``min_bars`` drops instruments with fewer rows than a detector can reason about. A name
    listed today has one bar: no bar before it, so no pole and no base — and the flag
    detector's pole lookup (``arg_max`` over the bars before today) has nothing to look at.
    On the first run over real NSE data (3 Sep 2026) that was a crash, not an empty answer.
    """
    upto = indicated.filter(pl.col("date") <= as_of)
    present = upto.group_by(_OVER).agg(
        pl.col("date").max().alias("_last"), pl.len().alias("_rows")
    )
    present = present.filter(
        (pl.col("_last") == as_of) & (pl.col("_rows") >= min_bars)
    ).select(_OVER)
    return upto.join(present, on=_OVER, how="semi").group_by(_OVER, maintain_order=True).tail(bars)


def _clamp01(expr: pl.Expr) -> pl.Expr:
    return expr.clip(0.0, 1.0)


# ---------------------------------------------------------------------------
# Setup 1 — FLAG
# ---------------------------------------------------------------------------


def detect_flags(
    indicated: pl.DataFrame, as_of: dt.date, config: SwingConfig = DEFAULT_SWING_CONFIG
) -> pl.DataFrame:
    """docs/swing/04 §2 — the flag / continuation setup, one row per qualifying instrument."""
    flag = config.flag
    # At least one bar before today: the pole is found among the bars before ``as_of``
    # (docs/swing/04 §2, "the instrument must have a bar on as_of" and a pole before it).
    window = _window(indicated, as_of, flag.lookback_bars + flag.base_max_bars, min_bars=2)
    if window.is_empty():
        return empty_candidates()

    # Signed, so that ``pole_idx - lookback`` can go negative and be clamped rather than wrap.
    # The pole is found among the bars BEFORE today: on a breakout day today's high is the
    # highest in the window, and a base measured from it would be zero bars long.
    n = pl.len().cast(pl.Int64)
    pole_idx = pl.col("high").slice(0, n - 1).arg_max().cast(pl.Int64)
    base_bars = n - 1 - pole_idx
    pole_start = pl.max_horizontal(pole_idx - flag.lookback_bars, pl.lit(0))
    base_start = pole_idx + 1
    half = base_bars // _HALF

    agg = window.group_by(_OVER, maintain_order=True).agg(
        pl.col("symbol").last(),
        pl.col("date").last(),
        pl.col("close").last(),
        pl.col("adj_factor").last(),
        pl.col("adr_pct").last(),
        pl.col("turnover_avg").last(),
        pl.col("ma_fast").last(),
        pl.col("ma_slow").last(),
        pl.col("ma_trend").last(),
        pl.col("ma_slow").shift(flag.ma_rising_bars).last().alias("ma_slow_before"),
        pl.col("vol_avg_fast").last(),
        pl.col("rvol").last(),
        pl.col("low").last().alias("low_today"),
        pl.col("high").last().alias("high_today"),
        pl.col("upper_circuit").last(),
        pl.col("ret_60").last(),
        pl.col("high").slice(0, n - 1).max().alias("pole_high"),
        base_bars.alias("base_bars"),
        pl.col("low").slice(pole_start, pole_idx - pole_start).min().alias("pole_low"),
        pl.col("low").slice(base_start).min().alias("base_low"),
        pl.col("low").slice(base_start, half).min().alias("base_low_first_half"),
        pl.col("low").slice(base_start + half).min().alias("base_low_second_half"),
        pl.col("volume").slice(base_start).mean().alias("base_vol_avg"),
        pl.col("high").tail(flag.tight_bars).max().alias("tight_high"),
        pl.col("low").tail(flag.tight_bars).min().alias("tight_low"),
        pl.col("high").tail(flag.pivot_bars).max().alias("pivot_incl_today"),
        pl.col("high").head(n - 1).tail(flag.pivot_bars).max().alias("pivot_prev"),
        liquid_expr(config).last().alias("liquid"),
    )

    agg = agg.with_columns(
        ((pl.col("pole_high") / pl.col("pole_low") - 1.0) * _PCT).alias("prior_move_pct"),
        ((1.0 - pl.col("base_low") / pl.col("pole_high")) * _PCT).alias("base_depth_pct"),
        ((pl.col("tight_high") / pl.col("tight_low") - 1.0) * _PCT / pl.col("adr_pct")).alias(
            "tightness_adr"
        ),
        (pl.col("vol_avg_fast") / pl.col("base_vol_avg")).alias("dryup_ratio"),
        ((pl.col("close") / pl.col("ma_fast") - 1.0) * _PCT).alias("dist_ma_fast_pct"),
        ((pl.col("close") / pl.col("ma_slow") - 1.0) * _PCT).alias("dist_ma_slow_pct"),
    )

    structure = (
        pl.col("liquid")
        & (pl.col("base_bars") >= flag.base_min_bars)
        & (pl.col("base_bars") <= flag.base_max_bars)
        & (pl.col("prior_move_pct") >= flag.flagpole_min_gain_pct)
        & (pl.col("base_depth_pct") <= flag.base_max_depth_pct)
        & (pl.col("tightness_adr") <= flag.tight_max_adr_multiple)
        & (pl.col("dist_ma_fast_pct") <= flag.max_extension_adr * pl.col("adr_pct"))
        & (pl.col("dist_ma_slow_pct") >= -flag.ma_tolerance_pct)
        & (pl.col("close") >= pl.col("ma_trend"))
        & (pl.col("ma_slow") >= pl.col("ma_slow_before"))
        & (pl.col("dryup_ratio") <= flag.dryup_max_ratio)
    )
    if flag.require_higher_lows:
        structure = structure & (pl.col("base_low_second_half") >= pl.col("base_low_first_half"))

    breakout = (pl.col("close") > pl.col("pivot_prev")) & (pl.col("rvol") >= flag.breakout_min_rvol)
    candidates = agg.filter(structure | (pl.col("liquid") & breakout & _base_ok(flag)))

    score = (
        pl.lit(30.0) * _clamp01(1.0 - pl.col("tightness_adr") / flag.tight_max_adr_multiple)
        + pl.lit(25.0)
        * _clamp01(pl.col("prior_move_pct") / (_FULL_MARKS * flag.flagpole_min_gain_pct))
        + pl.lit(20.0) * _clamp01(pl.col("adr_pct") / (_FULL_MARKS * config.liquidity.adr_min_pct))
        + pl.lit(15.0) * _clamp01(1.0 - pl.col("base_depth_pct") / flag.base_max_depth_pct)
        + pl.lit(10.0) * _clamp01(1.0 - pl.col("dryup_ratio"))
    )
    return _conform(
        candidates.with_columns(
            pl.lit(Setup.FLAG.value).alias("setup"),
            pl.when(breakout)
            .then(pl.lit(CandidateStatus.BREAKOUT_TODAY.value))
            .otherwise(pl.lit(CandidateStatus.SETTING_UP.value))
            .alias("status"),
            score.alias("score"),
            pl.when(breakout)
            .then(pl.col("high_today"))
            .otherwise(pl.col("pivot_incl_today"))
            .alias("trigger"),
            pl.col("low_today").alias("stop_ref"),
            pl.col("pivot_incl_today").alias("pivot_high"),
            _locked_expr(),
        )
    )


def _base_ok(flag: FlagConfig) -> pl.Expr:
    """A breakout still needs a real base behind it; the tightness test is waived for the day."""
    return (
        (pl.col("base_bars") >= flag.base_min_bars)
        & (pl.col("base_bars") <= flag.base_max_bars)
        & (pl.col("prior_move_pct") >= flag.flagpole_min_gain_pct)
        & (pl.col("base_depth_pct") <= flag.base_max_depth_pct)
    )


def _locked_expr() -> pl.Expr:
    """True when today's high touched the upper price band: a fill cannot be assumed."""
    return (
        pl.col("upper_circuit").is_not_null()
        & (pl.col("upper_circuit") > 0)
        & (pl.col("high_today") >= pl.col("upper_circuit"))
    ).alias("locked_upper_circuit")


# ---------------------------------------------------------------------------
# Setup 2 — EP
# ---------------------------------------------------------------------------


def detect_eps(
    indicated: pl.DataFrame, as_of: dt.date, config: SwingConfig = DEFAULT_SWING_CONFIG
) -> pl.DataFrame:
    """docs/swing/04 §3 — the episodic pivot, detected on the gap day itself."""
    ep = config.ep
    window = _window(indicated, as_of, ep.prior_bars + 1)
    if window.is_empty():
        return empty_candidates()

    agg = window.group_by(_OVER, maintain_order=True).agg(
        pl.col("symbol").last(),
        pl.col("date").last(),
        pl.col("close").last(),
        pl.col("adj_factor").last(),
        pl.col("adr_pct").last(),
        pl.col("turnover_avg").last(),
        pl.col("gap_pct").last(),
        pl.col("rvol").last(),
        pl.col("close_position").last(),
        pl.col("open").last().alias("open_today"),
        pl.col("high").last().alias("high_today"),
        pl.col("low").last().alias("low_today"),
        pl.col("upper_circuit").last(),
        pl.col("prev_close").last(),
        pl.col("close").first().alias("close_prior_start"),
        pl.col("ma_fast").last(),
        pl.col("ma_slow").last(),
        pl.len().alias("_n"),
        liquid_expr(config).last().alias("liquid"),
    )
    agg = agg.with_columns(
        ((pl.col("prev_close") / pl.col("close_prior_start") - 1.0) * _PCT).alias("prior_move_pct"),
        ((pl.col("close") / pl.col("ma_fast") - 1.0) * _PCT).alias("dist_ma_fast_pct"),
        ((pl.col("close") / pl.col("ma_slow") - 1.0) * _PCT).alias("dist_ma_slow_pct"),
    )
    conditions = (
        pl.col("liquid")
        & (pl.col("_n") > ep.prior_bars)
        & (pl.col("gap_pct") >= ep.min_gap_pct)
        & (pl.col("rvol") >= ep.min_rvol)
        & (pl.col("close") >= pl.col("open_today"))
        & (pl.col("close_position") >= ep.min_close_position)
        & (pl.col("prior_move_pct") <= ep.max_prior_gain_pct)
    )
    score = (
        pl.lit(35.0) * _clamp01(pl.col("gap_pct") / (_FULL_MARKS * ep.min_gap_pct))
        + pl.lit(35.0) * _clamp01(pl.col("rvol") / (_FULL_MARKS * ep.min_rvol))
        + pl.lit(15.0) * _clamp01(pl.col("close_position"))
        + pl.lit(15.0) * _clamp01(1.0 - pl.col("prior_move_pct") / ep.max_prior_gain_pct)
    )
    return _conform(
        agg.filter(conditions).with_columns(
            pl.lit(Setup.EP.value).alias("setup"),
            pl.lit(CandidateStatus.GAP_DAY.value).alias("status"),
            score.alias("score"),
            pl.col("high_today").alias("trigger"),
            pl.col("low_today").alias("stop_ref"),
            _locked_expr(),
        )
    )


# ---------------------------------------------------------------------------
# Setup 3 — PARABOLIC_SHORT (detect only)
# ---------------------------------------------------------------------------


def detect_parabolic(
    indicated: pl.DataFrame, as_of: dt.date, config: SwingConfig = DEFAULT_SWING_CONFIG
) -> pl.DataFrame:
    """docs/swing/04 §4 — the exhausted runner. Never a BUY line; see ``TRADEABLE_SETUPS``."""
    para = config.parabolic
    window = _window(indicated, as_of, para.min_up_streak + 1)
    if window.is_empty():
        return empty_candidates()

    agg = window.group_by(_OVER, maintain_order=True).agg(
        pl.col("symbol").last(),
        pl.col("date").last(),
        pl.col("close").last(),
        pl.col("adj_factor").last(),
        pl.col("adr_pct").last(),
        pl.col("turnover_avg").last(),
        pl.col("ret_5").last(),
        pl.col("ret_10").last(),
        pl.col("up_streak").last(),
        pl.col("up_streak").shift(1).last().alias("streak_yesterday"),
        pl.col("high").last().alias("high_today"),
        pl.col("low").last().alias("low_today"),
        pl.col("upper_circuit").last(),
        pl.col("ma_fast").last(),
        pl.col("ma_slow").last(),
        liquid_expr(config).last().alias("liquid"),
    )
    agg = agg.with_columns(
        ((pl.col("close") / pl.col("ma_fast") - 1.0) * _PCT).alias("dist_ma_fast_pct"),
        ((pl.col("close") / pl.col("ma_slow") - 1.0) * _PCT).alias("dist_ma_slow_pct"),
    )
    ran = (pl.col("ret_5") >= para.min_gain_5_bars_pct) | (
        pl.col("ret_10") >= para.min_gain_10_bars_pct
    )
    extended = pl.col("dist_ma_fast_pct") >= para.min_extension_adr * pl.col("adr_pct")
    running = pl.col("up_streak") >= para.min_up_streak
    exhausted = (pl.col("up_streak") == 0) & (pl.col("streak_yesterday") >= para.min_up_streak)
    conditions = pl.col("liquid") & ran & extended & (running | exhausted)
    score = (
        pl.lit(40.0) * _clamp01(pl.col("ret_5") / (_FULL_MARKS * para.min_gain_5_bars_pct))
        + pl.lit(30.0)
        * _clamp01(
            pl.col("dist_ma_fast_pct") / (_FULL_MARKS * para.min_extension_adr * pl.col("adr_pct"))
        )
        + pl.lit(30.0) * _clamp01(pl.col("up_streak") / (_FULL_MARKS * para.min_up_streak))
    )
    return _conform(
        agg.filter(conditions).with_columns(
            pl.lit(Setup.PARABOLIC_SHORT.value).alias("setup"),
            pl.when(exhausted)
            .then(pl.lit(CandidateStatus.EXHAUSTION.value))
            .otherwise(pl.lit(CandidateStatus.RUNNING.value))
            .alias("status"),
            score.alias("score"),
            pl.col("low_today").alias("trigger"),
            pl.col("high_today").alias("stop_ref"),
            _locked_expr(),
        )
    )


# ---------------------------------------------------------------------------
# All three
# ---------------------------------------------------------------------------


def detect_setups(
    indicated: pl.DataFrame, as_of: dt.date, config: SwingConfig = DEFAULT_SWING_CONFIG
) -> pl.DataFrame:
    """Every setup for one as-of date, best score first within each setup."""
    parts = [
        detect_flags(indicated, as_of, config),
        detect_eps(indicated, as_of, config),
        detect_parabolic(indicated, as_of, config),
    ]
    return pl.concat(parts, how="vertical").sort(
        ["setup", "score", "instrument_id"], descending=[False, True, False]
    )
