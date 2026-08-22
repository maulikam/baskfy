"""The generated artefacts stay in step with the app — Prompt 7 deliverable 7.

    "openapi.json emitted at build time into packages/api-client, and a CI step that regenerates
     the TypeScript client and fails if the checked-in client is stale."

The CI job in ``.github/workflows/ci.yml`` runs the real regeneration (it has node). These tests
are the part that can run anywhere: that the checked-in ``openapi.json`` is the one this code
produces, that it describes what docs/07 says it should, and that the checked-in TypeScript was
generated from it rather than from an older one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest

from baskfy_api.openapi import default_output, render
from baskfy_api.problems import STATUS_FOR
from baskfy_api.settings import API_PREFIX

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
GENERATED_TS: Final = REPO_ROOT / "packages" / "api-client" / "src" / "generated" / "schema.ts"

#: docs/07 §Metadata, §Screens, §Instruments and §"Market data surfaces" — the surface Prompts 7,
#: 10 and 11 are responsible for, verbatim, plus billing (Prompt 13) and portfolios (Prompt 14).
#: Backtests are Prompt 15's, and a route with no implementation would be a documented lie.
EXPECTED_PATHS: Final[dict[str, set[str]]] = {
    "/meta/factors": {"get"},
    "/meta/columns": {"get"},
    "/meta/universes": {"get"},
    "/meta/trading-days": {"get"},
    "/meta/status": {"get"},
    "/screens": {"get", "post"},
    # M22 (MERGE-PROMPTS.md), not docs/07 — which predates the merge and describes a screener
    # with no basket to show. Both are GET and only GET: execution stays in the desk console, and
    # `test_baskets_readonly.py` fails if a mutating verb ever appears on either.
    "/baskets": {"get"},
    "/baskets/plan": {"get"},
    "/screens/{public_id}": {"get", "patch", "delete"},
    "/screens/{public_id}/duplicate": {"post"},
    "/screens/{public_id}/run": {"post"},
    "/screens/preview": {"post"},
    "/screens/{public_id}/csv": {"get"},
    "/screens/{public_id}/runs": {"get"},
    "/instruments": {"get"},
    "/instruments/{symbol}": {"get"},
    "/instruments/{symbol}/history": {"get"},
    "/instruments/{symbol}/corporate-actions": {"get"},
    "/instruments/{symbol}/rank-history": {"get"},
    "/listings": {"get"},
    "/indices/dashboard": {"get"},
    "/market-health": {"get"},
    "/market-health/history": {"get"},
    # docs/07 §"Account & billing" — the auth half is Prompt 12's, the billing half Prompt 13's.
    "/auth/register": {"post"},
    "/auth/login": {"post"},
    "/auth/refresh": {"post"},
    "/auth/logout": {"post"},
    "/auth/request-otp": {"post"},
    "/auth/verify-otp": {"post"},
    "/auth/verify-email": {"post"},
    "/auth/forgot-password": {"post"},
    "/auth/reset-password": {"post"},
    "/me": {"get", "patch", "delete"},
    "/me/change-password": {"post"},
    "/me/export": {"get"},
    "/me/restore": {"post"},
    # docs/07 §"Account & billing", the billing half (Prompt 13).
    "/plans": {"get"},
    "/checkout/session": {"post"},
    "/webhooks/razorpay": {"post"},
    "/invoices": {"get"},
    # `{invoice_number:path}` because the number contains slashes (`DCL/2026-27/000001`); FastAPI
    # renders the converter into the OpenAPI path template.
    "/invoices/{invoice_number}/pdf": {"get"},
    # docs/07 §"Portfolios & rebalance" (Prompt 14). The four paths docs/07 does not list —
    # `sample-csv`, the rename/delete pair on `{portfolio_id}`, and the two `rebalances` reads —
    # are Prompt 14 deliverables 1, 3 and 4; each is argued for in
    # `baskfy_api.routers.portfolios` and recorded in docs/DECISIONS.md §14.
    "/portfolios": {"get", "post"},
    "/portfolios/import-csv": {"post"},
    "/portfolios/sample-csv": {"get"},
    "/portfolios/{portfolio_id}": {"get", "patch", "delete"},
    "/portfolios/{portfolio_id}/holdings": {"put"},
    "/portfolios/{portfolio_id}/rebalance": {"post"},
    "/portfolios/{portfolio_id}/rebalances": {"get"},
    "/portfolios/{portfolio_id}/rebalances/{rebalance_id}": {"get"},
    # docs/07 §Backtests (Prompt 15). The four paths docs/07 does not list — the collection read,
    # `holdings`, the SSE stream and the download the signed `export` link redeems against — are
    # Prompt 15 deliverables 4, 5 and 6; each is argued for in `baskfy_api.routers.backtests` and
    # recorded in docs/DECISIONS.md §15.
    "/backtests": {"get", "post"},
    "/backtests/{public_id}": {"get", "delete"},
    "/backtests/{public_id}/trades": {"get"},
    "/backtests/{public_id}/holdings": {"get"},
    "/backtests/{public_id}/export": {"get"},
    "/backtests/{public_id}/download/{artefact}": {"get"},
    "/backtests/{public_id}/events": {"get"},
    # Prompt 17 deliverable 4. docs/07 does not describe `/admin`; docs/09 §Observability does,
    # in one line: "`pipeline_run_step` is the operator UI; expose it at `/admin/pipeline` behind
    # staff auth." Listed here for the same reason every other path is — a route that is served
    # and not written down is a surface nobody agreed to. Every one of them is staff-gated.
    "/admin/pipeline/runs": {"get"},
    "/admin/pipeline/runs/{run_id}": {"get"},
    "/admin/pipeline/runs/{trade_date}/rerun": {"post"},
    "/admin/instruments/{symbol}/reprocess": {"post"},
    "/admin/data-versions": {"get"},
    "/admin/providers": {"get"},
    "/admin/users": {"get"},
    "/admin/users/{public_id}": {"get"},
    "/admin/users/{public_id}/entitlements": {"put"},
    "/admin/users/{public_id}/entitlements/{feature}": {"delete"},
    # Prompt 18 deliverable 2's contact form. docs/07 does not describe it either; a form has to
    # post somewhere and the mail credential belongs behind the service that already holds it
    # (`baskfy_api.routers.support`, `docs/DECISIONS.md` §18.5).
    "/support": {"post"},
    "/admin/actions": {"get"},
    # Prompt 20. docs/07 mentions `X-API-Key` in its header section and describes no endpoint that
    # issues one, and it describes neither alerts nor outbound webhooks at all. Listed here for the
    # same reason `/admin` and `/support` are: a route that is served and not written down is a
    # surface nobody agreed to. Each is argued for in its router and in docs/DECISIONS.md §20.
    "/keys": {"get", "post"},
    "/keys/{public_id}": {"delete"},
    "/keys/{public_id}/rotate": {"post"},
    "/keys/{public_id}/revoke": {"post"},
    "/keys/{public_id}/usage": {"get"},
    "/alerts": {"get", "post"},
    "/alerts/{public_id}": {"patch", "delete"},
    "/alerts/{public_id}/deliveries": {"get"},
    "/alerts/unsubscribe": {"post"},
    "/webhook-endpoints": {"get", "post"},
    "/webhook-endpoints/{public_id}": {"patch", "delete"},
    "/webhook-endpoints/{public_id}/rotate-secret": {"post"},
    "/webhook-endpoints/{public_id}/deliveries": {"get"},
    "/admin/public-api": {"get"},
    # NOT LISTED, and deliberately: nothing under `/api/public/v1`. The public tier's router is
    # not mounted while the data-redistribution review docs/11 §Compliance requires is
    # outstanding, so it is absent from this document by construction —
    # `test_public_api_flag.py` asserts that positively.
}


@pytest.fixture(scope="module")
def document() -> dict[str, object]:
    body: dict[str, object] = json.loads(render())
    return body


def paths(document: dict[str, object]) -> dict[str, dict[str, object]]:
    raw = document["paths"]
    assert isinstance(raw, dict)
    return raw


class TestTheCheckedInDocument:
    def test_openapi_json_is_committed(self) -> None:
        assert default_output().is_file(), "run `make openapi`"

    def test_openapi_json_is_current(self) -> None:
        """A stale spec means a stale client, and the client is the web app's only contract."""
        assert default_output().read_text(encoding="utf-8") == render(), (
            "packages/api-client/openapi.json is stale; run `make openapi`"
        )

    def test_it_is_byte_stable_across_renders(self) -> None:
        """Sorted keys, so an unrelated edit does not produce a diff in the generated client."""
        assert render() == render()


class TestTheDocumentedSurface:
    def test_every_documented_path_is_present(self, document: dict[str, object]) -> None:
        served = paths(document)
        for path, methods in EXPECTED_PATHS.items():
            full = f"{API_PREFIX}{path}"
            assert full in served, f"docs/07 documents {path}"
            assert methods <= set(served[full]), f"{path}: expected {methods}"

    def test_nothing_undocumented_is_exposed(self, document: dict[str, object]) -> None:
        """A route added without a docs/07 entry is a contract change nobody agreed to."""
        expected = {f"{API_PREFIX}{path}" for path in EXPECTED_PATHS}
        assert set(paths(document)) - expected == set()

    def test_every_error_in_the_catalogue_is_documented(self, document: dict[str, object]) -> None:
        """docs/07 §"Error catalogue" — so the generated client types the failures too."""
        statuses = {str(status) for status in STATUS_FOR.values()}
        for path, operations in paths(document).items():
            for method, operation in operations.items():
                if method not in {"get", "post", "patch", "delete"}:
                    continue
                assert isinstance(operation, dict)
                documented = set(operation["responses"])
                assert statuses <= documented, (
                    f"{method.upper()} {path} is missing {statuses - documented}"
                )

    def test_errors_are_advertised_as_problem_json_only(self, document: dict[str, object]) -> None:
        operation = paths(document)[f"{API_PREFIX}/meta/factors"]["get"]
        assert isinstance(operation, dict)
        for status in ("400", "401", "404", "409", "422", "429", "503"):
            content = operation["responses"][status]["content"]
            assert set(content) == {"application/problem+json"}, (status, content)

    def test_the_problem_schema_is_resolvable(self, document: dict[str, object]) -> None:
        """An unresolvable ``$ref`` makes openapi-typescript emit ``unknown``."""
        components = document["components"]
        assert isinstance(components, dict)
        assert "ProblemOut" in components["schemas"]

    def test_operation_ids_are_unique_and_camel_case(self, document: dict[str, object]) -> None:
        """They become the method names on the generated client."""
        ids = [
            operation["operationId"]
            for operations in paths(document).values()
            for method, operation in operations.items()
            if method in {"get", "post", "patch", "delete"} and isinstance(operation, dict)
        ]
        assert len(ids) == len(set(ids))
        assert all("_" not in name and name[0].islower() for name in ids), ids


class TestTheGeneratedClient:
    def test_it_is_committed(self) -> None:
        assert GENERATED_TS.is_file(), "run `make client`"

    def test_it_was_generated_from_this_document(self, document: dict[str, object]) -> None:
        """Cheap staleness check that needs no node: every path and operation id must appear.

        The authoritative check is the CI job, which regenerates and diffs. This catches the
        common case — a route added and the client forgotten — in the Python suite, where it is
        noticed in seconds rather than in review.
        """
        source = GENERATED_TS.read_text(encoding="utf-8")
        for path in paths(document):
            assert f'"{path}"' in source, f"{path} is missing from the generated client"
        for operations in paths(document).values():
            for method, operation in operations.items():
                if method in {"get", "post", "patch", "delete"} and isinstance(operation, dict):
                    assert operation["operationId"] in source

    def test_it_says_not_to_edit_it(self) -> None:
        head = GENERATED_TS.read_text(encoding="utf-8")[:400]
        assert "auto-generated" in head
