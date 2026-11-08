"""The volume-breakout sleeve's schema — ``docs/vbt/03-data-model.md``, module VB3.

Twelve tables, one migration. They are one migration rather than twelve because they are one
*thing*: a signal table without a plan, or a plan without positions, is not a half-working sleeve
— it is a schema no code can use, and alembic history is linear, so splitting them would only
serialise the modules that build on them. The swing book's ``0028_swing`` made the same call for
the same reason.

Nothing here alters an existing table. This sleeve joins ``instrument``, ``ohlcv_daily``,
``trading_day``, ``pipeline_run`` and the desk's order journal, and owns none of them.

FOUR THINGS TO KNOW BEFORE READING THE DDL
------------------------------------------
**Every table carries ``user_id``.** ``docs/vbt/02-scope-and-gating.md`` Track C §6, so that the
day multi-tenancy arrives nothing needs a migration. The two tables ``03`` sketches with a
market-wide key — ``vb_signal_daily`` and ``vb_breadth_daily`` — therefore have it in their
primary key: both are snapshots of what one user's system saw, and it is that user's settings
that decide what may be acted on.

**``vb_signal_daily`` is a snapshot, not a view**, and it keeps the rows it *rejected*
(``state = 'SCAN_ONLY'``, with ``failed_filters`` naming which of A-F failed). The ablation table
in ``docs/vbt/01`` §3 is the whole argument for the six trend filters, and a system that stores
only what it accepted cannot show a person what it passed over. DECISIONS-VB PACK.6.

**The stop can only ever rise.** ``ck_vb_position_stop_never_below_initial`` is the floor a bug
cannot get under. The rule is enforced where the update happens (``exits.apply_stop`` takes the
maximum; the desk refuses a raise below the resting trigger), and this constraint is the database
saying so a second time, because that rule is the difference between a losing trade and a blown
account.

**One working order per name per signal.** ``uq_vb_order_one_per_signal`` is what makes the
evening job idempotent: a re-run of the evening cannot double-place the limit it already placed.

WHAT THIS MIGRATION DELIBERATELY DOES NOT DO
--------------------------------------------
It seeds nothing. ``vb_config`` for the sole user is written by ``baskfy_api.seed``, because a
migration that inserted a row keyed on ``BASKFY_SOLE_USER_ID`` would embed an environment
variable into schema history. The seeded row has ``sleeve_capital_inr = 0``, which is the state
in which the sleeve plans nothing at all (``docs/vbt/02`` §3.4).

Revision ID: 0037_vbt
Revises: 0036_check_name
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0037_vbt"
down_revision: str | None = "0036_check_name"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Dropped in reverse dependency order by :func:`downgrade`. Listed once, so a table added to
#: :func:`upgrade` and forgotten here fails ``make downgrade`` rather than leaking into the next
#: developer's database.
TABLES: tuple[str, ...] = (
    "vb_backtest_run",
    "vb_session",
    "vb_plan_skip",
    "vb_plan_line",
    "vb_plan",
    "vb_fill",
    "vb_order",
    "vb_position",
    "vb_breadth_daily",
    "vb_signal_daily",
    "vb_config_audit",
    "vb_config",
)


def upgrade() -> None:
    op.create_table(
        "vb_config",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "sleeve_capital_inr",
            sa.Numeric(precision=20, scale=2),
            server_default="0",
            nullable=False,
        ),
        sa.Column("max_open_positions", sa.SmallInteger(), server_default="10", nullable=False),
        sa.Column(
            "max_position_pct",
            sa.Numeric(precision=5, scale=2),
            server_default="12.50",
            nullable=False,
        ),
        sa.Column(
            "stop_pct", sa.Numeric(precision=5, scale=2), server_default="12.00", nullable=False
        ),
        sa.Column(
            "first_live_sessions_left", sa.SmallInteger(), server_default="5", nullable=False
        ),
        sa.Column("dry_run_sessions", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "sleeve_capital_inr >= 0", name=op.f("ck_vb_config_capital_non_negative")
        ),
        sa.CheckConstraint(
            "dry_run_sessions >= 0", name=op.f("ck_vb_config_dry_run_sessions_non_negative")
        ),
        sa.CheckConstraint(
            "first_live_sessions_left >= 0", name=op.f("ck_vb_config_first_live_non_negative")
        ),
        sa.CheckConstraint(
            "max_open_positions > 0", name=op.f("ck_vb_config_max_positions_positive")
        ),
        sa.CheckConstraint("max_position_pct > 0", name=op.f("ck_vb_config_position_pct_positive")),
        sa.CheckConstraint("stop_pct > 0", name=op.f("ck_vb_config_stop_pct_positive")),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_vb_config")),
    )

    op.create_table(
        "vb_config_audit",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("changed_by", sa.String(length=64), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vb_config_audit")),
    )
    op.create_index(
        op.f("ix_vb_config_audit_user_time"),
        "vb_config_audit",
        ["user_id", "changed_at"],
        unique=False,
    )

    op.create_table(
        "vb_signal_daily",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column(
            "failed_filters",
            postgresql.ARRAY(sa.String(length=1)),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("open", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("high", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("low", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("close", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("close_raw", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "adj_factor", sa.Numeric(precision=18, scale=10), server_default="1", nullable=False
        ),
        sa.Column("limit_price", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("stop_price", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("change_pct", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("rvol", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("close_position", sa.Numeric(precision=6, scale=4), nullable=True),
        sa.Column("ret_20_pct", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("vol_sma_50", sa.BigInteger(), nullable=True),
        sa.Column("turnover_inr", sa.BigInteger(), nullable=True),
        sa.Column("turnover_avg_20", sa.BigInteger(), nullable=True),
        sa.Column("sma_200", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("ema_21", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("high_20_prior", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("rank_key", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("locked_upper_circuit", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("bars_in_window", sa.SmallInteger(), nullable=True),
        sa.Column("pipeline_run_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("limit_price > 0", name=op.f("ck_vb_signal_daily_limit_positive")),
        sa.CheckConstraint(
            "state IN ('SIGNAL', 'SCAN_ONLY')", name=op.f("ck_vb_signal_daily_state_known")
        ),
        sa.CheckConstraint(
            "stop_price > 0 AND stop_price < limit_price",
            name=op.f("ck_vb_signal_daily_stop_below_limit"),
        ),
        sa.ForeignKeyConstraint(["instrument_id"], ["instrument.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pipeline_run_id"], ["pipeline_run.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint(
            "user_id", "date", "instrument_id", name=op.f("pk_vb_signal_daily")
        ),
    )
    op.create_index(
        op.f("ix_vb_signal_daily_instrument"),
        "vb_signal_daily",
        ["instrument_id", "date"],
        unique=False,
    )
    op.create_index(
        op.f("ix_vb_signal_daily_rank"),
        "vb_signal_daily",
        ["date", "state", "rank_key"],
        unique=False,
    )

    op.create_table(
        "vb_breadth_daily",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("universe_count", sa.Integer(), nullable=False),
        sa.Column("measured_count", sa.Integer(), nullable=False),
        sa.Column("above_count", sa.Integer(), nullable=False),
        sa.Column("pct_above_dma", sa.Numeric(precision=7, scale=4), nullable=False),
        sa.Column("gate", sa.String(length=8), nullable=False),
        sa.Column("dma_bars", sa.SmallInteger(), server_default="200", nullable=False),
        sa.Column("thin_session", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("pipeline_run_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "above_count <= measured_count", name=op.f("ck_vb_breadth_daily_above_within_measured")
        ),
        sa.CheckConstraint("gate IN ('OPEN', 'SHUT')", name=op.f("ck_vb_breadth_daily_gate_known")),
        sa.CheckConstraint(
            "measured_count <= universe_count",
            name=op.f("ck_vb_breadth_daily_measured_within_universe"),
        ),
        sa.ForeignKeyConstraint(["pipeline_run_id"], ["pipeline_run.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "date", name=op.f("pk_vb_breadth_daily")),
    )

    op.create_table(
        "vb_position",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("broker_account_id", sa.BigInteger(), nullable=True),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("entry_avg", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("quantity_entered", sa.Integer(), nullable=False),
        sa.Column("quantity_open", sa.Integer(), nullable=False),
        sa.Column("initial_stop", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("stop_price", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("gtt_id", sa.String(length=64), nullable=True),
        sa.Column("gtt_trigger", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("gtt_armed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(length=16), server_default="OPEN", nullable=False),
        sa.Column("exit_queued_for", sa.Date(), nullable=True),
        sa.Column("exit_reason_queued", sa.String(length=24), nullable=True),
        sa.Column("closed_on", sa.Date(), nullable=True),
        sa.Column("exit_avg", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("close_reason", sa.String(length=24), nullable=True),
        sa.Column("pnl_inr", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("return_pct", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("r_multiple", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("hold_sessions", sa.SmallInteger(), nullable=True),
        sa.Column("size_cap", sa.String(length=16), nullable=True),
        sa.Column("simulated", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("half_risk", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "close_reason IN ('EMA_EXIT', 'STOP_HIT', 'STOP_GAP', 'NO_BAR', 'END_OF_RUN', 'MANUAL', 'MANUAL')",
            name=op.f("ck_vb_position_close_reason_known"),
        ),
        sa.CheckConstraint("quantity_open >= 0", name=op.f("ck_vb_position_open_non_negative")),
        sa.CheckConstraint(
            "quantity_open <= quantity_entered", name=op.f("ck_vb_position_open_within_entered")
        ),
        sa.CheckConstraint("quantity_entered > 0", name=op.f("ck_vb_position_quantity_positive")),
        sa.CheckConstraint("state IN ('OPEN', 'CLOSED')", name=op.f("ck_vb_position_state_known")),
        sa.CheckConstraint(
            "stop_price >= initial_stop", name=op.f("ck_vb_position_stop_never_below_initial")
        ),
        sa.ForeignKeyConstraint(["instrument_id"], ["instrument.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vb_position")),
    )
    op.create_index(
        op.f("ix_vb_position_book"),
        "vb_position",
        ["user_id", "state", "instrument_id"],
        unique=False,
    )

    op.create_table(
        "vb_order",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("broker_account_id", sa.BigInteger(), nullable=True),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("signal_date", sa.Date(), nullable=False),
        sa.Column("limit_price", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("stop_price", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("working_from", sa.Date(), nullable=True),
        sa.Column("expires_after_session", sa.Date(), nullable=True),
        sa.Column("sessions_worked", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("broker_order_id", sa.String(length=64), nullable=True),
        sa.Column("client_id", sa.String(length=128), nullable=True),
        sa.Column("filled_quantity", sa.Integer(), server_default="0", nullable=False),
        sa.Column("avg_fill_price", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("position_id", sa.BigInteger(), nullable=True),
        sa.Column("cancelled_on", sa.Date(), nullable=True),
        sa.Column("cancel_reason", sa.String(length=16), nullable=True),
        sa.Column("simulated", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "cancel_reason IN ('EXPIRY_SWEEP', 'MANUAL', 'GATE_SHUT', 'SLOT_TAKEN')",
            name=op.f("ck_vb_order_cancel_reason_known"),
        ),
        sa.CheckConstraint("filled_quantity >= 0", name=op.f("ck_vb_order_filled_non_negative")),
        sa.CheckConstraint(
            "filled_quantity <= quantity", name=op.f("ck_vb_order_filled_within_quantity")
        ),
        sa.CheckConstraint("limit_price > 0", name=op.f("ck_vb_order_limit_positive")),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_vb_order_quantity_positive")),
        sa.CheckConstraint(
            "sessions_worked >= 0", name=op.f("ck_vb_order_sessions_worked_non_negative")
        ),
        sa.CheckConstraint(
            "state IN ('PROPOSED', 'CONFIRMED', 'SENT', 'PARTIAL', 'FILLED', 'CANCELLED', 'EXPIRED', 'REJECTED')",
            name=op.f("ck_vb_order_state_known"),
        ),
        sa.CheckConstraint(
            "stop_price > 0 AND stop_price < limit_price", name=op.f("ck_vb_order_stop_below_limit")
        ),
        sa.ForeignKeyConstraint(["instrument_id"], ["instrument.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["position_id"], ["vb_position.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vb_order")),
        sa.UniqueConstraint(
            "user_id", "signal_date", "instrument_id", name=op.f("uq_vb_order_one_per_signal")
        ),
    )
    op.create_index(
        op.f("ix_vb_order_sweep"),
        "vb_order",
        ["user_id", "state", "expires_after_session"],
        unique=False,
    )

    op.create_table(
        "vb_fill",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("position_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("journal_ref", sa.String(length=64), nullable=True),
        sa.Column("simulated", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("price > 0", name=op.f("ck_vb_fill_price_positive")),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_vb_fill_quantity_positive")),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name=op.f("ck_vb_fill_side_known")),
        sa.ForeignKeyConstraint(["order_id"], ["vb_order.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["position_id"], ["vb_position.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vb_fill")),
    )
    op.create_index(
        op.f("ix_vb_fill_position"), "vb_fill", ["position_id", "filled_at"], unique=False
    )

    op.create_table(
        "vb_plan",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("gate", sa.String(length=8), nullable=False),
        sa.Column("sleeve_equity_inr", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column(
            "total_new_exposure_inr",
            sa.Numeric(precision=20, scale=2),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("gate IN ('OPEN', 'SHUT')", name=op.f("ck_vb_plan_gate_known")),
        sa.CheckConstraint(
            "source IN ('EVENING', 'MORNING', 'MANUAL')", name=op.f("ck_vb_plan_source_known")
        ),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vb_plan")),
        sa.UniqueConstraint("plan_id", name=op.f("uq_vb_plan_plan_id")),
    )
    op.create_index(
        op.f("ix_vb_plan_session"), "vb_plan", ["user_id", "session_date", "built_at"], unique=False
    )

    op.create_table(
        "vb_plan_line",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="PROPOSED", nullable=False),
        sa.Column("quantity", sa.Integer(), server_default="0", nullable=False),
        sa.Column("limit_price", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("stop_price", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column(
            "value_inr", sa.Numeric(precision=20, scale=2), server_default="0", nullable=False
        ),
        sa.Column("size_cap", sa.String(length=16), nullable=True),
        sa.Column("reason", sa.String(length=24), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("client_id", sa.String(length=128), nullable=True),
        sa.Column("journal_ref", sa.String(length=64), nullable=True),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("position_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('PLACE_LIMIT', 'SELL_AT_OPEN', 'CANCEL_LIMIT', 'ARM_GTT')",
            name=op.f("ck_vb_plan_line_kind_known"),
        ),
        sa.CheckConstraint("quantity >= 0", name=op.f("ck_vb_plan_line_quantity_non_negative")),
        sa.CheckConstraint(
            "state IN ('PROPOSED', 'CONFIRMED', 'SENT', 'FILLED', 'REJECTED', 'EXPIRED', 'SKIPPED')",
            name=op.f("ck_vb_plan_line_state_known"),
        ),
        sa.ForeignKeyConstraint(["instrument_id"], ["instrument.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["order_id"], ["vb_order.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["plan_id"], ["vb_plan.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["position_id"], ["vb_position.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vb_plan_line")),
        sa.UniqueConstraint("plan_id", "instrument_id", "kind", name=op.f("uq_vb_plan_line_once")),
    )
    op.create_index(op.f("ix_vb_plan_line_plan"), "vb_plan_line", ["plan_id", "kind"], unique=False)

    op.create_table(
        "vb_plan_skip",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reason IN ('NO_SLEEVE_CAPITAL', 'GATE_SHUT', 'LOCKED_UPPER_CIRCUIT', 'ALREADY_HELD', 'ALREADY_WORKING', 'SESSION_CAP', 'SLOTS_FULL', 'STOP_NOT_BELOW_ENTRY', 'BELOW_MIN_TRADE_VALUE', 'TURNOVER_CAP', 'EXPOSURE_FULL')",
            name=op.f("ck_vb_plan_skip_reason_known"),
        ),
        sa.ForeignKeyConstraint(["instrument_id"], ["instrument.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_id"], ["vb_plan.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vb_plan_skip")),
    )
    op.create_index(op.f("ix_vb_plan_skip_plan"), "vb_plan_skip", ["plan_id"], unique=False)

    op.create_table(
        "vb_session",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("mode", sa.String(length=8), server_default="DRY_RUN", nullable=False),
        sa.Column("gate", sa.String(length=8), server_default="SHUT", nullable=False),
        sa.Column("signals", sa.Integer(), server_default="0", nullable=False),
        sa.Column("orders_placed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("orders_expired", sa.Integer(), server_default="0", nullable=False),
        sa.Column("confirms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("fills", sa.Integer(), server_default="0", nullable=False),
        sa.Column("exits", sa.Integer(), server_default="0", nullable=False),
        sa.Column("first_live_counted", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("counted_for_dry_run_gate", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("plan_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("gate IN ('OPEN', 'SHUT')", name=op.f("ck_vb_session_gate_known")),
        sa.CheckConstraint("mode IN ('DRY_RUN', 'LIVE')", name=op.f("ck_vb_session_mode_known")),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "session_date", name=op.f("pk_vb_session")),
    )

    op.create_table(
        "vb_backtest_run",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("drift", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source IN ('PLANT', 'RESEARCH_EXPORT')", name=op.f("ck_vb_backtest_run_source_known")
        ),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vb_backtest_run")),
    )
    op.create_index(
        op.f("ix_vb_backtest_run_latest"),
        "vb_backtest_run",
        ["user_id", "source", "finished_at"],
        unique=False,
    )


def downgrade() -> None:
    for table in TABLES:
        op.drop_table(table)
