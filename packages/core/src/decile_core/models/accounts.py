"""Accounts, billing, portfolios, backtests and pipeline bookkeeping — docs/04-data-model.md."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from decile_core.models.base import (
    INR,
    PRICE_RAW,
    QUANTITY,
    Base,
    BigIntPk,
    CreatedAt,
    JsonObject,
    SmallIntPk,
)

SUBSCRIPTION_STATUSES: tuple[str, ...] = ("active", "past_due", "cancelled", "expired")
BACKTEST_STATUSES: tuple[str, ...] = ("queued", "running", "done", "failed")
PIPELINE_STATUSES: tuple[str, ...] = ("running", "succeeded", "failed", "aborted")
PLAN_INTERVALS: tuple[str, ...] = ("month", "year")
#: docs/04 does not enumerate ``payment.status``. Razorpay's own vocabulary for a payment, minus
#: the states we never persist (``authorized`` is transient; we record a charge once captured).
PAYMENT_STATUSES: tuple[str, ...] = ("created", "captured", "failed", "refunded")


class AppUser(Base):
    __tablename__ = "app_user"

    id: Mapped[BigIntPk]
    public_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    #: citext so e-mail uniqueness is case-insensitive without a functional index (docs/04).
    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    #: argon2id; NULL for OTP-only users.
    password_hash: Mapped[str | None] = mapped_column(String)
    name: Mapped[str | None] = mapped_column(String)
    email_verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[CreatedAt]
    #: Set when erasure is requested (Prompt 12 §5, DPDP). The account stops authenticating
    #: immediately; `account_deletion.purge_after` is when the rows actually go. NOT in docs/04 —
    #: see `docs/04c`. Deleting on the spot would be irreversible on a misclick.
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class Plan(Base):
    __tablename__ = "plan"
    __table_args__ = (
        CheckConstraint("interval IS NULL OR interval IN ('month', 'year')", name="plan_interval"),
    )

    id: Mapped[SmallIntPk]
    code: Mapped[str] = mapped_column(String, unique=True)
    price_inr: Mapped[Decimal] = mapped_column(INR, nullable=False)
    #: 'month' | 'year' | NULL (NULL = the one-time "Forever" plan).
    interval: Mapped[str | None] = mapped_column(String)
    features: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)


class Subscription(Base):
    __tablename__ = "subscription"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'past_due', 'cancelled', 'expired')", name="subscription_status"
        ),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_user.id"), nullable=False)
    plan_id: Mapped[int] = mapped_column(SmallInteger, ForeignKey("plan.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_period_end: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    razorpay_subscription_id: Mapped[str | None] = mapped_column(String)
    razorpay_customer_id: Mapped[str | None] = mapped_column(String)


class Payment(Base):
    """One charge, and the tax invoice raised for it — docs/04, plus the GST columns docs/11 needs.

    docs/04 gives this table ``gst_inr`` and nothing else about tax. docs/11 §"Compliance & legal
    (India)" requires "GST-compliant invoices with GSTIN, HSN/SAC, place of supply", and Rule 46
    of the CGST Rules requires the taxable value and each tax head to appear on the document.
    Those figures are **stored**, not recomputed at render time: an invoice is a document of
    record, and one whose numbers move when a rate setting changes is not one. The added columns
    are listed in ``docs/DECISIONS.md`` (Prompt 13) alongside ``decile_core.models.billing``.

    ``amount_inr`` is the gross the customer was charged, so
    ``taxable_inr + cgst_inr + sgst_inr + igst_inr == amount_inr`` exactly (docs/01 §1's prices
    are tax-inclusive — see ``decile_core.gst``).
    """

    __tablename__ = "payment"
    __table_args__ = (
        CheckConstraint(
            "status IN ('created', 'captured', 'failed', 'refunded')", name="payment_status"
        ),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_user.id"), nullable=False)
    subscription_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("subscription.id"))
    amount_inr: Mapped[Decimal] = mapped_column(INR, nullable=False)
    gst_inr: Mapped[Decimal | None] = mapped_column(INR)
    status: Mapped[str] = mapped_column(String, nullable=False)
    razorpay_payment_id: Mapped[str | None] = mapped_column(String, unique=True)
    invoice_number: Mapped[str | None] = mapped_column(String, unique=True)
    invoice_pdf_key: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[CreatedAt]

    # --- not in docs/04; required by docs/11 §Compliance (see the class docstring) ---------
    #: The order the charge belongs to, for reconciliation against the gateway.
    razorpay_order_id: Mapped[str | None] = mapped_column(String)
    #: Rule 46(a): the date of issue. Distinct from ``created_at``, which is a timestamp in UTC.
    invoice_date: Mapped[dt.date | None] = mapped_column(Date)
    taxable_inr: Mapped[Decimal | None] = mapped_column(INR)
    cgst_inr: Mapped[Decimal | None] = mapped_column(INR)
    sgst_inr: Mapped[Decimal | None] = mapped_column(INR)
    igst_inr: Mapped[Decimal | None] = mapped_column(INR)
    #: The rate applied, stored so a later rate change cannot rewrite an issued invoice.
    gst_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    #: docs/11: "place of supply", spelled as the return expects it: ``Karnataka (29)``.
    place_of_supply: Mapped[str | None] = mapped_column(String)
    #: docs/11: "GSTIN" — the *recipient's*, when they are registered. The supplier's is a setting.
    customer_gstin: Mapped[str | None] = mapped_column(String)
    #: docs/11: "HSN/SAC". A service, so SAC.
    sac_code: Mapped[str | None] = mapped_column(String)


class Portfolio(Base):
    __tablename__ = "portfolio"

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_user.id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[CreatedAt]


class PortfolioHolding(Base):
    __tablename__ = "portfolio_holding"
    __table_args__ = (PrimaryKeyConstraint("portfolio_id", "instrument_id"),)

    portfolio_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("portfolio.id", ondelete="CASCADE")
    )
    instrument_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("instrument.id"))
    quantity: Mapped[Decimal | None] = mapped_column(QUANTITY)
    avg_price: Mapped[Decimal | None] = mapped_column(PRICE_RAW)
    added_on: Mapped[dt.date] = mapped_column(Date, nullable=False)


class Backtest(Base):
    __tablename__ = "backtest"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed')", name="backtest_status"
        ),
    )

    id: Mapped[BigIntPk]
    public_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_user.id"), nullable=False)
    screen_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("screen.id"))
    config: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    metrics: Mapped[JsonObject | None] = mapped_column(JSONB)
    equity_curve: Mapped[JsonObject | None] = mapped_column(JSONB)
    #: Large artefacts (per-trade blotter) live in R2; this is the object key.
    trades_key: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[CreatedAt]
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class PipelineRun(Base):
    __tablename__ = "pipeline_run"

    id: Mapped[BigIntPk]
    trade_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: Bumped only by step 10 "publish", and only if the quality gate passed (docs/03).
    data_version: Mapped[int | None] = mapped_column(BigInteger)


class PipelineRunStep(Base):
    """One step of one run.

    ``UNIQUE (run_id, step)`` is an addition to docs/04's DDL. PROMPTS.md Prompt 3 deliverable 2
    requires every task to be "an idempotent unit that upserts (never blind-inserts) and writes a
    pipeline_run_step row"; without the constraint, re-running a step appends a second,
    contradictory record and `/admin/pipeline` (docs/09 §Observability) shows two answers for the
    same question. See docs/04b-ingest-cursor-addendum.md.
    """

    __tablename__ = "pipeline_run_step"
    __table_args__ = (UniqueConstraint("run_id", "step"),)

    id: Mapped[BigIntPk]
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("pipeline_run.id", ondelete="CASCADE"), nullable=False
    )
    step: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    rows_in: Mapped[int | None] = mapped_column(BigInteger)
    rows_out: Mapped[int | None] = mapped_column(BigInteger)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[JsonObject | None] = mapped_column(JSONB)
