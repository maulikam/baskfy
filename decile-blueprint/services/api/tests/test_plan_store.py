"""Non-negotiable #1, end to end, with an injected clock and no route.

    "Never auto-execute. Orders fire only from ``POST /execute`` with ``confirm=true`` and the
     ``plan_id`` issued by ``/analyze``; plans expire in 30 minutes."

Each clause of that sentence has a test here, and each is asserted against the *spec* rather
than against what ``kite-momentum-rebalancer/app/main.py:519-524`` happened to compile to: the
window is thirty minutes because CLAUDE.md says thirty minutes, and the boundary cases are one
second either side of it.

No ``sleep``, anywhere. Every method that needs the time is handed it, which is the whole reason
the predicate lives in ``packages/core`` and the store lives here.

THIS FILE ALSO PROVES THE LEAF'S CONSTRAINT: it drives an ``OrderGateway`` in ``DRY_RUN`` to show
that a re-presented plan is idempotent, and there is no route anywhere in the chain. The store
authorises; it does not fire.
"""

from __future__ import annotations

import ast
import asyncio
import datetime as dt
import inspect
import json
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest
from baskfy_execution import (
    OrderGateway,
    ProductGates,
    RiskManager,
    TenantIds,
    parse_client_id,
)

from baskfy_api import plan_store as plan_store_module
from baskfy_api.plan_store import (
    CONFIRMATION_TOKEN,
    PLAN_ID_PREFIX,
    PlanExpired,
    PlanNotConfirmed,
    PlanStore,
    StoredPlan,
    SymbolNotInPlan,
    UnknownPlan,
)
from baskfy_core.curated_plans import PLAN_TTL, DeskPlan, build_invest_plan


def _code_only(module: ModuleType) -> str:
    """The module's source with every comment and docstring removed.

    A prose scan over raw source is a trap: this file's own explanation of why there is no
    ``datetime.now()`` in it contains the string ``datetime.now(``. Unparsing the AST with the
    docstrings taken out leaves only what actually executes, so the assertion measures the code
    and not the paragraph above it.
    """
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            body[0] = ast.Pass()
    return ast.unparse(ast.fix_missing_locations(tree))


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
ISSUED = dt.datetime(2026, 8, 31, 10, 30, tzinfo=IST)

OWNER = TenantIds(user_id=1, broker_account_id=10)
STRANGER = TenantIds(user_id=2, broker_account_id=20)
SAME_USER_OTHER_ACCOUNT = TenantIds(user_id=1, broker_account_id=99)


def _at(*, minutes: int = 0, seconds: int = 0) -> dt.datetime:
    return ISSUED + dt.timedelta(minutes=minutes, seconds=seconds)


def _plan(now: dt.datetime = ISSUED) -> DeskPlan:
    """A two-leg BUY, built by the same pure function the preview endpoints call."""
    return build_invest_plan(
        target_weights={"RELIANCE": Decimal("0.6"), "INFY": Decimal("0.4")},
        prices={"RELIANCE": Decimal("100"), "INFY": Decimal("50")},
        amount=Decimal("100000"),
        now=now,
    )


@pytest.fixture
def store() -> PlanStore:
    return PlanStore()


# =====================================================================================
# Issue
# =====================================================================================
class TestIssuing:
    def test_a_plan_gets_an_id_and_the_deadline_the_preview_promised(
        self, store: PlanStore
    ) -> None:
        plan = _plan()
        stored = store.issue(plan, tenant=OWNER, now=ISSUED)
        assert stored.plan_id.startswith(PLAN_ID_PREFIX)
        assert stored.issued_at == ISSUED
        assert stored.expires_at == plan["expires_at_hint"] == ISSUED + PLAN_TTL

    def test_two_plans_never_share_an_id(self, store: PlanStore) -> None:
        first = store.issue(_plan(), tenant=OWNER, now=ISSUED)
        second = store.issue(_plan(), tenant=OWNER, now=ISSUED)
        assert first.plan_id != second.plan_id

    def test_the_deadline_follows_the_plan_not_the_moment_it_was_stored(
        self, store: PlanStore
    ) -> None:
        """A plan built at 10:30 and stored at 10:50 still dies at 11:00.

        Re-stamping it with the store's clock would extend a plan that had been sitting in a
        browser tab, so the countdown the investor watched and the deadline that refuses them
        would be twenty minutes apart.
        """
        stored = store.issue(_plan(now=ISSUED), tenant=OWNER, now=_at(minutes=20))
        assert stored.expires_at == ISSUED + PLAN_TTL

    def test_a_plan_that_is_already_dead_is_never_stored(self, store: PlanStore) -> None:
        with pytest.raises(PlanExpired):
            store.issue(_plan(now=ISSUED), tenant=OWNER, now=_at(minutes=31))
        assert len(store) == 0

    def test_mutating_the_plan_afterwards_cannot_change_what_was_authorised(
        self, store: PlanStore
    ) -> None:
        """Otherwise one ``plan_id`` and one ``client_id`` could authorise a different order —
        the exact substitution a plan id exists to prevent."""
        plan = _plan()
        stored = store.issue(plan, tenant=OWNER, now=ISSUED)
        original = stored.legs[0]["quantity"]
        plan["legs"][0]["quantity"] = 999_999
        stored.legs[0]["quantity"] = 888_888
        again = store.lookup(stored.plan_id, tenant=OWNER, now=ISSUED)
        assert again.legs[0]["quantity"] == original


# =====================================================================================
# Look up: unknown, and whose
# =====================================================================================
class TestLookup:
    def test_an_issued_plan_comes_back(self, store: PlanStore) -> None:
        stored = store.issue(_plan(), tenant=OWNER, now=ISSUED)
        assert store.lookup(stored.plan_id, tenant=OWNER, now=_at(minutes=1)) is stored

    def test_an_id_that_was_never_issued_is_refused(self, store: PlanStore) -> None:
        with pytest.raises(UnknownPlan):
            store.lookup("plan-does-not-exist", tenant=OWNER, now=ISSUED)

    def test_an_empty_plan_id_is_refused_like_any_other_unknown_one(self, store: PlanStore) -> None:
        with pytest.raises(UnknownPlan):
            store.lookup("", tenant=OWNER, now=ISSUED)

    def test_another_tenant_s_plan_reads_as_unknown(self, store: PlanStore) -> None:
        """Not "forbidden": telling a caller the id exists makes the id space enumerable."""
        stored = store.issue(_plan(), tenant=OWNER, now=ISSUED)
        with pytest.raises(UnknownPlan):
            store.lookup(stored.plan_id, tenant=STRANGER, now=ISSUED)

    def test_the_same_user_on_a_different_broker_account_is_a_different_tenant(
        self, store: PlanStore
    ) -> None:
        """Law #2's multi-tenant clause is the *pair*, not the user id."""
        stored = store.issue(_plan(), tenant=OWNER, now=ISSUED)
        with pytest.raises(UnknownPlan):
            store.lookup(stored.plan_id, tenant=SAME_USER_OTHER_ACCOUNT, now=ISSUED)


# =====================================================================================
# The thirty-minute window
# =====================================================================================
class TestTheThirtyMinuteWindow:
    def test_at_twenty_nine_fifty_nine_the_plan_is_live(self, store: PlanStore) -> None:
        stored = store.issue(_plan(), tenant=OWNER, now=ISSUED)
        alive = store.lookup(stored.plan_id, tenant=OWNER, now=_at(minutes=29, seconds=59))
        assert alive.plan_id == stored.plan_id

    def test_at_thirty_oh_one_the_plan_is_gone(self, store: PlanStore) -> None:
        stored = store.issue(_plan(), tenant=OWNER, now=ISSUED)
        with pytest.raises(PlanExpired):
            store.lookup(stored.plan_id, tenant=OWNER, now=_at(minutes=30, seconds=1))

    def test_at_exactly_thirty_minutes_the_plan_is_gone(self, store: PlanStore) -> None:
        stored = store.issue(_plan(), tenant=OWNER, now=ISSUED)
        with pytest.raises(PlanExpired):
            store.lookup(stored.plan_id, tenant=OWNER, now=_at(minutes=30))

    def test_an_expired_plan_gives_the_same_answer_every_time_it_is_asked(
        self, store: PlanStore
    ) -> None:
        """A client that retries a timed-out call must not be told two different stories.

        Dropping the record while answering would make the first attempt 410 and every one
        after it 404, and the second answer is the less true one.
        """
        stored = store.issue(_plan(), tenant=OWNER, now=ISSUED)
        for _ in range(3):
            with pytest.raises(PlanExpired):
                store.lookup(stored.plan_id, tenant=OWNER, now=_at(minutes=31))

    def test_the_refusal_says_when_it_expired(self, store: PlanStore) -> None:
        """A 410 that does not name the deadline sends the operator to the logs."""
        stored = store.issue(_plan(), tenant=OWNER, now=ISSUED)
        with pytest.raises(PlanExpired, match="stale"):
            store.lookup(stored.plan_id, tenant=OWNER, now=_at(minutes=45))

    def test_expiry_is_the_core_predicate_and_not_a_second_copy_of_it(self) -> None:
        """The number thirty appears once in this repo. A store with its own arithmetic could
        drift from ``PLAN_TTL`` and no test of either half would notice."""
        source = _code_only(plan_store_module)
        assert "plan_is_expired" in source
        assert "1800" not in source
        assert "minutes=30" not in source

    def test_expired_plans_do_not_accumulate(self, store: PlanStore) -> None:
        """The desk's ``PLANS`` dict grew for the life of the process."""
        for _ in range(3):
            store.issue(_plan(), tenant=OWNER, now=ISSUED)
        assert len(store) == 3
        store.issue(_plan(now=_at(minutes=40)), tenant=OWNER, now=_at(minutes=40))
        assert len(store) == 1


# =====================================================================================
# confirm=true
# =====================================================================================
class TestConfirmation:
    def test_the_happy_path_needs_the_literal_word_true(self, store: PlanStore) -> None:
        stored = store.issue(_plan(), tenant=OWNER, now=ISSUED)
        got = store.authorize(
            stored.plan_id, confirm=CONFIRMATION_TOKEN, tenant=OWNER, now=_at(minutes=1)
        )
        assert got.plan_id == stored.plan_id

    @pytest.mark.parametrize(
        "confirm",
        ["", "false", "True", "TRUE", "yes", "1", "true ", "on"],
    )
    def test_anything_else_is_refused(self, store: PlanStore, confirm: str) -> None:
        """Including ``"True"`` and ``"1"``. A near-miss that executes is the failure mode this
        gate exists for, and "close enough" is how an accidental POST becomes an order."""
        stored = store.issue(_plan(), tenant=OWNER, now=ISSUED)
        with pytest.raises(PlanNotConfirmed):
            store.authorize(stored.plan_id, confirm=confirm, tenant=OWNER, now=_at(minutes=1))

    def test_confirmation_is_checked_before_the_plan_is_even_looked_up(
        self, store: PlanStore
    ) -> None:
        """An unconfirmed request is refused for being unconfirmed, whatever id it carries."""
        with pytest.raises(PlanNotConfirmed):
            store.authorize("plan-nonsense", confirm="no", tenant=OWNER, now=ISSUED)

    def test_confirmation_does_not_rescue_an_expired_plan(self, store: PlanStore) -> None:
        stored = store.issue(_plan(), tenant=OWNER, now=ISSUED)
        with pytest.raises(PlanExpired):
            store.authorize(
                stored.plan_id,
                confirm=CONFIRMATION_TOKEN,
                tenant=OWNER,
                now=_at(minutes=30, seconds=1),
            )

    def test_confirmation_does_not_rescue_another_tenant_s_plan(self, store: PlanStore) -> None:
        stored = store.issue(_plan(), tenant=OWNER, now=ISSUED)
        with pytest.raises(UnknownPlan):
            store.authorize(stored.plan_id, confirm=CONFIRMATION_TOKEN, tenant=STRANGER, now=ISSUED)


# =====================================================================================
# The client ids, and what they buy
# =====================================================================================
class TestClientIds:
    def test_every_leg_gets_plan_id_colon_symbol(self, store: PlanStore) -> None:
        stored = store.issue(_plan(), tenant=OWNER, now=ISSUED)
        for leg, cid in zip(stored.legs, stored.client_ids(), strict=True):
            assert cid == f"{stored.plan_id}:{leg['symbol']}"
            assert parse_client_id(cid) == (stored.plan_id, leg["symbol"])

    def test_the_ids_are_stable_across_presentations(self, store: PlanStore) -> None:
        """The property the whole idempotency argument rests on."""
        stored = store.issue(_plan(), tenant=OWNER, now=ISSUED)
        first = store.authorize(
            stored.plan_id, confirm=CONFIRMATION_TOKEN, tenant=OWNER, now=_at(minutes=1)
        ).client_ids()
        second = store.authorize(
            stored.plan_id, confirm=CONFIRMATION_TOKEN, tenant=OWNER, now=_at(minutes=20)
        ).client_ids()
        assert first == second

    def test_a_symbol_the_plan_does_not_name_gets_no_client_id(self, store: PlanStore) -> None:
        """A plan IS its list of orders. Minting a key for anything else would let an
        authorised plan id carry an order the investor never previewed."""
        stored = store.issue(_plan(), tenant=OWNER, now=ISSUED)
        with pytest.raises(SymbolNotInPlan):
            stored.client_id_for("TATASTEEL")

    def test_a_leg_s_own_symbol_is_accepted_however_it_is_cased(self, store: PlanStore) -> None:
        """The check and the mint must agree about which instrument they mean."""
        stored = store.issue(_plan(), tenant=OWNER, now=ISSUED)
        assert stored.client_id_for("infy") == stored.client_id_for("INFY")

    def test_two_plans_over_the_same_basket_do_not_share_ids(self, store: PlanStore) -> None:
        """A second, legitimate rebalance of the same names must not read as a duplicate."""
        one = store.issue(_plan(), tenant=OWNER, now=ISSUED)
        two = store.issue(_plan(), tenant=OWNER, now=ISSUED)
        assert set(one.client_ids()).isdisjoint(two.client_ids())


# =====================================================================================
# Store + gateway: the re-presented plan does not double-send
# =====================================================================================
class TestARePresentedPlanIsIdempotent:
    """DRY_RUN throughout. The broker double records; nothing here can reach a network."""

    class SpyKC:
        def place_order(self, **_: object) -> str:  # pragma: no cover - dry run never calls it
            raise AssertionError("DRY_RUN must not reach the broker")

    def _gateway(self, tmp_path: Path) -> tuple[OrderGateway, Path]:
        journal = tmp_path / "journal.jsonl"
        gateway = OrderGateway(
            self.SpyKC(),
            RiskManager(),
            gates=lambda: ProductGates(dry_run=True),
            journal_path=str(journal),
        )
        return gateway, journal

    @staticmethod
    def _send(gateway: OrderGateway, stored: StoredPlan) -> list[str]:
        statuses: list[str] = []
        for leg in stored.legs:
            result = asyncio.run(
                gateway.place(
                    symbol=leg["symbol"],
                    qty=leg["quantity"],
                    side=leg["side"],
                    product="CNC",
                    order_type="LIMIT",
                    price=float(leg["ref_price"]),
                    exchange="NSE",
                    client_id=stored.client_id_for(leg["symbol"]),
                    tenant=stored.tenant,
                    plan_tenant=stored.tenant,
                    gross_exposure=0.0,
                )
            )
            statuses.append(str(result["status"]))
        return statuses

    def test_the_second_confirmation_places_nothing_new(self, tmp_path: Path) -> None:
        store = PlanStore()
        gateway, journal = self._gateway(tmp_path)
        stored = store.issue(_plan(), tenant=OWNER, now=ISSUED)

        first = store.authorize(
            stored.plan_id, confirm=CONFIRMATION_TOKEN, tenant=OWNER, now=_at(minutes=1)
        )
        assert self._send(gateway, first) == ["DRY_RUN", "DRY_RUN"]

        second = store.authorize(
            stored.plan_id, confirm=CONFIRMATION_TOKEN, tenant=OWNER, now=_at(minutes=2)
        )
        assert self._send(gateway, second) == ["DUPLICATE", "DUPLICATE"]

        lines = [json.loads(line) for line in journal.read_text().splitlines() if line]
        assert [line["event"] for line in lines] == ["dry_run", "dry_run"]
        assert {line["client_id"] for line in lines} == set(stored.client_ids())

    def test_a_replay_after_the_window_never_reaches_the_gateway_at_all(
        self, tmp_path: Path
    ) -> None:
        """Two layers, and the outer one is the plan store: an expired plan is refused before
        anything is asked of the order path."""
        store = PlanStore()
        journal = self._gateway(tmp_path)[1]
        stored = store.issue(_plan(), tenant=OWNER, now=ISSUED)
        with pytest.raises(PlanExpired):
            store.authorize(
                stored.plan_id,
                confirm=CONFIRMATION_TOKEN,
                tenant=OWNER,
                now=_at(minutes=30, seconds=1),
            )
        assert not journal.exists() or journal.read_text() == ""


# =====================================================================================
# The constraint this leaf ships under
# =====================================================================================
class TestNoFiringPin:
    def test_the_store_declares_no_route(self) -> None:
        source = _code_only(plan_store_module)
        for forbidden in ("@router.", "APIRouter", "fastapi"):
            assert forbidden not in source, f"plan_store.py names {forbidden}"

    def test_the_store_cannot_place_an_order(self) -> None:
        source = _code_only(plan_store_module)
        for forbidden in ("place_order", "place_gtt", "OrderGateway", "kiteconnect"):
            assert forbidden not in source, f"plan_store.py names {forbidden}"

    def test_the_store_reads_no_ambient_clock(self) -> None:
        """Law #1's spirit, one layer out: an injected clock is what makes the boundary
        testable at the second, and an ambient one is how a 29:59 test starts flaking."""
        source = _code_only(plan_store_module)
        for forbidden in ("datetime.now(", "dt.datetime.now(", "time.time(", "utcnow("):
            assert forbidden not in source, f"plan_store.py reads an ambient clock: {forbidden}"
