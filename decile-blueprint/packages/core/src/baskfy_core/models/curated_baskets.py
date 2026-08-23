"""Curated-basket product tables — docs/smallcase/03-data-model.md.

The ``cb_`` prefix separates this layer from the screener schema and the desk schema it joins.
Track B tables (``cb_plan``, ``cb_subscription``) are created here and exercised only when
``BASKFY_SUBSCRIPTIONS_ENABLED`` is on (docs/smallcase/02-scope-and-gating.md).
"""

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
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from baskfy_core.models.base import (
    INR,
    MONEY,
    PCT_2DP,
    PRICE,
    QUANTITY,
    RATIO_10DP,
    Base,
    BigIntPk,
    CreatedAt,
    JsonObject,
    UpdatedAt,
)

#: ``cb_constituent.weight`` — docs/smallcase/03 specifies numeric(7,4).
CONSTITUENT_WEIGHT = Numeric(7, 4)

MANAGER_KINDS: tuple[str, ...] = ("ENGINE", "HUMAN", "EXTERNAL")
BASKET_TYPES: tuple[str, ...] = ("STOCK", "MF", "US")
BASKET_ACCESS: tuple[str, ...] = ("FREE", "FEE")
BASKET_VISIBILITY: tuple[str, ...] = ("PUBLISHED", "PRIVATE")
REBALANCE_FREQUENCIES: tuple[str, ...] = (
    "WEEKLY",
    "MONTHLY",
    "QUARTERLY",
    "ANNUAL",
    "NEED_BASIS",
)
BASKET_SOURCES: tuple[str, ...] = ("SCAN", "MANUAL")
VERSION_LABELS: tuple[str, ...] = ("CHANGED", "NO_CHANGE", "GENESIS")
VOLATILITY_BUCKETS: tuple[str, ...] = ("LOW", "MED", "HIGH")
#: Which series a published ``volatility_value`` was measured on. Mirrors
#: ``baskfy_core.curated_metrics.VolatilityBasis`` — the job writes those literals, so a
#: check constraint spelled any other way would reject every row it produces.
VOLATILITY_BASES: tuple[str, ...] = (
    "BASKET_252D",
    "BASKET_FULL_HISTORY",
    "CONSTITUENT_WEIGHTED",
)
#: A6 disclosure. ``PRICE_RETURN`` is what the catalog computes today
#: (``baskfy_core.curated_metrics.RETURN_CONVENTION``); ``TOTAL_RETURN`` is admissible so a
#: dividend-inclusive series needs no migration, and nothing writes it yet.
RETURN_CONVENTIONS: tuple[str, ...] = ("PRICE_RETURN", "TOTAL_RETURN")
INVESTMENT_STATUSES: tuple[str, ...] = ("ACTIVE", "EXITED")
ORDER_BATCH_KINDS: tuple[str, ...] = (
    "BUY",
    "INVEST_MORE",
    "SIP",
    "REBALANCE",
    "EXIT",
    "PARTIAL_EXIT",
    "CUSTOMIZE",
)
ORDER_BATCH_STATUSES: tuple[str, ...] = (
    "DRAFT",
    "PLANNED",
    "EXPIRED",
    "EXECUTED",
    "PARTIAL",
    "CANCELLED",
)
DIVIDEND_SOURCES: tuple[str, ...] = ("CORPORATE_ACTIONS",)
SIP_MODES: tuple[str, ...] = ("REMINDER",)
SIP_STATUSES: tuple[str, ...] = ("ACTIVE", "PAUSED")
PENDING_ACTION_TYPES: tuple[str, ...] = ("DRIFT", "REBALANCE_AVAILABLE", "SIP_DUE", "GENERIC")
UPDATE_POST_SOURCES: tuple[str, ...] = ("ENGINE", "HUMAN")
REBALANCE_STATES: tuple[str, ...] = ("APPLIED", "SKIPPED", "PENDING")
CB_PLAN_DURATIONS: tuple[str, ...] = ("M1", "M3", "M6", "Y1")
CB_SUBSCRIPTION_STATUSES: tuple[str, ...] = ("ACTIVE", "CANCELLED", "LAPSED")


def _in_check(name: str, column: str, values: tuple[str, ...]) -> CheckConstraint:
    quoted = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({quoted})", name=name)


class CbManager(Base):
    __tablename__ = "cb_manager"
    __table_args__ = (_in_check("cb_manager_kind", "kind", MANAGER_KINDS),)

    id: Mapped[BigIntPk]
    slug: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    sebi_reg_no: Mapped[str | None] = mapped_column(String)
    bio: Mapped[str | None] = mapped_column(Text)
    strategies: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default=text("'{}'")
    )
    disclosures_md: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[CreatedAt]


class CbBasket(Base):
    __tablename__ = "cb_basket"
    __table_args__ = (
        _in_check("cb_basket_type", "type", BASKET_TYPES),
        _in_check("cb_basket_access", "access", BASKET_ACCESS),
        _in_check("cb_basket_visibility", "visibility", BASKET_VISIBILITY),
        _in_check("cb_basket_rebalance_frequency", "rebalance_frequency", REBALANCE_FREQUENCIES),
        _in_check("cb_basket_source", "source", BASKET_SOURCES),
        Index("ix_cb_basket_manager_id", "manager_id"),
    )

    id: Mapped[BigIntPk]
    slug: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    manager_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("cb_manager.id"), nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    access: Mapped[str] = mapped_column(String, nullable=False)
    visibility: Mapped[str] = mapped_column(String, nullable=False)
    categories: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default=text("'{}'")
    )
    description_md: Mapped[str | None] = mapped_column(Text)
    rationale_md: Mapped[str | None] = mapped_column(Text)
    rebalance_frequency: Mapped[str] = mapped_column(String, nullable=False)
    benchmark_instrument_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("instrument.id")
    )
    launched_at: Mapped[dt.date | None] = mapped_column(Date)
    next_review_at: Mapped[dt.date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String, nullable=False)
    scan_strategy_key: Mapped[str | None] = mapped_column(String)
    archived_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class CbBasketVersion(Base):
    """One immutable cut of a basket. Corrections are a new row, never an UPDATE."""

    __tablename__ = "cb_basket_version"
    __table_args__ = (
        _in_check("cb_basket_version_label", "label", VERSION_LABELS),
        UniqueConstraint("basket_id", "version_no", name="uq_cb_basket_version_basket_version_no"),
        Index("ix_cb_basket_version_basket_id", "basket_id"),
    )

    id: Mapped[BigIntPk]
    basket_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cb_basket.id", ondelete="CASCADE"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    added_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    removed_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    notes_md: Mapped[str | None] = mapped_column(Text)
    source_scan_run_id: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[CreatedAt]


class CbConstituent(Base):
    """Append-only membership for one version. Weight sum is asserted at insert time."""

    __tablename__ = "cb_constituent"
    __table_args__ = (
        Index("ix_cb_constituent_version_id", "version_id"),
        Index("ix_cb_constituent_instrument_id", "instrument_id"),
    )

    id: Mapped[BigIntPk]
    version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cb_basket_version.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.id"), nullable=False
    )
    segment: Mapped[str] = mapped_column(String, nullable=False)
    weight: Mapped[Decimal] = mapped_column(CONSTITUENT_WEIGHT, nullable=False)


class CbMetrics(Base):
    """One row per basket per trading day — written by the EOD metrics job (SC2)."""

    __tablename__ = "cb_metrics"
    __table_args__ = (
        _in_check("cb_metrics_volatility_bucket", "volatility_bucket", VOLATILITY_BUCKETS),
        _in_check("cb_metrics_volatility_basis", "volatility_basis", VOLATILITY_BASES),
        CheckConstraint(
            "months_available IS NULL OR months_available >= 0",
            name="cb_metrics_months_available",
        ),
        _in_check("cb_metrics_return_convention", "return_convention", RETURN_CONVENTIONS),
        PrimaryKeyConstraint("basket_id", "as_of_date"),
    )

    basket_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cb_basket.id", ondelete="CASCADE"), nullable=False
    )
    as_of_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    min_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    volatility_bucket: Mapped[str | None] = mapped_column(String)
    volatility_value: Mapped[Decimal | None] = mapped_column(RATIO_10DP)
    ret_1m: Mapped[Decimal | None] = mapped_column(PCT_2DP)
    ret_6m: Mapped[Decimal | None] = mapped_column(PCT_2DP)
    ret_1y: Mapped[Decimal | None] = mapped_column(PCT_2DP)
    cagr_3y: Mapped[Decimal | None] = mapped_column(PCT_2DP)
    cagr_5y: Mapped[Decimal | None] = mapped_column(PCT_2DP)
    since_inception_pct: Mapped[Decimal | None] = mapped_column(PCT_2DP)
    #: Which series ``volatility_value`` was measured on — a 252-bar window, the basket's whole
    #: (short) history, or the constituent-weighted fallback. NULL when there is no volatility.
    volatility_basis: Mapped[str | None] = mapped_column(Text)
    #: Whole months between the since-inception anchor and ``as_of_date``. NULL, never 0, when
    #: nothing was measured: 0 would claim a measurement.
    months_available: Mapped[int | None] = mapped_column(Integer)
    #: A6: every number in this row is a price return from dividend-free adjusted closes.
    return_convention: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'PRICE_RETURN'")
    )
    dividends_included: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    computed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CbCollection(Base):
    __tablename__ = "cb_collection"

    id: Mapped[BigIntPk]
    slug: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    subtitle: Mapped[str | None] = mapped_column(String)
    basket_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), nullable=False, server_default=text("'{}'")
    )
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    curated_meta: Mapped[JsonObject | None] = mapped_column(JSONB)


class CbWatchlistItem(Base):
    __tablename__ = "cb_watchlist_item"
    __table_args__ = (
        UniqueConstraint("user_id", "basket_id", name="uq_cb_watchlist_item_user_basket"),
        Index("ix_cb_watchlist_item_user_id", "user_id"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    basket_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cb_basket.id", ondelete="CASCADE"), nullable=False
    )
    watched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    nav_at_watch: Mapped[Decimal | None] = mapped_column(PRICE)


class CbInvestment(Base):
    __tablename__ = "cb_investment"
    __table_args__ = (
        _in_check("cb_investment_status", "status", INVESTMENT_STATUSES),
        Index("ix_cb_investment_user_id", "user_id"),
        Index("ix_cb_investment_basket_id", "basket_id"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    basket_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cb_basket.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String, nullable=False)
    version_applied_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cb_basket_version.id"), nullable=False
    )
    created_at: Mapped[CreatedAt]
    exited_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_invested_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class CbInvestmentHolding(Base):
    __tablename__ = "cb_investment_holding"
    __table_args__ = (
        Index("ix_cb_investment_holding_investment_id", "investment_id"),
        Index("ix_cb_investment_holding_instrument_id", "instrument_id"),
    )

    id: Mapped[BigIntPk]
    investment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cb_investment.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.id"), nullable=False
    )
    qty: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    avg_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    updated_at: Mapped[UpdatedAt]


class CbOrderBatch(Base):
    __tablename__ = "cb_order_batch"
    __table_args__ = (
        _in_check("cb_order_batch_kind", "kind", ORDER_BATCH_KINDS),
        _in_check("cb_order_batch_status", "status", ORDER_BATCH_STATUSES),
        Index("ix_cb_order_batch_investment_id", "investment_id"),
    )

    id: Mapped[BigIntPk]
    investment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cb_investment.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    requested_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    desk_plan_id: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False)
    fee_entry_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("cb_fee_ledger.id"))
    created_at: Mapped[CreatedAt]
    executed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class CbFeeLedger(Base):
    __tablename__ = "cb_fee_ledger"
    __table_args__ = (Index("ix_cb_fee_ledger_user_id", "user_id"),)

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    batch_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cb_order_batch.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    base_fee: Mapped[Decimal] = mapped_column(INR, nullable=False)
    gst: Mapped[Decimal] = mapped_column(INR, nullable=False)
    total: Mapped[Decimal] = mapped_column(INR, nullable=False)
    collected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    accrued_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CbDividend(Base):
    __tablename__ = "cb_dividend"
    __table_args__ = (
        _in_check("cb_dividend_source", "source", DIVIDEND_SOURCES),
        Index("ix_cb_dividend_investment_id", "investment_id"),
    )

    id: Mapped[BigIntPk]
    investment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cb_investment.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.id"), nullable=False
    )
    ex_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    amount_per_share: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    qty_held: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    total: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)


class CbSipPlan(Base):
    __tablename__ = "cb_sip_plan"
    __table_args__ = (
        _in_check("cb_sip_plan_mode", "mode", SIP_MODES),
        _in_check("cb_sip_plan_status", "status", SIP_STATUSES),
        Index("ix_cb_sip_plan_investment_id", "investment_id"),
    )

    id: Mapped[BigIntPk]
    investment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cb_investment.id", ondelete="CASCADE"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    day_of_month: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    next_fire_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    created_at: Mapped[CreatedAt]


class CbPendingAction(Base):
    __tablename__ = "cb_pending_action"
    __table_args__ = (
        _in_check("cb_pending_action_type", "type", PENDING_ACTION_TYPES),
        Index("ix_cb_pending_action_user_id", "user_id"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[CreatedAt]
    dismissed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class CbUpdatePost(Base):
    __tablename__ = "cb_update_post"
    __table_args__ = (_in_check("cb_update_post_source", "source", UPDATE_POST_SOURCES),)

    id: Mapped[BigIntPk]
    basket_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("cb_basket.id", ondelete="SET NULL")
    )
    manager_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("cb_manager.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    body_md: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)


class CbUserRebalanceState(Base):
    __tablename__ = "cb_user_rebalance_state"
    __table_args__ = (
        _in_check("cb_user_rebalance_state_state", "state", REBALANCE_STATES),
        PrimaryKeyConstraint("user_id", "version_id"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cb_basket_version.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[str] = mapped_column(String, nullable=False)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class CbPlan(Base):
    """Track B — subscription plan catalogue. Dormant until D3 clears."""

    __tablename__ = "cb_plan"
    __table_args__ = (_in_check("cb_plan_duration", "duration", CB_PLAN_DURATIONS),)

    id: Mapped[BigIntPk]
    basket_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("cb_basket.id", ondelete="SET NULL")
    )
    manager_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("cb_manager.id", ondelete="SET NULL")
    )
    duration: Mapped[str] = mapped_column(String, nullable=False)
    price: Mapped[Decimal] = mapped_column(INR, nullable=False)


class CbSubscription(Base):
    """Track B — user entitlement to a ``cb_plan``. Dormant until D3 clears."""

    __tablename__ = "cb_subscription"
    __table_args__ = (
        _in_check("cb_subscription_status", "status", CB_SUBSCRIPTION_STATUSES),
        Index("ix_cb_subscription_user_id", "user_id"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    plan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cb_plan.id", ondelete="CASCADE"), nullable=False
    )
    start_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    renew_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String, nullable=False)
    auto_renew: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
