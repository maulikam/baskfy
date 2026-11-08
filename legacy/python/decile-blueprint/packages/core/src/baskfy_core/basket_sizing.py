"""Sizing a basket cut from a screen: how many names, how much cash, how much per name.

A screen answers *"which stocks"*. It says nothing about how much of your money goes in, or
across how many of its names. Those are two separate questions with two separate owners, and
this module keeps them apart on purpose:

* **How many names** is a *concentration preference*. It is a fact about the investor — someone
  who wants twelve convictions and someone who wants twenty-five is not disagreeing about the
  market. That is what :class:`HoldingProfile` carries.
* **How much stays in cash** is a *fact about today*. The desk already decides it weekly as an
  equity cap (R1 100%, R2 70%, R3 40%), and :func:`cash_pct_for_tier` reads that cap rather than
  inventing a second opinion.

Folding the two together was the tempting shortcut and it would have been wrong: an investor who
wants a concentrated twelve-name basket would have had to also claim the market was defensive to
get it. ``docs/DECISIONS-MERGE.md`` already warns that R1-R4 mean *exposure* and nothing else;
this module is what that warning looks like in code.

**THE PROFILE SUGGESTS, THE INVESTOR DECIDES.** Every profile number here is a default, and
:func:`resolve_holdings` exists so an explicit count always wins. What it will not do is silently
clamp an impossible request — a screen that returned eleven names cannot fill a twenty-name
basket, and saying so is more use than quietly building something smaller than was asked for.

**HOW THE MONEY IS SPLIT** is a third question, and :class:`WeightMethod` is the answer. Equal
is the default so existing baskets do not move. Rank and inverse-vol are the same schemes the
backtest already names (docs/10). Score is the screen's own ranking factor — the "algo" path.
Custom lets the investor type the numbers; it cannot add or drop a name the screen did not
select.

Pure — candidates and an amount in, weights and rupee amounts out. No database, no quotes fetched,
no clock (house rule 1). Money is ``Decimal`` throughout and rounds at write time (house rules 8
and 9).

WHAT THIS DELIBERATELY CANNOT PRODUCE
-------------------------------------
A number of units to hold. Like ``baskfy_core.sleeves``, this module stops at rupee amounts and
target weights: a unit count is one step from an order list, and execution stays in the desk
console. The prices it accepts are used for exactly one thing — deciding whether an amount is
large enough to fill every name at all (:func:`minimum_amount`) — and that is a feasibility bound,
not an instruction.

WHY THE REMAINDER IS CASH
-------------------------
Equal weights over a whole-rupee budget almost never divide evenly. The shortfall joins the cash
rather than being smeared across the names, so **every basket reconciles to the rupee**:
``deployed + cash == amount``, asserted by test. Same rule, and the same reason, as ``sleeves``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Final

from baskfy_core.curated_baskets import WEIGHT_QUANTIZE, assert_weights_sum_to_one
from baskfy_core.sleeves import cap_for_tier

__all__ = [
    "DEFAULT_METHOD",
    "DEFAULT_PROFILE",
    "MAX_HOLDINGS",
    "MIN_CASH_BUFFER_PCT",
    "MIN_HOLDINGS",
    "SUGGESTED_HOLDINGS",
    "ZERO_CASH_PCT",
    "Candidate",
    "HoldingProfile",
    "SizedBasket",
    "SizedHolding",
    "WeightMethod",
    "cash_pct_for_tier",
    "minimum_amount",
    "resolve_holdings",
    "scheme_weights",
    "size_basket",
    "suggested_holdings",
]


class HoldingProfile(StrEnum):
    """How concentrated the investor wants to be. Not a view on the market — see the module docs.

    Deliberately *not* named R1/R2/R3: those are the desk's exposure tiers and mean something
    else. ``test_basket_sizing.py::TestTheTwoVocabulariesStaySeparate`` fails if they merge.
    """

    CONSERVATIVE = "CONSERVATIVE"
    BALANCED = "BALANCED"
    AGGRESSIVE = "AGGRESSIVE"


#: Suggested name counts. Fewer names is more concentrated, so AGGRESSIVE is the *smallest*
#: number — the direction people get backwards, which is why it is written down here.
#:
#: BALANCED is 20 because that is what the web layer has always materialized a screen at
#: (``DEFAULT_TOP_N``). Introducing profiles therefore changes no existing basket, which is what
#: made 25/20/12 preferable to a table that moved the default.
SUGGESTED_HOLDINGS: Final[Mapping[HoldingProfile, int]] = {
    HoldingProfile.CONSERVATIVE: 25,
    HoldingProfile.BALANCED: 20,
    HoldingProfile.AGGRESSIVE: 12,
}

#: What a caller who expressed no preference gets.
DEFAULT_PROFILE: Final = HoldingProfile.BALANCED


class WeightMethod(StrEnum):
    """How the deployed money is split across the chosen names.

    ``EQUAL`` / ``RANK`` / ``INV_VOL`` are the backtest's ``Weighting`` schemes under names this
    module can own (the backtest's values are lowercase and include ``marketcap``, which a
    create preview often cannot fill). ``SCORE`` is the screen's ranking factor — the algo path.
    ``CUSTOM`` is the investor's own numbers, applied to the screen's names, never instead of them.
    """

    EQUAL = "EQUAL"
    RANK = "RANK"
    SCORE = "SCORE"
    INV_VOL = "INV_VOL"
    CUSTOM = "CUSTOM"


#: Existing baskets were all equal-weight. Changing the default would rewrite every preview.
DEFAULT_METHOD: Final = WeightMethod.EQUAL

#: Two, matching ``baskfy_api.routers.curated_create``'s minimum: one name is not a basket.
MIN_HOLDINGS: Final = 2

#: An upper bound so a fat-fingered request cannot ask for a basket nobody could hold. Not a
#: statement about diversification; purely a guard on the input.
MAX_HOLDINGS: Final = 50

#: The *default* cash sleeve when the investor did not say otherwise. Whole-rupee weights always
#: leave a remainder, so a basket that *advertises* 0% cash as a policy would be a promise the
#: arithmetic cannot keep. Five percent is what the web layer has always used. An explicit
#: ``cash_pct=0`` is allowed: that is the investor opting out of a cash sleeve, and the leftover
#: is only the rounding remainder (SB7).
MIN_CASH_BUFFER_PCT: Final = Decimal(5)

#: An investor who opts out of a cash sleeve. Not a silent default.
ZERO_CASH_PCT: Final = Decimal(0)

#: Nothing may be entirely cash: that is not a basket, it is a decision not to build one.
MAX_CASH_PCT: Final = Decimal(95)

FULL_PCT: Final = Decimal(100)

#: Money is whole rupees here, for the reason ``sleeves`` gives: paise in an allocation table is
#: noise that makes it harder to read without making it truer.
RUPEE: Final = Decimal(1)

#: A minimum is rounded up to a round number, because "₹1,04,732" reads as a computation and
#: "₹1,04,800" reads as a threshold.
MINIMUM_STEP: Final = Decimal(100)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One ranked row of a screen result, as far as sizing is concerned.

    ``price`` is the exchange print (``close_raw``), used only by :func:`minimum_amount`. ``None``
    is honest and expected — a name whose price we do not hold simply cannot inform the floor.
    ``score`` is the screen's ranking factor (``SCORE``). ``vol`` is ``vol_12m`` (``INV_VOL``).
    """

    symbol: str
    rank: int
    price: Decimal | None = None
    score: Decimal | None = None
    vol: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SizedHolding:
    """One line of a sized basket."""

    rank: int
    symbol: str
    #: Share of the **deployed** money, at four decimal places. These sum to 1.0000 across the
    #: holdings, which is what ``cb_constituent.weight`` requires — cash is not a constituent.
    weight: Decimal
    #: Share of the investor's **whole amount**, cash included, as a percentage. What the UI
    #: shows, because it is the number that answers "how much of my money is in this stock".
    weight_pct_of_amount: Decimal
    amount: Decimal


@dataclass(frozen=True, slots=True)
class SizedBasket:
    """A screen's names, an amount, and the arithmetic that joins them."""

    holdings: tuple[SizedHolding, ...]
    #: What the investor asked to invest.
    amount: Decimal
    #: What the names add up to.
    deployed: Decimal
    #: ``amount - deployed``: the cash allocation plus the whole-rupee remainder.
    cash: Decimal
    cash_pct: Decimal
    #: The profile whose suggestion was used, or ``None`` when the count was chosen explicitly.
    profile: HoldingProfile | None
    #: True when the caller overrode the profile's suggestion.
    holdings_overridden: bool
    #: The smallest amount that fills every name, or ``None`` when no price was supplied.
    minimum: Decimal | None
    #: How the deployed money was split.
    method: WeightMethod = WeightMethod.EQUAL

    @property
    def balances(self) -> bool:
        return self.deployed + self.cash == self.amount

    @property
    def is_fundable(self) -> bool:
        """False when the amount cannot fill every name at its target weight."""
        return self.minimum is None or self.amount >= self.minimum

    @property
    def weights(self) -> tuple[Decimal, ...]:
        return tuple(holding.weight for holding in self.holdings)


def suggested_holdings(profile: HoldingProfile | None = None) -> int:
    """The name count a profile suggests. ``None`` means :data:`DEFAULT_PROFILE`."""
    return SUGGESTED_HOLDINGS[profile or DEFAULT_PROFILE]


def cash_pct_for_tier(
    tier: str | None,
    *,
    caps: dict[str, Decimal] | None = None,
) -> Decimal:
    """How much of the amount the current exposure tier says to leave in cash.

    Read straight off the desk's equity cap — R1 caps equity at 100%, so 0% is withheld; R2 at
    70%, so 30% is; R3 at 40%, so 60% is. ``caps`` is forwarded to
    :func:`baskfy_core.sleeves.cap_for_tier` for the same reason that function takes it: a table
    invented here would disagree with the one the desk trades on, and the caller with the live
    configuration should pass it.

    ``None``, or a tier nobody has defined, yields :data:`MIN_CASH_BUFFER_PCT` — the absence of a
    regime reading is not a reason to withhold money, but the rounding floor still applies.

    The result is clamped to :data:`MAX_CASH_PCT`, so the risk-off tiers produce a real basket
    with very little in it rather than an empty one. A tier that says "do not deploy" is a
    decision for the person, not an arithmetic edge case for this function to swallow.
    """
    cap = cap_for_tier(tier, caps) if tier else None
    if cap is None:
        return MIN_CASH_BUFFER_PCT
    withheld = FULL_PCT - cap
    return min(MAX_CASH_PCT, max(MIN_CASH_BUFFER_PCT, withheld))


def resolve_holdings(
    *,
    available: int,
    profile: HoldingProfile | None = None,
    requested: int | None = None,
) -> int:
    """How many names to hold: the investor's number when they gave one, else the profile's.

    ``available`` is how many rows the screen actually returned. A profile's *suggestion* is
    trimmed to it silently — suggesting 20 when a screen found 14 is this module's problem to
    absorb, not the investor's to read about. An *explicit* request that cannot be met raises
    instead: the person asked for something specific and deserves to be told it is not there.
    """
    if available < MIN_HOLDINGS:
        raise ValueError(
            f"a basket needs at least {MIN_HOLDINGS} names; this screen returned {available}"
        )

    if requested is None:
        return min(suggested_holdings(profile), available)

    if not MIN_HOLDINGS <= requested <= MAX_HOLDINGS:
        raise ValueError(
            f"a basket holds between {MIN_HOLDINGS} and {MAX_HOLDINGS} names; got {requested}"
        )
    if requested > available:
        raise ValueError(
            f"you asked for {requested} names and this screen returned {available}. "
            f"Widen the screen or lower the count."
        )
    return requested


def _top(candidates: Sequence[Candidate], holdings: int) -> tuple[Candidate, ...]:
    """The best `holdings` names by rank, first occurrence of a symbol winning."""
    seen: set[str] = set()
    chosen: list[Candidate] = []
    for candidate in sorted(candidates, key=lambda c: c.rank):
        symbol = candidate.symbol.strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        chosen.append(
            Candidate(
                symbol=symbol,
                rank=candidate.rank,
                price=candidate.price,
                score=candidate.score,
                vol=candidate.vol,
            )
        )
        if len(chosen) == holdings:
            break
    if len(chosen) < holdings:
        raise ValueError(
            f"only {len(chosen)} unique names available after de-duplication; need {holdings}"
        )
    return tuple(chosen)


def _equal_weights(count: int) -> tuple[Decimal, ...]:
    """`count` equal weights at four decimal places summing to 1.0000, residual on the last."""
    return _normalize([Decimal(1)] * count)


def _normalize(raw: Sequence[Decimal]) -> tuple[Decimal, ...]:
    """Positive raw scores → four-decimal weights summing to 1.0000, residual on the last."""
    if not raw:
        raise ValueError("a basket needs at least one weight")
    if any(value < 0 for value in raw):
        raise ValueError("weights cannot be negative")
    total = sum(raw)
    if total <= 0:
        raise ValueError("weights must include at least one positive value")
    weights = [(value / total).quantize(WEIGHT_QUANTIZE, rounding=ROUND_HALF_UP) for value in raw]
    weights[-1] = Decimal("1.0000") - sum(weights[:-1])
    assert_weights_sum_to_one(weights)
    return tuple(weights)


def _custom_raw(chosen: Sequence[Candidate], custom: Mapping[str, Decimal] | None) -> list[Decimal]:
    if custom is None:
        raise ValueError("custom weighting needs a weight for every chosen name")
    raw: list[Decimal] = []
    missing: list[str] = []
    for candidate in chosen:
        value = custom.get(candidate.symbol)
        if value is None or value <= 0:
            missing.append(candidate.symbol)
        else:
            raw.append(value)
    if missing:
        raise ValueError(
            "you did not give a positive weight to "
            + ", ".join(missing)
            + ". Every name the screen selected needs one."
        )
    extras = sorted(set(custom) - {candidate.symbol for candidate in chosen})
    if extras:
        raise ValueError(
            "weights for names this screen did not select: "
            + ", ".join(extras)
            + ". Custom weighting cannot add a holding."
        )
    return raw


def _score_raw(chosen: Sequence[Candidate]) -> list[Decimal]:
    raw = [
        candidate.score if candidate.score is not None and candidate.score > 0 else Decimal(0)
        for candidate in chosen
    ]
    if all(value == 0 for value in raw):
        raise ValueError(
            "score weighting needs a positive screen score on at least one name. "
            "Use equal weight, or pick a screen that ranks on a number."
        )
    return raw


def _inv_vol_raw(chosen: Sequence[Candidate]) -> list[Decimal]:
    raw: list[Decimal] = []
    for candidate in chosen:
        if candidate.vol is None or candidate.vol <= 0:
            raise ValueError(
                f"{candidate.symbol} has no 1-year volatility, so inverse-vol weighting "
                "cannot size it. Use equal weight or a screen that projects vol_12m."
            )
        raw.append(Decimal(1) / candidate.vol)
    return raw


def scheme_weights(
    chosen: Sequence[Candidate],
    method: WeightMethod,
    *,
    custom: Mapping[str, Decimal] | None = None,
) -> tuple[Decimal, ...]:
    """Target weights for the chosen names. Residual on the last so they sum to 1.0000.

    ``RANK`` is the backtest's formula: inside this set the best name gets n, the worst gets 1.
    Using the screen's absolute rank would make a slice from ranks 400-420 almost equal-weight.
    """
    if not chosen:
        raise ValueError("a basket needs at least one name")

    if method is WeightMethod.CUSTOM:
        return _normalize(_custom_raw(chosen, custom))
    if method is WeightMethod.EQUAL:
        return _equal_weights(len(chosen))
    if method is WeightMethod.RANK:
        count = len(chosen)
        return _normalize([Decimal(count - position) for position, _ in enumerate(chosen)])
    if method is WeightMethod.SCORE:
        return _normalize(_score_raw(chosen))
    if method is WeightMethod.INV_VOL:
        return _normalize(_inv_vol_raw(chosen))
    raise ValueError(f"unknown weight method: {method}")


def minimum_amount(
    candidates: Sequence[Candidate],
    *,
    holdings: int,
    cash_pct: Decimal,
    weights: Sequence[Decimal] | None = None,
) -> Decimal | None:
    """The smallest amount that puts at least one share's worth into every name.

    ``None`` when no candidate carries a price: a floor derived from nothing would be a number
    the reader would trust. Equal weight binds on the dearest name
    (``dearest x holdings / deployable``). Unequal weight binds on ``max(price_i / weight_i)``.
    """
    chosen = _top(candidates, holdings)
    if weights is not None and len(weights) != len(chosen):
        raise ValueError("weights and chosen names must be the same length")
    deployable = (FULL_PCT - cash_pct) / FULL_PCT
    if deployable <= 0:
        return None

    needed: Decimal | None = None
    for index, candidate in enumerate(chosen):
        if candidate.price is None or candidate.price <= 0:
            continue
        share = (Decimal(1) / Decimal(len(chosen))) if weights is None else weights[index]
        if share is None or share <= 0:
            continue
        bound = candidate.price / share / deployable
        needed = bound if needed is None else max(needed, bound)
    if needed is None:
        return None
    steps = (needed / MINIMUM_STEP).to_integral_value(rounding=ROUND_HALF_UP)
    if steps * MINIMUM_STEP < needed:
        steps += 1
    return steps * MINIMUM_STEP


def size_basket(  # noqa: PLR0913 - the extra kwargs are the investor's choices, not a grab-bag
    candidates: Sequence[Candidate],
    *,
    amount: Decimal,
    profile: HoldingProfile | None = None,
    holdings: int | None = None,
    cash_pct: Decimal | None = None,
    method: WeightMethod | None = None,
    custom_weights: Mapping[str, Decimal] | None = None,
) -> SizedBasket:
    """Turn a ranked screen result and an amount into weights and whole-rupee amounts.

    ``holdings`` overrides the profile's suggestion (see :func:`resolve_holdings`). ``cash_pct``
    defaults to :data:`MIN_CASH_BUFFER_PCT`; pass :func:`cash_pct_for_tier`'s answer to let the
    current exposure tier decide it. ``method`` defaults to :data:`DEFAULT_METHOD` (equal);
    ``custom_weights`` is required when the method is ``CUSTOM`` and is keyed by symbol.
    """
    if amount <= 0:
        raise ValueError(f"an investment amount must be positive; got {amount}")

    cash_fraction = MIN_CASH_BUFFER_PCT if cash_pct is None else cash_pct
    if not ZERO_CASH_PCT <= cash_fraction <= MAX_CASH_PCT:
        raise ValueError(
            f"cash must be between {ZERO_CASH_PCT}% and {MAX_CASH_PCT}%; got {cash_fraction}%"
        )

    count = resolve_holdings(
        available=len({c.symbol.strip().upper() for c in candidates if c.symbol.strip()}),
        profile=profile,
        requested=holdings,
    )
    chosen = _top(candidates, count)
    resolved_method = method or DEFAULT_METHOD
    normalised_custom = (
        {symbol.strip().upper(): value for symbol, value in custom_weights.items()}
        if custom_weights is not None
        else None
    )
    weights = scheme_weights(chosen, resolved_method, custom=normalised_custom)

    whole_amount = amount.quantize(RUPEE, rounding=ROUND_DOWN)
    budget = (whole_amount * (FULL_PCT - cash_fraction) / FULL_PCT).quantize(
        RUPEE, rounding=ROUND_DOWN
    )

    # EQUAL WEIGHT DIVIDES THE BUDGET. THE OTHER METHODS MULTIPLY IT.
    #
    # `_normalize` puts the four-decimal residual on the last name, because
    # `cb_constituent.weight` has to sum to exactly 1.0000. Multiplying a budget by those weights
    # under *equal* weight would hand the last name a visibly larger position. Dividing the
    # budget instead gives every name the identical amount — what equal weight means to the
    # person reading the table, and what `sleeves._one` does for the same reason.
    #
    # Rank / score / inv-vol / custom *intend* different amounts, so those paths multiply.
    # ROUND_DOWN either way, so the basket can never propose more than its budget. The
    # shortfall becomes cash.
    if resolved_method is WeightMethod.EQUAL:
        per_name = (budget / count).quantize(RUPEE, rounding=ROUND_DOWN)
        amounts = [per_name] * count
    else:
        amounts = [(budget * weight).quantize(RUPEE, rounding=ROUND_DOWN) for weight in weights]

    rows = [
        SizedHolding(
            rank=candidate.rank,
            symbol=candidate.symbol,
            weight=weight,
            weight_pct_of_amount=(amount_i * FULL_PCT / whole_amount).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if whole_amount
            else Decimal(0),
            amount=amount_i,
        )
        for candidate, weight, amount_i in zip(chosen, weights, amounts, strict=True)
    ]

    deployed = sum(amounts, Decimal(0))
    return SizedBasket(
        holdings=tuple(rows),
        amount=whole_amount,
        deployed=deployed,
        cash=whole_amount - deployed,
        cash_pct=cash_fraction,
        profile=None if holdings is not None else (profile or DEFAULT_PROFILE),
        holdings_overridden=holdings is not None,
        minimum=minimum_amount(chosen, holdings=count, cash_pct=cash_fraction, weights=weights),
        method=resolved_method,
    )
