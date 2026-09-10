"""The study's own answers, as a record (``research/volume-breakout/STRATEGY.md`` §4-§5).

These are **not** parameters. Nothing here changes what the strategy does; every number is a
result the research measured once, and the reason they are in the core rather than in a page is
that three consumers read them and none of them should be allowed to transcribe:

* ``/vbt/backtest`` puts them beside the run that just finished (``05`` §2);
* VB9's drift flag subtracts a fresh CAGR from ``CAGR_PCT`` and raises a banner past a point;
* ``tools/vbt/reproduce.py`` and ``test_vbt_goldens.py`` compare against ``out/final_metrics.json``
  directly, because a test that read this module would be checking a transcription.

That last point is the discipline that makes the rest safe: **the tests read the study's file and
this module is checked against it**, so a number that drifted here would fail rather than quietly
become the new truth.

The caveats belong with the numbers and `01` §5 carries them in full. The short form, because a
number without it invites the wrong use: fills are modelled rather than experienced, costs are
25 bps a side with no impact model, the universe carries 262 missing instrument-days, and the
whole thing is one country, one regime and 8.9 years.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Final

__all__ = ["PUBLISHED", "PublishedStudy"]


@dataclasses.dataclass(frozen=True, slots=True)
class PublishedStudy:
    """``out/final_metrics.json``, typed. Every field is a measurement, never a setting."""

    #: The window the study ran over, and the reason `years` is 8.9 rather than nine.
    start: str
    end: str
    years: float
    cagr_pct: float
    max_drawdown_pct: float
    trades: int
    win_rate_pct: float
    profit_factor: float
    avg_hold_sessions: float
    exposure_pct: float
    sharpe: float
    #: The in-sample and out-of-sample halves of `01` §4. The spread between them — 12.6% against
    #: 26.0% — is the single most important caveat on the headline, and a page that shows 18.2%
    #: without it is showing an average of two different worlds.
    in_sample_cagr_pct: float
    in_sample_dd_pct: float
    out_of_sample_cagr_pct: float
    out_of_sample_dd_pct: float
    #: How often the backtest's limit orders filled inside their three sessions: **761 fills out
    #: of 908 orders the engine actually offered** — past the session cap and the slot count,
    #: with a bar and no circuit lock. Measured on 10 Sep 2026 by
    #: `BacktestResult.fill_rate_pct()`, never quoted: DECISIONS-VB VB8.1 records that `04` §7.3
    #: had carried an invented 91% since VB0, and what the denominator has to be for the number
    #: to mean anything.
    modelled_fill_rate_pct: float

    @property
    def cagr(self) -> Decimal:
        """The headline as a Decimal, for a page that renders money-shaped numbers exactly."""
        return Decimal(str(self.cagr_pct))


#: STRATEGY §4, read off `research/volume-breakout/out/final_metrics.json` on 10 Sep 2026.
PUBLISHED: Final = PublishedStudy(
    start="2017-10-16",
    end="2026-09-09",
    years=8.9,
    cagr_pct=18.23,
    max_drawdown_pct=-27.94,
    trades=761,
    win_rate_pct=37.8,
    profit_factor=1.55,
    avg_hold_sessions=18.3,
    exposure_pct=63.2,
    sharpe=0.97,
    in_sample_cagr_pct=12.6,
    in_sample_dd_pct=-27.9,
    out_of_sample_cagr_pct=26.0,
    out_of_sample_dd_pct=-19.8,
    modelled_fill_rate_pct=83.8,
)
