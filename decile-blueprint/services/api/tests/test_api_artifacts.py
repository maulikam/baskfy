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
    # M47: the desk's latest plan shaped as a Kite Publisher basket. A GET, and it stays a GET —
    # it returns a payload the *browser* posts to Kite, where the user reviews and confirms in
    # their own session. `test_baskets_readonly.py` asserts that structurally.
    "/baskets/plan/kite": {"get"},
    # M53: a curated basket at a chosen amount, as a Kite basket. Weights become quantities via
    # `build_invest_plan` — the same pure function `POST /cb/plans/invest` calls — so the hand-off
    # cannot disagree in rupees with the plan preview the user was just shown.
    "/explore/{slug}/kite": {"get"},
    # M26 (MERGE-PROMPTS.md), not docs/07, for the same reason: the desk's own console pages,
    # moved onto the web app. Every one is GET and only GET -- `test_desk_readonly.py` fails if a
    # mutating verb appears, and asserts the module cannot reach a broker at all.
    "/desk/performance": {"get"},
    "/desk/holdings": {"get"},
    "/desk/tradebook": {"get"},
    "/desk/regime": {"get"},
    "/desk/reconcile": {"get"},
    # M34, not docs/07: sleeves and the allocation that follows. Amounts and weights only --
    # `test_sleeves_are_not_orders.py` asserts no share count or price can appear.
    "/portfolios/{portfolio_id}/sleeves": {"get", "put"},
    "/portfolios/{portfolio_id}/allocation": {"get"},
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
    # M46: the federated ⌘K search (`baskfynavrefactorreport` §F11). Documented here because a
    # route absent from this list is a surface nobody agreed to — which is the assertion below.
    "/search": {"get"},
    "/listings": {"get"},
    "/indices/dashboard": {"get"},
    "/market-health": {"get"},
    "/market-health/history": {"get"},
    # docs/07 §"Account & billing" — the auth half is Prompt 12's, the billing half Prompt 13's.
    #
    # M46 removed eight of these in one change — `register`, `login`, `request-otp`, `verify-otp`,
    # `verify-email`, `forgot-password`, `reset-password` and `me/change-password` — when Google
    # sign-in replaced the email/password funnel (`docs/DECISIONS-MERGE.md` M46). The list below
    # is the entire authenticated surface now, and the assertion under it is what makes that a
    # fact rather than a claim: a route absent from this list is a surface nobody agreed to, and
    # a route still *in* the API after being deleted from here fails just as loudly.
    "/auth/google": {"post"},
    "/auth/refresh": {"post"},
    "/auth/logout": {"post"},
    "/me": {"get", "patch", "delete"},
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
    # PORTFOLIO_REDESIGN.md §6 and §7, not docs/07 -- which predates the redesign and describes
    # a portfolio tracker with no allocation ledger under it. Seven reads and two writes, all
    # authenticated and all user-scoped; `baskfy_api.routers.portfolio_overview` argues for each.
    #
    # There is no execute route here and there will not be one (§9): a rebalance produces an
    # order plan the user takes to their broker, and
    # `test_baskets_readonly.py::test_the_whole_api_has_no_order_route` covers this router the
    # way it covers every other. The two writes are bookkeeping: `POST .../resolve` answers
    # §4.3's reconciliation question, and `POST /portfolio` creates a grouping. Both move an
    # allocation and neither reaches a broker.
    # §6.6 and §6.7 -- the onboarding half. `suggestions` is a read that serves
    # `baskfy_core.grouping_suggestions.suggest_groupings`; `POST /portfolio` is the redesign's
    # create, which files whole holdings (§4.2) into a portfolio whose kind (§4.1) and source (§3)
    # the caller states. Neither reaches a broker: creating a grouping is filing, not trading, and
    # `test_baskets_readonly.py::test_the_whole_api_has_no_order_route` covers both.
    "/portfolio": {"post"},
    "/portfolio/suggestions": {"get"},
    "/portfolio/overview": {"get"},
    "/portfolio/holdings": {"get"},
    "/portfolio/activity": {"get"},
    "/portfolio/reconciliation": {"get"},
    "/portfolio/reconciliation/{item_id}/resolve": {"post"},
    "/portfolio/{portfolio_id}": {"get"},
    "/portfolio/{portfolio_id}/nav": {"get"},
    # 11 Sep 2026: file more shares into a portfolio that already exists. Distinct from
    # `PUT /portfolios/{id}/holdings` above, which REPLACES a legacy portfolio's list from a CSV;
    # this one moves quantities between the redesign's groups and never removes a name.
    "/portfolio/{portfolio_id}/holdings": {"post"},
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
    # Leaf 3.1's resync button. Two methods on one path, deliberately: the GET is a dry
    # inspection that changes nothing and the POST is the repair, because a flag that turns a
    # read into a write is one typo away from a repair nobody asked for.
    "/admin/resync": {"get", "post"},
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
    # Tree-3 / M41 (MERGE-PROMPTS.md), not docs/07 -- the broker connect catalog and the OAuth
    # round trip for the operator's OWN account (D3 posture B; live authorize needs
    # `BROKER_OAUTH_REVIEW.signed_off`). None of these is an order path: `connect` starts a
    # redirect, `callback` exchanges a request token into an encrypted store, `sync-holdings`
    # reads. `test_baskets_readonly.py::test_the_whole_api_has_no_order_route` covers them.
    "/brokers": {"get"},
    "/brokers/{broker_id}": {"get"},
    "/brokers/{broker_id}/connect": {"post"},
    # The redirect target Kite returns to; it takes `request_token` + a state this app issued.
    "/brokers/callback": {"get"},
    "/brokers/{broker_id}/sync-holdings": {"post"},
    # SC2 (docs/smallcase/06-module-plan.md), not docs/07, which predates the curated-basket
    # product. The `/explore` catalog is Track A and read-only in every branch.
    "/explore": {"get"},
    "/explore/{slug}": {"get"},
    "/explore/managers": {"get"},
    "/explore/managers/{slug}": {"get"},
    "/explore/collections": {"get"},
    "/explore/collections/{slug}": {"get"},
    # SC6 (9 Sep 2026, `496b7b4`): the basket's constituents as of its newest published version.
    # Read-only, same visibility predicate as the card. **Not this run's route** — VB8 found it
    # missing from this table, which had made `test_nothing_undocumented_is_exposed` red since
    # SC6 shipped, and added it rather than leaving a red test in the tree.
    "/explore/{slug}/constituents": {"get"},
    # SC2 watchlist. User-scoped writes to the sole tenant's own watchlist -- a bookmark, not an
    # order; `test_explore_no_orders.py` fails if an order-shaped verb ever appears here.
    "/watchlist": {"get", "post"},
    "/watchlist/{slug}": {"delete"},
    # SC8 (leaf 2.2): create a PRIVATE STOCK basket + its GENESIS version for the sole tenant.
    # A catalog write with no broker path -- argued for in `test_baskets_readonly.py`'s
    # `DELIBERATE_MUTATING_BASKET_ROUTES`, which is where the order gate is decided.
    "/cb/baskets": {"post"},
    # SB1: save a screen as a basket. A catalog write like `/cb/baskets`, and inside the same
    # order gate -- `test_baskets_readonly.py`'s `DELIBERATE_MUTATING_BASKET_ROUTES`. Listed here
    # because it is served: the route runs the named screen server-side and stores what that
    # screen returned, so `source = 'SCREEN'` is a fact rather than a caller's claim.
    "/cb/baskets/from-screen": {"post"},
    # T8.1: mark-as-invested records a broker book the user already traded. GET list/detail
    # and the accrued fee ledger. No execute — batch status on mark is always PLANNED.
    "/cb/investments": {"get"},
    "/cb/investments/mark": {"post"},
    "/cb/investments/{investment_id}": {"get"},
    # Tree-5 leaf B3: an investment may be filed inside a portfolio, or sit outside every
    # portfolio. Records the filing only -- there is no order path on either verb.
    "/cb/investments/{investment_id}/portfolio": {"put", "delete"},
    # Tree-3 managers: a manager identity a person can hold, apply for, be reviewed against,
    # and publish under. Publishing sets `cb_basket.visibility` and places nothing;
    # `/revenue-share` is a dark Track-B surface that 404s while fee collection is off.
    "/managers/me": {"get", "post"},
    "/managers/{slug}": {"patch"},
    "/managers/me/baskets/{basket_slug}/publish": {"post", "delete"},
    "/managers/me/revenue-share": {"get"},
    "/cb/fees": {"get"},
    # T8.4: SIP reminder plan on an investment. REMINDER only; AUTO is refused in-router.
    "/cb/investments/{investment_id}/sip": {"get", "post"},
    # T8.5: costs after accrued (uncollected) fees. GET only — collection stays off.
    "/cb/investments/{investment_id}/costs": {"get"},
    # T8.6: drift scan + ledger rebase. No order path.
    "/cb/investments/{investment_id}/drift/scan": {"post"},
    "/cb/investments/{investment_id}/drift/fix": {"post"},
    # SC Tree 4: manage constituents on an existing investment (CUSTOMIZE batch, no order path).
    "/cb/investments/{investment_id}/customize": {"post"},
    # SC3: desk-shaped plan PREVIEWS. Producing a plan is not placing an order
    # (docs/smallcase/02, Track A); outside NSE hours they return the closed-market payload.
    # The desk console remains the only thing that can turn a plan into orders.
    "/cb/plans/invest": {"post"},
    "/cb/plans/apply": {"post"},
    "/cb/plans/exit": {"post"},
    # SC9 engagement slots. `dismiss` / `resolve` stamp a timestamp on the user's own pending
    # action row; neither reaches the order path.
    "/cb/pending-actions": {"get"},
    "/cb/pending-actions/{action_id}/dismiss": {"post"},
    "/cb/pending-actions/{action_id}/resolve": {"post"},
    "/cb/updates": {"get"},
    # SC9 trending. Read-only ranked lists; the aggregates are platform-wide and the pure
    # layer's MIN_POPULATION floor is what keeps a thin one from being published at all.
    "/cb/trending": {"get"},
    # SC10 Track-B surfaces (docs/smallcase/02 §"Track B -- build dark"). Mounted so this
    # document and the UI can name them, which is the point of listing them here: a route that
    # is served and not written down is a surface nobody agreed to.
    #
    # The first two only *report* flag state and are always readable -- while the flags are off
    # they say so, which is how the UI knows to render everything as Free Access.
    "/cb/track-b/flags": {"get"},
    "/cb/track-b/free-access": {"get"},
    # The four below are flag-gated stubs. Each calls its `require_*_enabled` guard first and
    # 404s while the flag is false, and all three flags default to false; when enabled they
    # return `EnabledStubOut` and do nothing else. `POST /cb/fees/collect` in particular
    # collects no money -- there is no payment call site anywhere in the tree -- so Track C
    # ("collecting money") is not breached by the path existing. `test_track_b_gates.py`
    # asserts the 404s and the defaults; this entry only writes down that the paths exist.
    "/cb/paywall": {"get"},
    "/cb/paywall/checkout": {"post"},
    "/cb/public-signup": {"post"},
    "/cb/fees/collect": {"post"},
    # SW4/SW5 (docs/swing/05 §2, docs/swing/02 Track A): the swing book, as a surface. Every
    # route is a GET except the four Track A permits because they "change no money" — the
    # settings form (which cannot name the exposure rung, SW4.1), watching a name, annotating it
    # and dismissing it. `test_swing_readonly.py` asserts that structurally, and asserts the
    # surface names no broker. Nothing here is `/swing/execute`: that lives in the desk console
    # (SW7), behind `BASKFY_SWING_EXECUTION_ENABLED`, and never on the web app.
    "/swing/setups": {"get"},
    "/swing/setups/{instrument_id}/bars": {"get"},
    "/swing/market": {"get"},
    "/swing/sectors": {"get"},
    "/swing/config": {"get", "patch"},
    "/swing/watch": {"get", "post"},
    "/swing/watch/{watch_id}": {"patch", "delete"},
    "/swing/positions": {"get"},
    # SW8: the journal — the book's results in R, real and simulated in separate cards, the
    # ladder rung `swing-eod` wrote back, and the session count against the 20-session gate.
    # A read of closed positions; it writes nothing and names no broker.
    "/swing/journal": {"get"},
    # SW14: what the monitor raised in a session — `sw_signal`, append-only, every verdict. The
    # watchlist page shows yesterday's rows under a name. A read; the plan line a signal may
    # have become is named by id and not reachable from here.
    "/swing/signals": {"get"},
    # SW15 (docs/swing/DECISIONS-SW SW15.1): "Scan now" — a money-free write that queues a
    # detection run, and the run's state. During the session the worker detects on a bar built
    # from live Kite quotes and labels every row provisional; `/swing/setups` carries the label.
    "/swing/scan": {"post"},
    "/swing/scan/{run_id}": {"get"},
    # VB8 (docs/vbt/05 §2, docs/vbt/02 Track C §4): the volume-breakout sleeve, as a surface.
    # Every route is a GET except the settings form, which writes four numbers and cannot name
    # the DRY_RUN counter or the first-live countdown. `test_vbt_readonly.py` asserts that
    # structurally and asserts the surface names no broker. There is no `/vbt/execute` here and
    # there is not going to be one: a line becomes an order in the desk console, on a click, and
    # nowhere else. This sleeve has no auto-execute flag at all — non-negotiable #1's exception
    # belongs to the swing book alone.
    "/vbt/today": {"get"},
    "/vbt/today/{instrument_id}/bars": {"get"},
    "/vbt/breadth": {"get"},
    "/vbt/book": {"get"},
    "/vbt/backtest": {"get"},
    "/vbt/config": {"get", "patch"},
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
