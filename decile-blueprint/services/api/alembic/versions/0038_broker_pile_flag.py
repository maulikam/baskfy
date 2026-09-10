"""Mark the broker's own holding group explicitly, instead of inferring it.

`broker_holdings_sync` creates one portfolio per synced account — "Zerodha holdings" — and it is
where every share lands before the user sorts it. Since 0035 that group has to be told apart from
a real portfolio, because its rows are the UNALLOCATED remainder rather than a strategy's slice:
count them as allocated and §6.7's picker offers "0 of 0 free" on every row, which is what Maulik
saw on 11 Sep 2026.

The obvious inference — `source = 'HOLDING_GROUP' AND broker_account_id IS NOT NULL` — is WRONG,
and a test caught it. `POST /portfolio` sets `broker_account_id` on any group whose holdings all
come from one account (the column means "everything under this is attributable to one broker"),
and §6.7 offers HOLDING_GROUP as a source. So a user grouping their IT stocks at Zerodha produces
a row indistinguishable from the pile, and it lost its value the moment it was created.

That inference was also already load-bearing in `portfolio_for_broker_account`, which looks the
pile up by exactly those two columns — so a user's own group could have been returned as the pile
and had a sync write over it. This migration closes that too.

A boolean rather than a new `PortfolioSource` value: source decides the headline metric (§5.2)
and appears as a badge on every surface, and the pile genuinely is "shares you already own,
grouped". What distinguishes it is not what it means but WHO MADE IT — the system, as a container
for things nobody has sorted yet. That is a different fact, and it gets its own column.

**The backfill names the pile by its name.** `broker_holdings_sync` builds it as
`f"{broker_name} holdings"`, and the two other conditions must hold as well. Deliberately narrow:
a false positive here would hide a real portfolio from the overview and zero its value, which is
far worse than a false negative — a pile that is missed simply keeps behaving as it did before
0035, visibly, and can be flagged by hand.

Revision ID: 0038_broker_pile_flag
Revises: 0037_vbt
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_broker_pile_flag"
down_revision: str | None = "0037_vbt"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "portfolio",
        sa.Column("is_broker_pile", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute(
        sa.text(
            "UPDATE portfolio SET is_broker_pile = true "
            "WHERE source = 'HOLDING_GROUP' AND broker_account_id IS NOT NULL "
            "AND name LIKE '%% holdings'"
        )
    )
    # One pile per broker account, and the database says so. `portfolio_for_broker_account` used
    # to `scalar_one_or_none()` a query that could legitimately match several rows; now the only
    # way to have two is a write the index refuses.
    op.create_index(
        "uq_portfolio_one_pile_per_broker_account",
        "portfolio",
        ["user_id", "broker_account_id"],
        unique=True,
        postgresql_where=sa.text("is_broker_pile"),
    )


def downgrade() -> None:
    op.drop_index("uq_portfolio_one_pile_per_broker_account", table_name="portfolio")
    op.drop_column("portfolio", "is_broker_pile")
