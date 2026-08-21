"""The interactive API reference — Prompt 20 deliverable 5.

    "Docs: an interactive API reference (Scalar or Redoc) served from the OpenAPI spec, with
     copy-paste examples in curl, Python and TypeScript."

**Redoc, and no new dependency.** docs/02 locks the stack and does not name a documentation
renderer, so the cheapest honest choice is the one FastAPI already knows how to serve: Redoc is a
single script tag against a pinned CDN build. Scalar would mean either a new package or the same
script tag, and Redoc has the property that decides it — it renders the ``x-codeSamples`` vendor
extension natively, which means the three copy-paste examples live **inside the OpenAPI document**
rather than in hand-written HTML beside it. They are therefore machine-readable, versioned with
the spec, and asserted by a test (``services/api/tests/test_public_docs.py``) rather than by
somebody remembering to update a page.

Two consequences worth stating:

* **The reference page loads a script from a CDN.** ``DECILE_REDOC_SCRIPT_URL`` pins the version
  and an air-gapped deployment can repoint it at a self-hosted copy. There is no Subresource
  Integrity hash, because computing one requires fetching the file and this repository's test
  suite is network-blocked — so it would be a hash nobody had verified. Recorded in
  ``docs/DECISIONS.md`` §20.8.
* **The public document is a filtered copy of the main one.** The service publishes one OpenAPI
  document; this module selects the paths under the public prefix and rewrites the ``servers``
  block. A second FastAPI sub-application would have produced a second document natively and
  would also have had its own dependency graph, which would take the public routes out of the
  test harness's session override — see ``services/api/tests/api_helpers.running_app``.
"""

from __future__ import annotations

import html
import json
from collections.abc import Mapping
from typing import Final

from decile_core.public_api import PUBLIC_API_PREFIX, PUBLIC_API_VERSION

__all__ = ["CODE_SAMPLE_KEY", "public_document", "redoc_page"]

#: Redoc's vendor extension. ``x-codeSamples`` (capital S) is the spelling Redoc reads; the older
#: ``x-code-samples`` is still accepted by it but is deprecated.
CODE_SAMPLE_KEY: Final = "x-codeSamples"

#: The header a caller authenticates with — docs/07 §header.
API_KEY_HEADER: Final = "X-API-Key"

PLACEHOLDER_KEY: Final = "dk_7f3a9c1b4d2e_YOUR_SECRET_HERE"

DOC_TITLE: Final = "Decile public API"
DOC_DESCRIPTION: Final = f"""
Derived analytics from Indian equity market data — screen results, factor values and market
breadth. Read-only, versioned at `{PUBLIC_API_VERSION}`, authenticated with an API key in the
`{API_KEY_HEADER}` header.

**Read the terms of use before you build against this.** `GET {PUBLIC_API_PREFIX}/terms` returns
them as JSON, including the exact list of fields this API serves and the list it withholds. The
underlying price and volume records are licensed for our own use and are never served: no
open/high/low/close, no volume, no moving averages, no highs. See the `withheld_fields` member.

Decile is not a SEBI-registered investment adviser. Nothing this API returns is investment advice.
""".strip()


def _curl(method: str, url: str) -> str:
    return (
        f"curl -sS -X {method} '{url}' \\\n"
        f"  -H '{API_KEY_HEADER}: {PLACEHOLDER_KEY}' \\\n"
        "  -H 'Accept: application/json'"
    )


def _python(method: str, url: str) -> str:
    return (
        "import httpx\n\n"
        f'response = httpx.request(\n    "{method}",\n    "{url}",\n'
        f'    headers={{"{API_KEY_HEADER}": "{PLACEHOLDER_KEY}"}},\n'
        "    timeout=30.0,\n)\n"
        "response.raise_for_status()\n"
        "print(response.json())"
    )


def _typescript(method: str, url: str) -> str:
    return (
        f'const response = await fetch("{url}", {{\n'
        f'  method: "{method}",\n'
        f'  headers: {{ "{API_KEY_HEADER}": "{PLACEHOLDER_KEY}" }},\n'
        "});\n"
        "if (!response.ok) throw new Error(`Decile API: ${response.status}`);\n"
        "console.log(await response.json());"
    )


def _example_url(base_url: str, path: str) -> str:
    """Substitute a plausible value for every path parameter, so the sample is runnable."""
    filled = path.replace("{public_id}", "8f2c1d4e9a7b").replace("{symbol}", "CUPID")
    return f"{base_url.rstrip('/')}{filled}"


def code_samples(base_url: str, method: str, path: str) -> list[dict[str, str]]:
    """The three samples Prompt 20 §5 names, in the order a reader is most likely to want them."""
    url = _example_url(base_url, path)
    verb = method.upper()
    return [
        {"lang": "curl", "label": "curl", "source": _curl(verb, url)},
        {"lang": "Python", "label": "Python (httpx)", "source": _python(verb, url)},
        {"lang": "JavaScript", "label": "TypeScript (fetch)", "source": _typescript(verb, url)},
    ]


def public_document(document: Mapping[str, object], *, base_url: str) -> dict[str, object]:
    """Filter the service's OpenAPI document down to the public API, and attach code samples.

    Component schemas are carried over wholesale rather than pruned. An over-broad ``components``
    block costs a reader nothing (Redoc renders only what is referenced) and pruning it correctly
    means walking every ``$ref`` transitively — a piece of machinery whose failure mode is a
    reference page with holes in it.
    """
    paths = document.get("paths")
    selected: dict[str, object] = {}
    if isinstance(paths, Mapping):
        for path, item in paths.items():
            if not isinstance(path, str) or not path.startswith(PUBLIC_API_PREFIX):
                continue
            if not isinstance(item, Mapping):  # pragma: no cover - OpenAPI shape
                continue
            operations: dict[str, object] = {}
            for method, operation in item.items():
                if isinstance(operation, Mapping) and method.lower() in {"get", "post"}:
                    enriched = dict(operation)
                    enriched[CODE_SAMPLE_KEY] = code_samples(base_url, method, path)
                    operations[method] = enriched
                else:  # pragma: no cover - parameters and other non-operation members
                    operations[method] = operation
            selected[path] = operations

    components = document.get("components")
    return {
        "openapi": document.get("openapi", "3.1.0"),
        "info": {
            "title": DOC_TITLE,
            "version": PUBLIC_API_VERSION,
            "description": DOC_DESCRIPTION,
        },
        "servers": [{"url": base_url.rstrip("/")}],
        "paths": selected,
        "components": components if isinstance(components, Mapping) else {},
    }


def redoc_page(*, spec_url: str, script_url: str) -> str:
    """The reference page. One script tag, one element, no build step.

    ``spec_url`` and ``script_url`` are escaped for an HTML attribute even though both come from
    our own configuration: a settings value that reaches a page unescaped is the shape of the bug,
    and the escaping costs nothing.
    """
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{html.escape(DOC_TITLE)}</title>
    <meta name="robots" content="noindex" />
    <style>body {{ margin: 0; padding: 0; }}</style>
  </head>
  <body>
    <redoc spec-url="{html.escape(spec_url, quote=True)}" hide-download-button></redoc>
    <script src="{html.escape(script_url, quote=True)}"></script>
  </body>
</html>
"""


def render_document(document: Mapping[str, object]) -> str:
    """Byte-stable JSON, for the same reason ``decile_api.openapi`` sorts its keys."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
