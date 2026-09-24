"""The nightly scan's arithmetic (``docs/fno/04`` §2, §3, §8, §10; ``06`` FO4).

The worker (``baskfy_worker.fno.scan``) reads the database and writes ``fo_scan``; every number
it writes is computed here, from frames and values it hands in (law 1).

**F1** — :func:`propose_condor`: the monthly condor ``04`` §2 would enter, proposed from the
bhavcopy of the session the scan read. ``F`` is the target monthly future's settle, ``sigma`` the
ATM IV on that monthly, ``T`` calendar days from the entry session to expiry ÷ 365; the credit is
priced from the legs' settles (the desk re-prices on live mids at 09:20, FO7). The liquidity the
bhavcopy can prove is checked by name — each leg printed, each short's open interest ≥
``f1_min_short_oi_lots``, each wing traded — while the live spread test of ``04`` §2 needs a quote
and stays with the plan. Then sizing (``04`` §3, never rounded up) and the round trip as a share of
the credit (``REJECTED_COST`` above 25 %). The first failure in that order is the state; every
reason found is kept, in words.

**F2** — :func:`f2_signals`: the continuous futures series re-derived over one window (FO2.8:
stored levels are anchored per night and never mixed across nights), then ``04`` §10's universe
(top ``f2_universe_turnover_pct`` of F&O stocks by the 20-session median futures turnover, ranked
as ``res_f2.py`` ranked it), breakout (close > the max of the prior 20 closes), trend (close > its
50-session average) and NIFTY's regime (NIFTY's continuous future > its own 50-session average).
The series is unitless (it compounds from 1.0), so every level the page shows is rescaled to
rupees on the session's held-contract settle, exactly as FO2.8 anchors the stored row.
:func:`propose_future` sizes and costs one candidate; :func:`allocate_capacity` applies
``f2_max_open``, one per stock and at most ``max_per_industry`` per industry, in the order given.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

import polars as pl

from baskfy_core.fno.condor import (
    ENTRY_SEQUENCE,
    CondorStrikes,
    LegRole,
    option_type_of,
    price_structure,
    select_strikes,
    sign_of,
)
from baskfy_core.fno.config import (
    F2Config,
    FnoCeilings,
    FnoConfig,
    PlanState,
    SeriesConfig,
)
from baskfy_core.fno.costs import CondorCost, FutureCharges, condor_round_trip, future_round_trip
from baskfy_core.fno.exits import f2_initial_stop, gtt_trigger
from baskfy_core.fno.series import continuous_futures
from baskfy_core.fno.sizing import FoSizing, size
from baskfy_core.options.config import CostRates, Mode, OptionType

_HUNDRED = Decimal(100)
_CENT = Decimal("0.01")
#: The index underlyings whose continuous future is F2's regime gauge (``04`` §10).
REGIME_UNDERLYING = "NIFTY"
_STOCK_FUTURE = "FUTSTK"


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


# ================================================================================================
# F1 — the proposed condor
# ================================================================================================


@dataclass(frozen=True, slots=True)
class LegPrint:
    """One option's row of the bhavcopy: its settle, open interest (units) and volume."""

    settle: Decimal
    open_interest: int | None
    volume: int | None


@dataclass(frozen=True, slots=True)
class CondorProposal:
    """What the scan proposes for one underlying on its entry session.

    ``state`` is ``None`` for a clean proposal, or the first ``PlanState`` rejection of
    ``04`` §2-§3 in the order structure → liquidity → size → cost. ``reasons`` lists every
    refusal found, by name.
    """

    state: PlanState | None
    reasons: tuple[str, ...]
    sd: Decimal
    strikes: CondorStrikes | None
    #: The settle of each leg (``None`` where the bhavcopy has no print).
    entry: Mapping[LegRole, Decimal]
    credit: Decimal | None
    max_loss_per_unit: Decimal | None
    max_loss_per_lot_inr: Decimal | None
    sizing: FoSizing | None
    cost: CondorCost | None


def _bhavcopy_liquidity(
    strikes: CondorStrikes,
    prints: Mapping[tuple[Decimal, OptionType], LegPrint],
    lot_size: int,
    min_short_oi_lots: int,
) -> tuple[str, ...]:
    """``04`` §2's liquidity, as far as a closing file can prove it, refusals by name."""
    found: list[str] = []
    for role in ENTRY_SEQUENCE:
        strike = strikes.strike(role)
        name = f"{role.value.lower().replace('_', ' ')} {strike}"
        row = prints.get((strike, option_type_of(role)))
        if row is None or row.settle <= 0:
            found.append(f"{name}: no settle in the bhavcopy")
            continue
        if sign_of(role) > 0:
            if not row.volume:
                found.append(f"{name}: the wing did not trade (volume 0)")
            continue
        oi_lots = (row.open_interest or 0) // lot_size
        if oi_lots < min_short_oi_lots:
            found.append(f"{name}: OI {oi_lots} lots < {min_short_oi_lots}")
    return tuple(found)


def propose_condor(  # noqa: PLR0913 - every input 04 §2-§3 names, by keyword
    *,
    forward: Decimal,
    iv: float,
    days_to_expiry: int,
    call_strikes: Sequence[Decimal],
    put_strikes: Sequence[Decimal],
    prints: Mapping[tuple[Decimal, OptionType], LegPrint],
    lot_size: int,
    capital_inr: Decimal,
    config: FnoConfig,
    rates: CostRates,
    ceilings: FnoCeilings,
) -> CondorProposal:
    """``04`` §2-§3 on one session's bhavcopy for one underlying's monthly."""
    choice = select_strikes(
        forward=forward,
        iv=iv,
        days_to_expiry=days_to_expiry,
        call_strikes=call_strikes,
        put_strikes=put_strikes,
        config=config.f1,
    )
    strikes = choice.strikes
    if strikes is None:
        return CondorProposal(
            PlanState.REJECTED_STRUCTURE,
            choice.reasons,
            choice.sd,
            None,
            {},
            None,
            None,
            None,
            None,
            None,
        )
    entry: dict[LegRole, Decimal] = {}
    for role in LegRole:
        row = prints.get((strikes.strike(role), option_type_of(role)))
        if row is not None and row.settle > 0:
            entry[role] = row.settle
    liquidity = _bhavcopy_liquidity(strikes, prints, lot_size, config.f1.min_short_oi_lots)
    if len(entry) < len(LegRole):
        return CondorProposal(
            PlanState.REJECTED_LIQUIDITY,
            liquidity,
            choice.sd,
            strikes,
            entry,
            None,
            None,
            None,
            None,
            None,
        )
    priced = price_structure(strikes, entry)
    reasons: list[str] = [*priced.reasons, *liquidity]
    states: list[PlanState] = []
    if priced.state is not None:
        states.append(priced.state)
    if liquidity:
        states.append(PlanState.REJECTED_LIQUIDITY)
    loss_per_lot = _money(priced.max_loss_per_unit * lot_size)
    sizing: FoSizing | None = None
    cost: CondorCost | None = None
    if priced.state is None:
        sizing = size(
            mode=Mode.PAPER,
            capital_inr=capital_inr,
            risk_pct=config.f1.risk_per_trade_pct,
            risk_per_unit=priced.max_loss_per_unit,
            lot_size=lot_size,
            common=config.common,
            ceilings=ceilings,
        )
        if sizing.state is not None:
            states.append(sizing.state)
            reasons.append(sizing.message)
        cost = condor_round_trip(
            entry=entry,
            exit_=None,
            quantity=max(sizing.lots, 1) * lot_size,
            rates=rates,
            config=config.f1,
        )
        if cost.state is not None:
            states.append(cost.state)
            reasons.append(
                f"round trip ₹{cost.round_trip_inr} is {cost.cost_share_pct} % of the credit "
                f"₹{cost.credit_inr} > {config.f1.max_cost_share_pct} %"
            )
    return CondorProposal(
        states[0] if states else None,
        tuple(reasons),
        choice.sd,
        strikes,
        entry,
        priced.credit,
        priced.max_loss_per_unit,
        loss_per_lot,
        sizing,
        cost,
    )


# ================================================================================================
# F2 — signals over one re-derived window
# ================================================================================================

#: The columns :func:`f2_signals` returns, one row per underlying that printed on the session.
F2_SIGNAL_COLUMNS: tuple[str, ...] = (
    "symbol",
    "instrument",
    "held_expiry",
    "held_settle",
    "lot_size",
    "good_sessions",
    "close_inr",
    "prior_high_inr",
    "average_inr",
    "atr_inr",
    "rv20",
    "ca_flag",
    "ca_recent",
    "turnover_20d",
    "turnover_rank",
    "ranked",
    "in_universe",
    "breakout",
    "trend",
)


def f2_signals(
    futures: pl.DataFrame, trade_date: dt.date, f2: F2Config, series: SeriesConfig
) -> pl.DataFrame:
    """``04`` §10 on ``trade_date`` from a window of futures rows ending there.

    ``futures`` carries :data:`baskfy_core.fno.series.FUTURES_COLUMNS` for every session of the
    window; a row after ``trade_date`` is refused (house rule 5). ``breakout`` and ``trend`` are
    null where the window is too short to say (fewer good sessions than the rule needs) — a
    null is not a ``False``. A corporate-action session has no level (``series``) and is dropped
    from the rolling windows, as ``cont.py`` dropped it; ``ca_recent`` still carries it.
    """
    if futures.is_empty():
        return pl.DataFrame(schema=_SIGNAL_SCHEMA)
    latest = futures.get_column("trade_date").max()
    if isinstance(latest, dt.date) and latest > trade_date:
        raise ValueError(f"the window holds {latest}, after {trade_date}: look-ahead")
    cont = continuous_futures(futures, series)
    good = (
        cont.filter(pl.col("level_c").is_not_null())
        .sort("symbol", "trade_date")
        .with_columns(
            pl.col("level_c")
            .shift(1)
            .rolling_max(f2.breakout_sessions)
            .over("symbol")
            .alias("_prior_high"),
            pl.col("level_c").rolling_mean(f2.trend_sessions).over("symbol").alias("_average"),
            pl.len().over("symbol").alias("good_sessions"),
        )
        .select("symbol", "trade_date", "_prior_high", "_average", "good_sessions")
    )
    today = cont.filter(pl.col("trade_date") == trade_date).join(
        good, on=["symbol", "trade_date"], how="left"
    )
    if today.is_empty():
        return pl.DataFrame(schema=_SIGNAL_SCHEMA)
    scale = (
        pl.when(pl.col("level_c").is_not_null() & (pl.col("level_c") > 0))
        .then(pl.col("held_settle") / pl.col("level_c"))
        .otherwise(None)
    )
    stocks = pl.col("instrument") == _STOCK_FUTURE
    rankable = stocks & pl.col("fut_turnover_20d").is_not_null()
    out = today.with_columns(scale.alias("_scale")).with_columns(
        (pl.col("level_c") * pl.col("_scale")).alias("close_inr"),
        (pl.col("_prior_high") * pl.col("_scale")).alias("prior_high_inr"),
        (pl.col("_average") * pl.col("_scale")).alias("average_inr"),
        (pl.col("atr14") * pl.col("_scale")).alias("atr_inr"),
        pl.col("fut_turnover_20d").alias("turnover_20d"),
        pl.col("good_sessions").fill_null(0),
        pl.col("ca_flag").fill_null(False),
        pl.col("ca_recent").fill_null(False),
    )
    ranks = (
        out.filter(rankable)
        .with_columns(
            pl.col("turnover_20d").rank(method="average", descending=True).alias("turnover_rank"),
            pl.len().alias("ranked"),
        )
        .select("symbol", "turnover_rank", "ranked")
    )
    share = float(f2.universe_turnover_pct) / 100.0
    out = (
        out.join(ranks, on="symbol", how="left")
        .with_columns(
            (
                stocks
                & pl.col("turnover_rank").is_not_null()
                & (pl.col("turnover_rank") <= share * pl.col("ranked"))
            ).alias("in_universe"),
            (pl.col("level_c") > pl.col("_prior_high")).alias("breakout"),
            (pl.col("level_c") > pl.col("_average")).alias("trend"),
        )
        .with_columns(pl.col("in_universe").fill_null(False))
    )
    return out.select(F2_SIGNAL_COLUMNS).sort("symbol")


_SIGNAL_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.String(),
    "instrument": pl.String(),
    "held_expiry": pl.Date(),
    "held_settle": pl.Float64(),
    "lot_size": pl.Int64(),
    "good_sessions": pl.UInt32(),
    "close_inr": pl.Float64(),
    "prior_high_inr": pl.Float64(),
    "average_inr": pl.Float64(),
    "atr_inr": pl.Float64(),
    "rv20": pl.Float64(),
    "ca_flag": pl.Boolean(),
    "ca_recent": pl.Boolean(),
    "turnover_20d": pl.Float64(),
    "turnover_rank": pl.Float64(),
    "ranked": pl.UInt32(),
    "in_universe": pl.Boolean(),
    "breakout": pl.Boolean(),
    "trend": pl.Boolean(),
}


@dataclass(frozen=True, slots=True)
class FutureProposal:
    """One F2 candidate's plan numbers (``04`` §10), in rupees per unit unless named."""

    entry: Decimal
    atr: Decimal
    stop: Decimal
    gtt_trigger: Decimal
    risk_per_unit: Decimal
    risk_per_lot_inr: Decimal
    sizing: FoSizing
    cost: FutureCharges
    #: Round trip ÷ one lot's risk, as a percent: the cost in R.
    cost_share_pct: Decimal


def propose_future(  # noqa: PLR0913 - every input 04 §10 names, by keyword
    *,
    entry: Decimal,
    atr: Decimal,
    ann_vol: float,
    lot_size: int,
    capital_inr: Decimal,
    config: FnoConfig,
    ceilings: FnoCeilings,
) -> FutureProposal:
    """Stop ``entry - 3 x ATR14``, the GTT at the tighter of that and ``stop_from_vol``, lots
    from the budget (paper at ₹0 runs one lot and records ``lots_at_ceiling``), and the round
    trip with the exit priced at the entry (the neutral guess, as FO1.5 made it for F1)."""
    if atr <= 0:
        raise ValueError("ATR must be positive to place a stop")
    stop = _money(f2_initial_stop(entry, atr, config.f2))
    trigger = _money(gtt_trigger(stop, entry, ann_vol, config.stop_vol))
    risk = entry - stop
    sizing = size(
        mode=Mode.PAPER,
        capital_inr=capital_inr,
        risk_pct=config.f2.risk_per_trade_pct,
        risk_per_unit=risk,
        lot_size=lot_size,
        common=config.common,
        ceilings=ceilings,
    )
    quantity = max(sizing.lots, 1) * lot_size
    cost = future_round_trip(entry, entry, quantity, config.future_costs)
    per_lot = _money(risk * lot_size)
    share = (cost.total / (per_lot * max(sizing.lots, 1)) * _HUNDRED).quantize(_CENT)
    return FutureProposal(entry, _money(atr), stop, trigger, risk, per_lot, sizing, cost, share)


def allocate_capacity(
    ordered: Sequence[tuple[str, str | None]],
    open_industries: Sequence[str | None],
    f2: F2Config,
) -> dict[str, str | None]:
    """``04`` §10's concurrency over ``ordered`` (symbol, industry) candidates.

    ``open_industries`` has one entry per open F2 position. A candidate is admitted while the
    book holds fewer than ``f2_max_open`` and its industry fewer than ``max_per_industry``; the
    answer maps each symbol to ``None`` (admitted) or the reason it is blocked, in words. A name
    with no known industry is capped only by ``f2_max_open``. A symbol already open is the
    caller's ``OPEN_POSITION``, never passed here.
    """
    held = len(open_industries)
    per_industry: dict[str, int] = {}
    for industry in open_industries:
        if industry is not None:
            per_industry[industry] = per_industry.get(industry, 0) + 1
    out: dict[str, str | None] = {}
    for symbol, industry in ordered:
        if held >= f2.max_open:
            out[symbol] = f"the book holds {held} of f2_max_open {f2.max_open} positions"
            continue
        if industry is not None and per_industry.get(industry, 0) >= f2.max_per_industry:
            out[symbol] = (
                f"industry {industry} already holds {per_industry[industry]} of "
                f"{f2.max_per_industry} positions"
            )
            continue
        out[symbol] = None
        held += 1
        if industry is not None:
            per_industry[industry] = per_industry.get(industry, 0) + 1
    return out
