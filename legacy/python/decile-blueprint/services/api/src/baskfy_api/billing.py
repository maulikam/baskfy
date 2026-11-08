"""Checkout and the webhook — Prompt 13 deliverables 2 and 4, the parts that touch the database.

    "Razorpay integration: subscriptions for monthly/yearly, one-time order for forever;
     /checkout/session; a signature-verified, idempotent /webhooks/razorpay that is safe to
     replay; subscription lifecycle handling (created, charged, halted, cancelled, expired)."

The HTTP surface is ``baskfy_api.routers.billing``; the gateway client and the signature
arithmetic are ``baskfy_api.razorpay``; this module is what happens to our own rows.

Three decisions the bundle does not make, all recorded in ``docs/DECISIONS.md``:

**A "Forever" purchase is a subscription row with no period end.** docs/04 gives ``payment`` a
nullable ``subscription_id``, so a one-time payment *can* stand alone — but then nothing links the
account to the plan it bought, and ``GET /me`` has no plan to report. ``subscription`` is
therefore the record of a *grant*: ``current_period_end IS NULL`` means it never lapses, which is
exactly what Forever is.

**A checkout creates the subscription row immediately, as ``past_due``.** The row has to exist to
hold ``razorpay_subscription_id`` before the gateway calls back. docs/04 offers four statuses and
none of them means "created, not yet paid"; ``past_due`` is the only non-terminal one that does
not entitle, which makes it the safe placeholder. ``subscription.activated`` promotes it.

**A replayed webhook is a no-op, not an error.** docs/07 requires the endpoint to be "idempotent
by event id" and docs/11 to "dedupe by event id, process idempotently". The insert into
``webhook_event`` is ``ON CONFLICT DO NOTHING``; if it returns no row, the event has been seen and
the handler stops. Five deliveries therefore produce one payment — Prompt 13's first acceptance
criterion — and Razorpay still gets its 200, because answering anything else makes it retry
forever.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api import invoices
from baskfy_api.razorpay import RazorpayGateway, from_paise
from baskfy_api.settings import Settings
from baskfy_core.models import AppUser, Payment, Plan, Subscription, WebhookEvent
from baskfy_providers.archive import RawArchive

log = logging.getLogger(__name__)

#: docs/04: ``subscription.status`` is one of these four.
ACTIVE: Final = "active"
PAST_DUE: Final = "past_due"
CANCELLED: Final = "cancelled"
EXPIRED: Final = "expired"

#: Razorpay's subscription events, mapped onto docs/04's four statuses.
#:
#: ``halted`` is Razorpay's "we retried the charge and gave up". docs/04 has no such status;
#: ``past_due`` is the closest and the only recoverable one, which is right — a halted
#: subscription resumes when the customer pays.
#: ``completed`` is a subscription that ran its ``total_count`` cycles, which for us is ten years
#: away and is an expiry when it happens.
SUBSCRIPTION_EVENT_STATUS: Final[Mapping[str, str]] = {
    "subscription.authenticated": PAST_DUE,
    "subscription.activated": ACTIVE,
    "subscription.charged": ACTIVE,
    "subscription.pending": PAST_DUE,
    "subscription.halted": PAST_DUE,
    "subscription.cancelled": CANCELLED,
    "subscription.paused": PAST_DUE,
    "subscription.resumed": ACTIVE,
    "subscription.completed": EXPIRED,
    "subscription.expired": EXPIRED,
    "subscription.updated": ACTIVE,
}

#: The events that record money changing hands.
PAYMENT_EVENTS: Final[frozenset[str]] = frozenset(
    {"payment.captured", "order.paid", "subscription.charged"}
)

#: Razorpay's ``notes`` carry our own identifiers back to us. The keys are ours.
NOTE_USER: Final = "baskfy_user_public_id"
NOTE_PLAN: Final = "baskfy_plan_code"


class CheckoutError(RuntimeError):
    """The checkout cannot be created — an unknown plan, a plan the flag hides, a missing key."""


@dataclass(frozen=True, slots=True)
class CheckoutSession:
    """What the browser needs to open Razorpay's widget."""

    plan_code: str
    kind: str  # "order" | "subscription"
    amount_inr: Decimal
    currency: str
    razorpay_key_id: str
    order_id: str | None = None
    subscription_id: str | None = None
    #: Our own subscription row, so the callback can be attributed without trusting the browser.
    subscription_public_ref: int | None = None
    #: docs/11 §Legal: stated at the point of sale, for the Forever plan.
    disclosure: str | None = None


async def load_plan(session: AsyncSession, code: str) -> Plan | None:
    return (await session.execute(select(Plan).where(Plan.code == code))).scalar_one_or_none()


def _notes(user: AppUser, plan: Plan) -> dict[str, str]:
    return {NOTE_USER: user.public_id, NOTE_PLAN: plan.code}


async def _upsert_pending_subscription(
    session: AsyncSession, user: AppUser, plan: Plan, razorpay_subscription_id: str | None
) -> Subscription:
    """The grant row, created before the gateway confirms anything.

    Re-running a checkout for the same plan reuses the pending row rather than accumulating one
    per abandoned attempt.
    """
    existing = (
        await session.execute(
            select(Subscription)
            .where(
                Subscription.user_id == user.id,
                Subscription.plan_id == plan.id,
                Subscription.status == PAST_DUE,
            )
            .order_by(Subscription.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if razorpay_subscription_id:
            existing.razorpay_subscription_id = razorpay_subscription_id
        await session.flush()
        return existing

    subscription = Subscription(
        user_id=user.id,
        plan_id=plan.id,
        status=PAST_DUE,
        started_at=dt.datetime.now(tz=dt.UTC),
        current_period_end=None,
        razorpay_subscription_id=razorpay_subscription_id,
    )
    session.add(subscription)
    await session.flush()
    return subscription


def _cycles(settings: Settings, plan: Plan) -> int:
    if plan.interval == "month":
        return settings.razorpay_subscription_cycles_monthly
    return settings.razorpay_subscription_cycles_yearly


def _gateway_plan_id(settings: Settings, plan: Plan) -> str:
    gateway_id = (
        settings.razorpay_plan_id_monthly
        if plan.interval == "month"
        else settings.razorpay_plan_id_yearly
    )
    if not gateway_id:
        raise CheckoutError(
            f"No Razorpay plan id is configured for the {plan.code!r} plan; set "
            f"BASKFY_RAZORPAY_PLAN_ID_{'MONTHLY' if plan.interval == 'month' else 'YEARLY'}."
        )
    return gateway_id


async def create_checkout_session(
    session: AsyncSession,
    settings: Settings,
    gateway: RazorpayGateway,
    *,
    user: AppUser,
    plan_code: str,
) -> CheckoutSession:
    """docs/07: "POST /checkout/session { plan_code } -> Razorpay order/subscription payload"."""
    plan = await load_plan(session, plan_code)
    if plan is None:
        raise CheckoutError(f"No plan with code {plan_code!r}.")
    if plan.price_inr <= 0 and not settings.free_tier_enabled:
        raise CheckoutError(f"The {plan_code!r} plan is not on sale.")
    if plan.price_inr <= 0:
        raise CheckoutError("The free plan does not need a checkout; it is granted on sign-up.")

    disclosure = plan.features.get("disclosure")
    notes = _notes(user, plan)

    if plan.interval is None:
        # docs/02: "one-time for Forever".
        order = await gateway.create_order(
            amount_inr=plan.price_inr,
            receipt=f"{user.public_id}-{plan.code}",
            notes=notes,
        )
        subscription = await _upsert_pending_subscription(session, user, plan, None)
        return CheckoutSession(
            plan_code=plan.code,
            kind="order",
            amount_inr=plan.price_inr,
            currency="INR",
            razorpay_key_id=settings.razorpay_key_id,
            order_id=str(order.get("id", "")),
            subscription_public_ref=subscription.id,
            disclosure=str(disclosure) if isinstance(disclosure, str) else None,
        )

    customer = await gateway.create_customer(name=user.name, email=user.email)
    created = await gateway.create_subscription(
        plan_id=_gateway_plan_id(settings, plan),
        total_count=_cycles(settings, plan),
        notes=notes,
        customer_id=str(customer.get("id")) if customer.get("id") else None,
    )
    gateway_subscription_id = str(created.get("id", ""))
    subscription = await _upsert_pending_subscription(
        session, user, plan, gateway_subscription_id or None
    )
    if customer.get("id"):
        subscription.razorpay_customer_id = str(customer["id"])
    await session.flush()
    return CheckoutSession(
        plan_code=plan.code,
        kind="subscription",
        amount_inr=plan.price_inr,
        currency="INR",
        razorpay_key_id=settings.razorpay_key_id,
        subscription_id=gateway_subscription_id or None,
        subscription_public_ref=subscription.id,
        disclosure=str(disclosure) if isinstance(disclosure, str) else None,
    )


# ---------------------------------------------------------------------------
# The webhook
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WebhookOutcome:
    """What one delivery did. Returned so the router can log it and the tests can assert it."""

    status: str
    detail: str
    payment_id: int | None = None
    duplicate: bool = False


def _entity(payload: Mapping[str, object], kind: str) -> Mapping[str, object]:
    """Razorpay nests entities as ``payload.<kind>.entity``. Missing reads as empty."""
    container = payload.get("payload")
    if not isinstance(container, Mapping):
        return {}
    holder = container.get(kind)
    if not isinstance(holder, Mapping):
        return {}
    entity = holder.get("entity")
    return entity if isinstance(entity, Mapping) else {}


def _text(entity: Mapping[str, object], key: str) -> str | None:
    value = entity.get(key)
    return str(value) if isinstance(value, (str, int)) else None


async def _claim_event(
    session: AsyncSession, event_id: str, event_type: str, payload: Mapping[str, object]
) -> bool:
    """Insert the delivery, or report that it has been seen before.

    ``ON CONFLICT DO NOTHING ... RETURNING`` returns no row when the id already exists, and does
    not abort the transaction the way a caught ``IntegrityError`` would.
    """
    inserted = (
        await session.execute(
            insert(WebhookEvent)
            .values(
                provider="razorpay",
                event_id=event_id,
                event_type=event_type,
                payload=dict(payload),
                status="received",
            )
            .on_conflict_do_nothing(index_elements=[WebhookEvent.event_id])
            .returning(WebhookEvent.id)
        )
    ).scalar_one_or_none()
    return inserted is not None


async def _finish_event(
    session: AsyncSession, event_id: str, status: str, note: str | None = None
) -> None:
    event = (
        await session.execute(select(WebhookEvent).where(WebhookEvent.event_id == event_id))
    ).scalar_one_or_none()
    if event is None:  # pragma: no cover - the row was inserted moments ago
        return
    event.status = status
    event.note = note
    event.processed_at = dt.datetime.now(tz=dt.UTC)
    await session.flush()


async def _user_from_notes(session: AsyncSession, entity: Mapping[str, object]) -> AppUser | None:
    notes = entity.get("notes")
    if not isinstance(notes, Mapping):
        return None
    public_id = notes.get(NOTE_USER)
    if not isinstance(public_id, str):
        return None
    return (
        await session.execute(select(AppUser).where(AppUser.public_id == public_id))
    ).scalar_one_or_none()


async def _subscription_by_gateway_id(
    session: AsyncSession, gateway_id: str
) -> Subscription | None:
    return (
        await session.execute(
            select(Subscription).where(Subscription.razorpay_subscription_id == gateway_id)
        )
    ).scalar_one_or_none()


def _period_end(entity: Mapping[str, object]) -> dt.datetime | None:
    """Razorpay sends epoch seconds. ``current_end`` is when the paid period runs out."""
    for key in ("current_end", "end_at", "charge_at"):
        value = entity.get(key)
        if isinstance(value, int) and value > 0:
            return dt.datetime.fromtimestamp(value, tz=dt.UTC)
    return None


@dataclass(frozen=True, slots=True)
class WebhookContext:
    """Everything the handler needs beyond the event itself."""

    settings: Settings
    #: Where an invoice PDF is written (`baskfy_api.invoices.build_invoice_archive`).
    archive: RawArchive


async def handle_event(
    session: AsyncSession,
    context: WebhookContext,
    *,
    event_id: str,
    payload: Mapping[str, object],
) -> WebhookOutcome:
    """Process one verified delivery. Safe to call five times with the same ``event_id``."""
    event_type = str(payload.get("event", "")) or "unknown"

    if not await _claim_event(session, event_id, event_type, payload):
        return WebhookOutcome(
            status="ignored", detail="Already processed this event.", duplicate=True
        )

    # No try/except here on purpose. If processing raises, the whole transaction — including the
    # `webhook_event` row just claimed — rolls back, the router answers 500, and Razorpay's retry
    # gets a clean attempt. Recording the delivery as "failed" would consume the event id, so the
    # retry would dedupe into a no-op and the payment would be lost in silence. The `failed`
    # status exists for a *later* operator-driven replay, not for this path.
    outcome = await _dispatch(session, context, event_type, payload)
    await _finish_event(session, event_id, outcome.status, outcome.detail[:500])
    return outcome


async def _dispatch(
    session: AsyncSession,
    context: WebhookContext,
    event_type: str,
    payload: Mapping[str, object],
) -> WebhookOutcome:
    lifecycle: WebhookOutcome | None = None
    if event_type in SUBSCRIPTION_EVENT_STATUS:
        lifecycle = await _apply_subscription_lifecycle(session, event_type, payload)

    if event_type in PAYMENT_EVENTS:
        recorded = await _record_payment(session, context, payload)
        if recorded is not None:
            return recorded

    if lifecycle is not None:
        return lifecycle
    return WebhookOutcome(status="ignored", detail=f"{event_type} needs no action.")


async def _apply_subscription_lifecycle(
    session: AsyncSession, event_type: str, payload: Mapping[str, object]
) -> WebhookOutcome:
    """docs/04's four statuses, driven by Razorpay's events (see SUBSCRIPTION_EVENT_STATUS)."""
    entity = _entity(payload, "subscription")
    gateway_id = _text(entity, "id")
    if gateway_id is None:
        return WebhookOutcome(status="ignored", detail="No subscription id on the event.")

    subscription = await _subscription_by_gateway_id(session, gateway_id)
    if subscription is None:
        return WebhookOutcome(status="ignored", detail=f"No subscription matches {gateway_id!r}.")

    subscription.status = SUBSCRIPTION_EVENT_STATUS[event_type]
    period_end = _period_end(entity)
    if period_end is not None:
        subscription.current_period_end = period_end
    await session.flush()
    return WebhookOutcome(status="processed", detail=f"{gateway_id} is now {subscription.status}.")


async def _record_payment(
    session: AsyncSession, context: WebhookContext, payload: Mapping[str, object]
) -> WebhookOutcome | None:
    """One captured charge -> one ``payment`` row and one tax invoice.

    ``razorpay_payment_id`` is UNIQUE, so the same charge arriving under two event types
    (``payment.captured`` and ``order.paid`` both fire for one order) records once.
    """
    entity = _entity(payload, "payment")
    payment_id = _text(entity, "id")
    if payment_id is None:
        return None
    if _text(entity, "status") not in (None, "captured"):
        return WebhookOutcome(status="ignored", detail=f"{payment_id} is not captured.")

    existing = (
        await session.execute(select(Payment).where(Payment.razorpay_payment_id == payment_id))
    ).scalar_one_or_none()
    if existing is not None:
        return WebhookOutcome(
            status="ignored",
            detail=f"{payment_id} is already recorded.",
            payment_id=existing.id,
            duplicate=True,
        )

    user = await _user_from_notes(session, entity)
    subscription, plan = await _grant_for(session, payload, entity, user)
    if user is None or plan is None:
        return WebhookOutcome(
            status="ignored", detail=f"{payment_id} could not be attributed to an account."
        )

    amount = entity.get("amount")
    amount_inr = from_paise(int(amount)) if isinstance(amount, int) else plan.price_inr

    payment = Payment(
        user_id=user.id,
        subscription_id=subscription.id if subscription is not None else None,
        amount_inr=amount_inr,
        status="captured",
        razorpay_payment_id=payment_id,
        razorpay_order_id=_text(entity, "order_id"),
        customer_gstin=None,
        place_of_supply=None,
    )
    session.add(payment)
    await session.flush()

    period = invoices.NO_PERIOD
    if subscription is not None and subscription.current_period_end is not None:
        period = invoices.BillingPeriod(
            start=subscription.started_at.date(), end=subscription.current_period_end.date()
        )
    await invoices.issue_invoice(
        session,
        context.settings,
        invoices.InvoiceRequest(
            payment=payment,
            user=user,
            plan=plan,
            archive=context.archive,
            period=period,
        ),
    )
    return WebhookOutcome(
        status="processed",
        detail=f"Recorded {payment_id} as {payment.invoice_number}.",
        payment_id=payment.id,
    )


async def _grant_for(
    session: AsyncSession,
    payload: Mapping[str, object],
    entity: Mapping[str, object],
    user: AppUser | None,
) -> tuple[Subscription | None, Plan | None]:
    """Find the subscription row and plan a captured charge belongs to.

    Preference order: the subscription the event names, then the pending grant this user has for
    the plan named in ``notes``. Nothing is inferred from the amount — two plans could one day
    cost the same.
    """
    subscription_entity = _entity(payload, "subscription")
    gateway_id = _text(subscription_entity, "id") or _text(entity, "subscription_id")
    if gateway_id is not None:
        subscription = await _subscription_by_gateway_id(session, gateway_id)
        if subscription is not None:
            plan = (
                await session.execute(select(Plan).where(Plan.id == subscription.plan_id))
            ).scalar_one_or_none()
            if subscription.status == PAST_DUE:
                subscription.status = ACTIVE
                await session.flush()
            return subscription, plan

    notes = entity.get("notes")
    plan_code = notes.get(NOTE_PLAN) if isinstance(notes, Mapping) else None
    if not isinstance(plan_code, str) or user is None:
        return None, None
    plan = await load_plan(session, plan_code)
    if plan is None:
        return None, None

    pending = (
        await session.execute(
            select(Subscription)
            .where(Subscription.user_id == user.id, Subscription.plan_id == plan.id)
            .order_by(Subscription.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if pending is None:
        pending = Subscription(
            user_id=user.id,
            plan_id=plan.id,
            status=ACTIVE,
            started_at=dt.datetime.now(tz=dt.UTC),
            # docs/02: the one-time Forever purchase never lapses.
            current_period_end=None,
        )
        session.add(pending)
    else:
        pending.status = ACTIVE
    await session.flush()
    return pending, plan
