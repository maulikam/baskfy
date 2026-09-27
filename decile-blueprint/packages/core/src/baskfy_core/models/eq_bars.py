"""One-minute equity bars for the liquid universe — ``eq_minute_bar`` (LV5).

The review's gap 1: "There is no intraday bar store for equities. Minute bars exist only for the
options collector's index levels." Nothing could run a live TWT or VBT variant, and nothing could
*backtest* one. This table is that store: one row per instrument per minute, written after each
session from Kite's ``historical_data(interval="minute")`` for the names the swing book calls
liquid, and backfilled over Kite's 60-day windows. Prices are ``numeric`` (house rule 9), rounded
at write (rule 8), and **raw** — the exchange's own prints, unadjusted: a corporate action is
applied by the reader that needs it, the way ``close_raw`` is served beside ``close``.

A TimescaleDB hypertable on ``ts`` (monthly chunks): ~570 names x 375 minutes a session is
roughly 210,000 rows a day, 50 million a year.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    PrimaryKeyConstraint,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from baskfy_core.models.base import PRICE, Base

#: Where a bar came from. ``KITE_HIST`` is the after-close reconcile and the backfill; ``TICKS``
#: is reserved for a live collector (not built in LV5 — DECISIONS-LV LV5.1).
EQ_BAR_SOURCES: tuple[str, ...] = ("KITE_HIST", "TICKS")


class EqMinuteBar(Base):
    """One instrument's one-minute candle. ``ts`` is the minute's start, timezone-aware."""

    __tablename__ = "eq_minute_bar"
    __table_args__ = (
        PrimaryKeyConstraint("instrument_id", "ts"),
        CheckConstraint("source IN ('KITE_HIST', 'TICKS')", name="source_known"),
        CheckConstraint("high >= low", name="high_not_below_low"),
        CheckConstraint("volume >= 0", name="volume_non_negative"),
    )

    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.id"), nullable=False
    )
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    high: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    low: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    close: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    source: Mapped[str] = mapped_column(String(16), nullable=False)
