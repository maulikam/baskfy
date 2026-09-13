"""ScreenDefinition asserts docs/04 §Screens and docs/01 §2.4-§2.14 — not current behaviour."""

from __future__ import annotations

import json
import typing
from decimal import Decimal

import pytest
from pydantic import ValidationError

from baskfy_core.screen_definition import (
    AWAY_FROM_HIGH_IGNORE,
    CIRCUITS_IGNORE_ABOVE,
    IGNORE_ABOVE_BETA_IGNORE,
    MAX_CUSTOM_FILTERS,
    POSITIVE_DAYS_IGNORE,
    ScreenDefinition,
)
from baskfy_core.screen_definition_corpus import CASES, Case
from baskfy_core.universes import UNIVERSE_SLUGS

#: docs/04 §Screens prints this object as the definition shape. Reproduced verbatim.
DOCS_04_EXAMPLE: dict[str, object] = {
    "index": "nifty-total-market",
    "sort_by": "avg_sharpe_12_6_3_1",
    "sort_direction": "desc",
    "apply_filters_on": "all",
    "ranking_mode": "composite",
    "ranking_scope": "filtered_results",
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
    """The inline Literal and baskfy_core.universes must never drift apart."""
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


# --- docs/ranking/PLAN.md C3: hash stability across the Phase-2 fields ----------------------

#: ``definition_hash()`` of every valid corpus case that existed at commit 999bf37, computed
#: **by the 999bf37 code** (a ``git worktree`` of that commit, its own ``screen_definition.py``,
#: over ``git show 999bf37:decile-blueprint/tests/fixtures/screen-definition-corpus.json``).
#: These are saved screens' identities: ``screen_run.definition_hash`` and the Redis cache key.
#: If one moves, every saved screen with that shape silently loses its run history and cache.
HASHES_MINIMAL = "c3c3cd787bb349c6d0442f450c1a8d7abbe08bcd8fd5e8ffec7c453fed04a5ae"
HASHES_AT_999BF37: dict[str, str] = {
    "minimal": HASHES_MINIMAL,
    "docs-04-full-example": "f0d3e16fb237216bf9be9bd74867bd2ee9b85875ee384a1d45e26ac9da277f84",
    "away-from-high-sentinel": "02c52a2b2f9fb79182c7cac2b673000e291b93bb0a13110b11a9080f204115f9",
    "positive-days-sentinel": "271d39c39eec398b3b5259ceee22102c658ba60ebedbf6a6b757efb97be54360",
    "circuits-ignore-threshold": "66f99c5285db03611f70d58889ca19b74382f22c8e152bedc72aae79d4795611",
    # Identical to "minimal": ignore_above_beta=100 is the default, so the meaning is the same.
    "ignore-above-beta-sentinel": HASHES_MINIMAL,
    "series-sme": "aac01ddbcefcab0cc0ddb73e8ea5921de7d45e553a10e2ba1d8103b7b7cd096e",
    "series-sme-and-main-board": "80e083cda3251fd222054b3b76fe6809b97b4d36adbec9a36963fcb22880d859",
    "per-factor-direction": "e49be0f66acb5b4c135985148c0a32144c2f2b445c4499ae4c78466fab2f4bce",
    "apply-filters-on-decile": "cc0293e3144d343346fa95f793e481a471ef71c230d7d363e14223175e06c730",
    "historical-date": "ea65c2871b2bed3f485e22b0f6b38d2058ed7a0c6697c2973d6258780db65e1e",
}

#: Three more shapes a saved screen could have had at 999bf37 that the corpus did not carry —
#: the Phase-1 desk_score and mode/scope fields, and non-null decimals — hashed the same way.
EXTRA_DEFINITIONS_AT_999BF37: tuple[tuple[dict[str, object], str], ...] = (
    (
        {"index": "nifty-500", "sort_by": "desk_score", "ranking_mode": "single"},
        "0bb75c214ad3fce158f7455e5f51de0df156c96e70fa2bcde66cb1bd3b4abd0e",
    ),
    (
        {
            "index": "nifty-200",
            "sort_by": "sharpe_12m",
            "ranking_mode": "sequential",
            "ranking_scope": "fixed_universe",
            "factor_two": {"enabled": True, "sort_by": "vol_12m", "sort_direction": "asc"},
            "factor_three": {"enabled": True, "sort_by": "ret_6m"},
        },
        "2a00de77e87f777390733a55d7e31377496ea26d67f62411b74a54d5eab5562c",
    ),
    (
        {
            "index": "nifty-total-market",
            "sort_by": "avg_sharpe_12_6_3_1",
            "marketcap": {"from": "500.5", "to": "90000"},
            "price": {"from": 20},
            "pe": {"enabled": True, "from": "0", "to": "60.25"},
            "min_return_1y": "12.5",
            "median_volume_1y": 50000000,
        },
        "440017bf30232521c308d6f05fbb80331458259aed433d41d47555326c01ee50",
    ),
)


@pytest.mark.parametrize("name", sorted(HASHES_AT_999BF37))
def test_hash_stability_for_every_pre_phase_2_corpus_definition(name: str) -> None:
    case = next(c for c in CASES if c.name == name)
    assert (
        ScreenDefinition.model_validate(case.value).definition_hash() == (HASHES_AT_999BF37[name])
    )


@pytest.mark.parametrize("index", range(len(EXTRA_DEFINITIONS_AT_999BF37)))
def test_hash_stability_for_other_pre_phase_2_shapes(index: int) -> None:
    payload, expected = EXTRA_DEFINITIONS_AT_999BF37[index]
    assert ScreenDefinition.model_validate(payload).definition_hash() == expected


def test_hash_stability_explicit_phase_2_defaults_hash_like_omitted() -> None:
    """Writing the defaults out must not fork a saved screen's identity."""
    terse = ScreenDefinition.model_validate({"index": "nifty-500", "sort_by": "ret_12m"})
    explicit = ScreenDefinition.model_validate(
        {
            "index": "nifty-500",
            "sort_by": "ret_12m",
            "ranking_terms": [],
            "family_weights": None,
            "missing_data": "penalize",
            "factor_ranges": [],
            "regime_in": None,
        }
    )
    assert explicit.definition_hash() == terse.definition_hash() == HASHES_AT_999BF37["minimal"]


def test_hash_stability_a_phase_2_field_that_is_set_changes_the_hash() -> None:
    base = ScreenDefinition.model_validate({"index": "nifty-500", "sort_by": "ret_12m"})
    variants: list[dict[str, object]] = [
        {"ranking_terms": [{"factor": "ret_12m", "preference": "higher"}]},
        {"factor_ranges": [{"factor": "ret_6m", "min": 0}]},
        {"regime_in": ["BULL"]},
        {
            "ranking_terms": [{"factor": "ret_12m", "preference": "higher"}],
            "missing_data": "exclude",
        },
        {
            "ranking_terms": [{"factor": "ret_12m", "preference": "higher"}],
            "family_weights": {"momentum": 1},
        },
    ]
    hashes = {
        ScreenDefinition.model_validate(
            {"index": "nifty-500", "sort_by": "ret_12m", **variant}
        ).definition_hash()
        for variant in variants
    }
    assert base.definition_hash() not in hashes
    assert len(hashes) == len(variants)


class TestPhase2Rules:
    """docs/ranking/PLAN.md C3 — the rules that need the Python registry (not in the TS corpus)."""

    def test_term_factor_must_be_a_registry_key(self) -> None:
        with pytest.raises(ValidationError, match="factor-registry"):
            ScreenDefinition.model_validate(
                {
                    "index": "nifty-500",
                    "sort_by": "ret_12m",
                    "ranking_terms": [
                        {"factor": "ret_12m", "preference": "higher"},
                        {"factor": "not_a_factor", "preference": "higher"},
                    ],
                }
            )

    def test_factor_range_refuses_a_computed_factor(self) -> None:
        with pytest.raises(ValidationError, match="computed"):
            ScreenDefinition.model_validate(
                {
                    "index": "nifty-500",
                    "sort_by": "ret_12m",
                    "factor_ranges": [{"factor": "desk_score", "min": 50}],
                }
            )

    def test_nan_bounds_are_refused(self) -> None:
        with pytest.raises(ValidationError):
            ScreenDefinition.model_validate(
                {
                    "index": "nifty-500",
                    "sort_by": "ret_12m",
                    "factor_ranges": [{"factor": "ret_6m", "min": float("nan")}],
                }
            )

    def test_within_sector_is_accepted_for_a_composite_of_terms(self) -> None:
        definition = ScreenDefinition.model_validate(
            {
                "index": "nifty-500",
                "sort_by": "ret_12m",
                "ranking_scope": "within_sector",
                "ranking_terms": [{"factor": "ret_12m", "preference": "higher"}],
            }
        )
        assert definition.ranking_scope == "within_sector"

    def test_schema_default_scope_stays_filtered_results(self) -> None:
        """Reference parity and saved screens: the *editor* defaults new screens elsewhere."""
        assert ScreenDefinition(index="nifty-500", sort_by="ret_12m").ranking_scope == (
            "filtered_results"
        )
