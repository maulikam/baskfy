"""Desk non-negotiable #1, as a mechanism in this repo rather than a route on the desk.

    "Never auto-execute. Orders fire only from ``POST /execute`` with ``confirm=true`` and the
     ``plan_id`` issued by ``/analyze``; plans expire in 30 minutes."

WHAT THIS PORTS. Every word of that rule was implemented in six lines of
``kite-momentum-rebalancer/app/main.py:519-524`` — a ``confirm != "true"`` check, a lookup in a
module-level ``PLANS`` dict, and ``time.time() - plan["created_at"] > 1800`` — plus the
``client_id=f"{plan_id}:{o['symbol']}"`` at ``:581``. All of it inside a route handler, in the
tree that is being retired. Measured by leaf 1.2.3: ``PLAN_TTL`` is named fifteen times under
``packages/`` and compared against a clock **zero** times. Retiring the desk would have deleted
the enforcement of a non-negotiable, not merely the code that happened to hold it.

THIS LEAF ADDS NO ROUTE, AND THAT IS DELIBERATE. The execute route is gated on counsel item C3.
What is built here is the thing that has to exist *before* a route can be safe, and that is
worth having on its own: the rule is now a tested object with an injectable clock instead of a
form handler nobody can call from a test.

WHY IT LIVES IN ``services/`` AND NOT ``packages/core``
-------------------------------------------------------
Law #1: "``packages/core`` touches nothing. No database, no network, no disk, no clock." A plan
store is *state plus a clock* — the two things core may not hold. So the split is:

* the **predicate** — ``baskfy_core.curated_plans.plan_is_expired`` — is pure, and sits beside
  the ``PLAN_TTL`` it enforces so the constant and its enforcement cannot drift apart;
* the **store** — this module — holds the plans, is handed the clock by its caller, and asks
  core for the verdict. It never re-implements the arithmetic, which is why "30 minutes" is
  written down exactly once in this repo.

``baskfy_api`` is the right service for it: a plan is *issued to* a caller by this API and
*re-presented by* that caller, and this package already depends on both ``baskfy_core`` (for
the predicate) and ``baskfy_execution`` (for the client-id mint). It could not live in
``packages/execution`` without giving the order path a memory of its callers, which is a larger
change than this leaf should make to the module that talks to a broker.

WHAT THIS STORE IS NOT
----------------------
It is **in-process**, exactly like the desk's ``PLANS`` dict, and that is a real limit rather
than an oversight: two API workers do not share it, so a plan issued by one and presented to
the other reads as unknown (a refusal — the safe direction) and, worse, the same plan presented
to both would pass this layer twice. It is safe today because nothing executes; before the C3
route ships, the backing map must move to storage shared by every worker. Recorded in
``docs/DECISIONS-MERGE.md`` (leaf 2.2). The second layer against double-sending — the gateway's
``client_id`` idempotency — is per-process too, so this is one open item, not two.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import ClassVar, Final

from baskfy_execution import TenantIds, mint_client_id

from baskfy_core.curated_plans import (
    PLAN_TTL,
    DeskPlan,
    PlanKind,
    PlanLeg,
    plan_expires_at,
    plan_is_expired,
)

__all__ = [
    "CONFIRMATION_TOKEN",
    "PLAN_ID_PREFIX",
    "PlanExpired",
    "PlanNotConfirmed",
    "PlanRefused",
    "PlanStore",
    "StoredPlan",
    "SymbolNotInPlan",
    "UnknownPlan",
]

#: The exact string ``confirm`` must carry. ``app/main.py:521``: ``if confirm != "true"``.
#:
#: A **string**, and compared for equality, because the field arrives from a form. The obvious
#: "improvement" — take a ``bool`` — is the bug: ``bool("false")`` is ``True``, so a client that
#: sends the word "false" would be read as having confirmed.
CONFIRMATION_TOKEN: Final = "true"

#: Marks an id this store minted. Deliberately not the API's ``cb-sim-`` preview prefix: a
#: preview id is a display handle for a plan nobody can execute, and the two must not be
#: mistaken for one another.
PLAN_ID_PREFIX: Final = "plan-"


def _copy_leg(leg: PlanLeg) -> PlanLeg:
    """A field-by-field copy, typed. ``dict(leg)`` would widen it to ``dict[str, object]`` and
    cost a ``type: ignore``, which house rule 3 does not permit and which would hide the day a
    field is added to :class:`~baskfy_core.curated_plans.PlanLeg` and not copied here.

    (Written without the leading hash on purpose. ``test_no_escape_hatches.py`` scans for the
    literal escape hatch and cannot tell it from prose about it, so the house style — see
    ``screener.py:102`` — is to name it unprefixed. The scanner is right to be blunt: teaching
    it to skip comments would let a real hatch hide inside one.)"""
    return {
        "symbol": leg["symbol"],
        "side": leg["side"],
        "quantity": leg["quantity"],
        "ref_price": leg["ref_price"],
    }


def _copy_plan(plan: DeskPlan) -> DeskPlan:
    """The stored plan, detached from the caller's. Same reasoning as :func:`_copy_leg`, one
    level up: the ``legs`` list itself is mutable, so sharing it would let a caller add a leg
    to a plan that has already been authorised."""
    return {
        "kind": plan["kind"],
        "legs": [_copy_leg(leg) for leg in plan["legs"]],
        "requested_amount": plan["requested_amount"],
        "expires_at_hint": plan["expires_at_hint"],
    }


class PlanRefused(Exception):
    """A plan was not authorised. Never raised for a reason the caller cannot be told.

    ``status`` is the HTTP code the desk answered with, carried here so the later route does
    not have to re-derive it and so this file records the whole ported contract in one place.
    Naming a status is not shipping a route: nothing in this module is registered anywhere.
    """

    status: ClassVar[int] = 400


class PlanNotConfirmed(PlanRefused):
    """``confirm`` was not exactly ``"true"`` (``app/main.py:521`` → 400)."""

    status: ClassVar[int] = 400


class UnknownPlan(PlanRefused):
    """No such plan for this caller (``app/main.py:523`` → 404).

    ALSO THE ANSWER FOR ANOTHER TENANT'S PLAN, and that is a choice. Telling a caller "that
    plan exists but is not yours" turns the id space into an oracle they can enumerate; "unknown"
    tells them the only thing they are entitled to know. The gateway refuses the cross-tenant
    case again at the order layer (``refuse_cross_tenant``), so nothing rests on this alone.
    """

    status: ClassVar[int] = 404


class PlanExpired(PlanRefused):
    """The plan is at or past ``issued_at + PLAN_TTL`` (``app/main.py:524`` → 410)."""

    status: ClassVar[int] = 410


class SymbolNotInPlan(PlanRefused):
    """A client id was asked for an instrument this plan does not name.

    NOT ON THE DESK, and it should have been. ``client_id=f"{plan_id}:{o['symbol']}"`` was built
    inside the loop over ``plan["orders"]``, so the plan constrained the symbols by construction
    and nothing had to check. The moment the mint becomes a function anyone may call, that
    constraint stops being structural: ``plan.client_id_for("SOMETHING_ELSE")`` would hand back
    a well-formed key under an authorised plan's id, and the gateway would accept it — the plan
    would have authorised an order it does not contain. A plan is a *list of orders*, and this
    is what makes it one.
    """

    status: ClassVar[int] = 400


class StoredPlan:
    """One issued plan: what it does, who it belongs to, and when it stops being executable.

    Immutable, and its legs are **copied** at issue. A stored plan that shared leg dicts with
    its caller could have its quantities changed after issue — the same ``plan_id`` and the same
    ``client_id`` would then authorise a different order, which is precisely the substitution
    the plan id exists to prevent.
    """

    __slots__ = ("_plan", "issued_at", "plan_id", "tenant")

    def __init__(
        self,
        *,
        plan_id: str,
        tenant: TenantIds,
        plan: DeskPlan,
        issued_at: dt.datetime,
    ) -> None:
        self.plan_id = plan_id
        self.tenant = tenant
        self.issued_at = issued_at
        self._plan = _copy_plan(plan)

    @property
    def kind(self) -> PlanKind:
        return self._plan["kind"]

    @property
    def requested_amount(self) -> Decimal:
        return self._plan["requested_amount"]

    @property
    def legs(self) -> tuple[PlanLeg, ...]:
        """The legs, as copies. Mutating what you get back cannot reach the stored plan."""
        return tuple(_copy_leg(leg) for leg in self._plan["legs"])

    @property
    def expires_at(self) -> dt.datetime:
        """``issued_at + PLAN_TTL``, from core — the same instant the preview showed."""
        return plan_expires_at(self.issued_at)

    def is_expired(self, *, now: dt.datetime) -> bool:
        return plan_is_expired(issued_at=self.issued_at, now=now)

    def client_id_for(self, symbol: str) -> str:
        """``plan_id:symbol`` — the gateway's idempotency key, minted in ``packages/``.

        Refuses a symbol the plan does not name: see :class:`SymbolNotInPlan`. Matched on the
        same upper-cased form the mint produces, so the check and the key cannot disagree about
        which instrument they mean.
        """
        wanted = symbol.upper()
        if wanted not in {leg["symbol"].upper() for leg in self._plan["legs"]}:
            raise SymbolNotInPlan(
                f"{symbol!r} is not a leg of plan {self.plan_id}; it authorises "
                f"{sorted(leg['symbol'] for leg in self._plan['legs'])}"
            )
        return mint_client_id(plan_id=self.plan_id, symbol=symbol)

    def client_ids(self) -> tuple[str, ...]:
        """One key per leg, in leg order. Deterministic: re-presenting mints the same tuple."""
        return tuple(self.client_id_for(leg["symbol"]) for leg in self._plan["legs"])

    def __repr__(self) -> str:
        return (
            f"StoredPlan(plan_id={self.plan_id!r}, kind={self.kind!r}, "
            f"legs={len(self._plan['legs'])}, issued_at={self.issued_at.isoformat()})"
        )


class PlanStore:
    """Issue a ``plan_id``, look one up, refuse an unknown or expired one.

    Every method that needs the time takes ``now`` as an argument. There is no
    ``datetime.now()`` in this file, and that is what lets the thirty-minute boundary be
    asserted at the second — 29:59 live, 30:01 refused — with no ``sleep`` anywhere.
    """

    def __init__(self) -> None:
        self._plans: dict[str, StoredPlan] = {}

    def __len__(self) -> int:
        return len(self._plans)

    # -- issue ---------------------------------------------------------------------------
    def issue(self, plan: DeskPlan, *, tenant: TenantIds, now: dt.datetime) -> StoredPlan:
        """Record *plan* under a fresh id and return it.

        ``issued_at`` IS DERIVED FROM THE PLAN, NOT FROM ``now``. The plan already carries
        ``expires_at_hint`` — the instant the preview promised the investor — and re-stamping it
        with the store's clock would silently extend a plan that had been sitting in a browser
        tab, so the countdown the user watched and the deadline that refuses them would be
        different numbers. ``now`` is used for one thing: refusing to store a plan that is
        already dead.
        """
        issued_at = plan["expires_at_hint"] - PLAN_TTL
        if plan_is_expired(issued_at=issued_at, now=now):
            raise PlanExpired(
                f"plan built at {issued_at.isoformat()} expired at "
                f"{plan_expires_at(issued_at).isoformat()}; it cannot be issued at "
                f"{now.isoformat()} — re-run the preview"
            )
        # Bounded without a background task: the only thing that grows this map is issuing, so
        # the only moment it needs pruning is now. The desk's `PLANS` dict grew for the life of
        # the process.
        self.purge_expired(now=now)
        stored = StoredPlan(
            plan_id=f"{PLAN_ID_PREFIX}{uuid.uuid4()}",
            tenant=tenant,
            plan=plan,
            issued_at=issued_at,
        )
        self._plans[stored.plan_id] = stored
        return stored

    # -- read ----------------------------------------------------------------------------
    def lookup(self, plan_id: str, *, tenant: TenantIds, now: dt.datetime) -> StoredPlan:
        """Return the caller's live plan, or refuse.

        The order of the two refusals matters: unknown before expired, so an id that was never
        issued cannot be distinguished from one that has aged out by which error comes back.
        """
        stored = self._plans.get(plan_id)
        if stored is None or stored.tenant != tenant:
            raise UnknownPlan(f"unknown or expired plan_id {plan_id!r} — re-run the preview")
        if stored.is_expired(now=now):
            # NOT DELETED HERE, deliberately. Dropping it on the way past would make the same
            # request answer 410 once and then 404 for ever after — the client that retries a
            # timed-out call would be told two different stories about one plan, and the second
            # is the less true one. ``purge_expired`` bounds the map at issue time instead, so
            # the record still goes, just not in the middle of answering a question about it.
            raise PlanExpired(
                f"plan {plan_id} expired at {stored.expires_at.isoformat()} "
                f"(issued {stored.issued_at.isoformat()}, now {now.isoformat()}) — prices are "
                f"stale, re-run the preview"
            )
        return stored

    def authorize(
        self,
        plan_id: str,
        *,
        confirm: str,
        tenant: TenantIds,
        now: dt.datetime,
    ) -> StoredPlan:
        """Non-negotiable #1 in one call: confirmed, known, this caller's, and not expired.

        RE-PRESENTING A PLAN IS ALLOWED, AND THAT IS THE PORTED BEHAVIOUR, NOT AN OVERSIGHT. A
        second authorisation inside the window returns the same plan and therefore the same
        ``client_ids()``, so the gateway answers ``DUPLICATE`` for everything it already sent —
        idempotent, not double-sent. Refusing the second call instead would look stricter and be
        worse: a batch that stopped halfway (the 18 Aug 2026 circuit-breaker case) could never be
        completed, and the operator's only remaining move would be to place the rest by hand,
        outside the gateway.

        Returning the plan is not placing an order. Nothing in this module can reach a broker.
        """
        if confirm != CONFIRMATION_TOKEN:
            raise PlanNotConfirmed(
                f"execution requires confirm=={CONFIRMATION_TOKEN!r}; got {confirm!r}"
            )
        return self.lookup(plan_id, tenant=tenant, now=now)

    # -- housekeeping --------------------------------------------------------------------
    def purge_expired(self, *, now: dt.datetime) -> int:
        """Drop every plan past its deadline; return how many. Purely hygiene — ``lookup``
        refuses an expired plan whether or not this has run."""
        dead = [pid for pid, plan in self._plans.items() if plan.is_expired(now=now)]
        for pid in dead:
            del self._plans[pid]
        return len(dead)
