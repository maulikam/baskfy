"""Let ``SPLIT_HOLDING`` be written. The enum grew on 10 Sep 2026; the CHECK never did.

THE DEFECT, WHICH HAD NEVER FIRED
---------------------------------
0022 fixed ``reconciliation_item.reason`` to three values:

    ("UNALLOCATED_HOLDING", "QUANTITY_MISMATCH", "UNKNOWN_INFLOW")

On 10 Sep 2026, when slices arrived, ``baskfy_core.allocation_ledger.ReconciliationReason`` gained
a fourth — ``SPLIT_HOLDING``, *"you hold this name in more than one portfolio — which of them did
you sell from?"* — and ``attribute_sell`` began returning it as the answer for **every** sell out
of a split position. Nothing widened the constraint, so that answer could not be stored: writing
it raises ``ck_reconciliation_item_reconciliation_item_reason_known`` and takes the enclosing
transaction with it.

Two days passed without anyone noticing, and the reason is the finding of this leaf rather than a
coincidence: **nothing in production writes a reconciliation item at all.**
``baskfy_worker.tasks.holdings_sync.run_holdings_sync`` is the only writer and it has no caller
outside its own tests, so ``reconciliation_item`` had 0 rows on a box that had been live for
eleven days. A constraint nobody reaches cannot be seen to be wrong. The PKTEA sweep in
``baskfy_api.broker_holdings_sync`` is the first code on the live path that asks the question, and
it found this on its first split position.

WHY THE VALUES ARE RE-STATED RATHER THAN APPENDED
-------------------------------------------------
The constraint is dropped and rebuilt from a single tuple that is the whole permitted set, so the
list in this file is the list in the database — there is no "plus whatever was there before" to
reason about during a review. The tuple is deliberately spelled out rather than imported from
``ReconciliationReason``: a migration must keep saying what it did on the day it ran, and one that
reads today's enum would silently change meaning the next time somebody adds a member.

The downgrade restores 0022's three exactly. It is **not** a no-op and it is not safe by itself:
any row already carrying ``SPLIT_HOLDING`` would fail revalidation. That is honest — a downgrade
past the point where a value became writable has to confront the rows that used it — and it is
why the downgrade deletes nothing on its own. Resolve or dismiss those items first.

Revision ID: 0043_split_holding_reason
Revises: 0042_twt_scan_run
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043_split_holding_reason"
down_revision: str | None = "0042_twt_scan_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The name alembic's naming convention actually produced for 0022's CHECK: the convention adds
#: ``ck_%(table_name)s_`` and 0022 had already put the table name in the constraint name. 0036
#: fixed the same doubling on ``portfolio_holding`` by renaming; here the constraint is being
#: replaced anyway, so the existing (doubled) name is simply what must be dropped.
_CONSTRAINT = "ck_reconciliation_item_reconciliation_item_reason_known"

#: What to *pass* to `create_check_constraint`. The convention prefixes `ck_reconciliation_item_`
#: to it, reproducing `_CONSTRAINT` exactly. Passing the full name instead is the mistake 0035
#: made and 0036 had to rename its way out of; the two names are kept apart here so it cannot
#: happen again by editing one of them.
_CONSTRAINT_ARG = "reconciliation_item_reason_known"

#: 0022's three, plus the fourth. Stated, not imported — see the docstring.
_REASONS = (
    "UNALLOCATED_HOLDING",
    "QUANTITY_MISMATCH",
    "UNKNOWN_INFLOW",
    "SPLIT_HOLDING",
)

_WAS = _REASONS[:3]


def _rebuild(values: tuple[str, ...]) -> None:
    # `IF EXISTS` rather than a lookup: unlike 0036 this migration is not deciding *whether* to
    # act, only tolerating a database where 0022's constraint carries the undoubled name.
    op.execute(f'ALTER TABLE reconciliation_item DROP CONSTRAINT IF EXISTS "{_CONSTRAINT}"')
    op.execute(f'ALTER TABLE reconciliation_item DROP CONSTRAINT IF EXISTS "{_CONSTRAINT_ARG}"')
    op.create_check_constraint(
        _CONSTRAINT_ARG,
        "reconciliation_item",
        sa.column("reason").in_(values),
    )


def upgrade() -> None:
    _rebuild(_REASONS)


def downgrade() -> None:
    _rebuild(_WAS)
