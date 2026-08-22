"""Declarative base and shared column vocabulary.

Precision policy: docs/13-csv-export-schema.md §4 pins the reference product's storage precision,
and docs/04-data-model.md defers to it in its "Precision matters" note ("Round at write time, so
the API, the UI and the CSV export can never disagree"). Where the inline DDL sketch in docs/04
disagrees with that note, the note — and therefore docs/13 §4 — wins. The named aliases below
exist so that no column invents its own precision.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from sqlalchemy import BigInteger, DateTime, Identity, MetaData, Numeric, SmallInteger, func
from sqlalchemy.orm import DeclarativeBase, mapped_column

# Deterministic constraint/index names, so Alembic autogenerate stays stable.
#: JSONB payloads are opaque to the domain layer. `object` (not `Any`) keeps mypy strict.
type JsonObject = dict[str, object]

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# --- Storage precisions (docs/13 §4) ----------------------------------------
PRICE = Numeric(18, 2)  # prices, moving averages, highs — 2 dp
PCT_2DP = Numeric(14, 2)  # returns, sharpe returns — 2 dp
AWAY_HIGH = Numeric(10, 2)  # away-from-high % — 2 dp
POS_DAYS = Numeric(7, 2)  # positive-days % — 2 dp
BREADTH = Numeric(7, 4)  # market_health_daily percentages — docs/04 DDL, not in docs/13 §4
RSI = Numeric(10, 4)  # RSI — 4 dp
RATIO_10DP = Numeric(18, 10)  # volatility (decimal fraction) and beta — 10 dp, feeds divisions
PRICE_RAW = Numeric(18, 4)  # ohlcv_daily source prices — 4 dp so adjustment is not lossy
ADJ_FACTOR = Numeric(18, 10)
MONEY = Numeric(20, 2)  # turnover in ₹
INR = Numeric(12, 2)  # plan prices, payments, GST
FUNDAMENTAL = Numeric(14, 4)  # PE / PB as published by NSE
YIELD = Numeric(10, 4)
WEIGHT = Numeric(10, 6)
FACE_VALUE = Numeric(12, 4)
RATIO_6DP = Numeric(18, 6)  # corporate-action ratios
QUANTITY = Numeric(20, 4)  # portfolio holding quantities

# --- Annotated column types --------------------------------------------------
# docs/04: "IDs are bigint identity" — GENERATED ALWAYS AS IDENTITY, not a serial default.
BigIntPk = Annotated[int, mapped_column(BigInteger, Identity(always=True), primary_key=True)]
SmallIntPk = Annotated[int, mapped_column(SmallInteger, primary_key=True, autoincrement=False)]
CreatedAt = Annotated[
    dt.datetime,
    mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now()),
]
UpdatedAt = Annotated[
    dt.datetime,
    mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    ),
]


class Base(DeclarativeBase):
    """Declarative base for every Baskfy table."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {  # noqa: RUF012
        dt.datetime: DateTime(timezone=True),
        int: BigInteger,
    }
