from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final
from zoneinfo import ZoneInfo

from baskfy_core.opportunities.config import (
    BEARISH_STRATEGIES,
    BULLISH_STRATEGIES,
    DEFAULT_OPTIONS_PARAMS,
    Direction,
    OptionsParams,
    QuoteRules,
    StrategyKind,
)
from baskfy_core.opportunities.ranking import ScoreBreakdown, score_option
from baskfy_core.options.config import CostRates, OptionType, Side
from baskfy_core.options.costs import CostFill, charges
from baskfy_core.options.greeks import solve, year_fraction

IST: Final = ZoneInfo("Asia/Kolkata")
SETTLE: Final = dt.time(15, 30)
_HUNDRED: Final = Decimal(100)
_TWO: Final = Decimal(2)
_PAISA: Final = Decimal("0.01")
_DAYS_PER_YEAR: Final = 365.0
GREEKS_SOURCE: Final = "MODEL_BLACK76_SPOT_FORWARD_R0"
MODEL_NOTE: Final = (
    "IV and delta are model-derived: Black-76 solved on each leg's bid/ask mid with spot as the "
    "forward and a zero rate. They are estimates, not exchange data. No probability of profit "
    "or historical IV percentile is computed."
)


@dataclass(frozen=True, slots=True)
class ContractQuote:
    tradingsymbol: str
    underlying: str
    expiry: dt.date
    strike: Decimal
    option_type: OptionType
    lot_size: int
    bid: Decimal | None
    ask: Decimal | None
    volume: int | None
    oi: int | None
    quote_ts: dt.datetime | None


@dataclass(frozen=True, slots=True)
class Rejection:
    tradingsymbol: str
    reason: str


@dataclass(frozen=True, slots=True)
class AcceptedQuote:
    contract: ContractQuote
    bid: Decimal
    ask: Decimal
    quote_age_s: int
    spread_pct: Decimal

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / _TWO


def _age(quote_ts: dt.datetime, now: dt.datetime) -> int:
    return max(0, int((now - quote_ts).total_seconds()))


def _quote_problem(
    quote: ContractQuote, *, underlying: str, now: dt.datetime, rules: QuoteRules
) -> str | None:
    problem: str | None = None
    if quote.underlying.upper() != underlying.upper():
        problem = f"underlying {quote.underlying} does not match {underlying}"
    elif quote.lot_size <= 0:
        problem = "lot size missing"
    elif quote.bid is None or quote.ask is None or quote.bid <= 0 or quote.ask <= 0:
        problem = "incomplete quote: no two-sided market"
    elif quote.quote_ts is None:
        problem = "incomplete quote: no exchange timestamp"
    elif quote.bid > quote.ask:
        problem = f"crossed quote: bid {quote.bid} above ask {quote.ask}"
    elif _age(quote.quote_ts, now) > rules.max_quote_age_s:
        age = _age(quote.quote_ts, now)
        problem = f"stale quote: {age}s old (limit {rules.max_quote_age_s}s)"
    elif _spread_pct(quote.bid, quote.ask) > rules.max_spread_pct:
        spread = _spread_pct(quote.bid, quote.ask)
        problem = f"spread {spread}% exceeds {rules.max_spread_pct}%"
    elif (quote.oi or 0) < rules.min_oi:
        problem = f"open interest {quote.oi or 0} below {rules.min_oi}"
    elif (quote.volume or 0) < rules.min_volume:
        problem = f"volume {quote.volume or 0} below {rules.min_volume}"
    return problem


def _spread_pct(bid: Decimal, ask: Decimal) -> Decimal:
    mid = (bid + ask) / _TWO
    return ((ask - bid) / mid * _HUNDRED).quantize(_PAISA, rounding=ROUND_HALF_UP)


def validate_quote(
    quote: ContractQuote, *, underlying: str, now: dt.datetime, rules: QuoteRules
) -> AcceptedQuote | Rejection:
    problem = _quote_problem(quote, underlying=underlying, now=now, rules=rules)
    if problem is not None or quote.bid is None or quote.ask is None or quote.quote_ts is None:
        return Rejection(quote.tradingsymbol, problem or "incomplete quote")
    return AcceptedQuote(
        quote, quote.bid, quote.ask, _age(quote.quote_ts, now), _spread_pct(quote.bid, quote.ask)
    )


@dataclass(frozen=True, slots=True)
class Leg:
    quote: AcceptedQuote
    side: Side
    iv: float | None
    delta: float | None

    @property
    def exec_price(self) -> Decimal:
        return self.quote.ask if self.side is Side.BUY else self.quote.bid

    @property
    def strike(self) -> Decimal:
        return self.quote.contract.strike

    @property
    def option_type(self) -> OptionType:
        return self.quote.contract.option_type


@dataclass(frozen=True, slots=True)
class PayoffPoint:
    underlying: Decimal
    pnl_per_lot: Decimal


@dataclass(frozen=True, slots=True)
class OptionCandidate:
    strategy: StrategyKind
    expiry: dt.date
    dte: int
    lot_size: int
    lots: int
    legs: tuple[Leg, ...]
    net_debit_per_unit: Decimal
    max_loss_per_unit: Decimal
    max_profit_per_unit: Decimal | None
    breakevens: tuple[Decimal, ...]
    entry_cost_inr: Decimal
    round_trip_cost_inr: Decimal
    payoff: tuple[PayoffPoint, ...]
    score: ScoreBreakdown
    limitations: tuple[str, ...]
    actionable: bool

    @property
    def id(self) -> str:
        symbols = (leg.quote.contract.tradingsymbol for leg in self.legs)
        return "|".join([self.strategy.value, *symbols])

    @property
    def net_debit_per_lot(self) -> Decimal:
        return self.net_debit_per_unit * self.lot_size

    @property
    def max_loss_per_lot(self) -> Decimal:
        return self.max_loss_per_unit * self.lot_size

    @property
    def max_profit_per_lot(self) -> Decimal | None:
        if self.max_profit_per_unit is None:
            return None
        return self.max_profit_per_unit * self.lot_size

    @property
    def max_loss_after_costs_per_lot(self) -> Decimal:
        return self.max_loss_per_lot + self.round_trip_cost_inr


def leg_payoff_per_unit(leg: Leg, underlying: Decimal) -> Decimal:
    if leg.option_type is OptionType.CE:
        value = max(underlying - leg.strike, Decimal(0))
    else:
        value = max(leg.strike - underlying, Decimal(0))
    return value if leg.side is Side.BUY else -value


def expiry_pnl_per_unit(legs: Sequence[Leg], underlying: Decimal) -> Decimal:
    intrinsic = sum((leg_payoff_per_unit(leg, underlying) for leg in legs), Decimal(0))
    return intrinsic - net_debit(legs)


def net_debit(legs: Sequence[Leg]) -> Decimal:
    return sum(
        (leg.exec_price if leg.side is Side.BUY else -leg.exec_price for leg in legs), Decimal(0)
    )


@dataclass(frozen=True, slots=True)
class StructureMetrics:
    debit: Decimal
    max_loss: Decimal
    max_profit: Decimal | None
    breakevens: tuple[Decimal, ...]


def structure_metrics(kind: StrategyKind, legs: Sequence[Leg]) -> StructureMetrics | str:
    debit = net_debit(legs)
    if debit <= 0:
        return "non-positive net debit from quotes"
    if kind is StrategyKind.LONG_CALL:
        return StructureMetrics(debit, debit, None, (legs[0].strike + debit,))
    if kind is StrategyKind.LONG_PUT:
        strike = legs[0].strike
        return StructureMetrics(debit, debit, strike - debit, (strike - debit,))
    width = abs(legs[0].strike - legs[1].strike)
    if debit >= width:
        return f"debit {debit} not below spread width {width}"
    if kind is StrategyKind.BULL_CALL_SPREAD:
        return StructureMetrics(debit, debit, width - debit, (legs[0].strike + debit,))
    return StructureMetrics(debit, debit, width - debit, (legs[0].strike - debit,))


_SHAPES: Final = {
    StrategyKind.LONG_CALL: [(OptionType.CE, Side.BUY)],
    StrategyKind.LONG_PUT: [(OptionType.PE, Side.BUY)],
    StrategyKind.BULL_CALL_SPREAD: [(OptionType.CE, Side.BUY), (OptionType.CE, Side.SELL)],
    StrategyKind.BEAR_PUT_SPREAD: [(OptionType.PE, Side.BUY), (OptionType.PE, Side.SELL)],
}


def validate_legs(kind: StrategyKind, legs: Sequence[Leg]) -> str | None:
    contracts = [leg.quote.contract for leg in legs]
    checks = (
        (len({c.underlying for c in contracts}) == 1, "legs on different underlyings"),
        (len({c.expiry for c in contracts}) == 1, "legs on different expiries"),
        (len({c.lot_size for c in contracts}) == 1, "legs with different lot sizes"),
        (
            [(leg.option_type, leg.side) for leg in legs] == _SHAPES[kind],
            "leg types do not match the strategy",
        ),
        (
            kind is not StrategyKind.BULL_CALL_SPREAD or legs[0].strike < legs[1].strike,
            "bull call spread needs the bought strike below the sold strike",
        ),
        (
            kind is not StrategyKind.BEAR_PUT_SPREAD or legs[0].strike > legs[1].strike,
            "bear put spread needs the bought strike above the sold strike",
        ),
    )
    return next((message for ok, message in checks if not ok), None)


def round_trip_costs(legs: Sequence[Leg], lots: int, rates: CostRates) -> tuple[Decimal, Decimal]:
    lot = legs[0].quote.contract.lot_size * lots
    entry = [CostFill(leg.side, leg.exec_price, lot) for leg in legs]
    exit_ = [
        CostFill(
            Side.SELL if leg.side is Side.BUY else Side.BUY,
            leg.quote.bid if leg.side is Side.BUY else leg.quote.ask,
            lot,
        )
        for leg in legs
    ]
    entry_total = charges(entry, rates).total
    return entry_total, entry_total + charges(exit_, rates).total


def payoff_grid(
    legs: Sequence[Leg], spot: Decimal, breakevens: Iterable[Decimal], params: OptionsParams
) -> tuple[PayoffPoint, ...]:
    span = spot * params.payoff_range_pct / _HUNDRED
    low, high = spot - span, spot + span
    steps = max(params.payoff_points - 1, 1)
    xs = {low + (high - low) * Decimal(i) / Decimal(steps) for i in range(steps + 1)}
    xs.update(leg.strike for leg in legs if low <= leg.strike <= high)
    xs.update(b for b in breakevens if low <= b <= high)
    xs.add(spot)
    lot = legs[0].quote.contract.lot_size
    return tuple(
        PayoffPoint(
            x.quantize(_PAISA, rounding=ROUND_HALF_UP),
            (expiry_pnl_per_unit(legs, x) * lot).quantize(_PAISA, rounding=ROUND_HALF_UP),
        )
        for x in sorted(xs)
        if x > 0
    )


def _model_leg(
    quote: AcceptedQuote, side: Side, spot: Decimal, now: dt.datetime, params: OptionsParams
) -> Leg:
    contract = quote.contract
    years = year_fraction(now.astimezone(IST), contract.expiry, SETTLE)
    tick = 0.05
    greeks = solve(
        float(quote.mid),
        float(spot),
        float(contract.strike),
        years,
        params.model_rate,
        contract.option_type,
        min_premium=params.min_model_premium,
        tick=tick,
        lower=params.iv_lower,
        upper=params.iv_upper,
        tolerance=params.iv_tolerance,
    )
    return Leg(
        quote,
        side,
        None if greeks is None else greeks.iv,
        None if greeks is None else greeks.delta,
    )


def _sizing(max_loss_after_costs_per_lot: Decimal, max_loss_inr: Decimal | None) -> int:
    if max_loss_inr is None:
        return 1
    if max_loss_after_costs_per_lot <= 0:
        return 0
    return int(max_loss_inr // max_loss_after_costs_per_lot)


@dataclass(frozen=True, slots=True)
class OptionsRequest:
    underlying: str
    direction: Direction
    strategies: frozenset[StrategyKind]
    max_loss_inr: Decimal | None
    min_dte: int
    max_dte: int
    rules: QuoteRules


@dataclass(frozen=True, slots=True)
class OptionsResult:
    candidates: tuple[OptionCandidate, ...]
    rejected: tuple[Rejection, ...]
    expiries: tuple[dt.date, ...]
    warnings: tuple[str, ...]


def _strike_window(strikes: Sequence[Decimal], spot: Decimal, each_side: int) -> list[Decimal]:
    ordered = sorted(set(strikes))
    if not ordered:
        return []
    atm_index = min(range(len(ordered)), key=lambda i: (abs(ordered[i] - spot), ordered[i]))
    return ordered[max(0, atm_index - each_side) : atm_index + each_side + 1]


def _dte(expiry: dt.date, now: dt.datetime) -> int:
    return (expiry - now.astimezone(IST).date()).days


def candidate_structures(
    kind: StrategyKind,
    by_strike: dict[tuple[Decimal, OptionType], AcceptedQuote],
    window: Sequence[Decimal],
    params: OptionsParams,
) -> list[list[tuple[AcceptedQuote, Side]]]:
    option_type = (
        OptionType.CE
        if kind in {StrategyKind.LONG_CALL, StrategyKind.BULL_CALL_SPREAD}
        else OptionType.PE
    )
    available = [k for k in window if (k, option_type) in by_strike]
    out: list[list[tuple[AcceptedQuote, Side]]] = []
    if kind in {StrategyKind.LONG_CALL, StrategyKind.LONG_PUT}:
        return [[(by_strike[(k, option_type)], Side.BUY)] for k in available]
    for i, long_strike in enumerate(available):
        if kind is StrategyKind.BULL_CALL_SPREAD:
            shorts = available[i + 1 : i + 1 + params.max_spread_width_steps]
        else:
            shorts = list(reversed(available[max(0, i - params.max_spread_width_steps) : i]))
        out.extend(
            [
                [
                    (by_strike[(long_strike, option_type)], Side.BUY),
                    (by_strike[(short, option_type)], Side.SELL),
                ]
                for short in shorts
            ]
        )
    return out


def analyze(  # noqa: PLR0913 - the request, its data and its clock
    request: OptionsRequest,
    quotes: Sequence[ContractQuote],
    *,
    spot: Decimal,
    now: dt.datetime,
    rates: CostRates,
    params: OptionsParams = DEFAULT_OPTIONS_PARAMS,
) -> OptionsResult:
    allowed = BULLISH_STRATEGIES if request.direction is Direction.BULLISH else BEARISH_STRATEGIES
    kinds = sorted(request.strategies & allowed, key=lambda k: k.value)
    warnings: list[str] = []
    dropped = sorted(k.value for k in request.strategies - allowed)
    if dropped:
        warnings.append(
            f"{', '.join(dropped)} skipped: they do not express a {request.direction.value} view."
        )
    in_range = sorted(
        {
            q.expiry
            for q in quotes
            if request.min_dte <= _dte(q.expiry, now) <= request.max_dte
            and q.underlying.upper() == request.underlying.upper()
        }
    )[: params.max_expiries]
    if not in_range:
        warnings.append(
            f"No listed expiry within {request.min_dte}-{request.max_dte} days to expiry."
        )
    rejected: list[Rejection] = []
    accepted: dict[dt.date, list[AcceptedQuote]] = defaultdict(list)
    for quote in quotes:
        if quote.expiry not in in_range:
            continue
        verdict = validate_quote(quote, underlying=request.underlying, now=now, rules=request.rules)
        if isinstance(verdict, Rejection):
            rejected.append(verdict)
        else:
            accepted[quote.expiry].append(verdict)

    candidates: list[OptionCandidate] = []
    for expiry in in_range:
        pool = accepted.get(expiry, [])
        lot_sizes = {q.contract.lot_size for q in pool}
        if len(lot_sizes) > 1:
            sizes = sorted(lot_sizes)
            warnings.append(f"Expiry {expiry.isoformat()} lists mixed lot sizes {sizes}.")
        by_strike = {(q.contract.strike, q.contract.option_type): q for q in pool}
        window = _strike_window([q.contract.strike for q in pool], spot, params.strikes_each_side)
        for kind in kinds:
            for structure in candidate_structures(kind, by_strike, window, params):
                legs = tuple(_model_leg(q, side, spot, now, params) for q, side in structure)
                built = build_candidate(
                    kind, legs, spot=spot, now=now, request=request, rates=rates, params=params
                )
                if isinstance(built, Rejection):
                    rejected.append(built)
                else:
                    candidates.append(built)
    ranked = sorted(candidates, key=lambda c: (-c.score.total, c.id))
    return OptionsResult(
        candidates=tuple(ranked[: params.max_candidates]),
        rejected=tuple(rejected),
        expiries=tuple(in_range),
        warnings=tuple(warnings),
    )


def build_candidate(  # noqa: PLR0913 - one structure, the market and the request
    kind: StrategyKind,
    legs: Sequence[Leg],
    *,
    spot: Decimal,
    now: dt.datetime,
    request: OptionsRequest,
    rates: CostRates,
    params: OptionsParams = DEFAULT_OPTIONS_PARAMS,
) -> OptionCandidate | Rejection:
    label = "/".join(leg.quote.contract.tradingsymbol for leg in legs)
    problem = validate_legs(kind, legs)
    if problem is not None:
        return Rejection(label, problem)
    metrics = structure_metrics(kind, legs)
    if isinstance(metrics, str):
        return Rejection(label, metrics)
    lot_size = legs[0].quote.contract.lot_size
    entry_cost, round_trip = round_trip_costs(legs, 1, rates)
    max_loss_after_costs = metrics.max_loss * lot_size + round_trip
    lots = _sizing(max_loss_after_costs, request.max_loss_inr)
    if lots < 1:
        return Rejection(
            label,
            f"one lot risks ₹{max_loss_after_costs} after costs, "
            f"above the ₹{request.max_loss_inr} limit",
        )
    expiry = legs[0].quote.contract.expiry
    dte = _dte(expiry, now)
    ivs = [leg.iv for leg in legs if leg.side is Side.BUY and leg.iv is not None]
    years = max(dte, 0) / _DAYS_PER_YEAR
    expected_move_pct = ivs[0] * math.sqrt(years) * 100.0 if ivs and years > 0 else None
    breakeven_move = abs(float(metrics.breakevens[0] - spot) / float(spot) * 100.0)
    max_loss_lot = metrics.max_loss * lot_size
    payoff_ratio = (
        None if metrics.max_profit is None else float(metrics.max_profit / metrics.max_loss)
    )
    limitations: list[str] = []
    if any(leg.iv is None for leg in legs):
        limitations.append("Model IV did not solve for at least one leg; Greeks omitted there.")
    if metrics.max_profit is None:
        limitations.append("Maximum profit is unlimited; payoff score is neutral.")
    if dte <= 1:
        limitations.append("Expires within a day; exercise/settlement STT may apply if held.")
    score = score_option(
        payoff_ratio=payoff_ratio,
        breakeven_move_pct=breakeven_move,
        expected_move_pct=expected_move_pct,
        worst_spread_pct=float(max(leg.quote.spread_pct for leg in legs)),
        max_spread_pct=float(request.rules.max_spread_pct),
        cost_share=float(round_trip / max_loss_lot) if max_loss_lot > 0 else 1.0,
        dte=dte,
        preferred=(params.preferred_dte_min, params.preferred_dte_max),
    )
    return OptionCandidate(
        strategy=kind,
        expiry=expiry,
        dte=dte,
        lot_size=lot_size,
        lots=lots,
        legs=tuple(legs),
        net_debit_per_unit=metrics.debit,
        max_loss_per_unit=metrics.max_loss,
        max_profit_per_unit=metrics.max_profit,
        breakevens=tuple(b.quantize(_PAISA, rounding=ROUND_HALF_UP) for b in metrics.breakevens),
        entry_cost_inr=entry_cost,
        round_trip_cost_inr=round_trip,
        payoff=payoff_grid(legs, spot, metrics.breakevens, params),
        score=score,
        limitations=tuple(limitations),
        actionable=True,
    )
