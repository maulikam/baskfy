"""The options run's tables — ``docs/options/03-data-model.md`` (OP2), the ``op_`` schema.

One schema for three sleeves (PACK.4): the master, the chain, the index bars, the calendar, the
session machine, the plan/leg/order/fill shape and the journal are shared, and a ``sleeve``
column (the Postgres enum ``op_sleeve``: ``O1M``, ``O1W``, ``O2``, ``O3A``, ``O3B``) keeps the
sleeves apart. ``0050_options`` creates all of them; nothing here alters an existing table.

TWO KINDS OF TABLE
------------------
**Market data, shared** — ``op_contract``, ``op_expiry``, ``op_index_minute``,
``op_chain_snapshot`` — are facts about NSE like ``ohlcv_daily`` and carry no ``user_id``.
**Everything else carries ``user_id``** (P4.1, ``02`` Track C §11), non-null, cascading from
``app_user`` — including ``op_event_day``, which ``03`` §3 lists with a ``user_id`` because a
person adds days from the web tab.

``op_contract`` IS ITS OWN TABLE, NOT ``instrument``
----------------------------------------------------
``instrument.instrument_type`` is ``EQ | ETF | INDEX`` and every screen, ranking and listing query
reads it; a thousand short-lived contracts a week do not belong there (``03`` "Correction to
docs/condor/03"). Rows are **never deleted**: an expired contract stays resolvable so a past
session's legs and snapshots keep their meaning.

``op_chain_snapshot`` IS PARTITIONED BY MONTH
---------------------------------------------
~46 k rows a day, ~11 M a year (``03`` §5). ``RANGE (ts)``, monthly partitions created by the
migration through Dec 2027 and thereafter by ``baskfy_worker.options.partitions`` before the
collector writes a month (OP3). No DEFAULT partition, deliberately: a row for a month with no
partition fails loudly rather than landing somewhere a later ``CREATE TABLE ... PARTITION OF``
would then refuse (DECISIONS-OP OP2.5). The primary key includes the partition key, as Postgres
requires, and is the idempotency key of house rule 7: ``(ts, instrument_token)``.

WHAT IS CONSTRAINED AND WHAT IS NOT
-----------------------------------
The vocabularies OP1 already owns (sleeves, expiry kinds, option types, sides, session states,
modes, sizing modes) are imported from ``baskfy_core.options`` and CHECKed, so the engine and the
database cannot disagree. The vocabularies later modules own — a scan's state, a skip or reject
code, a close reason (OP4, OP6 to OP9) — are plain text here and get their CHECKs from the module
that defines them, rather than a transcription that module would then have to match
(DECISIONS-OP OP2.4). **No threshold from ``04`` is a CHECK** (condor PACK.7 carried): the
constraints assert shapes — a lot size is positive, an expiry is not before its first sighting,
a plan cannot expire before it was issued — and leave the numbers to the config.

``op_book_config.underlying`` is the one exception worth naming: ``CHECK (underlying = 'NIFTY')``.
``02`` Track C §5 makes every other underlying forbidden in v1, the settings API refuses anything
else with a 422 (``baskfy_api.options_settings``), and the database says so a third time.
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
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from baskfy_core.models.base import INR, PRICE, Base, BigIntPk, CreatedAt, JsonObject
from baskfy_core.options.config import (
    ExpiryKind,
    Mode,
    OptionType,
    Side,
    SizingMode,
    Sleeve,
    SleeveGroup,
)
from baskfy_core.options.session import SessionState

# --- vocabularies (imported from the engine where the engine owns them) --------------------------

OP_SLEEVES: Final[tuple[str, ...]] = tuple(s.value for s in Sleeve)
OP_SLEEVE_GROUPS: Final[tuple[str, ...]] = tuple(g.value for g in SleeveGroup)
OP_EXPIRY_KINDS: Final[tuple[str, ...]] = tuple(k.value for k in ExpiryKind)
OP_OPTION_TYPES: Final[tuple[str, ...]] = tuple(t.value for t in OptionType)
OP_SIDES: Final[tuple[str, ...]] = tuple(s.value for s in Side)
OP_SESSION_MODES: Final[tuple[str, ...]] = tuple(m.value for m in Mode)
OP_SESSION_STATES: Final[tuple[str, ...]] = tuple(s.value for s in SessionState)
OP_SIZING_MODES: Final[tuple[str, ...]] = tuple(m.value for m in SizingMode)
#: ``03`` §3.
OP_EVENT_REASONS: Final[tuple[str, ...]] = (
    "RBI_POLICY",
    "UNION_BUDGET",
    "ELECTION_RESULT",
    "MANUAL",
)
OP_EVENT_SOURCES: Final[tuple[str, ...]] = ("SEED", "USER")
#: ``03`` §4.
OP_INDEX_BAR_SOURCES: Final[tuple[str, ...]] = ("KITE_HIST", "TICKS")
#: ``03`` §5.
OP_SNAPSHOT_SOURCES: Final[tuple[str, ...]] = ("QUOTE", "VENDOR")
#: ``03`` §9.
OP_STRUCTURES: Final[tuple[str, ...]] = ("IRON_CONDOR", "LONG_OPTION", "DEBIT_SPREAD")
OP_PLAN_KINDS: Final[tuple[str, ...]] = ("ENTRY", "EXIT")
OP_PLAN_STATUSES: Final[tuple[str, ...]] = ("ISSUED", "CONFIRMED", "LAPSED", "REJECTED")
OP_LEG_ROLES: Final[tuple[str, ...]] = ("LONG_CALL", "LONG_PUT", "SHORT_CALL", "SHORT_PUT")
OP_LEG_STATUSES: Final[tuple[str, ...]] = (
    "PENDING",
    "SENT",
    "FILLED",
    "PARTIAL",
    "CANCELLED",
    "REJECTED",
)
OP_SIM_METHODS: Final[tuple[str, ...]] = ("DEPTH_LADDER", "LIVE")
#: ``03`` §9: always MIS on NFO (``02`` Track C §1). The CHECK is the database saying it again.
OP_ORDER_PRODUCTS: Final[tuple[str, ...]] = ("MIS",)
OP_ORDER_EXCHANGES: Final[tuple[str, ...]] = ("NFO",)
#: ``02`` Track C §5 / PACK.12: NIFTY only in v1.
OP_UNDERLYINGS: Final[tuple[str, ...]] = ("NIFTY",)
#: ``04`` §13 — the evidence tiers.
OP_BACKTEST_TIERS: Final[tuple[int, ...]] = (1, 2, 3)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


#: The Postgres enum ``03`` names. ``create_type=False`` in the migration's own copy; here the
#: model describes it so ``Base.metadata`` knows the column's type.
OP_SLEEVE_ENUM = Enum(*OP_SLEEVES, name="op_sleeve", native_enum=True, create_constraint=False)

_USER_FK = "app_user.id"


def _user_id(*, primary_key: bool = False) -> Mapped[int]:
    return mapped_column(
        BigInteger,
        ForeignKey(_USER_FK, ondelete="CASCADE"),
        nullable=False,
        primary_key=primary_key,
    )


# ================================================================================================
# Market data, shared (no user_id)
# ================================================================================================


class OpContract(Base):
    """``03`` §1 — the NFO master for NIFTY options, written nightly, never deleted."""

    __tablename__ = "op_contract"
    __table_args__ = (
        CheckConstraint(_in("option_type", OP_OPTION_TYPES), name="option_type_known"),
        CheckConstraint("lot_size > 0", name="lot_size_positive"),
        CheckConstraint("tick_size > 0", name="tick_size_positive"),
        CheckConstraint("strike > 0", name="strike_positive"),
        CheckConstraint("last_seen >= first_seen", name="seen_in_order"),
        Index("ix_op_contract_underlying_expiry", "underlying", "expiry"),
    )

    instrument_token: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    #: As the master states it — never parsed for meaning (``03`` §1).
    tradingsymbol: Mapped[str] = mapped_column(String(64), nullable=False)
    underlying: Mapped[str] = mapped_column(String(16), nullable=False)
    expiry: Mapped[dt.date] = mapped_column(Date, nullable=False)
    strike: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    option_type: Mapped[str] = mapped_column(String(2), nullable=False)
    lot_size: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    tick_size: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    first_seen: Mapped[dt.date] = mapped_column(Date, nullable=False)
    last_seen: Mapped[dt.date] = mapped_column(Date, nullable=False)
    #: ``last_seen < expiry`` (dropped before expiry) or the master dropped it after expiry.
    expired: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class OpExpiry(Base):
    """``03`` §2 — the calendar the desk trusts, rebuilt nightly from ``op_contract``."""

    __tablename__ = "op_expiry"
    __table_args__ = (
        PrimaryKeyConstraint("underlying", "expiry_date"),
        CheckConstraint(_in("kind", OP_EXPIRY_KINDS), name="kind_known"),
        CheckConstraint("lot_size > 0", name="lot_size_positive"),
        CheckConstraint("seen_on >= first_seen", name="seen_in_order"),
    )

    underlying: Mapped[str] = mapped_column(String(16), nullable=False)
    expiry_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    lot_size: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    first_seen: Mapped[dt.date] = mapped_column(Date, nullable=False)
    #: The last night this expiry was in the master.
    seen_on: Mapped[dt.date] = mapped_column(Date, nullable=False)
    #: A changed lot size, a changed kind, or a withdrawal (a moved expiry) is recorded here.
    detail: Mapped[JsonObject] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class OpIndexMinute(Base):
    """``03`` §4 — NIFTY 50 and INDIA VIX one-minute bars."""

    __tablename__ = "op_index_minute"
    __table_args__ = (
        PrimaryKeyConstraint("instrument_id", "ts"),
        CheckConstraint(_in("source", OP_INDEX_BAR_SOURCES), name="source_known"),
        CheckConstraint("high >= low", name="high_not_below_low"),
    )

    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.id"), nullable=False
    )
    #: The minute's start.
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    high: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    low: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    close: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)


#: Greeks and IV: model outputs, ``numeric(10,6)`` rounded at write (OP1.8).
GREEK = Numeric(10, 6)


class OpChainSnapshot(Base):
    """``03`` §5 — the forward dataset, append-only, partitioned by month on ``ts``."""

    __tablename__ = "op_chain_snapshot"
    __table_args__ = (
        PrimaryKeyConstraint("ts", "instrument_token"),
        CheckConstraint(_in("option_type", OP_OPTION_TYPES), name="option_type_known"),
        CheckConstraint(_in("source", OP_SNAPSHOT_SOURCES), name="source_known"),
        Index("ix_op_chain_snapshot_ts_brin", "ts", postgresql_using="brin"),
        Index("ix_op_chain_snapshot_expiry_ts", "expiry", "ts"),
        {"postgresql_partition_by": "RANGE (ts)"},
    )

    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Not a foreign key: a foreign key from a partitioned table to ``op_contract`` would be
    #: checked on each of ~46 k inserts a day for a fact the collector read from the master.
    instrument_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expiry: Mapped[dt.date] = mapped_column(Date, nullable=False)
    strike: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    option_type: Mapped[str] = mapped_column(String(2), nullable=False)
    spot: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    bid: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    ask: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    last: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    bid_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ask_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    depth_json: Mapped[JsonObject | None] = mapped_column(JSONB, nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    oi: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    forward: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    iv: Mapped[Decimal | None] = mapped_column(GREEK, nullable=True)
    delta: Mapped[Decimal | None] = mapped_column(GREEK, nullable=True)
    gamma: Mapped[Decimal | None] = mapped_column(GREEK, nullable=True)
    theta: Mapped[Decimal | None] = mapped_column(GREEK, nullable=True)
    vega: Mapped[Decimal | None] = mapped_column(GREEK, nullable=True)
    greeks_model: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source: Mapped[str] = mapped_column(String(8), nullable=False, server_default="QUOTE")


# ================================================================================================
# User-scoped
# ================================================================================================


class OpEventDay(Base):
    """``03`` §3 — a day no sleeve trades. Seeded from primary sources; editable on the web."""

    __tablename__ = "op_event_day"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "date"),
        CheckConstraint(_in("reason", OP_EVENT_REASONS), name="reason_known"),
        CheckConstraint(_in("source", OP_EVENT_SOURCES), name="source_known"),
    )

    user_id: Mapped[int] = _user_id()
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(String(24), nullable=False)
    source: Mapped[str] = mapped_column(String(8), nullable=False)
    #: The primary source a seeded day was verified against (``04`` §1.3); null for a person's.
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[CreatedAt]


class OpScan(Base):
    """``03`` §6 — each sleeve's candidates, each minute. The web tab reads the latest row."""

    __tablename__ = "op_scan"
    __table_args__ = (
        UniqueConstraint("user_id", "sleeve", "ts", name="uq_op_scan_user_sleeve_ts"),
        Index("ix_op_scan_user_date_sleeve", "user_id", "trade_date", "sleeve"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_id()
    sleeve: Mapped[str] = mapped_column(OP_SLEEVE_ENUM, nullable=False)
    trade_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: ``04`` §10's scan state — OP4 owns the vocabulary (OP2.4).
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    reasons: Mapped[list[str]] = mapped_column(
        ARRAY(String(48)), nullable=False, server_default="{}"
    )
    numbers: Mapped[JsonObject] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    candidates: Mapped[JsonObject] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    as_of_minute: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class OpBookConfig(Base):
    """``03`` §7 — one row per user: the book's account, margin pool and derived-or-set limits."""

    __tablename__ = "op_book_config"
    __table_args__ = (
        CheckConstraint(_in("underlying", OP_UNDERLYINGS), name="underlying_nifty_only"),
        CheckConstraint("account_inr >= 0", name="account_non_negative"),
        CheckConstraint("margin_pool_inr >= 0", name="margin_pool_non_negative"),
        CheckConstraint("daily_loss_limit_inr >= 0", name="daily_limit_non_negative"),
        CheckConstraint("monthly_pause_inr >= 0", name="monthly_pause_non_negative"),
    )

    user_id: Mapped[int] = _user_id(primary_key=True)
    underlying: Mapped[str] = mapped_column(String(16), nullable=False, server_default="NIFTY")
    account_inr: Mapped[Decimal] = mapped_column(INR, nullable=False, server_default="0")
    margin_pool_inr: Mapped[Decimal] = mapped_column(INR, nullable=False, server_default="0")
    #: 0 = derived (``04`` §9.3). Ceiling ``BASKFY_OPTIONS_BOOK_DAILY_LOSS_INR_MAX``.
    daily_loss_limit_inr: Mapped[Decimal] = mapped_column(INR, nullable=False, server_default="0")
    #: 0 = derived. Ceiling ``BASKFY_OPTIONS_BOOK_MONTHLY_LOSS_INR_MAX``.
    monthly_pause_inr: Mapped[Decimal] = mapped_column(INR, nullable=False, server_default="0")
    paused_until: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    paused_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class OpSleeveConfig(Base):
    """``03`` §7 — one row per user per sleeve group (``O1M``, ``O1W``, ``O2``, ``O3``).

    ``sleeve_capital_inr``'s default is **0** (PACK.6): paper plans one lot, live refuses
    ``NO_SLEEVE_CAPITAL``. No agent sets it. The other defaults are ``03`` §7's and are written
    by the seeder per group; the column defaults are the conservative ones.
    """

    __tablename__ = "op_sleeve_config"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "sleeve"),
        CheckConstraint(_in("sleeve", OP_SLEEVE_GROUPS), name="sleeve_group_known"),
        CheckConstraint("sleeve_capital_inr >= 0", name="capital_non_negative"),
        CheckConstraint("risk_per_trade_pct > 0", name="risk_pct_positive"),
        CheckConstraint("max_lots > 0", name="max_lots_positive"),
    )

    user_id: Mapped[int] = _user_id()
    #: A sleeve **group** — O3-A and O3-B share one config row (``03`` "Sleeve codes").
    sleeve: Mapped[str] = mapped_column(String(4), nullable=False)
    sleeve_capital_inr: Mapped[Decimal] = mapped_column(INR, nullable=False, server_default="0")
    risk_per_trade_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, server_default="0.50"
    )
    max_lots: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="2")
    paper_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    #: Never later than ``BASKFY_OPTIONS_HARD_EXIT_LATEST`` [15:00] (``02`` Track C §1).
    hard_exit_time: Mapped[dt.time] = mapped_column(Time, nullable=False, server_default="14:30")
    paused_until: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    paused_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class OpConfigAudit(Base):
    """Every change to ``op_book_config`` / ``op_sleeve_config`` (``03`` §7, M4.1).

    Shaped like the desk's ``settings_audit`` and ``tw_config_audit`` (key, old, new, when, who,
    note), with ``scope`` naming the row: ``BOOK`` or the sleeve group.
    """

    __tablename__ = "op_config_audit"
    __table_args__ = (
        CheckConstraint(_in("scope", ("BOOK", *OP_SLEEVE_GROUPS)), name="scope_known"),
        Index("ix_op_config_audit_user_time", "user_id", "changed_at"),
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


class OpSession(Base):
    """``03`` §8 — one row per sleeve per trade date. ``mode`` is fixed at 09:15."""

    __tablename__ = "op_session"
    __table_args__ = (
        UniqueConstraint("user_id", "sleeve", "trade_date", name="uq_op_session_user_sleeve_date"),
        CheckConstraint(_in("mode", OP_SESSION_MODES), name="mode_known"),
        CheckConstraint(_in("state", OP_SESSION_STATES), name="state_known"),
        CheckConstraint(
            "slot_holder IS NULL OR " + _in("slot_holder", OP_SLEEVES), name="slot_holder_known"
        ),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_id()
    sleeve: Mapped[str] = mapped_column(OP_SLEEVE_ENUM, nullable=False)
    trade_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    expiry_used: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="OBSERVING")
    #: The sleeve's observation numbers (O1's gap/range/ER, O2's trend/OR, O3's range/gap).
    numbers: Mapped[JsonObject] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    verdict: Mapped[str | None] = mapped_column(String(16), nullable=True)
    skip_reasons: Mapped[list[str]] = mapped_column(
        ARRAY(String(48)), nullable=False, server_default="{}"
    )
    #: The entry plan, if any. Not a foreign key: ``op_plan`` names its session, and the pair
    #: would be mutually referential for a pointer the plan table already answers.
    plan_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    closed_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pnl_inr: Mapped[Decimal | None] = mapped_column(INR, nullable=True)
    pnl_r: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    slot_holder: Mapped[str | None] = mapped_column(String(4), nullable=True)
    created_at: Mapped[CreatedAt]


class OpPlan(Base):
    """``03`` §9 — an entry or exit plan; ``plan_id`` is the token the desk issues."""

    __tablename__ = "op_plan"
    __table_args__ = (
        CheckConstraint(_in("structure", OP_STRUCTURES), name="structure_known"),
        CheckConstraint(_in("kind", OP_PLAN_KINDS), name="kind_known"),
        CheckConstraint(_in("status", OP_PLAN_STATUSES), name="status_known"),
        CheckConstraint(_in("sizing_mode", OP_SIZING_MODES), name="sizing_mode_known"),
        CheckConstraint("expires_at > issued_at", name="expires_after_issue"),
        CheckConstraint("lots > 0", name="lots_positive"),
        CheckConstraint("lot_size > 0", name="lot_size_positive"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_id()
    plan_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    session_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("op_session.id", ondelete="CASCADE"), nullable=False
    )
    sleeve: Mapped[str] = mapped_column(OP_SLEEVE_ENUM, nullable=False)
    structure: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    sizing_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    issued_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_window_end: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    credit_points: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    debit_points: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    width_points: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    lots: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    lot_size: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    risk_per_lot_inr: Mapped[Decimal | None] = mapped_column(INR, nullable=True)
    risk_budget_inr: Mapped[Decimal | None] = mapped_column(INR, nullable=True)
    max_loss_inr: Mapped[Decimal | None] = mapped_column(INR, nullable=True)
    profit_target_inr: Mapped[Decimal | None] = mapped_column(INR, nullable=True)
    stop_inr: Mapped[Decimal | None] = mapped_column(INR, nullable=True)
    expected_cost_inr: Mapped[Decimal | None] = mapped_column(INR, nullable=True)
    cost_share: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    margin_required_inr: Mapped[Decimal | None] = mapped_column(INR, nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default="ISSUED")
    rejected_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    confirmed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    detail: Mapped[JsonObject | None] = mapped_column(JSONB, nullable=True)


class OpLeg(Base):
    """``03`` §9 — one leg of a plan, in send order (wings/longs first on entry)."""

    __tablename__ = "op_leg"
    __table_args__ = (
        UniqueConstraint("plan_id", "seq", name="uq_op_leg_plan_seq"),
        CheckConstraint(_in("role", OP_LEG_ROLES), name="role_known"),
        CheckConstraint(_in("side", OP_SIDES), name="side_known"),
        CheckConstraint(_in("option_type", OP_OPTION_TYPES), name="option_type_known"),
        CheckConstraint(_in("status", OP_LEG_STATUSES), name="status_known"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("filled_qty >= 0 AND filled_qty <= quantity", name="filled_within_qty"),
        CheckConstraint("seq > 0", name="seq_positive"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_id()
    plan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("op_plan.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(12), nullable=False)
    tradingsymbol: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument_token: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("op_contract.instrument_token"), nullable=False
    )
    strike: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    option_type: Mapped[str] = mapped_column(String(2), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    iv_at_plan: Mapped[Decimal | None] = mapped_column(GREEK, nullable=True)
    delta_at_plan: Mapped[Decimal | None] = mapped_column(GREEK, nullable=True)
    bid: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    ask: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    depth_json: Mapped[JsonObject | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default="PENDING")
    filled_qty: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    avg_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class OpOrder(Base):
    """``03`` §9 — one row per order the desk sends; ``client_id`` minted by the execution
    package (``plan_id:symbol`` / ``plan_id:symbol:CLOSE``). Always MIS on NFO."""

    __tablename__ = "op_order"
    __table_args__ = (
        CheckConstraint(_in("product", OP_ORDER_PRODUCTS), name="product_mis_only"),
        CheckConstraint(_in("exchange", OP_ORDER_EXCHANGES), name="exchange_nfo_only"),
        CheckConstraint(_in("side", OP_SIDES), name="side_known"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_id()
    leg_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("op_leg.id", ondelete="CASCADE"), nullable=False
    )
    client_id: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    product: Mapped[str] = mapped_column(String(8), nullable=False, server_default="MIS")
    exchange: Mapped[str] = mapped_column(String(8), nullable=False, server_default="NFO")
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The gateway's ``place()`` status (``DRY_RUN`` / ``PLACED`` / ``BLOCKED`` / …).
    gateway_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    gateway_result: Mapped[JsonObject | None] = mapped_column(JSONB, nullable=True)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[CreatedAt]


class OpFill(Base):
    """``03`` §9 — every fill, real or simulated (the depth ladder walked in ``detail``)."""

    __tablename__ = "op_fill"
    __table_args__ = (
        CheckConstraint(_in("sim_method", OP_SIM_METHODS), name="sim_method_known"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("price >= 0", name="price_non_negative"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_id()
    leg_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("op_leg.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("op_order.id", ondelete="CASCADE"), nullable=True
    )
    filled_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    sim_method: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[JsonObject | None] = mapped_column(JSONB, nullable=True)


class OpPosition(Base):
    """``03`` §10 — the open structure, one per session. Entry from fills, not the plan."""

    __tablename__ = "op_position"
    __table_args__ = (CheckConstraint("lots > 0", name="lots_positive"),)

    session_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("op_session.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = _user_id()
    leg_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), nullable=False)
    entry_points: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    entry_inr: Mapped[Decimal] = mapped_column(INR, nullable=False)
    lots: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    opened_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    hard_exit_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    peak_value: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    last_mark_points: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    last_mark_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_plan_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False)


class OpJournal(Base):
    """``03`` §11 — one row per session that opened. Never pooled across sleeves, ``simulated``
    or ``sizing_mode`` (``04`` §12)."""

    __tablename__ = "op_journal"
    __table_args__ = (
        CheckConstraint(_in("structure", OP_STRUCTURES), name="structure_known"),
        CheckConstraint(_in("sizing_mode", OP_SIZING_MODES), name="sizing_mode_known"),
        CheckConstraint("minutes_held >= 0", name="minutes_non_negative"),
    )

    session_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("op_session.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = _user_id()
    sleeve: Mapped[str] = mapped_column(OP_SLEEVE_ENUM, nullable=False)
    trade_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    expiry_used: Mapped[dt.date] = mapped_column(Date, nullable=False)
    structure: Mapped[str] = mapped_column(String(16), nullable=False)
    entry_inr: Mapped[Decimal] = mapped_column(INR, nullable=False)
    exit_inr: Mapped[Decimal] = mapped_column(INR, nullable=False)
    gross_pnl_inr: Mapped[Decimal] = mapped_column(INR, nullable=False)
    costs_inr: Mapped[Decimal] = mapped_column(INR, nullable=False)
    net_pnl_inr: Mapped[Decimal] = mapped_column(INR, nullable=False)
    risk_budget_inr: Mapped[Decimal] = mapped_column(INR, nullable=False)
    r_multiple: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    closed_reason: Mapped[str] = mapped_column(String(32), nullable=False)
    minutes_held: Mapped[int] = mapped_column(Integer, nullable=False)
    mae_r: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    mfe_r: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    sizing_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    half_size: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    detail: Mapped[JsonObject | None] = mapped_column(JSONB, nullable=True)


class OpBacktestRun(Base):
    """``03`` §12 — append-only; the caveat text is stored on the row, verbatim from ``07``."""

    __tablename__ = "op_backtest_run"
    __table_args__ = (
        CheckConstraint("tier IN (1, 2, 3)", name="tier_known"),
        CheckConstraint("date_to >= date_from", name="dates_in_order"),
        CheckConstraint(
            "sessions >= 0 AND signals >= 0 AND traded >= 0", name="counts_non_negative"
        ),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_id()
    sleeve: Mapped[str] = mapped_column(OP_SLEEVE_ENUM, nullable=False)
    tier: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    params_json: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    date_from: Mapped[dt.date] = mapped_column(Date, nullable=False)
    date_to: Mapped[dt.date] = mapped_column(Date, nullable=False)
    sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    signals: Mapped[int] = mapped_column(Integer, nullable=False)
    traded: Mapped[int] = mapped_column(Integer, nullable=False)
    skipped_by_reason_json: Mapped[JsonObject] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    win_rate: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    expectancy_r: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    net_pnl_inr: Mapped[Decimal | None] = mapped_column(INR, nullable=True)
    max_drawdown_r: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    caveats: Mapped[str] = mapped_column(Text, nullable=False)
    ran_at: Mapped[CreatedAt]
    git_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
