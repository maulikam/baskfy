"""Billing: webhook dedupe, the invoice counter, and the GST columns docs/11 requires (Prompt 13).

Two tables and eleven columns, none of which are in docs/04's DDL:

    webhook_event            docs/11 §Security: "Razorpay webhooks: verify signature, dedupe by
                             event id, process idempotently". The UNIQUE on `event_id` is the
                             deduplication.
    invoice_counter          Prompt 13 acceptance: "Invoice numbers are gapless and unique under
                             concurrent payment creation". A PostgreSQL SEQUENCE is deliberately
                             not gapless; a counter row taken with UPDATE ... RETURNING is.
    payment.<gst columns>    docs/11 §Compliance: "GST-compliant invoices with GSTIN, HSN/SAC,
                             place of supply", plus the taxable value and per-head tax amounts
                             Rule 46 of the CGST Rules requires on the face of the document.

Also a CHECK on `payment.status`, which docs/04 leaves unconstrained. The vocabulary is Razorpay's
(`decile_core.models.accounts.PAYMENT_STATUSES`).

Recorded in docs/DECISIONS.md.

Revision ID: 0006_billing_tables
Revises: 0005_auth_tables
Create Date: 2026-08-21 05:20:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_billing_tables"
down_revision: str | None = "0005_auth_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_event",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('received', 'processed', 'ignored', 'failed')",
            name=op.f("ck_webhook_event_webhook_event_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_event")),
        sa.UniqueConstraint("event_id", name=op.f("uq_webhook_event_event_id")),
    )
    op.create_table(
        "invoice_counter",
        sa.Column("series", sa.String(), nullable=False),
        sa.Column("next_value", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("series", name=op.f("pk_invoice_counter")),
    )

    op.add_column("payment", sa.Column("razorpay_order_id", sa.String(), nullable=True))
    op.add_column("payment", sa.Column("invoice_date", sa.Date(), nullable=True))
    op.add_column("payment", sa.Column("taxable_inr", sa.Numeric(12, 2), nullable=True))
    op.add_column("payment", sa.Column("cgst_inr", sa.Numeric(12, 2), nullable=True))
    op.add_column("payment", sa.Column("sgst_inr", sa.Numeric(12, 2), nullable=True))
    op.add_column("payment", sa.Column("igst_inr", sa.Numeric(12, 2), nullable=True))
    op.add_column("payment", sa.Column("gst_rate", sa.Numeric(5, 2), nullable=True))
    op.add_column("payment", sa.Column("place_of_supply", sa.String(), nullable=True))
    op.add_column("payment", sa.Column("customer_gstin", sa.String(), nullable=True))
    op.add_column("payment", sa.Column("sac_code", sa.String(), nullable=True))
    op.create_check_constraint(
        op.f("ck_payment_payment_status"),
        "payment",
        "status IN ('created', 'captured', 'failed', 'refunded')",
    )
    # A user's invoice list is "their payments, newest first" (docs/07: `GET /invoices`).
    op.create_index(
        op.f("ix_payment_user_id_created_at"), "payment", ["user_id", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_payment_user_id_created_at"), table_name="payment")
    op.drop_constraint(op.f("ck_payment_payment_status"), "payment", type_="check")
    for column in (
        "sac_code",
        "customer_gstin",
        "place_of_supply",
        "gst_rate",
        "igst_inr",
        "sgst_inr",
        "cgst_inr",
        "taxable_inr",
        "invoice_date",
        "razorpay_order_id",
    ):
        op.drop_column("payment", column)
    op.drop_table("invoice_counter")
    op.drop_table("webhook_event")
