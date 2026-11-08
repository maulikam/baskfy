"""The run's own trading calendar: thin sessions removed (``docs/vbt/04`` §2.1).

A muhurat session, or a special Saturday the exchange runs as a test, is a *session* to the
exchange and is **not a trading day for a daily strategy**: ~200 names print against ~1,900, and
a single such column poisons every 50- and 200-session rolling window that spans it. On the
research's first run that is precisely what happened — the 200-DMA filter appeared to vanish for
most of 2024-25.

The rule below is the contract. The six dates it finds on the 2017→ history
(``2017-10-19``, ``2018-11-07``, ``2024-01-20``, ``2024-03-02``, ``2024-05-18``, ``2025-02-01``)
are a **test's expectation**, not a list this module carries: a seventh such session in 2027 must
be found by the rule, not by an edit.

Pure: a frame in, a frame out. No calendar table is read here — the caller hands over the bars.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import polars as pl

from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG, DataConfig, VbtConfig

_DATE = "date"


@dataclass(frozen=True, slots=True)
class SessionCalendar:
    """The sessions a rolling window is allowed to count, with the thin ones named."""

    sessions: tuple[dt.date, ...]
    dropped: tuple[dt.date, ...]
    #: ``date -> traded-name count``, for the funnel and for the audit of why one was dropped.
    counts: dict[dt.date, int]

    def index_of(self, session: dt.date) -> int:
        """Position of ``session`` in the calendar; ``-1`` when it is not one."""
        try:
            return self.sessions.index(session)
        except ValueError:
            return -1

    def sessions_between(self, first: dt.date, last: dt.date) -> int:
        """How many calendar sessions lie in ``(first, last]``. Holidays do not count."""
        start, end = self.index_of(first), self.index_of(last)
        if start < 0 or end < 0:
            return 0
        return end - start

    def advance(self, session: dt.date, sessions: int) -> dt.date | None:
        """The session ``sessions`` places after ``session``, or ``None`` past the end."""
        here = self.index_of(session)
        if here < 0 or here + sessions >= len(self.sessions):
            return None
        return self.sessions[here + sessions]


def session_counts(bars: pl.DataFrame) -> pl.DataFrame:
    """``(date, traded)`` — how many instruments printed a bar on each session, sorted."""
    if _DATE not in bars.columns:
        raise ValueError(f"bars need a '{_DATE}' column; got {bars.columns}")
    return (
        bars.group_by(_DATE)
        .agg(pl.len().alias("traded"))
        .sort(_DATE)
        .with_columns(pl.col("traded").cast(pl.Int64))
    )


def thin_sessions(bars: pl.DataFrame, config: DataConfig | None = None) -> list[dt.date]:
    """The sessions ``04`` §2.1 removes, in order.

    A session is thin when its traded-name count is below ``thin_session_min_share`` of the
    **centred** rolling median of that count over ``thin_session_window_bars`` sessions. Centred,
    because the comparison a human makes is with the sessions on either side of it, not with the
    fortnight before it.
    """
    cfg = config or DEFAULT_VBT_CONFIG.data
    counts = session_counts(bars)
    if counts.height == 0:
        return []
    window = cfg.thin_session_window_bars
    median = (
        counts.select(
            pl.col("traded")
            .cast(pl.Float64)
            .rolling_median(window, min_samples=cfg.thin_session_min_periods, center=True)
            .alias("median")
        )
        .to_series()
        .to_list()
    )
    verdict = []
    for row, med in zip(counts.iter_rows(named=True), median, strict=True):
        if med is None:
            continue
        if row["traded"] < cfg.thin_session_min_share * med:
            verdict.append(row[_DATE])
    return verdict


def build_calendar(bars: pl.DataFrame, config: VbtConfig = DEFAULT_VBT_CONFIG) -> SessionCalendar:
    """Every session the bars contain, minus the thin ones."""
    counts = session_counts(bars)
    dropped = set(thin_sessions(bars, config.data))
    every = [row[_DATE] for row in counts.iter_rows(named=True)]
    return SessionCalendar(
        sessions=tuple(d for d in every if d not in dropped),
        dropped=tuple(d for d in every if d in dropped),
        counts={row[_DATE]: int(row["traded"]) for row in counts.iter_rows(named=True)},
    )


def drop_thin_sessions(
    bars: pl.DataFrame, config: VbtConfig = DEFAULT_VBT_CONFIG
) -> tuple[pl.DataFrame, SessionCalendar]:
    """``(bars without the thin sessions, the calendar)``.

    Called before any indicator. Everything downstream — the 50-day volume average, the 200-DMA,
    the 21-EMA, the prior-20-session high, the three-session order window — counts sessions of
    the returned calendar and nothing else.
    """
    calendar = build_calendar(bars, config)
    if not calendar.dropped:
        return bars, calendar
    return bars.filter(~pl.col(_DATE).is_in(list(calendar.dropped))), calendar
