"""Emit the ScreenDefinition JSON Schema that packages/api-client checks itself against.

``make schema`` writes this to ``packages/api-client/src/screen-definition.schema.json``. Two
tests keep the pair honest:

* ``packages/core/tests/test_screen_definition_parity.py`` fails if the committed file is stale
  relative to the Pydantic model;
* ``packages/api-client/test/parity.test.ts`` fails if the Zod schema diverges from that file.

So a change to either side that is not mirrored breaks a build, which is what PROMPTS.md
Prompt 1 deliverable 6 asks for.
"""

from __future__ import annotations

import json
import sys

from decile_core.screen_definition import ScreenDefinition


def build_schema() -> dict[str, object]:
    """The JSON Schema for a ScreenDefinition *input* payload (aliases applied)."""
    schema = ScreenDefinition.model_json_schema(by_alias=True, mode="validation")
    schema["$id"] = "https://decile.in/schemas/screen-definition.json"
    schema["title"] = "ScreenDefinition"
    return schema


def render() -> str:
    return json.dumps(build_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    sys.stdout.write(render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
