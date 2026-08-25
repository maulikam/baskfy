"""Portfolio graph — nesting, broker attribution, basket sleeves, investment↔portfolio.

Four changes, one migration, because they are one shape and half of it is not usable:

1. ``portfolio`` becomes a forest. ``parent_id`` is a nullable self-reference; ``broker_account_id``
   is nullable and its two states are different facts — NULL is a roll-up spanning brokers,
   NOT NULL is "everything here is attributable to this one account".
2. ``portfolio_holding``'s primary key gains ``broker_account_id``. The old two-column key
   asserted that a portfolio holds a name in exactly one place, which is false as soon as the
   same instrument is held at two brokers.
3. ``portfolio_sleeve.kind`` admits ``basket``, paired structurally with a new ``basket_id`` in
   the same way ``screen`` is paired with ``screen_id``.
4. ``cb_investment`` gains a **nullable** ``portfolio_id`` — an investment may sit outside any
   portfolio, and every row that predates this migration does.

**The risky one is (2).** ``broker_account_id`` is in the key, so it cannot be nullable, so every
pre-existing row needs a value. The rule used is the one migration 0018 already established for
``cb_investment``: the row's owner's default broker account, creating that account if the user
has none — exactly what ``baskfy_api.broker_accounts.ensure_default_broker_account`` does at
runtime and what 0018 did for every ``app_user`` that existed then. The account row is a name for
"this user's book at this broker"; whether the broker is actually *connected* is answered by
``broker_account.kite_user_id`` and the connection state, never by the row's existence, so
attributing legacy holdings to it claims nothing that is not already claimed.

The same rule is installed as a ``BEFORE INSERT`` trigger. Without it a writer that omits the
column — ``baskfy_api.portfolios.replace_holdings`` is the live example — would start failing on
a NOT NULL violation the moment this migration lands. The trigger fires only when the column is
NULL, so a caller that names an account is never second-guessed, and no holding can be written
unattributed by any writer, present or future.

``downgrade()`` genuinely reverses it. Rows written after the upgrade may hold the same
instrument at several brokers, and the old key cannot express that, so they are merged before the
column goes: quantities summed, ``avg_price`` re-derived as the quantity-weighted average,
``added_on`` taken as the earliest. A row that existed before the upgrade is untouched by the
merge (it is alone in its group) and returns byte-identical. What is lost on the way down is
exactly the attribution the old schema had no column for.

Revision ID: 0019_portfolio_graph
Revises: 0018_trading_path_tenancy
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0019_portfolio_graph"
down_revision: str | None = "0018_trading_path_tenancy"
branch_labels: str | None = None
depends_on: str | None = None

#: Matches ``baskfy_core.tenancy.DEFAULT_BROKER_ID``. Spelled out rather than imported so the
#: migration keeps meaning what it meant on the day it ran, whatever the constant becomes later.
DEFAULT_BROKER_ID = "zerodha"

#: The live half of the backfill rule. Resolution order, most specific first: the portfolio's own
#: attribution, then the owner's default broker account, then any account the owner has, then a
#: default account created for them. ``SECURITY INVOKER`` (the default) and a pinned ``search_path``
#: so the function cannot be redirected by a caller's schema settings.
_ATTRIBUTE_FN = f"""
CREATE OR REPLACE FUNCTION portfolio_holding_attribute_broker_account()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_user_id  bigint;
    v_account  bigint;
BEGIN
    IF NEW.broker_account_id IS NOT NULL THEN
        RETURN NEW;
    END IF;

    SELECT p.user_id, p.broker_account_id
      INTO v_user_id, v_account
      FROM public.portfolio p
     WHERE p.id = NEW.portfolio_id;

    IF v_account IS NULL THEN
        SELECT a.id
          INTO v_account
          FROM public.broker_account a
         WHERE a.user_id = v_user_id
         ORDER BY (a.broker_id <> '{DEFAULT_BROKER_ID}'), a.id
         LIMIT 1;
    END IF;

    IF v_account IS NULL THEN
        INSERT INTO public.broker_account (user_id, broker_id, label)
        VALUES (v_user_id, '{DEFAULT_BROKER_ID}', 'primary')
        RETURNING id INTO v_account;
    END IF;

    NEW.broker_account_id := v_account;
    RETURN NEW;
END;
$$
"""

_ATTRIBUTE_TRIGGER = """
CREATE TRIGGER portfolio_holding_attribute_broker_account
BEFORE INSERT ON portfolio_holding
FOR EACH ROW
EXECUTE FUNCTION portfolio_holding_attribute_broker_account()
"""

#: Pre-existing holdings whose owner has no ``broker_account`` at all get one, on the 0018 rule.
#: ``SELECT DISTINCT`` because one user may own many portfolios; ``NOT EXISTS`` because 0018 may
#: already have made the row, and ``uq_broker_account_user_broker`` would refuse a second.
_BACKFILL_ACCOUNTS = f"""
INSERT INTO broker_account (user_id, broker_id, label)
SELECT DISTINCT p.user_id, '{DEFAULT_BROKER_ID}', 'primary'
  FROM portfolio p
  JOIN portfolio_holding h ON h.portfolio_id = p.id
 WHERE NOT EXISTS (
       SELECT 1 FROM broker_account a
        WHERE a.user_id = p.user_id AND a.broker_id = '{DEFAULT_BROKER_ID}'
 )
"""

_BACKFILL_HOLDINGS = f"""
UPDATE portfolio_holding h
   SET broker_account_id = COALESCE(
           p.broker_account_id,
           (SELECT a.id
              FROM broker_account a
             WHERE a.user_id = p.user_id
             ORDER BY (a.broker_id <> '{DEFAULT_BROKER_ID}'), a.id
             LIMIT 1)
       )
  FROM portfolio p
 WHERE h.portfolio_id = p.id
   AND h.broker_account_id IS NULL
"""

#: Downgrade step 1 of 2. The surviving row of each duplicate group takes the group's totals.
#: ``avg_price`` is a quantity-weighted mean over the rows that have both numbers, falling back to
#: the smallest stated price when no row has a quantity to weight by; ``ROUND(..., 4)`` because
#: ``portfolio_holding.avg_price`` is ``numeric(18, 4)`` and rounding happens at write time
#: (house rule 8), not wherever the number is later displayed.
_MERGE_DUPLICATES = """
WITH merged AS (
    SELECT portfolio_id,
           instrument_id,
           MIN(broker_account_id) AS keep_account,
           SUM(quantity)          AS quantity,
           ROUND(
               COALESCE(
                   SUM(quantity * avg_price)
                       FILTER (WHERE quantity IS NOT NULL AND avg_price IS NOT NULL)
                   / NULLIF(
                       SUM(quantity)
                           FILTER (WHERE quantity IS NOT NULL AND avg_price IS NOT NULL),
                       0
                   ),
                   MIN(avg_price)
               ),
               4
           )                      AS avg_price,
           MIN(added_on)          AS added_on
      FROM portfolio_holding
     GROUP BY portfolio_id, instrument_id
    HAVING COUNT(*) > 1
)
UPDATE portfolio_holding h
   SET quantity  = m.quantity,
       avg_price = m.avg_price,
       added_on  = m.added_on
  FROM merged m
 WHERE h.portfolio_id      = m.portfolio_id
   AND h.instrument_id     = m.instrument_id
   AND h.broker_account_id = m.keep_account
"""

#: Downgrade step 2 of 2: the rows whose numbers were just folded into the survivor.
_DROP_MERGED_DUPLICATES = """
DELETE FROM portfolio_holding h
 USING (
     SELECT portfolio_id, instrument_id, MIN(broker_account_id) AS keep_account
       FROM portfolio_holding
      GROUP BY portfolio_id, instrument_id
 ) k
 WHERE h.portfolio_id      = k.portfolio_id
   AND h.instrument_id     = k.instrument_id
   AND h.broker_account_id <> k.keep_account
"""


def upgrade() -> None:
    _upgrade_portfolio_graph()
    _upgrade_holding_attribution()
    _upgrade_basket_sleeves()
    _upgrade_investment_link()


def downgrade() -> None:
    _downgrade_investment_link()
    _downgrade_basket_sleeves()
    _downgrade_holding_attribution()
    _downgrade_portfolio_graph()


# --- (1) portfolio becomes a forest, and may name a broker account -----------------------


def _upgrade_portfolio_graph() -> None:
    op.add_column("portfolio", sa.Column("parent_id", sa.BigInteger(), nullable=True))
    op.add_column("portfolio", sa.Column("broker_account_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_portfolio_parent_id_portfolio",
        "portfolio",
        "portfolio",
        ["parent_id"],
        ["id"],
        # Promote the children to roots. Deleting a grouping node must not cascade away the
        # holdings underneath it -- the same reasoning 0013 records for `portfolio_sleeve`.
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_portfolio_broker_account_id_broker_account",
        "portfolio",
        "broker_account",
        ["broker_account_id"],
        ["id"],
        # Unlinking a broker turns an attributed portfolio back into a roll-up, which is the
        # honest reading: nothing about the holdings changed, only what we can say about them.
        ondelete="SET NULL",
    )
    # Cycles and the depth cap are multi-row facts, checked in `baskfy_core.portfolio_graph`.
    # Self-parenting is the one a single write can introduce, so the database owns it.
    op.create_check_constraint(
        "portfolio_parent_not_self", "portfolio", "parent_id IS NULL OR parent_id <> id"
    )
    op.create_index("ix_portfolio_parent_id", "portfolio", ["parent_id"])
    op.create_index("ix_portfolio_broker_account_id", "portfolio", ["broker_account_id"])


def _downgrade_portfolio_graph() -> None:
    op.drop_index("ix_portfolio_broker_account_id", table_name="portfolio")
    op.drop_index("ix_portfolio_parent_id", table_name="portfolio")
    op.drop_constraint("portfolio_parent_not_self", "portfolio", type_="check")
    op.drop_constraint(
        "fk_portfolio_broker_account_id_broker_account", "portfolio", type_="foreignkey"
    )
    op.drop_constraint("fk_portfolio_parent_id_portfolio", "portfolio", type_="foreignkey")
    op.drop_column("portfolio", "broker_account_id")
    op.drop_column("portfolio", "parent_id")


# --- (2) a holding is held at a broker account, and that is part of its identity ---------


def _upgrade_holding_attribution() -> None:
    op.add_column(
        "portfolio_holding", sa.Column("broker_account_id", sa.BigInteger(), nullable=True)
    )
    op.execute(_BACKFILL_ACCOUNTS)
    op.execute(_BACKFILL_HOLDINGS)
    # No `WHERE broker_account_id IS NOT NULL` rescue clause: if a row survived the backfill
    # unattributed, the assumption behind this migration is wrong and it must fail here, loudly,
    # rather than leave a half-attributed table behind.
    op.alter_column("portfolio_holding", "broker_account_id", nullable=False)

    op.drop_constraint("pk_portfolio_holding", "portfolio_holding", type_="primary")
    op.create_primary_key(
        "pk_portfolio_holding",
        "portfolio_holding",
        ["portfolio_id", "instrument_id", "broker_account_id"],
    )
    op.create_foreign_key(
        "fk_portfolio_holding_broker_account_id_broker_account",
        "portfolio_holding",
        "broker_account",
        ["broker_account_id"],
        ["id"],
        # Deliberately no ondelete: SET NULL is impossible inside a primary key and CASCADE would
        # delete positions to tidy up a login. The delete is refused instead.
    )
    op.create_index(
        "ix_portfolio_holding_broker_account_id", "portfolio_holding", ["broker_account_id"]
    )
    op.execute(_ATTRIBUTE_FN)
    op.execute(_ATTRIBUTE_TRIGGER)


def _downgrade_holding_attribution() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS portfolio_holding_attribute_broker_account ON portfolio_holding"
    )
    op.execute("DROP FUNCTION IF EXISTS portfolio_holding_attribute_broker_account()")
    op.execute(_MERGE_DUPLICATES)
    op.execute(_DROP_MERGED_DUPLICATES)
    op.drop_index("ix_portfolio_holding_broker_account_id", table_name="portfolio_holding")
    op.drop_constraint(
        "fk_portfolio_holding_broker_account_id_broker_account",
        "portfolio_holding",
        type_="foreignkey",
    )
    op.drop_constraint("pk_portfolio_holding", "portfolio_holding", type_="primary")
    op.create_primary_key(
        "pk_portfolio_holding", "portfolio_holding", ["portfolio_id", "instrument_id"]
    )
    op.drop_column("portfolio_holding", "broker_account_id")


# --- (3) a sleeve may take its names from a curated basket -------------------------------


# `op.drop_constraint` re-applies the metadata naming convention
# (`ck_%(table_name)s_%(constraint_name)s`), so check constraints are named here by their
# bare name -- passing the rendered `ck_...` name would produce `ck_..._ck_...`.
def _upgrade_basket_sleeves() -> None:
    op.add_column("portfolio_sleeve", sa.Column("basket_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_portfolio_sleeve_basket_id_cb_basket",
        "portfolio_sleeve",
        "cb_basket",
        ["basket_id"],
        ["id"],
        # No ondelete on purpose. `screen_id` uses SET NULL, which contradicts the pairing
        # constraint below -- blanking the id while `kind` stays 'screen' fails the CHECK, so the
        # delete errors rather than unsourcing the sleeve. Refusing the delete outright is the
        # same outcome without the misleading error; see PortfolioSleeve's docstring.
    )
    op.drop_constraint("portfolio_sleeve_kind", "portfolio_sleeve", type_="check")
    op.create_check_constraint(
        "portfolio_sleeve_kind", "portfolio_sleeve", "kind IN ('screen', 'manual', 'basket')"
    )
    op.drop_constraint("portfolio_sleeve_source", "portfolio_sleeve", type_="check")
    op.create_check_constraint(
        "portfolio_sleeve_source",
        "portfolio_sleeve",
        "(kind = 'screen' AND screen_id IS NOT NULL AND basket_id IS NULL) OR "
        "(kind = 'manual' AND screen_id IS NULL AND basket_id IS NULL) OR "
        "(kind = 'basket' AND basket_id IS NOT NULL AND screen_id IS NULL)",
    )
    op.create_index("ix_portfolio_sleeve_basket_id", "portfolio_sleeve", ["basket_id"])


def _downgrade_basket_sleeves() -> None:
    # A basket sleeve cannot be expressed by the pre-0019 constraint. It becomes a `manual`
    # sleeve: the capital stays, visibly unsourced, which is the outcome 0013 chose for a sleeve
    # whose screen went away. Deleting the row instead would destroy an allocation.
    op.execute("UPDATE portfolio_sleeve SET kind = 'manual' WHERE kind = 'basket'")
    op.drop_index("ix_portfolio_sleeve_basket_id", table_name="portfolio_sleeve")
    op.drop_constraint("portfolio_sleeve_source", "portfolio_sleeve", type_="check")
    op.create_check_constraint(
        "portfolio_sleeve_source",
        "portfolio_sleeve",
        "(kind = 'screen' AND screen_id IS NOT NULL) OR (kind = 'manual' AND screen_id IS NULL)",
    )
    op.drop_constraint("portfolio_sleeve_kind", "portfolio_sleeve", type_="check")
    op.create_check_constraint(
        "portfolio_sleeve_kind", "portfolio_sleeve", "kind IN ('screen', 'manual')"
    )
    op.drop_constraint(
        "fk_portfolio_sleeve_basket_id_cb_basket", "portfolio_sleeve", type_="foreignkey"
    )
    op.drop_column("portfolio_sleeve", "basket_id")


# --- (4) an investment may be filed under a portfolio ------------------------------------


def _upgrade_investment_link() -> None:
    op.add_column("cb_investment", sa.Column("portfolio_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_cb_investment_portfolio_id_portfolio",
        "cb_investment",
        "portfolio",
        ["portfolio_id"],
        ["id"],
        # Deleting a portfolio unfiles the investment; it never deletes the money or its history.
        ondelete="SET NULL",
    )
    op.create_index("ix_cb_investment_portfolio_id", "cb_investment", ["portfolio_id"])


def _downgrade_investment_link() -> None:
    op.drop_index("ix_cb_investment_portfolio_id", table_name="cb_investment")
    op.drop_constraint(
        "fk_cb_investment_portfolio_id_portfolio", "cb_investment", type_="foreignkey"
    )
    op.drop_column("cb_investment", "portfolio_id")
