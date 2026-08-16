"""Defined-risk NIFTY option strategies.

This module plans trades; it never submits one.  A caller must present the plan for
explicit confirmation and route every leg through :class:`OrderGateway` in the listed
sequence.  Both strategies are intraday by default and use LIMIT orders only.

The return targets are reporting benchmarks, not entry quotas.  A weak setup produces
``OptionPlanRejected`` rather than a lower-quality trade merely to chase a monthly goal.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .base import BaseStrategy


CE, PE = "CE", "PE"
BUY, SELL = "BUY", "SELL"


class OptionPlanRejected(ValueError):
    """The current market snapshot cannot produce a valid, risk-bounded plan."""

    def __init__(self, code: str, reason: str):
        super().__init__(f"{code}: {reason}")
        self.code, self.reason = code, reason


def _date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def _time(value: str | dt.time) -> dt.time:
    if isinstance(value, dt.time):
        return value
    return dt.time.fromisoformat(value)


def _snap(price: float, tick: float) -> float:
    if tick <= 0:
        return round(price, 2)
    return round(round(price / tick) * tick, 2)


@dataclass(frozen=True)
class OptionInstrument:
    token: int
    symbol: str
    underlying: str
    expiry: dt.date
    strike: float
    kind: str
    lot_size: int
    tick_size: float = 0.05
    exchange: str = "NFO"

    def __post_init__(self):
        if self.kind not in (CE, PE):
            raise ValueError(f"unsupported option kind {self.kind!r}")
        if self.lot_size <= 0 or self.strike <= 0 or self.tick_size <= 0:
            raise ValueError("strike, lot size and tick size must be positive")

    @classmethod
    def from_kite(cls, row: Mapping[str, Any]) -> "OptionInstrument":
        return cls(
            token=int(row["instrument_token"]),
            symbol=str(row["tradingsymbol"]),
            underlying=str(row.get("name") or row.get("underlying") or ""),
            expiry=_date(row["expiry"]),
            strike=float(row["strike"]),
            kind=str(row["instrument_type"]).upper(),
            lot_size=int(row["lot_size"]),
            tick_size=float(row.get("tick_size") or 0.05),
            exchange=str(row.get("exchange") or "NFO"),
        )


@dataclass(frozen=True)
class OptionQuote:
    instrument: OptionInstrument
    bid: float
    ask: float
    last: float
    volume: int
    oi: int
    iv: float | None = None       # decimal, for example 0.14
    delta: float | None = None    # optional vendor/live calculation

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0 if self.bid > 0 and self.ask > 0 else 0.0

    @property
    def spread_pct(self) -> float:
        return ((self.ask - self.bid) / self.mid * 100.0) if self.mid > 0 else math.inf

    @classmethod
    def from_kite(cls, instrument: OptionInstrument, row: Mapping[str, Any]) -> "OptionQuote":
        depth = row.get("depth") or {}
        buys, sells = depth.get("buy") or [], depth.get("sell") or []
        bid = float((buys[0] if buys else {}).get("price") or 0.0)
        ask = float((sells[0] if sells else {}).get("price") or 0.0)
        return cls(instrument=instrument, bid=bid, ask=ask,
                   last=float(row.get("last_price") or 0.0),
                   volume=int(row.get("volume") or 0), oi=int(row.get("oi") or 0))


@dataclass(frozen=True)
class OptionMarketSnapshot:
    """All values describe one instant; ``as_of`` is interpreted as IST when naive.

    ``spot`` is the cash NIFTY value used for strikes and option valuation.  Because a
    cash index has no traded volume, live adapters may supply ``signal_price`` from the
    nearest NIFTY future; its own opening range, VWAP, EMAs and volume then form one
    internally consistent signal set.
    """

    as_of: dt.datetime
    spot: float
    vwap: float
    opening_range_high: float
    opening_range_low: float
    ema_fast: float
    ema_slow: float
    volume_ratio: float
    available_capital: float
    quotes: tuple[OptionQuote, ...]
    signal_price: float | None = None
    signal_source: str = "NIFTY spot"

    @property
    def trend_price(self) -> float:
        return self.signal_price if self.signal_price is not None else self.spot

    def validate(self) -> None:
        values = (self.spot, self.trend_price, self.vwap,
                  self.opening_range_high, self.opening_range_low,
                  self.ema_fast, self.ema_slow, self.available_capital)
        if any(v <= 0 or not math.isfinite(v) for v in values):
            raise OptionPlanRejected("BAD_SNAPSHOT", "prices and capital must be positive")
        if self.opening_range_low >= self.opening_range_high:
            raise OptionPlanRejected("BAD_SNAPSHOT", "opening range is inverted")
        if not self.quotes:
            raise OptionPlanRejected("NO_CHAIN", "no option quotes supplied")


@dataclass(frozen=True)
class SellingConfig:
    underlying: str = "NIFTY"
    entry_start: dt.time = dt.time(9, 45)
    entry_end: dt.time = dt.time(13, 30)
    square_off: dt.time = dt.time(15, 12)
    min_dte_days: int = 1
    max_dte_days: int = 7
    short_delta: float = 0.16
    wing_delta: float = 0.05
    max_spread_pct: float = 8.0
    min_volume: int = 1_000
    min_oi: int = 5_000
    max_vwap_distance_pct: float = 0.35
    max_range_to_expected_move: float = 0.65
    min_credit_width_pct: float = 10.0
    risk_per_trade_pct: float = 0.75
    max_lots: int = 4
    target_credit_capture_pct: float = 35.0
    stop_credit_multiple: float = 1.5
    monthly_return_benchmark_pct: float = 5.0
    risk_free_rate: float = 0.06

    def validate(self) -> None:
        if not 0 < self.wing_delta < self.short_delta < 0.5:
            raise ValueError("selling deltas must satisfy 0 < wing < short < 0.5")
        if not 0 < self.risk_per_trade_pct <= 2.0:
            raise ValueError("selling risk per trade must be in (0, 2]")
        if self.entry_start >= self.entry_end or self.entry_end >= self.square_off:
            raise ValueError("selling times must be entry_start < entry_end < square_off")
        if self.min_dte_days < 0 or self.max_dte_days < self.min_dte_days:
            raise ValueError("invalid selling DTE range")


@dataclass(frozen=True)
class BuyingConfig:
    underlying: str = "NIFTY"
    entry_start: dt.time = dt.time(9, 35)
    entry_end: dt.time = dt.time(14, 30)
    square_off: dt.time = dt.time(15, 12)
    min_dte_days: int = 1
    max_dte_days: int = 8
    target_delta: float = 0.60
    breakout_buffer_pct: float = 0.05
    min_volume_ratio: float = 1.20
    max_spread_pct: float = 8.0
    min_volume: int = 1_000
    min_oi: int = 5_000
    risk_per_trade_pct: float = 0.50
    max_premium_allocation_pct: float = 5.0
    max_lots: int = 6
    premium_stop_pct: float = 25.0
    trail_activation_pct: float = 15.0
    trail_drawdown_pct: float = 8.0
    target_return_benchmark_pct: float = 15.0
    risk_free_rate: float = 0.06

    def validate(self) -> None:
        if not 0.5 <= self.target_delta <= 0.75:
            raise ValueError("buying delta must be between 0.50 and 0.75")
        if not 0 < self.risk_per_trade_pct <= 1.0:
            raise ValueError("buying risk per trade must be in (0, 1]")
        if not 0 < self.premium_stop_pct < 100:
            raise ValueError("premium stop must be in (0, 100)")
        if not 0 < self.trail_drawdown_pct < self.trail_activation_pct:
            raise ValueError("trailing drawdown must be below its activation return")
        if self.entry_start >= self.entry_end or self.entry_end >= self.square_off:
            raise ValueError("buying times must be entry_start < entry_end < square_off")


@dataclass(frozen=True)
class OptionLeg:
    role: str
    side: str
    instrument: OptionInstrument
    quantity: int
    limit_price: float | None
    sequence: int

    def as_order(self, product: str = "MIS") -> dict[str, Any]:
        return {
            "symbol": self.instrument.symbol,
            "side": self.side,
            "qty": self.quantity,
            "price": (_snap(self.limit_price, self.instrument.tick_size)
                      if self.limit_price is not None else None),
            "tick_size": self.instrument.tick_size,
            "product": product,
            "order_type": "LIMIT",
            "exchange": self.instrument.exchange,
            "role": self.role,
            "sequence": self.sequence,
            "requires_live_price": self.limit_price is None,
        }


@dataclass(frozen=True)
class OptionPlan:
    strategy: str
    created_at: dt.datetime
    underlying: str
    expiry: dt.date
    product: str
    lots: int
    lot_size: int
    entry_credit: float
    entry_debit: float
    max_loss: float
    target_profit: float
    stop_loss: float
    benchmark_return_pct: float
    square_off: dt.time
    entry_legs: tuple[OptionLeg, ...]
    exit_legs: tuple[OptionLeg, ...]
    reasons: tuple[str, ...]
    diagnostics: Mapping[str, Any] = field(default_factory=dict)
    paper_only: bool = True

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out.pop("entry_legs", None)
        out.pop("exit_legs", None)
        out["created_at"] = self.created_at.isoformat()
        out["expiry"] = self.expiry.isoformat()
        out["square_off"] = self.square_off.isoformat(timespec="minutes")
        out["entry_orders"] = [leg.as_order(self.product) for leg in self.entry_legs]
        out["exit_orders"] = [leg.as_order(self.product) for leg in self.exit_legs]
        return out


@dataclass(frozen=True)
class ExitDecision:
    exit: bool
    reason: str | None
    pnl: float
    return_pct: float | None


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes_price(spot: float, strike: float, years: float, rate: float,
                        volatility: float, kind: str) -> float:
    if min(spot, strike, years, volatility) <= 0:
        return 0.0
    root = volatility * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + volatility * volatility / 2.0) * years) / root
    d2 = d1 - root
    if kind == CE:
        return spot * _norm_cdf(d1) - strike * math.exp(-rate * years) * _norm_cdf(d2)
    return strike * math.exp(-rate * years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def implied_volatility(price: float, spot: float, strike: float, years: float,
                       rate: float, kind: str) -> float | None:
    if price <= 0 or years <= 0:
        return None
    intrinsic = max(spot - strike, 0.0) if kind == CE else max(strike - spot, 0.0)
    if price < intrinsic - 1e-6:
        return None
    lo, hi = 0.01, 5.0
    if black_scholes_price(spot, strike, years, rate, hi, kind) < price:
        return None
    for _ in range(70):
        mid = (lo + hi) / 2.0
        model = black_scholes_price(spot, strike, years, rate, mid, kind)
        if model < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def option_delta(spot: float, strike: float, years: float, rate: float,
                 volatility: float, kind: str) -> float:
    root = volatility * math.sqrt(max(years, 1e-12))
    d1 = (math.log(spot / strike) + (rate + volatility * volatility / 2.0) * years) / root
    return _norm_cdf(d1) if kind == CE else _norm_cdf(d1) - 1.0


def _years(as_of: dt.datetime, expiry: dt.date) -> float:
    close = dt.datetime.combine(expiry, dt.time(15, 30), tzinfo=as_of.tzinfo)
    return max((close - as_of).total_seconds() / (365.0 * 86400.0), 1e-8)


def _liquid(q: OptionQuote, max_spread: float, min_volume: int, min_oi: int) -> bool:
    return (q.bid > 0 and q.ask >= q.bid and q.spread_pct <= max_spread
            and q.volume >= min_volume and q.oi >= min_oi)


def _expiry(quotes: Sequence[OptionQuote], today: dt.date, minimum: int,
            maximum: int) -> dt.date:
    expiries = sorted({q.instrument.expiry for q in quotes
                       if minimum <= (q.instrument.expiry - today).days <= maximum})
    if not expiries:
        raise OptionPlanRejected("NO_EXPIRY", f"no expiry between {minimum} and {maximum} DTE")
    return expiries[0]


def _delta(q: OptionQuote, market: OptionMarketSnapshot, rate: float) -> float | None:
    if q.delta is not None:
        return q.delta
    years = _years(market.as_of, q.instrument.expiry)
    iv = q.iv or implied_volatility(q.mid, market.spot, q.instrument.strike, years,
                                    rate, q.instrument.kind)
    if iv is None:
        return None
    return option_delta(market.spot, q.instrument.strike, years, rate, iv,
                        q.instrument.kind)


def _pick(quotes: Iterable[OptionQuote], market: OptionMarketSnapshot, kind: str,
          target_abs_delta: float, rate: float, *, strike_below: float | None = None,
          strike_above: float | None = None) -> OptionQuote:
    ranked = []
    for q in quotes:
        if q.instrument.kind != kind:
            continue
        if strike_below is not None and q.instrument.strike >= strike_below:
            continue
        if strike_above is not None and q.instrument.strike <= strike_above:
            continue
        d = _delta(q, market, rate)
        if d is None:
            continue
        ranked.append((abs(abs(d) - target_abs_delta), q.spread_pct, -q.oi, q))
    if not ranked:
        raise OptionPlanRejected("NO_STRIKE", f"no liquid {kind} near delta {target_abs_delta}")
    return min(ranked, key=lambda x: x[:3])[3]


def _close_legs(entry: Sequence[OptionLeg]) -> tuple[OptionLeg, ...]:
    # Close risk-creating short legs first; release protective long wings last. Exit
    # prices are deliberately absent: the executor must attach fresh marketable LIMIT
    # prices when the exit fires, never reuse the entry quote.
    ordered = sorted(entry, key=lambda leg: (leg.side == BUY, leg.sequence))
    return tuple(OptionLeg(role=f"close_{leg.role}", side=BUY if leg.side == SELL else SELL,
                           instrument=leg.instrument, quantity=leg.quantity,
                           limit_price=None, sequence=i + 1)
                 for i, leg in enumerate(ordered))


class IronCondorPlanner:
    def __init__(self, cfg: SellingConfig | None = None):
        self.cfg = cfg or SellingConfig()
        self.cfg.validate()

    def plan(self, market: OptionMarketSnapshot) -> OptionPlan:
        c = self.cfg
        market.validate()
        now = market.as_of.time()
        if not c.entry_start <= now <= c.entry_end:
            raise OptionPlanRejected("ENTRY_WINDOW", "outside the iron-condor entry window")
        signal_price = market.trend_price
        vwap_distance = abs(signal_price / market.vwap - 1.0) * 100.0
        if vwap_distance > c.max_vwap_distance_pct:
            raise OptionPlanRejected("TRENDING", "spot is too far from VWAP for neutral selling")

        expiry = _expiry(market.quotes, market.as_of.date(), c.min_dte_days, c.max_dte_days)
        chain = [q for q in market.quotes if q.instrument.underlying == c.underlying
                 and q.instrument.expiry == expiry
                 and _liquid(q, c.max_spread_pct, c.min_volume, c.min_oi)]
        if not chain:
            raise OptionPlanRejected("ILLIQUID_CHAIN", "no option quotes pass spread/volume/OI gates")

        by_strike: dict[float, dict[str, OptionQuote]] = {}
        for q in chain:
            by_strike.setdefault(q.instrument.strike, {})[q.instrument.kind] = q
        pairs = [(abs(k - market.spot), pair) for k, pair in by_strike.items()
                 if CE in pair and PE in pair]
        if not pairs:
            raise OptionPlanRejected("NO_ATM_STRADDLE", "cannot estimate the live expected move")
        atm = min(pairs, key=lambda x: x[0])[1]
        expected_move_pct = (atm[CE].mid + atm[PE].mid) / market.spot * 100.0
        opening_range_pct = ((market.opening_range_high - market.opening_range_low)
                             / signal_price * 100.0)
        ratio = opening_range_pct / expected_move_pct if expected_move_pct else math.inf
        if ratio > c.max_range_to_expected_move:
            raise OptionPlanRejected("RANGE_EXPANDED", "opening range already consumed too much expected move")

        short_call = _pick((q for q in chain if q.instrument.strike > market.spot), market,
                           CE, c.short_delta, c.risk_free_rate)
        short_put = _pick((q for q in chain if q.instrument.strike < market.spot), market,
                          PE, c.short_delta, c.risk_free_rate)
        long_call = _pick(chain, market, CE, c.wing_delta, c.risk_free_rate,
                          strike_above=short_call.instrument.strike)
        long_put = _pick(chain, market, PE, c.wing_delta, c.risk_free_rate,
                         strike_below=short_put.instrument.strike)

        instruments = (short_call.instrument, short_put.instrument,
                       long_call.instrument, long_put.instrument)
        lot_sizes = {i.lot_size for i in instruments}
        if len(lot_sizes) != 1:
            raise OptionPlanRejected("LOT_MISMATCH", "legs do not share one live lot size")
        lot_size = lot_sizes.pop()
        credit = short_call.bid + short_put.bid - long_call.ask - long_put.ask
        call_width = long_call.instrument.strike - short_call.instrument.strike
        put_width = short_put.instrument.strike - long_put.instrument.strike
        width = max(call_width, put_width)
        if credit <= 0 or width <= credit:
            raise OptionPlanRejected("BAD_PAYOFF", "condor has no positive, bounded credit payoff")
        if credit / width * 100.0 < c.min_credit_width_pct:
            raise OptionPlanRejected("THIN_CREDIT", "credit is too small relative to wing width")

        max_loss_per_lot = (width - credit) * lot_size
        risk_budget = market.available_capital * c.risk_per_trade_pct / 100.0
        lots = min(int(risk_budget // max_loss_per_lot), c.max_lots)
        if lots < 1:
            required = max_loss_per_lot / (c.risk_per_trade_pct / 100.0)
            raise OptionPlanRejected("INSUFFICIENT_CAPITAL",
                                     f"one lot needs about Rs {required:,.0f} at this risk cap")
        qty = lots * lot_size

        # Protective wings are always first.  The system never creates a naked short leg.
        entry = (
            OptionLeg("call_wing", BUY, long_call.instrument, qty, long_call.ask, 1),
            OptionLeg("put_wing", BUY, long_put.instrument, qty, long_put.ask, 2),
            OptionLeg("short_call", SELL, short_call.instrument, qty, short_call.bid, 3),
            OptionLeg("short_put", SELL, short_put.instrument, qty, short_put.bid, 4),
        )
        total_credit = credit * qty
        plan = OptionPlan(
            strategy="intraday_defined_risk_iron_condor", created_at=market.as_of,
            underlying=c.underlying, expiry=expiry, product="MIS", lots=lots,
            lot_size=lot_size, entry_credit=round(total_credit, 2), entry_debit=0.0,
            max_loss=round(max_loss_per_lot * lots, 2),
            target_profit=round(total_credit * c.target_credit_capture_pct / 100.0, 2),
            stop_loss=round(min(max_loss_per_lot * lots,
                                total_credit * c.stop_credit_multiple), 2),
            benchmark_return_pct=c.monthly_return_benchmark_pct,
            square_off=c.square_off, entry_legs=entry, exit_legs=_close_legs(entry),
            reasons=("spot is close to VWAP", "opening range is inside implied move",
                     "all four legs pass liquidity gates", "maximum loss is defined"),
            diagnostics={"spot": market.spot, "signal_price": signal_price,
                         "signal_source": market.signal_source,
                         "vwap_distance_pct": round(vwap_distance, 3),
                         "opening_range_pct": round(opening_range_pct, 3),
                         "expected_move_pct": round(expected_move_pct, 3),
                         "range_to_expected_move": round(ratio, 3),
                         "credit_per_unit": round(credit, 2),
                         "call_width": call_width, "put_width": put_width,
                         "risk_budget": round(risk_budget, 2)},
        )
        return plan


class BreakoutBuyerPlanner:
    def __init__(self, cfg: BuyingConfig | None = None):
        self.cfg = cfg or BuyingConfig()
        self.cfg.validate()

    def plan(self, market: OptionMarketSnapshot) -> OptionPlan:
        c = self.cfg
        market.validate()
        now = market.as_of.time()
        if not c.entry_start <= now <= c.entry_end:
            raise OptionPlanRejected("ENTRY_WINDOW", "outside the option-buying entry window")
        if market.volume_ratio < c.min_volume_ratio:
            raise OptionPlanRejected("WEAK_VOLUME", "breakout volume is below confirmation")

        buffer = c.breakout_buffer_pct / 100.0
        signal_price = market.trend_price
        bullish = (signal_price > market.opening_range_high * (1.0 + buffer)
                   and signal_price > market.vwap and market.ema_fast > market.ema_slow)
        bearish = (signal_price < market.opening_range_low * (1.0 - buffer)
                   and signal_price < market.vwap and market.ema_fast < market.ema_slow)
        if bullish == bearish:
            raise OptionPlanRejected("NO_BREAKOUT", "price, VWAP and EMA trend do not agree")
        kind, direction = (CE, "bullish") if bullish else (PE, "bearish")

        expiry = _expiry(market.quotes, market.as_of.date(), c.min_dte_days, c.max_dte_days)
        chain = [q for q in market.quotes if q.instrument.underlying == c.underlying
                 and q.instrument.expiry == expiry and q.instrument.kind == kind
                 and _liquid(q, c.max_spread_pct, c.min_volume, c.min_oi)]
        if kind == CE:
            chain = [q for q in chain if q.instrument.strike <= market.spot]
        else:
            chain = [q for q in chain if q.instrument.strike >= market.spot]
        option = _pick(chain, market, kind, c.target_delta, c.risk_free_rate)

        lot_size = option.instrument.lot_size
        premium_per_lot = option.ask * lot_size
        risk_per_lot = premium_per_lot * c.premium_stop_pct / 100.0
        risk_budget = market.available_capital * c.risk_per_trade_pct / 100.0
        premium_budget = market.available_capital * c.max_premium_allocation_pct / 100.0
        lots = min(int(risk_budget // risk_per_lot), int(premium_budget // premium_per_lot),
                   c.max_lots)
        if lots < 1:
            raise OptionPlanRejected("INSUFFICIENT_CAPITAL",
                                     "one liquid option lot exceeds risk or premium budget")
        qty = lots * lot_size
        debit = option.ask * qty
        entry = (OptionLeg(f"long_{kind.lower()}", BUY, option.instrument, qty,
                           option.ask, 1),)
        return OptionPlan(
            strategy="intraday_option_breakout_buyer", created_at=market.as_of,
            underlying=c.underlying, expiry=expiry, product="MIS", lots=lots,
            lot_size=lot_size, entry_credit=0.0, entry_debit=round(debit, 2),
            max_loss=round(debit, 2),
            target_profit=round(debit * c.target_return_benchmark_pct / 100.0, 2),
            stop_loss=round(debit * c.premium_stop_pct / 100.0, 2),
            benchmark_return_pct=c.target_return_benchmark_pct,
            square_off=c.square_off, entry_legs=entry, exit_legs=_close_legs(entry),
            reasons=(f"{direction} opening-range breakout confirmed",
                     "spot, VWAP and EMA trend agree", "volume confirms the move",
                     "15% return activates a trailing exit; upside is not capped"),
            diagnostics={"direction": direction, "spot": market.spot,
                         "signal_price": signal_price,
                         "signal_source": market.signal_source,
                         "volume_ratio": market.volume_ratio,
                         "premium_per_lot": round(premium_per_lot, 2),
                         "risk_budget": round(risk_budget, 2),
                         "trail_activation_pct": c.trail_activation_pct,
                         "trail_drawdown_pct": c.trail_drawdown_pct},
        )


def evaluate_exit(plan: OptionPlan, current_prices: Mapping[str, float], now: dt.time,
                  *, peak_return_pct: float | None = None,
                  spot: float | None = None) -> ExitDecision:
    """Evaluate an already-filled plan. Missing leg prices fail closed with no exit signal."""
    if any(leg.instrument.symbol not in current_prices for leg in plan.entry_legs):
        return ExitDecision(False, "MISSING_QUOTES", 0.0, None)
    pnl = 0.0
    for leg in plan.entry_legs:
        move = float(current_prices[leg.instrument.symbol]) - float(leg.limit_price or 0.0)
        pnl += move * leg.quantity * (1.0 if leg.side == BUY else -1.0)
    base = plan.entry_debit or plan.entry_credit
    ret = pnl / base * 100.0 if base > 0 else None

    if now >= plan.square_off:
        return ExitDecision(True, "INTRADAY_SQUARE_OFF", round(pnl, 2), ret)
    if pnl <= -plan.stop_loss:
        return ExitDecision(True, "HARD_STOP", round(pnl, 2), ret)
    if plan.entry_credit and pnl >= plan.target_profit:
        return ExitDecision(True, "CREDIT_CAPTURED", round(pnl, 2), ret)
    if plan.entry_credit and spot is not None:
        short_calls = [l.instrument.strike for l in plan.entry_legs if l.role == "short_call"]
        short_puts = [l.instrument.strike for l in plan.entry_legs if l.role == "short_put"]
        if (short_calls and spot >= min(short_calls)) or (short_puts and spot <= max(short_puts)):
            return ExitDecision(True, "SHORT_STRIKE_BREACH", round(pnl, 2), ret)
    if plan.entry_debit and ret is not None:
        activation = float(plan.diagnostics.get("trail_activation_pct", 15.0))
        drawdown = float(plan.diagnostics.get("trail_drawdown_pct", 8.0))
        peak = max(ret, peak_return_pct if peak_return_pct is not None else ret)
        if peak >= activation and peak - ret >= drawdown:
            return ExitDecision(True, "TRAILING_PROFIT", round(pnl, 2), ret)
    return ExitDecision(False, None, round(pnl, 2), ret)


class IntradayIronCondorSeller(BaseStrategy):
    name = "intraday_defined_risk_iron_condor"
    products = ("MIS",)

    def __init__(self, gateway, cfg: SellingConfig | None = None):
        super().__init__(gateway)
        self.planner = IronCondorPlanner(cfg)

    async def generate_targets(self, context: dict) -> list[dict]:
        plan = self.planner.plan(context["market"])
        context["option_plan"] = plan.as_dict()
        return [leg.as_order(plan.product) for leg in plan.entry_legs]


class IntradayOptionBreakoutBuyer(BaseStrategy):
    name = "intraday_option_breakout_buyer"
    products = ("MIS",)

    def __init__(self, gateway, cfg: BuyingConfig | None = None):
        super().__init__(gateway)
        self.planner = BreakoutBuyerPlanner(cfg)

    async def generate_targets(self, context: dict) -> list[dict]:
        plan = self.planner.plan(context["market"])
        context["option_plan"] = plan.as_dict()
        return [leg.as_order(plan.product) for leg in plan.entry_legs]


def instruments_from_kite(rows: Iterable[Mapping[str, Any]],
                          underlying: str = "NIFTY") -> list[OptionInstrument]:
    """Resolve contracts dynamically; lot sizes and expiry weekdays are never hard-coded."""
    out = []
    for row in rows:
        if str(row.get("name") or "") != underlying:
            continue
        if str(row.get("instrument_type") or "").upper() not in (CE, PE):
            continue
        out.append(OptionInstrument.from_kite(row))
    return out
