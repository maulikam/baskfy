"""The look-ahead reading of the tight-close scan. **A test module, on purpose.**

``docs/twt/04`` §3.2 and DECISIONS-TW **TW0.1**: ``baskfy_core.twt.signals.tight_state`` implements
the point-in-time reading and takes **no switch** — there is no ``include_current_week``, no
``lookahead=`` keyword and no ``TightReading`` enum. TW2 needs the look-ahead number because
``docs/twt/01`` §2's 83.1 % is the measurement that identifies the gap between the sleeve and
Chartink's export as *Chartink's candle semantics* rather than a defect in the plant. So the
reading lives here, in ``packages/core/tests/``, where the sleeve cannot reach it, and
``test_twt_lookahead_recall.py`` asserts by import scan that it never leaves.

**What the reading is.** Chartink's backtester evaluates a weekly candle as a *completed* candle,
so on a Tuesday it already knows Friday's close. Written out: ``w0`` is the **last close of the
current week**, on every session of that week — the future, on purpose — where the point-in-time
reading takes today's close. ``w1`` and ``w2`` are unchanged.

Nothing here may be imported by ``baskfy_core``. It reuses the sleeve's own
:func:`~baskfy_core.twt.signals.tight_state` and :func:`~baskfy_core.twt.signals.month_low_back`
so that the *only* difference between the two readings is the one column this file rewrites; a
second copy of the five lines would make the comparison a comparison of two implementations.
"""

from __future__ import annotations

from typing import Final

import polars as pl

from baskfy_core.twt.config import DEFAULT_TWT_CONFIG, TwtConfig
from baskfy_core.twt.indicators import with_twt_indicators
from baskfy_core.twt.signals import (
    WEEK_POSITION,
    month_low_back,
    tight_state,
    weekly_column,
)
from baskfy_core.vbt.calendar import SessionCalendar

_OVER: Final = "instrument_id"
_DATE: Final = "date"
_WEEK_KEY: Final = "week_key"
_LAST_CLOSE: Final = "_last_close_of_week"


def weekly_closes_lookahead(
    indicated: pl.DataFrame, config: TwtConfig = DEFAULT_TWT_CONFIG
) -> pl.DataFrame:
    """:func:`~baskfy_core.twt.signals.weekly_closes` with ``w0`` taken from the week's **end**.

    One line differs from the sleeve's: the loop starts at ``0`` instead of ``1``, so the current
    week's own bucket is joined the same way the two before it are — which hands every session of
    a week the close that week will finish on.
    """
    scan = config.scan
    weeks = (
        indicated.select(_WEEK_KEY)
        .unique()
        .sort(_WEEK_KEY)
        .with_row_index(name=WEEK_POSITION)
        .with_columns(pl.col(WEEK_POSITION).cast(pl.Int64))
    )
    frame = indicated.join(weeks, on=_WEEK_KEY, how="left")
    last_of_week = (
        frame.filter(pl.col("close").is_not_null())
        .group_by([_OVER, WEEK_POSITION])
        .agg(pl.col("close").sort_by(_DATE).last().alias(_LAST_CLOSE))
    )
    for back in range(scan.tight_weeks):
        earlier = last_of_week.with_columns(
            (pl.col(WEEK_POSITION) + back).alias(WEEK_POSITION)
        ).rename({_LAST_CLOSE: weekly_column(back)})
        frame = frame.join(earlier, on=[_OVER, WEEK_POSITION], how="left")
    return frame


def tight_state_lookahead(
    indicated: pl.DataFrame, config: TwtConfig = DEFAULT_TWT_CONFIG
) -> pl.DataFrame:
    """The scan's five lines over the look-ahead weekly closes. The ``tight_state`` column is it."""
    return tight_state(month_low_back(weekly_closes_lookahead(indicated, config), config), config)


def with_lookahead_columns(
    bars: pl.DataFrame, calendar: SessionCalendar, config: TwtConfig = DEFAULT_TWT_CONFIG
) -> pl.DataFrame:
    """Bars in, the look-ahead state out — the mirror of ``signals.with_twt_columns``.

    There is no ``entry_event`` here and there never will be: the look-ahead reading exists to be
    *measured*, not to be traded from.
    """
    return tight_state_lookahead(with_twt_indicators(bars, calendar, config), config)
