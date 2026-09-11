"""Per-bar columns the TWT-1 rules read (``docs/twt/04`` §2, §3).

One frame in, one frame out. Every column is a Polars window expression evaluated
``.over("instrument_id")``, the same shape as :mod:`baskfy_core.factors`,
:mod:`baskfy_core.swing.indicators` and :mod:`baskfy_core.vbt.indicators`.

**The frame is densified first, and that is not an implementation detail.** A rolling window here
counts *sessions*, not *rows*: the 50-session volume average of a name that did not trade on two of
the last fifty sessions is the average of the forty-eight bars it did print, over that same
fifty-session span — not the average of its last fifty traded bars, which would reach back further
in time for exactly the illiquid names the liquidity floor is trying to judge. So the instrument's
bars are laid out against the calendar with a **null** where no bar exists, and every rolling
statistic carries ``min_samples`` from ``DataConfig.rolling_min_share`` (``04`` §2.2).

The adjusted series is the input. ``ohlcv_daily.open/high/low/close`` are ``raw x adj_factor``
(:mod:`baskfy_core.adjustments`), so a split inside a 200-session window does not fake a trend.
``close_raw`` rides along because two rules are about exchange prices and not about the adjusted
series: Chartink's ``Close > 30`` (``04`` §3.1 line 1) and the rupee turnover of §3.5's floor.

The two bucket keys — ``week_key`` and ``month_key`` — are computed here rather than in
:mod:`baskfy_core.twt.signals` because they are a property of the session, not of the rule that
reads them, and because **``week_key`` is the one place in this sleeve where a wrong expression is
silent**: it is built from the **ISO year**, never the calendar year. 2024-12-30 belongs to ISO
week 2025-W01, and a key of ``calendar_year x 100 + iso_week`` would number it 202401 and sort it
*before* 2024-W52 — which would hand the last week of December the previous January's closes and
produce a tight range out of nothing. ``docs/twt/06`` TW1's acceptance criterion names this case.
"""

from __future__ import annotations

from typing import Final

import polars as pl

from baskfy_core.twt.config import DEFAULT_TWT_CONFIG, TwtConfig
from baskfy_core.vbt.calendar import SessionCalendar
from baskfy_core.vbt.indicators import densify

#: Columns :func:`with_twt_indicators` requires.
REQUIRED_COLUMNS: Final[tuple[str, ...]] = (
    "instrument_id",
    "date",
    "open",
    "high",
    "low",
    "close",
    "close_raw",
    "volume",
)

#: Columns used when present and tolerated when absent. ``is_etf`` is an instrument attribute the
#: universe join supplies (``04`` §1.3); absent, nothing is an ETF, which is the right default for
#: a hand-built fixture and the wrong one for a production panel — so the worker always passes it.
OPTIONAL_COLUMNS: Final[tuple[str, ...]] = ("adj_factor", "symbol", "is_etf")

#: The columns :func:`with_twt_indicators` adds, in the order it adds them.
INDICATOR_COLUMNS: Final[tuple[str, ...]] = (
    "week_key",
    "month_key",
    "turnover_inr",
    "vol_sma",
    "turnover_avg_20",
    "sma_dma",
    "limit_locked",
)

_OVER: Final = "instrument_id"
_DATE: Final = "date"
#: ``year x MONTHS_IN_YEAR + month - 1`` — a monotone integer key for a calendar month, so that
#: "three months before March 2025" is subtraction rather than date arithmetic (``04`` §3.3). A
#: definition, not a threshold: there is no calibration in which a year has some other number of
#: months.
MONTHS_IN_YEAR: Final[int] = 12
#: ``iso_year x WEEK_KEY_SCALE + iso_week`` — the same trick for an ISO week. 53 weeks fit.
WEEK_KEY_SCALE: Final[int] = 100


def require_columns(bars: pl.DataFrame) -> None:
    missing = [c for c in REQUIRED_COLUMNS if c not in bars.columns]
    if missing:
        raise ValueError(f"bars are missing required columns {missing}; got {bars.columns}")


def _prepare(bars: pl.DataFrame) -> pl.DataFrame:
    frame = bars
    for column in OPTIONAL_COLUMNS:
        if column not in frame.columns:
            dtype = {"symbol": pl.String, "is_etf": pl.Boolean}.get(column, pl.Float64)
            frame = frame.with_columns(pl.lit(None, dtype=dtype).alias(column))
    return frame.with_columns(
        [
            pl.col(c).cast(pl.Float64)
            for c in ("open", "high", "low", "close", "close_raw", "volume")
        ]
        + [
            pl.col("adj_factor").cast(pl.Float64).fill_null(1),
            pl.col("is_etf").cast(pl.Boolean).fill_null(value=False),
        ]
    )


def bucket_keys() -> tuple[pl.Expr, pl.Expr]:
    """``(week_key, month_key)`` — the two calendar buckets ``04`` §3.2 and §3.3 read.

    **The ISO year, never the calendar year.** See this module's docstring: a key built from
    ``dt.year()`` numbers 2024-12-30 as 202401 and sorts the first week of a year before the last
    week of the one before it, which is a look-back into January that reads as a tight base.
    """
    week = pl.col(_DATE).dt.iso_year().cast(pl.Int64) * WEEK_KEY_SCALE + pl.col(
        _DATE
    ).dt.week().cast(pl.Int64)
    month = (
        pl.col(_DATE).dt.year().cast(pl.Int64) * MONTHS_IN_YEAR
        + pl.col(_DATE).dt.month().cast(pl.Int64)
        - 1
    )
    return week.alias("week_key"), month.alias("month_key")


def with_twt_indicators(
    bars: pl.DataFrame,
    calendar: SessionCalendar,
    config: TwtConfig = DEFAULT_TWT_CONFIG,
) -> pl.DataFrame:
    """Add every indicator column, over the instrument, against the calendar.

    Columns added (all ``Float64`` unless stated):

    * ``week_key``, ``month_key`` (``Int64``) — the ISO-week and calendar-month buckets, above.
    * ``turnover_inr`` — ``close_raw x volume``, the exchange print times the traded quantity. It
      is **the rank key** (``04`` §6.3: the signal session's own turnover, DECISIONS-TW TW0.2) as
      well as the input to the average below.
    * ``vol_sma`` — mean volume over ``scan.vol_sma_bars`` [50] **including today**, which is how
      Chartink reads ``Sma(Volume, 50)`` (``04`` §3.1 line 5).
    * ``turnover_avg_20`` — mean ``turnover_inr`` over ``entry.turnover_avg_bars`` [20] sessions
      ending at this one. The liquidity floor of ``04`` §3.5 and the 1 %-of-turnover cap of §6.2
      both read it, and it is stored beside every signal so a past session can be re-funnelled at
      a different floor without re-detection.
    * ``sma_dma`` — mean close over ``breadth.dma_bars`` [200], including today. **Not a filter**:
      TWT-1 has no trend filter (``01`` §4 measured them and they hurt). It exists only so
      :mod:`baskfy_core.twt.breadth` can count the universe above its own average (``04`` §4.2).
    * ``limit_locked`` (``Boolean``) — ``open == high == low`` on a bar that printed: the name is
      limit-locked at the open and no fill is possible at a price the book would accept
      (``04`` §5.2). False where the name did not print; the plan tells the two apart by the null
      ``open``, which is its own skip (``NO_BAR``).

    ``turnover_inr`` is computed here rather than read from ``ohlcv_daily.turnover``: ``04`` §3.5's
    ₹5 crore is a rupee number about an exchange print, and the stored turnover column is not
    guaranteed to be present for every instrument-day in the plant's history. One definition, so a
    page and a test cannot disagree about what "₹5 crore of turnover" means.
    """
    require_columns(bars)
    frame = _prepare(densify(bars, calendar))
    data, scan, entry, breadth = config.data, config.scan, config.entry, config.breadth
    week_key, month_key = bucket_keys()

    frame = frame.with_columns(
        week_key,
        month_key,
        (pl.col("close_raw") * pl.col("volume")).alias("turnover_inr"),
    )
    return frame.with_columns(
        pl.col("volume")
        .rolling_mean(scan.vol_sma_bars, min_samples=data.min_samples(scan.vol_sma_bars))
        .over(_OVER)
        .alias("vol_sma"),
        pl.col("turnover_inr")
        .rolling_mean(
            entry.turnover_avg_bars, min_samples=data.min_samples(entry.turnover_avg_bars)
        )
        .over(_OVER)
        .alias("turnover_avg_20"),
        pl.col("close")
        .rolling_mean(breadth.dma_bars, min_samples=data.min_samples(breadth.dma_bars))
        .over(_OVER)
        .alias("sma_dma"),
        (
            pl.col("open").is_not_null()
            & (pl.col("open") == pl.col("high"))
            & (pl.col("high") == pl.col("low"))
        )
        .fill_null(value=False)
        .alias("limit_locked"),
    )
