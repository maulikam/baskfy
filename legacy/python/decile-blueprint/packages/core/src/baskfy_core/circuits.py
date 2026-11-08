"""Circuit-hit detection — **INFERRED** (docs/05 §12).

Prompt 5: "Flag clearly in code comments the two INFERRED areas (12-1 momentum definition,
circuit detection) and expose them as configurable strategies so they can be recalibrated without
a rewrite." This is one of the two.

docs/05 §12 states the rule:

    circuit_hit_t =  (high_t == low_t == close_t) AND (|r_t| >= band_t - epsilon)
                  OR (close_raw_t >= upper_circuit_t - tick)
                  OR (close_raw_t <= lower_circuit_t + tick)

    "`upper_circuit` / `lower_circuit` come from the NSE bhavcopy where available. Where they are
     not, fall back to the band heuristic on |r_t| in {2%, 5%, 10%, 20%} with a 0.25% tolerance.
     Store the method used per row so results stay auditable."

Why it is inferred, and what would settle it
--------------------------------------------
NSE does not publish "this stock was circuit-locked today" as a field. The reference product's
``circuits_*`` counts are therefore a reconstruction, and docs/01 §2.6 only tells us the *filter*
semantics ("maximum permitted circuit-hit days in the window"), not the detection rule.

The band heuristic is the weaker half: a stock that happens to close exactly 20% up on a day with
no band in force is indistinguishable from one that locked. The bhavcopy path has no such
ambiguity, which is why it is preferred whenever the bands are present, and why the method used
is recorded per row rather than assumed.

Recalibration against the reference export is Prompt 19's job (docs/05 §12, docs/12).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import polars as pl

#: docs/05 §12 — the NSE price bands the heuristic recognises, as fractions.
DEFAULT_BANDS: Final[tuple[float, ...]] = (0.02, 0.05, 0.10, 0.20)

#: docs/05 §12 — "with a 0.25% tolerance".
DEFAULT_BAND_TOLERANCE: Final = 0.0025

#: How close to a published band counts as "at" it. A rupee tick on a three-figure price.
DEFAULT_TICK: Final = 0.05


class CircuitMethod(StrEnum):
    """Which rule produced a row's verdict. Recorded so a count stays auditable."""

    #: Published upper/lower bands were available for the row.
    BHAVCOPY_BANDS = "bhavcopy_bands"
    #: No bands; the |r_t| band heuristic was used.
    BAND_HEURISTIC = "band_heuristic"
    #: Neither bands nor a usable previous close.
    UNDETERMINED = "undetermined"


@dataclass(frozen=True, slots=True)
class CircuitConfig:
    """Everything the inference can be recalibrated on, without touching the engine."""

    bands: tuple[float, ...] = DEFAULT_BANDS
    band_tolerance: float = DEFAULT_BAND_TOLERANCE
    tick: float = DEFAULT_TICK
    #: When False, only published bands count and the heuristic is never applied — the strict
    #: reading, for a recalibration run that wants to measure the heuristic's contribution.
    allow_band_heuristic: bool = True

    def __post_init__(self) -> None:
        if not self.bands:
            raise ValueError("at least one price band is required")
        if any(band <= 0 for band in self.bands):
            raise ValueError(f"price bands must be positive; got {self.bands}")
        if self.band_tolerance < 0:
            raise ValueError(f"band_tolerance must not be negative; got {self.band_tolerance}")


#: The default strategy, as docs/05 §12 describes it.
DEFAULT_CIRCUIT_CONFIG: Final = CircuitConfig()


def circuit_hit_expr(config: CircuitConfig = DEFAULT_CIRCUIT_CONFIG) -> pl.Expr:
    """A boolean expression marking circuit-locked days.

    Expects ``close_raw``, ``high``, ``low``, ``prev_close_raw``, ``upper_circuit`` and
    ``lower_circuit`` on the frame. Bands are compared against ``close_raw`` because the bhavcopy
    publishes them against the exchange print, not the adjusted series.
    """
    at_upper = (pl.col("upper_circuit").is_not_null()) & (
        pl.col("close_raw") >= pl.col("upper_circuit") - config.tick
    )
    at_lower = (pl.col("lower_circuit").is_not_null()) & (
        pl.col("close_raw") <= pl.col("lower_circuit") + config.tick
    )
    published = at_upper | at_lower

    if not config.allow_band_heuristic:
        return published.fill_null(value=False)

    # docs/05 §12's first clause: a locked stock does not move intraday, so open/high/low/close
    # collapse to one price *and* the day's move sits on a band.
    flat_day = (pl.col("high") == pl.col("low")) & (pl.col("low") == pl.col("close_raw"))
    move = (pl.col("close_raw") / pl.col("prev_close_raw") - 1).abs()
    on_a_band = pl.any_horizontal(
        [move >= pl.lit(band - config.band_tolerance) for band in config.bands]
    )
    heuristic = flat_day & on_a_band & pl.col("prev_close_raw").is_not_null()

    return (published | heuristic).fill_null(value=False)


def circuit_method_expr(config: CircuitConfig = DEFAULT_CIRCUIT_CONFIG) -> pl.Expr:
    """Which rule was available for each row (docs/05 §12: "Store the method used per row")."""
    has_bands = pl.col("upper_circuit").is_not_null() | pl.col("lower_circuit").is_not_null()
    fallback = (
        pl.lit(CircuitMethod.BAND_HEURISTIC.value)
        if config.allow_band_heuristic
        else pl.lit(CircuitMethod.UNDETERMINED.value)
    )
    return (
        pl.when(has_bands)
        .then(pl.lit(CircuitMethod.BHAVCOPY_BANDS.value))
        .when(pl.col("prev_close_raw").is_not_null())
        .then(fallback)
        .otherwise(pl.lit(CircuitMethod.UNDETERMINED.value))
    )
