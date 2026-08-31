"""`stop_from_vol()` still produces the desk's numbers — non-negotiable 4's vol scaling.

    "Every buy gets a GTT stop the same session, vol-scaled 8-12% via `stop_from_vol()`."
    -- CLAUDE.md, the desk's fourth non-negotiable

WHY THESE NUMBERS ARE LITERALS
The stop level is the only part of a GTT that is arithmetic, and it is the part a port can
silently change. Asserting `baskfy_core.score.stop_from_vol(...)` against a restatement of its
own formula would prove nothing at all -- the two would move together. So the expected values
below are **outputs of the desk's own function**, produced by executing the source bytes of

    /Users/maulikdave/Documents/portfolio/kite-momentum-rebalancer/app/scoring.py:115-117

(the external repo at `1cb5cb5`, which `docs/DESK-SOURCE-RECONCILIATION.md` §4.1 names
authoritative for order-path semantics) against the desk's own constants, read out of

    app/config.py:179   STOP_MIN, STOP_MAX, STOP_VOL_MULT = 0.08, 0.12, 2.2

Those three constants were confirmed **identical in both desk copies**, so the fork does not
reach this function. The desk's formula, quoted for the reader and implemented nowhere here:

    pct = float(np.clip(ann_vol / np.sqrt(52) * C.STOP_VOL_MULT, C.STOP_MIN, C.STOP_MAX))
    return round(price * (1 - pct), 1)

If the merged implementation ever drifts from these fourteen values, this file fails and the
live book's stops have moved. That is the whole point of it.
"""

from __future__ import annotations

import pytest
from baskfy_execution.gtt import StopBand, band_finding, refuse_stop

from baskfy_core.score import stop_from_vol


class DeskStopConfig:
    """The desk's `app.config`, reduced to what `stop_from_vol` reads off it.

    `baskfy_core.score.ScoringConfig` is a Protocol and the desk's config module satisfies it
    as-is; this stands in for the module so the test does not have to import the desk.
    """

    STOP_MIN = 0.08
    STOP_MAX = 0.12
    STOP_VOL_MULT = 2.2


#: (price, annualised volatility, the stop the desk's own function returns).
#: Chosen to pin both clamps, the interior, and the rounding: 0.2622 and 0.3934 are the
#: volatilities at which the weekly-scaled figure crosses STOP_MIN and STOP_MAX.
DESK_VALUES = [
    (100.0, 0.05, 92.0),
    (100.0, 0.20, 92.0),
    (100.0, 0.2622, 92.0),
    (100.0, 0.36, 89.0),
    (100.0, 0.3934, 88.0),
    (100.0, 0.50, 88.0),
    (100.0, 5.0, 88.0),
    (1000.0, 0.20, 920.0),
    (1000.0, 0.36, 890.2),
    (1234.5, 0.42, 1086.4),
    (57.35, 0.31, 51.9),
    (2750.0, 0.28, 2515.1),
    (19.8, 0.9, 17.4),
    (100.0, 0.0, 92.0),
]


@pytest.mark.parametrize(("price", "ann_vol", "expected"), DESK_VALUES)
def test_stop_from_vol_matches_the_desk_exactly(
    price: float, ann_vol: float, expected: float
) -> None:
    assert stop_from_vol(price, ann_vol, DeskStopConfig) == expected


def test_the_vol_scaling_is_clamped_to_eight_and_twelve_percent() -> None:
    """The band is not advisory: a quiet name cannot get a 2% stop that noise trips, and a wild
    one cannot get a 40% stop that is not protection."""
    quiet = stop_from_vol(100.0, 0.0001, DeskStopConfig)
    wild = stop_from_vol(100.0, 99.0, DeskStopConfig)
    assert quiet == 92.0, "the floor moved: a quiet name is now stopped tighter than 8%"
    assert wild == 88.0, "the ceiling moved: a wild name is now stopped further than 12%"
    assert DeskStopConfig.STOP_MIN < DeskStopConfig.STOP_MAX, "the stop band is inverted"


def test_the_vol_scaling_is_monotonic_in_volatility() -> None:
    """More volatility can only widen the stop, never tighten it. A non-monotonic scaling would
    stop the calmest names furthest away, which is the opposite of the rule."""
    vols = [0.05, 0.15, 0.26, 0.30, 0.36, 0.39, 0.45, 0.80]
    stops = [stop_from_vol(1000.0, v, DeskStopConfig) for v in vols]
    assert stops == sorted(stops, reverse=True)


#: What `band_finding` says about each stop above. Thirteen of the fourteen are inside the
#: band, and the fourteenth is the interesting one — see the test.
DESK_BAND_FINDINGS = [
    (100.0, 92.0, ""),
    (100.0, 89.0, ""),
    (100.0, 88.0, ""),
    (1000.0, 920.0, ""),
    (1000.0, 890.2, ""),
    (1234.5, 1086.4, ""),
    (57.35, 51.9, ""),
    (2750.0, 2515.1, ""),
    (19.8, 17.4, "too_far"),
]


@pytest.mark.parametrize(("price", "stop", "finding"), DESK_BAND_FINDINGS)
def test_the_gateway_reads_the_band_exactly_as_the_desks_review_page_does(
    price: float, stop: float, finding: str
) -> None:
    """`band_finding` is `protection.review()`'s TOO_FAR/TOO_CLOSE test, epsilon included, so
    the gateway and the desk's `/stops` page can never disagree about one trigger.

    The Rs 19.8 row is not a defect and it is not noise. `stop_from_vol` rounds to one decimal;
    on a low-priced scrip a half-paisa is 0.03% of the price, so a stop the unrounded formula
    put exactly on the 12% ceiling lands at 12.12% once rounded. The desk's own review page
    reports that holding as TOO_FAR, and so does this. It is also precisely why the gateway
    JOURNALS a band deviation instead of refusing one: refusing here would leave a real
    position with no stop at all because of a rounding step two modules upstream.
    """
    assert band_finding(trigger=stop, last_price=price, band=StopBand()) == finding


@pytest.mark.parametrize(("price", "ann_vol", "expected"), DESK_VALUES)
def test_every_desk_stop_is_a_stop_the_gateway_will_place(
    price: float, ann_vol: float, expected: float
) -> None:
    """The gateway refuses a trigger that is not a stop. Nothing `stop_from_vol` returns may
    ever be one of those -- a refusal here would leave a real position unprotected."""
    assert refuse_stop(symbol="TEST", qty=1, trigger=expected, last_price=price) == ""


def test_the_gateways_default_band_is_the_desks_configured_band() -> None:
    """`StopBand` is injectable, but its default must be what the live book has been traded
    on: `app/config.py:179`, identical in both desk copies."""
    band = StopBand()
    assert (band.min_pct, band.max_pct) == (DeskStopConfig.STOP_MIN, DeskStopConfig.STOP_MAX)
