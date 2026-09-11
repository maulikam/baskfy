"""TW9: how far a fresh run sits from the study, and when that is worth saying out loud.

``03`` §9 gives ``tw_backtest_run.drift`` three deltas and a flag, and ``05`` §3 turns the flag
into a banner **that names both numbers**. This module is the arithmetic, alone and pure, because
three things read it: the job that writes the row, the terminal tool that prints it, and the tests.

**Why one CAGR point.** The study's own numbers move by about a tenth of a point under a rounding
change, and ``01`` §7's neighbourhood cases move by a few tenths under a real parameter change. A
point is comfortably outside both, so a flag means something happened that is not arithmetic — the
bars changed, or the code did. A tighter threshold would fire on housekeeping; a looser one would
let a genuine break through as "close enough".

**The flag is not a verdict.** It says the two numbers stopped agreeing, not which one is wrong.
On this sleeve there are four differences that are *known* before the first run and are expected to
show up in the delta rather than be corrected out of it (DECISIONS-TW **TW2.13**): the exchange's
₹0.05 tick against the study's paisa, the shipped ₹5 crore liquidity floor against the study's ₹2
crore, real corporate actions where the research panel had none, and whatever the plant's own
history holds that the export did not. The banner asks a person to explain the difference; it never
prefers either number, and nothing here widens a tolerance to make one disappear.

**Every field leaves as a decimal string.** ``05`` §3's page formats percentages from decimal
strings so that a figure is rounded exactly once, in one place; a float in the JSONB would be
rounded again by whatever read it.
"""

from __future__ import annotations

import dataclasses
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from baskfy_core.twt.published import PUBLISHED, PublishedStudy

#: ``03`` §9: flagged above one CAGR point, in either direction. A run that *beats* the study by
#: two points is exactly as suspicious as one that misses it by two, and for the same reason.
DRIFT_CAGR_POINTS: Final = Decimal("1.0")

#: Two decimals — the precision ``01`` §6 prints and the page displays. A run flagged for a
#: difference nobody can see on the page is a difference nobody can explain.
_POINTS: Final = Decimal("0.01")

__all__ = ["DRIFT_CAGR_POINTS", "Drift", "compare"]


def _points(value: float | Decimal) -> Decimal:
    """A percentage point, to two places, without going through a binary float twice."""
    return Decimal(str(value)).quantize(_POINTS, rounding=ROUND_HALF_UP)


@dataclasses.dataclass(frozen=True, slots=True)
class Drift:
    """The three deltas, the flag, and **both** numbers the banner has to name.

    Every delta is **this run minus the study**, so a negative ``cagr_pct_delta`` means the re-run
    did worse than the published figure. Signed, not absolute: "3.4 points below" and "3.4 points
    above" need different sentences from whoever reads the banner.

    ``published_cagr_pct`` and ``run_cagr_pct`` ride along because ``05`` §3's warning names both,
    and a page that had to add the delta back onto the published figure would be re-deriving a
    number this row already knows.
    """

    cagr_pct_delta: Decimal
    max_dd_pct_delta: Decimal
    trades_delta: int
    flagged: bool
    published_cagr_pct: Decimal
    run_cagr_pct: Decimal
    threshold_cagr_points: Decimal = DRIFT_CAGR_POINTS

    def to_json(self) -> dict[str, object]:
        """The JSONB shape ``tw_backtest_run.drift`` stores and ``05`` §3 reads."""
        return {
            "cagr_pct_delta": str(self.cagr_pct_delta),
            "max_dd_pct_delta": str(self.max_dd_pct_delta),
            "trades_delta": self.trades_delta,
            "flagged": self.flagged,
            "published_cagr_pct": str(self.published_cagr_pct),
            "run_cagr_pct": str(self.run_cagr_pct),
            "threshold_cagr_points": str(self.threshold_cagr_points),
        }


def compare(
    *,
    cagr_pct: float | Decimal,
    max_drawdown_pct: float | Decimal,
    trades: int,
    study: PublishedStudy = PUBLISHED,
    threshold: Decimal = DRIFT_CAGR_POINTS,
) -> Drift:
    """This run against the study, in percentage points."""
    run_cagr = _points(cagr_pct)
    published_cagr = _points(study.cagr_pct)
    delta = run_cagr - published_cagr
    return Drift(
        cagr_pct_delta=delta,
        max_dd_pct_delta=_points(max_drawdown_pct) - _points(study.max_drawdown_pct),
        trades_delta=trades - study.trades,
        flagged=abs(delta) > threshold,
        published_cagr_pct=published_cagr,
        run_cagr_pct=run_cagr,
        threshold_cagr_points=threshold,
    )
