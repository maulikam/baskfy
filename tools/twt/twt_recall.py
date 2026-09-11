#!/usr/bin/env python
"""How much of Chartink's own export a reading of the scan reproduces (TW2).

``research/tight-close/STRATEGY.md`` §1 is the finding this measurement is the evidence for:
**Chartink's backtester evaluates a weekly candle as a completed candle**, so its historical export
of the tight-close scan knows Friday's close on Monday. Read point-in-time the panel reproduces
64.9 % of its stock-days at 61.5 % precision; read with look-ahead, 83.1 % at 97.8 %. The second
number is not a better reproduction — it is the size of the look-ahead, and house rule 5 is why the
sleeve ships the first one.

The scorer, stated plainly, because a recall number is only as honest as its definitions:

* **A stock-day is ``(session, symbol)`` and nothing else.** Not a time, not a price, not a rank.
  Two readings agree on a stock-day when both say "this name was in the scan on this session".
* **The measurement is symmetric.** ``score(a, b).recall_pct == score(b, a).precision_pct``, and
  the class asserts it rather than assuming it. Recall is "of the answer key, how much did we
  find"; precision is "of what we found, how much is in the answer key".
* **A stock-day nobody can produce still counts against recall.** A name the panel has no bar for
  on that session, or does not carry at all, is a **miss** and stays in the denominator. Dropping
  the unreachable rows would turn a data gap into a better score, which is the one thing a
  reproduction measurement must never do. :class:`MissReasons` is how the misses are *explained*;
  nothing in this module lets them be *removed*.
* **Duplicates are one stock-day.** Chartink's export lists a name once per session; a reading
  that emitted it twice would otherwise score itself twice.

Pure stdlib: no pandas, no numpy, no ``baskfy_core``. Both sides of every comparison — the
research's reading and, when TW1's core lands, the sleeve's — are scored by this one function.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

#: A stock-day: the session and the symbol. The whole match key.
StockDay = tuple[dt.date, str]

#: How many misses and spurious rows :meth:`ScoreCard.render` names before it says "and N more".
_SAMPLE: Final = 5


class MissReason(StrEnum):
    """Why a stock-day in the answer key is not in a reading (``tscan_verify.py``'s own buckets).

    Every one of these is still a **miss**. The enum exists to say what kind of miss it is, which
    is what separates "the plant has a gap" from "the rule disagrees".
    """

    #: The panel does not carry the symbol at all.
    UNKNOWN_SYMBOL = "unknown_symbol"
    #: The symbol is carried but printed no bar on that session.
    NO_BAR = "no_bar"
    #: A bar, but no 50-session volume average yet — the window is not valid under ``04`` §2.2.
    NO_VOLUME_AVERAGE = "no_volume_average"
    #: Three weekly closes, but the range is wider than 3.01 %.
    NOT_TIGHT = "not_tight"
    #: Tight, but not 30 % above the month-3 low.
    NOT_ABOVE_MONTH_LOW = "not_above_month_low"
    #: A price or volume floor, or the ETF exclusion.
    BELOW_A_FLOOR = "below_a_floor"
    #: Every input present and every line true — a genuine disagreement, and the interesting kind.
    UNEXPLAINED = "unexplained"


@dataclass(frozen=True, slots=True)
class MissReasons:
    """Counts by :class:`MissReason`. Explains misses; never removes them."""

    counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def render(self) -> str:
        if not self.counts:
            return "(not classified)"
        ordered = sorted(self.counts.items(), key=lambda item: (-item[1], item[0]))
        return ", ".join(f"{name} {count}" for name, count in ordered if count)


@dataclass(frozen=True, slots=True)
class ScoreCard:
    """One reading against one answer key."""

    #: Distinct stock-days the reading produced inside the window.
    produced: int
    #: Distinct stock-days the answer key holds inside the window.
    expected: int
    #: In both.
    matched: int
    #: In the answer key and not in the reading, sorted.
    missed: tuple[StockDay, ...]
    #: In the reading and not in the answer key, sorted.
    spurious: tuple[StockDay, ...]

    @property
    def recall_pct(self) -> float:
        """Of the answer key, how much the reading found. An empty key scores 0, never 100."""
        return 100.0 * self.matched / self.expected if self.expected else 0.0

    @property
    def precision_pct(self) -> float:
        """Of what the reading found, how much the answer key holds."""
        return 100.0 * self.matched / self.produced if self.produced else 0.0

    def render(self) -> str:
        return (
            f"produced {self.produced} expected {self.expected} both {self.matched} "
            f"recall {self.recall_pct:.1f}% precision {self.precision_pct:.1f}%"
        )

    def render_long(self, reasons: MissReasons | None = None) -> str:
        lines = [self.render()]
        lines.append(f"  missed {len(self.missed)}: {_sample(self.missed)}")
        lines.append(f"  spurious {len(self.spurious)}: {_sample(self.spurious)}")
        if reasons is not None:
            lines.append(f"  why missed: {reasons.render()}")
        return "\n".join(lines)


def _sample(pairs: tuple[StockDay, ...]) -> str:
    if not pairs:
        return "none"
    shown = ", ".join(f"{session} {symbol}" for session, symbol in pairs[:_SAMPLE])
    extra = len(pairs) - _SAMPLE
    return shown + (f", and {extra} more" if extra > 0 else "")


def score(produced: Iterable[StockDay], expected: Iterable[StockDay]) -> ScoreCard:
    """Score one reading against one answer key. Both sides are de-duplicated first."""
    mine = {(session, symbol) for session, symbol in produced}
    theirs = {(session, symbol) for session, symbol in expected}
    return ScoreCard(
        produced=len(mine),
        expected=len(theirs),
        matched=len(mine & theirs),
        missed=tuple(sorted(theirs - mine)),
        spurious=tuple(sorted(mine - theirs)),
    )


def within(measured: float, published: float, tolerance: float) -> bool:
    """``|measured - published| <= tolerance``, spelled out so a test reads as prose."""
    return abs(measured - published) <= tolerance
