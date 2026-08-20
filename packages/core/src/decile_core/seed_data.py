"""Static reference rows seeded into an empty database (PROMPTS.md Prompt 1 deliverable 4).

Everything here is data, not behaviour, so it can be asserted directly by tests and re-applied
idempotently by ``decile_core.seed``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from decile_core.models.base import JsonObject
from decile_core.screen_definition import (
    CustomFilter,
    ExtraFactor,
    MovingAverageFilter,
    PeFilter,
    PositiveDaysFilter,
    ScreenDefinition,
    TopRiskFilter,
)
from decile_core.universes import UNIVERSES

#: docs/14 §"The name". One spelling, shared by the seed rows, the emails and the invoices.
PRODUCT_NAME: Final = "Decile"

#: docs/04 — exchange table. NSE is the only exchange in scope.
NSE_EXCHANGE_ID: Final = 1
NSE_EXCHANGE_CODE: Final = "NSE"


@dataclass(frozen=True, slots=True)
class PlanSeed:
    id: int
    code: str
    price_inr: Decimal
    interval: str | None
    features: JsonObject


#: docs/01 §1: "Monthly ₹500 · Yearly ₹3,999 · Forever ₹14,999 (rising to ₹899 / ₹5,999 /
#: ₹19,999 in Dec 2026)." The current prices are seeded; the Dec-2026 prices are recorded in
#: ``features.price_from_dec_2026`` rather than as separate rows, because docs/04's ``plan.code``
#: is unique and the increase is a repricing of the same three plans.
#: Gated features are docs/01 §1: "export, custom columns, historical ranks, community Slack, AMAs".
GATED_FEATURES: Final[tuple[str, ...]] = (
    "csv_export",
    "custom_columns",
    "historical_ranks",
    "community_slack",
    "ama_access",
)

PLANS: Final[tuple[PlanSeed, ...]] = (
    PlanSeed(
        id=1,
        code="monthly",
        price_inr=Decimal("500.00"),
        interval="month",
        features={"gated": list(GATED_FEATURES), "price_from_dec_2026": "899.00"},
    ),
    PlanSeed(
        id=2,
        code="yearly",
        price_inr=Decimal("3999.00"),
        interval="year",
        features={"gated": list(GATED_FEATURES), "price_from_dec_2026": "5999.00"},
    ),
    PlanSeed(
        id=3,
        code="forever",
        price_inr=Decimal("14999.00"),
        # NULL interval = the one-time purchase (docs/02: "one-time for Forever").
        interval=None,
        features={
            "gated": list(GATED_FEATURES),
            "price_from_dec_2026": "19999.00",
            # docs/11 §Legal: must be stated at the point of sale.
            "disclosure": "Forever means the lifetime of the service.",
        },
    ),
)


@dataclass(frozen=True, slots=True)
class ScreenSeed:
    public_id: str
    name: str
    definition: ScreenDefinition
    columns: list[str] = field(default_factory=list)


#: docs/01 §4 — the results table's default columns.
DEFAULT_COLUMNS: Final[list[str]] = [
    "close_raw",
    "series",
    "marketcap_cr",
    "ret_12m",
    "sharpe_12m",
    "vol_12m",
    "beta_12m",
    "ma_200",
]

# ---------------------------------------------------------------------------
# The six example screens.
#
# ⚠️ NOT SPECIFIED IN THE BUNDLE. PROMPTS.md Prompt 1 deliverable 4 says "the 6 example screens
# from docs/01 §1", but docs/01 §1 only records that the reference product ships six read-only
# templates — it names none of them and gives no definitions, and no other doc does either.
#
# The six below are therefore *authored*, not reverse-engineered. Each one is built only from
# filters and semantics that the docs do pin down, and each exists to exercise a different corner
# of ScreenDefinition so that later prompts have non-trivial fixtures:
#   1. the reference product's own observed screen (docs/13: universe + sort factor are known)
#   2. a moving-average stack            (docs/01 §2.3)
#   3. decile bucketing + liquidity      (docs/06 step 3, docs/01 §2.2)
#   4. multi-factor combined ranking     (docs/01 §2.12)
#   5. custom field-to-field filters     (docs/01 §2.14)
#   6. risk exclusions + P/E band        (docs/01 §2.8, §2.10)
# Replace them with the real templates if the reference product's list is ever captured.
# ---------------------------------------------------------------------------

EXAMPLE_SCREENS: Final[tuple[ScreenSeed, ...]] = (
    ScreenSeed(
        public_id="exmpl0000001",
        # docs/13: the captured export is this screen — NIFTY TOTAL MARKET sorted by
        # AVERAGE SHARPE RETURN 12 6 3 1 MONTHS, with a ₹1 crore median-volume floor.
        #
        # SERIES: the export is a *result set*, and two of its 271 rows are series BE. A screen
        # left on the ``["EQ"]`` default could not have produced them, so the captured screen had
        # both series switched on (docs/01 §2.9). Corrected here, because docs/13 is the arbiter
        # for this particular screen and because Prompt 6's acceptance criterion is that running
        # this definition reproduces all 271 rows — which it cannot do while dropping the two.
        name="Investing 001",
        definition=ScreenDefinition(
            index="nifty-total-market",
            sort_by="avg_sharpe_12_6_3_1",
            sort_direction="desc",
            median_volume_1y=10_000_000,
            series=["EQ", "BE"],
        ),
        columns=list(DEFAULT_COLUMNS),
    ),
    ScreenSeed(
        public_id="exmpl0000002",
        name="Trend Stack",
        definition=ScreenDefinition(
            index="nifty-500",
            sort_by="ret_12m",
            sort_direction="desc",
            moving_average=MovingAverageFilter(
                enabled=True, above_200=True, above_100=True, above_50=True
            ),
            median_volume_1y=20_000_000,
        ),
        columns=list(DEFAULT_COLUMNS),
    ),
    ScreenSeed(
        public_id="exmpl0000003",
        name="Top Decile Liquid Momentum",
        definition=ScreenDefinition(
            index="nifty-total-market",
            sort_by="sharpe_12m",
            sort_direction="desc",
            apply_filters_on="decile_1",
            median_volume_1y=50_000_000,
            min_return_1y=Decimal("0"),
        ),
        columns=list(DEFAULT_COLUMNS),
    ),
    ScreenSeed(
        public_id="exmpl0000004",
        name="Momentum, Low Volatility",
        definition=ScreenDefinition(
            index="nifty-200",
            sort_by="ret_12m_minus_1m",
            sort_direction="desc",
            factor_two=ExtraFactor(enabled=True, sort_by="vol_12m", sort_direction="asc"),
            median_volume_1y=50_000_000,
        ),
        columns=list(DEFAULT_COLUMNS),
    ),
    ScreenSeed(
        public_id="exmpl0000005",
        name="Golden Cross, Volume Expansion",
        definition=ScreenDefinition(
            index="nifty-500",
            sort_by="sharpe_6m",
            sort_direction="desc",
            custom_filters=[
                CustomFilter(left="ma_50", op=">=", right="ma_200"),
                CustomFilter(left="vol_avg_1w", op=">=", right="vol_avg_12m"),
            ],
        ),
        columns=list(DEFAULT_COLUMNS),
    ),
    ScreenSeed(
        public_id="exmpl0000006",
        name="Quality Momentum, Risk Trimmed",
        definition=ScreenDefinition(
            index="nifty-500",
            sort_by="avg_sharpe_12_9_6_3",
            sort_direction="desc",
            pe=PeFilter(enabled=True, from_=Decimal("0"), to=Decimal("60")),
            ignore_top_beta=TopRiskFilter(enabled=True, count=10),
            ignore_top_volatility=TopRiskFilter(enabled=True, count=10),
            ignore_above_beta=2,
            positive_days=PositiveDaysFilter(m12=50),
            median_volume_1y=20_000_000,
        ),
        columns=list(DEFAULT_COLUMNS),
    ),
)


def index_def_rows() -> list[JsonObject]:
    """``index_def`` rows for the 14 selectable universes (docs/01 §2.1)."""
    return [
        {
            "id": u.index_id,
            "slug": u.slug,
            "name": u.name,
            "is_universe": True,
            "sort_order": u.ui_order,
        }
        for u in UNIVERSES
    ]
