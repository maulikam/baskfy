"""Recover corporate actions from the gap between an adjusted series and the exchange print.

M24's finding, in code. Two price series for the same instrument:

* ``close_raw`` — the NSE bhavcopy's exchange print, unadjusted by construction;
* a vendor's *adjusted* history — Kite's, which `docs/09` assumed was raw and is not.

Their ratio is the cumulative adjustment still owed at each date. It is flat between actions and
steps at every ex-date, so **every step is a corporate action**: the date is the ex-date and the
size of the step is the factor. That is the whole idea, and it recovered 85 actions across 65 of
the 271 reference symbols when `corporate_action` held four rows.

Pure — series in, actions out. No database, no network, no clock (docs/02's I/O-free rule).

WHAT SEPARATES AN ACTION FROM A GLITCH
--------------------------------------
A one-day error in either series produces **two** opposite steps a day apart. A corporate action
produces **one** step with a flat ratio on both sides. Requiring flatness across a few days either
way is what distinguishes them, and it is not optional: without it every missing bar becomes an
invented split.

WHAT SEPARATES A SPLIT FROM A DIVIDEND
--------------------------------------
A share-count action — a split or a bonus — multiplies the share count by a ratio of small whole
numbers, so its price factor is a ratio of small whole numbers: 2, 5, 10, 3/2, 4/3, 6/5. A cash
dividend's factor is ``P / (P - D)``, which is not.

The distinction decides what may be written, because M27 measured the reference corpus and it
computes momentum on a **price return**: the share-count actions are simply missing data and get
applied; the dividends are a different convention and do not.

WHAT THIS CANNOT TELL YOU
-------------------------
**A 5:1 split and a 4:1 bonus are the same price factor**, and no amount of price data separates
them. The caller is told the factor and the fraction, and records the ambiguity rather than
guessing — see ``RecoveredAction.ambiguous_with``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Final

#: A step smaller than this is rounding, not an action. The smallest real action in the reference
#: corpus is a 6:5 bonus (factor 1.2); 2% leaves a wide margin on both sides.
STEP_TOLERANCE: Final = 0.02

#: Trading days of flat ratio demanded on each side of a step before it is believed.
FLANK_DAYS: Final = 5

#: How flat "flat" is, as a fraction of the mean. Half a percent is far below the smallest real
#: action and far above the rounding in two independently rounded price series.
FLANK_SPREAD: Final = 0.005

#: Largest denominator a share-count ratio may have. A 21:20 bonus is real; a 51:50 is a dividend
#: wearing a fraction, and every corpus dividend lands above this bound.
MAX_DENOMINATOR: Final = 20

#: How close a factor must be to a small fraction to count as one.
RATIO_TOLERANCE: Final = 0.002

SHARE_COUNT: Final = "split/bonus"
CASH: Final = "cash/other"

#: Denominators a real split or bonus actually produces. A face-value split is n:1 or n:m for
#: small m; a bonus ``a:b`` gives ``(a+b):b`` and the common ones are 1:1, 1:2, 2:1, 1:5, 1:10,
#: 1:20, 3:2. Nothing issues a 19:17.
#:
#: A factor that resolves to a *ratio* but not to one of these shapes is almost always a
#: **demerger**: value leaves the company, the price steps down, and the step is whatever the
#: arithmetic of the spin-off makes it. M28's first write found 22 of them and labelled every one
#: a split — ITC's 22:19 is the ITC Hotels demerger, SIEMENS' 21:16 is Siemens Energy India,
#: VEDL's 21:11 and RAYMOND's 23:14 likewise.
#:
#: They still need the price adjustment, so they are still recovered. They are marked, because a
#: row calling a demerger a split is a row that lies about where a number came from.
CLEAN_DENOMINATORS: Final = frozenset({1, 2, 3, 4, 5, 10, 20})


@dataclass(frozen=True, slots=True)
class RecoveredAction:
    """One step in the ratio, and what it implies."""

    ex_date: dt.date
    #: `close_raw / adjusted` before the ex-date, divided by the same after it.
    factor: float
    kind: str
    #: The small fraction the factor resolves to, or None for a cash-shaped action.
    ratio: Fraction | None
    #: True when the ratio was flat for FLANK_DAYS on both sides.
    confirmed: bool
    #: True when there were not FLANK_DAYS of history on one side, so flatness could not be
    #: tested at all. **This is not the same as failing the test**, and conflating them was the
    #: first version's bug: NESTLEIND's 10:1 split sits four days into the observed window and is
    #: entirely real, while a one-day glitch has flanks available and fails on them. A caller
    #: deciding what to trust wants `confirmed or at_edge`, never `not confirmed`.
    at_edge: bool = False

    @property
    def rejected(self) -> bool:
        """Flanks were available and the ratio was not flat across them — a spike, not an action."""
        return not self.confirmed and not self.at_edge

    @property
    def share_count(self) -> bool:
        return self.kind == SHARE_COUNT

    @property
    def clean_shape(self) -> bool:
        """True when the ratio is one a split or a bonus actually produces.

        False marks an irregular ratio — nearly always a demerger. The distinction does not change
        whether the action is applied (it is: the price step is real either way), only what the
        stored row claims to be.
        """
        return self.ratio is not None and self.ratio.denominator in CLEAN_DENOMINATORS

    @property
    def shape(self) -> str:
        return "split_or_bonus" if self.clean_shape else "irregular_probably_demerger"

    @property
    def ambiguous_with(self) -> str | None:
        """A split ``n:1`` and a bonus ``(n-1):1`` are indistinguishable from price alone."""
        if self.ratio is None:
            return None
        bonus_new = self.ratio.numerator - self.ratio.denominator
        return f"bonus {bonus_new}:{self.ratio.denominator}"

    def as_split_ratio(self) -> tuple[Decimal, Decimal]:
        """``(ratio_from, ratio_to)`` for `corporate_action`, such that price factor = to/from.

        `baskfy_core.adjustments.split_factor` reads a split ``a:b`` as price factor ``b/a``, so a
        recovered factor of 5 is ``5:1`` and one of 4/3 is ``4:3``. Stored as the fraction rather
        than as a rounded decimal, because a split *is* a ratio of whole numbers and 1.3333 is not
        one.
        """
        if self.ratio is None:
            raise ValueError(f"{self.ex_date}: a cash-shaped action has no share-count ratio")
        return Decimal(self.ratio.numerator), Decimal(self.ratio.denominator)


def small_ratio(factor: float) -> Fraction | None:
    """The small fraction ``factor`` resolves to, or None if it does not resolve to one."""
    if factor <= 0:
        return None
    candidate = Fraction(factor).limit_denominator(MAX_DENOMINATOR)
    if candidate.numerator == candidate.denominator:
        return None
    return candidate if abs(float(candidate) - factor) / factor <= RATIO_TOLERANCE else None


def ratio_series(
    raw: Sequence[tuple[dt.date, float]], adjusted: Sequence[tuple[dt.date, float]]
) -> list[tuple[dt.date, float]]:
    """``raw / adjusted`` on the dates both series carry, in date order.

    Only shared dates: a date one source has and the other does not says nothing about an
    adjustment, and pairing across a gap would manufacture a step out of a missing bar.
    """
    by_date = dict(adjusted)
    out = [
        (day, close / by_date[day])
        for day, close in raw
        if day in by_date and by_date[day] and close
    ]
    out.sort(key=lambda pair: pair[0])
    return out


def _flat(values: Sequence[float]) -> bool:
    if not values:
        return False
    mean = sum(values) / len(values)
    return bool(mean) and (max(values) - min(values)) / mean <= FLANK_SPREAD


def recover_actions(
    raw: Sequence[tuple[dt.date, float]],
    adjusted: Sequence[tuple[dt.date, float]],
    *,
    minimum_days: int = 30,
) -> list[RecoveredAction]:
    """Every corporate action implied by the gap between the two series.

    ``minimum_days`` refuses a comparison too short to mean anything: a handful of shared dates
    can look like anything at all.
    """
    ratios = ratio_series(raw, adjusted)
    if len(ratios) < minimum_days:
        return []

    days = [day for day, _ in ratios]
    values = [value for _, value in ratios]
    found: list[RecoveredAction] = []

    for index in range(1, len(ratios)):
        before_value, after_value = values[index - 1], values[index]
        if after_value <= 0 or abs(before_value / after_value - 1.0) <= STEP_TOLERANCE:
            continue

        before = values[max(0, index - FLANK_DAYS) : index]
        after = values[index : index + FLANK_DAYS]
        at_edge = len(before) < FLANK_DAYS or len(after) < FLANK_DAYS
        confirmed = not at_edge and _flat(before) and _flat(after)
        # Averaging the flanks rather than taking the two bars either side of the step: both
        # series are rounded independently, and a mean over five days is a better estimate of the
        # level than one bar that happens to sit next to the discontinuity.
        factor = (
            (sum(before) / len(before)) / (sum(after) / len(after))
            if confirmed
            else before_value / after_value
        )
        ratio = small_ratio(factor)
        found.append(
            RecoveredAction(
                ex_date=days[index],
                factor=factor,
                kind=SHARE_COUNT if ratio is not None else CASH,
                ratio=ratio,
                confirmed=confirmed,
                at_edge=at_edge,
            )
        )
    return found
