"""Curated-basket domain rules — docs/smallcase/03 (SC1).

Tests assert the spec: weights must sum to one, off-by weights fail, manager slug constants,
and version rows are append-only facts.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from baskfy_core.curated_baskets import (
    MANAGER_SEED_SLUGS,
    MANAGER_SLUG_BASKFY_ENGINE,
    MANAGER_SLUG_MAULIK,
    SOLE_USER_ENV,
    VersionImmutableError,
    assert_version_is_new,
    assert_weights_sum_to_one,
    refuse_version_mutation,
)


class TestWeightSum:
    def test_equal_weights_sum_to_one(self) -> None:
        assert_weights_sum_to_one([Decimal("0.2500")] * 4)

    def test_unequal_weights_that_reconcile_pass(self) -> None:
        assert_weights_sum_to_one([Decimal("0.3333"), Decimal("0.3333"), Decimal("0.3334")])

    def test_empty_weights_fail(self) -> None:
        with pytest.raises(ValueError, match="cannot be empty"):
            assert_weights_sum_to_one([])

    def test_off_by_one_basis_point_fails(self) -> None:
        with pytest.raises(ValueError, match="must sum to"):
            assert_weights_sum_to_one([Decimal("0.5000"), Decimal("0.4999")])

    def test_tolerance_allows_sub_ulp_drift_before_quantize(self) -> None:
        # Noisy inputs quantize to 0.5000 + 0.5000 = 1.0000.
        assert_weights_sum_to_one([Decimal("0.50002"), Decimal("0.49998")])


class TestManagerSlugConstants:
    def test_seed_slugs_are_documented(self) -> None:
        assert MANAGER_SLUG_BASKFY_ENGINE == "baskfy-engine"
        assert MANAGER_SLUG_MAULIK == "maulik"
        assert frozenset({"baskfy-engine", "maulik"}) == MANAGER_SEED_SLUGS

    def test_sole_user_env_names_the_setting(self) -> None:
        assert SOLE_USER_ENV == "BASKFY_SOLE_USER_ID"


class TestVersionImmutability:
    def test_refuse_version_mutation_raises(self) -> None:
        with pytest.raises(VersionImmutableError, match="append-only"):
            refuse_version_mutation()

    def test_assert_version_is_new_refuses_duplicate_number(self) -> None:
        with pytest.raises(VersionImmutableError, match="version_no 2"):
            assert_version_is_new(existing_version_nos=(1, 2), new_version_no=2)

    def test_assert_version_is_new_accepts_fresh_number(self) -> None:
        assert_version_is_new(existing_version_nos=(1, 2), new_version_no=3)
