"""The study's own answers, as a record (``docs/twt/01-method.md`` §6-§7).

These are **not** parameters. Nothing here changes what the strategy does; every number is a
result the research measured once, and the reason they sit in the core rather than in a page is
that three consumers read them and none of them should be allowed to transcribe:

* ``/twt/backtest`` puts the run beside the published figure (``05`` §3);
* TW9's drift flag subtracts a fresh CAGR from :attr:`PublishedStudy.cagr_pct` and raises a banner
  past a point;
* ``tools/twt/backtest.py`` prints the comparison from a terminal.

The discipline that makes that safe is the one ``baskfy_core.vbt.published`` established: **the
goldens read the study's own file and this module is checked against it**
(``packages/core/tests/fixtures/twt/golden_metrics.json``, a byte-for-byte copy of
``research/tight-close/out/final_metrics.json``), so a number that drifted here fails rather than
quietly becoming the new truth.

The caveats belong with the numbers and ``01`` §8 carries them in full; ``05`` §3 renders them
**above** the figures. The short form, because a number without it invites the wrong use: **164
trades**, ten of which are 53 % of the gross profit, three years carrying the CAGR on 12-15 trades
each, a one-year average hold — perhaps fifteen independent observations of the book — modelled
fills at 25 bps a side, one country, one regime, 8.9 years, and none of it at the ₹25 lakh the
sleeve will actually run.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Final

__all__ = ["PUBLISHED", "PublishedStudy"]


@dataclasses.dataclass(frozen=True, slots=True)
class PublishedStudy:
    """``out/final_metrics.json``, typed. Every field is a measurement, never a setting."""

    #: The window the study ran over, and the reason ``years`` is 8.9 rather than nine.
    start: str
    end: str
    years: float
    cagr_pct: float
    max_drawdown_pct: float
    calmar: float
    sharpe: float
    trades: int
    win_rate_pct: float
    profit_factor: float
    avg_win_pct: float
    avg_loss_pct: float
    avg_trade_pct: float
    avg_hold_sessions: float
    exposure_pct: float
    final_equity_inr: str
    #: ``01`` §6's two halves. The spread — 11.1 % in sample against 36.1 % out — is the single
    #: most important caveat on the headline, and a page showing 20.9 % without it is showing an
    #: average of two different worlds. ``01`` §8 says the in-sample half is the guide.
    in_sample_cagr_pct: float
    in_sample_dd_pct: float
    out_of_sample_cagr_pct: float
    out_of_sample_dd_pct: float
    #: **Not from** ``final_metrics.json``: ``01`` §7's sensitivity table (``gate … none``), which
    #: is the only argument for the breadth gate — 17.2 % at -43 % ungated against 20.9 % at
    #: -24.7 % gated. Carried here so ``05`` §3's "what the gate was worth" cell has a published
    #: figure to sit beside the one a fresh run computes.
    gate_off_cagr_pct: float
    gate_off_max_drawdown_pct: float
    #: Also **not** from ``final_metrics.json``: ``01`` §7's ``turnover >= ₹5 cr`` row. It matters
    #: more than any other line of that table, because ``01`` §6's headline was produced at the
    #: research's ₹2 crore floor and **the sleeve ships at ₹5 crore** (``04`` §3.5, DECISIONS-TW
    #: **TW0.3**). A run over the plant's bars is therefore measured against a headline it was
    #: never asked to reproduce, and this is the row that says what it *was* asked to reproduce.
    #: TW9 measured 22.17 / -26.47 / 169 on the plant against these three (DECISIONS-TW TW9.3).
    shipped_floor_cagr_pct: float
    shipped_floor_max_drawdown_pct: float
    shipped_floor_trades: int

    @property
    def cagr(self) -> Decimal:
        """The headline as a Decimal, for a page that renders money-shaped numbers exactly."""
        return Decimal(str(self.cagr_pct))


#: ``01`` §6, read off ``research/tight-close/out/final_metrics.json`` on 11 Sep 2026, with the
#: two gate-off figures from ``01`` §7's ``out/final_sensitivity.csv`` row.
PUBLISHED: Final = PublishedStudy(
    start="2017-10-16",
    end="2026-09-09",
    years=8.9,
    cagr_pct=20.92,
    max_drawdown_pct=-24.7,
    calmar=0.85,
    sharpe=1.21,
    trades=164,
    win_rate_pct=40.9,
    profit_factor=2.71,
    avg_win_pct=55.57,
    avg_loss_pct=-12.35,
    avg_trade_pct=15.4,
    avg_hold_sessions=104.6,
    exposure_pct=78.0,
    final_equity_inr="5413123.00",
    in_sample_cagr_pct=11.1,
    in_sample_dd_pct=-20.8,
    out_of_sample_cagr_pct=36.1,
    out_of_sample_dd_pct=-24.7,
    gate_off_cagr_pct=17.2,
    gate_off_max_drawdown_pct=-43.0,
    shipped_floor_cagr_pct=22.5,
    shipped_floor_max_drawdown_pct=-27.0,
    shipped_floor_trades=169,
)
