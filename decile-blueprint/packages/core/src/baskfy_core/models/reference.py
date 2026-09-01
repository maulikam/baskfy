"""Reference & instrument data — docs/04-data-model.md "Reference & instrument data".

Also holds ``trading_day``, which Prompt 1 deliverable 3 requires but docs/04 does not define.
See docs/04a-trading-day-addendum.md for its rationale.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from baskfy_core.models.base import FACE_VALUE, Base, BigIntPk, CreatedAt, SmallIntPk, UpdatedAt

#: docs/04: instrument.instrument_type ∈ {'EQ','ETF','INDEX'}
INSTRUMENT_TYPES: tuple[str, ...] = ("EQ", "ETF", "INDEX")


class Exchange(Base):
    __tablename__ = "exchange"

    id: Mapped[SmallIntPk]
    code: Mapped[str] = mapped_column(String, nullable=False, unique=True)  # 'NSE'


class Instrument(Base):
    """Rows are never deleted; ``delisted_on`` retires an instrument.

    Retaining delisted instruments is what keeps point-in-time screens free of survivorship
    bias (docs/01 §10, docs/02 rule 1).
    """

    __tablename__ = "instrument"
    __table_args__ = (
        UniqueConstraint("exchange_id", "symbol", "series"),
        CheckConstraint("instrument_type IN ('EQ', 'ETF', 'INDEX')", name="instrument_type_known"),
        Index("ix_instrument_symbol", "symbol"),
        Index("ix_instrument_kite_token", "kite_token"),
        # The listings register's exact sort key: `coalesce(listed_on, '0001-01-01') DESC,
        # symbol ASC` over active rows (`baskfy_api.market_data.listings`). An expression index,
        # because the sort key is an expression — an index on `listed_on` alone cannot serve it,
        # and without this the keyset page is a sequential scan even with `enable_seqscan = off`.
        # Prompt 16 deliverable 2 — migration 0008.
        Index(
            "ix_instrument_listings_page",
            text("(coalesce(listed_on, DATE '0001-01-01')) DESC"),
            text("symbol ASC"),
            postgresql_where=text("is_active IS TRUE"),
        ),
    )

    id: Mapped[BigIntPk]
    exchange_id: Mapped[int] = mapped_column(
        SmallInteger, ForeignKey("exchange.id"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    isin: Mapped[str | None] = mapped_column(String)
    instrument_type: Mapped[str] = mapped_column(String, nullable=False)
    series: Mapped[str | None] = mapped_column(String)
    face_value: Mapped[Decimal | None] = mapped_column(FACE_VALUE)
    lot_size: Mapped[int | None] = mapped_column(Integer)
    listed_on: Mapped[dt.date | None] = mapped_column(Date)
    delisted_on: Mapped[dt.date | None] = mapped_column(Date)
    kite_token: Mapped[int | None] = mapped_column(BigInteger)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class SymbolAlias(Base):
    """NSE renames symbols; keep the history mapped (docs/04)."""

    __tablename__ = "symbol_alias"

    id: Mapped[BigIntPk]
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.id"), nullable=False
    )
    old_symbol: Mapped[str] = mapped_column(String, nullable=False)
    changed_on: Mapped[dt.date] = mapped_column(Date, nullable=False)


#: The ``holiday_name`` `reconcile_calendar` writes when it decides a weekday with no bars was a
#: holiday. A *guess*, and the one guess in this table that has been wrong in production: on
#: 2026-08-28 a failed Kite fetch left a real trading Friday marked shut, and every backfill
#: afterwards skipped it because they all iterate trading days. M62 stopped the inference from
#: firing when NSE published a bhavcopy; the marker is a constant here so both the worker that
#: writes it and the API's resync detector, which hunts for days already in that state, name the
#: same string instead of two copies that can drift.
INFERRED_HOLIDAY_NAME: Final = "inferred: no instrument traded"

#: Every inferred verdict starts with this, so a detector can recognise the class rather than one
#: exact sentence.
INFERRED_HOLIDAY_PREFIX: Final = "inferred:"


class TradingDay(Base):
    """The NSE trading calendar.

    NOT specified in docs/04 — required by PROMPTS.md Prompt 1 deliverable 3. Every calendar day
    in the covered range gets a row so that ``snap_forward_to_trading_day`` /
    ``snap_backward_to_trading_day`` (docs/05 "Notation", docs/06 step 1) are a single indexed
    lookup rather than a loop, and so a missing range is distinguishable from a holiday.

    ``source`` records how the row was established, because holiday lists and exchange reality
    can disagree:
      ``weekend``   — Saturday/Sunday, derived.
      ``holiday``   — from the seeded NSE trading-holiday list.
      ``derived``   — assumed trading day, not yet corroborated by market data.
      ``bhavcopy``  — corroborated by an actual NSE bar/bhavcopy for that date (authoritative).
    """

    __tablename__ = "trading_day"
    __table_args__ = (
        CheckConstraint(
            "source IN ('weekend', 'holiday', 'derived', 'bhavcopy')", name="trading_day_source"
        ),
        Index("ix_trading_day_is_trading_day_date", "is_trading_day", "date"),
    )

    exchange_id: Mapped[int] = mapped_column(
        SmallInteger, ForeignKey("exchange.id"), primary_key=True
    )
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    is_trading_day: Mapped[bool] = mapped_column(Boolean, nullable=False)
    holiday_name: Mapped[str | None] = mapped_column(String)
    source: Mapped[str] = mapped_column(String, nullable=False)
