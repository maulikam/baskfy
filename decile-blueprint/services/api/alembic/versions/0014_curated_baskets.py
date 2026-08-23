"""Curated-basket product schema (SC1).

Eighteen ``cb_`` tables implement docs/smallcase/03-data-model.md.  Basket versions and their
constituents are append-only at the database boundary.  ``cb_order_batch`` records product-layer
intent only: its desk plan id is opaque, while the mandatory sole-user and broker-account ids
preserve the dormant order identity contract without introducing multi-tenant execution.

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


def upgrade() -> None:
    op.create_table(
        "cb_manager",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("sebi_reg_no", sa.String(), nullable=True),
        sa.Column("bio", sa.String(), nullable=False),
        sa.Column("strategies", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("disclosures_md", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('ENGINE', 'HUMAN', 'EXTERNAL')", name=op.f("ck_cb_manager_cb_manager_kind")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cb_manager")),
        sa.UniqueConstraint("slug", name=op.f("uq_cb_manager_slug")),
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
        sa.Column("categories", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("description_md", sa.String(), nullable=False),
        sa.Column("rationale_md", sa.String(), nullable=False),
        sa.Column("rebalance_frequency", sa.String(), nullable=False),
        sa.Column("benchmark_instrument_id", sa.BigInteger(), nullable=True),
        sa.Column("launched_at", sa.Date(), nullable=False),
        sa.Column("next_review_at", sa.Date(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("scan_strategy_key", sa.String(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("access IN ('FREE', 'FEE')", name=op.f("ck_cb_basket_cb_basket_access")),
        sa.CheckConstraint(
            "rebalance_frequency IN ('WEEKLY', 'MONTHLY', 'QUARTERLY', 'ANNUAL', 'NEED_BASIS')",
            name=op.f("ck_cb_basket_cb_basket_rebalance_frequency"),
        ),
        sa.CheckConstraint(
            "source IN ('SCAN', 'MANUAL')", name=op.f("ck_cb_basket_cb_basket_source")
        ),
        sa.CheckConstraint(
            "type IN ('STOCK', 'MF', 'US')", name=op.f("ck_cb_basket_cb_basket_type")
        ),
        sa.CheckConstraint(
            "visibility IN ('PUBLISHED', 'PRIVATE')",
            name=op.f("ck_cb_basket_cb_basket_visibility"),
        ),
        sa.ForeignKeyConstraint(
            ["benchmark_instrument_id"],
            ["instrument.id"],
            name=op.f("fk_cb_basket_benchmark_instrument_id_instrument"),
        ),
        sa.ForeignKeyConstraint(
            ["manager_id"],
            ["cb_manager.id"],
            name=op.f("fk_cb_basket_manager_id_cb_manager"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cb_basket")),
        sa.UniqueConstraint("slug", name=op.f("uq_cb_basket_slug")),
    )
    op.create_index(
        "ix_cb_basket_benchmark_instrument_id",
        "cb_basket",
        ["benchmark_instrument_id"],
        unique=False,
    )
    op.create_index(
        "ix_cb_basket_catalog",
        "cb_basket",
        ["visibility", "archived_at", "launched_at"],
        unique=False,
    )
    op.create_index("ix_cb_basket_manager_id", "cb_basket", ["manager_id"], unique=False)
    op.create_table(
        "cb_basket_version",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("basket_id", sa.BigInteger(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("added_count", sa.Integer(), nullable=False),
        sa.Column("removed_count", sa.Integer(), nullable=False),
        sa.Column("notes_md", sa.String(), nullable=True),
        sa.Column("source_scan_run_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "added_count >= 0",
            name=op.f("ck_cb_basket_version_cb_basket_version_added_non_negative"),
        ),
        sa.CheckConstraint(
            "label IN ('CHANGED', 'NO_CHANGE', 'GENESIS')",
            name=op.f("ck_cb_basket_version_cb_basket_version_label"),
        ),
        sa.CheckConstraint(
            "removed_count >= 0",
            name=op.f("ck_cb_basket_version_cb_basket_version_removed_non_negative"),
        ),
        sa.CheckConstraint(
            "version_no > 0",
            name=op.f("ck_cb_basket_version_cb_basket_version_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["basket_id"],
            ["cb_basket.id"],
            name=op.f("fk_cb_basket_version_basket_id_cb_basket"),
        ),
        sa.ForeignKeyConstraint(
            ["source_scan_run_id"],
            ["screen_run.id"],
            name=op.f("fk_cb_basket_version_source_scan_run_id_screen_run"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cb_basket_version")),
        sa.UniqueConstraint(
            "basket_id",
            "effective_date",
            name="uq_cb_basket_version_basket_effective_date",
        ),
        sa.UniqueConstraint(
            "basket_id", "version_no", name="uq_cb_basket_version_basket_version_no"
        ),
    )
    op.create_index(
        "ix_cb_basket_version_source_scan_run_id",
        "cb_basket_version",
        ["source_scan_run_id"],
        unique=False,
    )
    op.create_table(
        "cb_constituent",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("segment", sa.String(), nullable=False),
        sa.Column("weight", sa.Numeric(7, 4), nullable=False),
        sa.CheckConstraint(
            "weight > 0 AND weight <= 1",
            name=op.f("ck_cb_constituent_cb_constituent_weight_range"),
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name=op.f("fk_cb_constituent_instrument_id_instrument"),
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["cb_basket_version.id"],
            name=op.f("fk_cb_constituent_version_id_cb_basket_version"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cb_constituent")),
        sa.UniqueConstraint(
            "version_id",
            "instrument_id",
            name=op.f("uq_cb_constituent_version_id_instrument_id"),
        ),
    )
    op.create_index(
        "ix_cb_constituent_instrument_id",
        "cb_constituent",
        ["instrument_id"],
        unique=False,
    )
    op.create_table(
        "cb_metrics",
        sa.Column("basket_id", sa.BigInteger(), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("min_amount", sa.Numeric(20, 2), nullable=False),
        sa.Column("volatility_bucket", sa.String(), nullable=False),
        sa.Column("volatility_value", sa.Numeric(18, 10), nullable=False),
        sa.Column("ret_1m", sa.Numeric(18, 10), nullable=True),
        sa.Column("ret_6m", sa.Numeric(18, 10), nullable=True),
        sa.Column("ret_1y", sa.Numeric(18, 10), nullable=True),
        sa.Column("cagr_3y", sa.Numeric(18, 10), nullable=True),
        sa.Column("cagr_5y", sa.Numeric(18, 10), nullable=True),
        sa.Column("since_inception_pct", sa.Numeric(18, 10), nullable=True),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "min_amount >= 0", name=op.f("ck_cb_metrics_cb_metrics_min_amount_non_negative")
        ),
        sa.CheckConstraint(
            "volatility_bucket IN ('LOW', 'MED', 'HIGH')",
            name=op.f("ck_cb_metrics_cb_metrics_volatility_bucket"),
        ),
        sa.CheckConstraint(
            "volatility_value >= 0",
            name=op.f("ck_cb_metrics_cb_metrics_volatility_value_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["basket_id"],
            ["cb_basket.id"],
            name=op.f("fk_cb_metrics_basket_id_cb_basket"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("basket_id", "as_of_date", name=op.f("pk_cb_metrics")),
    )
    op.create_table(
        "cb_collection",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("subtitle", sa.String(), nullable=False),
        sa.Column("basket_ids", postgresql.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("curated_meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cb_collection")),
        sa.UniqueConstraint("slug", name=op.f("uq_cb_collection_slug")),
    )
    op.create_table(
        "cb_watchlist_item",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("basket_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "watched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("nav_at_watch", sa.Numeric(18, 4), nullable=False),
        sa.ForeignKeyConstraint(
            ["basket_id"],
            ["cb_basket.id"],
            name=op.f("fk_cb_watchlist_item_basket_id_cb_basket"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_cb_watchlist_item_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cb_watchlist_item")),
        sa.UniqueConstraint(
            "user_id",
            "basket_id",
            name=op.f("uq_cb_watchlist_item_user_id_basket_id"),
        ),
    )
    op.create_index(
        "ix_cb_watchlist_item_basket_id",
        "cb_watchlist_item",
        ["basket_id"],
        unique=False,
    )
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
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("exited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_invested_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'EXITED')",
            name=op.f("ck_cb_investment_cb_investment_status"),
        ),
        sa.ForeignKeyConstraint(
            ["basket_id"],
            ["cb_basket.id"],
            name=op.f("fk_cb_investment_basket_id_cb_basket"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_cb_investment_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["version_applied_id"],
            ["cb_basket_version.id"],
            name=op.f("fk_cb_investment_version_applied_id_cb_basket_version"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cb_investment")),
    )
    op.create_index("ix_cb_investment_basket_id", "cb_investment", ["basket_id"], unique=False)
    op.create_index("ix_cb_investment_user_id", "cb_investment", ["user_id"], unique=False)
    op.create_index(
        "ix_cb_investment_version_applied_id",
        "cb_investment",
        ["version_applied_id"],
        unique=False,
    )
    op.create_table(
        "cb_investment_holding",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("investment_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("qty", sa.Numeric(20, 4), nullable=False),
        sa.Column("avg_price", sa.Numeric(18, 4), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "avg_price >= 0",
            name=op.f("ck_cb_investment_holding_avg_price_non_negative"),
        ),
        sa.CheckConstraint(
            "qty >= 0",
            name=op.f("ck_cb_investment_holding_qty_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name=op.f("fk_cb_investment_holding_instrument_id_instrument"),
        ),
        sa.ForeignKeyConstraint(
            ["investment_id"],
            ["cb_investment.id"],
            name=op.f("fk_cb_investment_holding_investment_id_cb_investment"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cb_investment_holding")),
        sa.UniqueConstraint(
            "investment_id",
            "instrument_id",
            name=op.f("uq_cb_investment_holding_investment_id_instrument_id"),
        ),
    )
    op.create_index(
        "ix_cb_investment_holding_instrument_id",
        "cb_investment_holding",
        ["instrument_id"],
        unique=False,
    )
    op.create_table(
        "cb_order_batch",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("investment_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("broker_account_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("requested_amount", sa.Numeric(20, 2), nullable=True),
        sa.Column("desk_plan_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("fee_entry_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('BUY', 'INVEST_MORE', 'SIP', 'REBALANCE', 'EXIT', "
            "'PARTIAL_EXIT', 'CUSTOMIZE')",
            name=op.f("ck_cb_order_batch_cb_order_batch_kind"),
        ),
        sa.CheckConstraint(
            "requested_amount IS NULL OR requested_amount > 0",
            name=op.f("ck_cb_order_batch_cb_order_batch_requested_amount_positive"),
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'PLANNED', 'EXPIRED', 'EXECUTED', 'PARTIAL', 'CANCELLED')",
            name=op.f("ck_cb_order_batch_cb_order_batch_status"),
        ),
        sa.ForeignKeyConstraint(
            ["investment_id"],
            ["cb_investment.id"],
            name=op.f("fk_cb_order_batch_investment_id_cb_investment"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name=op.f("fk_cb_order_batch_user_id_app_user")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cb_order_batch")),
    )
    op.create_index(
        "ix_cb_order_batch_desk_plan_id", "cb_order_batch", ["desk_plan_id"], unique=False
    )
    op.create_index(
        "ix_cb_order_batch_fee_entry_id", "cb_order_batch", ["fee_entry_id"], unique=False
    )
    op.create_index(
        "ix_cb_order_batch_investment_id", "cb_order_batch", ["investment_id"], unique=False
    )
    op.create_index("ix_cb_order_batch_user_id", "cb_order_batch", ["user_id"], unique=False)
    op.create_table(
        "cb_fee_ledger",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("base_fee", sa.Numeric(20, 2), nullable=False),
        sa.Column("gst", sa.Numeric(20, 2), nullable=False),
        sa.Column("total", sa.Numeric(20, 2), nullable=False),
        sa.Column("collected", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "accrued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "base_fee >= 0",
            name=op.f("ck_cb_fee_ledger_cb_fee_ledger_base_fee_non_negative"),
        ),
        sa.CheckConstraint(
            "gst >= 0", name=op.f("ck_cb_fee_ledger_cb_fee_ledger_gst_non_negative")
        ),
        sa.CheckConstraint(
            "total >= 0", name=op.f("ck_cb_fee_ledger_cb_fee_ledger_total_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["cb_order_batch.id"],
            name=op.f("fk_cb_fee_ledger_batch_id_cb_order_batch"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name=op.f("fk_cb_fee_ledger_user_id_app_user")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cb_fee_ledger")),
        sa.UniqueConstraint("batch_id", name=op.f("uq_cb_fee_ledger_batch_id")),
    )
    op.create_index("ix_cb_fee_ledger_user_id", "cb_fee_ledger", ["user_id"], unique=False)
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
        sa.Column("amount_per_share", sa.Numeric(18, 4), nullable=False),
        sa.Column("qty_held", sa.Numeric(20, 4), nullable=False),
        sa.Column("total", sa.Numeric(20, 2), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.CheckConstraint(
            "amount_per_share >= 0", name=op.f("ck_cb_dividend_cb_dividend_amount_non_negative")
        ),
        sa.CheckConstraint(
            "qty_held >= 0", name=op.f("ck_cb_dividend_cb_dividend_qty_non_negative")
        ),
        sa.CheckConstraint(
            "source IN ('CORPORATE_ACTIONS')",
            name=op.f("ck_cb_dividend_cb_dividend_source"),
        ),
        sa.CheckConstraint(
            "total >= 0", name=op.f("ck_cb_dividend_cb_dividend_total_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name=op.f("fk_cb_dividend_instrument_id_instrument"),
        ),
        sa.ForeignKeyConstraint(
            ["investment_id"],
            ["cb_investment.id"],
            name=op.f("fk_cb_dividend_investment_id_cb_investment"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cb_dividend")),
        sa.UniqueConstraint(
            "investment_id",
            "instrument_id",
            "ex_date",
            name=op.f("uq_cb_dividend_investment_id_instrument_id_ex_date"),
        ),
    )
    op.create_index("ix_cb_dividend_instrument_id", "cb_dividend", ["instrument_id"], unique=False)
    op.create_table(
        "cb_sip_plan",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("investment_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(20, 2), nullable=False),
        sa.Column("day_of_month", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("next_fire_date", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_cb_sip_plan_cb_sip_plan_amount_positive")),
        sa.CheckConstraint(
            "day_of_month BETWEEN 1 AND 31", name=op.f("ck_cb_sip_plan_cb_sip_plan_day_of_month")
        ),
        sa.CheckConstraint("mode IN ('REMINDER')", name=op.f("ck_cb_sip_plan_cb_sip_plan_mode")),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'PAUSED')", name=op.f("ck_cb_sip_plan_cb_sip_plan_status")
        ),
        sa.ForeignKeyConstraint(
            ["investment_id"],
            ["cb_investment.id"],
            name=op.f("fk_cb_sip_plan_investment_id_cb_investment"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cb_sip_plan")),
    )
    op.create_index("ix_cb_sip_plan_investment_id", "cb_sip_plan", ["investment_id"], unique=False)
    op.create_index(
        "ix_cb_sip_plan_next_fire_date", "cb_sip_plan", ["next_fire_date"], unique=False
    )
    op.create_table(
        "cb_pending_action",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "type IN ('DRIFT', 'REBALANCE_AVAILABLE', 'SIP_DUE', 'GENERIC')",
            name=op.f("ck_cb_pending_action_cb_pending_action_type"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_cb_pending_action_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cb_pending_action")),
    )
    op.create_index("ix_cb_pending_action_user_id", "cb_pending_action", ["user_id"], unique=False)
    op.create_index(
        "ix_cb_pending_action_user_unresolved",
        "cb_pending_action",
        ["user_id", "resolved_at"],
        unique=False,
    )
    op.create_table(
        "cb_update_post",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("basket_id", sa.BigInteger(), nullable=True),
        sa.Column("manager_id", sa.BigInteger(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("body_md", sa.String(), nullable=False),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("source", sa.String(), nullable=False),
        sa.CheckConstraint(
            "basket_id IS NOT NULL OR manager_id IS NOT NULL",
            name=op.f("ck_cb_update_post_cb_update_post_owner"),
        ),
        sa.CheckConstraint(
            "source IN ('ENGINE', 'HUMAN')", name=op.f("ck_cb_update_post_cb_update_post_source")
        ),
        sa.ForeignKeyConstraint(
            ["basket_id"],
            ["cb_basket.id"],
            name=op.f("fk_cb_update_post_basket_id_cb_basket"),
        ),
        sa.ForeignKeyConstraint(
            ["manager_id"],
            ["cb_manager.id"],
            name=op.f("fk_cb_update_post_manager_id_cb_manager"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cb_update_post")),
    )
    op.create_index("ix_cb_update_post_basket_id", "cb_update_post", ["basket_id"], unique=False)
    op.create_index("ix_cb_update_post_manager_id", "cb_update_post", ["manager_id"], unique=False)
    op.create_index(
        "ix_cb_update_post_published_at", "cb_update_post", ["published_at"], unique=False
    )
    op.create_table(
        "cb_user_rebalance_state",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('APPLIED', 'SKIPPED', 'PENDING')",
            name=op.f("ck_cb_user_rebalance_state_cb_user_rebalance_state_state"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_cb_user_rebalance_state_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["cb_basket_version.id"],
            name=op.f("fk_cb_user_rebalance_state_version_id_cb_basket_version"),
        ),
        sa.PrimaryKeyConstraint("user_id", "version_id", name=op.f("pk_cb_user_rebalance_state")),
    )
    op.create_index(
        "ix_cb_user_rebalance_state_version_id",
        "cb_user_rebalance_state",
        ["version_id"],
        unique=False,
    )
    op.create_table(
        "cb_plan",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("basket_id", sa.BigInteger(), nullable=True),
        sa.Column("manager_id", sa.BigInteger(), nullable=True),
        sa.Column("duration", sa.String(), nullable=False),
        sa.Column("price", sa.Numeric(20, 2), nullable=False),
        sa.CheckConstraint(
            "duration IN ('M1', 'M3', 'M6', 'Y1')", name=op.f("ck_cb_plan_cb_plan_duration")
        ),
        sa.CheckConstraint(
            "basket_id IS NOT NULL OR manager_id IS NOT NULL",
            name=op.f("ck_cb_plan_cb_plan_owner"),
        ),
        sa.CheckConstraint("price >= 0", name=op.f("ck_cb_plan_cb_plan_price_non_negative")),
        sa.ForeignKeyConstraint(
            ["basket_id"], ["cb_basket.id"], name=op.f("fk_cb_plan_basket_id_cb_basket")
        ),
        sa.ForeignKeyConstraint(
            ["manager_id"], ["cb_manager.id"], name=op.f("fk_cb_plan_manager_id_cb_manager")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cb_plan")),
    )
    op.create_index("ix_cb_plan_basket_id", "cb_plan", ["basket_id"], unique=False)
    op.create_index("ix_cb_plan_manager_id", "cb_plan", ["manager_id"], unique=False)
    op.create_table(
        "cb_subscription",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("renew_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("auto_renew", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'CANCELLED', 'LAPSED')",
            name=op.f("ck_cb_subscription_cb_subscription_status"),
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["cb_plan.id"], name=op.f("fk_cb_subscription_plan_id_cb_plan")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_cb_subscription_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cb_subscription")),
    )
    op.create_index("ix_cb_subscription_plan_id", "cb_subscription", ["plan_id"], unique=False)
    op.create_index("ix_cb_subscription_user_id", "cb_subscription", ["user_id"], unique=False)

    op.execute(
        """
        CREATE FUNCTION cb_reject_immutable_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only; create a new basket version instead', TG_TABLE_NAME
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER cb_basket_version_immutable "
        "BEFORE UPDATE OR DELETE ON cb_basket_version "
        "FOR EACH ROW EXECUTE FUNCTION cb_reject_immutable_change()"
    )
    op.execute(
        "CREATE TRIGGER cb_constituent_immutable "
        "BEFORE UPDATE OR DELETE ON cb_constituent "
        "FOR EACH ROW EXECUTE FUNCTION cb_reject_immutable_change()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS cb_constituent_immutable ON cb_constituent")
    op.execute("DROP TRIGGER IF EXISTS cb_basket_version_immutable ON cb_basket_version")
    op.execute("DROP FUNCTION IF EXISTS cb_reject_immutable_change()")
    op.drop_table("cb_subscription")
    op.drop_table("cb_plan")
    op.drop_table("cb_user_rebalance_state")
    op.drop_table("cb_update_post")
    op.drop_table("cb_pending_action")
    op.drop_table("cb_sip_plan")
    op.drop_table("cb_dividend")
    op.drop_constraint(
        "fk_cb_order_batch_fee_entry_id_cb_fee_ledger", "cb_order_batch", type_="foreignkey"
    )
    op.drop_table("cb_fee_ledger")
    op.drop_table("cb_order_batch")
    op.drop_table("cb_investment_holding")
    op.drop_table("cb_investment")
    op.drop_table("cb_watchlist_item")
    op.drop_table("cb_collection")
    op.drop_table("cb_metrics")
    op.drop_table("cb_constituent")
    op.drop_table("cb_basket_version")
    op.drop_table("cb_basket")
    op.drop_table("cb_manager")
