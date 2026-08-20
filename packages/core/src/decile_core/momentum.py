"""Skip-month momentum — **INFERRED** (docs/05 §8).

Prompt 5: "Flag clearly in code comments the two INFERRED areas (12-1 momentum definition,
circuit detection) and expose them as configurable strategies so they can be recalibrated without
a rewrite." This is the other one.

docs/05 §8, in full, because the caveat is the important part:

    "Academic 12-1 momentum skips the most recent month to avoid short-term reversal.

        ret_12m_minus_1m = (P_{t-21}  / P_{t-252} - 1) x 100
        ret_12m_minus_2m = (P_{t-42}  / P_{t-252} - 1) x 100

     ⚠️ The reference site's own numbers for CUPID (608.37 and 852.21) do **not** reconcile with
     this definition given its other published figures. ... So the discrepancy is a *definitional*
     one, not an adjustment artefact, and must be resolved empirically in Prompt 19. Implement the
     definition above on adjusted data, and add a golden test on a corporate-action-free stock.
     An alternative reading — the 12-month return of the window *ending* one month ago,
     `P_{t-21}/P_{t-273}` — should be evaluated during calibration and one of the two locked in."

So there are two candidate definitions and the document does not know which the reference product
uses. Both are implemented; :data:`DEFAULT_SKIP_MONTH_DEFINITION` is the one docs/05 says to ship,
and switching is a constructor argument rather than an edit.

What "does not reconcile" means concretely: this is the one factor family that the decisive
reference-parity test is *expected* to miss until Prompt 19 settles it. That expectation is
recorded in the parity test rather than hidden by a widened tolerance.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import polars as pl

#: Trading-day offsets docs/05 §8 states directly.
SKIP_1M_BARS: Final = 21
SKIP_2M_BARS: Final = 42
LOOKBACK_BARS: Final = 252


class SkipMonthDefinition(StrEnum):
    """The two readings docs/05 §8 puts on the table."""

    #: ``P_{t-21} / P_{t-252} - 1`` — an 11-month return ending a month ago. docs/05's primary.
    SKIP_END = "skip_end"
    #: ``P_{t-21} / P_{t-273} - 1`` — a full 12-month return ending a month ago. The alternative
    #: docs/05 asks to be "evaluated during calibration and one of the two locked in".
    FULL_YEAR_ENDING_EARLIER = "full_year_ending_earlier"


DEFAULT_SKIP_MONTH_DEFINITION: Final = SkipMonthDefinition.SKIP_END


@dataclass(frozen=True, slots=True)
class SkipMonthConfig:
    definition: SkipMonthDefinition = DEFAULT_SKIP_MONTH_DEFINITION
    skip_1m_bars: int = SKIP_1M_BARS
    skip_2m_bars: int = SKIP_2M_BARS
    lookback_bars: int = LOOKBACK_BARS

    def __post_init__(self) -> None:
        if self.skip_1m_bars < 1 or self.skip_2m_bars < 1:
            raise ValueError("skip offsets must be at least one bar")
        if self.lookback_bars <= self.skip_2m_bars:
            raise ValueError("the lookback must be longer than the skip")

    def numerator_denominator(self, skip_bars: int) -> tuple[int, int]:
        """``(bars_back_for_numerator, bars_back_for_denominator)``."""
        if self.definition is SkipMonthDefinition.SKIP_END:
            return skip_bars, self.lookback_bars
        return skip_bars, skip_bars + self.lookback_bars


DEFAULT_SKIP_MONTH_CONFIG: Final = SkipMonthConfig()


def skip_month_return_expr(
    price: str, skip_bars: int, config: SkipMonthConfig = DEFAULT_SKIP_MONTH_CONFIG
) -> pl.Expr:
    """``(P_{t-skip} / P_{t-lookback} - 1) x 100`` under the configured definition.

    ``price`` is the *adjusted* close: docs/05 §8 is explicit that the reference series is already
    split- and bonus-adjusted, so the discrepancy it flags is definitional rather than an
    adjustment artefact.
    """
    numerator_back, denominator_back = config.numerator_denominator(skip_bars)
    numerator = pl.col(price).shift(numerator_back)
    denominator = pl.col(price).shift(denominator_back)
    return (
        pl.when(denominator.is_null() | (denominator == 0) | numerator.is_null())
        .then(None)
        .otherwise((numerator / denominator - 1) * 100)
    )
