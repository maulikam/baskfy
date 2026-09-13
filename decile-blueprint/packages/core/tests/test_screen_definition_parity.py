"""Keeps the Python side of the Pydantic/Zod mirror honest (Prompt 1 deliverable 6).

The TypeScript half of this contract lives in packages/api-client/test/parity.test.ts, which
compares the Zod schema against the two generated artefacts below. That comparison is only
meaningful if the artefacts actually track the Pydantic model, so this asserts they are current.

If either fails, regenerate:
    make schema
    uv run python -m baskfy_core.screen_definition_corpus \
        > tests/fixtures/screen-definition-corpus.json
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from baskfy_core.factor_registry import FACTORS
from baskfy_core.screen_definition import NON_RANKABLE_FACTORS, ScreenDefinition
from baskfy_core.screen_definition_corpus import CASES
from baskfy_core.screen_definition_corpus import render as render_corpus
from baskfy_core.screen_definition_schema import render as render_schema

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "packages" / "api-client" / "src" / "screen-definition.schema.json"
CORPUS_PATH = REPO_ROOT / "tests" / "fixtures" / "screen-definition-corpus.json"
ZOD_PATH = REPO_ROOT / "packages" / "api-client" / "src" / "screen-definition.ts"


def test_generated_schema_is_committed() -> None:
    assert SCHEMA_PATH.is_file(), f"{SCHEMA_PATH} is missing; run `make schema`"


def test_generated_schema_is_up_to_date() -> None:
    """A stale schema would let the Zod mirror drift while the TS test still passed."""
    assert SCHEMA_PATH.read_text(encoding="utf-8") == render_schema(), (
        "packages/api-client/src/screen-definition.schema.json is stale; run `make schema`"
    )


def test_generated_corpus_is_up_to_date() -> None:
    assert CORPUS_PATH.read_text(encoding="utf-8") == render_corpus(), (
        "tests/fixtures/screen-definition-corpus.json is stale; regenerate it"
    )


def test_schema_forbids_additional_properties_at_every_level() -> None:
    """docs/07: extra="forbid". A nested node that allowed extras would leak typos through."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    nodes = [schema, *schema.get("$defs", {}).values()]
    permissive = [
        node.get("title", "<root>")
        for node in nodes
        if node.get("type") == "object" and node.get("additionalProperties") is not False
    ]
    assert permissive == []


def test_schema_requires_only_index_and_sort_by() -> None:
    """Everything else has a documented default, so a minimal definition must be accepted."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert sorted(schema["required"]) == ["index", "sort_by"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_materialised_output_is_recorded_for_every_valid_case(case: object) -> None:
    """The TS side compares against these, so an unrecorded case would silently check nothing."""
    assert isinstance(case, type(CASES[0]))
    if not case.valid:
        return
    recorded = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    entry = next(c for c in recorded if c["name"] == case.name)
    try:
        model = ScreenDefinition.model_validate(case.value)
    except ValidationError as exc:  # pragma: no cover - failure path
        pytest.fail(f"corpus case {case.name} is marked valid but Pydantic rejects it: {exc}")
    assert entry["materialised"] == model.model_dump(mode="json", by_alias=True)


def test_non_rankable_factors_are_the_registry_s_rankable_false_keys() -> None:
    """C1: ``rankable=False`` is the registry's word; the refusal list is derived from it."""
    assert set(NON_RANKABLE_FACTORS) == {key for key, f in FACTORS.items() if not f.rankable}
    assert NON_RANKABLE_FACTORS, "C1 names filter-only factors; an empty list refuses nothing"


def test_the_zod_mirror_refuses_the_same_non_rankable_factors() -> None:
    """The TS side has no registry, so it carries the list; it must be the Python list exactly."""
    source = ZOD_PATH.read_text(encoding="utf-8")
    match = re.search(r"NON_RANKABLE_FACTORS\s*=\s*\[(.*?)\]\s*as const", source, re.S)
    assert match is not None, f"NON_RANKABLE_FACTORS not found in {ZOD_PATH}"
    assert tuple(re.findall(r'"([a-z0-9_]+)"', match.group(1))) == NON_RANKABLE_FACTORS


@pytest.mark.parametrize("key", NON_RANKABLE_FACTORS)
def test_every_non_rankable_factor_is_refused_as_a_ranking_term(key: str) -> None:
    """PLAN correction 1: refused in any term position, with a message naming factor_ranges."""
    with pytest.raises(ValidationError, match="factor_ranges"):
        ScreenDefinition.model_validate(
            {
                "index": "nifty-500",
                "sort_by": "ret_12m",
                "ranking_mode": "sequential",
                "ranking_terms": [
                    {"factor": "ret_12m", "preference": "higher"},
                    {"factor": key, "preference": "higher"},
                ],
            }
        )
