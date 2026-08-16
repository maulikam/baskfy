"""HARD instrument guards — the lowest layer of the system.
Every order, GTT, and basket in ANY engine (rebalance, intraday, options) MUST pass
assert_tradeable() inside the gateway. No strategy code can bypass it because the
gateway is the only module allowed to talk to Kite's order APIs.
"""
from __future__ import annotations

import re

# Symbols that must NEVER be traded by this system, in any product, any direction.
UNTOUCHABLE_SYMBOLS: frozenset[str] = frozenset({"SGBDE31III"})
# Prefix/série guards: all Sovereign Gold Bonds trade as SGB*; GB series = G-secs.
UNTOUCHABLE_PREFIXES: tuple[str, ...] = ("SGB",)
UNTOUCHABLE_SERIES: frozenset[str] = frozenset({"GB", "GS"})


# --- overnight option carry --------------------------------------------------------------
# Policy decision, 16 Aug 2026: this system never holds an option position past the close.
# Hard-coded rather than a config flag for the same reason the SGB list is: a knob that can
# be turned is not a guarantee, and the risk here is the kind that arrives overnight when
# nobody is at the desk. A short option gaps against you with no stop that can fire while
# the market is shut, and on the eve of an expiry it also carries an ELM of 2% of contract
# value per short leg — Rs 63,440 on one NIFTY lot, charged whether or not the position is
# hedged.
#
# The rule blocks BOTH directions, not just sells. Blocking only sells would leave a long
# option openable under NRML and then refuse the sell that closes it — a guard that traps a
# position is worse than the risk it was written to prevent. Since no option can be opened
# under a carry product, none can ever need closing under one.
CARRY_PRODUCTS: frozenset[str] = frozenset({"NRML", "CNC"})
DERIVATIVE_EXCHANGES: frozenset[str] = frozenset({"NFO", "BFO", "CDS", "BCD", "MCX"})


class UntouchableInstrumentError(RuntimeError):
    pass


class OvernightOptionError(RuntimeError):
    """Raised for any option order that could be held past the close."""


def is_option(symbol: str) -> bool:
    """NSE/BSE option tradingsymbols end in a STRIKE followed by CE or PE.

    The digit matters: RELIANCE and JUSTDIAL end in CE and AL, and a bare suffix test
    classifies RELIANCE as a call. Only the exchange check stopped that being a live false
    positive, and a predicate that is wrong on its own will eventually be called on its own.
    """
    return bool(re.search(r"\d(CE|PE)$", (symbol or "").upper().strip()))


def assert_not_overnight_option(symbol: str, exchange: str, product: str) -> None:
    """Refuse any option order placed under a product that can carry past the close.

    MIS is squared off intraday by the broker; NRML and CNC do not. This is checked at the
    same layer as the untouchable list, so no engine can route around it.
    """
    if (exchange or "").upper() in DERIVATIVE_EXCHANGES and is_option(symbol) \
       and (product or "").upper() in CARRY_PRODUCTS:
        raise OvernightOptionError(
            f"{symbol}: {product} would carry this option past the close. This system "
            f"never holds an option overnight — option orders are MIS only.")


def assert_tradeable(symbol: str, series: str | None = None) -> None:
    s = (symbol or "").upper().strip()
    if s in UNTOUCHABLE_SYMBOLS or any(s.startswith(p) for p in UNTOUCHABLE_PREFIXES) \
       or (series or "").upper() in UNTOUCHABLE_SERIES:
        raise UntouchableInstrumentError(
            f"{symbol}: protected instrument (SGB/G-sec). This system will never trade it.")


def filter_tradeable(symbols: list[str]) -> list[str]:
    out = []
    for s in symbols:
        try:
            assert_tradeable(s)
            out.append(s)
        except UntouchableInstrumentError:
            pass
    return out
