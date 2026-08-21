"""ScreenDefinition asserts docs/04 §Screens and docs/01 §2.4-§2.14 — not current behaviour."""

from __future__ import annotations

import json
import typing
from decimal import Decimal

import pytest
from pydantic import ValidationError

from decile_core.screen_definition import (
    AWAY_FROM_HIGH_IGNORE,
    CIRCUITS_IGNORE_ABOVE,
    IGNORE_ABOVE_BETA_IGNORE,
    MAX_CUSTOM_FILTERS,
    POSITIVE_DAYS_IGNORE,
    ScreenDefinition,
)
from decile_core.screen_definition_corpus import CASES, Case
from decile_core.universes import UNIVERSE_SLUGS

#: docs/04 §Screens prints this object as the definition shape. Reproduced verbatim.
DOCS_04_EXAMPLE: dict[str, object] = {
    "index": "nifty-total-market",
    "sort_by": "avg_sharpe_12_6_3_1",
    "sort_direction": "desc",
    "apply_filters_on": "all",
    "min_return_1y": None,
    "median_volume_1y": 10000000,
    "moving_average": {
        "enabled": False,
        "above_200": False,
        "above_100": False,
        "above_50": False,
        "above_20": False,
        "below_200": False,
        "below_100": False,
        "below_50": False,
        "below_20": False,
    },
    "away_from_high": {"ath": 100, "one_year": 100},
    "positive_days": {"m12": 0, "m9": 0, "m6": 0, "m3": 0, "m1": 0},
    "circuits": {"m12": 999, "m9": 999, "m6": 999, "m3": 999, "m1": 999},
    "marketcap": {"from": None, "to": None},
    "pe": {"enabled": False, "from": None, "to": None},
    "series": ["EQ"],
    "ignore_top_beta": {"enabled": False, "count": 0},
    "ignore_top_volatility": {"enabled": False, "count": 0},
    "ignore_above_beta": 100,
    "price": {"from": None, "to": None},
    "factor_two": {"enabled": False, "sort_by": None, "sort_direction": "desc"},
    "factor_three": {"enabled": False, "sort_by": None, "sort_direction": "desc"},
    "historical_date": None,
    "custom_filters": [{"enabled": True, "left": "ma_50", "op": ">=", "right": "ma_200"}],
}


def test_accepts_the_docs_04_example_verbatim() -> None:
    assert ScreenDefinition.model_validate(DOCS_04_EXAMPLE).index == "nifty-total-market"


def test_round_trip_reproduces_every_documented_key() -> None:
    """docs/04's shape is the contract: no key may be dropped, renamed or invented."""
    dumped = json.loads(ScreenDefinition.model_validate(DOCS_04_EXAMPLE).canonical_json())
    assert set(dumped) == set(DOCS_04_EXAMPLE)


def test_universe_slug_literal_matches_registry() -> None:
    """The inline Literal and decile_core.universes must never drift apart."""
    literal = typing.get_args(ScreenDefinition.model_fields["index"].annotation)
    assert literal == UNIVERSE_SLUGS


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_corpus(case: Case) -> None:
    """Every case in the shared corpus — the same list packages/api-client runs against Zod."""
    if case.valid:
        ScreenDefinition.model_validate(case.value)
    else:
        with pytest.raises(ValidationError):
            ScreenDefinition.model_validate(case.value)


class TestSentinels:
    """docs/01 §2.4-§2.10: 'off' is encoded in the value, so nothing may read it as a filter."""

    def test_away_from_high_sentinel_is_inactive(self) -> None:
        definition = ScreenDefinition(index="nifty-500", sort_by="ret_12m")
        assert definition.away_from_high.ath == AWAY_FROM_HIGH_IGNORE
        assert not definition.away_from_high.ath_is_active()
        assert not definition.away_from_high.one_year_is_active()

    def test_away_from_high_below_sentinel_is_active(self) -> None:
        definition = ScreenDefinition.model_validate(
            {"index": "nifty-500", "sort_by": "ret_12m", "away_from_high": {"ath": 25}}
        )
        assert definition.away_from_high.ath_is_active()

    def test_positive_days_zero_is_inactive(self) -> None:
        definition = ScreenDefinition(index="nifty-500", sort_by="ret_12m")
        assert definition.positive_days.m12 == POSITIVE_DAYS_IGNORE
        assert definition.positive_days.active_windows() == ()

    def test_positive_days_reports_only_the_set_windows(self) -> None:
        definition = ScreenDefinition.model_validate(
            {"index": "nifty-500", "sort_by": "ret_12m", "positive_days": {"m12": 55, "m3": 60}}
        )
        assert definition.positive_days.active_windows() == ("m12", "m3")

    def test_circuits_above_the_threshold_is_inactive(self) -> None:
        """docs/01 §2.6: '> 250 = ignore' — a threshold, not one magic number."""
        definition = ScreenDefinition.model_validate(
            {
                "index": "nifty-500",
                "sort_by": "ret_12m",
                "circuits": {"m12": CIRCUITS_IGNORE_ABOVE + 1, "m1": 3},
            }
        )
        assert definition.circuits.active_windows() == ("m1",)

    def test_circuits_at_the_threshold_still_binds(self) -> None:
        """250 itself is a cap, not the 'off' value; only strictly above 250 disables."""
        definition = ScreenDefinition.model_validate(
            {"index": "nifty-500", "sort_by": "ret_12m", "circuits": {"m6": CIRCUITS_IGNORE_ABOVE}}
        )
        assert "m6" in definition.circuits.active_windows()

    def test_ignore_above_beta_sentinel(self) -> None:
        definition = ScreenDefinition(index="nifty-500", sort_by="ret_12m")
        assert definition.ignore_above_beta == IGNORE_ABOVE_BETA_IGNORE
        assert not definition.ignore_above_beta_is_active()

    def test_ignore_above_beta_below_sentinel_is_active(self) -> None:
        definition = ScreenDefinition.model_validate(
            {"index": "nifty-500", "sort_by": "ret_12m", "ignore_above_beta": 2}
        )
        assert definition.ignore_above_beta_is_active()


class TestRankingFactors:
    """docs/06 §5: ranks are summed across one to three independently-directed factors."""

    def test_single_factor(self) -> None:
        definition = ScreenDefinition(index="nifty-500", sort_by="ret_12m")
        assert definition.ranking_factors() == (("ret_12m", "desc"),)

    def test_direction_is_per_factor(self) -> None:
        definition = ScreenDefinition.model_validate(
            {
                "index": "nifty-500",
                "sort_by": "sharpe_12m",
                "sort_direction": "desc",
                "factor_two": {"enabled": True, "sort_by": "vol_12m", "sort_direction": "asc"},
            }
        )
        assert definition.ranking_factors() == (("sharpe_12m", "desc"), ("vol_12m", "asc"))

    def test_disabled_extra_factor_is_not_ranked(self) -> None:
        definition = ScreenDefinition.model_validate(
            {
                "index": "nifty-500",
                "sort_by": "sharpe_12m",
                "factor_two": {"enabled": False, "sort_by": "vol_12m"},
            }
        )
        assert definition.ranking_factors() == (("sharpe_12m", "desc"),)


class TestCanonicalJson:
    """docs/06 §"Determinism guarantee": the hash must identify the *meaning*, not the bytes."""

    def test_omitted_defaults_hash_like_explicit_defaults(self) -> None:
        terse = ScreenDefinition.model_validate({"index": "nifty-500", "sort_by": "ret_12m"})
        verbose = ScreenDefinition.model_validate(
            {
                "index": "nifty-500",
                "sort_by": "ret_12m",
                "sort_direction": "desc",
                "away_from_high": {"ath": 100, "one_year": 100},
                "series": ["EQ"],
            }
        )
        assert terse.definition_hash() == verbose.definition_hash()

    def test_key_order_does_not_change_the_hash(self) -> None:
        a = ScreenDefinition.model_validate({"index": "nifty-500", "sort_by": "ret_12m"})
        b = ScreenDefinition.model_validate({"sort_by": "ret_12m", "index": "nifty-500"})
        assert a.definition_hash() == b.definition_hash()

    def test_a_meaningful_change_changes_the_hash(self) -> None:
        a = ScreenDefinition(index="nifty-500", sort_by="ret_12m")
        b = ScreenDefinition(index="nifty-500", sort_by="ret_12m", ignore_above_beta=2)
        assert a.definition_hash() != b.definition_hash()

    def test_custom_filter_order_is_significant(self) -> None:
        """Slot order is user-visible, so it must survive into the hash."""
        first = ScreenDefinition.model_validate(
            {
                "index": "nifty-500",
                "sort_by": "ret_12m",
                "custom_filters": [
                    {"left": "ma_50", "op": ">=", "right": "ma_200"},
                    {"left": "ma_20", "op": ">=", "right": "ma_50"},
                ],
            }
        )
        second = ScreenDefinition.model_validate(
            {
                "index": "nifty-500",
                "sort_by": "ret_12m",
                "custom_filters": [
                    {"left": "ma_20", "op": ">=", "right": "ma_50"},
                    {"left": "ma_50", "op": ">=", "right": "ma_200"},
                ],
            }
        )
        assert first.definition_hash() != second.definition_hash()

    def test_canonical_json_is_compact_and_sorted(self) -> None:
        rendered = ScreenDefinition(index="nifty-500", sort_by="ret_12m").canonical_json()
        assert ", " not in rendered and '": ' not in rendered
        parsed = json.loads(rendered)
        assert list(parsed) == sorted(parsed)

    def test_decimals_survive_without_float_rounding(self) -> None:
        """Money never becomes a float, in the hash input or anywhere else."""
        definition = ScreenDefinition.model_validate(
            {"index": "nifty-500", "sort_by": "ret_12m", "marketcap": {"from": "0.1", "to": "0.3"}}
        )
        assert definition.marketcap.from_ == Decimal("0.1")
        assert "0.1" in definition.canonical_json()


def test_custom_filter_slot_limit_matches_the_ui() -> None:
    """docs/01 §2.14 describes three slots; the model must not silently allow a fourth."""
    assert MAX_CUSTOM_FILTERS == 3
