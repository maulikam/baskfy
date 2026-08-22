"""portfolio_sleeve — a portfolio run as several screens plus a slice managed by hand.

M34. The Rebalance Tracker answers "which symbols changed" and knows nothing about money
(`docs/01` §8). That is the right answer for one screen and the wrong one for a portfolio run as
several: the question is how much goes where.

A sleeve is one slice with its own capital and its own source — a saved screen, or `manual` for
capital the owner runs themselves. `docs/04` predates the merge and has none of this; the addendum
is `docs/04b`.

Revision ID: 0013_portfolio_sleeves
Revises: 0012_basket_snapshot
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0013_portfolio_sleeves"
down_revision: str | None = "0012_basket_snapshot"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "portfolio_sleeve",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "portfolio_id",
            sa.BigInteger(),
            sa.ForeignKey("portfolio.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        # NULL for a manual sleeve. ON DELETE SET NULL rather than CASCADE: deleting a screen must
        # not silently delete the capital allocated against it -- the sleeve becomes unsourced and
        # visibly needs attention, which is the honest outcome.
        sa.Column(
            "screen_id",
            sa.BigInteger(),
            sa.ForeignKey("screen.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Money is numeric, never float (house rule 9). Whole rupees is the display precision;
        # the column keeps two places so a future need for paise is not a migration.
        sa.Column("capital", sa.Numeric(18, 2), nullable=False, server_default="0"),
        # How many of the screen's names this sleeve takes, in rank order. Stored because it is
        # the owner's choice -- accepting it and then ignoring it is worse than not offering it.
        sa.Column("top_n", sa.SmallInteger(), nullable=False, server_default="15"),
        sa.Column("sort_order", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("kind IN ('screen', 'manual')", name="portfolio_sleeve_kind"),
        sa.CheckConstraint("capital >= 0", name="portfolio_sleeve_capital_non_negative"),
        sa.CheckConstraint("top_n BETWEEN 1 AND 100", name="portfolio_sleeve_top_n"),
        # A screen sleeve must name a screen; a manual sleeve must not. The rule that keeps
        # "which screen is this sleeve from" answerable without a join returning NULL.
        sa.CheckConstraint(
            "(kind = 'screen' AND screen_id IS NOT NULL) OR (kind = 'manual' AND screen_id IS NULL)",
            name="portfolio_sleeve_source",
        ),
        sa.UniqueConstraint("portfolio_id", "name", name="uq_portfolio_sleeve_name"),
    )
    op.create_index(
        "ix_portfolio_sleeve_portfolio", "portfolio_sleeve", ["portfolio_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_portfolio_sleeve_portfolio", table_name="portfolio_sleeve")
    op.drop_table("portfolio_sleeve")
