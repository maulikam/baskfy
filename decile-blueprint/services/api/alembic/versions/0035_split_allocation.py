"""A holding may be filed into several capital portfolios, a quantity at a time.

Maulik, 10 Sep 2026: "one stock can appear in multiple portfolios, so if stock a bought 100 qty
for shortterm 20 for long term 34 for some swing 36 for momentum".

WHAT THIS DROPS, AND WHY THAT IS SAFE
-------------------------------------
`uq_portfolio_holding_one_capital_portfolio` — 0021's partial unique index on
`(instrument_id, broker_account_id) WHERE portfolio_kind = 'CAPITAL'`. It is the database half of
the rule `allocation_ledger` used to open with: "a holding belongs to exactly one capital
portfolio". PF1 replaced that rule with the arithmetic it was really protecting — the slices of a
holding never add up to more than the holding — so the index is now forbidding the feature rather
than protecting the total.

Dropping a unique index cannot fail on existing data and cannot invalidate a row: every state that
was legal before is still legal. That direction matters for the downgrade, which is where the risk
actually is; see below.

WHAT REPLACES IT
----------------
Nothing, in the database, and that is deliberate. The invariant is `sum(slices) <= held`, which is
a fact about a GROUP of rows: no CHECK can see it, and the trigger that could would have to
re-aggregate the position on every write to the busiest table in the schema.

It does not need one, because after this migration a `portfolio_holding` row IS a slice and a
position's quantity is the SUM of its capital rows. Allocation MOVES quantity between rows rather
than asserting a new total — `_apply_allocation` decrements the source and inserts the target in
one transaction — so shares are conserved by the operation. There is no write that can inflate a
position, so there is no constraint needed to refuse one. `baskfy_core.allocation_ledger.
validate_against_holdings` is still called on every valuation path as the belt to that braces: it
catches a caller that assembled a bad picture in memory, which is the failure a constraint on this
table could never have caught anyway.

The row-local half of the invariant IS enforced here: `ck_portfolio_holding_quantity_not_negative`.
A negative slice would be a position that subtracts from its own portfolio, and unlike the sum it
is visible from a single row. NULL is still permitted — 0022's `history_source` records that a
holding's numbers may be unknown, and "we do not know how many" is not "minus one".

THE DOWNGRADE IS THE DANGEROUS DIRECTION, AND IT REFUSES RATHER THAN DESTROYS
-----------------------------------------------------------------------------
Re-creating a unique index over data that has since become legitimately non-unique fails halfway,
with the index half-built and no statement saying which rows were at fault. So the downgrade
checks first and raises a sentence naming the instrument, the broker account and the portfolios
that share it. The operator's choices are then to merge those slices by hand or to stay on this
revision — never to have the migration silently pick a slice to keep, which would delete a record
of real money that nothing else holds.

Revision ID: 0035_split_allocation
Revises: 0034_swing_intraday_plan
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_split_allocation"
down_revision: str | None = "0034_swing_intraday_plan"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 0021 named it; PF2 drops it. Spelled once so the upgrade and the downgrade cannot disagree.
_ONE_CAPITAL_INDEX = "uq_portfolio_holding_one_capital_portfolio"

#: The name WITHOUT the `ck_` prefix, because `alembic`'s naming convention adds it — and with
#: the table name repeated, because that is this schema's house style for every other check
#: (`ck_portfolio_portfolio_parent_not_self`, `ck_portfolio_portfolio_kind_known`), and because
#: the ORM model spells it the same way. Getting this wrong on the first deploy produced
#: `ck_portfolio_holding_ck_portfolio_holding_quantity_not_negative` on the box; 0036 renames it.
_QUANTITY_CHECK = "portfolio_holding_quantity_not_negative"


def upgrade() -> None:
    op.drop_index(_ONE_CAPITAL_INDEX, table_name="portfolio_holding")
    op.create_check_constraint(
        _QUANTITY_CHECK,
        "portfolio_holding",
        "quantity IS NULL OR quantity >= 0",
    )


def downgrade() -> None:
    # The bare name, like `create_check_constraint` above: alembic applies the `ck_%(table_name)s_`
    # convention to BOTH, and passing the full name here produced
    # `ck_portfolio_holding_ck_portfolio_holding_portfolio_hol_f256` — prefixed twice and then
    # truncated to fit. Proved by running the downgrade, not by reading the docs.
    op.drop_constraint(_QUANTITY_CHECK, "portfolio_holding", type_="check")

    # Refuse loudly rather than fail obscurely, or worse, choose a slice to keep. See the module
    # docstring: the rows this would collide on are a record of real money.
    split = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT instrument_id, broker_account_id, count(*) AS slices "
                "FROM portfolio_holding WHERE portfolio_kind = 'CAPITAL' "
                "GROUP BY instrument_id, broker_account_id HAVING count(*) > 1 "
                "ORDER BY instrument_id, broker_account_id"
            )
        )
        .fetchall()
    )
    if split:
        listed = ", ".join(
            f"instrument {row.instrument_id} at broker account {row.broker_account_id} "
            f"({row.slices} slices)"
            for row in split
        )
        raise RuntimeError(
            "Cannot downgrade past 0035: these holdings are split across capital portfolios and "
            f"the unique index this restores would refuse them — {listed}. Merge each holding "
            "into one capital portfolio first (the Portfolio page can do it), then re-run. This "
            "migration will not pick a slice to keep, because the ones it discarded would be a "
            "record of real money that nothing else holds."
        )

    op.create_index(
        _ONE_CAPITAL_INDEX,
        "portfolio_holding",
        ["instrument_id", "broker_account_id"],
        unique=True,
        postgresql_where=sa.text("portfolio_kind = 'CAPITAL'"),
    )
