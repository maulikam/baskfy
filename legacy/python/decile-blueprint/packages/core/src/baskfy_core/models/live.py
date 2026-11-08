"""The "live at any login time" pack's tables (``docs/live/PLAN.md``, LV2).

Five tables the desk's processes share, beside the sleeves' own ``sw_*``/``tw_*``/``vb_*`` rows and
in the same ``public`` schema, so the desk's stores reach them through the same ``t()`` prefix and
the web API can read the two that describe state (heartbeats, issues) without a second connection.

* ``lv_protection_issue`` — what the reconciler found wrong with a position's protection: a fill
  with no stop (``NAKED``), a stop the broker no longer lists (``GTT_MISSING``), a stop for more or
  fewer shares than are held (``GTT_OVERSIZED`` / ``GTT_UNDERSIZED``), a stop that fired without a
  fill (``GTT_TRIGGERED_UNFILLED``), shares gone from the broker's book that the sleeve still
  records (``EXTERNAL_EXIT``). One open row per ``(user, sleeve, position, kind)``; a resolved
  row keeps its ``resolved_at``. Every sleeve's buy refuses while one is open for that sleeve.
* ``lv_heartbeat`` — one row per process (``supervisor``, ``reconciler``, ``swing_monitor``,
  ``twt_auto``): its last state, a detail line and when. The API's ``/sleeves/state`` reads it.
* ``lv_exit_order`` — a sell the broker accepted and has not yet filled, for the sleeve whose
  exit is an order rather than a GTT (VBT's ``SELL_AT_OPEN``). The position is booked only from
  the broker's fill, never from the placement (review P0.1's exit-side defect).
* ``lv_adoption`` — a holding bought by hand in Kite and adopted into a sleeve explicitly
  (LV6): the cost and quantity the person stated, and the stop it came with.
* ``risk_ledger`` — the account-wide risk state (``baskfy_execution.risk.RiskState``) as one
  JSON row per IST day, locked with ``SELECT … FOR UPDATE`` by every process that places an order
  (LV3).

Prices are ``numeric`` (house rule 9). Nothing here is read by a rank, a size or a plan.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from baskfy_core.models.base import PRICE_RAW, Base, BigIntPk, CreatedAt, UpdatedAt

#: The three sleeves whose books the pack reconciles.
LV_SLEEVES: tuple[str, ...] = ("swing", "twt", "vbt")

#: What the reconciler can find wrong with a position's protection.
LV_ISSUE_KINDS: tuple[str, ...] = (
    "NAKED",
    "GTT_MISSING",
    "GTT_OVERSIZED",
    "GTT_UNDERSIZED",
    "GTT_TRIGGERED_UNFILLED",
    "EXTERNAL_EXIT",
    "STOP_REJECTED",
)

#: A pending exit order's states, the broker's vocabulary reduced to what the book needs.
LV_EXIT_ORDER_STATES: tuple[str, ...] = ("SENT", "PARTIAL", "FILLED", "CANCELLED", "REJECTED")

#: The processes that write a heartbeat (``docs/live/PLAN.md``, LV4).
LV_HEARTBEAT_PROCESSES: tuple[str, ...] = ("supervisor", "reconciler", "swing_monitor", "twt_auto")


def _in(name: str, column: str, values: tuple[str, ...]) -> CheckConstraint:
    quoted = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({quoted})", name=name)


class LvProtectionIssue(Base):
    """One thing wrong with one position's protection, open until the reconciler stops seeing it."""

    __tablename__ = "lv_protection_issue"
    __table_args__ = (
        _in("sleeve_known", "sleeve", LV_SLEEVES),
        _in("kind_known", "kind", LV_ISSUE_KINDS),
        Index(
            "uq_lv_protection_issue_open",
            "user_id",
            "sleeve",
            "position_id",
            "kind",
            unique=True,
            postgresql_where="resolved_at IS NULL",
        ),
        Index("ix_lv_protection_issue_open_by_sleeve", "user_id", "sleeve", "resolved_at"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    sleeve: Mapped[str] = mapped_column(String(8), nullable=False)
    position_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[CreatedAt]


class LvHeartbeat(Base):
    """The last word from one of the desk's processes."""

    __tablename__ = "lv_heartbeat"
    __table_args__ = (PrimaryKeyConstraint("user_id", "process"),)

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    process: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LvExitOrder(Base):
    """A sell the broker accepted for a sleeve position, booked only when it fills."""

    __tablename__ = "lv_exit_order"
    __table_args__ = (
        _in("sleeve_known", "sleeve", LV_SLEEVES),
        _in("state_known", "state", LV_EXIT_ORDER_STATES),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("filled_quantity >= 0", name="filled_non_negative"),
        CheckConstraint("filled_quantity <= quantity", name="filled_within_quantity"),
        Index("ix_lv_exit_order_open", "user_id", "sleeve", "state"),
        Index("ix_lv_exit_order_broker", "user_id", "broker_order_id"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    sleeve: Mapped[str] = mapped_column(String(8), nullable=False)
    position_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    line_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    client_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    reference_price: Mapped[Decimal] = mapped_column(PRICE_RAW, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="SENT")
    filled_quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    avg_fill_price: Mapped[Decimal | None] = mapped_column(PRICE_RAW, nullable=True)
    reason: Mapped[str] = mapped_column(String(24), nullable=False)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class LvAdoption(Base):
    """A hand-bought holding adopted into a sleeve, on the person's stated cost and quantity."""

    __tablename__ = "lv_adoption"
    __table_args__ = (
        _in("sleeve_known", "sleeve", LV_SLEEVES),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("avg_cost > 0", name="cost_positive"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    sleeve: Mapped[str] = mapped_column(String(8), nullable=False)
    position_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_cost: Mapped[Decimal] = mapped_column(PRICE_RAW, nullable=False)
    gtt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[CreatedAt]


class RiskLedger(Base):
    """The account's risk state for one IST day, shared by every order-capable process (LV3)."""

    __tablename__ = "risk_ledger"
    __table_args__ = (PrimaryKeyConstraint("user_id", "day"),)

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    day: Mapped[dt.date] = mapped_column(Date, nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[UpdatedAt]
