"""A manager becomes a person who can join.

Adds to ``cb_manager``: the account holding the identity, the onboarding lifecycle, and a SEBI
registration that can actually be checked. Adds ``cb_manager_revenue_share`` as a dark Track-B
table with no default rate.

The two pre-existing seed rows (the engine and the operator) are backfilled to ``APPROVED``
rather than ``DRAFT``: they were publishing baskets before a lifecycle existed, and dropping them
into DRAFT would un-publish live baskets to satisfy a state machine that arrived afterwards.
Their ``user_id`` stays NULL — the engine is a pipeline, not a person.

``downgrade`` drops the revenue-share table outright. That is safe today precisely because the
surface is dark: fee collection is disabled, so no agreement can have been acted on. If this
migration is ever reversed after the flag has been flipped, the table must be archived first —
said here rather than assumed, because the reversal is cheap now and expensive later.

Revision ID: 0020_manager_identity
Revises: 0019_portfolio_graph
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_manager_identity"
down_revision: str | None = "0019_portfolio_graph"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATES = ("DRAFT", "SUBMITTED", "APPROVED", "REJECTED", "SUSPENDED")
_REG_TYPES = (
    "NONE",
    "RESEARCH_ANALYST",
    "INVESTMENT_ADVISER",
    "PORTFOLIO_MANAGER",
    "AIF",
    "MF_DISTRIBUTOR",
    "UNKNOWN",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(chr(39) + v + chr(39) for v in values)})"


def upgrade() -> None:
    op.add_column("cb_manager", sa.Column("user_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_cb_manager_user_id_app_user", "cb_manager", "app_user", ["user_id"], ["id"]
    )
    op.create_index(
        "uq_cb_manager_user_id",
        "cb_manager",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )

    op.add_column(
        "cb_manager",
        sa.Column("state", sa.String(), nullable=False, server_default=sa.text("'DRAFT'")),
    )
    # Every row that exists at this point is a seed row that was already publishing.
    op.execute("UPDATE cb_manager SET state = 'APPROVED'")
    op.create_check_constraint("cb_manager_state", "cb_manager", _in("state", _STATES))

    op.add_column(
        "cb_manager",
        sa.Column("sebi_reg_type", sa.String(), nullable=False, server_default=sa.text("'NONE'")),
    )
    op.add_column("cb_manager", sa.Column("sebi_reg_valid_from", sa.Date(), nullable=True))
    op.add_column("cb_manager", sa.Column("sebi_reg_valid_to", sa.Date(), nullable=True))
    op.add_column(
        "cb_manager",
        sa.Column("sebi_reg_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    # A pre-existing row carrying a number but no type would fail the pairing check below, so
    # classify it as UNKNOWN for a human rather than guessing which registration it is.
    op.execute("UPDATE cb_manager SET sebi_reg_type = 'UNKNOWN' WHERE sebi_reg_no IS NOT NULL")
    op.create_check_constraint(
        "cb_manager_sebi_reg_type", "cb_manager", _in("sebi_reg_type", _REG_TYPES)
    )
    op.create_check_constraint(
        "cb_manager_sebi_reg_pairing",
        "cb_manager",
        "(sebi_reg_type = 'NONE' AND sebi_reg_no IS NULL) OR "
        "(sebi_reg_type <> 'NONE' AND sebi_reg_no IS NOT NULL)",
    )
    op.create_check_constraint(
        "cb_manager_sebi_reg_window",
        "cb_manager",
        "sebi_reg_valid_to IS NULL OR sebi_reg_valid_from IS NULL OR "
        "sebi_reg_valid_to >= sebi_reg_valid_from",
    )

    op.create_table(
        "cb_manager_revenue_share",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("manager_id", sa.BigInteger(), nullable=False),
        # Deliberately no server_default: D7 amounts are human-track.
        sa.Column("rate_bps", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["manager_id"],
            ["cb_manager.id"],
            name="fk_cb_manager_revenue_share_manager_id_cb_manager",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("rate_bps BETWEEN 0 AND 10000", name="cb_manager_revenue_share_rate"),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="cb_manager_revenue_share_window",
        ),
    )
    op.create_index(
        "ix_cb_manager_revenue_share_manager_id", "cb_manager_revenue_share", ["manager_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_cb_manager_revenue_share_manager_id", table_name="cb_manager_revenue_share")
    op.drop_table("cb_manager_revenue_share")

    # Check constraints are dropped by their bare name: `op.drop_constraint` re-applies the
    # metadata naming convention, so passing the rendered `ck_...` name yields `ck_..._ck_...`.
    for name in (
        "cb_manager_sebi_reg_window",
        "cb_manager_sebi_reg_pairing",
        "cb_manager_sebi_reg_type",
        "cb_manager_state",
    ):
        op.drop_constraint(name, "cb_manager", type_="check")
    for column in (
        "sebi_reg_verified_at",
        "sebi_reg_valid_to",
        "sebi_reg_valid_from",
        "sebi_reg_type",
        "state",
    ):
        op.drop_column("cb_manager", column)

    op.drop_index("uq_cb_manager_user_id", table_name="cb_manager")
    op.drop_constraint("fk_cb_manager_user_id_app_user", "cb_manager", type_="foreignkey")
    op.drop_column("cb_manager", "user_id")
