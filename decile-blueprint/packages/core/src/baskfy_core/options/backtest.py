"""The backtest engine — one sleeve, one tier, one row (``04`` §13; ``07`` §4).

Engine only (OP1): the day loop, the funnel, the R statistics, the caveats and the Tier 2 model
quote. Each sleeve's day function — its gate, trigger, structure and exits — is OP4's and is
passed in; the tools and the worker task are OP12's.

What the engine enforces rather than trusts:

* **One sleeve and one tier per run** (§13.4). ``run`` takes exactly one of each and the result
  carries them; there is no function that combines two results.
* **Tier 1 has no P&L** (§13.1): a Tier 1 day outcome that carries rupees raises.
* **The caveat is on the row**, verbatim from ``07`` / condor ``07`` §4 — so a later edit to the
  document never silently changes what an old run claimed.
* **The Tier 3 sample banner** stays until ``tier3_min_sessions`` observed sessions (§13.3).
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from baskfy_core.options.config import CostRates, OptionType, Sleeve, SleeveGroup, group_of
from baskfy_core.options.costs import synthetic_half_spread
from baskfy_core.options.greeks import black76_price
from baskfy_core.options.journal import max_drawdown

_HUNDRED = Decimal(100)


class Tier(StrEnum):
    SIGNALS = "1"
    MODELLED = "2"
    OBSERVED = "3"


#: ``07`` §4, Tier 1's "must say".
TIER1_CAVEAT = "No P&L. This measures selectivity, not profitability."
#: Condor ``07`` §4, verbatim — Tier 2 for every sleeve.
TIER2_CAVEAT = (
    "Prices are modelled, not observed. Real expiry-day premiums, skew and slippage differ from "
    "a flat-VIX Black\u2013Scholes; treat the P&L as a shape, not a number."
)
#: ``04`` §13.2's extra sentence for O2, verbatim.
TIER2_O2_CAVEAT = (
    "A bought option's model price ignores intraday IV changes; real premiums often fall after "
    "the open and on a move's reversal. Expect Tier 2 to flatter O2."
)


def caveats(
    sleeve: Sleeve, tier: Tier, observed_sessions: int, min_sessions: int
) -> tuple[str, ...]:
    """The text a run's row stores and its card renders (``07`` §4)."""
    if tier is Tier.SIGNALS:
        return (TIER1_CAVEAT,)
    if tier is Tier.MODELLED:
        if group_of(sleeve) is SleeveGroup.O2:
            return (TIER2_CAVEAT, TIER2_O2_CAVEAT)
        return (TIER2_CAVEAT,)
    banner = sample_banner(observed_sessions, min_sessions)
    sample = f"Observed sample: {observed_sessions} sessions."
    return (sample, banner) if banner else (sample,)


def sample_banner(observed_sessions: int, min_sessions: int) -> str | None:
    """Tier 3's "insufficient sample" banner below ``tier3_min_sessions`` (§13.3)."""
    if observed_sessions >= min_sessions:
        return None
    return f"Insufficient sample: {observed_sessions} of {min_sessions} observed sessions."


@dataclass(frozen=True, slots=True)
class DayOutcome:
    """What a sleeve's day function returns for one session.

    A skipped day carries its reason; a traded day carries its R (and ₹ from Tier 2 up). A Tier 1
    traded day carries neither — it is a signal, and ``move_points`` may record the index's
    MFE/MAE after the trigger.
    """

    trade_date: dt.date
    traded: bool
    skip_reason: str | None = None
    net_pnl_inr: Decimal | None = None
    r_multiple: Decimal | None = None
    closed_reason: str | None = None
    trigger_time: dt.time | None = None
    mfe_points: Decimal | None = None
    mae_points: Decimal | None = None


@dataclass(frozen=True, slots=True)
class BacktestResult:
    sleeve: Sleeve
    tier: Tier
    date_from: dt.date | None
    date_to: dt.date | None
    sessions: int
    traded: int
    skipped_by_reason: tuple[tuple[str, int], ...]
    by_year: tuple[tuple[int, int, int], ...]
    win_rate: Decimal | None
    expectancy_r: Decimal | None
    net_pnl_inr: Decimal | None
    max_drawdown_r: Decimal | None
    caveats: tuple[str, ...]


def run[D](
    sleeve: Sleeve,
    tier: Tier,
    days: Iterable[D],
    decide: Callable[[D], DayOutcome],
    *,
    min_sessions: int,
) -> BacktestResult:
    """Run ``decide`` over ``days`` in order and aggregate one row.

    ``by_year`` is ``(year, sessions, traded)``. P&L statistics exist only from Tier 2 up; a Tier 1
    outcome with rupees or R raises, and a Tier 2/3 traded outcome without R raises.
    """
    outcomes = [decide(day) for day in days]
    ordered = sorted(outcomes, key=lambda outcome: outcome.trade_date)
    for outcome in ordered:
        carries_pnl = outcome.net_pnl_inr is not None or outcome.r_multiple is not None
        if tier is Tier.SIGNALS and carries_pnl:
            raise ValueError("Tier 1 measures signals; it has no P&L (04 §13.1)")
        if tier is not Tier.SIGNALS and outcome.traded and outcome.r_multiple is None:
            raise ValueError(f"a Tier {tier.value} traded day needs its R")
    traded = [o for o in ordered if o.traded]
    skipped = Counter(o.skip_reason or "UNSPECIFIED" for o in ordered if not o.traded)
    years: dict[int, list[int]] = {}
    for outcome in ordered:
        tally = years.setdefault(outcome.trade_date.year, [0, 0])
        tally[0] += 1
        tally[1] += int(outcome.traded)
    rs = [o.r_multiple for o in traded if o.r_multiple is not None]
    has_pnl = tier is not Tier.SIGNALS
    return BacktestResult(
        sleeve=sleeve,
        tier=tier,
        date_from=ordered[0].trade_date if ordered else None,
        date_to=ordered[-1].trade_date if ordered else None,
        sessions=len(ordered),
        traded=len(traded),
        skipped_by_reason=tuple(sorted(skipped.items())),
        by_year=tuple((year, t[0], t[1]) for year, t in sorted(years.items())),
        win_rate=Decimal(sum(1 for r in rs if r > 0)) / len(rs) if has_pnl and rs else None,
        expectancy_r=sum(rs, Decimal(0)) / len(rs) if has_pnl and rs else None,
        net_pnl_inr=(
            sum((o.net_pnl_inr or Decimal(0) for o in traded), Decimal(0)) if has_pnl else None
        ),
        max_drawdown_r=max_drawdown(rs) if has_pnl else None,
        caveats=caveats(sleeve, tier, len(ordered), min_sessions),
    )


@dataclass(frozen=True, slots=True)
class ModelQuote:
    """Tier 2's quote: the model premium and the synthetic bid/ask around it."""

    model: Decimal
    bid: Decimal
    ask: Decimal


def tier2_quote(  # noqa: PLR0913 - every input of the flat-VIX model is explicit
    spot: Decimal,
    strike: Decimal,
    *,
    years: float,
    vix_close: Decimal,
    kind: OptionType,
    rate: float,
    rates: CostRates,
) -> ModelQuote:
    """``04`` §13.2: Black-76 at the previous day's India VIX as flat IV, forward = spot, and
    §6.2's synthetic half spread either side (the bid floored at zero)."""
    model = Decimal(
        repr(
            black76_price(
                float(spot), float(strike), years, rate, float(vix_close / _HUNDRED), kind
            )
        )
    )
    half = synthetic_half_spread(model, rates)
    return ModelQuote(model=model, bid=max(model - half, Decimal(0)), ask=model + half)
