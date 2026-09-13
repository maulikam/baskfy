"""docs/ranking/PLAN.md C1 — the registry half of the contract.

Every expectation below is copied from C1's table and its "Registry gains" paragraph, not read
back from ``factor_registry``: the key list, each key's preference, weight family and rankable
flag, ``regime_priority``'s SQL order, and the legacy/research split.
"""

from __future__ import annotations

import pytest

from baskfy_core.factor_registry import (
    FACTORS,
    REGIME_PRIORITY_SQL,
    ValidationStatus,
    WeightFamily,
    sql_for,
)
from baskfy_core.ranking import FactorPreference

H = FactorPreference.HIGHER
L = FactorPreference.LOWER
T = FactorPreference.TARGET_RANGE
E = FactorPreference.ELIGIBILITY
MOM = WeightFamily.MOMENTUM
PATH = WeightFamily.PATH_QUALITY
TREND = WeightFamily.TREND_STRUCTURE
PART = WeightFamily.PARTICIPATION
RISK = WeightFamily.RISK_EXECUTION

#: C1's table, one row per key: (preference, weight_family, rankable).
C1_STORED: dict[str, tuple[FactorPreference, WeightFamily, bool]] = {
    "atr_14": (E, RISK, False),
    "atr_ext_20": (T, TREND, True),
    "ma50_slope_20": (H, TREND, True),
    "eff_ratio_63": (H, PATH, True),
    "max_dd_6m": (L, PATH, True),
    "max_dd_12m": (L, PATH, True),
    "downside_vol_6m": (L, RISK, True),
    "downside_vol_12m": (L, RISK, True),
    "sortino_6m": (H, RISK, True),
    "sortino_12m": (H, RISK, True),
    "underwater_12m": (L, PATH, True),
    "ret_ex_top3_12m": (H, PATH, True),
    "accel_21_105": (H, MOM, True),
    "accel_21_105_vs": (H, MOM, True),
    "vol_exp_21_126": (H, PART, True),
    "vol_persist_20": (H, PART, True),
    "excess_ret_3m": (E, MOM, False),
    "excess_ret_6m": (E, MOM, False),
    "excess_ret_12m": (E, MOM, False),
    "resid_ret_12m": (H, MOM, True),
    "rs_persist_126": (H, MOM, True),
    "mom_pctile": (E, MOM, False),
    "rank_persist_20": (H, MOM, True),
    "nse_mr6": (E, MOM, False),
    "nse_mr12": (E, MOM, False),
}

#: C1: "new SQL factor regime_priority" and "Computed (non-SQL) factors: ... nse_momentum_score".
PHASE_TWO_KEYS: frozenset[str] = frozenset({*C1_STORED, "regime_priority", "nse_momentum_score"})

#: C1 names these as the computed factors that have no SQL.
COMPUTED_KEYS: frozenset[str] = frozenset({"desk_score", "nse_momentum_score"})


@pytest.mark.parametrize("key", sorted(C1_STORED))
def test_every_c1_key_carries_the_c1_preference_family_and_rankable_flag(key: str) -> None:
    preference, family, rankable = C1_STORED[key]
    factor = FACTORS[key]
    assert factor.preference is preference
    assert factor.weight_family is family
    assert factor.rankable is rankable
    assert factor.is_stored and factor.sql_expr == key


def test_the_filter_only_keys_are_not_rankable() -> None:
    for key in ("excess_ret_3m", "excess_ret_6m", "excess_ret_12m", "mom_pctile"):
        assert FACTORS[key].rankable is False, key
    for key in ("nse_mr6", "nse_mr12", "atr_14"):
        assert FACTORS[key].rankable is False, key


def test_regime_priority_is_an_explicit_higher_trend_order() -> None:
    factor = FACTORS["regime_priority"]
    assert factor.preference is H
    assert factor.weight_family is TREND
    assert factor.rankable is True
    assert factor.validation_status is ValidationStatus.RESEARCH
    assert sql_for("regime_priority") == REGIME_PRIORITY_SQL
    # BULL -> 2, NEUTRAL -> 1, BEAR -> 0: an order, never a distance.
    assert "'BULL' THEN 2" in REGIME_PRIORITY_SQL
    assert "'NEUTRAL' THEN 1" in REGIME_PRIORITY_SQL
    assert "'BEAR' THEN 0" in REGIME_PRIORITY_SQL


@pytest.mark.parametrize("key", sorted(FACTORS))
def test_every_factor_has_a_weight_family_and_a_one_sentence_definition(key: str) -> None:
    factor = FACTORS[key]
    assert isinstance(factor.weight_family, WeightFamily)
    assert factor.definition.strip(), f"{key} has an empty definition"


@pytest.mark.parametrize("key", sorted(FACTORS))
def test_pre_phase_two_factors_are_legacy_and_new_ones_research(key: str) -> None:
    expected = ValidationStatus.RESEARCH if key in PHASE_TWO_KEYS else ValidationStatus.LEGACY
    assert FACTORS[key].validation_status is expected


def test_every_phase_two_key_is_registered() -> None:
    assert PHASE_TWO_KEYS.issubset(FACTORS)


@pytest.mark.parametrize("key", sorted(set(FACTORS) - COMPUTED_KEYS))
def test_sql_for_answers_every_sql_key(key: str) -> None:
    assert sql_for(key).strip()


@pytest.mark.parametrize("key", sorted(COMPUTED_KEYS))
def test_sql_for_refuses_the_computed_keys(key: str) -> None:
    assert FACTORS[key].is_computed
    with pytest.raises(ValueError, match="computed"):
        sql_for(key)
