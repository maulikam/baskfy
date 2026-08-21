"""Billing tables that docs/04 does not define — Prompt 13.

docs/04's billing section defines ``plan``, ``subscription`` and ``payment``, and stops there. Two
requirements in the bundle cannot be met with those three tables alone:

============================  ==============================================================
``webhook_event``             docs/11 §Security: "Razorpay webhooks: verify signature, **dedupe
                              by event id**, process idempotently", and docs/07: "POST
                              /webhooks/razorpay (signature-verified, **idempotent by event
                              id**)". Deduplication needs somewhere to remember the ids already
                              seen; Redis is the wrong place, because a cache flush would let
                              every past event be replayed into a second payment row.
``invoice_counter``           PROMPTS.md Prompt 13 acceptance: "Invoice numbers are **gapless**
                              and unique under concurrent payment creation". A PostgreSQL
                              ``SEQUENCE`` is explicitly *not* gapless — it is non-transactional
                              so that concurrent callers never block, which means a rolled-back
                              transaction burns its number. Rule 46(b) of the CGST Rules wants a
                              consecutive series. A single counter row, taken with ``UPDATE ...
                              RETURNING`` (which holds a row lock for the rest of the
                              transaction), is the standard way to buy that: allocation
                              serialises with the insert that uses it.
============================  ==============================================================

Plus the GST columns on ``payment``. docs/04 gives it ``gst_inr`` alone; docs/11 §Compliance
requires "GSTIN, HSN/SAC, place of supply" on the invoice, and an invoice whose figures are
recomputed at render time rather than stored is an invoice that can silently change after it was
issued. So the split is stored once, when the payment is recorded.

Written up in ``docs/DECISIONS.md`` (Prompt 13).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import BigInteger, CheckConstraint, DateTime, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from baskfy_core.models.base import Base, BigIntPk, CreatedAt, JsonObject

#: What a delivered webhook is doing. ``received`` exists for the window between the row being
#: claimed and the handler finishing — a crash in between leaves evidence rather than silence.
WEBHOOK_STATUSES: tuple[str, ...] = ("received", "processed", "ignored", "failed")

#: docs/02: "Payments — Razorpay". One column rather than one table per gateway.
RAZORPAY_PROVIDER: str = "razorpay"


class WebhookEvent(Base):
    """One delivery from a payment gateway, remembered by its own event id.

    ``event_id`` is UNIQUE, and that constraint *is* the idempotency: the handler inserts first
    with ``ON CONFLICT DO NOTHING`` and does nothing further if the insert returned no row. Five
    deliveries of the same event therefore produce one payment, which is Prompt 13's first
    acceptance criterion.

    The raw ``payload`` is kept because it is the only record of what the gateway actually said;
    reconciling a disputed charge six months later against a summary we derived is not
    reconciling.
    """

    __tablename__ = "webhook_event"
    __table_args__ = (
        CheckConstraint(
            "status IN ('received', 'processed', 'ignored', 'failed')", name="webhook_event_status"
        ),
    )

    id: Mapped[BigIntPk]
    provider: Mapped[str] = mapped_column(String, nullable=False, default=RAZORPAY_PROVIDER)
    #: The gateway's own event id (Razorpay's ``x-razorpay-event-id`` header / ``payload.id``).
    event_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="received")
    #: Why a delivery was ignored or failed. Never a secret: the signature is not stored.
    note: Mapped[str | None] = mapped_column(String)
    received_at: Mapped[CreatedAt]
    processed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class InvoiceCounter(Base):
    """The next invoice number in a series. One row per financial year.

    ``series`` is the Indian financial year (``2026-27``), because Rule 46(b) requires the number
    to be unique within one, and a series that never restarts makes the year unreadable from the
    number.
    """

    __tablename__ = "invoice_counter"

    series: Mapped[str] = mapped_column(String, primary_key=True)
    #: The number the *next* invoice in this series will take. Starts at 1.
    next_value: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
