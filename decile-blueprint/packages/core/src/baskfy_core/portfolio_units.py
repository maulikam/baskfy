"""Capital + target weights + a price map -> whole unit counts (tree 5, leaf A3).

``portfolio_sleeve`` carries ``capital`` and ``top_n`` and no quantity column, so a sleeve has
never been able to say *what you actually hold*. That was structural rather than an oversight:
``baskfy_core.sleeves`` receives no price, and a unit count needs one. This module is the pure
half of closing that gap — the arithmetic that turns money and weights into units once somebody
else has done the fetching.

**Prices arrive as an argument.** Nothing here reaches a broker, a database or a clock (law 1).
The caller that has the quotes passes them in; this module has no way to obtain one and no way
to guess one.

WHAT THIS IS NOT
----------------
Not an order list. A unit count is a *report* — "this sleeve targets 47 units and holds 40" —
and the report names no side, no product, no order type and no venue. Execution stays in the
desk console behind ``POST /execute`` with an explicit confirmation (non-negotiable #1), and
``test_portfolio_units.py`` scans this module for the vocabulary of an order so that a future
edit which quietly crosses that line fails a test rather than a review.

The line M34 drew for ``sleeves`` has not moved; this module sits on the other side of it *on
purpose*, and says so, rather than a second module drifting across it unannounced.

WHY THE REMAINDER IS EXPLICIT
-----------------------------
You cannot buy 3.4 shares. Capital times a weight almost never divides into a whole number of
units, so something has to happen to the leftover, and there are only two honest options: hand
it back as cash, or smear it across the names by rounding units up. Rounding up spends money the
investor does not have, so the leftover is cash and it is a field:

    sum(units x price) + remainder == capital

exactly, in ``Decimal``, asserted by :attr:`UnitAllocation.balances` and by test. This is the
same rule and the same reasoning as ``sleeves`` ("deployed + cash == capital") and
``basket_sizing`` ("deployed + cash == amount"); a third module inventing a third answer for the
leftover is how three surfaces come to disagree about one number.

WHERE THIS DIVERGES FROM ``sleeves`` AND ``basket_sizing``, AND WHY
------------------------------------------------------------------
Both of those quantise money to whole rupees (``RUPEE = Decimal(1)``) because paise in a
*proposed allocation table* are noise. Here the remainder is not a proposal — it is the cash that
would genuinely still be sitting in the account after buying whole units at these prices.
Rounding it down would quietly lose up to a rupee per sleeve and break the identity above, so
nothing is quantised: units are exact integers, prices arrive at their stored precision (house
rule 8 already rounded them at write time — see :mod:`baskfy_core.precision`), and every total
here is an exact sum of those. The rounding this module does is the only rounding it *can* do
honestly: down, to a whole unit.

WHY A MISSING PRICE RAISES INSTEAD OF BEING REPORTED
----------------------------------------------------
An unpriced name has two tempting silent treatments — call it zero, or drop it — and both make a
sleeve's total wrong on a surface where a wrong total costs money. Returning a result that
merely *carries* the unpriced names has the same failure mode one field further away: the result
would still have to publish a ``deployed`` and a ``remainder`` for the names it could price, and
every caller that forgot to check the extra field would render a number that is wrong by exactly
the missing names' worth. An exception cannot be forgotten. It is also actionable by the only
party who can act: whoever fetched the quotes knows how to retry or to tell the reader that
prices are unavailable. Every unpriceable name is named in one raise, because a caller
discovering them one retry at a time is a worse loop than a caller fixing them all at once.

MONEY IS ``Decimal``, AND A ``float`` IS REFUSED RATHER THAN COERCED
--------------------------------------------------------------------
House rule 9. ``Decimal(1234.55)`` is not 1234.55, and a coercion here would bake that error
into a rupee total and into the unit count derived from it. The refusal is a ``TypeError`` and
it names the rule, because the realistic way a float arrives is a JSON body deserialised without
``parse_float=Decimal`` — a boundary bug, and the boundary is where it should be fixed.

Unit *counts* are ``int``: whole is the whole point. A broker or a ``numeric`` column that hands
over a ``Decimal`` quantity is accepted and required to be integral, so non-negotiable #2's
``quantity + t1_quantity + collateral_quantity`` can be passed straight through.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Final

from baskfy_core.curated_baskets import WEIGHT_QUANTIZE

__all__ = [
    "FULL_WEIGHT",
    "WEIGHT_TOLERANCE",
    "InvalidPriceError",
    "MissingPriceError",
    "PriceRefusedError",
    "UnitAllocation",
    "UnitLine",
    "allocate_units",
    "units_affordable",
]

#: Target weights are shares of the sleeve's capital. One whole sleeve is 1, never 100.
FULL_WEIGHT: Final = Decimal(1)

#: The same four decimal places ``cb_constituent.weight`` stores and
#: ``curated_baskets.assert_weights_sum_to_one`` compares at. Reused rather than restated so that
#: a weight vector this module accepts is one that module would also accept.
WEIGHT_TOLERANCE: Final[Decimal] = WEIGHT_QUANTIZE

ZERO: Final = Decimal(0)

#: ``Decimal`` arithmetic obeys a 28-significant-digit context by default, and a product carried
#: past it is *rounded*. Rounding ``capital x weight`` up, even in the 28th digit, would hand a
#: name a budget the capital does not have and could put the allocation a paisa over — and
#: :attr:`UnitAllocation.balances`, evaluated at the default context, would answer ``False`` for
#: totals that are in fact exact. Every product and sum here, in the allocator and in the derived
#: properties alike, is therefore taken at a precision no realistic input can reach, so "never
#: more than the capital affords" is a guarantee rather than a statement about typical
#: magnitudes. :func:`units_affordable` needs no such widening: it divides on integer ratios,
#: which are exact at any size.
_EXACT_PRECISION: Final = 200


class PriceRefusedError(ValueError):
    """At least one name could not be priced, so no allocation was produced.

    A ``ValueError`` because it is a statement about the values passed in, which is what an API
    layer already renders as a 400. :attr:`symbols` carries every offending name, not the first.
    """

    def __init__(self, message: str, symbols: Sequence[str]) -> None:
        super().__init__(message)
        self.symbols: tuple[str, ...] = tuple(symbols)


class MissingPriceError(PriceRefusedError):
    """A name in the allocation has no entry in the price map, or its entry is ``None``."""


class InvalidPriceError(PriceRefusedError):
    """A name has a price that cannot buy anything — zero or negative.

    Zero is the case worth naming: it is what a suspended or unbacked instrument prints, and
    dividing capital by it is a division by zero rather than an infinite position. Treating it as
    "0 units" instead would understate the sleeve without saying so.
    """


@dataclass(frozen=True, slots=True)
class UnitLine:
    """One name: what the weights ask for, what is actually held, and the gap between them."""

    symbol: str
    #: Share of the sleeve's capital this name was targeted at. ``0`` is legal and means the
    #: sleeve no longer targets a name it may still hold.
    weight: Decimal
    #: The price the units were counted at, exactly as supplied.
    price: Decimal
    #: Whole units the capital affords at ``weight`` and ``price``. Never rounded up.
    target_units: int
    #: Whole units actually held, as reported by the caller. ``0`` when nothing is held.
    held_units: int
    #: ``target_units x price``.
    target_value: Decimal
    #: ``held_units x price``.
    held_value: Decimal

    @property
    def drift_units(self) -> int:
        """Target minus held. Positive means the sleeve is short of its target."""
        return self.target_units - self.held_units

    @property
    def drift_value(self) -> Decimal:
        """The gap at this price. Positive means the sleeve is short of its target."""
        with localcontext() as context:
            context.prec = _EXACT_PRECISION
            return self.target_value - self.held_value

    @property
    def is_on_target(self) -> bool:
        return self.drift_units == 0


@dataclass(frozen=True, slots=True)
class UnitAllocation:
    """A sleeve's capital expressed in whole units, with the leftover cash stated."""

    rows: tuple[UnitLine, ...]
    #: The capital that was divided, exactly as supplied.
    capital: Decimal
    #: ``sum(target_units x price)`` — what the target units cost at these prices.
    deployed: Decimal
    #: ``capital - deployed``. Whole units never divide the capital evenly; this is the leftover,
    #: plus any capital the weights deliberately left unallocated.
    remainder: Decimal
    #: ``sum(held_units x price)`` — what is actually held, marked at the same prices.
    held_value: Decimal
    #: What the weights summed to. Below :data:`FULL_WEIGHT` when the caller held some back.
    weight_total: Decimal

    @property
    def balances(self) -> bool:
        """The contract: ``sum(units x price) + remainder == capital``, exactly."""
        with localcontext() as context:
            context.prec = _EXACT_PRECISION
            return self.deployed + self.remainder == self.capital

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(row.symbol for row in self.rows)

    @property
    def drifting(self) -> tuple[str, ...]:
        """Names whose held units differ from their target units."""
        return tuple(row.symbol for row in self.rows if not row.is_on_target)

    @property
    def drift_value(self) -> Decimal:
        """``deployed - held_value``: how far the sleeve is from its target, in money."""
        with localcontext() as context:
            context.prec = _EXACT_PRECISION
            return self.deployed - self.held_value


def _require_money(value: object, label: str) -> Decimal:
    """A ``Decimal`` or a loud refusal. Floats are named specially — see the module docs."""
    if isinstance(value, float):
        raise TypeError(
            f"{label} arrived as a float ({value!r}). Money and prices are Decimal here "
            "(house rule 9): a float cannot hold 1234.55 exactly, so coercing one would bake "
            "that error into a rupee total and into the unit count taken from it. Convert at "
            "the boundary that produced it — Decimal(str(value)), or json.loads(..., "
            "parse_float=Decimal)."
        )
    if not isinstance(value, Decimal):
        raise TypeError(f"{label} must be a Decimal; got {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"{label} must be a finite number; got {value}")
    return value


def _require_units(value: object, label: str) -> int:
    """Whole units as an ``int``. An integral ``Decimal`` (a ``numeric`` column) is accepted."""
    if isinstance(value, bool):
        raise TypeError(f"{label} must be a whole number of units; got a bool ({value!r})")
    if isinstance(value, float):
        raise TypeError(
            f"{label} arrived as a float ({value!r}). Unit counts are whole numbers "
            "(house rule 9); pass an int, or the Decimal a numeric column gave you."
        )
    if isinstance(value, int):
        units = value
    elif isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            raise ValueError(
                f"{label} must be a whole number of units; got {value}. Equities trade in whole "
                "units, so a fractional holding is a data defect rather than a position."
            )
        units = int(value)
    else:
        raise TypeError(f"{label} must be an int or a Decimal; got {type(value).__name__}")
    if units < 0:
        raise ValueError(f"{label} cannot be negative; got {units}")
    return units


def _normalise_keys[T](mapping: Mapping[str, T], label: str) -> dict[str, T]:
    """Upper-case, stripped symbols, refusing a collision rather than letting one key win.

    ``basket_sizing._top`` normalises the same way. Two spellings of one symbol arriving in one
    mapping is a caller bug whose silent resolution would drop somebody's position.
    """
    normalised: dict[str, T] = {}
    for raw, value in mapping.items():
        symbol = raw.strip().upper()
        if not symbol:
            raise ValueError(f"{label} contains an empty symbol")
        if symbol in normalised:
            raise ValueError(
                f"{label} names {symbol} twice under different spellings. Normalise the symbols "
                "before calling; dropping one of them would drop a position."
            )
        normalised[symbol] = value
    return normalised


def units_affordable(*, budget: Decimal, price: Decimal) -> int:
    """Whole units a budget buys at a price, rounded **down**, computed exactly.

    The division is done on integer ratios rather than on ``Decimal``s because ``Decimal``
    division obeys the 28-significant-digit context: a quotient of ``9.999...`` carried past that
    many digits rounds *up* to ``10`` and would then floor to one unit too many — one unit the
    capital cannot pay for. Python's integers are unbounded, so the floor here is exact for any
    input, and ``units x price <= budget`` holds unconditionally.
    """
    budget = _require_money(budget, "budget")
    price = _require_money(price, "price")
    if budget < ZERO:
        raise ValueError(f"a budget cannot be negative; got {budget}")
    if price <= ZERO:
        raise ValueError(f"a unit price must be positive; got {price}")
    budget_num, budget_den = budget.as_integer_ratio()
    price_num, price_den = price.as_integer_ratio()
    return (budget_num * price_den) // (budget_den * price_num)


def _resolve_prices(
    symbols: Sequence[str], prices: Mapping[str, Decimal | None]
) -> dict[str, Decimal]:
    """Every symbol priced, or a refusal naming all of the ones that are not."""
    supplied = _normalise_keys(prices, "the price map")
    missing: list[str] = []
    invalid: list[str] = []
    resolved: dict[str, Decimal] = {}
    for symbol in symbols:
        raw = supplied.get(symbol)
        if raw is None:
            missing.append(symbol)
            continue
        price = _require_money(raw, f"the price of {symbol}")
        if price <= ZERO:
            invalid.append(symbol)
            continue
        resolved[symbol] = price
    if missing:
        raise MissingPriceError(
            "no price for " + ", ".join(missing) + ". A sleeve cannot be counted in units "
            "without one, and treating an unpriced name as zero or leaving it out would make "
            "the sleeve's total wrong without saying so.",
            missing,
        )
    if invalid:
        raise InvalidPriceError(
            "a price of zero or less for " + ", ".join(invalid) + ". No number of units is "
            "affordable at that price, so the allocation is refused rather than divided by it.",
            invalid,
        )
    return resolved


def allocate_units(
    *,
    capital: Decimal,
    weights: Mapping[str, Decimal],
    prices: Mapping[str, Decimal | None],
    held: Mapping[str, int | Decimal] | None = None,
) -> UnitAllocation:
    """Turn a sleeve's capital and target weights into whole unit counts, at the given prices.

    ``weights`` are shares of ``capital`` (``0.25``, not ``25``) and must sum to at most
    :data:`FULL_WEIGHT`. A total *below* one is accepted and means the caller deliberately left
    capital unallocated — a cash sleeve, or a regime cap already applied by
    ``basket_sizing.cash_pct_for_tier`` — and that capital lands in
    :attr:`UnitAllocation.remainder` alongside the whole-unit leftover. A total *above* one is
    refused: this module cannot tell a caller who meant to normalise from a caller who typed a
    weight wrong, and normalising silently would change a number they believe they chose.

    ``held`` is what the sleeve actually holds, so the result can show drift. Names held but no
    longer targeted keep a row, at weight zero — dropping them would understate what is held,
    which is the exact failure this module exists to end. Every row needs a price, held-only rows
    included, because every row reports a value.

    Rows come back in descending target weight, ties broken alphabetically, so two calls with the
    same inputs render identically.
    """
    capital = _require_money(capital, "capital")
    if capital < ZERO:
        raise ValueError(f"capital cannot be negative; got {capital}")

    targets = _normalise_keys(weights, "the weights")
    for symbol, raw_weight in targets.items():
        weight = _require_money(raw_weight, f"the weight of {symbol}")
        if weight < ZERO:
            raise ValueError(f"the weight of {symbol} cannot be negative; got {weight}")
        targets[symbol] = weight

    weight_total = sum(targets.values(), ZERO)
    if weight_total > FULL_WEIGHT + WEIGHT_TOLERANCE:
        raise ValueError(
            f"the target weights sum to {weight_total}, which is more than the whole sleeve "
            f"({FULL_WEIGHT}). Normalise them before calling, or lower one — scaling them here "
            "would change a number you chose without telling you."
        )

    holdings = _normalise_keys(held or {}, "the holdings")
    for symbol, raw_units in holdings.items():
        holdings[symbol] = _require_units(raw_units, f"the held units of {symbol}")

    symbols = sorted(
        set(targets) | set(holdings),
        key=lambda symbol: (-targets.get(symbol, ZERO), symbol),
    )
    resolved = _resolve_prices(symbols, prices)

    with localcontext() as context:
        context.prec = _EXACT_PRECISION
        rows: list[UnitLine] = []
        for symbol in symbols:
            weight = targets.get(symbol, ZERO)
            price = resolved[symbol]
            target_units = units_affordable(budget=capital * weight, price=price)
            held_units = _require_units(holdings.get(symbol, 0), f"the held units of {symbol}")
            rows.append(
                UnitLine(
                    symbol=symbol,
                    weight=weight,
                    price=price,
                    target_units=target_units,
                    held_units=held_units,
                    target_value=price * target_units,
                    held_value=price * held_units,
                )
            )
        deployed = sum((row.target_value for row in rows), ZERO)
        held_value = sum((row.held_value for row in rows), ZERO)
        remainder = capital - deployed

    return UnitAllocation(
        rows=tuple(rows),
        capital=capital,
        deployed=deployed,
        remainder=remainder,
        held_value=held_value,
        weight_total=weight_total,
    )
