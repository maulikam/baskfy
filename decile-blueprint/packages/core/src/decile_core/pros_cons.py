"""PROS / CONS rules for the instrument page — docs/05 §16.

docs/01 §5 observed them: "**PROS** — auto-generated bullet list from rules, e.g. 'The close is
above 200-day moving average.' (x4 for 200/100/50/20), 'The close is within 25% of all time
high.', 'The beta is less than 1.25'. (A CONS list presumably renders the inverse.)"

docs/05 §16: "Pure boolean rules over `factor_daily`, rendered as sentences. ... CONS are the
negations, with their own wording. **Keep the rule table in one module so it stays consistent
between API and UI.**"

Hence one table here, with both wordings on each rule. A rule whose input is NULL yields neither a
PRO nor a CON — "we do not know" is not the same as "no", and asserting the negative for a stock
with four months of history would put a CON on it that the data does not support.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

#: docs/05 §16's own thresholds.
ATH_PROXIMITY_PCT: Final = Decimal(25)
BETA_CEILING: Final = Decimal("1.25")
POSITIVE_DAYS_FLOOR: Final = Decimal(55)
#: "Median daily turnover is above ₹1 crore." One crore in rupees.
MEDIAN_TURNOVER_FLOOR: Final = Decimal(10_000_000)

FactorRow = Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Rule:
    """One boolean rule and both of its renderings."""

    key: str
    #: The stored columns it reads. If any is NULL the rule is undecided.
    inputs: tuple[str, ...]
    predicate: Callable[[FactorRow], bool]
    pro: str
    con: str


def _num(row: FactorRow, column: str) -> Decimal | None:
    value = row.get(column)
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float, str)):
        try:
            return Decimal(str(value))
        except ArithmeticError:
            return None
    return None


def _above_ma(length: int) -> Callable[[FactorRow], bool]:
    def predicate(row: FactorRow) -> bool:
        close = _num(row, "close")
        ma = _num(row, f"ma_{length}")
        return close is not None and ma is not None and close > ma

    return predicate


def _within_ath(row: FactorRow) -> bool:
    away = _num(row, "away_high_ath")
    return away is not None and abs(away) <= ATH_PROXIMITY_PCT


def _low_beta(row: FactorRow) -> bool:
    beta = _num(row, "beta_12m")
    return beta is not None and beta < BETA_CEILING


def _positive_days(row: FactorRow) -> bool:
    value = _num(row, "pos_days_12m")
    return value is not None and value > POSITIVE_DAYS_FLOOR


def _liquid(row: FactorRow) -> bool:
    value = _num(row, "median_vol_12m")
    return value is not None and value >= MEDIAN_TURNOVER_FLOOR


#: docs/05 §16's table, in the order it lists them.
RULES: Final[tuple[Rule, ...]] = (
    *(
        Rule(
            key=f"close_above_ma_{length}",
            inputs=("close", f"ma_{length}"),
            predicate=_above_ma(length),
            pro=f"The close is above {length}-day moving average.",
            con=f"The close is below {length}-day moving average.",
        )
        for length in (200, 100, 50, 20)
    ),
    Rule(
        key="within_25pct_of_ath",
        inputs=("away_high_ath",),
        predicate=_within_ath,
        pro=f"The close is within {ATH_PROXIMITY_PCT:g}% of all time high.",
        con=f"The close is more than {ATH_PROXIMITY_PCT:g}% below its all time high.",
    ),
    Rule(
        key="beta_below_ceiling",
        inputs=("beta_12m",),
        predicate=_low_beta,
        pro=f"The beta is less than {BETA_CEILING}",
        con=f"The beta is {BETA_CEILING} or higher",
    ),
    Rule(
        key="positive_days_above_floor",
        inputs=("pos_days_12m",),
        predicate=_positive_days,
        pro=(f"More than {POSITIVE_DAYS_FLOOR:g}% of days in the last year closed positive."),
        con=(f"{POSITIVE_DAYS_FLOOR:g}% or fewer of days in the last year closed positive."),
    ),
    Rule(
        key="liquid_enough",
        inputs=("median_vol_12m",),
        predicate=_liquid,
        pro="Median daily turnover is above ₹1 crore.",
        con="Median daily turnover is below ₹1 crore.",
    ),
)


def _decided(row: FactorRow, rule: Rule) -> bool:
    return all(row.get(column) is not None for column in rule.inputs)


def evaluate(row: FactorRow) -> tuple[list[str], list[str]]:
    """``(pros, cons)`` for one ``factor_daily`` row.

    A rule with a NULL input appears in neither list: docs/06 §"Step 4" makes the same point about
    filters — "NULLs never satisfy a predicate" — and asserting the CON would be claiming
    knowledge we do not have about a young listing.
    """
    pros: list[str] = []
    cons: list[str] = []
    for rule in RULES:
        if not _decided(row, rule):
            continue
        if rule.predicate(row):
            pros.append(rule.pro)
        else:
            cons.append(rule.con)
    return pros, cons


def undecided(row: FactorRow) -> list[str]:
    """Rule keys with insufficient data — surfaced so the UI can explain a short list."""
    return [rule.key for rule in RULES if not _decided(row, rule)]
