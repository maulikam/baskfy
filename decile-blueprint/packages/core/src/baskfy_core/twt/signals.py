"""The TWT-1 signal: Chartink's five lines, read point-in-time, and the event inside the state.

``docs/twt/04`` §3, and ``docs/twt/01`` §2 for the finding that decides how it is computed.

**Chartink's backtester knows Friday's close on Monday.** Its weekly and monthly candles are
evaluated as *completed* candles, so its historical export of this scan is not the set of names a
live 15:30 run produces: read point-in-time the panel reproduces 64.9 % of Chartink's stock-days at
61.5 % precision, read with look-ahead 83.1 % at 97.8 %. Everything here is the **point-in-time**
reading, because that is the scan a person can actually run at the close, and it is also what
Chartink's *live* scan shows.

**The look-ahead reading is not a configuration of this module.** :func:`tight_state` takes no
``include_current_week`` switch, there is no ``lookahead=`` keyword and there is no ``TightReading``
enum. TW2 keeps the look-ahead function in its *test* module, because ``01`` §2's 83.1 % is the
measurement that identifies the gap as Chartink's candle semantics rather than a plant defect, and
the sleeve must never be able to run it by accident. DECISIONS-TW **TW0.1**.

Every predicate is built from a field of :mod:`baskfy_core.twt.config`. **A null on either side of
a comparison is a fail, never a pass**: a name without three weekly closes has no range, and
Polars' three-valued logic would otherwise let it through a ``~(x > y)``.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Final

import polars as pl

from baskfy_core.twt.config import DEFAULT_TWT_CONFIG, SignalState, TwtConfig
from baskfy_core.twt.indicators import with_twt_indicators
from baskfy_core.vbt.calendar import SessionCalendar

_OVER: Final = "instrument_id"
_DATE: Final = "date"
_WEEK_KEY: Final = "week_key"
_MONTH_KEY: Final = "month_key"
#: Position of a session's ISO week among the week buckets **the data holds**, ascending. ``04``
#: §3.2: "the two week-buckets immediately before the current one *in the data*, not the two
#: calendar weeks before it". On NSE the two coincide; a whole ISO week with no session would make
#: them differ, and the data's own answer is the one a screener would give.
WEEK_POSITION: Final[str] = "week_pos"
_LAST_CLOSE: Final = "_last_close_of_week"

#: The columns :func:`weekly_closes` adds are ``weekly_close_0`` … ``weekly_close_{n-1}``.
WEEKLY_CLOSE_PREFIX: Final[str] = "weekly_close_"

#: The columns :func:`with_twt_columns` adds beyond the indicator columns.
SIGNAL_COLUMNS: Final[tuple[str, ...]] = (
    WEEK_POSITION,
    "month_low_back",
    "tight_range_pct",
    "tight",
    "above_month_low",
    "tight_state",
    "sessions_out_before",
    "sessions_listed",
    "entry_event",
)


def weekly_column(back: int) -> str:
    """``weekly_close_0`` is today's close; ``weekly_close_1`` the previous week's last close."""
    return f"{WEEKLY_CLOSE_PREFIX}{back}"


def _true(expr: pl.Expr) -> pl.Expr:
    """``expr`` with null read as False — the "a null is a fail" rule, in one place."""
    return expr.fill_null(value=False)


def weekly_closes(indicated: pl.DataFrame, config: TwtConfig = DEFAULT_TWT_CONFIG) -> pl.DataFrame:
    """``W = (w0, w1, w2)``, aligned to every session (``04`` §3.2).

    * **``w0`` is today's adjusted close.** The current week is a *partial* candle and Chartink's
      live scan reads it as such. This is the point-in-time reading and it is the whole of
      ``01`` §2.
    * **``w1`` and ``w2`` are the last close of each of the two preceding weeks the calendar
      holds.** A week is an **ISO week**; "the last close" is the last **non-null** close in that
      week, which is what a screener sees; and "the preceding weeks the calendar holds" means the
      two week-buckets immediately before the current one *in the data*.

    The week buckets are numbered by their position in the sorted list of keys the panel holds, and
    the key is built from the **ISO year** (:func:`baskfy_core.twt.indicators.bucket_keys`). That is
    the whole of the year-boundary case: ISO week 1 of the next year is *after* week 52, a key of
    ``calendar_year x 100 + iso_week`` would sort it before, and a session in the last days of
    December would then be handed the previous January's closes.

    A week bucket in which an instrument printed no bar leaves that instrument's ``w`` **null** for
    the sessions that read it. It is not skipped over to an earlier week: a screener asked for
    "last week's close" of a name that did not trade last week has no answer, and inventing one
    from a fortnight ago would manufacture a tight base out of an absence.
    """
    scan = config.scan
    weeks = (
        indicated.select(_WEEK_KEY)
        .unique()
        .sort(_WEEK_KEY)
        .with_row_index(name=WEEK_POSITION)
        .with_columns(pl.col(WEEK_POSITION).cast(pl.Int64))
    )
    frame = indicated.join(weeks, on=_WEEK_KEY, how="left").with_columns(
        pl.col("close").alias(weekly_column(0))
    )
    last_of_week = (
        frame.filter(pl.col("close").is_not_null())
        .group_by([_OVER, WEEK_POSITION])
        .agg(pl.col("close").sort_by(_DATE).last().alias(_LAST_CLOSE))
    )
    for back in range(1, scan.tight_weeks):
        earlier = last_of_week.with_columns(
            (pl.col(WEEK_POSITION) + back).alias(WEEK_POSITION)
        ).rename({_LAST_CLOSE: weekly_column(back)})
        frame = frame.join(earlier, on=[_OVER, WEEK_POSITION], how="left")
    return frame


def month_low_back(indicated: pl.DataFrame, config: TwtConfig = DEFAULT_TWT_CONFIG) -> pl.DataFrame:
    """``month_low_back`` — the low of the calendar month three months back (``04`` §3.3).

    The **minimum adjusted low over every session of the calendar month**
    ``scan.month_low_months_back`` [3] months before the session's own month. January 2026's
    sessions read October 2025's low; March reads December, which is the boundary TW1's test walks
    because a month key of ``year x 12 + month - 1`` is the only expression that crosses it without
    arithmetic on the year.

    A session whose month-3 has **no bar in the panel** has no value and is therefore not in the
    state — and so does one whose month-3 exists in the panel but in which *this instrument* did
    not print. Both are a left join that finds nothing, which is the honest answer to "what was
    this name's low three months ago" when the name was not trading.

    Measured alternatives, all worse against Chartink's export: two months back, four months back,
    and a rolling 63-session low.
    """
    scan = config.scan
    lows = (
        indicated.filter(pl.col("low").is_not_null())
        .group_by([_OVER, _MONTH_KEY])
        .agg(pl.col("low").min().alias("month_low_back"))
    )
    reads = lows.with_columns((pl.col(_MONTH_KEY) + scan.month_low_months_back).alias(_MONTH_KEY))
    return indicated.join(reads, on=[_OVER, _MONTH_KEY], how="left")


def tight_state(framed: pl.DataFrame, config: TwtConfig = DEFAULT_TWT_CONFIG) -> pl.DataFrame:
    """Chartink's five lines, read literally on the closed daily bar (``04`` §3.1).

    ============================================  =========================================
    ``close_raw > scan.min_close_raw``             line 1, the **exchange print**
    ``close >= month_low_multiple x month_low``    line 2, the adjusted series
    ``(max(W) / min(W) - 1) x 100 <= band``        line 3, the three weekly closes
    *market cap > 1*                               line 4, **not implemented** — a no-op in
                                                   Chartink and a no-op here
    ``vol_sma >= scan.min_vol_sma``                line 5, the adjusted volume average
    ============================================  =========================================

    Line 1 reads ``close_raw``: a ₹28 name that a 1:2 split makes ₹56 in the adjusted series did not
    clear Chartink's line. Lines 2, 3 and 5 read the adjusted series, because they compare a price
    to another price of a different date, or a volume to an average of volumes.

    The state is ``line 1 and line 2 and line 3 and line 5 and not ETF``. **A name with any input
    missing is not in the state**; there is no "assume true when unknown" branch.

    ``abs()`` is not applied to the range and is not needed: ``max >= min > 0`` by construction.
    """
    scan = config.scan
    columns = [weekly_column(back) for back in range(scan.tight_weeks)]
    have_all = pl.all_horizontal([pl.col(name).is_not_null() for name in columns])
    widest = pl.max_horizontal([pl.col(name) for name in columns])
    narrowest = pl.min_horizontal([pl.col(name) for name in columns])
    frame = framed.with_columns(
        pl.when(have_all)
        .then((widest / narrowest - 1) * 100)
        .otherwise(None)
        .alias("tight_range_pct")
    )
    return frame.with_columns(
        _true(have_all & (pl.col("tight_range_pct") <= scan.tight_band_pct)).alias("tight"),
        _true(pl.col("close") >= pl.col("month_low_back") * scan.month_low_multiple).alias(
            "above_month_low"
        ),
    ).with_columns(
        _true(
            (pl.col("close_raw") > scan.min_close_raw)
            & (pl.col("vol_sma") >= scan.min_vol_sma)
            & pl.col("tight")
            & pl.col("above_month_low")
            & ~pl.col("is_etf")
        ).alias("tight_state")
    )


def entry_events(stated: pl.DataFrame, config: TwtConfig = DEFAULT_TWT_CONFIG) -> pl.DataFrame:
    """The first session of a state (``04`` §3.4).

    About 50 names hold the state on an average day and the median stay is five sessions, so buying
    the state means buying the same name repeatedly. The tradable object is the **entry**:
    ``tight_state[i, t]`` is true and ``tight_state[i, t-k]`` is false for every
    ``k in 1 … entry_min_sessions_out`` [5]. Measured: 10 sessions gives 19.7 % CAGR at -27 %,
    20 gives 15.8 % at -32 %.

    Three columns are added, and the middle one is the reason the other two are honest:

    * ``sessions_out_before`` — how many sessions **immediately before this one** the state was
      false, ``null`` when the name has never been in the state at all. It is the research's own
      ``since_out`` counter to the session: state true at ``t-1`` gives 0, and five sessions out
      gives 5. Stored beside every signal so any past session can be re-read under a different gap
      without re-detection (DECISIONS-TW TW0.6's reversal path).
    * ``sessions_listed`` — sessions of the run's calendar since **this instrument's first printed
      bar**, inclusive of today.
    * ``entry_event`` — the state, the gap, and the history.

    **A fresh listing has no entry event** (DECISIONS-TW **TW0.6**). "Was false for five sessions"
    is a claim about five sessions that exist, and a name whose first ever bar satisfies the scan
    has no base to be tight in — three weekly closes of which two do not exist is not a 3 % range.
    The research's implementation seeds its sessions-out counter at a large number and would fire
    on a listing day; this one requires ``entry_min_sessions_out`` sessions of existing history
    before ``t``, which puts a new name's first possible entry on its sixth session.
    """
    gap = config.entry.entry_min_sessions_out
    position = pl.int_range(pl.len())
    last_in_state = (
        pl.when(pl.col("tight_state")).then(position).otherwise(None).forward_fill().shift(1)
    )
    listed = (pl.col("close").is_not_null().cum_sum() > 0).cum_sum()
    frame = stated.sort([_OVER, _DATE]).with_columns(
        (position - last_in_state - 1).over(_OVER).alias("sessions_out_before"),
        listed.over(_OVER).cast(pl.Int64).alias("sessions_listed"),
    )
    long_enough_out = pl.col("sessions_out_before").is_null() | (
        pl.col("sessions_out_before") >= gap
    )
    return frame.with_columns(
        _true(
            pl.col("tight_state") & long_enough_out & (pl.col("sessions_listed") - 1 >= gap)
        ).alias("entry_event")
    )


def with_twt_columns(
    bars: pl.DataFrame,
    calendar: SessionCalendar,
    config: TwtConfig = DEFAULT_TWT_CONFIG,
) -> pl.DataFrame:
    """Bars in, the whole detection out: indicators, the weekly closes, the month-3 low, the state
    and the entry event.

    The one entry point the nightly job and the backtest share, so that a plan and a study can
    never be looking at two different definitions of the same session.
    """
    indicated = with_twt_indicators(bars, calendar, config)
    framed = month_low_back(weekly_closes(indicated, config), config)
    return entry_events(tight_state(framed, config), config)


def liquidity_floor(
    config: TwtConfig = DEFAULT_TWT_CONFIG, floor_inr: Decimal | None = None
) -> pl.Expr:
    """``turnover_avg_20 >= the floor`` — ``04`` §3.5, with a null read as a fail.

    ``floor_inr`` exists for **exactly one caller**: TW2's golden parameter set, which passes
    ``EntryConfig.research_min_turnover_inr`` [₹2 crore] because the research's headline used it.
    The detector, the plan and every page pass nothing and get ``min_turnover_inr`` [₹5 crore].
    """
    floor = config.entry.min_turnover_inr if floor_inr is None else floor_inr
    return _true(pl.col("turnover_avg_20") >= floor)


def signal_mask(
    detected: pl.DataFrame,
    config: TwtConfig = DEFAULT_TWT_CONFIG,
    floor_inr: Decimal | None = None,
) -> pl.Series:
    """A boolean column over ``detected``: is this row a full TWT-1 signal?

    The backtest's own entry point — it wants a mask over the whole panel, not a filtered frame.
    """
    return (
        detected.select(
            (pl.col("entry_event") & liquidity_floor(config, floor_inr)).alias("signal")
        )
        .to_series()
        .fill_null(value=False)
    )


def detect_signals(
    detected: pl.DataFrame,
    as_of: dt.date | None = None,
    config: TwtConfig = DEFAULT_TWT_CONFIG,
    floor_inr: Decimal | None = None,
) -> pl.DataFrame:
    """Every entry event of the frame — ``SIGNAL`` rows and ``SCAN_ONLY`` rows, ranked.

    ``as_of`` restricts the answer to one session, which is what the nightly job wants; the
    backtest passes ``None`` and gets the whole history in one pass.

    **An entry event below the liquidity floor is a ``SCAN_ONLY`` row, never a dropped one**
    (``04`` §3.5, ``03`` §3): the funnel is the argument for the floor, and a system that stores
    only what it accepted cannot show a person what it passed over.

    The rank key is the **signal session's own** rupee turnover (``04`` §6.3, DECISIONS-TW TW0.2),
    and the order is ``rank_key`` descending then ``symbol`` ascending — a total order, so two runs
    of the same session produce the same plan.
    """
    frame = detected if as_of is None else detected.filter(pl.col(_DATE) == as_of)
    return (
        frame.filter(pl.col("entry_event"))
        .with_columns(
            pl.col("turnover_inr").alias("rank_key"),
            pl.when(liquidity_floor(config, floor_inr))
            .then(pl.lit(SignalState.SIGNAL.value))
            .otherwise(pl.lit(SignalState.SCAN_ONLY.value))
            .alias("signal_state"),
        )
        .sort([_DATE, "rank_key", "symbol"], descending=[False, True, False], nulls_last=True)
    )
