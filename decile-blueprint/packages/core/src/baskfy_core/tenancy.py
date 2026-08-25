"""Trading-path tenant inventory (docs/05 P4.1).

Pure names and sets — no database, no network, no clock. The test that enumerates
:data:`TRADING_PATH_USER_ID_TABLES` is the P4.1 acceptance criterion: no table on this
list may lack ``user_id``. Order-shaped tables also carry ``broker_account_id`` (Law 2).

Desk SQLite (``rebalance_orders``, ``fills``, ``trades``, ``snapshots``) is still the
operator's sole-tenant ledger and is listed in :data:`DESK_SQLITE_DEFERRED` rather than
migrated in this sitting — ``portfolio.db`` is unrebuildable evidence.
"""

from __future__ import annotations

#: Broker id written for the founder backfill and the default ``broker_account`` row.
DEFAULT_BROKER_ID: str = "zerodha"

#: Postgres ORM tables whose rows are per-user trading state. Each must have a non-null
#: ``user_id`` so a query names its tenant without a join (P4.1 / future P4.10 RLS).
TRADING_PATH_USER_ID_TABLES: frozenset[str] = frozenset(
    {
        "broker_account",
        "portfolio",
        "backtest",
        "cb_watchlist_item",
        "cb_investment",
        "cb_order_batch",
        "cb_fee_ledger",
        "cb_pending_action",
        "cb_user_rebalance_state",
        "cb_subscription",
    }
)

#: Rows that are an order or the book an order executes against. Law 2: every order
#: carries ``user_id`` **and** ``broker_account_id``. ``broker_account`` itself is the
#: account — its ``id`` is that value, so it is not in this set.
ORDER_SHAPED_TABLES: frozenset[str] = frozenset(
    {
        "cb_investment",
        "cb_order_batch",
    }
)

#: Desk tables that still live in SQLite / the ``desk`` schema projection. One operator,
#: no second tenant, until the writer flips. Named so the enumeration test cannot "pass"
#: by forgetting them.
DESK_SQLITE_DEFERRED: frozenset[str] = frozenset(
    {
        "rebalance_versions",
        "rebalance_orders",
        "fills",
        "trades",
        "snapshots",
        "regime_evaluations",
        "regime_exposure",
    }
)
