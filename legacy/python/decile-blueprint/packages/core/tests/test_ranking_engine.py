"""Ranking engine — asserts docs/ranking/PLAN.md contract C4 with hand-computed expectations.

Every expected score is worked on paper in the comment beside it. Percentile, per C4 step 3:
``(average-tie rank - 1) / (n - 1)`` ascending over the non-missing values in the scope set.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from baskfy_core.ranking_engine import (
    COMPOSITE_SCORE,
    RANKING_ENGINE_VERSION,
    UNCLASSIFIED_SECTOR,
    ExplainContext,
    MissingPolicy,
    PreferenceName,
    RankingSpec,
    TermSpec,
    effective_weights,
    explain,
    explain_instrument,
    family_shares,
    percentile,
    rank_frame,
    rank_history,
    target_distance,
    target_score,
    transform,
)

AS_OF = dt.date(2026, 9, 11)


def _term(  # noqa: PLR0913 - mirrors TermSpec's own fields
    key: str,
    family: str = "momentum",
    preference: PreferenceName = "higher",
    *,
    weight: float = 1.0,
    target_min: float | None = None,
    target_max: float | None = None,
) -> TermSpec:
    return TermSpec(
        key=key,
        label=key.upper(),
        weight_family=family,
        preference=preference,
        weight=weight,
        target_min=target_min,
        target_max=target_max,
    )


def _frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    if "passes_filters" not in frame.columns:
        frame["passes_filters"] = True
    return frame


def _order(frame: pd.DataFrame) -> list[object]:
    return frame["symbol"].tolist()


# ---------------------------------------------------------------------------
# Step 3 — transforms
# ---------------------------------------------------------------------------


class TestPreferenceTransforms:
    def test_higher_with_ties(self) -> None:
        # ranks [1, 2.5, 2.5, 4], n = 4 -> [(1-1)/3, 1.5/3, 1.5/3, 3/3]
        values = pd.Series([10.0, 20.0, 20.0, 40.0])
        assert percentile(values).tolist() == [0.0, 0.5, 0.5, 1.0]

    def test_lower_is_higher_on_the_negated_value(self) -> None:
        values = pd.Series([10.0, 20.0, 20.0, 40.0])
        assert transform(values, _term("x", preference="lower")).tolist() == [1.0, 0.5, 0.5, 0.0]

    def test_n_equal_one_scores_one(self) -> None:
        assert percentile(pd.Series([7.0])).tolist() == [1.0]
        assert transform(pd.Series([7.0]), _term("x", preference="lower")).tolist() == [1.0]

    def test_missing_values_do_not_count_toward_n(self) -> None:
        # n = 2 over [10, 30]
        result = percentile(pd.Series([10.0, np.nan, 30.0]))
        assert result.iloc[0] == 0.0 and math.isnan(result.iloc[1]) and result.iloc[2] == 1.0

    def test_all_equal_values_share_the_midpoint(self) -> None:
        # ranks all 2 (average of 1..3) -> (2-1)/2
        assert percentile(pd.Series([5.0, 5.0, 5.0])).tolist() == [0.5, 0.5, 0.5]

    def test_target_range_inside_and_outside(self) -> None:
        # [0, 3]: distances [0, 0, 2, 2, 0]. Inside -> 1.0. Distances' ranks: zeros avg 2,
        # twos avg 4.5 -> percentile of 2 = 3.5/4 = 0.875 -> score 0.125.
        term = _term("x", preference="target_range", target_min=0.0, target_max=3.0)
        values = pd.Series([1.0, 2.0, 5.0, -2.0, 3.0])
        assert target_distance(values, 0.0, 3.0).tolist() == [0.0, 0.0, 2.0, 2.0, 0.0]
        assert transform(values, term).tolist() == [1.0, 1.0, 0.125, 0.125, 1.0]

    def test_target_range_all_outside_is_relative(self) -> None:
        # [0, 3]: distances [1, 3, 7] -> percentiles [0, 0.5, 1] -> scores [1, 0.5, 0]
        term = _term("x", preference="target_range", target_min=0.0, target_max=3.0)
        assert transform(pd.Series([4.0, 6.0, 10.0]), term).tolist() == [1.0, 0.5, 0.0]

    def test_target_range_all_inside_scores_one(self) -> None:
        assert target_score(pd.Series([0.0, 0.0, 0.0])).tolist() == [1.0, 1.0, 1.0]

    def test_target_range_one_bound(self) -> None:
        # max 3 only: distances [0, 2, 5]; outside rows' percentiles among [0, 2, 5] are 0.5, 1
        term = _term("x", preference="target_range", target_max=3.0)
        assert transform(pd.Series([1.0, 5.0, 8.0]), term).tolist() == [1.0, 0.5, 0.0]
        low_only = _term("x", preference="target_range", target_min=2.0)
        assert target_distance(pd.Series([1.0, 5.0]), 2.0, None).tolist() == [1.0, 0.0]
        assert transform(pd.Series([1.0, 5.0]), low_only).tolist() == [0.0, 1.0]


# ---------------------------------------------------------------------------
# Missing data
# ---------------------------------------------------------------------------


def _missing_frame() -> pd.DataFrame:
    return _frame(
        [
            {"instrument_id": 1, "symbol": "A", "x": 10.0, "y": 1.0},
            {"instrument_id": 2, "symbol": "B", "x": None, "y": 2.0},
            {"instrument_id": 3, "symbol": "C", "x": 30.0, "y": 3.0},
        ]
    )


class TestMissingData:
    def test_penalize_scores_zero(self) -> None:
        spec = RankingSpec(terms=(_term("x"),), mode="composite", scope="filtered_results")
        result = rank_frame(_missing_frame(), spec)
        scores = result.scored.set_index("symbol")["term_score__x"]
        # x over [10, 30]: A 0, C 1; B missing -> 0.0
        assert scores.to_dict() == {"A": 0.0, "B": 0.0, "C": 1.0}
        assert _order(result.ranked) == ["C", "A", "B"]

    def test_neutral_scores_half(self) -> None:
        spec = RankingSpec(
            terms=(_term("x"),), mode="composite", scope="filtered_results", missing_data="neutral"
        )
        result = rank_frame(_missing_frame(), spec)
        assert result.scored.set_index("symbol")["term_score__x"]["B"] == 0.5
        assert _order(result.ranked) == ["C", "B", "A"]

    def test_exclude_drops_the_row_and_its_value_from_filtered_results(self) -> None:
        spec = RankingSpec(
            terms=(_term("x"), _term("y")),
            mode="composite",
            scope="filtered_results",
            missing_data="exclude",
        )
        result = rank_frame(_missing_frame(), spec)
        assert _order(result.ranked) == ["C", "A"]
        # y over the results {A: 1, C: 3} only: A 0, C 1 (B's y=2 is not in the scope set)
        y = result.scored.set_index("symbol")["term_score__y"]
        assert y["A"] == 0.0 and y["C"] == 1.0 and math.isnan(y["B"])

    def test_exclude_under_fixed_universe_keeps_the_row_in_the_percentile(self) -> None:
        spec = RankingSpec(
            terms=(_term("x"), _term("y")),
            mode="composite",
            scope="fixed_universe",
            missing_data="exclude",
        )
        result = rank_frame(_missing_frame(), spec)
        assert _order(result.ranked) == ["C", "A"]
        # y over the universe {1, 2, 3}: A 0, B 0.5, C 1
        assert result.scored.set_index("symbol")["term_score__y"].to_dict() == {
            "A": 0.0,
            "B": 0.5,
            "C": 1.0,
        }

    def test_sequential_sorts_missing_last(self) -> None:
        for policy in ("penalize", "neutral"):
            spec = RankingSpec(
                terms=(_term("x", preference="lower"),),
                mode="sequential",
                scope="filtered_results",
                missing_data=policy,
            )
            assert _order(rank_frame(_missing_frame(), spec).ranked) == ["A", "C", "B"]

    def test_a_term_with_no_values_at_all(self) -> None:
        frame = _frame(
            [
                {"instrument_id": 1, "symbol": "A", "x": None, "y": 1.0},
                {"instrument_id": 2, "symbol": "B", "x": None, "y": 2.0},
            ]
        )
        spec = RankingSpec(terms=(_term("x"), _term("y")), mode="composite", scope="fixed_universe")
        result = rank_frame(frame, spec)
        # x -> penalize 0.0 for both; y: A 0, B 1; composite = 100 x 0.5 x y
        assert result.ranked[COMPOSITE_SCORE].tolist() == [50.0, 0.0]
        assert _order(result.ranked) == ["B", "A"]


# ---------------------------------------------------------------------------
# Step 4 — family weighting
# ---------------------------------------------------------------------------


class TestFamilyWeighting:
    def _terms(self) -> tuple[TermSpec, ...]:
        return (
            _term("ret_12m", "momentum"),
            _term("ret_6m", "momentum"),
            _term("ret_3m", "momentum"),
            _term("pos_days_6m", "path_quality"),
        )

    def test_three_correlated_momentum_terms_split_momentums_share(self) -> None:
        spec = RankingSpec(terms=self._terms(), mode="composite", scope="fixed_universe")
        weights = effective_weights(spec)
        # two families present -> 1/2 each; momentum's half split over three equal terms
        assert weights == pytest.approx(
            {"ret_12m": 1 / 6, "ret_6m": 1 / 6, "ret_3m": 1 / 6, "pos_days_6m": 1 / 2}
        )
        assert sum(weights.values()) == pytest.approx(1.0)

    def test_momentum_share_on_a_frame_is_half_not_three_quarters(self) -> None:
        frame = _frame(
            [
                # P leads every momentum term, trails path quality; Q the reverse.
                {
                    "instrument_id": 1,
                    "symbol": "P",
                    "ret_12m": 9.0,
                    "ret_6m": 9.0,
                    "ret_3m": 9.0,
                    "pos_days_6m": 40.0,
                },
                {
                    "instrument_id": 2,
                    "symbol": "Q",
                    "ret_12m": 1.0,
                    "ret_6m": 1.0,
                    "ret_3m": 1.0,
                    "pos_days_6m": 60.0,
                },
            ]
        )
        spec = RankingSpec(terms=self._terms(), mode="composite", scope="fixed_universe")
        ranked = rank_frame(frame, spec).ranked.set_index("symbol")
        # Each scores 1.0 on one family only -> 50.00 each; a naive per-term mean would give P 75.
        assert ranked[COMPOSITE_SCORE].to_dict() == {"P": 50.0, "Q": 50.0}

    def test_term_weights_inside_a_family(self) -> None:
        terms = (_term("a", "momentum", weight=3.0), _term("b", "momentum", weight=1.0))
        spec = RankingSpec(terms=terms, mode="composite", scope="fixed_universe")
        assert effective_weights(spec) == pytest.approx({"a": 0.75, "b": 0.25})

    def test_explicit_family_weights_are_scale_free(self) -> None:
        terms = (_term("a", "momentum"), _term("b", "path_quality"))
        pct = RankingSpec(
            terms=terms,
            mode="composite",
            scope="fixed_universe",
            family_weights={"momentum": 75.0, "path_quality": 25.0},
        )
        frac = RankingSpec(
            terms=terms,
            mode="composite",
            scope="fixed_universe",
            family_weights={"momentum": 0.75, "path_quality": 0.25},
        )
        assert family_shares(pct) == pytest.approx({"momentum": 0.75, "path_quality": 0.25})
        assert family_shares(pct) == pytest.approx(family_shares(frac))

    def test_unset_family_gets_the_mean_of_the_explicit_ones(self) -> None:
        terms = (_term("a", "momentum"), _term("b", "path_quality"), _term("c", "participation"))
        spec = RankingSpec(
            terms=terms,
            mode="composite",
            scope="fixed_universe",
            family_weights={"momentum": 60.0, "path_quality": 40.0, "participation": None},
        )
        # participation -> mean(60, 40) = 50; total 150
        assert family_shares(spec) == pytest.approx(
            {"momentum": 0.4, "path_quality": 40 / 150, "participation": 50 / 150}
        )

    def test_weights_for_absent_families_are_ignored(self) -> None:
        spec = RankingSpec(
            terms=(_term("a", "momentum"),),
            mode="composite",
            scope="fixed_universe",
            family_weights={"momentum": 1.0, "risk_execution": 99.0},
        )
        assert family_shares(spec) == {"momentum": 1.0}

    def test_all_zero_shares_are_refused(self) -> None:
        spec = RankingSpec(
            terms=(_term("a", "momentum"),),
            mode="composite",
            scope="fixed_universe",
            family_weights={"momentum": 0.0},
        )
        with pytest.raises(ValueError, match="zero share"):
            effective_weights(spec)


# ---------------------------------------------------------------------------
# Step 2 — scopes
# ---------------------------------------------------------------------------


def _scope_frame() -> pd.DataFrame:
    return _frame(
        [
            {"instrument_id": 1, "symbol": "A", "x": 1.0, "y": 30.0, "sector": "nifty-it"},
            {"instrument_id": 2, "symbol": "B", "x": 2.0, "y": 10.0, "sector": "nifty-it"},
            {"instrument_id": 3, "symbol": "C", "x": 3.0, "y": 20.0, "sector": None},
            {"instrument_id": 4, "symbol": "D", "x": 4.0, "y": 0.0, "sector": None},
        ]
    ).assign(passes_filters=[True, True, False, True])


class TestScopes:
    TERMS = (_term("x"), _term("y"))

    def test_filtered_results_ranks_among_the_survivors(self) -> None:
        spec = RankingSpec(terms=self.TERMS, mode="composite", scope="filtered_results")
        result = rank_frame(_scope_frame(), spec)
        scored = result.scored.set_index("symbol")
        # x over {1, 2, 4}: A 0, B 0.5, D 1.  y over {30, 10, 0}: A 1, B 0.5, D 0.
        assert scored["term_score__x"].drop("C").to_dict() == {"A": 0.0, "B": 0.5, "D": 1.0}
        assert math.isnan(scored["term_score__x"]["C"])
        # All three composite to 50.00; ties broken by x raw desc: D, B, A.
        assert result.ranked[COMPOSITE_SCORE].tolist() == [50.0, 50.0, 50.0]
        assert _order(result.ranked) == ["D", "B", "A"]

    def test_fixed_universe_ranks_across_the_universe_then_filters(self) -> None:
        spec = RankingSpec(terms=self.TERMS, mode="composite", scope="fixed_universe")
        result = rank_frame(_scope_frame(), spec)
        scored = result.scored.set_index("symbol")
        # x over {1,2,3,4}: A 0, B 1/3, C 2/3, D 1.  y over {30,10,20,0}: A 1, B 1/3, C 2/3, D 0.
        assert scored["term_score__x"].to_dict() == pytest.approx(
            {"A": 0.0, "B": 1 / 3, "C": 2 / 3, "D": 1.0}
        )
        # A 50, B 33.33, D 50 -> A/D tie broken by x desc -> D, A, B. C fails the filters.
        assert result.ranked.set_index("symbol")[COMPOSITE_SCORE].to_dict() == {
            "D": 50.0,
            "A": 50.0,
            "B": 33.33,
        }
        assert _order(result.ranked) == ["D", "A", "B"]

    def test_fixed_and_filtered_differ_on_the_same_frame(self) -> None:
        filtered = rank_frame(
            _scope_frame(),
            RankingSpec(terms=self.TERMS, mode="composite", scope="filtered_results"),
        )
        fixed = rank_frame(
            _scope_frame(), RankingSpec(terms=self.TERMS, mode="composite", scope="fixed_universe")
        )
        b_filtered = filtered.scored.set_index("symbol")["term_score__x"]["B"]
        b_fixed = fixed.scored.set_index("symbol")["term_score__x"]["B"]
        assert b_filtered == 0.5 and b_fixed == pytest.approx(1 / 3)
        assert _order(filtered.ranked) != _order(fixed.ranked)

    def test_within_sector_groups_and_the_unclassified_bucket(self) -> None:
        spec = RankingSpec(terms=(_term("x"),), mode="composite", scope="within_sector")
        result = rank_frame(_scope_frame(), spec)
        scored = result.scored.set_index("symbol")
        assert scored["sector"].to_dict() == {
            "A": "nifty-it",
            "B": "nifty-it",
            "C": UNCLASSIFIED_SECTOR,
            "D": UNCLASSIFIED_SECTOR,
        }
        # nifty-it {A 1, B 2}: A 0, B 1. unclassified {C 3, D 4}: C 0, D 1 (C is in scope
        # although it fails the filters: within_sector ranks the whole universe).
        assert scored["term_score__x"].to_dict() == {"A": 0.0, "B": 1.0, "C": 0.0, "D": 1.0}
        # B and D both 100.00; x desc breaks the tie -> D, B, A
        assert _order(result.ranked) == ["D", "B", "A"]

    def test_a_single_row_sector_scores_one(self) -> None:
        frame = _frame(
            [
                {"instrument_id": 1, "symbol": "A", "x": -5.0, "sector": "nifty-media"},
                {"instrument_id": 2, "symbol": "B", "x": 1.0, "sector": "nifty-it"},
                {"instrument_id": 3, "symbol": "C", "x": 2.0, "sector": "nifty-it"},
            ]
        )
        spec = RankingSpec(terms=(_term("x"),), mode="composite", scope="within_sector")
        scored = rank_frame(frame, spec).scored.set_index("symbol")
        assert scored["term_score__x"]["A"] == 1.0

    def test_no_row_passes_the_filters(self) -> None:
        frame = _scope_frame().assign(passes_filters=False)
        for scope in ("filtered_results", "fixed_universe", "within_sector"):
            spec = RankingSpec(terms=self.TERMS, mode="composite", scope=scope)
            result = rank_frame(frame, spec)
            assert result.ranked.empty
            assert result.scored["rank"].isna().all()

    def test_an_empty_frame(self) -> None:
        frame = pd.DataFrame(
            {"instrument_id": [], "symbol": [], "x": [], "y": [], "passes_filters": []}
        )
        spec = RankingSpec(terms=self.TERMS, mode="composite", scope="filtered_results")
        assert rank_frame(frame, spec).ranked.empty


# ---------------------------------------------------------------------------
# Step 4 — modes and ordering
# ---------------------------------------------------------------------------


class TestModes:
    def _frame(self) -> pd.DataFrame:
        return _frame(
            [
                {"instrument_id": 1, "symbol": "P", "x": 10.0, "y": 0.0},
                {"instrument_id": 2, "symbol": "Q", "x": 9.0, "y": 100.0},
                {"instrument_id": 3, "symbol": "R", "x": 8.0, "y": 99.0},
            ]
        )

    def test_sequential_differs_from_composite(self) -> None:
        terms = (_term("x"), _term("y"))
        sequential = rank_frame(
            self._frame(), RankingSpec(terms=terms, mode="sequential", scope="filtered_results")
        )
        composite = rank_frame(
            self._frame(), RankingSpec(terms=terms, mode="composite", scope="filtered_results")
        )
        assert _order(sequential.ranked) == ["P", "Q", "R"]
        # x: P 1, Q .5, R 0; y: P 0, Q 1, R .5 -> P 50, Q 75, R 25
        assert composite.ranked.set_index("symbol")[COMPOSITE_SCORE].to_dict() == {
            "Q": 75.0,
            "P": 50.0,
            "R": 25.0,
        }
        assert _order(composite.ranked) == ["Q", "P", "R"]
        assert COMPOSITE_SCORE not in sequential.ranked.columns

    def test_sequential_second_term_decides_a_tie_in_the_first(self) -> None:
        frame = _frame(
            [
                {"instrument_id": 1, "symbol": "S", "x": 5.0, "y": 1.0},
                {"instrument_id": 2, "symbol": "T", "x": 5.0, "y": 2.0},
                {"instrument_id": 3, "symbol": "U", "x": 6.0, "y": 0.0},
            ]
        )
        spec = RankingSpec(
            terms=(_term("x"), _term("y", preference="lower")),
            mode="sequential",
            scope="filtered_results",
        )
        assert _order(rank_frame(frame, spec).ranked) == ["U", "S", "T"]

    def test_sequential_target_range_orders_by_distance(self) -> None:
        frame = _frame(
            [
                {"instrument_id": 1, "symbol": "FAR", "x": 9.0},
                {"instrument_id": 2, "symbol": "IN", "x": 2.0},
                {"instrument_id": 3, "symbol": "NEAR", "x": 4.0},
            ]
        )
        spec = RankingSpec(
            terms=(_term("x", preference="target_range", target_min=0.0, target_max=3.0),),
            mode="single",
            scope="filtered_results",
        )
        assert _order(rank_frame(frame, spec).ranked) == ["IN", "NEAR", "FAR"]

    def test_composite_tie_breaks_on_first_term_raw_by_preference(self) -> None:
        frame = _frame(
            [
                {"instrument_id": 1, "symbol": "A", "vol": 0.2, "ret": 10.0},
                {"instrument_id": 2, "symbol": "B", "vol": 0.3, "ret": 20.0},
            ]
        )
        spec = RankingSpec(
            terms=(_term("vol", "risk_execution", preference="lower"), _term("ret")),
            mode="composite",
            scope="filtered_results",
        )
        result = rank_frame(frame, spec)
        # vol lower: A 1, B 0; ret: A 0, B 1 -> both 50. Lower vol wins the tie.
        assert result.ranked[COMPOSITE_SCORE].tolist() == [50.0, 50.0]
        assert _order(result.ranked) == ["A", "B"]

    def test_deterministic_tie_order_is_instrument_id(self) -> None:
        rows: list[dict[str, object]] = [
            {"instrument_id": i, "symbol": f"S{i}", "x": 1.0, "y": 1.0} for i in (40, 7, 19, 3)
        ]
        for mode in ("composite", "sequential"):
            spec = RankingSpec(terms=(_term("x"), _term("y")), mode=mode, scope="fixed_universe")
            forward = rank_frame(_frame(rows), spec).ranked
            backward = rank_frame(_frame(rows[::-1]), spec).ranked
            assert forward["instrument_id"].tolist() == [3, 7, 19, 40]
            assert backward["instrument_id"].tolist() == [3, 7, 19, 40]
            assert forward["rank"].tolist() == [1, 2, 3, 4]

    def test_contributions_sum_to_the_composite_score(self) -> None:
        rng = np.random.default_rng(7)
        frame = _frame(
            [
                {
                    "instrument_id": i,
                    "symbol": f"S{i}",
                    "a": float(rng.normal()),
                    "b": float(rng.normal()),
                    "c": float(rng.normal()),
                }
                for i in range(1, 30)
            ]
        )
        spec = RankingSpec(
            terms=(
                _term("a", "momentum", weight=2.0),
                _term("b", "momentum"),
                _term(
                    "c", "path_quality", preference="target_range", target_min=-0.5, target_max=0.5
                ),
            ),
            mode="composite",
            scope="fixed_universe",
            family_weights={"momentum": 0.7, "path_quality": 0.3},
        )
        ranked = rank_frame(frame, spec).ranked
        contributions = ranked[["term_contrib__a", "term_contrib__b", "term_contrib__c"]].sum(
            axis=1
        )
        assert (contributions - ranked[COMPOSITE_SCORE]).abs().max() <= 0.005 + 1e-9
        # And each contribution is 100 x effective weight x transformed score.
        weights = effective_weights(spec)
        assert weights == pytest.approx({"a": 0.7 * 2 / 3, "b": 0.7 / 3, "c": 0.3})
        for key in ("a", "b", "c"):
            expected = 100 * weights[key] * ranked[f"term_score__{key}"]
            assert ranked[f"term_contrib__{key}"].tolist() == pytest.approx(expected.tolist())

    def test_composite_is_rounded_to_two_places_half_up(self) -> None:
        frame = _frame(
            [{"instrument_id": i, "symbol": f"S{i}", "x": float(i)} for i in range(1, 9)]
        )
        spec = RankingSpec(terms=(_term("x"),), mode="composite", scope="fixed_universe")
        ranked = rank_frame(frame, spec).ranked.set_index("symbol")
        # S2: 1/7 x 100 = 14.2857... -> 14.29; S6: 5/7 x 100 = 71.428... -> 71.43
        assert ranked[COMPOSITE_SCORE]["S2"] == 14.29
        assert ranked[COMPOSITE_SCORE]["S6"] == 71.43


# ---------------------------------------------------------------------------
# Step 1 — computed factors and input hygiene
# ---------------------------------------------------------------------------


class TestComputedFactors:
    def test_desk_score_term_never_ranks_a_rejected_row(self) -> None:
        frame = _frame(
            [
                {"instrument_id": 1, "symbol": "OK", "desk_score": 71.5, "desk_reject": ""},
                {
                    "instrument_id": 2,
                    "symbol": "REJ",
                    "desk_score": None,
                    "desk_reject": "illiquid;",
                },
                {"instrument_id": 3, "symbol": "NOROW", "desk_score": None, "desk_reject": None},
            ]
        )
        for policy in ("penalize", "neutral"):
            spec = RankingSpec(
                terms=(_term("desk_score"),),
                mode="single",
                scope="filtered_results",
                missing_data=policy,
            )
            result = rank_frame(frame, spec)
            assert _order(result.ranked) == ["OK"]
            reasons = result.scored.set_index("symbol")["excluded_reason"].to_dict()
            assert reasons == {
                "OK": "",
                "REJ": "desk_score_ineligible",
                "NOROW": "desk_score_ineligible",
            }

    def test_nse_momentum_score_is_computed_over_eligible_rows(self) -> None:
        frame = _frame(
            [
                {"instrument_id": 1, "symbol": "A", "nse_mr12": 2.0, "nse_mr6": 0.5},
                {"instrument_id": 2, "symbol": "B", "nse_mr12": 1.0, "nse_mr6": 1.5},
                {"instrument_id": 3, "symbol": "C", "nse_mr12": 0.0, "nse_mr6": -0.5},
                {"instrument_id": 4, "symbol": "D", "nse_mr12": -1.0, "nse_mr6": 0.5},
                {"instrument_id": 5, "symbol": "E", "nse_mr12": 3.0, "nse_mr6": 1.0},
                {"instrument_id": 6, "symbol": "X", "nse_mr12": 9.0, "nse_mr6": 9.0},
            ]
        ).assign(in_nifty_200=[True] * 5 + [False], is_fno=True)
        spec = RankingSpec(
            terms=(_term("nse_momentum_score"),),
            mode="single",
            scope="filtered_results",
            missing_data="exclude",
        )
        result = rank_frame(frame, spec)
        # The five-stock example of test_nse_momentum: E 2.008618 > B > A > D > C; X ineligible.
        assert _order(result.ranked) == ["E", "B", "A", "D", "C"]
        assert result.ranked["nse_momentum_score"].round(6).tolist() == [
            2.008618,
            1.678401,
            1.278176,
            0.561015,
            0.458146,
        ]

    def test_nse_population_columns_override_the_frame(self) -> None:
        frame = _frame(
            [
                {"instrument_id": 1, "symbol": "A", "nse_mr12": 2.0, "nse_mr6": 0.5},
                {"instrument_id": 2, "symbol": "B", "nse_mr12": 1.0, "nse_mr6": 1.5},
            ]
        ).assign(
            in_nifty_200=True,
            is_fno=True,
            nse_mr6_mean=0.6,
            nse_mr6_std=math.sqrt(0.44),
            nse_mr12_mean=1.0,
            nse_mr12_std=math.sqrt(2.0),
        )
        spec = RankingSpec(
            terms=(_term("nse_momentum_score"),), mode="single", scope="filtered_results"
        )
        scores = rank_frame(frame, spec).ranked.set_index("symbol")["nse_momentum_score"]
        assert scores.round(6).to_dict() == {"A": 1.278176, "B": 1.678401}

    def test_decimal_and_none_inputs_are_accepted(self) -> None:
        frame = _frame(
            [
                {"instrument_id": 1, "symbol": "A", "x": Decimal("1.50")},
                {"instrument_id": 2, "symbol": "B", "x": None},
                {"instrument_id": 3, "symbol": "C", "x": Decimal("0.25")},
            ]
        )
        spec = RankingSpec(terms=(_term("x"),), mode="composite", scope="filtered_results")
        assert _order(rank_frame(frame, spec).ranked) == ["A", "C", "B"]

    def test_a_null_passes_filters_is_not_a_pass(self) -> None:
        frame = _frame(
            [
                {"instrument_id": 1, "symbol": "A", "x": 1.0},
                {"instrument_id": 2, "symbol": "B", "x": 2.0},
            ]
        )
        frame["passes_filters"] = pd.Series([True, None], dtype="object")
        spec = RankingSpec(terms=(_term("x"),), mode="composite", scope="filtered_results")
        result = rank_frame(frame, spec)
        assert _order(result.ranked) == ["A"]
        context = ExplainContext(result=result, universe="nifty-500", as_of=AS_OF)
        assert explain_instrument(2, context).eligibility.passed is False

    def test_a_missing_term_column_is_an_error_not_a_silent_zero(self) -> None:
        spec = RankingSpec(terms=(_term("absent"),), mode="single", scope="filtered_results")
        with pytest.raises(ValueError, match="absent"):
            rank_frame(_missing_frame(), spec)

    def test_duplicate_instruments_are_refused(self) -> None:
        frame = _frame(
            [
                {"instrument_id": 1, "symbol": "A", "x": 1.0},
                {"instrument_id": 1, "symbol": "A", "x": 2.0},
            ]
        )
        spec = RankingSpec(terms=(_term("x"),), mode="single", scope="filtered_results")
        with pytest.raises(ValueError, match="duplicate"):
            rank_frame(frame, spec)

    def test_spec_validation(self) -> None:
        with pytest.raises(ValueError):
            RankingSpec(terms=(), mode="composite", scope="fixed_universe")
        with pytest.raises(ValueError):
            RankingSpec(terms=(_term("x"), _term("y")), mode="single", scope="fixed_universe")
        with pytest.raises(ValueError):
            _term("x", family="value")
        with pytest.raises(ValueError):
            _term("x", preference="target_range")


# ---------------------------------------------------------------------------
# Step 6 — explain
# ---------------------------------------------------------------------------


def _explain_frame() -> pd.DataFrame:
    base = {
        "desk_score_rank": None,
        "desk_a_trend": 25.0,
        "desk_b_momentum": 12.0,
        "desk_c_sharpe": 18.0,
        "desk_d_consistency": 5.0,
        "desk_e_liquidity": 8.0,
        "desk_f_penalty": 0.0,
        "last_bar_date": AS_OF,
        "recent_corporate_action": False,
        "fail__min_return_1y": False,
        "fail__moving_average_above_200": False,
    }
    rows: list[dict[str, object]] = [
        {
            **base,
            "instrument_id": 1,
            "symbol": "LEADER",
            "ret_12m": 80.0,
            "atr_ext_20": 1.0,
            "desk_score": 68.0,
            "desk_score_rank": 3,
            "desk_reject": "",
            "desk_ext_over_20dma": 8.2,
            "desk_score_version": "desk-score-2026.09.13",
        },
        {
            **base,
            "instrument_id": 2,
            "symbol": "LAGGARD",
            "ret_12m": 5.0,
            "atr_ext_20": 6.0,
            "desk_score": 40.0,
            "desk_score_rank": 90,
            "desk_reject": "",
            "desk_f_penalty": -3.0,
        },
        {
            **base,
            "instrument_id": 3,
            "symbol": "FAILS",
            "ret_12m": 50.0,
            "atr_ext_20": 2.0,
            "desk_score": 55.0,
            "desk_reject": "",
            "fail__min_return_1y": True,
            "passes_filters": False,
        },
        {
            **base,
            "instrument_id": 4,
            "symbol": "REJECTED",
            "ret_12m": 60.0,
            "atr_ext_20": 2.5,
            "desk_score": None,
            "desk_reject": "illiquid;far_from_high;",
            "desk_a_trend": None,
            "desk_f_penalty": None,
        },
        {
            **base,
            "instrument_id": 5,
            "symbol": "YOUNG",
            "ret_12m": None,
            "atr_ext_20": 1.5,
            "desk_score": None,
            "desk_reject": None,
            "last_bar_date": AS_OF - dt.timedelta(days=3),
            "recent_corporate_action": True,
        },
        {
            **base,
            "instrument_id": 6,
            "symbol": "MIDDLE",
            "ret_12m": 30.0,
            "atr_ext_20": 2.0,
            "desk_score": 50.0,
            "desk_reject": "",
        },
    ]
    frame = pd.DataFrame(rows)
    frame["passes_filters"] = frame["passes_filters"].fillna(True).astype(bool)
    return frame


def _explain_context(missing_data: MissingPolicy = "penalize") -> ExplainContext:
    spec = RankingSpec(
        terms=(
            TermSpec(
                key="ret_12m",
                label="1Y RETURN",
                weight_family="momentum",
                preference="higher",
                null_policy="insufficient_history",
            ),
            TermSpec(
                key="atr_ext_20",
                label="ATR EXTENSION",
                weight_family="trend_structure",
                preference="target_range",
                target_min=0.0,
                target_max=3.0,
                null_policy="insufficient_history",
            ),
        ),
        mode="composite",
        scope="filtered_results",
        missing_data=missing_data,
    )
    return ExplainContext(
        result=rank_frame(_explain_frame(), spec),
        universe="nifty-500",
        as_of=AS_OF,
        data_version=42,
        desk_score_version="desk-score-2026.09.13",
        clause_details={"min_return_1y": "1Y return >= 20%"},
    )


class TestExplain:
    def test_explain_a_passing_row(self) -> None:
        context = _explain_context()
        explanation = explain_instrument(1, context)
        assert explanation.symbol == "LEADER"
        assert explanation.rank == 1
        # results {LEADER 80, LAGGARD 5, REJECTED 60, YOUNG missing, MIDDLE 30}:
        # ret_12m over the four present values -> LEADER 1.0; atr_ext_20 1.0 is inside [0, 3] -> 1.0
        assert explanation.total == 100.0
        ret = explanation.terms[0]
        assert (ret.raw, ret.transformed, ret.effective_weight, ret.contribution) == (
            80.0,
            1.0,
            0.5,
            50.0,
        )
        assert ret.missing is False and ret.weight_family == "momentum"
        assert sum(t.contribution or 0.0 for t in explanation.terms) == explanation.total
        assert "Top 20% on 1Y RETURN within the filtered results" in explanation.positives
        assert "Inside the target range on ATR EXTENSION" in explanation.positives
        assert "Desk A trend 25.0 of 25" in explanation.positives
        assert "Desk C Sharpe 18.0 of 20" in explanation.positives
        assert explanation.eligibility.passed is True
        assert explanation.eligibility.failures == ()
        assert explanation.data_quality.missing_factors == ()
        assert explanation.data_quality.stale_price is False
        assert explanation.desk is not None
        assert (explanation.desk.score, explanation.desk.rank, explanation.desk.eligible) == (
            68.0,
            3,
            True,
        )
        assert explanation.provenance.ranking_engine_version == RANKING_ENGINE_VERSION
        assert explanation.provenance.desk_score_version == "desk-score-2026.09.13"
        assert explanation.provenance.nse_momentum_version == "nifty200-momentum30-2026.09"
        assert (explanation.provenance.scope, explanation.provenance.mode) == (
            "filtered_results",
            "composite",
        )
        assert explanation.provenance.as_of == "2026-09-11"
        json.dumps(explanation.to_dict())

    def test_explain_deductions_on_a_low_row(self) -> None:
        explanation = explain_instrument(2, _explain_context())
        assert "Bottom 20% on 1Y RETURN within the filtered results" in explanation.deductions
        assert "Outside the target range on ATR EXTENSION by 3" in explanation.deductions
        assert "Desk F penalty -3.0" in explanation.deductions

    def test_explain_a_failing_row(self) -> None:
        explanation = explain_instrument(3, _explain_context())
        assert explanation.rank is None
        assert explanation.total is None
        assert explanation.eligibility.passed is False
        assert [(f.filter, f.detail) for f in explanation.eligibility.failures] == [
            ("min_return_1y", "1Y return >= 20%")
        ]
        # Not in the filtered_results scope set, so no percentile is claimed for it.
        assert all(t.transformed is None for t in explanation.terms)
        assert explanation.terms[0].raw == 50.0

    def test_explain_a_desk_rejected_row(self) -> None:
        explanation = explain_instrument(4, _explain_context())
        assert explanation.desk is not None
        assert explanation.desk.eligible is False
        assert explanation.desk.score is None
        assert explanation.desk.reject == "illiquid;far_from_high;"
        assert "Desk reject: illiquid" in explanation.deductions
        assert "Desk reject: far_from_high" in explanation.deductions
        # desk_score is not a term here, so the rejection informs but does not unrank.
        assert explanation.rank is not None

    def test_explain_a_desk_rejected_row_when_desk_score_ranks(self) -> None:
        spec = RankingSpec(
            terms=(TermSpec("desk_score", "DESK SCORE", "momentum", "higher"),),
            mode="single",
            scope="filtered_results",
        )
        context = ExplainContext(
            result=rank_frame(_explain_frame(), spec), universe="nifty-500", as_of=AS_OF
        )
        explanation = explain_instrument(4, context)
        assert explanation.rank is None
        assert explanation.eligibility.passed is False
        assert [f.filter for f in explanation.eligibility.failures] == ["desk_score"]

    def test_explain_a_missing_data_row(self) -> None:
        explanation = explain_instrument(5, _explain_context())
        assert explanation.terms[0].missing is True
        assert explanation.terms[0].raw is None
        assert explanation.terms[0].transformed == 0.0  # penalize
        assert "Missing 1Y RETURN (missing_data=penalize)" in explanation.deductions
        assert explanation.data_quality.missing_factors == ("ret_12m",)
        assert explanation.data_quality.insufficient_history is True
        assert explanation.data_quality.stale_price is True
        assert explanation.data_quality.recent_corporate_action is True
        assert explanation.desk is None  # no desk_score_daily row at all

    def test_explain_a_missing_data_row_under_exclude(self) -> None:
        explanation = explain_instrument(5, _explain_context("exclude"))
        assert explanation.rank is None
        assert explanation.eligibility.passed is False
        assert explanation.eligibility.failures[0].filter == "missing_data"
        assert "1Y RETURN" in explanation.eligibility.failures[0].detail

    def test_explain_within_sector_names_the_sector(self) -> None:
        sectors = pd.Series(["nifty-it", "nifty-it", None, None, None, None], dtype="object")
        frame = _explain_frame().assign(sector=sectors)
        spec = RankingSpec(
            terms=(TermSpec("ret_12m", "1Y RETURN", "momentum", "higher"),),
            mode="composite",
            scope="within_sector",
        )
        context = ExplainContext(result=rank_frame(frame, spec), universe="nifty-500", as_of=AS_OF)
        explanation = explain(context.result.scored.iloc[0], context)
        assert "Top 20% on 1Y RETURN within sector nifty-it" in explanation.positives

    def test_explain_desk_block_carries_each_grade_with_its_stored_inputs(self) -> None:
        """C2: the desk block is the stored ``desk_score_daily`` row, grade by grade.

        Ranges are ``score.score``'s clips: A 0-25, B 0-25, C 0-20, D 0-10, E 0-10, F -10..0.
        The row stores one raw input, ``ext_over_20dma``, and the formula reads it in A (points
        for 0-20% above the 20-DMA) and F (deduction above 18%) — so exactly those two grades
        carry it; B-E store no inputs and claim none.
        """
        desk = explain_instrument(1, _explain_context()).desk
        assert desk is not None
        assert desk.ext_over_20dma == 8.2
        assert desk.score_version == "desk-score-2026.09.13"
        assert [
            (c.grade, c.key, c.points, c.min_points, c.max_points) for c in desk.components
        ] == [
            ("A", "a_trend", 25.0, 0.0, 25.0),
            ("B", "b_momentum", 12.0, 0.0, 25.0),
            ("C", "c_sharpe", 18.0, 0.0, 20.0),
            ("D", "d_consistency", 5.0, 0.0, 10.0),
            ("E", "e_liquidity", 8.0, 0.0, 10.0),
            ("F", "f_penalty", 0.0, -10.0, 0.0),
        ]
        inputs = {c.grade: [(i.name, i.value) for i in c.inputs] for c in desk.components}
        assert inputs == {
            "A": [("ext_over_20dma", 8.2)],
            "B": [],
            "C": [],
            "D": [],
            "E": [],
            "F": [("ext_over_20dma", 8.2)],
        }
        json.dumps(explain_instrument(1, _explain_context()).to_dict())

    def test_explain_desk_block_without_stored_inputs_reports_none(self) -> None:
        desk = explain_instrument(4, _explain_context()).desk
        assert desk is not None
        assert desk.ext_over_20dma is None
        assert desk.score_version is None
        assert desk.components[0].points is None
        assert desk.components[0].inputs[0].value is None

    def test_explain_rank_history_from_the_previous_session(self) -> None:
        """Previous 4, today 1: three places gained (rank 1 is best, change = previous - today)."""
        base = _explain_context()
        context = ExplainContext(
            result=base.result,
            universe=base.universe,
            as_of=AS_OF,
            previous_as_of=AS_OF - dt.timedelta(days=1),
            previous_rank=4,
        )
        history = explain_instrument(1, context).rank_history
        assert (history.today, history.previous, history.change) == (1, 4, 3)
        assert history.previous_as_of == "2026-09-10"

    def test_explain_rank_history_without_a_previous_rank(self) -> None:
        history = explain_instrument(1, _explain_context()).rank_history
        assert (history.today, history.previous, history.previous_as_of, history.change) == (
            1,
            None,
            None,
            None,
        )
        # Ranked yesterday but not today: no change is claimed either.
        unranked = rank_history(None, 7, AS_OF)
        assert (unranked.previous, unranked.change) == (7, None)
        assert rank_history(5, 2, AS_OF).change == -3

    def test_explain_an_unknown_instrument(self) -> None:
        with pytest.raises(KeyError):
            explain_instrument(999, _explain_context())
