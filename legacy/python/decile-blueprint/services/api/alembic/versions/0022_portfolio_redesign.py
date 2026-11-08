"""The rest of the portfolio-redesign schema: cash, NAV, reconciliation, benchmarks.

``PORTFOLIO_REDESIGN.md`` §4.4, §4.6, §5.1 and §4.3. 0021 gave a portfolio its kind and made
criterion 2 a database fact; this carries the four things the spine still needs, in one migration
because alembic history is linear and the redesign's leaves are built in parallel — a migration
per leaf would serialise the work it is meant to unblock.

§4.6 asks for three data layers kept separate in the schema, and that separation is the shape of
what follows:

1. **Broker ledger** — ``broker_cash`` is the per-account cash the broker actually holds.
2. **Market data** — untouched here; ``ohlcv_daily`` already is that layer.
3. **Baskfy portfolio ledger** — ``portfolio_cash_flow`` (internal assignments),
   ``portfolio_nav_daily`` (the official EOD series) and ``reconciliation_item`` (the questions
   sync could not answer).

WHY CASH IS TWO TABLES AND NOT A COLUMN
---------------------------------------
§4.4 draws a line that a single balance column erases. Money arriving from outside (a deposit)
lands in the broker's **Unallocated cash** and is not a portfolio event. Money *assigned* to a
portfolio is an internal flow and **is** the XIRR cash-flow event. Buying a stock inside a
portfolio is a cash→stock transfer and is **not** an XIRR event at all. Only a ledger of flows
can tell those three apart afterwards, and per-portfolio XIRR is computed from nothing else — so
the flows are rows, and the balance is derived.

WHY THE NAV SERIES IS STORED AND NOT COMPUTED ON READ
-----------------------------------------------------
§5.1 makes the EOD NAV the single source for the combined chart, per-day P&L, contribution and
drawdown. Recomputing a year of it per page load would be the cheap version; worse, it would
silently change history whenever a holding's allocation changed. A stored series is what "valued
at close of {date}" means — the number is a fact about that day, not a re-derivation.

``pending_reconciliation`` on a NAV row is §4.3's freeze made visible: a day whose value could
not be trusted says so rather than being omitted, because a gap in a chart reads as zero.

Revision ID: 0022_portfolio_redesign
Revises: 0021_allocation_ledger
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_portfolio_redesign"
down_revision: str | None = "0021_allocation_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: §4.4. ``EXTERNAL_*`` cross the boundary of the whole account and are not portfolio events;
#: ``ASSIGN``/``RELEASE`` move cash between Unallocated and a portfolio and are the XIRR events;
#: ``BUY``/``SELL`` are cash<->stock inside one portfolio and are deliberately **not** XIRR
#: events, which is the distinction §4.4 exists to preserve.
_FLOW_KINDS = (
    "EXTERNAL_DEPOSIT",
    "EXTERNAL_WITHDRAWAL",
    "ASSIGN",
    "RELEASE",
    "BUY",
    "SELL",
    "DIVIDEND",
)

#: §4.3. Mirrors ``baskfy_core.allocation_ledger.ReconciliationReason``.
_RECONCILE_REASONS = ("UNALLOCATED_HOLDING", "QUANTITY_MISMATCH", "UNKNOWN_INFLOW")

_RECONCILE_STATES = ("OPEN", "RESOLVED", "DISMISSED")

MONEY = sa.Numeric(20, 2)  # matches baskfy_core.models.base.MONEY
QUANTITY = sa.Numeric(20, 4)


def upgrade() -> None:
    # --- portfolio metadata the redesign's surfaces need ---------------------
    # §5.2 measures every metric from a start date, and §6.3 overlays a benchmark that §6.5 lets
    # a portfolio override. Both were implicit before; a metric with an implicit start date is a
    # number nobody can check.
    op.add_column("portfolio", sa.Column("started_on", sa.Date(), nullable=True))
    op.add_column("portfolio", sa.Column("benchmark_index_id", sa.SmallInteger(), nullable=True))
    op.execute(
        sa.text("UPDATE portfolio SET started_on = created_at::date WHERE started_on IS NULL")
    )
    op.alter_column("portfolio", "started_on", nullable=False)
    op.create_foreign_key(
        "fk_portfolio_benchmark_index_id_index_def",
        "portfolio",
        "index_def",
        ["benchmark_index_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # --- layer 1: the broker's own cash --------------------------------------
    op.create_table(
        "broker_cash",
        sa.Column("broker_account_id", sa.BigInteger(), nullable=False),
        # The Unallocated cash bucket of §4.4 — one per broker account, derived from flows but
        # materialised so a page does not sum a lifetime of rows to show one number.
        sa.Column("balance", MONEY, nullable=False, server_default="0"),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(["broker_account_id"], ["broker_account.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("broker_account_id"),
    )

    # --- layer 3: the Baskfy portfolio ledger --------------------------------
    op.create_table(
        "portfolio_cash_flow",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("broker_account_id", sa.BigInteger(), nullable=False),
        # NULL means the flow belongs to Unallocated rather than to a portfolio — an external
        # deposit has no portfolio, and saying so with NULL keeps §4.4's boundary in the data.
        sa.Column("portfolio_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("occurred_on", sa.Date(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=True),
        sa.Column("quantity", QUANTITY, nullable=True),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            sa.column("kind").in_(_FLOW_KINDS), name="portfolio_cash_flow_kind_known"
        ),
        # An external flow has no portfolio; an internal one must name it. Written as a
        # constraint because the whole XIRR story depends on telling them apart.
        sa.CheckConstraint(
            "(kind IN ('EXTERNAL_DEPOSIT','EXTERNAL_WITHDRAWAL') AND portfolio_id IS NULL) "
            "OR (kind NOT IN ('EXTERNAL_DEPOSIT','EXTERNAL_WITHDRAWAL') AND portfolio_id IS NOT NULL)",
            name="portfolio_cash_flow_external_has_no_portfolio",
        ),
        sa.ForeignKeyConstraint(["broker_account_id"], ["broker_account.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolio.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instrument.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_portfolio_cash_flow_portfolio_occurred",
        "portfolio_cash_flow",
        ["portfolio_id", "occurred_on"],
    )
    op.create_index(
        "ix_portfolio_cash_flow_broker_occurred",
        "portfolio_cash_flow",
        ["broker_account_id", "occurred_on"],
    )

    op.create_table(
        "portfolio_nav_daily",
        # NULL portfolio_id is the consolidated series — the same row shape, so the combined
        # chart and a single portfolio's chart read one table rather than two.
        sa.Column("portfolio_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("market_value", MONEY, nullable=False),
        sa.Column("cash", MONEY, nullable=False, server_default="0"),
        sa.Column("net_flow", MONEY, nullable=False, server_default="0"),
        # §4.3's freeze, visible: a day we could not value honestly says so rather than vanishing.
        sa.Column(
            "pending_reconciliation", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.ForeignKeyConstraint(["portfolio_id"], ["portfolio.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "date", "portfolio_id"),
    )
    op.create_index(
        "ix_portfolio_nav_daily_portfolio_date", "portfolio_nav_daily", ["portfolio_id", "date"]
    )

    op.create_table(
        "reconciliation_item",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("broker_account_id", sa.BigInteger(), nullable=False),
        sa.Column("quantity", QUANTITY, nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False, server_default="OPEN"),
        sa.Column("suggested_portfolio_id", sa.BigInteger(), nullable=True),
        sa.Column("resolved_portfolio_id", sa.BigInteger(), nullable=True),
        sa.Column("detected_on", sa.Date(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            sa.column("reason").in_(_RECONCILE_REASONS), name="reconciliation_item_reason_known"
        ),
        sa.CheckConstraint(
            sa.column("state").in_(_RECONCILE_STATES), name="reconciliation_item_state_known"
        ),
        # A resolved item names the portfolio it was resolved to; an open one cannot. Otherwise
        # "resolved" could mean "someone clicked something" and the return series would move on
        # a decision nobody recorded.
        sa.CheckConstraint(
            "(state = 'RESOLVED' AND resolved_portfolio_id IS NOT NULL) OR "
            "(state <> 'RESOLVED' AND resolved_portfolio_id IS NULL)",
            name="reconciliation_item_resolved_names_portfolio",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instrument.id"]),
        sa.ForeignKeyConstraint(["broker_account_id"], ["broker_account.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["suggested_portfolio_id"], ["portfolio.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["resolved_portfolio_id"], ["portfolio.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_reconciliation_item_user_state", "reconciliation_item", ["user_id", "state"]
    )

    # --- §5.3: transaction history, so a holding group can leave "since grouped" -------------
    op.add_column("portfolio_holding", sa.Column("first_bought_on", sa.Date(), nullable=True))
    op.add_column(
        "portfolio_holding",
        sa.Column("history_source", sa.String(), nullable=False, server_default="NONE"),
    )
    op.create_check_constraint(
        "portfolio_holding_history_source_known",
        "portfolio_holding",
        sa.column("history_source").in_(("NONE", "CAS", "BROKER", "MANUAL")),
    )


def downgrade() -> None:
    op.drop_constraint("portfolio_holding_history_source_known", "portfolio_holding", type_="check")
    op.drop_column("portfolio_holding", "history_source")
    op.drop_column("portfolio_holding", "first_bought_on")

    op.drop_index("ix_reconciliation_item_user_state", table_name="reconciliation_item")
    op.drop_table("reconciliation_item")

    op.drop_index("ix_portfolio_nav_daily_portfolio_date", table_name="portfolio_nav_daily")
    op.drop_table("portfolio_nav_daily")

    op.drop_index("ix_portfolio_cash_flow_broker_occurred", table_name="portfolio_cash_flow")
    op.drop_index("ix_portfolio_cash_flow_portfolio_occurred", table_name="portfolio_cash_flow")
    op.drop_table("portfolio_cash_flow")

    op.drop_table("broker_cash")

    op.drop_constraint("fk_portfolio_benchmark_index_id_index_def", "portfolio", type_="foreignkey")
    op.drop_column("portfolio", "benchmark_index_id")
    op.drop_column("portfolio", "started_on")
