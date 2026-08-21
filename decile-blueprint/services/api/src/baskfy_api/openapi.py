"""Emit ``openapi.json`` (Prompt 7 deliverable 7).

    "openapi.json emitted at build time into packages/api-client, and a CI step that regenerates
     the TypeScript client and fails if the checked-in client is stale."

docs/02 rule 5: "Typed end to end. Pydantic models -> OpenAPI -> generated TS client. No
hand-written fetch types."

Usage:
    python -m baskfy_api.openapi                 # write packages/api-client/openapi.json
    python -m baskfy_api.openapi --check         # exit 1 if the checked-in file is stale
    python -m baskfy_api.openapi --stdout        # print it

The document is written with sorted keys and a trailing newline so that two runs on two machines
produce the same bytes — a spec whose key order depends on dict insertion is a spec that shows a
diff every time anyone touches an unrelated route.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from baskfy_api.app import create_app
from baskfy_api.settings import Settings

#: Written into the TypeScript package, which is where the generator reads it from.
OUTPUT_RELATIVE: Final = Path("packages") / "api-client" / "openapi.json"


def repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "packages" / "api-client").is_dir():
            return parent
    raise FileNotFoundError("packages/api-client not found above this file")


def default_output() -> Path:
    return repo_root() / OUTPUT_RELATIVE


def render() -> str:
    """The spec, byte-stable.

    Built from a settings object with fixed values rather than the ambient environment: the
    document must not change because a developer has a different ``BASKFY_ENVIRONMENT``.
    """
    app = create_app(Settings(environment="local", jwt_secret="", rate_limit_enabled=False))
    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Emit the Decile OpenAPI document.")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the checked-in document differs from the generated one.",
    )
    parser.add_argument("--stdout", action="store_true", help="Print instead of writing.")
    args = parser.parse_args(argv)

    document = render()
    if args.stdout:
        sys.stdout.write(document)
        return 0

    target = args.output or default_output()
    if args.check:
        if not target.is_file():
            print(f"{target} is missing; run `make openapi`", file=sys.stderr)
            return 1
        if target.read_text(encoding="utf-8") != document:
            print(f"{target} is stale; run `make openapi`", file=sys.stderr)
            return 1
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(document, encoding="utf-8")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
