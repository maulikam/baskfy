"""``/plans``, ``/checkout/session``, ``/webhooks/razorpay`` and ``/invoices`` — docs/07
§"Account & billing" (Prompt 13 deliverables 1, 2, 4).

    GET  /plans
    POST /checkout/session                    { plan_code } -> Razorpay order/subscription payload
    POST /webhooks/razorpay                   (signature-verified, idempotent by event id)
    GET  /invoices  /invoices/{id}/pdf

Three shapes worth stating:

**The webhook answers 200 for a replay and 200 for something it ignored.** Razorpay retries any
non-2xx, so a 4xx for "I have seen this" would produce an unbounded retry loop over an event that
is already handled. The only non-2xx it returns is 400 for a *bad signature* — which is not a
delivery from Razorpay at all — and 500 when processing genuinely failed, which is exactly when a
retry is wanted.

**The PDF is streamed, not redirected to.** A presigned bucket URL is a bearer credential for a
document carrying the customer's name and GSTIN. Streaming keeps the ownership check on the read.

**``/invoices`` is cursor-paginated** per docs/07 §Conventions, keyed on the payment id, which is
monotonic and unique — an ``created_at`` cursor would skip rows that share a timestamp.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from decile_api import billing, invoices
from decile_api.auth import AuthenticatedDep, settings_for
from decile_api.db import SessionDep
from decile_api.problems import Problem, ProblemType, not_found
from decile_api.razorpay import (
    EVENT_ID_HEADER,
    SIGNATURE_HEADER,
    PaymentsNotConfigured,
    RazorpayError,
    RazorpayGateway,
    verify_webhook_signature,
)
from decile_api.schemas import (
    DEFAULT_PAGE_SIZE,
    CheckoutSessionIn,
    CheckoutSessionOut,
    EntitlementsOut,
    InvoiceOut,
    InvoicePage,
    Limit,
    PlanFeatureOut,
    PlanListOut,
    PlanOut,
    WebhookAck,
)
from decile_api.settings import API_PREFIX, Settings
from decile_core.entitlements import Entitlements
from decile_core.models import AppUser, Payment, Plan, Subscription
from decile_core.seed_data import FREE_PLAN, PRE_PURCHASE_DISCLAIMERS
from decile_providers.archive import RawArchive

log = logging.getLogger(__name__)

router = APIRouter(tags=["billing"])


def _gateway(request: Request) -> RazorpayGateway:
    """One gateway per request, or the one a test attached to ``app.state``."""
    existing = getattr(request.app.state, "razorpay", None)
    if isinstance(existing, RazorpayGateway):
        return existing
    return RazorpayGateway(settings_for(request))


def _invoice_archive(request: Request) -> RawArchive:
    """The process-wide archive, built once by ``create_app`` (docs/02 §"Object storage")."""
    archive = getattr(request.app.state, "invoice_archive", None)
    if archive is None:  # pragma: no cover - lifespan always sets it
        archive = invoices.build_invoice_archive(settings_for(request))
        request.app.state.invoice_archive = archive
    resolved: RawArchive = archive
    return resolved


def _decimal_or_none(value: object) -> Decimal | None:
    if isinstance(value, str):
        return Decimal(value)
    return None


def _plan_out(plan: Plan) -> PlanOut:
    """A ``plan`` row rendered for the pricing page. Nothing here is computed in the web app."""
    features = plan.features
    raw_includes = features.get("includes")
    includes = raw_includes if isinstance(raw_includes, list) else []
    entitlements = Entitlements.from_plan_features(features)
    interval = plan.interval if plan.interval in ("month", "year") else None
    return PlanOut(
        code=plan.code,
        label=str(features.get("label") or _label_for(plan)),
        tagline=str(features.get("tagline") or ""),
        price_inr=plan.price_inr,
        interval=interval,
        features=[
            PlanFeatureOut(
                label=str(entry.get("label", "")),
                entitlement=(
                    str(entry["entitlement"])
                    if isinstance(entry, dict) and entry.get("entitlement")
                    else None
                ),
            )
            for entry in includes
            if isinstance(entry, dict)
        ],
        entitlements=EntitlementsOut.model_validate(entitlements.as_dict()),
        price_from_dec_2026=_decimal_or_none(features.get("price_from_dec_2026")),
        disclosure=(
            str(features["disclosure"]) if isinstance(features.get("disclosure"), str) else None
        ),
    )


def _label_for(plan: Plan) -> str:
    return plan.code.replace("_", " ").title()


@router.get("/plans", response_model=PlanListOut, summary="The plan catalogue")
async def list_plans(request: Request, session: SessionDep) -> PlanListOut:
    """docs/07: `GET /plans`.

    Public: `/pricing` is a marketing surface and must render for a signed-out visitor. The ₹0
    tier is hidden unless its feature flag is on (Prompt 13 §5).
    """
    settings = settings_for(request)
    rows = (await session.execute(select(Plan).order_by(Plan.id))).scalars().all()
    visible = [plan for plan in rows if plan.code != FREE_PLAN.code or settings.free_tier_enabled]
    return PlanListOut(
        data=[_plan_out(plan) for plan in visible],
        disclaimers=list(PRE_PURCHASE_DISCLAIMERS),
        free_tier_enabled=settings.free_tier_enabled,
    )


async def _load_user(session: AsyncSession, user_id: int) -> AppUser:
    user = (
        await session.execute(select(AppUser).where(AppUser.id == user_id))
    ).scalar_one_or_none()
    if user is None:  # pragma: no cover - the token was verified against this row
        raise Problem(ProblemType.UNAUTHENTICATED, "That account no longer exists.")
    return user


@router.post(
    "/checkout/session", response_model=CheckoutSessionOut, summary="Start a Razorpay checkout"
)
async def create_checkout_session(
    request: Request,
    body: CheckoutSessionIn,
    session: SessionDep,
    principal: AuthenticatedDep,
) -> CheckoutSessionOut:
    """docs/07: `POST /checkout/session { plan_code } -> Razorpay order/subscription payload`."""
    settings = settings_for(request)
    user = await _load_user(session, principal.require_user())
    try:
        created = await billing.create_checkout_session(
            session, settings, _gateway(request), user=user, plan_code=body.plan_code
        )
    except billing.CheckoutError as exc:
        raise Problem(ProblemType.NOT_FOUND, str(exc)) from exc
    except PaymentsNotConfigured as exc:
        # Honest rather than convenient: this deployment cannot take money. Production refuses to
        # start in this state (`Settings.require_configured`), so it is a local-only answer.
        raise Problem(
            ProblemType.INTERNAL_ERROR, "Payments are not configured on this deployment."
        ) from exc
    except RazorpayError as exc:
        log.error("razorpay refused a checkout", extra={"plan": body.plan_code})
        raise Problem(
            ProblemType.PIPELINE_DEGRADED, "The payment gateway is not responding."
        ) from exc

    return CheckoutSessionOut(
        plan_code=created.plan_code,
        kind="order" if created.kind == "order" else "subscription",
        amount_inr=created.amount_inr,
        key_id=created.razorpay_key_id,
        order_id=created.order_id,
        subscription_id=created.subscription_id,
        disclosure=created.disclosure,
    )


@router.post("/webhooks/razorpay", response_model=WebhookAck, summary="Razorpay webhook")
async def razorpay_webhook(
    request: Request,
    session: SessionDep,
    signature: Annotated[str | None, Header(alias=SIGNATURE_HEADER)] = None,
    event_id: Annotated[str | None, Header(alias=EVENT_ID_HEADER)] = None,
) -> WebhookAck:
    """docs/07: "(signature-verified, idempotent by event id)".

    docs/11 §Security: "verify signature, dedupe by event id, process idempotently".
    """
    settings: Settings = settings_for(request)
    raw = await request.body()
    if not verify_webhook_signature(raw, signature, settings.razorpay_webhook_secret):
        # Not authenticated as Razorpay, so not a webhook. 400 rather than 401: there is no
        # credential to re-present, and a 401 would invite a client to try one.
        log.warning("rejected a webhook with an invalid signature")
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            "The webhook signature did not verify.",
            errors=[{"field": SIGNATURE_HEADER, "message": "signature mismatch"}],
        )

    payload = await request.json()
    if not isinstance(payload, dict):
        raise Problem(
            ProblemType.INVALID_SCREEN_DEFINITION,
            "The webhook body was not a JSON object.",
            errors=[{"field": "body", "message": "expected an object"}],
        )

    # docs/07 keys idempotency on the *event id*. Razorpay puts it in a header, not the body; when
    # a delivery arrives without one the signature is used instead — it is a MAC of the exact
    # bytes, so two deliveries of the same event share it and two different events cannot.
    key = event_id or f"sig:{signature}"
    outcome = await billing.handle_event(
        session,
        billing.WebhookContext(settings=settings, archive=_invoice_archive(request)),
        event_id=key,
        payload=payload,
    )
    log.info(
        "razorpay webhook",
        extra={"event_type": str(payload.get("event", "")), "outcome": outcome.status},
    )
    return WebhookAck(status="processed" if outcome.status == "processed" else "ignored")


def _invoice_out(payment: Payment, plan_code: str | None) -> InvoiceOut:
    # The query filters on `invoice_number IS NOT NULL`; the column is nullable because a payment
    # row exists before its invoice is raised. An empty string here would be a bug worth seeing.
    number = payment.invoice_number or ""
    return InvoiceOut(
        invoice_number=number,
        invoice_date=payment.invoice_date or payment.created_at.date(),
        plan_code=plan_code,
        amount_inr=payment.amount_inr,
        taxable_inr=payment.taxable_inr,
        cgst_inr=payment.cgst_inr,
        sgst_inr=payment.sgst_inr,
        igst_inr=payment.igst_inr,
        gst_inr=payment.gst_inr,
        gst_rate=payment.gst_rate,
        place_of_supply=payment.place_of_supply,
        sac_code=payment.sac_code,
        status=payment.status,
        pdf_url=f"{API_PREFIX}/invoices/{number}/pdf",
    )


@router.get("/invoices", response_model=InvoicePage, summary="Your invoices")
async def list_invoices(
    session: SessionDep,
    principal: AuthenticatedDep,
    limit: Limit = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
) -> InvoicePage:
    """docs/07: `GET /invoices`, cursor-paginated per §Conventions.

    Only payments that have an invoice number: a `created` row is an attempt, not a document.
    """
    user_id = principal.require_user()
    statement = (
        select(Payment, Plan.code)
        .join(Subscription, Subscription.id == Payment.subscription_id, isouter=True)
        .join(Plan, Plan.id == Subscription.plan_id, isouter=True)
        .where(Payment.user_id == user_id, Payment.invoice_number.is_not(None))
        .order_by(Payment.id.desc())
        .limit(limit + 1)
    )
    if cursor is not None:
        try:
            statement = statement.where(Payment.id < int(cursor))
        except ValueError as exc:
            raise Problem(
                ProblemType.INVALID_SCREEN_DEFINITION,
                "That cursor is not a valid one.",
                errors=[{"field": "cursor", "message": "expected an integer"}],
            ) from exc

    rows = (await session.execute(statement)).all()
    page = rows[:limit]
    next_cursor = str(page[-1][0].id) if len(rows) > limit and page else None
    return InvoicePage(
        data=[_invoice_out(payment, plan_code) for payment, plan_code in page],
        next_cursor=next_cursor,
    )


@router.get(
    "/invoices/{invoice_number:path}/pdf",
    summary="Download one invoice",
    response_class=Response,
    responses={status.HTTP_200_OK: {"content": {"application/pdf": {}}}},
)
async def download_invoice(
    request: Request, invoice_number: str, session: SessionDep, principal: AuthenticatedDep
) -> Response:
    """docs/07: `GET /invoices/{id}/pdf`.

    ``{invoice_number:path}`` because the number contains slashes — ``DCL/2026-27/000001`` — which
    Rule 46(b)'s "consecutive serial number" tradition puts there and a path parameter would
    otherwise split.
    """
    payment = await invoices.load_invoice(session, principal.require_user(), invoice_number)
    if payment is None or payment.invoice_pdf_key is None:
        # Not-yours reads as absent, so the endpoint cannot be used to enumerate the series.
        raise not_found("invoice", invoice_number)

    payload = await invoices.load_pdf(_invoice_archive(request), payment.invoice_pdf_key)
    filename = invoice_number.replace("/", "-") + ".pdf"
    return Response(
        content=payload,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
