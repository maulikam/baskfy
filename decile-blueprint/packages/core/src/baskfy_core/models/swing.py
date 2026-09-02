"""The swing book's tables — ``docs/swing/03-data-model.md``.

The ``sw_`` prefix separates this layer from the screener schema (``ohlcv_daily``,
``factor_daily``), the curated-basket schema (``cb_``) and the desk's journal. Nothing here
replaces an existing table; every one of them joins ``instrument`` and, for the day's numbers,
``ohlcv_daily``.

WHERE THE VOCABULARY COMES FROM
-------------------------------
Every status, kind, reason and gate string below is **imported from** ``baskfy_core.swing``
rather than retyped. The engine writes these values and the database constrains them, so a
second copy of the list is a way for the two to disagree: a detector emitting a status the check
constraint rejects is an outage at 20:30 on a Tuesday, and a constraint permitting a status no
detector emits is a column nobody can trust. One definition, two consumers.

EVERY TABLE CARRIES ``user_id``
-------------------------------
``docs/swing/02-scope-and-gating.md`` Track C §6: "Every ``sw_`` row carries ``user_id``, as P4.1
requires, so the day D3 is answered nothing needs a migration — but no second user exists in this
run." That includes the two tables ``03`` sketches with a market-wide key (``sw_setup_daily``
keyed on ``(date, instrument_id, setup)`` and ``sw_market_daily`` on ``(date)``): both are
computed **through the liquidity floors in ``sw_config``**, which are per-user settings, so a row
of either is already a statement about one user's universe rather than about the market. See
``docs/swing/DECISIONS-SW.md`` SW2.1.

MONEY AND PRICES ARE ``numeric``
--------------------------------
House rule 9, and ``base.py``'s named precisions are the vocabulary: ``PRICE`` (18,2) for levels
the user reads, ``PRICE_RAW`` (18,4) where an exchange print must survive adjustment arithmetic,
``INR`` (12,2) for money, ``BREADTH`` (7,4) for the breadth percentages. Rounding happens at write
time (house rule 8) in the worker, not here.
"""

from __future__ import annotations

import datetime as dt
import uuid
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
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from baskfy_core.models.base import (
    ADJ_FACTOR,
    BREADTH,
    INR,
    MONEY,
    PRICE,
    PRICE_RAW,
    Base,
    BigIntPk,
    CreatedAt,
    JsonObject,
    UpdatedAt,
)
from baskfy_core.swing.config import Setup
from baskfy_core.swing.market import MarketGate
from baskfy_core.swing.opening_range import TriggerState
from baskfy_core.swing.plan import LineKind, SkipReason
from baskfy_core.swing.setups import CandidateStatus
from baskfy_core.swing.stops import ActionReason, StopMode, TrailMa

#: ``sw_setup_daily.score`` and the detector statistics — 0-100 with two decimals.
SCORE = Numeric(5, 2)
#: ``sw_setup_daily``'s per-setup measurements (ADR%, base depth, RVOL, gap%). ``docs/swing/03``.
MEASURE = Numeric(10, 2)
#: ``sw_config.risk_per_trade_pct`` — three decimals, because 0.125% is a real answer.
RISK_PCT = Numeric(5, 3)
#: ``sw_config.max_position_pct`` and the tier's exposure ceiling.
POSITION_PCT = Numeric(5, 2)


def _values(enum: type[Setup] | type[CandidateStatus] | type[MarketGate]) -> tuple[str, ...]:
    return tuple(member.value for member in enum)


#: The three setups (``baskfy_core.swing.config.Setup``). ``PARABOLIC_SHORT`` is storable and
#: never tradeable — the constraint that stops it becoming an order is ``TRADEABLE_SETUPS`` in
#: the plan builder and the test in SW10, not this column.
SW_SETUPS: tuple[str, ...] = _values(Setup)
SW_STATUSES: tuple[str, ...] = _values(CandidateStatus)
SW_GATES: tuple[str, ...] = _values(MarketGate)
SW_SIGNAL_STATES: tuple[str, ...] = tuple(s.value for s in TriggerState)
SW_LINE_KINDS: tuple[str, ...] = tuple(k.value for k in LineKind)
SW_SKIP_REASONS: tuple[str, ...] = tuple(r.value for r in SkipReason)
SW_TRAILS: tuple[str, ...] = tuple(t.value for t in TrailMa)
SW_STOP_MODES: tuple[str, ...] = tuple(m.value for m in StopMode)

#: ``sw_watch.source`` — who put the row there. A ``MANUAL`` row keeps the levels Maulik typed;
#: ``swing-premarket`` refreshes only ``DETECTOR`` rows (``docs/swing/03`` §4).
SW_WATCH_SOURCES: tuple[str, ...] = ("DETECTOR", "MANUAL")
SW_WATCH_STATES: tuple[str, ...] = ("WATCHING", "TRIGGERED", "EXPIRED", "DISMISSED")

#: ``sw_plan.source`` — the EOD preview, the 09:10 rebuild, or the one-line plan a live trigger
#: creates (``docs/swing/03`` §6).
SW_PLAN_SOURCES: tuple[str, ...] = ("EOD_PREVIEW", "MORNING", "SIGNAL")
SW_LINE_STATES: tuple[str, ...] = (
    "PROPOSED",
    "CONFIRMED",
    "SENT",
    "FILLED",
    "REJECTED",
    "EXPIRED",
    "SKIPPED",
)

SW_POSITION_STATES: tuple[str, ...] = ("OPEN", "PARTIAL", "CLOSED")
SW_FILL_SIDES: tuple[str, ...] = ("BUY", "SELL")

#: ``sw_position.close_reason`` — an ``ActionReason`` the stop rules produced, or ``MANUAL`` for
#: a position Maulik closed himself. ``MANUAL`` is not an ``ActionReason`` because the rules never
#: emit it; the column has to admit it because the book does.
SW_CLOSE_REASONS: tuple[str, ...] = (*(r.value for r in ActionReason), "MANUAL")

#: ``sw_session.mode``. ``DRY_RUN`` is what the 20-session paper gate in
#: ``docs/swing/02-scope-and-gating.md`` §3.2 counts.
SW_SESSION_MODES: tuple[str, ...] = ("DRY_RUN", "LIVE")

#: ``docs/swing/03`` §6: "a 30-minute expiry", the desk's own plan lifetime restated for this
#: surface. Named here because both the writer (the EOD job) and the reader (the desk's
#: ``/swing/execute``) need it and neither may invent its own number.
PLAN_TTL_MINUTES: int = 30


def _in_check(name: str, column: str, values: tuple[str, ...]) -> CheckConstraint:
    quoted = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({quoted})", name=name)


def _user_fk() -> Mapped[int]:
    """``user_id``, non-null, cascading. Every ``sw_`` table has one (Track C §6)."""
    return mapped_column(BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False)


class SwConfig(Base):
    """One row per user: the sleeve's capital and the knobs that are settings (PACK.5).

    Everything **not** here — base geometry, the EP gap, the ladder's tiers — is a
    ``baskfy_core.swing.config`` default and changing it is a code change with a DECISIONS-SW
    entry. The reason is in PACK.5 and it is behavioural rather than technical: "a threshold that
    can be changed in a form gets changed after a bad week, which is the failure mode the method
    exists to prevent".

    ``sleeve_capital_inr`` defaults to **0**, and that is the safety property of this table. A
    sleeve with no capital sizes nothing, so the system seeded for a new user plans nothing until
    a person decides what the swing book is allowed to risk. ``docs/swing/02`` §3.4 makes writing
    that number one of the five conditions on the real-money flag.

    ``exposure_level`` and ``first_live_sessions_left`` are written by the EOD job and the
    execute route respectively, **never by a form** — they are the system's memory of how the
    book has been doing, and a user who could set the rung could set it to 3 after three losses.
    """

    __tablename__ = "sw_config"
    __table_args__ = (
        _in_check("stop_mode_known", "stop_mode", SW_STOP_MODES),
        CheckConstraint("sleeve_capital_inr >= 0", name="capital_non_negative"),
        CheckConstraint("risk_per_trade_pct > 0", name="risk_positive"),
        CheckConstraint("max_position_pct > 0", name="position_pct_positive"),
        CheckConstraint("max_open_positions > 0", name="max_positions_positive"),
        CheckConstraint("or_window_minutes IN (1, 5, 60)", name="or_window_known"),
        CheckConstraint("exposure_level >= 0 AND exposure_level <= 3", name="exposure_level_range"),
        CheckConstraint("first_live_sessions_left >= 0", name="first_live_sessions_non_negative"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True
    )
    #: The cash the swing book may deploy. Its own sleeve, never the whole account.
    sleeve_capital_inr: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    risk_per_trade_pct: Mapped[Decimal] = mapped_column(
        RISK_PCT, nullable=False, server_default="0.500"
    )
    max_position_pct: Mapped[Decimal] = mapped_column(
        POSITION_PCT, nullable=False, server_default="20.00"
    )
    max_open_positions: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="8"
    )
    or_window_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="5")
    stop_mode: Mapped[str] = mapped_column(
        String, nullable=False, server_default=StopMode.LOW_OF_DAY.value
    )
    #: The liquidity floors of ``docs/swing/04`` §1, as settings rather than code, because they
    #: are the one part of the universe definition a person legitimately re-decides (a ₹5 cr
    #: floor is a different market from a ₹50 cr floor, and neither is wrong).
    adr_min_pct: Mapped[Decimal] = mapped_column(MEASURE, nullable=False, server_default="3.50")
    turnover_min_inr: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, server_default="50000000"
    )
    price_min: Mapped[Decimal] = mapped_column(PRICE, nullable=False, server_default="20.00")
    #: The ladder rung in force, 0-3. Written by ``swing-eod``; displayed, never edited.
    exposure_level: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    #: ``docs/swing/02`` §3.5 — "start small". Counts down from 5 once execution is enabled;
    #: while it is above zero the risk per trade is halved before sizing.
    first_live_sessions_left: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="5"
    )
    updated_at: Mapped[UpdatedAt]
    #: Who wrote the row last — a user id, or a job name for the fields a job owns.
    updated_by: Mapped[str | None] = mapped_column(String)


class SwConfigAudit(Base):
    """One row per changed field of :class:`SwConfig`. Append-only.

    The desk's ``settings_audit`` in one sentence: "You cannot reconstruct why a trade was sized
    the way it was without knowing what the parameters were at the time." ``sw_config`` carries
    ``updated_at``/``updated_by``, which answers *who last touched it* and nothing else — and the
    number that matters six months from now is what ``risk_per_trade_pct`` was on the morning of
    the trade, not what it is today.

    Shaped like ``desk.settings_audit`` on purpose (key, old, new, when, note), including
    one row **per field**, so a save that changes two knobs leaves two legible rows rather than a
    diff a person has to read. It is written in the same transaction as the change; a settings
    write whose audit row failed is a settings write that did not happen.

    ``docs/swing/DECISIONS-SW.md`` SW2.2 records why this is a table rather than the two columns
    ``docs/swing/03`` §1 sketches.
    """

    __tablename__ = "sw_config_audit"
    __table_args__ = (Index("ix_sw_config_audit_user_id_changed_at", "user_id", "changed_at"),)

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_fk()
    #: The ``sw_config`` column name, e.g. ``risk_per_trade_pct``.
    key: Mapped[str] = mapped_column(String, nullable=False)
    #: Rendered as text, like the desk's table: the audit trail must survive a column whose type
    #: changes, and a JSONB blob per row would make the common query ("show me every change to
    #: risk_per_trade_pct") a JSON path expression instead of an equality.
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str] = mapped_column(Text, nullable=False)
    changed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Who: a user id as text, or a job name for the fields a job owns (``swing-eod`` writes
    #: ``exposure_level``; ``/swing/execute`` writes ``first_live_sessions_left``).
    changed_by: Mapped[str] = mapped_column(String, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)


class SwSetupDaily(Base):
    """What the detectors found, per day. **Snapshotted, never recomputed** for a past date.

    The same rule as ``market_health_daily``, and for a stronger reason: a detector
    recalibration must not rewrite the record of what the system saw on the morning a trade was
    taken. Re-running the job for *today* is idempotent (the upsert below); re-running it for a
    past date after a threshold changed would silently rewrite history, so the task refuses that
    rather than the schema forbidding it.

    ``close``, ``trigger``, ``stop_ref`` and ``pivot_high`` are **exchange prices**. The core
    computes them on the adjusted series; the worker divides by the row's ``adj_factor`` before
    writing, and stores that factor alongside so a later split can be recognised for what it is.
    """

    __tablename__ = "sw_setup_daily"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "date", "instrument_id", "setup"),
        _in_check("setup_known", "setup", SW_SETUPS),
        _in_check("status_known", "status", SW_STATUSES),
        Index("ix_sw_setup_daily_date_setup_score", "date", "setup", "score"),
        Index("ix_sw_setup_daily_instrument_id_date", "instrument_id", "date"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.id"), nullable=False
    )
    setup: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    score: Mapped[Decimal] = mapped_column(SCORE, nullable=False)

    close: Mapped[Decimal | None] = mapped_column(PRICE)
    trigger: Mapped[Decimal | None] = mapped_column(PRICE)
    stop_ref: Mapped[Decimal | None] = mapped_column(PRICE)
    pivot_high: Mapped[Decimal | None] = mapped_column(PRICE)
    adj_factor: Mapped[Decimal | None] = mapped_column(ADJ_FACTOR)

    adr_pct: Mapped[Decimal | None] = mapped_column(MEASURE)
    prior_move_pct: Mapped[Decimal | None] = mapped_column(MEASURE)
    base_depth_pct: Mapped[Decimal | None] = mapped_column(MEASURE)
    tightness_adr: Mapped[Decimal | None] = mapped_column(MEASURE)
    dryup_ratio: Mapped[Decimal | None] = mapped_column(MEASURE)
    dist_ma_fast_pct: Mapped[Decimal | None] = mapped_column(MEASURE)
    dist_ma_slow_pct: Mapped[Decimal | None] = mapped_column(MEASURE)
    rvol: Mapped[Decimal | None] = mapped_column(MEASURE)
    gap_pct: Mapped[Decimal | None] = mapped_column(MEASURE)
    #: ₹, whole rupees — the same shape as ``factor_daily.vol_avg_1m``.
    turnover_avg: Mapped[int | None] = mapped_column(BigInteger)
    base_bars: Mapped[int | None] = mapped_column(SmallInteger)
    up_streak: Mapped[int | None] = mapped_column(SmallInteger)
    locked_upper_circuit: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    #: The instrument's sector index at ``date`` (``index_member_daily``); nullable, because a
    #: name that belongs to no sector index is still a candidate.
    sector_slug: Mapped[str | None] = mapped_column(String)
    #: ``instrument.listed_on`` within two years of ``date`` — his "young stock" preference,
    #: scored rather than filtered (``docs/swing/01`` §2).
    listed_within_2y: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    #: Which nightly run produced the row (provenance, as ``screen_run`` has).
    pipeline_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("pipeline_run.id", ondelete="SET NULL")
    )
    created_at: Mapped[CreatedAt]


class SwMarketDaily(Base):
    """Breadth, the index reading, the gate and tomorrow's tier — one row per session.

    ``detail`` carries the closed-trade R list the ladder read. A rung that cannot be explained
    is a rung nobody trusts, and "why am I allowed only two positions today" is the first
    question the page has to answer.
    """

    __tablename__ = "sw_market_daily"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "date"),
        _in_check("gate_known", "gate", SW_GATES),
        CheckConstraint("exposure_level >= 0 AND exposure_level <= 3", name="exposure_level_range"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    #: The liquid universe that day — the denominator of every percentage below.
    constituent_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    pct_up_strong_1m: Mapped[Decimal | None] = mapped_column(BREADTH)
    pct_new_52w_high: Mapped[Decimal | None] = mapped_column(BREADTH)
    pct_above_ma_slow: Mapped[Decimal | None] = mapped_column(BREADTH)

    index_slug: Mapped[str | None] = mapped_column(String)
    index_close: Mapped[Decimal | None] = mapped_column(PRICE)
    index_ma_fast: Mapped[Decimal | None] = mapped_column(PRICE)
    index_ma_slow: Mapped[Decimal | None] = mapped_column(PRICE)

    gate: Mapped[str] = mapped_column(String, nullable=False)
    exposure_level: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    max_open_positions: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    max_exposure_pct: Mapped[Decimal] = mapped_column(POSITION_PCT, nullable=False)
    new_entries_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: How many ``PARABOLIC_SHORT`` rows today — a froth gauge, not a trade list.
    parabolic_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    detail: Mapped[JsonObject | None] = mapped_column(JSONB)
    created_at: Mapped[CreatedAt]


class SwWatch(Base):
    """The watchlist, with the levels that would trigger.

    The one table on ``/swing`` the web app may write, and only in ways that move no money:
    adding a name, dismissing one, and the ``note``/``catalyst`` free text that is the method's
    "news check" (``docs/swing/01`` §3 — Baskfy holds no news feed, so a person types it).
    """

    __tablename__ = "sw_watch"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "instrument_id",
            "setup",
            "added_on",
            name="uq_sw_watch_user_id_instrument_id_setup_added_on",
        ),
        _in_check("setup_known", "setup", SW_SETUPS),
        _in_check("source_known", "source", SW_WATCH_SOURCES),
        _in_check("state_known", "state", SW_WATCH_STATES),
        Index("ix_sw_watch_user_id_state", "user_id", "state"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_fk()
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.id"), nullable=False
    )
    setup: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    added_on: Mapped[dt.date] = mapped_column(Date, nullable=False)
    #: Flags expire after ten sessions without a trigger, EPs after ``ep.valid_bars``. The
    #: worker computes the date from the trading calendar; the row only stores the answer.
    expires_on: Mapped[dt.date | None] = mapped_column(Date)
    trigger: Mapped[Decimal | None] = mapped_column(PRICE)
    stop_ref: Mapped[Decimal | None] = mapped_column(PRICE)
    #: The ``sw_setup_daily`` row this came from; NULL for a MANUAL row.
    setup_daily_date: Mapped[dt.date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)
    catalyst: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String, nullable=False, server_default="WATCHING")
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class SwSignal(Base):
    """What the monitor raised. Append-only, and **nothing here is an order**.

    Every verdict is stored, not only the triggers: a ``LOCKED_UPPER_CIRCUIT`` on the morning a
    name gapped is the record of why no trade happened, and ``BELOW_PIVOT`` is the record of a
    break that was not a breakout. A signal table that keeps only the yeses cannot answer "what
    did the system see".
    """

    __tablename__ = "sw_signal"
    __table_args__ = (
        _in_check("setup_known", "setup", SW_SETUPS),
        _in_check("state_known", "state", SW_SIGNAL_STATES),
        Index("ix_sw_signal_user_id_session_date", "user_id", "session_date"),
        Index("ix_sw_signal_instrument_id_session_date", "instrument_id", "session_date"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_fk()
    watch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("sw_watch.id", ondelete="SET NULL")
    )
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.id"), nullable=False
    )
    setup: Mapped[str] = mapped_column(String, nullable=False)
    session_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    raised_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    or_window_minutes: Mapped[int | None] = mapped_column(SmallInteger)
    range_high: Mapped[Decimal | None] = mapped_column(PRICE)
    range_low: Mapped[Decimal | None] = mapped_column(PRICE)
    low_of_day: Mapped[Decimal | None] = mapped_column(PRICE)
    last_price: Mapped[Decimal | None] = mapped_column(PRICE)
    entry: Mapped[Decimal | None] = mapped_column(PRICE)
    stop: Mapped[Decimal | None] = mapped_column(PRICE)
    plan_line_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("sw_plan_line.id", ondelete="SET NULL")
    )
    created_at: Mapped[CreatedAt]


class SwPlan(Base):
    """A day's plan. Mirrors the desk's plan lifecycle, including its 30-minute expiry.

    ``plan_hash`` is ``SwingPlan.plan_hash()`` — the sha256 of the canonical lines — so the same
    inputs produce the same plan and a re-post can be recognised as one. The ``plan_id`` is a
    uuid rather than the hash because two identical plans built an hour apart are still two
    plans, and the desk's idempotency key (``plan_id:symbol:kind``) has to distinguish them.
    """

    __tablename__ = "sw_plan"
    __table_args__ = (
        _in_check("source_known", "source", SW_PLAN_SOURCES),
        _in_check("gate_known", "gate", SW_GATES),
        Index("ix_sw_plan_user_id_as_of", "user_id", "as_of"),
    )

    id: Mapped[BigIntPk]
    plan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True)
    user_id: Mapped[int] = _user_fk()
    as_of: Mapped[dt.date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    built_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: ``built_at + 30 minutes``. Stored rather than derived so a change to
    #: :data:`PLAN_TTL_MINUTES` cannot retroactively extend a plan that was already issued.
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String, nullable=False)
    gate: Mapped[str] = mapped_column(String, nullable=False)
    exposure_level: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    total_risk_inr: Mapped[Decimal] = mapped_column(INR, nullable=False, server_default="0")
    total_new_exposure_inr: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, server_default="0"
    )
    created_at: Mapped[CreatedAt]


class SwPlanLine(Base):
    """One line of a plan — the unit a person confirms, one at a time.

    ``client_id`` is ``plan_id:symbol:kind``, the gateway's idempotency key. The ``kind`` is in
    it deliberately: a plan that both raises a stop and sells part of a position touches the same
    symbol twice, and ``plan_id:symbol`` alone would report the second as a DUPLICATE and
    silently skip it (the failure ``baskfy_execution.gateway`` documents for its separate GTT
    idempotency map).
    """

    __tablename__ = "sw_plan_line"
    __table_args__ = (
        UniqueConstraint("client_id", name="uq_sw_plan_line_client_id"),
        _in_check("kind_known", "kind", SW_LINE_KINDS),
        _in_check("state_known", "state", SW_LINE_STATES),
        Index("ix_sw_plan_line_plan_id", "plan_id"),
    )

    id: Mapped[BigIntPk]
    plan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sw_plan.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = _user_fk()
    kind: Mapped[str] = mapped_column(String, nullable=False)
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.id"), nullable=False
    )
    setup: Mapped[str | None] = mapped_column(String)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    trigger: Mapped[Decimal | None] = mapped_column(PRICE)
    stop: Mapped[Decimal | None] = mapped_column(PRICE)
    risk_inr: Mapped[Decimal] = mapped_column(INR, nullable=False, server_default="0")
    position_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    trail: Mapped[str | None] = mapped_column(String)
    note: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String, nullable=False, server_default="PROPOSED")
    client_id: Mapped[str] = mapped_column(String, nullable=False)
    #: The gateway journal line this became, once it became one.
    journal_ref: Mapped[str | None] = mapped_column(String)
    #: The ``sw_position`` the line opened or acted on, once it has one.
    position_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("sw_position.id", ondelete="SET NULL")
    )
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class SwPlanSkip(Base):
    """Why a watched name did **not** become a line. "A plan is not honest without them."""

    __tablename__ = "sw_plan_skip"
    __table_args__ = (
        _in_check("reason_known", "reason", SW_SKIP_REASONS),
        Index("ix_sw_plan_skip_plan_id", "plan_id"),
    )

    id: Mapped[BigIntPk]
    plan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sw_plan.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = _user_fk()
    instrument_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("instrument.id", ondelete="SET NULL")
    )
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[CreatedAt]


class SwPosition(Base):
    """The book: one row per **entry**, never per symbol.

    A second entry into a name after an exit is a second row, because the R-multiple of a trade
    is a fact about that entry's stop, and averaging two entries into one row destroys the only
    number the journal is kept in.

    This table is also the answer to "what does the swing sleeve own". ``docs/swing/02`` Track C
    §5: the sleeve "never sells a holding it did not buy", and the desk's ``/swing/execute``
    checks a SELL against ``quantity_open`` here rather than against the broker's holdings —
    which contain the weekly momentum book as well, and are not this book's to sell.
    """

    __tablename__ = "sw_position"
    __table_args__ = (
        _in_check("setup_known", "setup", SW_SETUPS),
        _in_check("state_known", "state", SW_POSITION_STATES),
        _in_check("trail_known", "trail", SW_TRAILS),
        _in_check("close_reason_known", "close_reason", SW_CLOSE_REASONS),
        CheckConstraint("quantity_open >= 0", name="quantity_open_non_negative"),
        CheckConstraint("quantity_open <= quantity_entered", name="quantity_open_within_entered"),
        CheckConstraint("initial_stop < entry_avg", name="initial_stop_below_entry"),
        #: A stop only ever rises, and it starts at ``initial_stop`` — so it can never be below
        #: it. The "never falls" rule itself is enforced where the update happens (the desk
        #: refuses a lower trigger); this is the floor that a bug in that code cannot get under.
        CheckConstraint("stop >= initial_stop", name="stop_never_below_initial"),
        Index("ix_sw_position_user_id_state", "user_id", "state"),
        Index("ix_sw_position_instrument_id", "instrument_id"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_fk()
    broker_account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("broker_account.id", ondelete="SET NULL")
    )
    instrument_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instrument.id"), nullable=False
    )
    setup: Mapped[str] = mapped_column(String, nullable=False)
    entry_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    entry_avg: Mapped[Decimal] = mapped_column(PRICE_RAW, nullable=False)
    quantity_entered: Mapped[int] = mapped_column(Integer, nullable=False)
    initial_stop: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    stop: Mapped[Decimal] = mapped_column(PRICE, nullable=False)

    #: The resting GTT. ``gtt_id IS NULL`` with ``quantity_open > 0`` is the one state the method
    #: forbids, and SW11's ``SWING_POSITION_NAKED`` alert is written against exactly that query.
    gtt_id: Mapped[str | None] = mapped_column(String)
    gtt_trigger: Mapped[Decimal | None] = mapped_column(PRICE)
    gtt_armed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    trail: Mapped[str] = mapped_column(String, nullable=False)
    partial_done: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    partial_date: Mapped[dt.date | None] = mapped_column(Date)
    quantity_open: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False, server_default="OPEN")

    closed_on: Mapped[dt.date | None] = mapped_column(Date)
    exit_avg: Mapped[Decimal | None] = mapped_column(PRICE_RAW)
    close_reason: Mapped[str | None] = mapped_column(String)
    r_multiple: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    pnl_inr: Mapped[Decimal | None] = mapped_column(INR)
    #: **True for every DRY_RUN / flag-off fill.** The journal page labels them and, until
    #: ``BASKFY_SWING_EXECUTION_ENABLED`` is true, the ladder reads them (PACK.6).
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class SwFill(Base):
    """One fill. ``exit_avg`` is derivable from these, and therefore auditable."""

    __tablename__ = "sw_fill"
    __table_args__ = (
        _in_check("side_known", "side", SW_FILL_SIDES),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        Index("ix_sw_fill_position_id", "position_id"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_fk()
    position_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sw_position.id", ondelete="CASCADE"), nullable=False
    )
    side: Mapped[str] = mapped_column(String, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(PRICE_RAW, nullable=False)
    filled_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    journal_ref: Mapped[str | None] = mapped_column(String)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[CreatedAt]


class SwSession(Base):
    """One row per session the system ran. This is the paper track record §3.2 counts.

    A session row exists whether or not anything was confirmed: "the monitor ran and raised
    nothing" is a session, and a gate that counts only the eventful days would be satisfied by
    twenty interesting mornings rather than by twenty ordinary ones.
    """

    __tablename__ = "sw_session"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "session_date"),
        _in_check("mode_known", "mode", SW_SESSION_MODES),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    session_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    monitor_ran: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    #: The plans built that day, as their uuids — the audit trail a session row points at.
    plan_ids: Mapped[JsonObject | None] = mapped_column(JSONB)
    signals: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    confirms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    fills: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    manage_actions: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class SwBacktestRun(Base):
    """One run of the swing EOD backtest (`docs/swing/03` §10, SW9). Append-only.

    The engine (``baskfy_core.swing.backtest``) is pure and this row is where its answer lands.
    A run is a *fact*: re-running inserts a new row and nothing edits a stored ``stats``, so the
    number that was on the page the week the flag was considered is still there after a detector
    recalibration produces a different one.

    ``params`` is written on the way in (what the run was asked to do), ``stats`` on the way out
    (``BacktestResult.to_json()``, stored as-is — every price a string of its exact decimal), and
    a run that raised carries the exception in ``error`` with ``finished_at`` set. "Finished" for
    the journal means ``finished_at`` set **and** ``error`` null.
    """

    __tablename__ = "sw_backtest_run"
    __table_args__ = (
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="finished_after_started",
        ),
        Index("ix_sw_backtest_run_user_id_finished_at", "user_id", "finished_at"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_fk()
    #: ``BacktestParams`` as JSON: ``start``, ``end``, ``sleeve_inr``, ``cost_pct_per_side`` and
    #: the whole ``config`` — the run is reproducible from this column and the bars alone.
    params: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: ``BacktestResult.to_json()``: ``params, trades, stats, by_setup, by_year, equity_curve,
    #: funnel, ladder, caveats``. Null until the run finishes, and forever if it failed.
    stats: Mapped[JsonObject | None] = mapped_column(JSONB)
    #: ``"{ExceptionType}: {message}"`` and the traceback, when the run raised.
    error: Mapped[str | None] = mapped_column(Text)
