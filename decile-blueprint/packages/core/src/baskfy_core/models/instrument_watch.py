"""Instrument watchlist + saved discover preferences (AF lane I).

Separate from ``CbWatchlistItem`` (baskets) and the swing book's ``SwWatch`` (setups). This is
the consumer stock list: star a name from a factsheet or a screen, see it under Watchlist →
Stocks.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from baskfy_core.models.base import INR, PRICE, Base, BigIntPk, CreatedAt


class InstrumentWatchItem(Base):
    """One watched equity (or ETF) for one account."""

    __tablename__ = "instrument_watch_item"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "instrument_id", name="uq_instrument_watch_item_user_instrument"
        ),
        Index("ix_instrument_watch_item_user_id", "user_id"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.id"), nullable=False
    )
    watched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Last close when watched, when known — moved-since uses this, never invents a baseline.
    close_at_watch: Mapped[Decimal | None] = mapped_column(PRICE)


class UserDiscoverPreferences(Base):
    """Goal-composer choices saved to the account (AF I.4).

    URL state on ``/discover`` remains the shareable filter. This row is what onboarding and a
    returning session read when there is no query string.
    """

    __tablename__ = "user_discover_preferences"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True
    )
    goal: Mapped[str] = mapped_column(String, nullable=False)
    horizon: Mapped[str] = mapped_column(String, nullable=False)
    risk: Mapped[str] = mapped_column(String, nullable=False)
    #: Rupees, whole; money is numeric, never float.
    amount: Mapped[Decimal] = mapped_column(INR, nullable=False)
    rebalance: Mapped[str] = mapped_column(String, nullable=False)
    #: Set when the connect → import → pick-a-basket flow finishes.
    onboarding_completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[CreatedAt]
