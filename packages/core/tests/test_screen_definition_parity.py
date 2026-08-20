"""Keeps the Python side of the Pydantic/Zod mirror honest (Prompt 1 deliverable 6).

The TypeScript half of this contract lives in packages/api-client/test/parity.test.ts, which
compares the Zod schema against the two generated artefacts below. That comparison is only
meaningful if the artefacts actually track the Pydantic model, so this asserts they are current.

If either fails, regenerate:
    make schema
    uv run python -m decile_core.screen_definition_corpus \
        > tests/fixtures/screen-definition-corpus.json
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from decile_core.screen_definition import ScreenDefinition
from decile_core.screen_definition_corpus import CASES
from decile_core.screen_definition_corpus import render as render_corpus
from decile_core.screen_definition_schema import render as render_schema

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "packages" / "api-client" / "src" / "screen-definition.schema.json"
CORPUS_PATH = REPO_ROOT / "tests" / "fixtures" / "screen-definition-corpus.json"


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
