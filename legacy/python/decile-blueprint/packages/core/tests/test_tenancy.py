"""P4.1 — a test enumerates the trading path and requires a tenant column."""

from __future__ import annotations

from baskfy_core.models import Base
from baskfy_core.tenancy import (
    DEFAULT_BROKER_ID,
    DESK_SQLITE_DEFERRED,
    ORDER_SHAPED_TABLES,
    TRADING_PATH_USER_ID_TABLES,
)


def test_default_broker_is_zerodha() -> None:
    assert DEFAULT_BROKER_ID == "zerodha"


def test_inventory_is_the_spec_not_empty() -> None:
    assert "cb_order_batch" in ORDER_SHAPED_TABLES
    assert "cb_investment" in ORDER_SHAPED_TABLES
    assert "broker_account" in TRADING_PATH_USER_ID_TABLES
    assert ORDER_SHAPED_TABLES <= TRADING_PATH_USER_ID_TABLES
    assert len(TRADING_PATH_USER_ID_TABLES) >= 8
    assert len(DESK_SQLITE_DEFERRED) >= 5


def test_every_trading_path_orm_table_exists_and_has_user_id() -> None:
    """P4.1 acceptance: no table in the trading path lacks a tenant column."""
    missing_tables: list[str] = []
    nullable: list[str] = []
    for name in sorted(TRADING_PATH_USER_ID_TABLES):
        table = Base.metadata.tables.get(name)
        if table is None:
            missing_tables.append(name)
            continue
        column = table.c.get("user_id")
        if column is None:
            missing_tables.append(f"{name}.user_id")
            continue
        if column.nullable:
            nullable.append(name)
    assert missing_tables == []
    assert nullable == []


def test_order_shaped_tables_carry_broker_account_id() -> None:
    missing: list[str] = []
    nullable: list[str] = []
    for name in sorted(ORDER_SHAPED_TABLES):
        table = Base.metadata.tables[name]
        column = table.c.get("broker_account_id")
        if column is None:
            missing.append(name)
            continue
        if column.nullable:
            nullable.append(name)
    assert missing == []
    assert nullable == []


def test_desk_sqlite_deferred_are_not_silently_in_the_orm() -> None:
    """If a desk table lands in Base.metadata, it must join the tenant inventory."""
    leaked = sorted(name for name in DESK_SQLITE_DEFERRED if name in Base.metadata.tables)
    assert leaked == []
