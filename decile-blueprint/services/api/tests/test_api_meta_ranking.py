"""``/meta/factors`` ranking fields and ``/meta/ranking-presets`` — docs/ranking/PLAN.md C6.

No database: both routes are static catalogues served from ``baskfy_core``, so the route
functions are called directly and the OpenAPI contract is read off the app. The HTTP-level
tests (anonymous access, the wire JSON) live in ``test_api_meta.py`` and need Postgres.
"""

from __future__ import annotations

import json

from baskfy_api.openapi import render
from baskfy_api.routers.meta import get_factors, get_ranking_presets
from baskfy_core.factor_registry import FACTORS, SORT_FACTOR_KEYS, ValidationStatus, WeightFamily
from baskfy_core.ranking import FactorPreference
from baskfy_core.ranking_presets import PRESET_SPECS
from baskfy_core.screen_definition import ScreenDefinition

RANKING_FIELDS = ("preference", "rankable", "weight_family", "validation_status", "definition")


def _schema(name: str) -> dict[str, object]:
    document = json.loads(render())
    schema: dict[str, object] = document["components"]["schemas"][name]
    return schema


class TestFactorOut:
    async def test_every_factor_carries_the_registry_ranking_fields(self) -> None:
        rows = await get_factors()
        assert [row.key for row in rows] == list(SORT_FACTOR_KEYS)
        for row in rows:
            factor = FACTORS[row.key]
            assert row.preference == factor.preference
            assert row.rankable is factor.rankable
            assert row.weight_family == factor.weight_family
            assert row.validation_status == factor.validation_status
            assert row.definition == factor.definition
            assert row.definition, f"{row.key}: C1 requires a one-sentence definition"

    def test_the_contract_requires_the_ranking_fields(self) -> None:
        """The web reported ``preference`` missing from the generated client; it must be there."""
        schema = _schema("FactorOut")
        required = schema["required"]
        assert isinstance(required, list)
        assert set(RANKING_FIELDS) <= set(required)

    def test_the_contract_enums_are_the_core_enums(self) -> None:
        document = json.loads(render())
        schemas = document["components"]["schemas"]
        for enum_type in (FactorPreference, WeightFamily, ValidationStatus):
            assert schemas[enum_type.__name__]["enum"] == [member.value for member in enum_type]


class TestRankingPresets:
    async def test_it_serves_every_core_preset_with_its_status(self) -> None:
        rows = await get_ranking_presets()
        assert [row.key for row in rows] == list(PRESET_SPECS)
        for row in rows:
            spec = PRESET_SPECS[row.key]
            assert row.label == spec.label
            assert row.status == spec.status
            assert row.description == spec.description
            assert row.patch == spec.patch
            assert row.sort_by == spec.patch["sort_by"]

    async def test_it_serves_c7s_seven_presets(self) -> None:
        """PLAN C7, including ``nse_momentum`` now that its score and eligibility exist."""
        assert [row.key for row in await get_ranking_presets()] == [
            "desk_quality",
            "path_quality",
            "trend_structure",
            "participation",
            "leadership",
            "nse_momentum",
            "desk_sequential",
        ]

    async def test_nse_momentum_carries_c7s_label_and_universe(self) -> None:
        """PLAN correction 8: the NSE label only with NSE's universe."""
        row = next(r for r in await get_ranking_presets() if r.key == "nse_momentum")
        assert row.label == "NIFTY200 Momentum 30 score (NSE methodology)"
        assert row.patch["index"] == "nifty-200"
        assert row.sort_by == "nse_momentum_score"

    async def test_every_served_patch_is_a_valid_definition_over_the_default(self) -> None:
        for row in await get_ranking_presets():
            ScreenDefinition.model_validate(
                {"index": "nifty-500", "sort_by": "ret_12m", **row.patch}
            )

    def test_the_contract_requires_label(self) -> None:
        """C6: ``[{key, label, description, status, patch}]``."""
        required = _schema("RankingPresetOut")["required"]
        assert isinstance(required, list)
        assert {"key", "label", "description", "status", "patch"} <= set(required)

    def test_the_route_is_documented_without_a_security_requirement(self) -> None:
        """Static reference data, like ``/meta/factors``: no principal, so no auth refusal."""
        document = json.loads(render())
        operation = document["paths"]["/api/v1/meta/ranking-presets"]["get"]
        factors_operation = document["paths"]["/api/v1/meta/factors"]["get"]
        assert operation.get("security") == factors_operation.get("security")
        assert set(document["paths"]["/api/v1/meta/ranking-presets"]) == {"get"}
