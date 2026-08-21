"""An inclusive date window.

A value object rather than a pair of loose arguments because the same window is threaded through
the bar fetch, the backfill cursor and the adjustment rebuild, and a silently swapped
``start``/``end`` is the kind of bug that produces an empty result instead of an error.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DateWindow:
    start: dt.date
    end: dt.date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"window end {self.end} precedes start {self.start}")

    @classmethod
    def single(cls, day: dt.date) -> DateWindow:
        return cls(day, day)

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def __str__(self) -> str:
        return f"{self.start.isoformat()}..{self.end.isoformat()}"
