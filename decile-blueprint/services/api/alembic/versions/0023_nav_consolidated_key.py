"""Let ``portfolio_nav_daily`` actually hold the consolidated row.

``PORTFOLIO_REDESIGN.md`` §5.1 and acceptance criterion 1.

WHY THIS EXISTS AT ALL — a contradiction inside 0022, not a change of design
---------------------------------------------------------------------------
0022 says, in its own words and in two places, that ``portfolio_id IS NULL`` **is** the
consolidated series: the column is declared ``nullable=True`` and the comment beside it reads
"NULL portfolio_id is the consolidated series — the same row shape, so the combined chart and a
single portfolio's chart read one table rather than two". ``PortfolioNavDaily`` in
``baskfy_core.models.accounts`` repeats it, and §5.1 depends on it.

The table it produced cannot store that row. ``PrimaryKeyConstraint("user_id", "date",
"portfolio_id")`` makes every one of its columns ``NOT NULL`` in PostgreSQL regardless of the
``nullable=True`` on the column, so the consolidated insert fails::

    ERROR:  null value in column "portfolio_id" of relation "portfolio_nav_daily"
            violates not-null constraint

So this is not a second opinion about the schema. It is the schema 0022 describes, made
writable. Nothing about the row shape, the column list or the intended semantics changes.

WHY A UNIQUE INDEX AND NOT A PRIMARY KEY
----------------------------------------
The uniqueness that matters is unchanged — one row per ``(user_id, date, portfolio_id)``, and
exactly one consolidated row per user per day. A primary key cannot express it, because a primary
key forbids the NULL that names the consolidated series.

``UNIQUE ... NULLS NOT DISTINCT`` (PostgreSQL 15+; this deployment is 16.6) expresses it exactly:
two consolidated rows for the same user and date collide rather than both being accepted, which
is what the primary key was there for. It also serves as an ``ON CONFLICT (user_id, date,
portfolio_id)`` arbiter, so the nightly job's upsert — house rule 7, re-running a date produces
identical rows — keeps working unchanged.

The rejected alternative was a sentinel ``portfolio_id`` (0, or a per-user "consolidated"
portfolio row). It would have kept the primary key, and it would have put a fake portfolio into
``portfolio`` — something that can be renamed, deleted, given a benchmark, or counted by any
query that walks a user's portfolios. §4.1's totals are the one place this product cannot afford
a plausible-looking extra row.

Reversible: :func:`downgrade` restores 0022's primary key exactly, after deleting the
consolidated rows that could not have existed under it.

Revision ID: 0023_nav_consolidated_key
Revises: 0022_portfolio_redesign
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_nav_consolidated_key"
down_revision: str | None = "0022_portfolio_redesign"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Follows ``NAMING_CONVENTION["uq"]`` in ``baskfy_core.models.base`` — ``uq_%(table_name)s_``
#: plus the column names — so a later autogenerate does not see a stranger and propose dropping
#: it.
INDEX_NAME = "uq_portfolio_nav_daily_user_id_date_portfolio_id"


def upgrade() -> None:
    op.drop_constraint("pk_portfolio_nav_daily", "portfolio_nav_daily", type_="primary")
    op.alter_column(
        "portfolio_nav_daily",
        "portfolio_id",
        existing_type=sa.BigInteger(),
        nullable=True,
    )
    # Raw SQL rather than ``op.create_index(..., postgresql_nulls_not_distinct=True)``: the
    # dialect keyword is recent, and a migration that silently degrades to NULLS DISTINCT on an
    # older SQLAlchemy would let a user accumulate one consolidated row per run. Spelled out, it
    # either works or the migration fails loudly.
    op.execute(
        sa.text(
            f"CREATE UNIQUE INDEX {INDEX_NAME} "
            "ON portfolio_nav_daily (user_id, date, portfolio_id) NULLS NOT DISTINCT"
        )
    )


def downgrade() -> None:
    # The consolidated rows are exactly the rows 0022's key could not hold, so they go first.
    # They are derived data — the nightly job rebuilds any date from holdings, prices and flows —
    # which is why deleting them here is a restore rather than a loss.
    op.execute(sa.text("DELETE FROM portfolio_nav_daily WHERE portfolio_id IS NULL"))
    op.drop_index(INDEX_NAME, table_name="portfolio_nav_daily")
    op.alter_column(
        "portfolio_nav_daily",
        "portfolio_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.create_primary_key(
        "pk_portfolio_nav_daily",
        "portfolio_nav_daily",
        ["user_id", "date", "portfolio_id"],
    )
