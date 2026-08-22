"""Accounts, billing, portfolios, backtests and pipeline bookkeeping — docs/04-data-model.md."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from baskfy_core.models.base import (
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
    #: docs/09 §Observability: `/admin/pipeline` is "behind staff auth", and nothing in the bundle
    #: says what makes an account staff. This is that answer — a server fact, set by hand or by a
    #: migration, never by a self-service form. NOT in docs/04; see `baskfy_core.models.admin`
    #: and `docs/DECISIONS.md` §17.
    is_staff: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )


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
    are listed in ``docs/DECISIONS.md`` (Prompt 13) alongside ``baskfy_core.models.billing``.

    ``amount_inr`` is the gross the customer was charged, so
    ``taxable_inr + cgst_inr + sgst_inr + igst_inr == amount_inr`` exactly (docs/01 §1's prices
    are tax-inclusive — see ``baskfy_core.gst``).
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
    #: The composite primary key leads on ``portfolio_id``, so it cannot serve a predicate on
    #: ``instrument_id`` alone — which is the direction the rebalance join reads.
    __table_args__ = (
        PrimaryKeyConstraint("portfolio_id", "instrument_id"),
        Index("ix_portfolio_holding_instrument_id", "instrument_id"),
    )

    portfolio_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("portfolio.id", ondelete="CASCADE")
    )
    instrument_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("instrument.id"))
    quantity: Mapped[Decimal | None] = mapped_column(QUANTITY)
    avg_price: Mapped[Decimal | None] = mapped_column(PRICE_RAW)
    added_on: Mapped[dt.date] = mapped_column(Date, nullable=False)


class PortfolioRebalance(Base):
    """One computed rebalance, kept so the user can see what they were told and when.

    **NOT IN docs/04.** PROMPTS.md Prompt 14 §4 requires it in as many words — "Rebalance history:
    persist each computed rebalance so a user can see what they were told and when" — and docs/04
    stops at ``portfolio`` and ``portfolio_holding``. Same footing as the auth tables of
    ``docs/04c`` and the billing tables of ``baskfy_core.models.billing``: an addition a numbered
    deliverable asks for, recorded in ``docs/DECISIONS.md`` §14.

    ``payload`` is the response body **verbatim**, not a set of columns to re-render from. The
    point of the history is that it is a record of advice given on a date: the screen can be
    edited, the buffer changed, a name delisted, and this row must still say what the user saw.
    Recomputing it from ``screen_id`` + ``as_of`` would produce today's answer under yesterday's
    timestamp, which is the one thing an audit trail may not do.

    ``screen_id`` is ``ON DELETE SET NULL`` rather than ``CASCADE`` for the same reason: deleting
    a screen must not erase the history of what it once told someone. The screen's name and public
    id are inside ``payload``.
    """

    __tablename__ = "portfolio_rebalance"
    __table_args__ = (Index("ix_portfolio_rebalance_portfolio_id", "portfolio_id"),)

    id: Mapped[BigIntPk]
    portfolio_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("portfolio.id", ondelete="CASCADE"), nullable=False
    )
    screen_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("screen.id", ondelete="SET NULL")
    )
    #: The trading day the screen was run for (docs/06 §step 1's resolved as-of).
    as_of: Mapped[dt.date] = mapped_column(Date, nullable=False)
    #: The published data version behind it, so a stale answer is identifiable as stale.
    data_version: Mapped[int | None] = mapped_column(BigInteger)
    top_n: Mapped[int] = mapped_column(Integer, nullable=False)
    hold_buffer: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[CreatedAt]


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


class PortfolioSleeve(Base):
    """One slice of a portfolio, with its own capital and its own source (M34).

    The Rebalance Tracker is a symbol diff and knows nothing about money. A portfolio run as
    several screens needs the other question answered — how much goes where — and that is what a
    sleeve is.

    ``kind`` is ``screen`` (names come from ``screen_id``) or ``manual`` (capital the owner runs
    themselves, reported so the totals are honest and never allocated). The check constraints make
    the pairing structural rather than conventional.

    Deleting a screen sets ``screen_id`` to NULL rather than cascading: capital allocated against a
    screen that no longer exists should become visibly unsourced, not silently vanish.
    """

    __tablename__ = "portfolio_sleeve"
    __table_args__ = (
        CheckConstraint("kind IN ('screen', 'manual')", name="portfolio_sleeve_kind"),
        CheckConstraint("capital >= 0", name="portfolio_sleeve_capital_non_negative"),
        CheckConstraint("top_n BETWEEN 1 AND 100", name="portfolio_sleeve_top_n"),
        CheckConstraint(
            "(kind = 'screen' AND screen_id IS NOT NULL) OR "
            "(kind = 'manual' AND screen_id IS NULL)",
            name="portfolio_sleeve_source",
        ),
        UniqueConstraint("portfolio_id", "name", name="uq_portfolio_sleeve_name"),
    )

    id: Mapped[BigIntPk]
    portfolio_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("portfolio.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    screen_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("screen.id", ondelete="SET NULL")
    )
    capital: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, server_default="0")
    #: How many of the screen's names this sleeve takes, in rank order.
    top_n: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="15")
    sort_order: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    created_at: Mapped[CreatedAt]
