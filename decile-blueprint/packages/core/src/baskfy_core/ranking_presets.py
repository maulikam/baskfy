"""Named ranking presets — docs/ranking/PLAN.md Phase 1.5.

Each key in :data:`baskfy_core.ranking.RANKING_PRESETS` has a structured patch that can be
merged into a ``ScreenDefinition`` payload. Weighted composites beyond ``desk_quality`` stay
descriptive until a hold-out promotes them; they still resolve to concrete Sort By keys so the
UI can apply a named starting point.

``nse_momentum`` is intentionally absent: PLAN correction #8 forbids that label without the
exact NSE methodology and universe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from baskfy_core.ranking import DESK_SCORE_KEY, RANKING_PRESETS, RankingMode, RankingScope

#: JSON-shaped patches for ``ScreenDefinition.model_validate({**base, **patch})``.
PresetPatch = dict[str, object]


@dataclass(frozen=True, slots=True)
class PresetSpec:
    """One named preset: human blurb + definition patch + promotion status."""

    key: str
    description: str
    patch: PresetPatch
    #: ``ready`` — safe to apply as Sort By today. ``research`` — patch is a starting point;
    #: pair with drawdown / target-range discipline before treating as a product default.
    status: str


def _spec(key: str, patch: PresetPatch, *, status: str) -> PresetSpec:
    if key not in RANKING_PRESETS:
        raise KeyError(f"preset {key!r} missing from RANKING_PRESETS")
    return PresetSpec(key=key, description=RANKING_PRESETS[key], patch=patch, status=status)


PRESET_SPECS: Final[dict[str, PresetSpec]] = {
    "desk_quality": _spec(
        "desk_quality",
        {
            "sort_by": DESK_SCORE_KEY,
            "sort_direction": "desc",
            "ranking_mode": RankingMode.SINGLE.value,
            "ranking_scope": RankingScope.FILTERED_RESULTS.value,
            "factor_two": {"enabled": False, "sort_by": None, "sort_direction": "desc"},
            "factor_three": {"enabled": False, "sort_by": None, "sort_direction": "desc"},
        },
        status="ready",
    ),
    "path_quality": _spec(
        "path_quality",
        {
            # Promote only with drawdown pairing (PLAN correction #3) — starting Sort By only.
            "sort_by": "pos_days_6m",
            "sort_direction": "desc",
            "ranking_mode": RankingMode.SINGLE.value,
            "ranking_scope": RankingScope.FILTERED_RESULTS.value,
        },
        status="research",
    ),
    "trend_structure": _spec(
        "trend_structure",
        {
            # Stack is monotonic; MA distance stays filter/target_range, not the sort key.
            "sort_by": "ma_stack_score",
            "sort_direction": "desc",
            "ranking_mode": RankingMode.SINGLE.value,
            "ranking_scope": RankingScope.FILTERED_RESULTS.value,
            "moving_average": {
                "enabled": True,
                "above_200": True,
                "above_100": True,
                "above_50": True,
                "above_20": True,
                "below_200": False,
                "below_100": False,
                "below_50": False,
                "below_20": False,
            },
        },
        status="research",
    ),
    "participation": _spec(
        "participation",
        {
            "sort_by": "vol_expansion_1w_12m",
            "sort_direction": "desc",
            "ranking_mode": RankingMode.SINGLE.value,
            "ranking_scope": RankingScope.FILTERED_RESULTS.value,
        },
        status="research",
    ),
}


def apply_preset(name: str) -> PresetPatch:
    """Return the ScreenDefinition patch for a named preset.

    Raises ``KeyError`` for unknown names — including ``nse_momentum``, which is refused until
    the exact NSE methodology ships (PLAN correction #8).
    """
    lowered = name.strip().lower()
    if lowered in {"nse_momentum", "nse-momentum", "nse momentum"}:
        raise KeyError(
            "unknown ranking preset 'nse_momentum': exact NSE methodology + universe "
            "required before that label may be used (docs/ranking/PLAN.md correction #8)"
        )
    spec = PRESET_SPECS.get(lowered)
    if spec is None:
        known = ", ".join(sorted(PRESET_SPECS))
        raise KeyError(f"unknown ranking preset {name!r}; known: {known}")
    return dict(spec.patch)


def list_presets() -> list[dict[str, str]]:
    """Wire-friendly list for a future ``GET /meta/ranking-presets``."""
    return [
        {
            "key": spec.key,
            "description": spec.description,
            "status": spec.status,
            "sort_by": str(spec.patch["sort_by"]),
        }
        for spec in PRESET_SPECS.values()
    ]
