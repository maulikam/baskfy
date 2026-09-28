"""The FO run's tables — ``docs/fno/03-data-model.md`` (FO2), the ``fo_`` schema.

``0052_fno`` creates all of them. They share the NFO master (``op_contract``, widened by FO2 to
every F&O underlying) with the options pack and nothing else (PACK.5): the O-sleeves live on a
minute chain, the FO sleeves on the end-of-day bhavcopy.

TWO KINDS OF TABLE
------------------
**Market data, shared** — ``fo_contract_daily``, ``fo_underlying_daily``, ``fo_ingest_day`` and
``fo_spread_sample`` — are facts about NSE like ``ohlcv_daily`` and carry no ``user_id``.
**Everything else carries ``user_id``** (P4.1, ``02`` Track C §11), non-null, cascading from
``app_user``.

``fo_contract_daily`` IS PARTITIONED BY MONTH
---------------------------------------------
~13,000 retained rows a day, ~3.3 M a year (``03`` §1). ``RANGE (trade_date)``, monthly
partitions created by the migration from Jan 2022 (the backfill's start) through Dec 2027 and
thereafter by ``baskfy_worker.fno.partitions`` before the nightly ingest writes a month. No
DEFAULT partition, for the reason DECISIONS-OP OP2.5 gave for the chain: a row for a month with no
partition fails loudly. The primary key is the idempotency key of house rule 7 and includes the
partition key, as Postgres requires. A future's ``strike`` is 0 and its ``option_type`` ``XX``,
so the key has no nulls.

``fo_ingest_day`` IS THE STATUS PAGE'S ROW
------------------------------------------
``04`` §4: "A day with no file by then is ``MISSING`` on the status page, never interpolated." One
row per session the nightly task has tried: ``PENDING`` while it retries, ``INGESTED`` once the
day's rows are written, ``MISSING`` when 23:30 passes with no file. It also records the ban list
the same run read for the next session (Track C §9), so both facts of a night live in one row.

WHAT IS CONSTRAINED AND WHAT IS NOT
-----------------------------------
Vocabularies this module owns (sleeves, groups, instruments, structures, plan kinds, sides, fill
methods) are CHECKed. Vocabularies later modules own — a scan's state, a plan's status, a close
reason (FO4, FO7, FO10) — are plain text here and get their CHECKs from the module that defines
them (DECISIONS-OP OP2.4, carried). **No threshold from ``04`` is a CHECK**: constraints assert
shapes, not numbers.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Final

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from baskfy_core.models.base import INR, MONEY, PRICE, Base, BigIntPk, CreatedAt, JsonObject

# --- vocabularies ---------------------------------------------------------------------------------

#: ``01`` §4: ``F1N`` (NIFTY), ``F1B`` (BANKNIFTY) and ``F2``. Fixed by the research. ``F3N``
#: (NIFTY weekly) and ``F3B`` (BANKNIFTY monthly) added by 0059 (M.5, ``01`` §1c).
FO_SLEEVES: Final[tuple[str, ...]] = ("F1N", "F1B", "F2", "F3N", "F3B")
#: Config and flags are grouped as ``F1``, ``F2`` and ``F3`` (``01`` §4); F1's capital is one
#: number for both underlyings together (M.1), and so is F3's (M.5).
FO_SLEEVE_GROUPS: Final[tuple[str, ...]] = ("F1", "F2", "F3")
#: ``03`` §1: the legacy ``INSTRUMENT`` vocabulary, for both bhavcopy layouts.
FO_INSTRUMENTS: Final[tuple[str, ...]] = ("FUTSTK", "FUTIDX", "OPTSTK", "OPTIDX")
FO_FUTURE_INSTRUMENTS: Final[tuple[str, ...]] = ("FUTSTK", "FUTIDX")
#: ``XX`` for a future, so the primary key carries no null.
FO_CONTRACT_TYPES: Final[tuple[str, ...]] = ("CE", "PE", "XX")
FUTURE_TYPE: Final = "XX"
FO_INGEST_STATUSES: Final[tuple[str, ...]] = ("PENDING", "INGESTED", "MISSING")
#: ``03`` §4: ``IRON_CONDOR`` (F1), ``FUTURE`` (F2), ``CREDIT_SPREAD`` (F3, 0059).
FO_STRUCTURES: Final[tuple[str, ...]] = ("IRON_CONDOR", "FUTURE", "CREDIT_SPREAD")
#: ``ADD`` is F3's pyramid (``04`` §11): more lots onto the open spread, never a new position.
FO_PLAN_KINDS: Final[tuple[str, ...]] = ("ENTRY", "EXIT", "ROLL", "ADD")
#: ``03`` §9: the index underlyings ``fo_index_daily`` carries.
FO_INDEX_UNDERLYINGS: Final[tuple[str, ...]] = ("NIFTY", "BANKNIFTY")
FO_INDEX_SOURCES: Final[tuple[str, ...]] = ("KITE_HIST",)
FO_SIZING_MODES: Final[tuple[str, ...]] = ("BUDGET", "PAPER_ONE_LOT")
FO_LEG_ROLES: Final[tuple[str, ...]] = (
    "LONG_CALL",
    "LONG_PUT",
    "SHORT_CALL",
    "SHORT_PUT",
    "LONG_FUTURE",
)
FO_SIDES: Final[tuple[str, ...]] = ("BUY", "SELL")
FO_LEG_STATUSES: Final[tuple[str, ...]] = (
    "PENDING",
    "SENT",
    "FILLED",
    "PARTIAL",
    "CANCELLED",
    "REJECTED",
)
FO_SIM_METHODS: Final[tuple[str, ...]] = ("DEPTH_LADDER", "LIVE")
FO_SLIPPAGE_SOURCES: Final[tuple[str, ...]] = ("ASSUMED", "MEASURED")

#: Implied and realised vol, basis: model outputs, ``numeric(10,6)`` rounded at write (OP1.8).
VOL = Numeric(10, 6)
#: R multiples on the journal, as ``op_journal`` stores them.
R_MULTIPLE = Numeric(8, 2)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


FO_SLEEVE_ENUM = Enum(*FO_SLEEVES, name="fo_sleeve", native_enum=True, create_constraint=False)

_USER_FK = "app_user.id"


def _user_id(*, primary_key: bool = False) -> Mapped[int]:
    return mapped_column(
        BigInteger,
        ForeignKey(_USER_FK, ondelete="CASCADE"),
        nullable=False,
        primary_key=primary_key,
    )


def _updated_at() -> Mapped[dt.datetime]:
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )


# ================================================================================================
# Market data, shared (no user_id)
# ================================================================================================


class FoContractDaily(Base):
    """``03`` §1 — the F&O bhavcopy, one row per retained contract per session."""

    __tablename__ = "fo_contract_daily"
    __table_args__ = (
        PrimaryKeyConstraint("trade_date", "symbol", "expiry", "strike", "option_type"),
        CheckConstraint(_in("instrument", FO_INSTRUMENTS), name="instrument_known"),
        CheckConstraint(_in("option_type", FO_CONTRACT_TYPES), name="option_type_known"),
        CheckConstraint(
            "(instrument IN ('FUTSTK', 'FUTIDX')) = (option_type = 'XX')", name="future_iff_xx"
        ),
        CheckConstraint("strike >= 0", name="strike_non_negative"),
        Index("ix_fo_contract_daily_symbol_date", "symbol", "trade_date"),
        {"postgresql_partition_by": "RANGE (trade_date)"},
    )

    trade_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    instrument: Mapped[str] = mapped_column(String(6), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    expiry: Mapped[dt.date] = mapped_column(Date, nullable=False)
    #: 0 for a future.
    strike: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    #: ``CE``, ``PE`` or ``XX`` (a future).
    option_type: Mapped[str] = mapped_column(String(2), nullable=False)
    open: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    high: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    low: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    close: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    settle: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    #: UDiFF only (8 Jul 2024 onward).
    underlying: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    #: In shares, as NSE prints it.
    open_interest: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    oi_change: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: ₹.
    turnover: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    #: UDiFF only.
    lot_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The raw-archive key the row was parsed from (docs/09).
    source_key: Mapped[str] = mapped_column(Text, nullable=False)


class FoUnderlyingDaily(Base):
    """``03`` §2 — the derived per-underlying series, plus the ban list for the next session.

    Every derived column is nullable: the ban list lands on the row before the derivation has run
    (the nightly writes ``in_ban`` for tonight's session, and ``derive_underlying_daily`` fills
    the rest without touching it).
    """

    __tablename__ = "fo_underlying_daily"
    __table_args__ = (
        PrimaryKeyConstraint("trade_date", "symbol"),
        Index("ix_fo_underlying_daily_symbol_date", "symbol", "trade_date"),
    )

    trade_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    held_expiry: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    level_o: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    level_h: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    level_l: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    level_c: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    #: Log return of the held contract, previous settle → this settle.
    ret: Mapped[Decimal | None] = mapped_column(Numeric(14, 10), nullable=True)
    atr14: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    oi_total: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fut_turnover_20d: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    iv_atm: Mapped[Decimal | None] = mapped_column(VOL, nullable=True)
    rv20: Mapped[Decimal | None] = mapped_column(VOL, nullable=True)
    basis_ann: Mapped[Decimal | None] = mapped_column(VOL, nullable=True)
    #: The F&O ban list for the **next** session (Track C §9).
    in_ban: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    ca_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    updated_at: Mapped[dt.datetime] = _updated_at()


class FoIndexDaily(Base):
    """``03`` §9 — the index's own daily OHLC, from Kite's history on the index token (F3).

    The bhavcopy has no index candle and ``index_snapshot_daily`` keeps only a level; F3's
    levels, trend and weekly range need the highs and lows. Idempotent on ``(underlying,
    trade_date)``; prices rounded to the paisa at write (house rule 8).
    """

    __tablename__ = "fo_index_daily"
    __table_args__ = (
        PrimaryKeyConstraint("underlying", "trade_date"),
        CheckConstraint(_in("underlying", FO_INDEX_UNDERLYINGS), name="underlying_known"),
        CheckConstraint(_in("source", FO_INDEX_SOURCES), name="source_known"),
        CheckConstraint("high >= low", name="high_not_below_low"),
    )

    underlying: Mapped[str] = mapped_column(String(16), nullable=False)
    trade_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    open: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    high: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    low: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    close: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="KITE_HIST")
    updated_at: Mapped[dt.datetime] = _updated_at()


class FoIngestDay(Base):
    """One row per session the nightly ingest has tried — what the status page reads (``04`` §4)."""

    __tablename__ = "fo_ingest_day"
    __table_args__ = (
        CheckConstraint(_in("status", FO_INGEST_STATUSES), name="status_known"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
    )

    trade_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    rows_in_file: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rows_kept: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The session the ban list below applies to (the next one after ``trade_date``).
    ban_for_session: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    ban_symbols: Mapped[list[str] | None] = mapped_column(ARRAY(String(32)), nullable=True)
    ban_source_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[dt.datetime] = _updated_at()


class FoSpreadSample(Base):
    """``03`` §7 — the 15:00 quote sample (FO3). Market data, append-only."""

    __tablename__ = "fo_spread_sample"
    __table_args__ = (
        UniqueConstraint(
            "taken_at",
            "symbol",
            "expiry",
            "strike",
            "option_type",
            name="uq_fo_spread_sample_contract_moment",
        ),
        CheckConstraint(_in("option_type", ("CE", "PE")), name="option_type_known"),
        Index("ix_fo_spread_sample_symbol_date", "symbol", "trade_date"),
    )

    id: Mapped[BigIntPk]
    trade_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    expiry: Mapped[dt.date] = mapped_column(Date, nullable=False)
    strike: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    option_type: Mapped[str] = mapped_column(String(2), nullable=False)
    bid: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    ask: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    mid: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    oi: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    taken_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ================================================================================================
# User-scoped
# ================================================================================================


class FoBookConfig(Base):
    """``03`` §6 — one row per user: the book's monthly loss pause (≤ the ₹75,000 ceiling)."""

    __tablename__ = "fo_book_config"
    __table_args__ = (CheckConstraint("monthly_pause_inr >= 0", name="monthly_pause_non_negative"),)

    user_id: Mapped[int] = _user_id(primary_key=True)
    #: 0 until a person sets it; FO10 owns what 0 means.
    #: Ceiling ``BASKFY_FNO_BOOK_MONTHLY_LOSS_INR_MAX``.
    monthly_pause_inr: Mapped[Decimal] = mapped_column(INR, nullable=False, server_default="0")
    paused_until: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    paused_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[dt.datetime] = _updated_at()
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class FoSleeveConfig(Base):
    """``03`` §6 — one row per user per sleeve group (``F1``, ``F2``).

    ``capital_inr`` defaults to **0**; FO2's seed writes F1's ₹10,00,000 (Maulik, M.1) and leaves
    F2 at 0 (paper one lot, live refuses ``NO_SLEEVE_CAPITAL``).
    """

    __tablename__ = "fo_sleeve_config"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "sleeve"),
        CheckConstraint(_in("sleeve", FO_SLEEVE_GROUPS), name="sleeve_group_known"),
        CheckConstraint("capital_inr >= 0", name="capital_non_negative"),
        CheckConstraint("risk_per_trade_pct > 0", name="risk_pct_positive"),
        CheckConstraint("max_lots > 0", name="max_lots_positive"),
        CheckConstraint("max_open_positions > 0", name="max_open_positive"),
    )

    user_id: Mapped[int] = _user_id()
    sleeve: Mapped[str] = mapped_column(String(4), nullable=False)
    capital_inr: Mapped[Decimal] = mapped_column(INR, nullable=False, server_default="0")
    risk_per_trade_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, server_default="1.00"
    )
    max_lots: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="2")
    max_open_positions: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="1"
    )
    paper_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    paused_until: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    paused_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[dt.datetime] = _updated_at()
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class FoConfigAudit(Base):
    """Every change to ``fo_book_config`` / ``fo_sleeve_config`` (``03`` §6, "settings_audit").

    Shaped like ``op_config_audit``; ``scope`` names the row: ``BOOK``, ``F1`` or ``F2``.
    """

    __tablename__ = "fo_config_audit"
    __table_args__ = (
        CheckConstraint(_in("scope", ("BOOK", *FO_SLEEVE_GROUPS)), name="scope_known"),
        Index("ix_fo_config_audit_user_time", "user_id", "changed_at"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_id()
    scope: Mapped[str] = mapped_column(String(4), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    changed_by: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class FoScan(Base):
    """``03`` §3 — tomorrow's candidates, per sleeve, per symbol. FO4 owns ``state``."""

    __tablename__ = "fo_scan"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "sleeve", "trade_date", "symbol", name="uq_fo_scan_user_sleeve_date_symbol"
        ),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_id()
    sleeve: Mapped[str] = mapped_column(FO_SLEEVE_ENUM, nullable=False)
    trade_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    #: Every skip carries its reason in words, never a blank (``03`` §3).
    reasons: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    detail: Mapped[JsonObject] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    credit: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    max_loss_per_lot: Mapped[Decimal | None] = mapped_column(INR, nullable=True)
    cost_share: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    iv: Mapped[Decimal | None] = mapped_column(VOL, nullable=True)
    rv20: Mapped[Decimal | None] = mapped_column(VOL, nullable=True)
    iv_rv: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    created_at: Mapped[CreatedAt]


class FoPlan(Base):
    """``03`` §4 — the morning plan: ``op_plan``'s shape plus structure, kind and hard exit."""

    __tablename__ = "fo_plan"
    __table_args__ = (
        UniqueConstraint("plan_id", name="uq_fo_plan_plan_id"),
        CheckConstraint(_in("structure", FO_STRUCTURES), name="structure_known"),
        CheckConstraint(_in("kind", FO_PLAN_KINDS), name="kind_known"),
        CheckConstraint(_in("sizing_mode", FO_SIZING_MODES), name="sizing_mode_known"),
        CheckConstraint("expires_at > issued_at", name="expires_after_issue"),
        CheckConstraint("lots > 0", name="lots_positive"),
        CheckConstraint("lot_size > 0", name="lot_size_positive"),
        Index("ix_fo_plan_user_date_sleeve", "user_id", "trade_date", "sleeve"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_id()
    #: Deterministic (``03`` §4).
    plan_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sleeve: Mapped[str] = mapped_column(FO_SLEEVE_ENUM, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    trade_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    structure: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    sizing_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    #: A ``ROLL`` runs under its original confirm; this names that plan.
    parent_plan_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    issued_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_window_end: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: ``E - fo_hard_exit_before_expiry`` exchange sessions, from ``trading_day``.
    hard_exit_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    credit_points: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    debit_points: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    width_points: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    lots: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    lot_size: Mapped[int] = mapped_column(Integer, nullable=False)
    risk_per_lot_inr: Mapped[Decimal | None] = mapped_column(INR, nullable=True)
    risk_budget_inr: Mapped[Decimal | None] = mapped_column(INR, nullable=True)
    #: Computed from the legs, never entered.
    max_loss_inr: Mapped[Decimal | None] = mapped_column(INR, nullable=True)
    expected_cost_inr: Mapped[Decimal | None] = mapped_column(INR, nullable=True)
    cost_share: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    margin_required_inr: Mapped[Decimal | None] = mapped_column(INR, nullable=True)
    #: Recorded on every F1 plan so the forward sample can test IV ÷ RV20 (``01`` §1).
    iv_rv: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    #: ``04`` §8's plan state — FO7 owns the vocabulary.
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="ISSUED")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detail: Mapped[JsonObject | None] = mapped_column(JSONB, nullable=True)


class FoLeg(Base):
    """``03`` §4 — a plan's legs, in ``entry_seq`` order (longs first, ``02`` §2.1).

    ``instrument_token`` is not a foreign key: ``op_contract`` holds options only, and an F2 leg
    is a future.
    """

    __tablename__ = "fo_leg"
    __table_args__ = (
        UniqueConstraint("plan_id", "entry_seq", name="uq_fo_leg_plan_seq"),
        CheckConstraint(_in("role", FO_LEG_ROLES), name="role_known"),
        CheckConstraint(_in("side", FO_SIDES), name="side_known"),
        CheckConstraint(_in("option_type", FO_CONTRACT_TYPES), name="option_type_known"),
        CheckConstraint(_in("status", FO_LEG_STATUSES), name="status_known"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("filled_qty >= 0 AND filled_qty <= quantity", name="filled_within_qty"),
        CheckConstraint("entry_seq > 0", name="entry_seq_positive"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_id()
    plan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fo_plan.id", ondelete="CASCADE"), nullable=False
    )
    entry_seq: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(12), nullable=False)
    tradingsymbol: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expiry: Mapped[dt.date] = mapped_column(Date, nullable=False)
    strike: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    option_type: Mapped[str] = mapped_column(String(2), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    #: The bhavcopy settle the plan was built on.
    reference_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    bid: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    ask: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default="PENDING")
    filled_qty: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    avg_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class FoPosition(Base):
    """``03`` §5 — one row per open structure, carried across closes."""

    __tablename__ = "fo_position"
    __table_args__ = (
        CheckConstraint(_in("structure", FO_STRUCTURES), name="structure_known"),
        CheckConstraint("lots > 0", name="lots_positive"),
        CheckConstraint("lot_size > 0", name="lot_size_positive"),
        Index("ix_fo_position_user_open", "user_id", "closed_at"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_id()
    sleeve: Mapped[str] = mapped_column(FO_SLEEVE_ENUM, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    structure: Mapped[str] = mapped_column(String(16), nullable=False)
    entry_plan_id: Mapped[str] = mapped_column(String(64), nullable=False)
    legs: Mapped[JsonObject] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    lots: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    lot_size: Mapped[int] = mapped_column(Integer, nullable=False)
    #: F2: the future's entry price.
    entry_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    #: F1: the condor's credit, in points.
    entry_credit: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    max_loss_inr: Mapped[Decimal | None] = mapped_column(INR, nullable=True)
    profit_take_points: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    loss_close_points: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    #: F2: the current trailing stop, and the GTT that rests it broker-side.
    stop_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    gtt_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    hard_exit_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    next_roll_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    opened_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False)


class FoFill(Base):
    """``03`` §5 — every order's fill, real or simulated."""

    __tablename__ = "fo_fill"
    __table_args__ = (
        CheckConstraint(_in("side", FO_SIDES), name="side_known"),
        CheckConstraint(_in("sim_method", FO_SIM_METHODS), name="sim_method_known"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("price >= 0", name="price_non_negative"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_id()
    leg_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fo_leg.id", ondelete="CASCADE"), nullable=False
    )
    position_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("fo_position.id", ondelete="CASCADE"), nullable=True
    )
    #: The order journal's ``client_id`` (``plan_id:symbol``), for a real or simulated order.
    client_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    filled_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    sim_method: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[JsonObject | None] = mapped_column(JSONB, nullable=True)


class FoMark(Base):
    """``03`` §5 — one row per open position per session, at that session's settle."""

    __tablename__ = "fo_mark"
    __table_args__ = (PrimaryKeyConstraint("position_id", "trade_date"),)

    position_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fo_position.id", ondelete="CASCADE"), nullable=False
    )
    trade_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    user_id: Mapped[int] = _user_id()
    mark_points: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    pnl_inr: Mapped[Decimal] = mapped_column(INR, nullable=False)
    stop_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    detail: Mapped[JsonObject] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[CreatedAt]


class FoJournal(Base):
    """``03`` §6 — closed trades in ₹ and R, one row per structure, never pooling
    ``(sleeve, simulated)``."""

    __tablename__ = "fo_journal"
    __table_args__ = (
        CheckConstraint(_in("structure", FO_STRUCTURES), name="structure_known"),
        CheckConstraint(_in("sizing_mode", FO_SIZING_MODES), name="sizing_mode_known"),
        CheckConstraint("sessions_held >= 0", name="sessions_non_negative"),
        CheckConstraint("rolls >= 0", name="rolls_non_negative"),
        CheckConstraint("closed_on >= opened_on", name="dates_in_order"),
    )

    position_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fo_position.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = _user_id()
    sleeve: Mapped[str] = mapped_column(FO_SLEEVE_ENUM, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    structure: Mapped[str] = mapped_column(String(16), nullable=False)
    opened_on: Mapped[dt.date] = mapped_column(Date, nullable=False)
    closed_on: Mapped[dt.date] = mapped_column(Date, nullable=False)
    entry_inr: Mapped[Decimal] = mapped_column(INR, nullable=False)
    exit_inr: Mapped[Decimal] = mapped_column(INR, nullable=False)
    gross_pnl_inr: Mapped[Decimal] = mapped_column(INR, nullable=False)
    costs_inr: Mapped[Decimal] = mapped_column(INR, nullable=False)
    net_pnl_inr: Mapped[Decimal] = mapped_column(INR, nullable=False)
    risk_budget_inr: Mapped[Decimal] = mapped_column(INR, nullable=False)
    r_multiple: Mapped[Decimal] = mapped_column(R_MULTIPLE, nullable=False)
    closed_reason: Mapped[str] = mapped_column(String(32), nullable=False)
    sessions_held: Mapped[int] = mapped_column(Integer, nullable=False)
    rolls: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    mae_r: Mapped[Decimal | None] = mapped_column(R_MULTIPLE, nullable=True)
    mfe_r: Mapped[Decimal | None] = mapped_column(R_MULTIPLE, nullable=True)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    sizing_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[JsonObject | None] = mapped_column(JSONB, nullable=True)


class FoBacktestRun(Base):
    """``03`` §7 — one row per family per re-test run (FO9). Append-only; the page reads the
    latest row per family."""

    __tablename__ = "fo_backtest_run"
    __table_args__ = (
        CheckConstraint(_in("slippage_source", FO_SLIPPAGE_SOURCES), name="slippage_source_known"),
        CheckConstraint("sample_to >= sample_from", name="dates_in_order"),
        CheckConstraint("n >= 0", name="n_non_negative"),
        Index("ix_fo_backtest_run_user_family_time", "user_id", "family", "run_at"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_id()
    family: Mapped[str] = mapped_column(String(32), nullable=False)
    params: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    tier: Mapped[str] = mapped_column(String(4), nullable=False, server_default="2E")
    #: The verbatim caveat text.
    caveat: Mapped[str] = mapped_column(Text, nullable=False)
    sample_from: Mapped[dt.date] = mapped_column(Date, nullable=False)
    sample_to: Mapped[dt.date] = mapped_column(Date, nullable=False)
    n: Mapped[int] = mapped_column(Integer, nullable=False)
    net_r: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    gross_r: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    per_year: Mapped[JsonObject] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    slippage_source: Mapped[str] = mapped_column(String(8), nullable=False)
    run_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    git_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
