"""Static reference rows seeded into an empty database (PROMPTS.md Prompt 1 deliverable 4).

Everything here is data, not behaviour, so it can be asserted directly by tests and re-applied
idempotently by ``baskfy_core.seed``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from baskfy_core.entitlements import (
    ENTITLEMENTS_KEY,
    INCLUDED_FEATURE_LABELS,
    Entitlements,
    Feature,
)
from baskfy_core.entitlements import FREE_TIER as FREE_TIER_ENTITLEMENTS
from baskfy_core.entitlements import PAID as PAID_ENTITLEMENTS
from baskfy_core.models.base import JsonObject
from baskfy_core.screen_definition import (
    CustomFilter,
    ExtraFactor,
    MovingAverageFilter,
    PeFilter,
    PositiveDaysFilter,
    ScreenDefinition,
    TopRiskFilter,
)
from baskfy_core.universes import UNIVERSES

#: docs/14 §"The name". One spelling, shared by the seed rows, the emails and the invoices.
PRODUCT_NAME: Final = "Baskfy"

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
    #: What the pricing page calls it. docs/01 §1 names the three plans and nothing else.
    label: str = ""
    #: One line under the name on `/pricing`. Served by `GET /plans`, never written in the web
    #: app — PROMPTS.md Prompt 13 acceptance criterion 4.
    tagline: str = ""
    #: Hidden from `GET /plans` unless a feature flag turns it on (Prompt 13 §5's optional ₹0
    #: tier). Seeded unconditionally so the row exists to be pointed at when the flag flips.
    flagged: bool = False


#: docs/01 §1: "Gated features: export, custom columns, historical ranks, community Slack, AMAs".
#: Kept as a plain list of names on every plan row for the pricing page's "what you get" copy; the
#: *enforceable* half of it is `entitlements`, which is what `baskfy_api.entitlements` reads.
GATED_FEATURES: Final[tuple[str, ...]] = (
    "csv_export",
    "custom_columns",
    "historical_ranks",
    "community_slack",
    "ama_access",
)

#: docs/11 §"Compliance & legal (India)": "**'Forever' plan** must state, at the point of sale,
#: that it means the lifetime of the service." PROMPTS.md Prompt 13 §5 quotes the reference
#: product's own words — "the lifetime of the website" — so both sentences are here: the
#: reference's clarification, then what it means for us. docs/DECISIONS.md.
FOREVER_DISCLOSURE: Final = (
    "Forever means the lifetime of the website. If Baskfy stops operating, the plan stops with "
    "it — there is no refund of the unused remainder and no transfer to another service."
)

#: docs/11 §Compliance requires a disclaimer at checkout and forbids advice language anywhere.
#: docs/01 §1: 'Explicit "not a SEBI registered advisor" disclaimer.' Served by `GET /plans` so
#: `/pricing` renders the same words the API holds rather than a copy in a component.
PRE_PURCHASE_DISCLAIMERS: Final[tuple[str, ...]] = (
    "Baskfy is not a SEBI-registered investment adviser. Nothing here is investment advice, a "
    "recommendation to buy or sell, or a target price.",
    "Baskfy is a screening and analytics tool. Every number on it is derived from historical "
    "exchange data and can be wrong, stale, or both.",
    "Past performance — including anything a backtest shows — does not indicate future returns.",
    "Prices are in Indian rupees and include GST at the prevailing rate. A GST invoice is issued "
    "for every payment and is available under Invoices.",
    "Subscriptions renew automatically until cancelled. Cancel any time; access continues to the "
    "end of the period already paid for.",
)


def _included(key: str | None, entitlements: Entitlements) -> bool:
    """Is this advertised line true of this plan?

    ``None`` marks the two docs/01 §1 features this service cannot enforce — community Slack and
    the AMAs. They come with a *paid* plan, so they follow whether the plan is paid at all rather
    than an entitlement key that does not exist.
    """
    if key is None:
        return Feature.EXPORT_CSV in entitlements.granted
    return Feature(key) in entitlements.granted


def _label(label: str, key: str | None, entitlements: Entitlements) -> str:
    """The screener line has to say *which* universes when a plan is restricted to some."""
    if key == Feature.SCREENER.value and entitlements.universes is not None:
        names = ", ".join(sorted(entitlements.universes))
        return f"The screener, limited to {names}"
    return label


def _plan_features(
    entitlements: Entitlements,
    *,
    price_from_dec_2026: str | None = None,
    disclosure: str | None = None,
) -> JsonObject:
    """One shape for every plan row, so `GET /plans` never has to special-case one."""
    features: JsonObject = {
        # The enforceable half. `baskfy_api.entitlements` reads exactly this block.
        ENTITLEMENTS_KEY: entitlements.to_plan_features(),
        # The advertised half, for the pricing page. Two of these are not API surfaces.
        "gated": list(GATED_FEATURES),
        "includes": [
            {"label": _label(label, key, entitlements), "entitlement": key}
            for label, key in INCLUDED_FEATURE_LABELS
            if _included(key, entitlements)
        ],
    }
    if price_from_dec_2026 is not None:
        features["price_from_dec_2026"] = price_from_dec_2026
    if disclosure is not None:
        features["disclosure"] = disclosure
    return features


#: docs/01 §1: "Monthly ₹500 · Yearly ₹3,999 · Forever ₹14,999 (rising to ₹899 / ₹5,999 /
#: ₹19,999 in Dec 2026)." The current prices are seeded; the Dec-2026 prices are recorded in
#: ``features.price_from_dec_2026`` rather than as separate rows, because docs/04's ``plan.code``
#: is unique and the increase is a repricing of the same three plans.
#:
#: The fourth row is PROMPTS.md Prompt 13 §5's optional ₹0 tier. It is seeded but hidden: only
#: ``BASKFY_FREE_TIER_ENABLED`` puts it in front of anyone.
PLANS: Final[tuple[PlanSeed, ...]] = (
    PlanSeed(
        id=1,
        code="monthly",
        price_inr=Decimal("500.00"),
        interval="month",
        label="Monthly",
        tagline="Everything, billed every month. Cancel any time.",
        features=_plan_features(PAID_ENTITLEMENTS, price_from_dec_2026="899.00"),
    ),
    PlanSeed(
        id=2,
        code="yearly",
        price_inr=Decimal("3999.00"),
        interval="year",
        label="Yearly",
        tagline="The same thing, billed once a year.",
        features=_plan_features(PAID_ENTITLEMENTS, price_from_dec_2026="5999.00"),
    ),
    PlanSeed(
        id=3,
        code="forever",
        price_inr=Decimal("14999.00"),
        # NULL interval = the one-time purchase (docs/02: "one-time for Forever").
        interval=None,
        label="Forever",
        tagline="Pay once. No renewal.",
        features=_plan_features(
            PAID_ENTITLEMENTS,
            price_from_dec_2026="19999.00",
            # docs/11 §Legal: must be stated at the point of sale.
            disclosure=FOREVER_DISCLOSURE,
        ),
    ),
)

#: PROMPTS.md Prompt 13 §5: "A ₹0 free tier with a limited universe is **optional** — implement it
#: behind a feature flag." So it is not one of docs/01 §1's three plans and is deliberately not a
#: member of :data:`PLANS`. The row is seeded regardless — a plan row nobody is shown costs
#: nothing, and creating it lazily when the flag flips would mean a migration at runtime — but
#: ``GET /plans`` hides it and ``POST /checkout/session`` refuses it while
#: ``BASKFY_FREE_TIER_ENABLED`` is false.
FREE_PLAN: Final = PlanSeed(
    id=4,
    code="free",
    price_inr=Decimal("0.00"),
    interval=None,
    label="Free",
    tagline="The screener on NIFTY 50, and nothing to export.",
    flagged=True,
    features=_plan_features(FREE_TIER_ENTITLEMENTS),
)

#: Everything ``seed_plans`` writes: docs/01 §1's three, plus the flagged ₹0 row.
ALL_PLANS: Final[tuple[PlanSeed, ...]] = (*PLANS, FREE_PLAN)

#: The three docs/01 §1 names, in the order the pricing page shows them.
PAID_PLAN_CODES: Final[tuple[str, ...]] = tuple(plan.code for plan in PLANS)


@dataclass(frozen=True, slots=True)
class ScreenSeed:
    public_id: str
    name: str
    definition: ScreenDefinition
    columns: list[str] = field(default_factory=list)


#: docs/01 §4 — the results table's default columns.
DEFAULT_COLUMNS: Final[list[str]] = [
    "ret_12m",
    "vol_12m",
    "close_raw",
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
        name="Top Baskfy Liquid Momentum",
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
