"""HARD instrument guards — the lowest layer of the system.
Every order, GTT, and basket in ANY engine (rebalance, intraday, options) MUST pass
assert_tradeable() inside the gateway. No strategy code can bypass it because the
gateway is the only module allowed to talk to Kite's order APIs.
"""
from __future__ import annotations

# Symbols that must NEVER be traded by this system, in any product, any direction.
UNTOUCHABLE_SYMBOLS: frozenset[str] = frozenset({"SGBDE31III"})
# Prefix/série guards: all Sovereign Gold Bonds trade as SGB*; GB series = G-secs.
UNTOUCHABLE_PREFIXES: tuple[str, ...] = ("SGB",)
UNTOUCHABLE_SERIES: frozenset[str] = frozenset({"GB", "GS"})


class UntouchableInstrumentError(RuntimeError):
    pass


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
