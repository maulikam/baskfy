"""Pure curated-plan shaping (invest / apply / exit) — SC3 leaf 1.2.2 companion."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from baskfy_core.curated_plans import (
    PLAN_TTL,
    build_apply_plan,
    build_customize_plan,
    build_exit_plan,
    build_invest_plan,
)
from baskfy_core.market_hours_cb import IST


def _d(value: str) -> Decimal:
    return Decimal(value)


NOW = dt.datetime(2026, 8, 24, 11, 0, tzinfo=IST)


class TestBuildInvestPlan:
    def test_buys_share_allocation_and_ttl(self) -> None:
        plan = build_invest_plan(
            target_weights={"AAA": _d("0.5"), "BBB": _d("0.5")},
            prices={"AAA": _d("100"), "BBB": _d("200")},
            amount=_d("400"),
            now=NOW,
        )
        assert plan["kind"] == "BUY"
        assert plan["requested_amount"] == _d("400")
        assert plan["expires_at_hint"] == NOW + PLAN_TTL
        by_sym = {leg["symbol"]: leg for leg in plan["legs"]}
        assert by_sym["AAA"]["side"] == "BUY"
        assert by_sym["AAA"]["quantity"] == 2
        assert by_sym["BBB"]["quantity"] == 1

    def test_refuses_non_positive_amount(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            build_invest_plan(
                target_weights={"AAA": _d("1")},
                prices={"AAA": _d("10")},
                amount=_d("0"),
                now=NOW,
            )


class TestBuildApplyPlan:
    def test_diff_produces_buy_and_sell(self) -> None:
        plan = build_apply_plan(
            holdings={"AAA": 10, "BBB": 0, "CCC": 5},
            target_weights={"AAA": _d("0.5"), "BBB": _d("0.5")},
            prices={"AAA": _d("100"), "BBB": _d("100"), "CCC": _d("100")},
            amount=_d("1000"),
            now=NOW,
        )
        assert plan["kind"] == "REBALANCE"
        by_sym = {leg["symbol"]: leg for leg in plan["legs"]}
        # target: 5 AAA + 5 BBB; hold 10 AAA + 5 CCC → sell 5 AAA, buy 5 BBB, sell 5 CCC
        assert by_sym["AAA"]["side"] == "SELL"
        assert by_sym["AAA"]["quantity"] == 5
        assert by_sym["BBB"]["side"] == "BUY"
        assert by_sym["BBB"]["quantity"] == 5
        assert by_sym["CCC"]["side"] == "SELL"
        assert by_sym["CCC"]["quantity"] == 5


class TestBuildCustomizePlan:
    def test_kind_is_customize_not_rebalance(self) -> None:
        plan = build_customize_plan(
            holdings={"AAA": 10, "BBB": 0},
            target_weights={"AAA": _d("0.5"), "BBB": _d("0.5")},
            prices={"AAA": _d("100"), "BBB": _d("100")},
            amount=_d("1000"),
            now=NOW,
        )
        assert plan["kind"] == "CUSTOMIZE"
        assert plan["expires_at_hint"] == NOW + PLAN_TTL
        assert plan["legs"]
        sides = {leg["side"] for leg in plan["legs"]}
        assert "BUY" in sides or "SELL" in sides


class TestBuildExitPlan:
    def test_sells_every_holding(self) -> None:
        plan = build_exit_plan(
            holdings={"AAA": 3, "BBB": 2},
            prices={"AAA": _d("50"), "BBB": _d("100")},
            now=NOW,
        )
        assert plan["kind"] == "EXIT"
        assert plan["requested_amount"] == _d("350")
        assert {leg["side"] for leg in plan["legs"]} == {"SELL"}
        assert sum(leg["quantity"] for leg in plan["legs"]) == 5
