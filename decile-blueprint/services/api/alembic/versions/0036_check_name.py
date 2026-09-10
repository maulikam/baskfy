"""Give 0035's CHECK the name the ORM model expects.

0035 passed `ck_portfolio_holding_quantity_not_negative` to `op.create_check_constraint`, and
alembic's naming convention (`ck_%(table_name)s_%(constraint_name)s`) prefixed it again — so the
box ended up with `ck_portfolio_holding_ck_portfolio_holding_quantity_not_negative` while
`PortfolioHolding.__table_args__` names `ck_portfolio_holding_portfolio_holding_quantity_not_
negative`. Two consequences, both real:

* 0035's own `downgrade()` drops a constraint that does not exist under that name, so the
  rollback fails halfway;
* any future autogenerate sees a constraint the model does not declare and a declared constraint
  the database does not have, and proposes to drop and re-create it.

This renames rather than dropping and re-creating, so the check is never off — not even for the
instant between two statements — and so the table is never re-validated on a database where it
might have grown.

Guarded with a lookup rather than `IF EXISTS`, because a database that ran the *corrected* 0035
already has the right name and there is nothing here to do. That makes this migration a no-op on
a fresh install and a rename on the one box that ran the first version, which is the whole job.

Revision ID: 0036_check_name
Revises: 0035_split_allocation
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_check_name"
down_revision: str | None = "0035_split_allocation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WRONG = "ck_portfolio_holding_ck_portfolio_holding_quantity_not_negative"
_RIGHT = "ck_portfolio_holding_portfolio_holding_quantity_not_negative"


def _exists(name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM pg_constraint WHERE conname = :name "
                "AND conrelid = 'portfolio_holding'::regclass"
            ),
            {"name": name},
        )
        .scalar()
    )


def upgrade() -> None:
    if _exists(_WRONG) and not _exists(_RIGHT):
        op.execute(f'ALTER TABLE portfolio_holding RENAME CONSTRAINT "{_WRONG}" TO "{_RIGHT}"')


def downgrade() -> None:
    """Deliberately nothing. Renaming back would re-introduce the defect, and break 0035.

    Proved by running it: with the rename reversed, 0035's own `downgrade()` then looked for the
    corrected name, did not find it, and the chain failed one step later — a rollback that gets
    stuck between two revisions is worse than one that leaves a constraint correctly named.

    A downgrade is for undoing a change in *behaviour*. This migration changes an identifier and
    nothing else: the constraint checks the same rows either way, and 0035 (as corrected) drops
    it by the name this leaves behind. So the honest inverse of "fix a name" is "leave it fixed".
    """
