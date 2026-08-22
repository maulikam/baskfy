"""Splitting one portfolio across several screens, and a slice you manage yourself (M34).

The Rebalance Tracker answers *"which symbols changed"* — enter, hold, exit, and nothing about
money (`docs/01` §8). That is the right answer for one screen and the wrong one for a portfolio
run as several: a person with a crore does not ask which names moved, they ask **how much goes
where**.

A **sleeve** is one slice of a portfolio with its own capital and its own source:

* a **screen sleeve** takes its names from a saved screen;
* a **manual sleeve** is capital you run yourself. It is carried through the arithmetic so the
  totals are honest, and nothing is ever proposed for it.

Pure — sleeves and symbols in, amounts out. No database, no market quotes, no clock (docs/02's
I/O-free rule). **The absence of any market quote is the point**: this produces rupee amounts and
target weights, and it is structurally incapable of producing a number of units to buy, because
that would need a quote it never receives. A unit count is one step from an order list, and
execution stays in the desk console until D3 is answered.

(`test_sleeves_are_not_orders.py` scans this module for the vocabulary of an order. That is why
the words are described here rather than written — the same collision M22.4 recorded.)

THE REGIME CAP IS AN INPUT, NOT A DECISION
------------------------------------------
The desk computes a policy tier weekly — R1 caps equity at 100%, R2 at 70%, R3 at 40%. That is a
*fact about the strategy*, and this module accepts it as a number. It does not fetch it, choose it,
or recommend it. A caller that passes ``None`` gets the sleeves sized at their full capital.

The cap applies to screen sleeves only. A manual sleeve is somebody's own money being run their
own way, and scaling it would be this module making a decision about capital it was told not to
touch.

WHY THE REMAINDER IS CASH AND NOT A ROUNDING ERROR
--------------------------------------------------
Equal weights over a sleeve rarely divide into whole rupees. The shortfall is assigned to the
sleeve's cash rather than smeared across the names, so **every sleeve reconciles to the rupee**:
``deployed + cash == capital``, asserted by test. A tracker whose columns do not add up is worse
than one that does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Final

#: A sleeve that draws its names from a saved screen.
SCREEN: Final = "screen"
#: A sleeve the owner runs themselves. Reported, never allocated.
MANUAL: Final = "manual"

#: Money is whole rupees here. Paise in a portfolio allocation is noise that makes a table harder
#: to read without making it more true (house rule 9 keeps the *type* exact; this is precision).
RUPEE: Final = Decimal("1")

#: A cap is a percentage. Named because the bound is the contract, not a magic number: passing a
#: fraction (0.7 for R2) instead of a percentage would silently deploy 0.7% of the portfolio.
FULL_ALLOCATION_PCT: Final = Decimal(100)


@dataclass(frozen=True, slots=True)
class SleeveSpec:
    """One slice of the portfolio, as configured."""

    name: str
    kind: str
    capital: Decimal
    #: The screen's names, in rank order. Empty for a manual sleeve.
    symbols: tuple[str, ...] = ()

    @property
    def is_screen(self) -> bool:
        return self.kind == SCREEN


@dataclass(frozen=True, slots=True)
class AllocationRow:
    symbol: str
    #: Percent of this sleeve, not of the portfolio. A sleeve is the unit a person reasons about.
    weight_pct: Decimal
    amount: Decimal


@dataclass(frozen=True, slots=True)
class SleeveAllocation:
    name: str
    kind: str
    capital: Decimal
    #: What the names add up to. Zero for a manual sleeve.
    deployed: Decimal
    #: `capital - deployed`: the regime cap's withholding plus the rounding remainder.
    cash: Decimal
    rows: tuple[AllocationRow, ...]

    @property
    def balances(self) -> bool:
        return self.deployed + self.cash == self.capital


@dataclass(frozen=True, slots=True)
class Allocation:
    sleeves: tuple[SleeveAllocation, ...]
    capital: Decimal
    deployed: Decimal
    cash: Decimal
    #: The cap that was applied, or None when the sleeves were sized at full capital.
    equity_cap_pct: Decimal | None

    @property
    def balances(self) -> bool:
        return self.deployed + self.cash == self.capital


def allocate(
    sleeves: list[SleeveSpec] | tuple[SleeveSpec, ...],
    *,
    equity_cap_pct: Decimal | None = None,
) -> Allocation:
    """Turn configured sleeves into rupee amounts.

    ``equity_cap_pct`` is the strategy's current tier cap — 100 at R1, 70 at R2, 40 at R3 — or
    ``None`` to size at full capital. It scales **screen sleeves only**; see the module docstring.
    """
    if equity_cap_pct is not None and not (0 <= equity_cap_pct <= FULL_ALLOCATION_PCT):
        raise ValueError(f"an equity cap must be a percentage; got {equity_cap_pct}")

    allocated = [_one(sleeve, equity_cap_pct) for sleeve in sleeves]
    return Allocation(
        sleeves=tuple(allocated),
        capital=sum((s.capital for s in allocated), Decimal(0)),
        deployed=sum((s.deployed for s in allocated), Decimal(0)),
        cash=sum((s.cash for s in allocated), Decimal(0)),
        equity_cap_pct=equity_cap_pct,
    )


def _one(sleeve: SleeveSpec, equity_cap_pct: Decimal | None) -> SleeveAllocation:
    if sleeve.capital < 0:
        raise ValueError(f"{sleeve.name}: capital cannot be negative")

    # A manual sleeve is reported and never allocated. Its capital is neither deployed by this
    # module nor called cash, because it is being run -- it is simply not this module's business.
    if not sleeve.is_screen or not sleeve.symbols:
        return SleeveAllocation(
            name=sleeve.name,
            kind=sleeve.kind,
            capital=sleeve.capital,
            deployed=Decimal(0),
            cash=sleeve.capital,
            rows=(),
        )

    budget = sleeve.capital
    if equity_cap_pct is not None:
        budget = (sleeve.capital * equity_cap_pct / FULL_ALLOCATION_PCT).quantize(
            RUPEE, rounding=ROUND_DOWN
        )

    count = len(sleeve.symbols)
    weight = (FULL_ALLOCATION_PCT / count).quantize(Decimal("0.01"))
    # ROUND_DOWN, so the sleeve can never propose more than its budget. The shortfall becomes
    # cash, which is the honest place for it.
    each = (budget / count).quantize(RUPEE, rounding=ROUND_DOWN)
    rows = tuple(
        AllocationRow(symbol=symbol, weight_pct=weight, amount=each) for symbol in sleeve.symbols
    )
    deployed = each * count
    return SleeveAllocation(
        name=sleeve.name,
        kind=sleeve.kind,
        capital=sleeve.capital,
        deployed=deployed,
        cash=sleeve.capital - deployed,
        rows=rows,
    )


def cap_for_tier(tier: str, caps: dict[str, Decimal] | None = None) -> Decimal | None:
    """The equity cap a policy tier implies, or None for a tier nobody has defined.

    The defaults mirror the desk's own settings (`REGIME_R*_EQUITY_PCT`): R1 100, R2 70, R3 40.
    They are *defaults*, not truth — the caller should pass the desk's live configuration when it
    has it, because a cap this module invented would disagree with the one the desk trades on.
    """
    table = caps or {"R1": Decimal(100), "R2": Decimal(70), "R3": Decimal(40), "R4": Decimal(0)}
    return table.get(tier.upper())
