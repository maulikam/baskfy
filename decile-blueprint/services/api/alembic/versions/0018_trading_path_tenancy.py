"""P4.1 — tenant columns on the Postgres trading path.

``broker_account`` is the row Law 2's ``broker_account_id`` names. Existing
``cb_investment`` / ``cb_order_batch`` rows are backfilled to a Zerodha account per
``app_user`` (the founder book). Desk SQLite is not touched — ``portfolio.db`` is
unrebuildable evidence.

Revision ID: 0018_trading_path_tenancy
Revises: 0017_cb_basket_from_screen
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0018_trading_path_tenancy"
down_revision: str | None = "0017_cb_basket_from_screen"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "broker_account",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("broker_id", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False, server_default=sa.text("'primary'")),
        sa.Column("kite_user_id", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "broker_id", name="uq_broker_account_user_broker"),
    )
    op.create_index("ix_broker_account_user_id", "broker_account", ["user_id"])

    op.add_column("cb_investment", sa.Column("broker_account_id", sa.BigInteger(), nullable=True))
    op.add_column("cb_order_batch", sa.Column("user_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "cb_order_batch", sa.Column("broker_account_id", sa.BigInteger(), nullable=True)
    )

    op.execute(
        """
        INSERT INTO broker_account (user_id, broker_id, label)
        SELECT u.id, 'zerodha', 'primary'
        FROM app_user u
        WHERE NOT EXISTS (
            SELECT 1 FROM broker_account a
            WHERE a.user_id = u.id AND a.broker_id = 'zerodha'
        )
        """
    )
    op.execute(
        """
        UPDATE cb_investment i
        SET broker_account_id = a.id
        FROM broker_account a
        WHERE a.user_id = i.user_id AND a.broker_id = 'zerodha'
        """
    )
    op.execute(
        """
        UPDATE cb_order_batch b
        SET user_id = i.user_id,
            broker_account_id = i.broker_account_id
        FROM cb_investment i
        WHERE b.investment_id = i.id
        """
    )

    op.alter_column("cb_investment", "broker_account_id", nullable=False)
    op.alter_column("cb_order_batch", "user_id", nullable=False)
    op.alter_column("cb_order_batch", "broker_account_id", nullable=False)

    op.create_foreign_key(
        "fk_cb_investment_broker_account_id_broker_account",
        "cb_investment",
        "broker_account",
        ["broker_account_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_cb_order_batch_user_id_app_user",
        "cb_order_batch",
        "app_user",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_cb_order_batch_broker_account_id_broker_account",
        "cb_order_batch",
        "broker_account",
        ["broker_account_id"],
        ["id"],
    )
    op.create_index("ix_cb_investment_broker_account_id", "cb_investment", ["broker_account_id"])
    op.create_index("ix_cb_order_batch_user_id", "cb_order_batch", ["user_id"])
    op.create_index("ix_cb_order_batch_broker_account_id", "cb_order_batch", ["broker_account_id"])


def downgrade() -> None:
    op.drop_index("ix_cb_order_batch_broker_account_id", table_name="cb_order_batch")
    op.drop_index("ix_cb_order_batch_user_id", table_name="cb_order_batch")
    op.drop_index("ix_cb_investment_broker_account_id", table_name="cb_investment")
    op.drop_constraint(
        "fk_cb_order_batch_broker_account_id_broker_account",
        "cb_order_batch",
        type_="foreignkey",
    )
    op.drop_constraint("fk_cb_order_batch_user_id_app_user", "cb_order_batch", type_="foreignkey")
    op.drop_constraint(
        "fk_cb_investment_broker_account_id_broker_account",
        "cb_investment",
        type_="foreignkey",
    )
    op.drop_column("cb_order_batch", "broker_account_id")
    op.drop_column("cb_order_batch", "user_id")
    op.drop_column("cb_investment", "broker_account_id")
    op.drop_index("ix_broker_account_user_id", table_name="broker_account")
    op.drop_table("broker_account")
