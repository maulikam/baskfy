"""The swing book's schema — ``docs/swing/03-data-model.md``, module SW2.

Twelve tables, one migration. They are one migration rather than one per table because they are
one *thing*: a watchlist without a plan, or a plan without positions, is not a half-working swing
book — it is a schema no code can use, and alembic history is linear, so splitting them would
only serialise the modules that build on them.

Nothing here alters an existing table. The swing book joins ``instrument``, ``ohlcv_daily``,
``factor_daily``, ``index_snapshot_daily`` and the desk's order journal, and owns none of them.

THREE THINGS TO KNOW BEFORE READING THE DDL
-------------------------------------------
**Every table carries ``user_id``.** ``docs/swing/02-scope-and-gating.md`` Track C §6 requires it
so that the day multi-tenancy arrives (D3, counsel) nothing needs a migration. Two of the tables
``03`` sketches with a market-wide key — ``sw_setup_daily`` and ``sw_market_daily`` — therefore
have it in their primary key instead. That is not a widening of scope: both are computed through
the liquidity floors stored in ``sw_config``, which are per-user settings, so a row of either was
already a statement about one user's universe. ``docs/swing/DECISIONS-SW.md`` SW2.1.

**``sw_setup_daily`` is a snapshot, not a view.** The same rule ``market_health_daily`` follows,
for a stronger reason: recalibrating a detector must never rewrite the record of what the system
saw on the morning a trade was taken.

**The stop can only ever rise.** ``ck_sw_position_stop_never_below_initial`` is the floor a bug
cannot get under. The rule itself — "never widen a stop" — is enforced where the update happens
(``stops.apply`` takes ``max(stop, new_stop)``; the desk refuses a ``RAISE_GTT_STOP`` below the
resting trigger), and this constraint is the database saying so a second time, because that rule
is the difference between a losing trade and a blown account.

WHAT THIS MIGRATION DELIBERATELY DOES NOT DO
--------------------------------------------
It seeds nothing. ``sw_config`` for the sole user is written by ``baskfy_api.seed`` (``make
seed``), because a migration that inserts a row keyed on ``BASKFY_SOLE_USER_ID`` would embed an
environment variable into schema history. The seeded row has ``sleeve_capital_inr = 0``, which is
the state in which the book plans nothing at all.

Revision ID: 0028_swing
Revises: 0027_instrument_null_dedup
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028_swing"
down_revision: str | None = "0027_instrument_null_dedup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Dropped in reverse dependency order by :func:`downgrade`. Listed once, so a table added to
#: :func:`upgrade` and forgotten here fails ``make downgrade`` rather than leaking into the next
#: developer's database.
TABLES: tuple[str, ...] = (
    "sw_signal",
    "sw_plan_line",
    "sw_plan_skip",
    "sw_fill",
    "sw_position",
    "sw_plan",
    "sw_watch",
    "sw_setup_daily",
    "sw_market_daily",
    "sw_session",
    "sw_config_audit",
    "sw_config",
)


def upgrade() -> None:
    op.create_table(
        "sw_config",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "sleeve_capital_inr",
            sa.Numeric(precision=20, scale=2),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "risk_per_trade_pct",
            sa.Numeric(precision=5, scale=3),
            server_default="0.500",
            nullable=False,
        ),
        sa.Column(
            "max_position_pct",
            sa.Numeric(precision=5, scale=2),
            server_default="20.00",
            nullable=False,
        ),
        sa.Column("max_open_positions", sa.SmallInteger(), server_default="8", nullable=False),
        sa.Column("or_window_minutes", sa.SmallInteger(), server_default="5", nullable=False),
        sa.Column("stop_mode", sa.String(), server_default="LOW_OF_DAY", nullable=False),
        sa.Column(
            "adr_min_pct", sa.Numeric(precision=10, scale=2), server_default="3.50", nullable=False
        ),
        sa.Column(
            "turnover_min_inr",
            sa.Numeric(precision=20, scale=2),
            server_default="50000000",
            nullable=False,
        ),
        sa.Column(
            "price_min", sa.Numeric(precision=18, scale=2), server_default="20.00", nullable=False
        ),
        sa.Column("exposure_level", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column(
            "first_live_sessions_left", sa.SmallInteger(), server_default="5", nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.CheckConstraint(
            "stop_mode IN ('LOW_OF_DAY', 'OPENING_RANGE_LOW')",
            name=op.f("ck_sw_config_stop_mode_known"),
        ),
        sa.CheckConstraint(
            "exposure_level >= 0 AND exposure_level <= 3",
            name=op.f("ck_sw_config_exposure_level_range"),
        ),
        sa.CheckConstraint(
            "first_live_sessions_left >= 0",
            name=op.f("ck_sw_config_first_live_sessions_non_negative"),
        ),
        sa.CheckConstraint(
            "max_open_positions > 0", name=op.f("ck_sw_config_max_positions_positive")
        ),
        sa.CheckConstraint("max_position_pct > 0", name=op.f("ck_sw_config_position_pct_positive")),
        sa.CheckConstraint(
            "or_window_minutes IN (1, 5, 60)", name=op.f("ck_sw_config_or_window_known")
        ),
        sa.CheckConstraint("risk_per_trade_pct > 0", name=op.f("ck_sw_config_risk_positive")),
        sa.CheckConstraint(
            "sleeve_capital_inr >= 0", name=op.f("ck_sw_config_capital_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_sw_config_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_sw_config")),
    )
    op.create_table(
        "sw_config_audit",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("changed_by", sa.String(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_sw_config_audit_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sw_config_audit")),
    )
    op.create_index(
        "ix_sw_config_audit_user_id_changed_at",
        "sw_config_audit",
        ["user_id", "changed_at"],
        unique=False,
    )
    op.create_table(
        "sw_market_daily",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("constituent_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("pct_up_strong_1m", sa.Numeric(precision=7, scale=4), nullable=True),
        sa.Column("pct_new_52w_high", sa.Numeric(precision=7, scale=4), nullable=True),
        sa.Column("pct_above_ma_slow", sa.Numeric(precision=7, scale=4), nullable=True),
        sa.Column("index_slug", sa.String(), nullable=True),
        sa.Column("index_close", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("index_ma_fast", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("index_ma_slow", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("gate", sa.String(), nullable=False),
        sa.Column("exposure_level", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("max_open_positions", sa.SmallInteger(), nullable=False),
        sa.Column("max_exposure_pct", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("new_entries_allowed", sa.Boolean(), nullable=False),
        sa.Column("parabolic_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "gate IN ('GREEN', 'AMBER', 'RED')", name=op.f("ck_sw_market_daily_gate_known")
        ),
        sa.CheckConstraint(
            "exposure_level >= 0 AND exposure_level <= 3",
            name=op.f("ck_sw_market_daily_exposure_level_range"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_sw_market_daily_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "date", name=op.f("pk_sw_market_daily")),
    )
    op.create_table(
        "sw_plan",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("plan_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("plan_hash", sa.String(), nullable=False),
        sa.Column("gate", sa.String(), nullable=False),
        sa.Column("exposure_level", sa.SmallInteger(), nullable=False),
        sa.Column(
            "total_risk_inr", sa.Numeric(precision=12, scale=2), server_default="0", nullable=False
        ),
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
        sa.CheckConstraint("gate IN ('GREEN', 'AMBER', 'RED')", name=op.f("ck_sw_plan_gate_known")),
        sa.CheckConstraint(
            "source IN ('EOD_PREVIEW', 'MORNING', 'SIGNAL')", name=op.f("ck_sw_plan_source_known")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_sw_plan_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sw_plan")),
        sa.UniqueConstraint("plan_id", name=op.f("uq_sw_plan_plan_id")),
    )
    op.create_index("ix_sw_plan_user_id_as_of", "sw_plan", ["user_id", "as_of"], unique=False)
    op.create_table(
        "sw_session",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("monitor_ran", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("plan_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("signals", sa.Integer(), server_default="0", nullable=False),
        sa.Column("confirms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("fills", sa.Integer(), server_default="0", nullable=False),
        sa.Column("manage_actions", sa.Integer(), server_default="0", nullable=False),
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
        sa.CheckConstraint("mode IN ('DRY_RUN', 'LIVE')", name=op.f("ck_sw_session_mode_known")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_sw_session_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "session_date", name=op.f("pk_sw_session")),
    )
    op.create_table(
        "sw_plan_skip",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=True),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reason IN ('NOT_TRADEABLE_SETUP', 'GATE_RED', 'TIER_FULL', 'EXPOSURE_FULL', 'ALREADY_HELD', 'LOCKED_UPPER_CIRCUIT', 'SIZE_REFUSED')",
            name=op.f("ck_sw_plan_skip_reason_known"),
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name=op.f("fk_sw_plan_skip_instrument_id_instrument"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["sw_plan.id"],
            name=op.f("fk_sw_plan_skip_plan_id_sw_plan"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_sw_plan_skip_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sw_plan_skip")),
    )
    op.create_index("ix_sw_plan_skip_plan_id", "sw_plan_skip", ["plan_id"], unique=False)
    op.create_table(
        "sw_position",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("broker_account_id", sa.BigInteger(), nullable=True),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("setup", sa.String(), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("entry_avg", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("quantity_entered", sa.Integer(), nullable=False),
        sa.Column("initial_stop", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("stop", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("gtt_id", sa.String(), nullable=True),
        sa.Column("gtt_trigger", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("gtt_armed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trail", sa.String(), nullable=False),
        sa.Column("partial_done", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("partial_date", sa.Date(), nullable=True),
        sa.Column("quantity_open", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(), server_default="OPEN", nullable=False),
        sa.Column("closed_on", sa.Date(), nullable=True),
        sa.Column("exit_avg", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("close_reason", sa.String(), nullable=True),
        sa.Column("r_multiple", sa.Numeric(precision=8, scale=2), nullable=True),
        sa.Column("pnl_inr", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("simulated", sa.Boolean(), server_default="true", nullable=False),
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
            "close_reason IN ('PARTIAL_INTO_STRENGTH', 'BREAKEVEN_AFTER_PARTIAL', 'BREAKEVEN_AT_R', 'CLOSE_BELOW_TRAIL_MA', 'EP_FAILED_RED_ON_DAY', 'HARD_STOP_HIT', 'NOTHING_TO_DO', 'MANUAL')",
            name=op.f("ck_sw_position_close_reason_known"),
        ),
        sa.CheckConstraint(
            "setup IN ('FLAG', 'EP', 'PARABOLIC_SHORT')", name=op.f("ck_sw_position_setup_known")
        ),
        sa.CheckConstraint(
            "state IN ('OPEN', 'PARTIAL', 'CLOSED')", name=op.f("ck_sw_position_state_known")
        ),
        sa.CheckConstraint("trail IN ('MA10', 'MA20')", name=op.f("ck_sw_position_trail_known")),
        sa.CheckConstraint(
            "initial_stop < entry_avg", name=op.f("ck_sw_position_initial_stop_below_entry")
        ),
        sa.CheckConstraint(
            "quantity_open <= quantity_entered",
            name=op.f("ck_sw_position_quantity_open_within_entered"),
        ),
        sa.CheckConstraint(
            "quantity_open >= 0", name=op.f("ck_sw_position_quantity_open_non_negative")
        ),
        sa.CheckConstraint(
            "stop >= initial_stop", name=op.f("ck_sw_position_stop_never_below_initial")
        ),
        sa.ForeignKeyConstraint(
            ["broker_account_id"],
            ["broker_account.id"],
            name=op.f("fk_sw_position_broker_account_id_broker_account"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name=op.f("fk_sw_position_instrument_id_instrument"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_sw_position_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sw_position")),
    )
    op.create_index("ix_sw_position_instrument_id", "sw_position", ["instrument_id"], unique=False)
    op.create_index(
        "ix_sw_position_user_id_state", "sw_position", ["user_id", "state"], unique=False
    )
    op.create_table(
        "sw_setup_daily",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("setup", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("score", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("close", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("trigger", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("stop_ref", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("pivot_high", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("adj_factor", sa.Numeric(precision=18, scale=10), nullable=True),
        sa.Column("adr_pct", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("prior_move_pct", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("base_depth_pct", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("tightness_adr", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("dryup_ratio", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("dist_ma_fast_pct", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("dist_ma_slow_pct", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("rvol", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("gap_pct", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("turnover_avg", sa.BigInteger(), nullable=True),
        sa.Column("base_bars", sa.SmallInteger(), nullable=True),
        sa.Column("up_streak", sa.SmallInteger(), nullable=True),
        sa.Column("locked_upper_circuit", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("sector_slug", sa.String(), nullable=True),
        sa.Column("listed_within_2y", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("pipeline_run_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "setup IN ('FLAG', 'EP', 'PARABOLIC_SHORT')", name=op.f("ck_sw_setup_daily_setup_known")
        ),
        sa.CheckConstraint(
            "status IN ('SETTING_UP', 'BREAKOUT_TODAY', 'GAP_DAY', 'RUNNING', 'EXHAUSTION')",
            name=op.f("ck_sw_setup_daily_status_known"),
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name=op.f("fk_sw_setup_daily_instrument_id_instrument"),
        ),
        sa.ForeignKeyConstraint(
            ["pipeline_run_id"],
            ["pipeline_run.id"],
            name=op.f("fk_sw_setup_daily_pipeline_run_id_pipeline_run"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_sw_setup_daily_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "user_id", "date", "instrument_id", "setup", name=op.f("pk_sw_setup_daily")
        ),
    )
    op.create_index(
        "ix_sw_setup_daily_date_setup_score",
        "sw_setup_daily",
        ["date", "setup", "score"],
        unique=False,
    )
    op.create_index(
        "ix_sw_setup_daily_instrument_id_date",
        "sw_setup_daily",
        ["instrument_id", "date"],
        unique=False,
    )
    op.create_table(
        "sw_watch",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("setup", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("added_on", sa.Date(), nullable=False),
        sa.Column("expires_on", sa.Date(), nullable=True),
        sa.Column("trigger", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("stop_ref", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("setup_daily_date", sa.Date(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("catalyst", sa.Text(), nullable=True),
        sa.Column("state", sa.String(), server_default="WATCHING", nullable=False),
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
            "setup IN ('FLAG', 'EP', 'PARABOLIC_SHORT')", name=op.f("ck_sw_watch_setup_known")
        ),
        sa.CheckConstraint(
            "source IN ('DETECTOR', 'MANUAL')", name=op.f("ck_sw_watch_source_known")
        ),
        sa.CheckConstraint(
            "state IN ('WATCHING', 'TRIGGERED', 'EXPIRED', 'DISMISSED')",
            name=op.f("ck_sw_watch_state_known"),
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instrument.id"], name=op.f("fk_sw_watch_instrument_id_instrument")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_sw_watch_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sw_watch")),
        sa.UniqueConstraint(
            "user_id",
            "instrument_id",
            "setup",
            "added_on",
            name="uq_sw_watch_user_id_instrument_id_setup_added_on",
        ),
    )
    op.create_index("ix_sw_watch_user_id_state", "sw_watch", ["user_id", "state"], unique=False)
    op.create_table(
        "sw_fill",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("position_id", sa.BigInteger(), nullable=False),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("journal_ref", sa.String(), nullable=True),
        sa.Column("simulated", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name=op.f("ck_sw_fill_side_known")),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_sw_fill_quantity_positive")),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["sw_position.id"],
            name=op.f("fk_sw_fill_position_id_sw_position"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_sw_fill_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sw_fill")),
    )
    op.create_index("ix_sw_fill_position_id", "sw_fill", ["position_id"], unique=False)
    op.create_table(
        "sw_plan_line",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("setup", sa.String(), nullable=True),
        sa.Column("quantity", sa.Integer(), server_default="0", nullable=False),
        sa.Column("trigger", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("stop", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column(
            "risk_inr", sa.Numeric(precision=12, scale=2), server_default="0", nullable=False
        ),
        sa.Column(
            "position_value", sa.Numeric(precision=20, scale=2), server_default="0", nullable=False
        ),
        sa.Column("trail", sa.String(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("state", sa.String(), server_default="PROPOSED", nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("journal_ref", sa.String(), nullable=True),
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
            "kind IN ('BUY_ON_TRIGGER', 'SELL_AT_OPEN', 'RAISE_GTT_STOP')",
            name=op.f("ck_sw_plan_line_kind_known"),
        ),
        sa.CheckConstraint(
            "state IN ('PROPOSED', 'CONFIRMED', 'SENT', 'FILLED', 'REJECTED', 'EXPIRED', 'SKIPPED')",
            name=op.f("ck_sw_plan_line_state_known"),
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name=op.f("fk_sw_plan_line_instrument_id_instrument"),
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["sw_plan.id"],
            name=op.f("fk_sw_plan_line_plan_id_sw_plan"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["position_id"],
            ["sw_position.id"],
            name=op.f("fk_sw_plan_line_position_id_sw_position"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_sw_plan_line_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sw_plan_line")),
        sa.UniqueConstraint("client_id", name="uq_sw_plan_line_client_id"),
    )
    op.create_index("ix_sw_plan_line_plan_id", "sw_plan_line", ["plan_id"], unique=False)
    op.create_table(
        "sw_signal",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("watch_id", sa.BigInteger(), nullable=True),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("setup", sa.String(), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("raised_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("or_window_minutes", sa.SmallInteger(), nullable=True),
        sa.Column("range_high", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("range_low", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("low_of_day", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("last_price", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("entry", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("stop", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("plan_line_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "setup IN ('FLAG', 'EP', 'PARABOLIC_SHORT')", name=op.f("ck_sw_signal_setup_known")
        ),
        sa.CheckConstraint(
            "state IN ('TRIGGERED', 'WAITING', 'RANGE_INCOMPLETE', 'BELOW_PIVOT', 'LOCKED_UPPER_CIRCUIT', 'SESSION_OVER')",
            name=op.f("ck_sw_signal_state_known"),
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instrument.id"], name=op.f("fk_sw_signal_instrument_id_instrument")
        ),
        sa.ForeignKeyConstraint(
            ["plan_line_id"],
            ["sw_plan_line.id"],
            name=op.f("fk_sw_signal_plan_line_id_sw_plan_line"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_sw_signal_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["watch_id"],
            ["sw_watch.id"],
            name=op.f("fk_sw_signal_watch_id_sw_watch"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sw_signal")),
    )
    op.create_index(
        "ix_sw_signal_instrument_id_session_date",
        "sw_signal",
        ["instrument_id", "session_date"],
        unique=False,
    )
    op.create_index(
        "ix_sw_signal_user_id_session_date", "sw_signal", ["user_id", "session_date"], unique=False
    )


def downgrade() -> None:
    """Drop the twelve tables. Indexes and constraints go with them.

    ``CASCADE`` is not used and is not needed: nothing outside this migration references an
    ``sw_`` table, which is the point of the prefix. Reverse dependency order, so a foreign key
    never outlives the table it points at.
    """
    for table in TABLES:
        op.drop_table(table)
