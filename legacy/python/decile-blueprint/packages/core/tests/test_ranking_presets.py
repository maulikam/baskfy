"""Named presets + desk_quality validation vs baskfy_core.score."""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import pytest

from baskfy_core.factor_registry import FACTORS, ValidationStatus
from baskfy_core.ranking import DESK_SCORE_KEY, RANKING_PRESETS, rerank_survivors_by_desk_score
from baskfy_core.ranking_presets import PRESET_SPECS, apply_preset, list_presets
from baskfy_core.score import score
from baskfy_core.screen_definition import ScreenDefinition


@dataclass
class _Cfg:
    EXCLUDED_SYMBOLS: Collection[str] = ()
    REJECT_SERIES: Collection[str] = ()
    MIN_MEDIAN_DAILY_VALUE: float = 0.0
    MAX_AWAY_FROM_HIGH: float = -100.0
    MAX_CIRCUITS_3M: float = 99.0
    PENALTY_CIRCUITS_1Y: float = 99.0
    MOMENTUM_BLEND: dict[str, float] = field(
        default_factory=lambda: {
            "one_month": 0.2,
            "three_months": 0.2,
            "six_months": 0.2,
            "nine_months": 0.2,
            "one_year": 0.2,
        }
    )
    SHARPE_BLEND: dict[str, float] = field(
        default_factory=lambda: {
            "one_month": 0.2,
            "three_months": 0.2,
            "six_months": 0.2,
            "nine_months": 0.2,
            "one_year": 0.2,
        }
    )
    STOP_VOL_MULT: float = 2.0
    STOP_MIN: float = 0.08
    STOP_MAX: float = 0.25


def _desk_row(symbol: str, **overrides: float | str | int) -> dict[str, float | str | int]:
    row: dict[str, float | str | int] = {
        "symbol": symbol,
        "series": "EQ",
        "date": "2026-08-18",
        "close": 100.0,
        "marketcap": 50_000.0,
        "median_volume_one_year": 100_000_000.0,
        "rsi_one_month": 60.0,
        "volatility_one_year": 0.3,
        "beta": 1.0,
        "circuits_three_months": 0.0,
        "circuits_one_year": 0.0,
        "positive_days_percent_three_months": 55.0,
        "positive_days_percent_six_months": 54.0,
        "ma_20": 95.0,
        "ma_50": 90.0,
        "ma_100": 85.0,
        "ma_200": 80.0,
        "away_from_high_one_year": -5.0,
        "is_nifty_fno": 1,
        "absolute_return_one_month": 5.0,
        "absolute_return_three_months": 12.0,
        "absolute_return_six_months": 20.0,
        "absolute_return_nine_months": 25.0,
        "absolute_return_one_year": 30.0,
        "sharpe_return_one_month": 0.5,
        "sharpe_return_three_months": 1.0,
        "sharpe_return_six_months": 1.5,
        "sharpe_return_nine_months": 1.8,
        "sharpe_return_one_year": 2.0,
        "instrument_id": hash(symbol) % 10_000,
        "universe_mask": 1,
    }
    row.update(overrides)
    return row


#: docs/ranking/PLAN.md C7, verbatim: key -> (mode, [(factor, preference, target bounds)]).
C7: dict[str, tuple[str, list[tuple[str, str, tuple[float, float] | None]]]] = {
    "desk_quality": ("single", [("desk_score", "higher", None)]),
    "path_quality": (
        "composite",
        [
            ("pos_days_6m", "higher", None),
            ("max_dd_12m", "lower", None),
            ("downside_vol_12m", "lower", None),
            ("ret_ex_top3_12m", "higher", None),
        ],
    ),
    "trend_structure": (
        "composite",
        [
            ("ma_stack_score", "higher", None),
            ("ma50_slope_20", "higher", None),
            ("atr_ext_20", "target_range", (0.0, 3.0)),
        ],
    ),
    "participation": (
        "composite",
        [("vol_exp_21_126", "higher", None), ("vol_persist_20", "higher", None)],
    ),
    "leadership": (
        "composite",
        [
            ("rank_persist_20", "higher", None),
            ("rs_persist_126", "higher", None),
            ("avg_sharpe_12_6_3_1", "higher", None),
        ],
    ),
    "nse_momentum": ("single", [("nse_momentum_score", "higher", None)]),
    # C7 names a third tie-break, median_vol_12m higher. It is a result column, not a registry
    # factor, so no ranking term can carry it (DECISIONS-MERGE 2D.8); the first two are pinned.
    "desk_sequential": (
        "sequential",
        [("desk_score", "higher", None), ("atr_ext_20", "lower", None)],
    ),
}

#: The web editor's default definition (`apps/web/src/lib/screens/defaults.ts` defaultDefinition):
#: the base a preset patch is validated over.
DEFAULT_DEFINITION: dict[str, object] = {
    "index": "nifty-500",
    "sort_by": "avg_sharpe_12_6_3_1",
    "sort_direction": "desc",
    "ranking_mode": "composite",
    "ranking_scope": "fixed_universe",
}


def _definition(key: str) -> ScreenDefinition:
    return ScreenDefinition.model_validate({**DEFAULT_DEFINITION, **PRESET_SPECS[key].patch})


def _validation_doc() -> str:
    return next(
        parent / "docs" / "ranking" / "VALIDATION.md"
        for parent in Path(__file__).resolve().parents
        if (parent / "docs" / "ranking" / "VALIDATION.md").is_file()
    ).read_text(encoding="utf-8")


def test_preset_specs_cover_ranking_presets() -> None:
    assert set(PRESET_SPECS) == set(RANKING_PRESETS)


def test_the_presets_are_exactly_c7s() -> None:
    assert list(PRESET_SPECS) == list(C7)


@pytest.mark.parametrize("key", list(C7))
def test_every_preset_validates_as_a_patch_over_the_default_definition(key: str) -> None:
    definition = _definition(key)
    mode, terms = C7[key]
    assert definition.ranking_mode == mode
    if key == "desk_quality":
        # C7 "single desk_score": the legacy single sort, which reads desk_score_daily (C2).
        assert definition.ranking_terms == []
        assert definition.sort_by == DESK_SCORE_KEY
        return
    assert [
        (
            t.factor,
            t.preference,
            None if t.target_min is None and t.target_max is None else (t.target_min, t.target_max),
        )
        for t in definition.ranking_terms
    ] == terms
    assert definition.sort_by == terms[0][0]


@pytest.mark.parametrize("key", list(C7))
def test_a_preset_resets_what_its_ranking_replaces(key: str) -> None:
    """Applied over a screen with extras and term settings, the result is still a valid screen."""
    busy = {
        **DEFAULT_DEFINITION,
        "ranking_mode": "sequential",
        "factor_two": {"enabled": True, "sort_by": "ret_3m", "sort_direction": "desc"},
        "factor_three": {"enabled": True, "sort_by": "ret_1m", "sort_direction": "desc"},
    }
    definition = ScreenDefinition.model_validate({**busy, **PRESET_SPECS[key].patch})
    assert not definition.factor_two.is_active()
    assert not definition.factor_three.is_active()


def test_nse_momentum_is_offered_with_c7s_universe_and_label() -> None:
    """PLAN correction 8: the NSE label only with the exact score and universe."""
    patch = apply_preset("nse_momentum")
    definition = _definition("nse_momentum")
    assert definition.index == "nifty-200"
    assert definition.sort_by == "nse_momentum_score"
    assert PRESET_SPECS["nse_momentum"].label == "NIFTY200 Momentum 30 score (NSE methodology)"
    assert patch["index"] == "nifty-200"
    description = PRESET_SPECS["nse_momentum"].description.lower()
    for construction_step in ("free-float", "cap", "15/45"):
        assert construction_step in description, construction_step


@pytest.mark.parametrize("key", list(C7))
def test_every_preset_carries_a_status_and_a_label(key: str) -> None:
    spec = PRESET_SPECS[key]
    assert spec.status in ("ready", "research")
    assert spec.label


@pytest.mark.parametrize("key", list(C7))
def test_no_preset_is_ready_on_a_rejected_or_untested_factor(key: str) -> None:
    """C8: a rejected or not-testable factor (registry ``research``) cannot make a preset ready."""
    spec = PRESET_SPECS[key]
    weak = [
        f
        for f in spec.factors
        if FACTORS[f].validation_status in (ValidationStatus.REJECTED, ValidationStatus.RESEARCH)
    ]
    if weak:
        assert spec.status == "research", weak


def test_only_desk_quality_is_ready() -> None:
    """VALIDATION.md §7: no C7 composite has all terms validated; desk_quality is score() parity."""
    assert [k for k, s in PRESET_SPECS.items() if s.status == "ready"] == ["desk_quality"]


def test_preset_statuses_match_validation_md() -> None:
    """docs/ranking/VALIDATION.md §7 (generated): each preset's mode, terms and status after C8."""
    section = re.search(
        r"<!-- generated:presets:begin[^>]*-->(.*?)<!-- generated:presets:end -->",
        _validation_doc(),
        re.S,
    )
    assert section, "VALIDATION.md has no generated presets section"
    documented = {
        m.group(1): (m.group(2), tuple(re.findall(r"`([a-z0-9_]+)`", m.group(3))), m.group(4))
        for m in re.finditer(
            r"^\| `([a-z_]+)` \| ([a-z]+) \| (.+?) \| .* \| ([a-z]+) \|$", section.group(1), re.M
        )
    }
    assert documented == {
        spec.key: (spec.mode, spec.factors, spec.status) for spec in PRESET_SPECS.values()
    }


def test_desk_quality_patch_is_desk_score() -> None:
    patch = apply_preset("desk_quality")
    assert patch["sort_by"] == DESK_SCORE_KEY
    definition = ScreenDefinition.model_validate(
        {"index": "nifty-500", "sort_by": "ret_12m", **patch}
    )
    assert definition.sort_by == DESK_SCORE_KEY
    assert definition.ranking_mode == "single"


def test_an_unknown_preset_is_refused() -> None:
    with pytest.raises(KeyError, match="unknown ranking preset"):
        apply_preset("not_a_preset")


def test_apply_preset_returns_a_copy() -> None:
    patch = apply_preset("leadership")
    patch["sort_by"] = "ret_1m"
    assert PRESET_SPECS["leadership"].patch["sort_by"] == "rank_persist_20"


def test_list_presets_exposes_label_status_and_sort_by() -> None:
    rows = list_presets()
    assert [r["key"] for r in rows] == list(PRESET_SPECS)
    desk = next(r for r in rows if r["key"] == "desk_quality")
    assert desk["sort_by"] == DESK_SCORE_KEY
    assert desk["status"] == "ready"
    assert desk["label"] == "Desk quality"


def test_desk_quality_ordering_matches_score() -> None:
    """Validation harness: preset path order == direct score() order."""
    cfg = _Cfg()
    frame = pd.DataFrame(
        [
            _desk_row("AAA", absolute_return_one_year=40.0),
            _desk_row("BBB", absolute_return_one_year=10.0),
            _desk_row("CCC", absolute_return_one_year=25.0),
        ]
    )
    direct = score(frame.copy(), cfg).sort_values("SCORE", ascending=False, kind="mergesort")
    ordered, _ = rerank_survivors_by_desk_score(frame.copy(), cfg=cfg, direction="desc")
    eligible = ordered.loc[ordered["desk_eligible"]]
    assert list(eligible["symbol"]) == list(direct.loc[direct["reject"].fillna("") == ""]["symbol"])
