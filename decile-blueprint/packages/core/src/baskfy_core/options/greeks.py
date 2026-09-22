"""Black-76 on the put-call-parity forward: price, IV, delta, gamma, theta, vega (``04`` §2.3).

Kite gives no greeks (PACK.8), so they are computed here. The model is **Black-76 on the forward
``F``** that ``chain.parity_forward`` derives from the ATM pair, not Black-Scholes on spot: the
forward absorbs dividends and the rate, which spot does not, and on O2's multi-day contracts the
difference is a biased delta. Time is **calendar** time to 15:30 on expiry, ``minutes / (365 *
24 * 60)`` — one convention for 0-DTE and 7-DTE.

Floats, deliberately: these are model outputs (``numeric(10,6)``, rounded at write by the
collector — house rule 8), not money. Money stays ``Decimal`` everywhere it is money.

Ported by re-implementation from the frozen lab's ``strategies/options.py``
(``black_scholes_price``, ``implied_volatility``, ``option_delta``; PACK.2): its bracketed
bisection on [0.01, 5.0] survives, its spot-based model is replaced by Black-76 on ``F`` (``04``
§2.3 over condor ``04`` §3.3).
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from baskfy_core.options.config import OptionType

_MINUTES_PER_YEAR = 365.0 * 24.0 * 60.0
_DAYS_PER_YEAR = 365.0
_VOL_POINT = 100.0


def year_fraction(now: dt.datetime, expiry: dt.date, settle: dt.time) -> float:
    """``T`` in calendar years from ``now`` to ``settle`` (15:30) on ``expiry`` (``04`` §2.3).

    ``now`` is naive IST or aware; the settlement moment takes ``now``'s tzinfo. Negative after
    the settlement — callers refuse ``T <= 0``.
    """
    close = dt.datetime.combine(expiry, settle, tzinfo=now.tzinfo)
    return (close - now).total_seconds() / 60.0 / _MINUTES_PER_YEAR


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(forward: float, strike: float, years: float, vol: float) -> tuple[float, float]:
    root = vol * math.sqrt(years)
    d1 = (math.log(forward / strike) + 0.5 * vol * vol * years) / root
    return d1, d1 - root


def _valid(forward: float, strike: float, years: float, vol: float) -> bool:
    return forward > 0 and strike > 0 and years > 0 and vol > 0


def black76_price(  # noqa: PLR0913, PLR0917 - the model's inputs, in the textbook order
    forward: float, strike: float, years: float, rate: float, vol: float, kind: OptionType
) -> float:
    """The Black-76 premium; the discounted intrinsic when the inputs are degenerate."""
    discount = math.exp(-rate * max(years, 0.0))
    if not _valid(forward, strike, years, vol):
        intrinsic = forward - strike if kind is OptionType.CE else strike - forward
        return discount * max(intrinsic, 0.0)
    d1, d2 = _d1_d2(forward, strike, years, vol)
    if kind is OptionType.CE:
        return discount * (forward * _norm_cdf(d1) - strike * _norm_cdf(d2))
    return discount * (strike * _norm_cdf(-d2) - forward * _norm_cdf(-d1))


def intrinsic(forward: float, strike: float, kind: OptionType) -> float:
    """Intrinsic value on the forward, undiscounted — ``04`` §2.3's refusal floor."""
    return max(forward - strike, 0.0) if kind is OptionType.CE else max(strike - forward, 0.0)


def implied_vol(  # noqa: PLR0913, PLR0917 - the model's inputs, in the textbook order
    price: float,
    forward: float,
    strike: float,
    years: float,
    rate: float,
    kind: OptionType,
    *,
    lower: float,
    upper: float,
    tolerance: float,
) -> float | None:
    """The Black-76 vol that reprices ``price``, by bisection on ``[lower, upper]``.

    ``None`` — never a clipped number — when the price is not bracketed by the model at the two
    bounds (below the lowest-vol price or above the highest) or the inputs are degenerate: the
    solver "does not converge" in ``04`` §2.3's words.
    """
    if price <= 0 or years <= 0 or forward <= 0 or strike <= 0:
        return None
    low_price = black76_price(forward, strike, years, rate, lower, kind)
    high_price = black76_price(forward, strike, years, rate, upper, kind)
    if price < low_price or price > high_price:
        return None
    lo, hi = lower, upper
    while hi - lo > tolerance:
        mid = (lo + hi) / 2.0
        if black76_price(forward, strike, years, rate, mid, kind) < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


@dataclass(frozen=True, slots=True)
class Greeks:
    """Black-76 on ``F``: delta (per point of ``F``), gamma, theta per calendar day, vega per
    vol point."""

    iv: float
    delta: float
    gamma: float
    theta: float
    vega: float


def black76_greeks(  # noqa: PLR0913, PLR0917 - the model's inputs, in the textbook order
    forward: float, strike: float, years: float, rate: float, vol: float, kind: OptionType
) -> Greeks:
    """Analytic greeks at ``vol``. Raises on degenerate inputs — callers refuse first."""
    if not _valid(forward, strike, years, vol):
        raise ValueError("greeks need a positive forward, strike, time and vol")
    discount = math.exp(-rate * years)
    d1, _ = _d1_d2(forward, strike, years, vol)
    density = _norm_pdf(d1)
    root_t = math.sqrt(years)
    price = black76_price(forward, strike, years, rate, vol, kind)
    delta = discount * _norm_cdf(d1) if kind is OptionType.CE else -discount * _norm_cdf(-d1)
    gamma = discount * density / (forward * vol * root_t)
    decay = discount * forward * density * vol / (2.0 * root_t)
    theta_year = rate * price - decay
    vega = discount * forward * density * root_t / _VOL_POINT
    return Greeks(iv=vol, delta=delta, gamma=gamma, theta=theta_year / _DAYS_PER_YEAR, vega=vega)


def solve(  # noqa: PLR0913, PLR0917 - the model's inputs, in the textbook order
    mid: float,
    forward: float,
    strike: float,
    years: float,
    rate: float,
    kind: OptionType,
    *,
    min_premium: float,
    tick: float,
    lower: float,
    upper: float,
    tolerance: float,
) -> Greeks | None:
    """IV and greeks for one quote, or ``None`` where ``04`` §2.3 refuses.

    Refused when ``mid < min_premium``, when ``mid`` is below intrinsic on ``F`` plus one tick,
    when ``T <= 0``, or when the solver does not converge. A refused contract has no delta and
    can never be a leg whose selection depends on delta.
    """
    if years <= 0 or mid < min_premium:
        return None
    if mid < intrinsic(forward, strike, kind) + tick:
        return None
    vol = implied_vol(
        mid, forward, strike, years, rate, kind, lower=lower, upper=upper, tolerance=tolerance
    )
    if vol is None:
        return None
    return black76_greeks(forward, strike, years, rate, vol, kind)
