"""A writer that omits ``portfolio_kind`` gets an attributed row, not an integrity error.

0021 added ``portfolio_holding.portfolio_kind`` NOT NULL to make acceptance criterion 2 a database
fact. It did not give existing writers a way to populate it, and there are several — the create
route, the CSV import, the rebalance apply. Every one of them broke, which is a regression the
migration should have carried its own fix for. This is that fix.

WHY A TRIGGER HERE, HAVING ARGUED AGAINST ONE IN 0021
-----------------------------------------------------
The two uses are not the same use, and the distinction is worth stating because it looks like a
reversal.

0021 rejected a trigger for **enforcing** the rule: a trigger is skipped by
``SET session_replication_role = replica`` and by some bulk-load paths, so a constraint that lives
in one is a constraint a bulk import can walk past. That argument stands, was verified, and the
partial unique index it produced is untouched here.

This trigger **defaults** a value; it does not enforce anything. If it is ever bypassed the row
still meets ``NOT NULL`` and still meets the composite foreign key to ``portfolio (id, kind)`` —
so a bypass produces a loud failure, never a wrong row. Defaulting is exactly what a trigger is
safe for, and ``portfolio_holding`` already carries a precedent: 0019's
``portfolio_holding_attribute_broker_account`` fills ``broker_account_id`` the same way, for the
same reason, on the same table.

WHY NOT A COLUMN DEFAULT
------------------------
``DEFAULT 'CAPITAL'`` would be wrong for a monitoring view and the foreign key would reject the
insert — correctly, but with an error naming the key rather than the mistake. The value is not a
constant; it is a fact about the owning portfolio, and only a trigger can read one.

Revision ID: 0024_portfolio_kind_default
Revises: 0023_nav_consolidated_key
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0024_portfolio_kind_default"
down_revision: str | None = "0023_nav_consolidated_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FUNCTION = """
CREATE OR REPLACE FUNCTION portfolio_holding_attribute_kind()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
    -- A writer that named the kind is left alone: it may be deliberately inserting into a
    -- monitoring view, and second-guessing it here would silently move the row.
    IF NEW.portfolio_kind IS NOT NULL THEN
        RETURN NEW;
    END IF;

    SELECT p.kind
      INTO NEW.portfolio_kind
      FROM public.portfolio p
     WHERE p.id = NEW.portfolio_id;

    -- A NULL here means the portfolio does not exist. Returning the row unchanged lets the
    -- foreign key say so, which is a better error than anything this function could raise.
    RETURN NEW;
END;
$$
"""

_TRIGGER = """
CREATE TRIGGER portfolio_holding_attribute_kind
BEFORE INSERT ON portfolio_holding
FOR EACH ROW
EXECUTE FUNCTION portfolio_holding_attribute_kind()
"""


def upgrade() -> None:
    op.execute(_FUNCTION)
    op.execute(_TRIGGER)
    # Any row written between 0021 and this migration cannot exist — the column is NOT NULL, so
    # such a write failed rather than landing wrong. Nothing to backfill, and saying so here
    # saves the next reader from looking for the backfill that is missing.


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS portfolio_holding_attribute_kind ON portfolio_holding")
    op.execute("DROP FUNCTION IF EXISTS portfolio_holding_attribute_kind()")
