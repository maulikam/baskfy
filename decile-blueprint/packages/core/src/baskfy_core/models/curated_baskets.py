"""Curated-basket product storage — ``docs/smallcase/03-data-model.md``.

These classes describe metadata only.  In particular, importing this module performs no I/O:
services own every query and write, while basket construction and accounting stay pure in core.

The database stores controlled vocabularies as checked strings rather than PostgreSQL enums.
That matches the rest of Baskfy's schema, keeps migrations reversible, and makes adding a value
an ordinary constraint migration instead of an irreversible enum-type mutation.
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
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from baskfy_core.models.base import Base, BigIntPk, CreatedAt, JsonObject

MANAGER_KINDS: tuple[str, ...] = ("ENGINE", "HUMAN", "EXTERNAL")
BASKET_TYPES: tuple[str, ...] = ("STOCK", "MF", "US")
BASKET_ACCESS_LEVELS: tuple[str, ...] = ("FREE", "FEE")
BASKET_VISIBILITIES: tuple[str, ...] = ("PUBLISHED", "PRIVATE")
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
PENDING_ACTION_TYPES: tuple[str, ...] = (
    "DRIFT",
    "REBALANCE_AVAILABLE",
    "SIP_DUE",
    "GENERIC",
)
UPDATE_POST_SOURCES: tuple[str, ...] = ("ENGINE", "HUMAN")
REBALANCE_STATES: tuple[str, ...] = ("APPLIED", "SKIPPED", "PENDING")
PLAN_DURATIONS: tuple[str, ...] = ("M1", "M3", "M6", "Y1")
CURATED_SUBSCRIPTION_STATUSES: tuple[str, ...] = ("ACTIVE", "CANCELLED", "LAPSED")

# Product-layer precision.  Price-like index values retain four decimals; return series and
# volatility retain ten because they feed chained calculations.  Rounding to the displayed two
# decimal places happens only at the write boundary described by docs/smallcase/04.
CB_MONEY = Numeric(20, 2)
CB_PRICE = Numeric(18, 4)
CB_QUANTITY = Numeric(20, 4)
CB_RETURN = Numeric(18, 10)
CB_WEIGHT = Numeric(7, 4)


class CuratedManager(Base):
    """A basket publisher: Baskfy's engine, the owner, or a future external manager."""

    __tablename__ = "cb_manager"
    __table_args__ = (
        CheckConstraint("kind IN ('ENGINE', 'HUMAN', 'EXTERNAL')", name="cb_manager_kind"),
    )

    id: Mapped[BigIntPk]
    slug: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    sebi_reg_no: Mapped[str | None] = mapped_column(String)
    bio: Mapped[str] = mapped_column(String, nullable=False)
    strategies: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    disclosures_md: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[CreatedAt]


class CuratedBasket(Base):
    """A published or private model portfolio whose history lives in immutable versions."""

    __tablename__ = "cb_basket"
    __table_args__ = (
        CheckConstraint("type IN ('STOCK', 'MF', 'US')", name="cb_basket_type"),
        CheckConstraint("access IN ('FREE', 'FEE')", name="cb_basket_access"),
        CheckConstraint("visibility IN ('PUBLISHED', 'PRIVATE')", name="cb_basket_visibility"),
        CheckConstraint(
            "rebalance_frequency IN ('WEEKLY', 'MONTHLY', 'QUARTERLY', 'ANNUAL', 'NEED_BASIS')",
            name="cb_basket_rebalance_frequency",
        ),
        CheckConstraint("source IN ('SCAN', 'MANUAL')", name="cb_basket_source"),
        Index("ix_cb_basket_manager_id", "manager_id"),
        Index("ix_cb_basket_benchmark_instrument_id", "benchmark_instrument_id"),
        Index("ix_cb_basket_catalog", "visibility", "archived_at", "launched_at"),
    )

    id: Mapped[BigIntPk]
    slug: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    manager_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("cb_manager.id"), nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    access: Mapped[str] = mapped_column(String, nullable=False)
    visibility: Mapped[str] = mapped_column(String, nullable=False)
    categories: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    description_md: Mapped[str] = mapped_column(String, nullable=False)
    rationale_md: Mapped[str] = mapped_column(String, nullable=False)
    rebalance_frequency: Mapped[str] = mapped_column(String, nullable=False)
    benchmark_instrument_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("instrument.id")
    )
    launched_at: Mapped[dt.date] = mapped_column(Date, nullable=False)
    next_review_at: Mapped[dt.date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String, nullable=False)
    scan_strategy_key: Mapped[str | None] = mapped_column(String)
    archived_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class BasketVersion(Base):
    """An append-only constituent snapshot effective from one trading date."""

    __tablename__ = "cb_basket_version"
    __table_args__ = (
        CheckConstraint(
            "label IN ('CHANGED', 'NO_CHANGE', 'GENESIS')", name="cb_basket_version_label"
        ),
        CheckConstraint("version_no > 0", name="cb_basket_version_version_positive"),
        CheckConstraint("added_count >= 0", name="cb_basket_version_added_non_negative"),
        CheckConstraint("removed_count >= 0", name="cb_basket_version_removed_non_negative"),
        UniqueConstraint("basket_id", "version_no", name="uq_cb_basket_version_basket_version_no"),
        UniqueConstraint(
            "basket_id",
            "effective_date",
            name="uq_cb_basket_version_basket_effective_date",
        ),
        Index("ix_cb_basket_version_source_scan_run_id", "source_scan_run_id"),
    )

    id: Mapped[BigIntPk]
    basket_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("cb_basket.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    added_count: Mapped[int] = mapped_column(Integer, nullable=False)
    removed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    notes_md: Mapped[str | None] = mapped_column(String)
    source_scan_run_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("screen_run.id"))
    created_at: Mapped[CreatedAt]


class BasketConstituent(Base):
    """One target holding in a version; total weight is validated by the write service."""

    __tablename__ = "cb_constituent"
    __table_args__ = (
        CheckConstraint("weight > 0 AND weight <= 1", name="cb_constituent_weight_range"),
        UniqueConstraint("version_id", "instrument_id"),
        Index("ix_cb_constituent_instrument_id", "instrument_id"),
    )

    id: Mapped[BigIntPk]
    version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cb_basket_version.id"), nullable=False
    )
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.id"), nullable=False
    )
    segment: Mapped[str] = mapped_column(String, nullable=False)
    weight: Mapped[Decimal] = mapped_column(CB_WEIGHT, nullable=False)


class BasketMetrics(Base):
    """Recomputable EOD catalog metrics for one basket and trading date."""

    __tablename__ = "cb_metrics"
    __table_args__ = (
        PrimaryKeyConstraint("basket_id", "as_of_date"),
        CheckConstraint(
            "volatility_bucket IN ('LOW', 'MED', 'HIGH')", name="cb_metrics_volatility_bucket"
        ),
        CheckConstraint("min_amount >= 0", name="cb_metrics_min_amount_non_negative"),
        CheckConstraint("volatility_value >= 0", name="cb_metrics_volatility_value_non_negative"),
    )

    basket_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cb_basket.id", ondelete="CASCADE")
    )
    as_of_date: Mapped[dt.date] = mapped_column(Date)
    min_amount: Mapped[Decimal] = mapped_column(CB_MONEY, nullable=False)
    volatility_bucket: Mapped[str] = mapped_column(String, nullable=False)
    volatility_value: Mapped[Decimal] = mapped_column(CB_RETURN, nullable=False)
    ret_1m: Mapped[Decimal | None] = mapped_column(CB_RETURN)
    ret_6m: Mapped[Decimal | None] = mapped_column(CB_RETURN)
    ret_1y: Mapped[Decimal | None] = mapped_column(CB_RETURN)
    cagr_3y: Mapped[Decimal | None] = mapped_column(CB_RETURN)
    cagr_5y: Mapped[Decimal | None] = mapped_column(CB_RETURN)
    since_inception_pct: Mapped[Decimal | None] = mapped_column(CB_RETURN)
    computed_at: Mapped[CreatedAt]


class BasketCollection(Base):
    """A manually curated catalog row; basket ids preserve display order."""

    __tablename__ = "cb_collection"

    id: Mapped[BigIntPk]
    slug: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    subtitle: Mapped[str] = mapped_column(String, nullable=False)
    basket_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    curated_meta: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)


class BasketWatchlistItem(Base):
    """One user's watch baseline for one basket."""

    __tablename__ = "cb_watchlist_item"
    __table_args__ = (
        UniqueConstraint("user_id", "basket_id"),
        Index("ix_cb_watchlist_item_basket_id", "basket_id"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    basket_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cb_basket.id", ondelete="CASCADE"), nullable=False
    )
    watched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    nav_at_watch: Mapped[Decimal] = mapped_column(CB_PRICE, nullable=False)


class BasketInvestment(Base):
    """The user's lifecycle state for one basket."""

    __tablename__ = "cb_investment"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE', 'EXITED')", name="cb_investment_status"),
        Index("ix_cb_investment_user_id", "user_id"),
        Index("ix_cb_investment_basket_id", "basket_id"),
        Index("ix_cb_investment_version_applied_id", "version_applied_id"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    basket_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("cb_basket.id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    version_applied_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cb_basket_version.id"), nullable=False
    )
    created_at: Mapped[CreatedAt]
    exited_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_invested_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BasketInvestmentHolding(Base):
    """Intended holdings, compared with the desk snapshot to detect drift."""

    __tablename__ = "cb_investment_holding"
    __table_args__ = (
        UniqueConstraint("investment_id", "instrument_id"),
        Index("ix_cb_investment_holding_instrument_id", "instrument_id"),
        CheckConstraint("qty >= 0", name="qty_non_negative"),
        CheckConstraint("avg_price >= 0", name="avg_price_non_negative"),
    )

    id: Mapped[BigIntPk]
    investment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cb_investment.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.id"), nullable=False
    )
    qty: Mapped[Decimal] = mapped_column(CB_QUANTITY, nullable=False)
    avg_price: Mapped[Decimal] = mapped_column(CB_PRICE, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class BasketOrderBatch(Base):
    """Order intent and desk-plan linkage; never an execution record.

    The sole user and broker-account identifiers are mandatory even during this single-tenant
    run. ``desk_plan_id`` is deliberately an opaque string: execution owns its own schema, and
    the product layer cannot create a cross-schema path around the execution gateway.
    """

    __tablename__ = "cb_order_batch"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('BUY', 'INVEST_MORE', 'SIP', 'REBALANCE', 'EXIT', "
            "'PARTIAL_EXIT', 'CUSTOMIZE')",
            name="cb_order_batch_kind",
        ),
        CheckConstraint(
            "status IN ('DRAFT', 'PLANNED', 'EXPIRED', 'EXECUTED', 'PARTIAL', 'CANCELLED')",
            name="cb_order_batch_status",
        ),
        CheckConstraint(
            "requested_amount IS NULL OR requested_amount > 0",
            name="cb_order_batch_requested_amount_positive",
        ),
        Index("ix_cb_order_batch_investment_id", "investment_id"),
        Index("ix_cb_order_batch_user_id", "user_id"),
        Index("ix_cb_order_batch_fee_entry_id", "fee_entry_id"),
        Index("ix_cb_order_batch_desk_plan_id", "desk_plan_id"),
    )

    id: Mapped[BigIntPk]
    investment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cb_investment.id"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_user.id"), nullable=False)
    broker_account_id: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    requested_amount: Mapped[Decimal | None] = mapped_column(CB_MONEY)
    desk_plan_id: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False)
    fee_entry_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "cb_fee_ledger.id",
            use_alter=True,
            name="fk_cb_order_batch_fee_entry_id_cb_fee_ledger",
        ),
    )
    created_at: Mapped[CreatedAt]
    executed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class BasketFeeLedger(Base):
    """Computed platform fee for a batch; collection is forbidden in this run."""

    __tablename__ = "cb_fee_ledger"
    __table_args__ = (
        CheckConstraint("base_fee >= 0", name="cb_fee_ledger_base_fee_non_negative"),
        CheckConstraint("gst >= 0", name="cb_fee_ledger_gst_non_negative"),
        CheckConstraint("total >= 0", name="cb_fee_ledger_total_non_negative"),
        Index("ix_cb_fee_ledger_user_id", "user_id"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_user.id"), nullable=False)
    batch_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cb_order_batch.id"), nullable=False, unique=True
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    base_fee: Mapped[Decimal] = mapped_column(CB_MONEY, nullable=False)
    gst: Mapped[Decimal] = mapped_column(CB_MONEY, nullable=False)
    total: Mapped[Decimal] = mapped_column(CB_MONEY, nullable=False)
    collected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    accrued_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class BasketDividend(Base):
    """A corporate-action dividend derived against holdings history."""

    __tablename__ = "cb_dividend"
    __table_args__ = (
        CheckConstraint("source IN ('CORPORATE_ACTIONS')", name="cb_dividend_source"),
        CheckConstraint("amount_per_share >= 0", name="cb_dividend_amount_non_negative"),
        CheckConstraint("qty_held >= 0", name="cb_dividend_qty_non_negative"),
        CheckConstraint("total >= 0", name="cb_dividend_total_non_negative"),
        UniqueConstraint("investment_id", "instrument_id", "ex_date"),
        Index("ix_cb_dividend_instrument_id", "instrument_id"),
    )

    id: Mapped[BigIntPk]
    investment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cb_investment.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.id"), nullable=False
    )
    ex_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    amount_per_share: Mapped[Decimal] = mapped_column(CB_PRICE, nullable=False)
    qty_held: Mapped[Decimal] = mapped_column(CB_QUANTITY, nullable=False)
    total: Mapped[Decimal] = mapped_column(CB_MONEY, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)


class BasketSipPlan(Base):
    """A reminder-only SIP schedule. AUTO is intentionally absent before D3."""

    __tablename__ = "cb_sip_plan"
    __table_args__ = (
        CheckConstraint("mode IN ('REMINDER')", name="cb_sip_plan_mode"),
        CheckConstraint("status IN ('ACTIVE', 'PAUSED')", name="cb_sip_plan_status"),
        CheckConstraint("amount > 0", name="cb_sip_plan_amount_positive"),
        CheckConstraint("day_of_month BETWEEN 1 AND 31", name="cb_sip_plan_day_of_month"),
        Index("ix_cb_sip_plan_investment_id", "investment_id"),
        Index("ix_cb_sip_plan_next_fire_date", "next_fire_date"),
    )

    id: Mapped[BigIntPk]
    investment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cb_investment.id", ondelete="CASCADE"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(CB_MONEY, nullable=False)
    day_of_month: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    next_fire_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    created_at: Mapped[CreatedAt]


class BasketPendingAction(Base):
    """One actionable or informational notification for the sole user."""

    __tablename__ = "cb_pending_action"
    __table_args__ = (
        CheckConstraint(
            "type IN ('DRIFT', 'REBALANCE_AVAILABLE', 'SIP_DUE', 'GENERIC')",
            name="cb_pending_action_type",
        ),
        Index("ix_cb_pending_action_user_id", "user_id"),
        Index("ix_cb_pending_action_user_unresolved", "user_id", "resolved_at"),
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


class BasketUpdatePost(Base):
    """Manager or engine copy shown on the update feed."""

    __tablename__ = "cb_update_post"
    __table_args__ = (
        CheckConstraint("source IN ('ENGINE', 'HUMAN')", name="cb_update_post_source"),
        CheckConstraint(
            "basket_id IS NOT NULL OR manager_id IS NOT NULL", name="cb_update_post_owner"
        ),
        Index("ix_cb_update_post_basket_id", "basket_id"),
        Index("ix_cb_update_post_manager_id", "manager_id"),
        Index("ix_cb_update_post_published_at", "published_at"),
    )

    id: Mapped[BigIntPk]
    basket_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("cb_basket.id"))
    manager_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("cb_manager.id"))
    title: Mapped[str] = mapped_column(String, nullable=False)
    body_md: Mapped[str] = mapped_column(String, nullable=False)
    published_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    source: Mapped[str] = mapped_column(String, nullable=False)


class BasketUserRebalanceState(Base):
    """Per-user decision for an immutable basket version."""

    __tablename__ = "cb_user_rebalance_state"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "version_id"),
        CheckConstraint(
            "state IN ('APPLIED', 'SKIPPED', 'PENDING')", name="cb_user_rebalance_state_state"
        ),
        Index("ix_cb_user_rebalance_state_version_id", "version_id"),
    )

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"))
    version_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("cb_basket_version.id"))
    state: Mapped[str] = mapped_column(String, nullable=False)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class BasketPlan(Base):
    """Dormant Track-B entitlement plan; SC10 keeps every surface unreachable by default."""

    __tablename__ = "cb_plan"
    __table_args__ = (
        CheckConstraint("duration IN ('M1', 'M3', 'M6', 'Y1')", name="cb_plan_duration"),
        CheckConstraint("price >= 0", name="cb_plan_price_non_negative"),
        CheckConstraint("basket_id IS NOT NULL OR manager_id IS NOT NULL", name="cb_plan_owner"),
        Index("ix_cb_plan_basket_id", "basket_id"),
        Index("ix_cb_plan_manager_id", "manager_id"),
    )

    id: Mapped[BigIntPk]
    basket_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("cb_basket.id"))
    manager_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("cb_manager.id"))
    duration: Mapped[str] = mapped_column(String, nullable=False)
    price: Mapped[Decimal] = mapped_column(CB_MONEY, nullable=False)


class BasketSubscription(Base):
    """Dormant Track-B entitlement state; no charge or payment identifiers exist here."""

    __tablename__ = "cb_subscription"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'CANCELLED', 'LAPSED')", name="cb_subscription_status"
        ),
        Index("ix_cb_subscription_user_id", "user_id"),
        Index("ix_cb_subscription_plan_id", "plan_id"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    plan_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("cb_plan.id"), nullable=False)
    start_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    renew_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    auto_renew: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
