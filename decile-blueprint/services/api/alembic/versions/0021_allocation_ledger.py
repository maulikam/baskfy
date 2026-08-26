"""A portfolio gains a kind and a source, and criterion 2 becomes a database fact.

``PORTFOLIO_REDESIGN.md`` §4.2 and acceptance criterion 2: *a holding can never be in two capital
portfolios*. `baskfy_core.allocation_ledger` already refuses it, but a rule enforced only in the
application is a rule that survives exactly as long as every writer remembers it — and a broker
sync writing in bulk is precisely the writer most likely to forget.

WHY NOT A TRIGGER
-----------------
The natural instinct is a ``BEFORE INSERT`` trigger, the way 0019 attributes a broker account.
Rejected here: a trigger is procedural, runs per row, and is skipped outright by
``ALTER TABLE ... DISABLE TRIGGER`` and by some bulk-load paths — which are the circumstances of
the very import this constraint exists to survive. An index is declarative and the planner
enforces it on every path into the table.

THE SHAPE, AND WHY THE REDUNDANT COLUMN IS NOT REDUNDANT
--------------------------------------------------------
The constraint has to say "at most one CAPITAL row per physical holding", but *kind* lives on
``portfolio`` and a unique index cannot read another table. So:

1. ``portfolio.kind`` arrives, plus ``UNIQUE (id, kind)`` — pointless against the primary key on
   its own, and present only to be the target of (2).
2. ``portfolio_holding.portfolio_kind`` arrives with a **composite foreign key**
   ``(portfolio_id, portfolio_kind) -> portfolio (id, kind)``, ``ON UPDATE CASCADE``. The copy
   cannot drift: the foreign key *is* the synchroniser, so there is no second source of truth to
   keep in step, only a denormalisation the database maintains.
3. A **partial unique index** on ``(instrument_id, broker_account_id) WHERE portfolio_kind =
   'CAPITAL'``. Monitoring rows are not in the index at all, which is §4.1 exactly: a lens
   overlaps freely and never enters a total.

The cascade pays for itself in both directions. Flipping a portfolio CAPITAL -> MONITORING drops
its holdings out of the index with no application code. Flipping MONITORING -> CAPITAL when that
would collide **fails the flip**, which is the correct answer — the alternative is a ledger that
silently double-counts and still balances.

PRE-EXISTING VIOLATIONS ARE REFUSED, NOT RESOLVED
--------------------------------------------------
Building the index over data that already breaks the rule fails with
``UniqueViolationError: Key (instrument_id, broker_account_id)=(6, 6) is duplicated`` — true,
useless, and delivered halfway through a migration. So the violations are looked for first and
reported as symbols and portfolio names, with the count.

The migration does **not** pick a winner. Which portfolio keeps a doubly-allocated holding is a
question only the account's owner can answer — it is §4.3's reconciliation in a different costume
— and a migration that guessed would silently rewrite someone's return series to make its own
DDL succeed. Refusing is the honest failure: the operator resolves the duplicates and runs it
again.

SAFETY
------
``portfolio_holding`` is empty at the time of writing, so the restructure moves no data.
``portfolio`` has rows, so the new columns arrive with a server default, are backfilled, and only
then become NOT NULL. The defaults are then dropped: a default on ``kind`` would let a future
writer create a portfolio without saying which kind it is, and §4.1 makes that a decision, not a
detail.

Revision ID: 0021_allocation_ledger
Revises: 0020_manager_identity
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_allocation_ledger"
down_revision: str | None = "0020_manager_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: §4.1. Mirrors ``baskfy_core.allocation_ledger.PortfolioKind`` — a migration cannot import the
#: application, so the values are restated here and a test asserts the two agree.
_KINDS = ("CAPITAL", "MONITORING")

#: §3. Mirrors ``baskfy_core.allocation_ledger.PortfolioSource``.
_SOURCES = ("SUBSCRIBED", "MY_SCREEN", "MY_STRATEGY", "HOLDING_GROUP")

#: Every portfolio that predates this migration was assembled by hand from broker holdings —
#: there was no subscription, screen or strategy binding to record. HOLDING_GROUP is therefore
#: the true answer for them rather than a convenient one, and §5.2 gives that source the most
#: conservative metric ("since grouped"), so a wrong guess here would understate nothing.
_BACKFILL_SOURCE = "HOLDING_GROUP"

#: Existing portfolios hold real money and sum into net worth, so they are capital portfolios.
#: Backfilling them as MONITORING would silently drop them out of every total.
_BACKFILL_KIND = "CAPITAL"

_ONE_CAPITAL_INDEX = "uq_portfolio_holding_one_capital_portfolio"


def upgrade() -> None:
    op.add_column(
        "portfolio",
        sa.Column("kind", sa.String(), nullable=True),
    )
    op.add_column(
        "portfolio",
        sa.Column("source", sa.String(), nullable=True),
    )
    op.execute(
        sa.text("UPDATE portfolio SET kind = :kind, source = :source").bindparams(
            kind=_BACKFILL_KIND, source=_BACKFILL_SOURCE
        )
    )
    op.alter_column("portfolio", "kind", nullable=False)
    op.alter_column("portfolio", "source", nullable=False)

    op.create_check_constraint(
        "portfolio_kind_known",
        "portfolio",
        sa.column("kind").in_(_KINDS),
    )
    op.create_check_constraint(
        "portfolio_source_known",
        "portfolio",
        sa.column("source").in_(_SOURCES),
    )
    # Redundant against the primary key on its own; it exists so the composite foreign key below
    # has something to reference.
    op.create_unique_constraint("uq_portfolio_id_kind", "portfolio", ["id", "kind"])

    op.add_column(
        "portfolio_holding",
        sa.Column("portfolio_kind", sa.String(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE portfolio_holding ph SET portfolio_kind = p.kind "
            "FROM portfolio p WHERE p.id = ph.portfolio_id"
        )
    )
    op.alter_column("portfolio_holding", "portfolio_kind", nullable=False)
    op.create_foreign_key(
        "fk_portfolio_holding_portfolio_kind",
        "portfolio_holding",
        "portfolio",
        ["portfolio_id", "portfolio_kind"],
        ["id", "kind"],
        onupdate="CASCADE",
    )

    _refuse_pre_existing_violations()

    # Acceptance criterion 2, as a database fact.
    op.create_index(
        _ONE_CAPITAL_INDEX,
        "portfolio_holding",
        ["instrument_id", "broker_account_id"],
        unique=True,
        postgresql_where=sa.text("portfolio_kind = 'CAPITAL'"),
    )


def _refuse_pre_existing_violations() -> None:
    """Stop with a readable message if the data already breaks criterion 2.

    Runs after ``portfolio_kind`` is populated and before the index is built, which is the only
    window in which the question can be asked in terms of the new column.
    """
    duplicates = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT i.symbol, ph.broker_account_id, count(*) AS n, "
                "       string_agg(p.name, ' / ' ORDER BY p.name) AS portfolios "
                "FROM portfolio_holding ph "
                "JOIN portfolio p ON p.id = ph.portfolio_id "
                "JOIN instrument i ON i.id = ph.instrument_id "
                "WHERE ph.portfolio_kind = 'CAPITAL' "
                "GROUP BY i.symbol, ph.broker_account_id "
                "HAVING count(*) > 1 "
                "ORDER BY n DESC, i.symbol"
            )
        )
        .fetchall()
    )
    if not duplicates:
        return

    lines = [
        f"  {row.symbol} at broker account {row.broker_account_id}: "
        f"in {row.n} capital portfolios ({row.portfolios})"
        for row in duplicates
    ]
    raise RuntimeError(
        "this database already breaks acceptance criterion 2 — a holding is allocated to more "
        f"than one capital portfolio, in {len(duplicates)} case(s):\n"
        + "\n".join(lines)
        + "\n\nThe migration will not choose a winner: which portfolio keeps a holding decides "
        "whose return series it belongs to, and only the account's owner can answer that. "
        "Resolve each case by removing the holding from all but one capital portfolio (or by "
        "making the others MONITORING views, which may overlap freely), then re-run."
    )


def downgrade() -> None:
    op.drop_index(_ONE_CAPITAL_INDEX, table_name="portfolio_holding")
    op.drop_constraint(
        "fk_portfolio_holding_portfolio_kind", "portfolio_holding", type_="foreignkey"
    )
    op.drop_column("portfolio_holding", "portfolio_kind")

    op.drop_constraint("uq_portfolio_id_kind", "portfolio", type_="unique")
    op.drop_constraint("portfolio_source_known", "portfolio", type_="check")
    op.drop_constraint("portfolio_kind_known", "portfolio", type_="check")
    op.drop_column("portfolio", "source")
    op.drop_column("portfolio", "kind")
