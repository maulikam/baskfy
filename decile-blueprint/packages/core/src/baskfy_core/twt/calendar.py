"""The run's own trading calendar: thin sessions removed (``docs/twt/04`` §2.1).

A muhurat session, or a special Saturday the exchange runs as a test, is a *session* to the
exchange and is **not a trading day for a daily strategy**: about 200 names print against about
1,900, and a single such column poisons every 50- and 200-session rolling window that spans it.

**The arithmetic is VBT-1's and is called, not copied.** ``docs/twt/06`` TW1 asks for the thin
session rule "shared with VBT-1's implementation, TWT's own thresholds", for the same reason
``04`` §4.4 gives about breadth: two implementations of one measurement is the one thing that can
make two pages disagree about the same day. What is *not* shared is the numbers — TWT's own
:class:`~baskfy_core.twt.config.DataConfig` is translated field by field below, **spelled out**, so
a VBT recalibration cannot silently change which sessions this sleeve counts.

The rule is the contract. The six dates it finds on the 2017 -> history (``2017-10-19``,
``2018-11-07``, ``2024-01-20``, ``2024-03-02``, ``2024-05-18``, ``2025-02-01``) are a **test's
expectation**, not a list this module carries: a seventh such session in 2027 must be found by the
rule, not by an edit.

Pure: a frame in, a frame out. No calendar table is read here — the caller hands over the bars.
"""

from __future__ import annotations

import datetime as dt

import polars as pl

from baskfy_core.twt.config import DEFAULT_TWT_CONFIG, DataConfig, TwtConfig
from baskfy_core.vbt.calendar import SessionCalendar
from baskfy_core.vbt.calendar import build_calendar as _vbt_build_calendar
from baskfy_core.vbt.calendar import drop_thin_sessions as _vbt_drop_thin_sessions
from baskfy_core.vbt.calendar import session_counts as _vbt_session_counts
from baskfy_core.vbt.calendar import thin_sessions as _vbt_thin_sessions
from baskfy_core.vbt.config import DataConfig as VbtDataConfig
from baskfy_core.vbt.config import VbtConfig

__all__ = [
    "SessionCalendar",
    "as_shared_config",
    "build_calendar",
    "drop_thin_sessions",
    "session_counts",
    "thin_sessions",
]


def as_shared_config(config: DataConfig) -> VbtConfig:
    """TWT's calendar thresholds, in the shape the shared implementation takes.

    Every field is written out rather than inherited. That is the point of the function: a reader
    who wants to know which numbers this sleeve's calendar uses reads them here, and a change to
    VBT-1's defaults cannot reach them.
    """
    return VbtConfig(
        data=VbtDataConfig(
            thin_session_min_share=config.thin_session_min_share,
            thin_session_window_bars=config.thin_session_window_bars,
            thin_session_min_periods=config.thin_session_min_periods,
            rolling_min_share=config.rolling_min_share,
        )
    )


def session_counts(bars: pl.DataFrame) -> pl.DataFrame:
    """``(date, traded)`` — how many instruments printed a bar on each session, sorted."""
    return _vbt_session_counts(bars)


def thin_sessions(bars: pl.DataFrame, config: DataConfig | None = None) -> list[dt.date]:
    """The sessions ``04`` §2.1 removes, in order.

    A session is thin when its traded-name count is below ``thin_session_min_share`` of the
    **centred** rolling median of that count over ``thin_session_window_bars`` sessions. Centred,
    because the comparison a human makes is with the sessions on either side of it, not with the
    fortnight before it.
    """
    cfg = config or DEFAULT_TWT_CONFIG.data
    return _vbt_thin_sessions(bars, as_shared_config(cfg).data)


def build_calendar(bars: pl.DataFrame, config: TwtConfig = DEFAULT_TWT_CONFIG) -> SessionCalendar:
    """Every session the bars contain, minus the thin ones."""
    return _vbt_build_calendar(bars, as_shared_config(config.data))


def drop_thin_sessions(
    bars: pl.DataFrame, config: TwtConfig = DEFAULT_TWT_CONFIG
) -> tuple[pl.DataFrame, SessionCalendar]:
    """``(bars without the thin sessions, the calendar)``.

    Called before any indicator. Everything downstream — the 50-session volume average, the
    200-session average, the 20-session turnover average, the five-session entry gap and the
    weekly and monthly buckets — counts sessions of the returned calendar and nothing else.
    """
    return _vbt_drop_thin_sessions(bars, as_shared_config(config.data))
