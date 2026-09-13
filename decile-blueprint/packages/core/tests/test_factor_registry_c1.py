"""docs/ranking/PLAN.md C1 — the registry half of the contract.

Every expectation below is copied from C1's table and its "Registry gains" paragraph, not read
back from ``factor_registry``: the key list, each key's preference, weight family and rankable
flag, ``regime_priority``'s SQL order, and the legacy/research split. The Phase-2 statuses are
read from docs/ranking/VALIDATION.md's generated status table (C8), not from the registry.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

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


def _validation_md() -> str:
    """docs/ranking/VALIDATION.md, found by walking up rather than a fixed number of hops."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs" / "ranking" / "VALIDATION.md"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise AssertionError("docs/ranking/VALIDATION.md was not found above this file")


def _generated(text: str, name: str) -> str:
    match = re.search(
        rf"<!-- generated:{name}:begin[^>]*-->(.*?)<!-- generated:{name}:end -->", text, re.S
    )
    assert match, f"VALIDATION.md has no generated {name!r} section"
    return match.group(1)


#: VALIDATION.md §5: ``| `key` | ... | **status** | registry value |`` — the rule's output.
C8_REGISTRY_STATUS: Final[dict[str, ValidationStatus]] = {
    m.group(1): ValidationStatus(m.group(2))
    for m in re.finditer(
        r"^\| `([a-z0-9_]+)` \|.*\| (legacy|research|validated|rejected) \|$",
        _generated(_validation_md(), "promotion"),
        re.M,
    )
}


def test_validation_md_judged_every_rankable_phase_two_factor() -> None:
    rankable = {key for key in PHASE_TWO_KEYS if FACTORS[key].rankable}
    assert set(C8_REGISTRY_STATUS) == rankable


@pytest.mark.parametrize("key", sorted(FACTORS))
def test_validation_status_matches_validation_md(key: str) -> None:
    """Pre-Phase-2 keys are legacy; Phase-2 keys carry VALIDATION.md's status, else research."""
    if key not in PHASE_TWO_KEYS:
        expected = ValidationStatus.LEGACY
    else:
        expected = C8_REGISTRY_STATUS.get(key, ValidationStatus.RESEARCH)
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
