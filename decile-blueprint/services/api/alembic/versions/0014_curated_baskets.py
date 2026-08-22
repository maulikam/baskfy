"""curated basket product layer — docs/smallcase/03-data-model.md (SC1).

Creates all ``cb_*`` tables including Track B (``cb_plan``, ``cb_subscription``), which stay
dormant until ``BASKFY_SUBSCRIPTIONS_ENABLED`` is flipped (docs/smallcase/02).

Revision ID: 0014_curated_baskets
Revises: 0013_portfolio_sleeves
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_curated_baskets"
down_revision: str | None = "0013_portfolio_sleeves"
branch_labels: str | None = None
depends_on: str | None = None

# Shared enum check fragments — must match baskfy_core.models.curated_baskets.
_MANAGER_KIND = "kind IN ('ENGINE', 'HUMAN', 'EXTERNAL')"
_BASKET_TYPE = "type IN ('STOCK', 'MF', 'US')"
_BASKET_ACCESS = "access IN ('FREE', 'FEE')"
_BASKET_VISIBILITY = "visibility IN ('PUBLISHED', 'PRIVATE')"
_REBALANCE_FREQ = (
    "rebalance_frequency IN ('WEEKLY', 'MONTHLY', 'QUARTERLY', 'ANNUAL', 'NEED_BASIS')"
)
_BASKET_SOURCE = "source IN ('SCAN', 'MANUAL')"
_VERSION_LABEL = "label IN ('CHANGED', 'NO_CHANGE', 'GENESIS')"
_VOL_BUCKET = "volatility_bucket IS NULL OR volatility_bucket IN ('LOW', 'MED', 'HIGH')"
_INVESTMENT_STATUS = "status IN ('ACTIVE', 'EXITED')"
_ORDER_KIND = (
    "kind IN ('BUY', 'INVEST_MORE', 'SIP', 'REBALANCE', 'EXIT', 'PARTIAL_EXIT', 'CUSTOMIZE')"
)
_ORDER_STATUS = (
    "status IN ('DRAFT', 'PLANNED', 'EXPIRED', 'EXECUTED', 'PARTIAL', 'CANCELLED')"
)
_DIVIDEND_SOURCE = "source IN ('CORPORATE_ACTIONS')"
_SIP_MODE = "mode IN ('REMINDER')"
_SIP_STATUS = "status IN ('ACTIVE', 'PAUSED')"
_PENDING_TYPE = "type IN ('DRIFT', 'REBALANCE_AVAILABLE', 'SIP_DUE', 'GENERIC')"
_UPDATE_SOURCE = "source IN ('ENGINE', 'HUMAN')"
_REBALANCE_STATE = "state IN ('APPLIED', 'SKIPPED', 'PENDING')"
_PLAN_DURATION = "duration IN ('M1', 'M3', 'M6', 'Y1')"
_CB_SUB_STATUS = "status IN ('ACTIVE', 'CANCELLED', 'LAPSED')"


def upgrade() -> None:
    op.create_table(
        "cb_manager",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("sebi_reg_no", sa.String(), nullable=True),
        sa.Column("bio", sa.Text(), nullable=True),
        sa.Column(
            "strategies",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("disclosures_md", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(_MANAGER_KIND, name="cb_manager_kind"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_cb_manager_slug"),
    )

    op.create_table(
        "cb_basket",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("manager_id", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("access", sa.String(), nullable=False),
        sa.Column("visibility", sa.String(), nullable=False),
        sa.Column(
            "categories",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("description_md", sa.Text(), nullable=True),
        sa.Column("rationale_md", sa.Text(), nullable=True),
        sa.Column("rebalance_frequency", sa.String(), nullable=False),
        sa.Column("benchmark_instrument_id", sa.BigInteger(), nullable=True),
        sa.Column("launched_at", sa.Date(), nullable=True),
        sa.Column("next_review_at", sa.Date(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("scan_strategy_key", sa.String(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_BASKET_TYPE, name="cb_basket_type"),
        sa.CheckConstraint(_BASKET_ACCESS, name="cb_basket_access"),
        sa.CheckConstraint(_BASKET_VISIBILITY, name="cb_basket_visibility"),
        sa.CheckConstraint(_REBALANCE_FREQ, name="cb_basket_rebalance_frequency"),
        sa.CheckConstraint(_BASKET_SOURCE, name="cb_basket_source"),
        sa.ForeignKeyConstraint(["manager_id"], ["cb_manager.id"]),
        sa.ForeignKeyConstraint(["benchmark_instrument_id"], ["instrument.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_cb_basket_slug"),
    )
    op.create_index("ix_cb_basket_manager_id", "cb_basket", ["manager_id"])

    op.create_table(
        "cb_basket_version",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("basket_id", sa.BigInteger(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("added_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("removed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notes_md", sa.Text(), nullable=True),
        sa.Column("source_scan_run_id", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(_VERSION_LABEL, name="cb_basket_version_label"),
        sa.ForeignKeyConstraint(["basket_id"], ["cb_basket.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "basket_id", "version_no", name="uq_cb_basket_version_basket_version_no"
        ),
    )
    op.create_index("ix_cb_basket_version_basket_id", "cb_basket_version", ["basket_id"])

    op.create_table(
        "cb_constituent",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("segment", sa.String(), nullable=False),
        sa.Column("weight", sa.Numeric(precision=7, scale=4), nullable=False),
        sa.ForeignKeyConstraint(["version_id"], ["cb_basket_version.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instrument.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cb_constituent_version_id", "cb_constituent", ["version_id"])
    op.create_index("ix_cb_constituent_instrument_id", "cb_constituent", ["instrument_id"])

    op.create_table(
        "cb_metrics",
        sa.Column("basket_id", sa.BigInteger(), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("min_amount", sa.Numeric(precision=20, scale=2), nullable=True),
        sa.Column("volatility_bucket", sa.String(), nullable=True),
        sa.Column("volatility_value", sa.Numeric(precision=18, scale=10), nullable=True),
        sa.Column("ret_1m", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("ret_6m", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("ret_1y", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("cagr_3y", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("cagr_5y", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("since_inception_pct", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(_VOL_BUCKET, name="cb_metrics_volatility_bucket"),
        sa.ForeignKeyConstraint(["basket_id"], ["cb_basket.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("basket_id", "as_of_date"),
    )

    op.create_table(
        "cb_collection",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("subtitle", sa.String(), nullable=True),
        sa.Column(
            "basket_ids",
            postgresql.ARRAY(sa.BigInteger()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("position", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("curated_meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_cb_collection_slug"),
    )

    op.create_table(
        "cb_watchlist_item",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("basket_id", sa.BigInteger(), nullable=False),
        sa.Column("watched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("nav_at_watch", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["basket_id"], ["cb_basket.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "basket_id", name="uq_cb_watchlist_item_user_basket"),
    )
    op.create_index("ix_cb_watchlist_item_user_id", "cb_watchlist_item", ["user_id"])

    op.create_table(
        "cb_investment",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("basket_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("version_applied_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("exited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_invested_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_INVESTMENT_STATUS, name="cb_investment_status"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["basket_id"], ["cb_basket.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["version_applied_id"], ["cb_basket_version.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cb_investment_user_id", "cb_investment", ["user_id"])
    op.create_index("ix_cb_investment_basket_id", "cb_investment", ["basket_id"])

    op.create_table(
        "cb_investment_holding",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("investment_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("qty", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("avg_price", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["investment_id"], ["cb_investment.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instrument.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_cb_investment_holding_investment_id", "cb_investment_holding", ["investment_id"]
    )
    op.create_index(
        "ix_cb_investment_holding_instrument_id", "cb_investment_holding", ["instrument_id"]
    )

    # ``fee_entry_id`` FK is added after ``cb_fee_ledger`` exists (circular reference).
    op.create_table(
        "cb_order_batch",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("investment_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("requested_amount", sa.Numeric(precision=20, scale=2), nullable=True),
        sa.Column("desk_plan_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("fee_entry_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_ORDER_KIND, name="cb_order_batch_kind"),
        sa.CheckConstraint(_ORDER_STATUS, name="cb_order_batch_status"),
        sa.ForeignKeyConstraint(["investment_id"], ["cb_investment.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cb_order_batch_investment_id", "cb_order_batch", ["investment_id"])

    op.create_table(
        "cb_fee_ledger",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("base_fee", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("gst", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("total", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("collected", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("accrued_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["batch_id"], ["cb_order_batch.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cb_fee_ledger_user_id", "cb_fee_ledger", ["user_id"])

    op.create_foreign_key(
        "fk_cb_order_batch_fee_entry_id_cb_fee_ledger",
        "cb_order_batch",
        "cb_fee_ledger",
        ["fee_entry_id"],
        ["id"],
    )

    op.create_table(
        "cb_dividend",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("investment_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("ex_date", sa.Date(), nullable=False),
        sa.Column("amount_per_share", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("qty_held", sa.Numeric(precision=20, scale=4), nullable=False),
        sa.Column("total", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.CheckConstraint(_DIVIDEND_SOURCE, name="cb_dividend_source"),
        sa.ForeignKeyConstraint(["investment_id"], ["cb_investment.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instrument.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cb_dividend_investment_id", "cb_dividend", ["investment_id"])

    op.create_table(
        "cb_sip_plan",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("investment_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column("day_of_month", sa.SmallInteger(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("next_fire_date", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(_SIP_MODE, name="cb_sip_plan_mode"),
        sa.CheckConstraint(_SIP_STATUS, name="cb_sip_plan_status"),
        sa.ForeignKeyConstraint(["investment_id"], ["cb_investment.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cb_sip_plan_investment_id", "cb_sip_plan", ["investment_id"])

    op.create_table(
        "cb_pending_action",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_PENDING_TYPE, name="cb_pending_action_type"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cb_pending_action_user_id", "cb_pending_action", ["user_id"])

    op.create_table(
        "cb_update_post",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("basket_id", sa.BigInteger(), nullable=True),
        sa.Column("manager_id", sa.BigInteger(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("body_md", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.CheckConstraint(_UPDATE_SOURCE, name="cb_update_post_source"),
        sa.ForeignKeyConstraint(["basket_id"], ["cb_basket.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["manager_id"], ["cb_manager.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "cb_user_rebalance_state",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_REBALANCE_STATE, name="cb_user_rebalance_state_state"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["version_id"], ["cb_basket_version.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "version_id"),
    )

    op.create_table(
        "cb_plan",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("basket_id", sa.BigInteger(), nullable=True),
        sa.Column("manager_id", sa.BigInteger(), nullable=True),
        sa.Column("duration", sa.String(), nullable=False),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.CheckConstraint(_PLAN_DURATION, name="cb_plan_duration"),
        sa.ForeignKeyConstraint(["basket_id"], ["cb_basket.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["manager_id"], ["cb_manager.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "cb_subscription",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("renew_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("auto_renew", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.CheckConstraint(_CB_SUB_STATUS, name="cb_subscription_status"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_id"], ["cb_plan.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cb_subscription_user_id", "cb_subscription", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_cb_subscription_user_id", table_name="cb_subscription")
    op.drop_table("cb_subscription")
    op.drop_table("cb_plan")
    op.drop_table("cb_user_rebalance_state")
    op.drop_table("cb_update_post")
    op.drop_index("ix_cb_pending_action_user_id", table_name="cb_pending_action")
    op.drop_table("cb_pending_action")
    op.drop_index("ix_cb_sip_plan_investment_id", table_name="cb_sip_plan")
    op.drop_table("cb_sip_plan")
    op.drop_index("ix_cb_dividend_investment_id", table_name="cb_dividend")
    op.drop_table("cb_dividend")
    op.drop_constraint(
        "fk_cb_order_batch_fee_entry_id_cb_fee_ledger", "cb_order_batch", type_="foreignkey"
    )
    op.drop_index("ix_cb_fee_ledger_user_id", table_name="cb_fee_ledger")
    op.drop_table("cb_fee_ledger")
    op.drop_index("ix_cb_order_batch_investment_id", table_name="cb_order_batch")
    op.drop_table("cb_order_batch")
    op.drop_index("ix_cb_investment_holding_instrument_id", table_name="cb_investment_holding")
    op.drop_index("ix_cb_investment_holding_investment_id", table_name="cb_investment_holding")
    op.drop_table("cb_investment_holding")
    op.drop_index("ix_cb_investment_basket_id", table_name="cb_investment")
    op.drop_index("ix_cb_investment_user_id", table_name="cb_investment")
    op.drop_table("cb_investment")
    op.drop_index("ix_cb_watchlist_item_user_id", table_name="cb_watchlist_item")
    op.drop_table("cb_watchlist_item")
    op.drop_table("cb_collection")
    op.drop_table("cb_metrics")
    op.drop_index("ix_cb_constituent_instrument_id", table_name="cb_constituent")
    op.drop_index("ix_cb_constituent_version_id", table_name="cb_constituent")
    op.drop_table("cb_constituent")
    op.drop_index("ix_cb_basket_version_basket_id", table_name="cb_basket_version")
    op.drop_table("cb_basket_version")
    op.drop_index("ix_cb_basket_manager_id", table_name="cb_basket")
    op.drop_table("cb_basket")
    op.drop_table("cb_manager")
