"""basket_snapshot — the nightly chain's answer to "what does the strategy want today".

M30. `/baskets` computed its basket live on every request: every bar the 271 scanned symbols have
ever had, loaded into Polars, scored, and turned into a plan. That was about a second while the
history was two years deep. After M29 took it to nine, it was **67 seconds**.

Nothing about the computation was wrong, and none of it needs to happen per request: the inputs
change once a night, when the pipeline publishes. So the pipeline computes it once and stores the
result here, and the page reads a row.

The payload is stored whole, as JSONB, rather than shredded into columns. It is the response body
of a read-only endpoint — a record of what the strategy wanted on a date, not something anything
queries across. Shredding it would mean a migration every time the basket page gains a column,
which is the opposite of what a snapshot table is for.

Revision ID: 0012_basket_snapshot
Revises: 0011_breadth_above_20dma
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_basket_snapshot"
down_revision: str | None = "0011_breadth_above_20dma"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "basket_snapshot",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("as_of", sa.Date(), nullable=False),
        #: docs/06's cache key: the screen definition, the as-of and the data version. Two
        #: snapshots with the same one are the same basket.
        sa.Column("screen_run_id", sa.String(), nullable=False),
        sa.Column("data_version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        #: How long the computation took, so a regression like M29's is visible in the table
        #: rather than only in somebody's patience.
        sa.Column("computed_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # One row per (date, version). A re-run of the same night replaces its own row rather
        # than piling up, which is house rule 7.
        sa.UniqueConstraint("as_of", "data_version", name="uq_basket_snapshot_as_of_version"),
    )
    # The page always wants the newest. This is the whole access pattern.
    op.create_index("ix_basket_snapshot_as_of", "basket_snapshot", ["as_of"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_basket_snapshot_as_of", table_name="basket_snapshot")
    op.drop_table("basket_snapshot")
