"""SCAN → genesis projection — docs/smallcase/03 rule 5 (SC2)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from baskfy_core.curated_baskets import assert_weights_sum_to_one
from baskfy_core.scan_projection import (
    DEFAULT_SCAN_TOP_N,
    FIXTURE_SCAN_SYMBOLS,
    MOMENTUM_SCAN_BASKET_SLUG,
    MOMENTUM_SCAN_STRATEGY_KEY,
    equal_weights,
    project_scan_top_n,
)


class TestEqualWeights:
    def test_sum_to_one_for_common_sizes(self) -> None:
        for n in (2, 3, 10, 15):
            assert_weights_sum_to_one(equal_weights(n))


class TestProjectScanTopN:
    def test_deterministic_for_identical_fixture(self) -> None:
        a = project_scan_top_n(FIXTURE_SCAN_SYMBOLS, scan_run_id="abc123")
        b = project_scan_top_n(FIXTURE_SCAN_SYMBOLS, scan_run_id="abc123")
        assert a == b
        assert a.slug == MOMENTUM_SCAN_BASKET_SLUG
        assert a.strategy_key == MOMENTUM_SCAN_STRATEGY_KEY
        assert a.version_no == 1
        assert a.label == "GENESIS"
        assert len(a.constituents) == DEFAULT_SCAN_TOP_N
        assert_weights_sum_to_one(a.weights)
        assert [c.symbol for c in a.constituents] == list(FIXTURE_SCAN_SYMBOLS[:15])

    def test_dedupes_preserving_first_rank(self) -> None:
        ranked = ("AAA", "BBB", "AAA", "CCC", "DDD")
        proj = project_scan_top_n(ranked, top_n=3)
        assert [c.symbol for c in proj.constituents] == ["AAA", "BBB", "CCC"]

    def test_refuses_short_scan(self) -> None:
        with pytest.raises(ValueError, match="need at least"):
            project_scan_top_n(("AAA", "BBB"), top_n=5)

    def test_weights_are_four_dp(self) -> None:
        proj = project_scan_top_n(FIXTURE_SCAN_SYMBOLS, top_n=3)
        for c in proj.constituents:
            assert c.weight == c.weight.quantize(Decimal("0.0001"))
