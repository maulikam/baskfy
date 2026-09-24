"""HARD instrument guards — the lowest layer of the system.
Every order, GTT, and basket in ANY engine (rebalance, intraday, options) MUST pass
assert_tradeable() inside the gateway. No strategy code can bypass it because the
gateway is the only module allowed to talk to Kite's order APIs.
"""
from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

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
#: Cash-equity venues. Anything else is either a derivative exchange (needs OPTIONS_ENABLED)
#: or an unknown venue the gateway refuses rather than quietly sending.
CASH_EXCHANGES: frozenset[str] = frozenset({"NSE", "BSE"})


#: The one product/venue pair the FO run may carry past the close (``docs/fno/02`` §1).
FO_CARRY_PRODUCT = "NRML"
FO_CARRY_EXCHANGE = "NFO"


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


# --- the FO run: carry on NFO, covered overnight (docs/fno/02 §1, 04 §2; FO6) ---------------
#
# The 16 Aug policy above stays in force for every order WITHOUT a ``fo_plan`` reference —
# every O1-O3 order, every cash order, every desk book — and ``assert_not_overnight_option`` is
# untouched. An order that carries a ``FoPlanRef`` meets a different, narrower guard instead:
# an option leg under a carry product passes only when the book AFTER the order has no short
# option without a long of the same underlying, type and expiry further from the money, in at
# least the same quantity. That is ``baskfy_core.fno.covered``'s predicate. It is not re-coded
# here: ``packages/execution`` does not import ``baskfy_core`` (``tenancy.py``), so the rule is
# INJECTED into the gateway (``OrderGateway(coverage=...)``), fail closed — no rule wired means
# every FO option order is refused (DECISIONS-FO FO6.1).

#: The ``fo_sleeve`` codes (``docs/fno/01`` §4). F1 trades index option structures, F2 stock
#: futures; anything else is not an FO plan and is refused.
FO_OPTION_SLEEVES: frozenset[str] = frozenset({"F1N", "F1B"})
FO_FUTURE_SLEEVES: frozenset[str] = frozenset({"F2"})
#: Index underlyings: a GTT is admitted for a STOCK future only (``02`` §1, M.1).
INDEX_UNDERLYINGS: frozenset[str] = frozenset(
    {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX", "SENSEX50"}
)
_EXPIRY_CODE = re.compile(r"\d{2}(?:[A-Z]{3}|[1-9OND]\d{2})")
_FUTURE = re.compile(r"^([A-Z0-9&\-]+?)(\d{2}[A-Z]{3})FUT$")


class UncoveredOptionError(OvernightOptionError):
    """An FO option order the covered-overnight guard refuses. A subclass on purpose: anything
    that already catches ``OvernightOptionError`` treats it as the same kind of refusal."""


class SignedOptionLeg(Protocol):
    """One option position in units, positive long, negative short.

    ``baskfy_core.fno.covered.OptionPosition`` satisfies it structurally; execution declares only
    the shape it reads, so it does not import core.
    """

    @property
    def underlying(self) -> str: ...
    @property
    def expiry(self) -> dt.date: ...
    @property
    def option_type(self) -> str: ...
    @property
    def strike(self) -> Decimal: ...
    @property
    def quantity(self) -> int: ...


#: ``book_after -> reasons``; empty means covered. Wire ``baskfy_core.fno.covered.uncovered``.
CoverageRule = Callable[[tuple[SignedOptionLeg, ...]], Sequence[str]]


@dataclass(frozen=True)
class FoPlanRef:
    """The ``fo_plan`` reference an FO order carries (``docs/fno/02`` §1).

    ``step`` is this order as a signed option position (options only). ``broker_positions`` is
    the broker's NRML option book **net of this plan's own fills** — e.g. the snapshot taken
    before the plan's first leg, or ``covered.net_of_plan(raw, plan_filled)`` — and
    ``plan_filled`` the plan's filled legs. The guard adds the three, so a fill counted in both
    would count twice and overstate the cover (DECISIONS-FO FO1.2).
    """

    plan_id: str
    sleeve: str
    step: SignedOptionLeg | None = None
    broker_positions: tuple[SignedOptionLeg, ...] = ()
    plan_filled: tuple[SignedOptionLeg, ...] = ()


def future_underlying(symbol: str) -> str | None:
    """``RELIANCE26SEPFUT`` -> ``RELIANCE``; ``None`` for anything that is not a future."""
    m = _FUTURE.match((symbol or "").upper().strip())
    return m.group(1) if m else None


def fo_order_refusal(symbol: str, exchange: str, fo_plan: FoPlanRef) -> str:
    """Why this ``fo_plan`` reference cannot ride on this order, or ``""``.

    An FO plan is on NFO; F1 legs are options and F2 legs are futures. A reference that fails
    this is not a reference, and the order is refused rather than routed down either path.
    """
    if not (fo_plan.plan_id or "").strip():
        return "fo_plan reference carries no plan_id"
    sleeve = (fo_plan.sleeve or "").upper().strip()
    if sleeve not in FO_OPTION_SLEEVES | FO_FUTURE_SLEEVES:
        return f"fo_plan {fo_plan.plan_id}: {fo_plan.sleeve!r} is not an FO sleeve"
    if (exchange or "").upper().strip() != FO_CARRY_EXCHANGE:
        return f"fo_plan {fo_plan.plan_id}: FO orders are on NFO, not {exchange!r}"
    if sleeve in FO_OPTION_SLEEVES and not is_option(symbol):
        return f"fo_plan {fo_plan.plan_id}: {sleeve} trades options; {symbol} is not one"
    if sleeve in FO_FUTURE_SLEEVES and future_underlying(symbol) is None:
        return f"fo_plan {fo_plan.plan_id}: {sleeve} trades futures; {symbol} is not one"
    return ""


def _strike_text(strike: Decimal) -> str:
    return format(Decimal(strike).normalize(), "f")


def _leg_mismatch(symbol: str, qty: int, side: str, step: SignedOptionLeg) -> str:
    """The order must BE the step the guard assesses: underlying, strike, type, sign, size."""
    sym = (symbol or "").upper().strip()
    side_u = (side or "").upper().strip()
    if side_u not in ("BUY", "SELL"):
        return f"side {side!r} is neither BUY nor SELL"
    signed = abs(int(qty)) if side_u == "BUY" else -abs(int(qty))
    if int(qty) <= 0 or step.quantity != signed:
        return f"{symbol}: the order is {side_u} {qty} but the plan step is {step.quantity}"
    underlying = str(step.underlying).upper()
    tail = f"{_strike_text(step.strike)}{str(step.option_type).upper()}"
    if not sym.startswith(underlying):
        return f"{symbol}: the plan step is on {step.underlying}"
    if not sym.endswith(tail):
        return f"{symbol}: the plan step is the {tail} leg"
    # What sits between must be exactly an expiry (monthly 26SEP, weekly 26922), so NIFTY cannot
    # match a NIFTYNXT50 symbol and strike 23700 cannot match a symbol for 123700.
    if not _EXPIRY_CODE.fullmatch(sym[len(underlying) : len(sym) - len(tail)]):
        return f"{symbol}: does not read as {underlying} <expiry> {tail}"
    return ""


def assert_overnight_option_is_covered(
    symbol: str,
    qty: int,
    side: str,
    *,
    fo_plan: FoPlanRef,
    coverage: CoverageRule | None,
) -> None:
    """``docs/fno/04`` §2, for orders that carry a ``fo_plan`` reference only.

    Refuses an option order unless every (underlying, expiry, type) of the book after it —
    broker positions (net of this plan's fills) + the plan's filled legs + this order — has
    every short covered by at least as many longs at strikes further from the money. Runs before
    any network call. Futures carry no cover rule here; their protection is the GTT (§2.3).
    """
    if not is_option(symbol):
        return
    step = fo_plan.step
    if step is None:
        raise UncoveredOptionError(
            f"{symbol}: fo_plan {fo_plan.plan_id} names no step for this option order, so its "
            f"cover cannot be assessed")
    mismatch = _leg_mismatch(symbol, qty, side, step)
    if mismatch:
        raise UncoveredOptionError(f"fo_plan {fo_plan.plan_id}: {mismatch}")
    if coverage is None:
        raise UncoveredOptionError(
            f"{symbol}: no covered-overnight rule is wired into this gateway; an FO option "
            f"order is refused until one is")
    reasons = tuple(coverage((*fo_plan.broker_positions, *fo_plan.plan_filled, step)))
    if reasons:
        raise UncoveredOptionError(
            f"{symbol}: the book after this order would hold an uncovered short option — "
            + "; ".join(reasons))


def fo_gtt_refusal(
    symbol: str,
    exchange: str,
    fo_plan: FoPlanRef,
    *,
    options_enabled: bool,
    fno_carry_enabled: bool,
) -> str:
    """The GTT branch of ``docs/fno/02`` §1: an F2 stock future on NFO/NRML, both flags on.

    An option GTT is refused whatever the switches and whatever the plan (§2.4): a trigger on a
    long leg would un-hedge the short.
    """
    if is_option(symbol):
        return (
            f"{symbol}: an option GTT is refused whatever the switches; an option structure's "
            f"stop is its structure (docs/fno/02 §2.4)")
    why = fo_order_refusal(symbol, exchange, fo_plan)
    if why:
        return why
    if fo_plan.sleeve.upper().strip() not in FO_FUTURE_SLEEVES:
        return f"fo_plan {fo_plan.plan_id}: only an F2 future may rest a GTT on NFO"
    underlying = future_underlying(symbol)
    if underlying is None or underlying in INDEX_UNDERLYINGS:
        return f"{symbol}: a GTT on NFO is admitted for a stock future only"
    return product_exchange_refusal(
        FO_CARRY_PRODUCT,
        exchange,
        intraday_enabled=False,
        options_enabled=options_enabled,
        fno_carry_enabled=fno_carry_enabled,
        fo_plan=True,
    )


def assert_tradeable(symbol: str, series: str | None = None) -> None:
    s = (symbol or "").upper().strip()
    if s in UNTOUCHABLE_SYMBOLS or any(s.startswith(p) for p in UNTOUCHABLE_PREFIXES) \
       or (series or "").upper() in UNTOUCHABLE_SERIES:
        raise UntouchableInstrumentError(
            f"{symbol}: protected instrument (SGB/G-sec). This system will never trade it.")


def product_exchange_refusal(
    product: str,
    exchange: str,
    *,
    intraday_enabled: bool,
    options_enabled: bool,
    fno_carry_enabled: bool = False,
    fo_plan: bool = False,
) -> str:
    """Why this product/exchange pair is refused, or "" when the allow-list admits it.

    Allow-list, not deny-list (AF 0.7): CNC on NSE/BSE is the only cash path; MIS needs
    ``intraday_enabled``; any venue in ``DERIVATIVE_EXCHANGES`` needs ``options_enabled``.
    A deny-list of NFO/BFO alone let MCX/NRML and CDS through every guard.

    **A derivative venue admits MIS only, and MIS needs ``intraday_enabled`` there too** (OP2,
    ``docs/options/DECISIONS-OP.md`` OP0.6/OP2.1). Until 22 Sep 2026 this branch returned "" for
    *any* product once ``options_enabled`` was true, so the flag alone admitted NFO MIS without
    the intraday switch and NRML futures — non-negotiable 5 says "MIS needs INTRADAY_ENABLED",
    and nothing in this system may carry a derivative past the close. Both flags are false
    everywhere, so nothing in force changed when this tightened.

    **One branch more, for the FO run** (``docs/fno/02`` §1, FO6): ``NRML`` on ``NFO`` is
    admitted when ``options_enabled`` **and** ``fno_carry_enabled`` are both true **and** the
    order carries a ``fo_plan`` reference. Nothing else moves: without ``fo_plan`` the NRML
    refusal is the old one word for word (every O1-O3 order meets it), BFO/MCX/CDS/BCD admit
    MIS only exactly as before, and ``INTRADAY_ENABLED`` is not consulted for this branch (an FO
    order is NRML, never MIS). Both new keywords default to ``False``, so every existing caller
    gets the old function. The option leg of such an order still has to pass
    :func:`assert_overnight_option_is_covered` in the gateway.
    """
    product_u = (product or "").upper().strip()
    exchange_u = (exchange or "").upper().strip()
    if exchange_u in DERIVATIVE_EXCHANGES:
        if not options_enabled:
            return "F&O/derivatives disabled (config.OPTIONS_ENABLED)"
        if product_u == FO_CARRY_PRODUCT and exchange_u == FO_CARRY_EXCHANGE and fo_plan:
            if not fno_carry_enabled:
                return (
                    f"{product_u}: only MIS is allowed on a derivative venue ({exchange_u}); "
                    f"an fo_plan order may carry NRML only with BASKFY_FNO_CARRY_ENABLED"
                )
            return ""
        if product_u != "MIS":
            return (
                f"{product_u or product!r}: only MIS is allowed on a derivative venue "
                f"({exchange_u}); nothing is carried past the close"
            )
        if not intraday_enabled:
            return "MIS/intraday disabled (config.INTRADAY_ENABLED)"
        return ""
    if exchange_u not in CASH_EXCHANGES:
        return (
            f"{exchange_u or exchange!r}: exchange is neither cash equity (NSE/BSE) nor an "
            f"enabled derivative venue"
        )
    if product_u == "MIS":
        if not intraday_enabled:
            return "MIS/intraday disabled (config.INTRADAY_ENABLED)"
        return ""
    if product_u != "CNC":
        return (
            f"{product_u or product!r}: only CNC is allowed on cash equity unless "
            f"INTRADAY_ENABLED (MIS)"
        )
    return ""


def filter_tradeable(symbols: list[str]) -> list[str]:
    out = []
    for s in symbols:
        try:
            assert_tradeable(s)
            out.append(s)
        except UntouchableInstrumentError:
            pass
    return out
