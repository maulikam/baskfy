"""Named ranking presets, docs/ranking/PLAN.md C7: each one a ``ScreenDefinition`` patch.

Every key in :data:`baskfy_core.ranking.RANKING_PRESETS` has one :class:`PresetSpec` here. A patch
is merged over a definition (``ScreenDefinition.model_validate({**base, **patch})``) and names every
ranking field it depends on, so applying it never inherits a stale ranking setting: a preset that
ranks by explicit terms also clears ``family_weights``, resets ``missing_data`` and turns
``factor_two`` / ``factor_three`` off.

Status (C7: "Status per preset comes from leaf F's evidence", ``docs/ranking/VALIDATION.md`` §7):

* ``ready`` — every term the preset ranks by passed C8 (``validated``), or, for ``desk_quality``,
  the preset is the book's own ``score()`` and is pinned to it row for row.
* ``research`` — at least one term was ``rejected`` by C8 or could not be tested (registry
  ``research``). Such a preset is a starting point, never a product default.

``desk_sequential`` departs from C7 in one place: C7's third tie-break, ``median_vol_12m``, is a
result column, not a factor-registry key, so no ranking term can name it. The preset ranks by the
first two terms and says so in its description (``docs/DECISIONS-MERGE.md`` "Ranking 2.D", 2D.8).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Final, Literal

from baskfy_core.ranking import DESK_SCORE_KEY, RANKING_PRESETS, RankingMode, RankingScope

#: JSON-shaped patches for ``ScreenDefinition.model_validate({**base, **patch})``.
PresetPatch = dict[str, object]
PresetStatus = Literal["ready", "research"]

#: The label C7 gives ``nse_momentum``. PLAN correction 8: this wording, or no NSE label at all.
NSE_MOMENTUM_LABEL: Final = "NIFTY200 Momentum 30 score (NSE methodology)"


@dataclass(frozen=True, slots=True)
class PresetSpec:
    """One named preset: label, description, definition patch and promotion status."""

    key: str
    label: str
    description: str
    patch: PresetPatch
    status: PresetStatus

    @property
    def mode(self) -> str:
        return str(self.patch["ranking_mode"])

    @property
    def factors(self) -> tuple[str, ...]:
        """The factor keys the preset ranks by, in order: its terms, else its ``sort_by``."""
        terms = self.patch.get("ranking_terms")
        if isinstance(terms, list) and terms:
            return tuple(str(term["factor"]) for term in terms if isinstance(term, dict))
        return (str(self.patch["sort_by"]),)


_EXTRA_OFF: Final[dict[str, object]] = {"enabled": False, "sort_by": None, "sort_direction": "desc"}


def _term(factor: str, preference: str, **bounds: float) -> dict[str, object]:
    return {"factor": factor, "preference": preference, **bounds}


def _ranked_by_terms(mode: RankingMode, *terms: dict[str, object]) -> PresetPatch:
    """A patch that ranks by explicit terms and resets every setting the terms replace."""
    return {
        "sort_by": terms[0]["factor"],
        "sort_direction": "desc",
        "ranking_mode": mode.value,
        "ranking_scope": RankingScope.FILTERED_RESULTS.value,
        "ranking_terms": list(terms),
        "family_weights": None,
        "missing_data": "penalize",
        "factor_two": dict(_EXTRA_OFF),
        "factor_three": dict(_EXTRA_OFF),
    }


def _spec(key: str, label: str, patch: PresetPatch, *, status: PresetStatus) -> PresetSpec:
    if key not in RANKING_PRESETS:
        raise KeyError(f"preset {key!r} missing from RANKING_PRESETS")
    return PresetSpec(
        key=key, label=label, description=RANKING_PRESETS[key], patch=patch, status=status
    )


PRESET_SPECS: Final[dict[str, PresetSpec]] = {
    "desk_quality": _spec(
        "desk_quality",
        "Desk quality",
        {
            # C7: single desk_score. The legacy single sort reads desk_score_daily (C2), which is
            # what the parity test against baskfy_core.score pins.
            "sort_by": DESK_SCORE_KEY,
            "sort_direction": "desc",
            "ranking_mode": RankingMode.SINGLE.value,
            "ranking_scope": RankingScope.FILTERED_RESULTS.value,
            "factor_two": dict(_EXTRA_OFF),
            "factor_three": dict(_EXTRA_OFF),
        },
        status="ready",
    ),
    "path_quality": _spec(
        "path_quality",
        "Path quality",
        _ranked_by_terms(
            RankingMode.COMPOSITE,
            _term("pos_days_6m", "higher"),
            _term("max_dd_12m", "lower"),
            _term("downside_vol_12m", "lower"),
            _term("ret_ex_top3_12m", "higher"),
        ),
        # max_dd_12m, downside_vol_12m and ret_ex_top3_12m: C8 rejected.
        status="research",
    ),
    "trend_structure": _spec(
        "trend_structure",
        "Trend structure",
        _ranked_by_terms(
            RankingMode.COMPOSITE,
            _term("ma_stack_score", "higher"),
            _term("ma50_slope_20", "higher"),
            _term("atr_ext_20", "target_range", target_min=0.0, target_max=3.0),
        ),
        # ma50_slope_20 and atr_ext_20: C8 rejected.
        status="research",
    ),
    "participation": _spec(
        "participation",
        "Participation",
        _ranked_by_terms(
            RankingMode.COMPOSITE,
            _term("vol_exp_21_126", "higher"),
            _term("vol_persist_20", "higher"),
        ),
        # vol_exp_21_126 and vol_persist_20: C8 rejected.
        status="research",
    ),
    "leadership": _spec(
        "leadership",
        "Leadership",
        _ranked_by_terms(
            RankingMode.COMPOSITE,
            _term("rank_persist_20", "higher"),
            _term("rs_persist_126", "higher"),
            _term("avg_sharpe_12_6_3_1", "higher"),
        ),
        # rank_persist_20 validated, but rs_persist_126 was not testable (no NIFTY 500 levels).
        status="research",
    ),
    "nse_momentum": _spec(
        "nse_momentum",
        NSE_MOMENTUM_LABEL,
        {
            **_ranked_by_terms(RankingMode.SINGLE, _term("nse_momentum_score", "higher")),
            "index": "nifty-200",
        },
        # nse_momentum_score: not testable (Nifty 200 / F&O membership only from 2021-08).
        status="research",
    ),
    "desk_sequential": _spec(
        "desk_sequential",
        "Desk sequential",
        _ranked_by_terms(
            RankingMode.SEQUENTIAL,
            _term(DESK_SCORE_KEY, "higher"),
            _term("atr_ext_20", "lower"),
        ),
        # atr_ext_20: C8 rejected.
        status="research",
    ),
}


def apply_preset(name: str) -> PresetPatch:
    """Return the ScreenDefinition patch for a named preset. ``KeyError`` for unknown names."""
    spec = PRESET_SPECS.get(name.strip().lower())
    if spec is None:
        known = ", ".join(sorted(PRESET_SPECS))
        raise KeyError(f"unknown ranking preset {name!r}; known: {known}")
    return copy.deepcopy(spec.patch)


def list_presets() -> list[dict[str, str]]:
    """Wire-friendly rows for ``GET /meta/ranking-presets`` (C6), in :data:`PRESET_SPECS` order."""
    return [
        {
            "key": spec.key,
            "label": spec.label,
            "description": spec.description,
            "status": spec.status,
            "sort_by": str(spec.patch["sort_by"]),
        }
        for spec in PRESET_SPECS.values()
    ]
