"""The "live at any login time" pack's five tables — ``docs/live/PLAN.md``, LV2.

``lv_protection_issue`` (what the reconciler found wrong with a position's protection),
``lv_heartbeat`` (the last word from each desk process), ``lv_exit_order`` (a sell the broker
accepted and has not yet filled — VBT's exit is booked from the fill, not the placement),
``lv_adoption`` (a hand-bought holding adopted into a sleeve, explicitly) and ``risk_ledger``
(the account-wide risk state, one JSON row per IST day, row-locked by every process that
places an order). Models: ``baskfy_core.models.live``.

Public schema, beside ``sw_*``/``tw_*``/``vb_*``, for the same reason those are: the desk's
stores prefix ``public.`` and the API reads the two state tables without a second connection.

The downgrade drops all five. None is an input to a rank, a size or a plan; the reconciler
re-derives issues on its next pass, heartbeats are re-written within a minute, and the risk
ledger's day row is rebuilt from the day's orders by the first ``pre_order`` — the JSON file
the risk manager kept before LV3 remains the fallback.

Revision ID: 0055_live_desk_state
Revises: 0054_candidate_review_label
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0055_live_desk_state"
down_revision: str | None = "0054_candidate_review_label"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: ``baskfy_core.models.live``, written out because a migration is frozen.
SLEEVES: tuple[str, ...] = ("swing", "twt", "vbt")
ISSUE_KINDS: tuple[str, ...] = (
    "NAKED",
    "GTT_MISSING",
    "GTT_OVERSIZED",
    "GTT_UNDERSIZED",
    "GTT_TRIGGERED_UNFILLED",
    "EXTERNAL_EXIT",
    "STOP_REJECTED",
)
EXIT_ORDER_STATES: tuple[str, ...] = ("SENT", "PARTIAL", "FILLED", "CANCELLED", "REJECTED")

TABLES: tuple[str, ...] = (
    "risk_ledger",
    "lv_adoption",
    "lv_exit_order",
    "lv_heartbeat",
    "lv_protection_issue",
)


def _in(table: str, name: str, column: str, values: tuple[str, ...]) -> sa.CheckConstraint:
    quoted = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({quoted})", name=op.f(f"ck_{table}_{name}"))


def _user_id() -> sa.Column[int]:
    return sa.Column("user_id", sa.BigInteger(), nullable=False)


def _user_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["user_id"], ["app_user.id"], name=op.f(f"fk_{table}_user_id_app_user"), ondelete="CASCADE"
    )


def _created_at() -> sa.Column[object]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "lv_protection_issue",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        _user_id(),
        sa.Column("sleeve", sa.String(length=8), nullable=False),
        sa.Column("position_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        _in("lv_protection_issue", "sleeve_known", "sleeve", SLEEVES),
        _in("lv_protection_issue", "kind_known", "kind", ISSUE_KINDS),
        _user_fk("lv_protection_issue"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lv_protection_issue")),
    )
    op.create_index(
        "uq_lv_protection_issue_open",
        "lv_protection_issue",
        ["user_id", "sleeve", "position_id", "kind"],
        unique=True,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )
    op.create_index(
        "ix_lv_protection_issue_open_by_sleeve",
        "lv_protection_issue",
        ["user_id", "sleeve", "resolved_at"],
        unique=False,
    )

    op.create_table(
        "lv_heartbeat",
        _user_id(),
        sa.Column("process", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("detail", sa.Text(), server_default="", nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        _user_fk("lv_heartbeat"),
        sa.PrimaryKeyConstraint("user_id", "process", name=op.f("pk_lv_heartbeat")),
    )

    op.create_table(
        "lv_exit_order",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        _user_id(),
        sa.Column("sleeve", sa.String(length=8), nullable=False),
        sa.Column("position_id", sa.BigInteger(), nullable=False),
        sa.Column("line_id", sa.BigInteger(), nullable=True),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("broker_order_id", sa.String(length=64), nullable=True),
        sa.Column("client_id", sa.String(length=128), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("reference_price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="SENT", nullable=False),
        sa.Column("filled_quantity", sa.Integer(), server_default="0", nullable=False),
        sa.Column("avg_fill_price", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("reason", sa.String(length=24), nullable=False),
        sa.Column("simulated", sa.Boolean(), server_default="true", nullable=False),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _in("lv_exit_order", "sleeve_known", "sleeve", SLEEVES),
        _in("lv_exit_order", "state_known", "state", EXIT_ORDER_STATES),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_lv_exit_order_quantity_positive")),
        sa.CheckConstraint(
            "filled_quantity >= 0", name=op.f("ck_lv_exit_order_filled_non_negative")
        ),
        sa.CheckConstraint(
            "filled_quantity <= quantity", name=op.f("ck_lv_exit_order_filled_within_quantity")
        ),
        _user_fk("lv_exit_order"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lv_exit_order")),
    )
    op.create_index(
        "ix_lv_exit_order_open", "lv_exit_order", ["user_id", "sleeve", "state"], unique=False
    )
    op.create_index(
        "ix_lv_exit_order_broker", "lv_exit_order", ["user_id", "broker_order_id"], unique=False
    )

    op.create_table(
        "lv_adoption",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        _user_id(),
        sa.Column("sleeve", sa.String(length=8), nullable=False),
        sa.Column("position_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("avg_cost", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("gtt_id", sa.String(length=64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        _created_at(),
        _in("lv_adoption", "sleeve_known", "sleeve", SLEEVES),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_lv_adoption_quantity_positive")),
        sa.CheckConstraint("avg_cost > 0", name=op.f("ck_lv_adoption_cost_positive")),
        _user_fk("lv_adoption"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lv_adoption")),
    )

    op.create_table(
        "risk_ledger",
        _user_id(),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _user_fk("risk_ledger"),
        sa.PrimaryKeyConstraint("user_id", "day", name=op.f("pk_risk_ledger")),
    )


def downgrade() -> None:
    for table in TABLES:
        op.drop_table(table)
