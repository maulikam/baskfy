"""A sleeve's backtest runs, by tier, and Tier 1's threshold sensitivity (``06`` OP12).

``backtest.run`` aggregates one sleeve and one tier; ``replay_day`` decides one day by the live
code. This module joins them: :func:`run_tier` walks a sleeve's days through the tier's day function
into one :class:`~baskfy_core.options.backtest.BacktestResult` — its caveats on the row, verbatim
(``07`` §4) — and :func:`tier1_sensitivity` re-runs Tier 1 with each gate threshold at ±25 %
(``06`` OP12: "Tier 1 all sleeves with ±25 % sensitivity on each threshold").

One sleeve, one tier per result, always: there is no function here that combines two (§13.4).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal

from baskfy_core.options.backtest import BacktestResult, DayOutcome, Tier, run
from baskfy_core.options.config import OptionsCeilings, OptionsConfig, Sleeve
from baskfy_core.options.replay_day import SnapshotSource, priced_day, tier1_day
from baskfy_core.options.scan import DayContext, MarketDay

#: The gate thresholds each sleeve's Tier 1 sensitivity varies, as (config group, field).
THRESHOLDS: dict[Sleeve, tuple[tuple[str, str], ...]] = {
    Sleeve.O1M: (("condor_monthly", "gap_max_pct"), ("condor_monthly", "range_max_pct"),
                 ("condor_monthly", "er_max")),
    Sleeve.O1W: (("condor_weekly", "gap_max_pct"), ("condor_weekly", "range_max_pct"),
                 ("condor_weekly", "er_max")),
    Sleeve.O2: (("directional", "gap_max_pct"), ("directional", "or_max_pct"),
                ("directional", "vix_max"), ("directional", "buffer_pct")),
    Sleeve.O3A: (("expiry_setups", "o3a_range_max_pct"), ("expiry_setups", "o3a_buffer_pct"),
                 ("expiry_setups", "o3a_er_min")),
    Sleeve.O3B: (("expiry_setups", "o3b_gap_min_pct"), ("expiry_setups", "o3b_gap_max_pct")),
}  # fmt: skip
SENSITIVITY_FACTORS: tuple[Decimal, ...] = (Decimal("0.75"), Decimal("1.25"))


def min_sessions(sleeve: Sleeve, options: OptionsConfig) -> int:
    """``04`` §13.3's ``tier3_min_sessions`` for the sleeve's config group."""
    if sleeve is Sleeve.O1M:
        return options.condor_monthly.tier3_min_sessions
    if sleeve is Sleeve.O1W:
        return options.condor_weekly.tier3_min_sessions
    if sleeve is Sleeve.O2:
        return options.directional.tier3_min_sessions
    return options.expiry_setups.tier3_min_sessions


def run_tier(  # noqa: PLR0913 - the sleeve, the tier, the days, the chain and the rules
    sleeve: Sleeve,
    tier: Tier,
    days: Iterable[MarketDay],
    *,
    context: DayContext,
    options: OptionsConfig,
    ceilings: OptionsCeilings,
    source_for: Callable[[MarketDay], SnapshotSource] | None = None,
) -> BacktestResult:
    """One sleeve, one tier, over ``days`` (the sleeve's non-days are dropped, never counted)."""
    outcomes: list[DayOutcome] = []
    for market in days:
        if tier is Tier.SIGNALS:
            outcome = tier1_day(sleeve, market, context, options)
        else:
            if source_for is None:
                raise ValueError(f"Tier {tier.value} needs a snapshot source")
            outcome = priced_day(
                sleeve, market, context, source_for(market), options=options, ceilings=ceilings
            )
        if outcome is not None:
            outcomes.append(outcome)
    return run(sleeve, tier, outcomes, lambda o: o, min_sessions=min_sessions(sleeve, options))


@dataclass(frozen=True, slots=True)
class Sensitivity:
    group: str
    field: str
    factor: Decimal
    value: Decimal
    sessions: int
    signals: int


def _scaled(options: OptionsConfig, group: str, name: str, factor: Decimal) -> OptionsConfig:
    part = getattr(options, group)
    value = getattr(part, name)
    return dataclasses.replace(
        options, **{group: dataclasses.replace(part, **{name: value * factor})}
    )


def tier1_sensitivity(
    sleeve: Sleeve, days: Iterable[MarketDay], *, context: DayContext, options: OptionsConfig
) -> tuple[Sensitivity, ...]:
    """Tier 1's signal count with each threshold of the sleeve's gate at x0.75 and x1.25."""
    listed = list(days)
    out: list[Sensitivity] = []
    for group, name in THRESHOLDS[sleeve]:
        for factor in SENSITIVITY_FACTORS:
            varied = _scaled(options, group, name, factor)
            result = run_tier(sleeve, Tier.SIGNALS, listed, context=context, options=varied,
                              ceilings=OptionsCeilings())  # fmt: skip
            out.append(Sensitivity(group, name, factor, getattr(getattr(varied, group), name),
                                   result.sessions, result.traded))  # fmt: skip
    return tuple(out)


__all__ = [
    "SENSITIVITY_FACTORS",
    "THRESHOLDS",
    "Sensitivity",
    "min_sessions",
    "run_tier",
    "tier1_sensitivity",
]
