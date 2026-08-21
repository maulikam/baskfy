"""Entitlements — docs/07 §Entitlements (Prompt 13 deliverables 1 and 3).

    { "screener": true, "export_csv": true, "custom_columns": true, "historical_ranks": true,
      "backtests": true, "api_access": false, "max_screens": 50 }

These assert the document, not the implementation: the payload's exact members, that a paid plan
grants what docs/01 §1 says is gated, and that a plan row round-trips through `plan.features`
without losing anything the enforcement path reads.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from baskfy_core.entitlements import (
    ANONYMOUS,
    FREE_TIER,
    MAX_SCREENS_KEY,
    PAID,
    PAID_MAX_SCREENS,
    REGISTERED,
    Entitlements,
    Feature,
    FeatureNotEntitled,
)
from baskfy_core.seed_data import ALL_PLANS, FREE_PLAN, PAID_PLAN_CODES, PLANS, PlanSeed

#: docs/07 §Entitlements, the payload's members verbatim and in its order.
DOCUMENTED_KEYS = (
    "screener",
    "export_csv",
    "custom_columns",
    "historical_ranks",
    "backtests",
    "api_access",
    "max_screens",
)


class TestTheWireShape:
    def test_the_payload_has_exactly_the_documented_keys(self) -> None:
        assert tuple(PAID.as_dict()) == DOCUMENTED_KEYS

    def test_max_screens_is_a_number_and_the_rest_are_booleans(self) -> None:
        payload = PAID.as_dict()
        assert isinstance(payload[MAX_SCREENS_KEY], int)
        assert all(isinstance(payload[key], bool) for key in DOCUMENTED_KEYS[:-1])

    def test_the_universe_restriction_is_not_a_member(self) -> None:
        """docs/07 fixes the payload at seven keys; an eighth would break the generated client."""
        assert "universes" not in FREE_TIER.as_dict()


class TestWhatEachTierGrants:
    @pytest.mark.parametrize(
        "feature",
        [
            Feature.SCREENER,
            Feature.EXPORT_CSV,
            Feature.CUSTOM_COLUMNS,
            Feature.HISTORICAL_RANKS,
            Feature.BACKTESTS,
        ],
    )
    def test_a_paid_plan_grants_every_gated_feature(self, feature: Feature) -> None:
        """docs/01 §1: "Gated features: export, custom columns, historical ranks ..."."""
        assert PAID.allows(feature)

    def test_no_plan_grants_api_access(self) -> None:
        """docs/07's own example shows `api_access: false`; docs/11 §Compliance requires a written
        data-redistribution opinion before a public API tier exists at all."""
        assert not PAID.allows(Feature.API_ACCESS)
        assert not any(
            Entitlements.from_plan_features(plan.features).allows(Feature.API_ACCESS)
            for plan in ALL_PLANS
        )

    def test_paid_max_screens_is_the_documented_fifty(self) -> None:
        assert PAID.max_screens == PAID_MAX_SCREENS == 50

    @pytest.mark.parametrize("tier", [ANONYMOUS, REGISTERED, FREE_TIER])
    def test_an_unpaid_tier_can_still_screen(self, tier: Entitlements) -> None:
        """docs/01 §1 gates export and the rest, not the screener itself."""
        assert tier.allows(Feature.SCREENER)

    @pytest.mark.parametrize("tier", [ANONYMOUS, REGISTERED, FREE_TIER])
    @pytest.mark.parametrize(
        "feature", [Feature.EXPORT_CSV, Feature.CUSTOM_COLUMNS, Feature.HISTORICAL_RANKS]
    )
    def test_an_unpaid_tier_gets_none_of_the_gated_features(
        self, tier: Entitlements, feature: Feature
    ) -> None:
        assert not tier.allows(feature)

    def test_anonymous_can_save_nothing(self) -> None:
        assert ANONYMOUS.max_screens == 0


class TestRequire:
    def test_it_raises_with_the_feature_name(self) -> None:
        with pytest.raises(FeatureNotEntitled) as caught:
            REGISTERED.require(Feature.EXPORT_CSV)
        assert caught.value.feature == "export_csv"

    def test_it_is_silent_when_granted(self) -> None:
        PAID.require(Feature.EXPORT_CSV)

    def test_an_unrestricted_plan_allows_every_universe(self) -> None:
        PAID.require_universe("nifty-microcap-250")

    def test_a_restricted_plan_refuses_the_others_and_says_which(self) -> None:
        """Prompt 13 §5: the ₹0 tier is "a ₹0 free tier with a limited universe"."""
        FREE_TIER.require_universe("nifty-50")
        with pytest.raises(FeatureNotEntitled) as caught:
            FREE_TIER.require_universe("nifty-500")
        assert caught.value.feature == "universes"
        assert "nifty-50" in caught.value.detail


class TestThePlanRows:
    @pytest.mark.parametrize("code", PAID_PLAN_CODES)
    def test_every_paid_plan_round_trips_to_the_paid_entitlements(self, code: str) -> None:
        plan = next(p for p in PLANS if p.code == code)
        assert Entitlements.from_plan_features(plan.features) == PAID

    def test_the_free_row_round_trips_to_the_free_tier(self) -> None:
        assert Entitlements.from_plan_features(FREE_PLAN.features) == FREE_TIER

    def test_the_free_row_is_zero_rupees(self) -> None:
        assert FREE_PLAN.price_inr == Decimal("0.00")

    def test_a_malformed_row_grants_nothing(self) -> None:
        """A plan row is data an operator can edit; a typo must fail closed."""
        empty = Entitlements.from_plan_features({})
        assert empty.granted == frozenset()
        assert empty.max_screens == 0

    def test_a_string_max_screens_is_not_believed(self) -> None:
        rebuilt = Entitlements.from_plan_features({"entitlements": {"max_screens": "50"}})
        assert rebuilt.max_screens == 0

    def test_true_is_not_a_screen_count(self) -> None:
        """`bool` is a subclass of `int`; `max_screens: true` must not become one screen."""
        rebuilt = Entitlements.from_plan_features({"entitlements": {"max_screens": True}})
        assert rebuilt.max_screens == 0

    @pytest.mark.parametrize("plan", ALL_PLANS, ids=lambda p: p.code)
    def test_every_advertised_feature_is_one_the_plan_actually_grants(self, plan: PlanSeed) -> None:
        """`/pricing` renders this list. A line the plan does not grant is a false claim."""
        features = plan.features
        entitlements = Entitlements.from_plan_features(features)
        includes = features["includes"]
        assert isinstance(includes, list)
        for entry in includes:
            assert isinstance(entry, dict)
            key = entry["entitlement"]
            if key is not None:
                assert entitlements.allows(Feature(str(key))), entry["label"]
