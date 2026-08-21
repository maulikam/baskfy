"""Invoices: the number, the tax, the PDF, and where it is kept — Prompt 13 deliverable 4.

    "Invoices: sequential invoice numbers, GST fields (GSTIN, HSN/SAC, place of supply), PDF
     generation stored in R2, /invoices list and download." — PROMPTS.md Prompt 13 §4.

    "Invoice numbers are gapless and unique under concurrent payment creation." — its acceptance
     criterion.

Why not a PostgreSQL sequence
-----------------------------
A ``SEQUENCE`` is explicitly non-transactional so that concurrent callers never block on it —
which means a rolled-back transaction burns its number and leaves a hole. Rule 46(b) of the CGST
Rules wants "a consecutive serial number". So the counter is an ordinary row, and
``UPDATE invoice_counter SET next_value = next_value + 1 ... RETURNING`` takes a row lock that is
held until the transaction ends. Two concurrent allocations therefore *serialise*: the second
waits for the first to commit, and takes the next number. That is the cost of gaplessness, it is
paid once per payment, and payments are not a hot path.

Gaplessness has one honest caveat, and it is inherent rather than a shortcut: a transaction that
allocates a number and then rolls back leaves a gap. :func:`allocate_invoice_number` is therefore
called from inside the same transaction that inserts the ``payment`` row, and nothing between
them can fail without both being undone.

Where the PDF goes
------------------
``decile_providers.archive`` already models "an S3-compatible bucket, or a directory when there is
no bucket" for the raw-file archive (docs/09), with R2 as the configured target (docs/02
§"Object storage"). Invoices reuse it rather than growing a second storage abstraction. The
boto3 calls are synchronous, so they are run on a worker thread — blocking the event loop for the
duration of an upload would stall every other request on the process.

The download endpoint streams the bytes rather than handing out a presigned URL. A presigned URL
is a bearer credential for a document containing the customer's name, address and GSTIN, and it
works for anyone who obtains it; streaming keeps the ownership check on every read.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final

import anyio
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from decile_api.settings import Settings
from decile_core.gst import GstSplit, financial_year, split_inclusive
from decile_core.invoice import InvoiceDocument, Party, render_invoice_pdf
from decile_core.models import AppUser, InvoiceCounter, Payment, Plan
from decile_providers.archive import RawArchive
from decile_providers.factory import build_archive
from decile_providers.settings import get_provider_settings

log = logging.getLogger(__name__)

#: IST. Every date on an Indian invoice is a local date, not a UTC one — an invoice raised at
#: 03:00 IST on 1 April belongs to the new financial year, and UTC would put it in the old one.
IST: Final = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")


def today_ist(now: dt.datetime | None = None) -> dt.date:
    moment = now or dt.datetime.now(tz=dt.UTC)
    return moment.astimezone(IST).date()


def invoice_series(on: dt.date) -> str:
    """``2026-27`` — the financial year the invoice belongs to."""
    return financial_year(on.year, on.month)


def format_invoice_number(settings: Settings, series: str, value: int) -> str:
    """``DCL/2026-27/000001``."""
    return f"{settings.invoice_series_prefix}/{series}/{value:0{settings.invoice_number_width}d}"


def invoice_object_key(settings: Settings, invoice_number: str) -> str:
    """Where the PDF lives in the bucket. Slashes in the number become path segments."""
    return f"{settings.invoice_object_prefix}/{invoice_number}.pdf"


async def allocate_invoice_number(
    session: AsyncSession, settings: Settings, *, on: dt.date | None = None
) -> str:
    """Take the next number in the series for ``on``, holding a row lock until commit.

    The counter row is created on first use with ``ON CONFLICT DO NOTHING``, so two concurrent
    first payments of a financial year do not race each other into a duplicate key.
    """
    issued_on = on or today_ist()
    series = invoice_series(issued_on)

    await session.execute(
        insert(InvoiceCounter)
        .values(series=series, next_value=1)
        .on_conflict_do_nothing(index_elements=[InvoiceCounter.series])
    )
    allocated = (
        await session.execute(
            update(InvoiceCounter)
            .where(InvoiceCounter.series == series)
            .values(next_value=InvoiceCounter.next_value + 1)
            .returning(InvoiceCounter.next_value)
        )
    ).scalar_one()
    # `next_value` now points at the *following* invoice, so this one takes the predecessor.
    return format_invoice_number(settings, series, allocated - 1)


@dataclass(frozen=True, slots=True)
class InvoiceFacts:
    """Everything a ``payment`` row needs to become a tax invoice."""

    invoice_number: str
    invoice_date: dt.date
    tax: GstSplit
    place_of_supply: str
    sac_code: str
    customer_gstin: str | None
    description: str


def place_of_supply_for(settings: Settings, customer_state: str | None) -> str:
    """IGST Act §12(2): for an unregistered recipient whose address we do not hold, the place of
    supply is the supplier's location. A customer who tells us their state gets that instead."""
    return customer_state or settings.supplier_state or "Unknown"


def compute_tax(settings: Settings, amount_inr: Decimal, *, place_of_supply: str) -> GstSplit:
    """docs/01 §1's prices are what the customer is charged, so they are tax-inclusive."""
    intra_state = place_of_supply == (settings.supplier_state or place_of_supply)
    return split_inclusive(
        amount_inr, rate_percent=settings.gst_rate_percent, intra_state=intra_state
    )


def describe(plan: Plan, period_end: dt.datetime | None) -> str:
    """What the line item says. The plan code is the only name docs/04 gives a plan."""
    del period_end
    label = plan.code.replace("_", " ").title()
    kind = "subscription" if plan.interval else "plan"
    return f"Decile {label} {kind}"


@dataclass(frozen=True, slots=True)
class BillingPeriod:
    """What the subscription covers. Both ``None`` for a one-time purchase."""

    start: dt.date | None = None
    end: dt.date | None = None


#: "No period" — a one-time purchase. A module-level singleton because a frozen dataclass with no
#: fields set is a constant, and ruff rightly refuses a constructor call in a default.
NO_PERIOD: Final = BillingPeriod()


def build_document(
    settings: Settings,
    *,
    user: AppUser,
    facts: InvoiceFacts,
    payment_reference: str | None,
    period: BillingPeriod = NO_PERIOD,
) -> InvoiceDocument:
    return InvoiceDocument(
        invoice_number=facts.invoice_number,
        invoice_date=facts.invoice_date,
        supplier=Party(
            name=settings.supplier_legal_name,
            address_lines=tuple(settings.supplier_address_lines),
            gstin=settings.supplier_gstin or None,
            state=settings.supplier_state or None,
        ),
        recipient=Party(
            name=user.name or user.email,
            gstin=facts.customer_gstin,
            email=user.email,
        ),
        description=facts.description,
        sac_code=facts.sac_code,
        place_of_supply=facts.place_of_supply,
        tax=facts.tax,
        period_start=period.start,
        period_end=period.end,
        payment_reference=payment_reference,
    )


def build_invoice_archive(settings: Settings) -> RawArchive:
    """Where invoice PDFs go: R2 when the bucket is configured, a directory otherwise.

    ``decile_providers.factory.build_archive`` already makes exactly that choice for the raw-file
    archive (docs/09), with R2 as the target docs/02 §"Object storage" locks. Reusing it means one
    storage abstraction rather than two, and means local development exercises the same
    store-then-read path a deployment does.

    Built once per process in ``create_app``'s lifespan and kept on ``app.state``; an S3 client
    per invoice would open a new connection pool each time.
    """
    return build_archive(get_provider_settings(), local_root=Path(settings.invoice_local_dir))


async def store_pdf(archive: RawArchive, key: str, payload: bytes) -> str:
    """Write the PDF. boto3 is synchronous, so it is moved off the event loop."""
    return await anyio.to_thread.run_sync(
        lambda: archive.put(key, payload, content_type="application/pdf")
    )


async def load_pdf(archive: RawArchive, key: str) -> bytes:
    return await anyio.to_thread.run_sync(lambda: archive.get(key))


@dataclass(frozen=True, slots=True)
class InvoiceRequest:
    """One invoice to raise. A record rather than six keyword arguments."""

    payment: Payment
    user: AppUser
    plan: Plan
    #: Where the PDF is written. ``create_app`` builds one per process onto ``app.state``.
    archive: RawArchive
    period: BillingPeriod = NO_PERIOD
    #: The date of issue. Defaults to today in IST.
    on: dt.date | None = None


async def issue_invoice(
    session: AsyncSession, settings: Settings, request: InvoiceRequest
) -> InvoiceFacts:
    """Number it, tax it, render it, store it, and stamp the ``payment`` row.

    Called inside the transaction that created the payment, which is what makes the number
    gapless. If the PDF cannot be stored the whole thing rolls back, taking the number with it —
    a payment recorded with an invoice number whose document does not exist would be worse.
    """
    payment = request.payment
    issued_on = request.on or today_ist()
    place = place_of_supply_for(settings, payment.place_of_supply)
    tax = compute_tax(settings, payment.amount_inr, place_of_supply=place)
    number = await allocate_invoice_number(session, settings, on=issued_on)

    facts = InvoiceFacts(
        invoice_number=number,
        invoice_date=issued_on,
        tax=tax,
        place_of_supply=place,
        sac_code=settings.gst_sac_code,
        customer_gstin=payment.customer_gstin,
        description=describe(request.plan, None),
    )

    document = build_document(
        settings,
        user=request.user,
        facts=facts,
        payment_reference=payment.razorpay_payment_id,
        period=request.period,
    )
    key = invoice_object_key(settings, number)
    await store_pdf(request.archive, key, render_invoice_pdf(document))

    payment.invoice_number = number
    payment.invoice_date = issued_on
    payment.invoice_pdf_key = key
    payment.taxable_inr = tax.taxable_inr
    payment.cgst_inr = tax.cgst_inr
    payment.sgst_inr = tax.sgst_inr
    payment.igst_inr = tax.igst_inr
    payment.gst_inr = tax.tax_inr
    payment.gst_rate = tax.rate_percent
    payment.place_of_supply = place
    payment.sac_code = settings.gst_sac_code
    await session.flush()
    return facts


async def load_invoice(session: AsyncSession, user_id: int, invoice_number: str) -> Payment | None:
    """One invoice, if it belongs to this account. Not-yours reads as absent."""
    return (
        await session.execute(
            select(Payment).where(
                Payment.user_id == user_id, Payment.invoice_number == invoice_number
            )
        )
    ).scalar_one_or_none()
