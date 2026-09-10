"""VB9: how far a fresh run sits from the study, and when that is worth saying out loud.

`03` §8 gives `vb_backtest_run.drift` four fields — three deltas and a flag — and `05` §2 turns
the flag into a banner. This module is the arithmetic, alone and pure, because three things read
it: the task that writes the row, the page that renders the banner, and the tests.

**Why one CAGR point.** The study's own numbers move by about a tenth of a point under a
rounding change, and the neighbourhood cases of `01` §4 move by a few tenths under a real
parameter change. A point is comfortably outside both, so a flag means something happened that
is not arithmetic — the bars changed, or the code did. A tighter threshold would fire on
housekeeping; a looser one would let a genuine break through as "close enough".

**The flag is not a verdict.** It says the two numbers stopped agreeing, not which one is wrong.
`05` §2's banner says exactly that, and asks a person to explain the difference rather than to
prefer either number.
"""

from __future__ import annotations

import dataclasses
from typing import Final

from baskfy_core.vbt.published import PUBLISHED, PublishedStudy

#: `03` §8: flagged above one CAGR point, in either direction. A run that *beats* the study by
#: two points is exactly as suspicious as one that misses it by two, and for the same reason.
DRIFT_CAGR_POINTS: Final[float] = 1.0

__all__ = ["DRIFT_CAGR_POINTS", "Drift", "compare"]


@dataclasses.dataclass(frozen=True, slots=True)
class Drift:
    """The three deltas and the flag, in the shape `vb_backtest_run.drift` stores.

    Every delta is **this run minus the study**, so a negative `cagr_pct_delta` means the re-run
    did worse than the published number. Signed, not absolute: "3.4 points below" and "3.4 points
    above" need different sentences from whoever reads the banner.
    """

    cagr_pct_delta: float
    max_dd_pct_delta: float
    trades_delta: int
    flagged: bool
    threshold_cagr_points: float = DRIFT_CAGR_POINTS

    def to_json(self) -> dict[str, object]:
        return {
            "cagr_pct_delta": self.cagr_pct_delta,
            "max_dd_pct_delta": self.max_dd_pct_delta,
            "trades_delta": self.trades_delta,
            "flagged": self.flagged,
            "threshold_cagr_points": self.threshold_cagr_points,
        }


def compare(
    *,
    cagr_pct: float,
    max_drawdown_pct: float,
    trades: int,
    study: PublishedStudy = PUBLISHED,
    threshold: float = DRIFT_CAGR_POINTS,
) -> Drift:
    """This run against the study.

    Rounded to one decimal, which is the precision the study's own file stores and the page
    displays. Comparing unrounded floats would let a run be flagged for a difference nobody can
    see on the page, and a number a reader cannot see is a number they cannot explain.
    """
    cagr_delta = round(cagr_pct - study.cagr_pct, 2)
    return Drift(
        cagr_pct_delta=cagr_delta,
        max_dd_pct_delta=round(max_drawdown_pct - study.max_drawdown_pct, 2),
        trades_delta=trades - study.trades,
        flagged=abs(cagr_delta) > threshold,
        threshold_cagr_points=threshold,
    )
