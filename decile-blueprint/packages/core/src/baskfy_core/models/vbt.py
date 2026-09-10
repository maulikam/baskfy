"""The volume-breakout sleeve's tables — ``docs/vbt/03-data-model.md``.

The ``vb_`` prefix separates this sleeve from the screener schema (``ohlcv_daily``,
``factor_daily``), the swing book (``sw_``), the curated baskets (``cb_``) and the desk's journal.
Nothing here replaces or alters an existing table.

WHERE THE VOCABULARY COMES FROM
-------------------------------
Every state, kind, reason and gate string below is **imported from** ``baskfy_core.vbt`` rather
than retyped. The engine writes these values and the database constrains them; a second copy of
the list is a way for the two to disagree — a job emitting a state the check constraint rejects
is an outage at 21:00 on a Tuesday, and a constraint permitting a state no job emits is a column
nobody can trust. One definition, two consumers. (The swing tree learned this first; the pattern
is deliberately identical so a reader of one recognises the other.)

EVERY TABLE CARRIES ``user_id``
-------------------------------
``docs/vbt/02-scope-and-gating.md`` Track C §6. That includes the two tables ``03`` sketches with
a market-wide key — ``vb_signal_daily`` on ``(date, instrument_id)`` and ``vb_breadth_daily`` on
``(date)``. Both are **snapshots of what this user's system saw**, and the sleeve's own settings
decide what it may act on, so a row of either was already a statement about one user's book.

MONEY AND PRICES ARE ``numeric``
--------------------------------
House rule 9, and ``base.py``'s named precisions are the vocabulary: ``PRICE`` (18,2) for levels
a person reads, ``PRICE_RAW`` (18,4) where an exchange print must survive adjustment arithmetic,
``MONEY`` (20,2) for rupee turnover, ``INR`` (12,2) for the sleeve's money, ``BREADTH`` (7,4) for
the gate's percentage, ``ADJ_FACTOR`` (18,10). Rounding happens at write time (house rule 8) in
the worker, not here.

TWO CONSTRAINTS THAT ARE RULES RATHER THAN HYGIENE
--------------------------------------------------
``ck_vb_position_stop_never_below_initial`` is the floor a bug cannot get under: the stop only
ever rises (``04`` §6.1), and this is the database saying so a second time. And
``uq_vb_order_one_per_signal`` is what makes the evening job idempotent — one working order per
name per signal, so a re-run cannot double-place.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
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
from baskfy_core.vbt.config import Gate, SignalState, TrendFilter
from baskfy_core.vbt.exits import ExitReason
from baskfy_core.vbt.orders import CancelReason, OrderState
from baskfy_core.vbt.plan import LineKind, SkipReason
from baskfy_core.vbt.sizing import SizeCap

#: The sleeve's percentage settings — ``max_position_pct`` and ``stop_pct``.
SLEEVE_PCT = Numeric(5, 2)
#: ``vb_signal_daily``'s per-bar measurements: change %, relative volume, close position, the
#: 20-session return. Two decimals, as ``04`` §1 computes them.
MEASURE = Numeric(10, 2)

VB_SIGNAL_STATES: tuple[str, ...] = tuple(state.value for state in SignalState)
VB_TREND_FILTERS: tuple[str, ...] = tuple(name.value for name in TrendFilter)
VB_GATES: tuple[str, ...] = tuple(gate.value for gate in Gate)
VB_ORDER_STATES: tuple[str, ...] = tuple(state.value for state in OrderState)
VB_CANCEL_REASONS: tuple[str, ...] = tuple(reason.value for reason in CancelReason)
VB_LINE_KINDS: tuple[str, ...] = tuple(kind.value for kind in LineKind)
VB_SKIP_REASONS: tuple[str, ...] = tuple(reason.value for reason in SkipReason)
VB_SIZE_CAPS: tuple[str, ...] = tuple(cap.value for cap in SizeCap)

#: ``vb_position.close_reason`` — an ``ExitReason`` the rules produced, or ``MANUAL`` for a
#: position a person closed. ``MANUAL`` is not an ``ExitReason`` because the rules never emit it;
#: the column has to admit it because the book does.
VB_CLOSE_REASONS: tuple[str, ...] = (*(reason.value for reason in ExitReason), "MANUAL")

VB_POSITION_STATES: tuple[str, ...] = ("OPEN", "CLOSED")
VB_FILL_SIDES: tuple[str, ...] = ("BUY", "SELL")

#: ``vb_plan.source`` — the evening's plan from the closed session, the morning's rebuild before
#: the open, or a rebuild a person asked the desk for. There is no live-trigger source: this is
#: an end-of-day strategy and nothing about it fires inside a session (``02`` Track C §3).
VB_PLAN_SOURCES: tuple[str, ...] = ("EVENING", "MORNING", "MANUAL")
VB_LINE_STATES: tuple[str, ...] = (
    "PROPOSED",
    "CONFIRMED",
    "SENT",
    "FILLED",
    "REJECTED",
    "EXPIRED",
    "SKIPPED",
)

#: ``vb_session.mode``. ``DRY_RUN`` is what ``02`` §3.1's twenty-session gate counts — a gate for
#: this sleeve, not the information counter the swing book's became.
VB_SESSION_MODES: tuple[str, ...] = ("DRY_RUN", "LIVE")

#: ``vb_backtest_run.source`` — which bars produced the number. Kept apart on the page so a
#: reproduction of the study and a run over the plant can never be read as the same claim.
VB_BACKTEST_SOURCES: tuple[str, ...] = ("PLANT", "RESEARCH_EXPORT")

#: ``docs/vbt/03`` §6: the desk's own plan lifetime, restated for this surface. Named here
#: because both the writer (the evening job) and the reader (``/vbt/execute``) need it and
#: neither may invent its own number. Prefixed, because the swing book exports its own
#: ``PLAN_TTL_MINUTES`` and two sleeves agreeing on a number today is not the same as one number.
VB_PLAN_TTL_MINUTES: int = 30


def _in_check(name: str, column: str, values: tuple[str, ...]) -> CheckConstraint:
    quoted = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({quoted})", name=name)


def _user_fk() -> Mapped[int]:
    """``user_id``, non-null, cascading. Every ``vb_`` table has one (Track C §6)."""
    return mapped_column(BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False)


def _instrument_fk() -> Mapped[int]:
    return mapped_column(
        BigInteger, ForeignKey("instrument.id", ondelete="CASCADE"), nullable=False
    )


class VbConfig(Base):
    """One row per user: the sleeve's capital and the four numbers that are settings.

    Everything **not** here is a ``baskfy_core.vbt.config`` default, and changing one is a code
    change with a ``DECISIONS-VB.md`` entry (PACK.5). The reason is behavioural rather than
    technical: a threshold that can be changed in a form gets changed after a bad week, which is
    the failure mode a rule-based sleeve exists to prevent.

    ``sleeve_capital_inr`` defaults to **0**, and that is this table's safety property. A sleeve
    with no capital sizes nothing, so a freshly seeded system plans nothing at all until a person
    decides what this book may risk. ``docs/vbt/02`` §3.4 makes writing that number one of the
    five conditions on the real-money flag.

    ``dry_run_sessions`` and ``first_live_sessions_left`` are the evening job's, **never a
    form's**: the first is ``02`` §3.1's gate and a user who could set it could set it to 20, and
    the second is the countdown that halves the first live sessions' size.
    """

    __tablename__ = "vb_config"
    __table_args__ = (
        CheckConstraint("sleeve_capital_inr >= 0", name="capital_non_negative"),
        CheckConstraint("max_open_positions > 0", name="max_positions_positive"),
        CheckConstraint("max_position_pct > 0", name="position_pct_positive"),
        CheckConstraint("stop_pct > 0", name="stop_pct_positive"),
        CheckConstraint("first_live_sessions_left >= 0", name="first_live_non_negative"),
        CheckConstraint("dry_run_sessions >= 0", name="dry_run_sessions_non_negative"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True
    )
    #: The cash this sleeve may deploy. Its own money, never the whole account (PACK.4).
    sleeve_capital_inr: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    max_open_positions: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="10"
    )
    max_position_pct: Mapped[Decimal] = mapped_column(
        SLEEVE_PCT, nullable=False, server_default="12.50"
    )
    #: The one exit number that is a setting, because STRATEGY §4 measured 10% and 15% and found
    #: the stop to be insurance either way.
    stop_pct: Mapped[Decimal] = mapped_column(SLEEVE_PCT, nullable=False, server_default="12.00")
    first_live_sessions_left: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="5"
    )
    #: ``02`` §3.1's gate: how many DRY_RUN sessions have closed with a plan built and a line
    #: simulated. Written once per session by the evening job.
    dry_run_sessions: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    updated_at: Mapped[UpdatedAt]
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class VbConfigAudit(Base):
    """What a setting **was** on the evening a line was sized.

    ``updated_at`` says who touched the row last; it cannot answer the question a person asks
    after a bad trade. One row per field changed, written in the same transaction as the change —
    the shape the desk's own ``settings_audit`` and ``sw_config_audit`` already use.
    """

    __tablename__ = "vb_config_audit"
    __table_args__ = (Index("ix_vb_config_audit_user_time", "user_id", "changed_at"),)

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_fk()
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_at: Mapped[CreatedAt]
    #: A user id as text, or a job name for the fields a job owns (``vbt-evening``).
    changed_by: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class VbSignalDaily(Base):
    """What the detector found, per session. A **snapshot**, never recomputed for a past date.

    ``SCAN_ONLY`` rows — the five Chartink lines held and a trend filter did not — are stored on
    purpose (DECISIONS-VB PACK.6). ``01`` §3's ablation table is the whole argument for the six
    filters, and a system that stores only what it accepted cannot show a person what it passed
    over, or answer "why not this one?".

    Every level is an **exchange price**: the worker divides the adjusted level by the row's
    ``adj_factor`` at write time and records the factor alongside, so a later split is
    recognisable rather than silently rewriting history.
    """

    __tablename__ = "vb_signal_daily"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "date", "instrument_id", name="pk_vb_signal_daily"),
        _in_check("state_known", "state", VB_SIGNAL_STATES),
        CheckConstraint("limit_price > 0", name="limit_positive"),
        CheckConstraint("stop_price > 0 AND stop_price < limit_price", name="stop_below_limit"),
        Index("ix_vb_signal_daily_rank", "date", "state", "rank_key"),
        Index("ix_vb_signal_daily_instrument", "instrument_id", "date"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    instrument_id: Mapped[int] = _instrument_fk()
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Which of ``A``…``F`` failed. Empty for a ``SIGNAL`` row; the page reads it verbatim.
    failed_filters: Mapped[list[str]] = mapped_column(
        ARRAY(String(1)), nullable=False, server_default="{}"
    )

    open: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    high: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    low: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    close: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    close_raw: Mapped[Decimal] = mapped_column(PRICE_RAW, nullable=False)
    adj_factor: Mapped[Decimal] = mapped_column(ADJ_FACTOR, nullable=False, server_default="1")

    #: The entry level: the signal bar's close as an exchange price, snapped to the tick.
    limit_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    #: A preview of the stop the fill will get. The real one is measured from the fill (`04` §6.1).
    stop_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)

    change_pct: Mapped[Decimal | None] = mapped_column(MEASURE, nullable=True)
    rvol: Mapped[Decimal | None] = mapped_column(MEASURE, nullable=True)
    close_position: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    ret_20_pct: Mapped[Decimal | None] = mapped_column(MEASURE, nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    vol_sma_50: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    turnover_inr: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    turnover_avg_20: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sma_200: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    ema_21: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    high_20_prior: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)

    #: What a same-session tie is broken by: the signal-day rupee turnover (``04`` §3.4).
    rank_key: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    #: Kept and flagged, never dropped; the plan is what skips it.
    locked_upper_circuit: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    #: How many bars the 200-session window actually held, so ``04`` §2.2's tolerance is auditable.
    bars_in_window: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    pipeline_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("pipeline_run.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[CreatedAt]


class VbBreadthDaily(Base):
    """The gate's reading, per session. Written in the same transaction as the signals.

    Not to be confused with ``market_health_daily.pct_above_200dma``, which measures an index's
    point-in-time membership on the plant's ordinary calendar. This one measures the whole traded
    universe on the run's thin-session calendar; they answer different questions and will differ
    (DECISIONS-VB VB0.3).
    """

    __tablename__ = "vb_breadth_daily"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "date", name="pk_vb_breadth_daily"),
        _in_check("gate_known", "gate", VB_GATES),
        CheckConstraint("above_count <= measured_count", name="above_within_measured"),
        CheckConstraint("measured_count <= universe_count", name="measured_within_universe"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    universe_count: Mapped[int] = mapped_column(Integer, nullable=False)
    measured_count: Mapped[int] = mapped_column(Integer, nullable=False)
    above_count: Mapped[int] = mapped_column(Integer, nullable=False)
    pct_above_dma: Mapped[Decimal] = mapped_column(BREADTH, nullable=False)
    gate: Mapped[str] = mapped_column(String(8), nullable=False)
    #: Stored so a recalibration of the window is visible in the history rather than implied.
    dma_bars: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="200")
    #: This date was dropped from the rolling calendar (``04`` §2.1). Such a row is written for
    #: the record with a shut gate: the sleeve did not trade a muhurat session.
    thin_session: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    #: The funnel: universe → with a bar → with a 200-day average → above it → signals →
    #: scan-only, and the thin sessions dropped inside the window.
    detail: Mapped[JsonObject | None] = mapped_column(JSONB, nullable=True)
    pipeline_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("pipeline_run.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[CreatedAt]


class VbOrder(Base):
    """The working limit order — the one mechanism the desk did not have before this sleeve.

    A VBT entry is a limit at the signal bar's close that works for three **sessions** and is then
    cancelled (``04`` §7). ``sessions_worked`` is a count rather than a date difference because a
    limit resting over a holiday has not worked a session, and "three sessions" has to mean the
    same thing in the book as it does in the backtest.

    ``uq_vb_order_one_per_signal`` is what makes the evening job idempotent: one working order per
    name per signal, so a re-run of the evening cannot double-place.
    """

    __tablename__ = "vb_order"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "signal_date", "instrument_id", name="uq_vb_order_one_per_signal"
        ),
        _in_check("state_known", "state", VB_ORDER_STATES),
        _in_check("cancel_reason_known", "cancel_reason", VB_CANCEL_REASONS),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("limit_price > 0", name="limit_positive"),
        CheckConstraint("stop_price > 0 AND stop_price < limit_price", name="stop_below_limit"),
        CheckConstraint("filled_quantity >= 0", name="filled_non_negative"),
        CheckConstraint("filled_quantity <= quantity", name="filled_within_quantity"),
        CheckConstraint("sessions_worked >= 0", name="sessions_worked_non_negative"),
        Index("ix_vb_order_sweep", "user_id", "state", "expires_after_session"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_fk()
    broker_account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    instrument_id: Mapped[int] = _instrument_fk()
    signal_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    limit_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    stop_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    #: The first session it may fill on, and the last (``04`` §7.2). ``expires_after_session`` is
    #: null only while the calendar does not yet reach that far.
    working_from: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    expires_after_session: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    sessions_worked: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: ``plan_id:symbol:kind`` — the gateway's idempotency key, so a re-post cannot double-send.
    client_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    filled_quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    avg_fill_price: Mapped[Decimal | None] = mapped_column(PRICE_RAW, nullable=True)
    position_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("vb_position.id", ondelete="SET NULL"), nullable=True
    )
    cancelled_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: True for every DRY_RUN / flag-off order. The pages label them and never mix the totals.
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    detail: Mapped[JsonObject | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class VbPosition(Base):
    """The book. One row per **entry**, never per symbol.

    This table is the sleeve's source of truth for what it owns, and Track C §5 turns that into a
    rule: the sleeve never sells a holding it did not buy. A name in the broker's account that is
    not here is the weekly book's, the swing book's or Maulik's, and this sleeve cannot see it.
    """

    __tablename__ = "vb_position"
    __table_args__ = (
        _in_check("state_known", "state", VB_POSITION_STATES),
        _in_check("close_reason_known", "close_reason", VB_CLOSE_REASONS),
        CheckConstraint("quantity_entered > 0", name="quantity_positive"),
        CheckConstraint("quantity_open >= 0", name="open_non_negative"),
        CheckConstraint("quantity_open <= quantity_entered", name="open_within_entered"),
        CheckConstraint("stop_price >= initial_stop", name="stop_never_below_initial"),
        Index("ix_vb_position_book", "user_id", "state", "instrument_id"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_fk()
    broker_account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    instrument_id: Mapped[int] = _instrument_fk()
    entry_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    entry_avg: Mapped[Decimal] = mapped_column(PRICE_RAW, nullable=False)
    quantity_entered: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_open: Mapped[int] = mapped_column(Integer, nullable=False)
    initial_stop: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    #: The stop in force. Only ever rises — asserted here as well as in the code (``04`` §6.1).
    stop_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    gtt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gtt_trigger: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    gtt_armed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="OPEN")
    #: The session whose open this position is to be sold at, written by the evening when the
    #: close fell below the 21-day EMA. Null means hold (``04`` §6.2).
    exit_queued_for: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    exit_reason_queued: Mapped[str | None] = mapped_column(String(24), nullable=True)
    closed_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    exit_avg: Mapped[Decimal | None] = mapped_column(PRICE_RAW, nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)
    pnl_inr: Mapped[Decimal | None] = mapped_column(INR, nullable=True)
    return_pct: Mapped[Decimal | None] = mapped_column(MEASURE, nullable=True)
    r_multiple: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    hold_sessions: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    #: Which budget bound the size, so a page can say "capped by 1% of the name's turnover".
    size_cap: Mapped[str | None] = mapped_column(String(16), nullable=True)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    #: Sized at the first-live multiplier (``04`` §5.4). The journal's tag.
    half_risk: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class VbFill(Base):
    """One row per fill, so ``entry_avg`` and ``exit_avg`` are derivable and auditable."""

    __tablename__ = "vb_fill"
    __table_args__ = (
        _in_check("side_known", "side", VB_FILL_SIDES),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("price > 0", name="price_positive"),
        Index("ix_vb_fill_position", "position_id", "filled_at"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_fk()
    position_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("vb_position.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("vb_order.id", ondelete="SET NULL"), nullable=True
    )
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(PRICE_RAW, nullable=False)
    filled_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    journal_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[CreatedAt]


class VbPlan(Base):
    """A session's plan. The desk's lifecycle, unchanged: a ``plan_id`` and a 30-minute expiry."""

    __tablename__ = "vb_plan"
    __table_args__ = (
        _in_check("source_known", "source", VB_PLAN_SOURCES),
        _in_check("gate_known", "gate", VB_GATES),
        Index("ix_vb_plan_session", "user_id", "session_date", "built_at"),
    )

    id: Mapped[BigIntPk]
    plan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True)
    user_id: Mapped[int] = _user_fk()
    session_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    built_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: sha256 of the canonical lines — the same plan hashes the same (``04`` §9.3).
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    gate: Mapped[str] = mapped_column(String(8), nullable=False)
    sleeve_equity_inr: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    total_new_exposure_inr: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, server_default="0"
    )
    created_at: Mapped[CreatedAt]


class VbPlanLine(Base):
    """One confirmable line. Four kinds, and none of them is an opening sell (Track C §1)."""

    __tablename__ = "vb_plan_line"
    __table_args__ = (
        _in_check("kind_known", "kind", VB_LINE_KINDS),
        _in_check("state_known", "state", VB_LINE_STATES),
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        UniqueConstraint("plan_id", "instrument_id", "kind", name="uq_vb_plan_line_once"),
        Index("ix_vb_plan_line_plan", "plan_id", "kind"),
    )

    id: Mapped[BigIntPk]
    plan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("vb_plan.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = _user_fk()
    instrument_id: Mapped[int] = _instrument_fk()
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="PROPOSED")
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    limit_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    value_inr: Mapped[Decimal] = mapped_column(MONEY, nullable=False, server_default="0")
    size_cap: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(24), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    journal_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    order_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("vb_order.id", ondelete="SET NULL"), nullable=True
    )
    position_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("vb_position.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class VbPlanSkip(Base):
    """Every signal the rules passed over, with its reason.

    **A plan is not honest without them.**
    """

    __tablename__ = "vb_plan_skip"
    __table_args__ = (
        _in_check("reason_known", "reason", VB_SKIP_REASONS),
        Index("ix_vb_plan_skip_plan", "plan_id"),
    )

    id: Mapped[BigIntPk]
    plan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("vb_plan.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = _user_fk()
    instrument_id: Mapped[int] = _instrument_fk()
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[CreatedAt]


class VbSession(Base):
    """One row per session the sleeve ran, and the counter ``02`` §3.1 gates on."""

    __tablename__ = "vb_session"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "session_date", name="pk_vb_session"),
        _in_check("mode_known", "mode", VB_SESSION_MODES),
        _in_check("gate_known", "gate", VB_GATES),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    session_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, server_default="DRY_RUN")
    gate: Mapped[str] = mapped_column(String(8), nullable=False, server_default="SHUT")
    signals: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    orders_placed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    orders_expired: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    confirms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    fills: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    exits: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: Set once, so a re-run of the evening moves neither counter twice.
    first_live_counted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    counted_for_dry_run_gate: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    plan_ids: Mapped[JsonObject | None] = mapped_column(JSONB, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class VbBacktestRun(Base):
    """One row per run of the backtest. **Append-only.**

    A re-run inserts a new row and nothing edits a stored result, for the same reason
    ``vb_signal_daily`` is a snapshot: the number that was on the page when the flag was
    considered must survive a recalibration that produces a different one.
    """

    __tablename__ = "vb_backtest_run"
    __table_args__ = (
        _in_check("source_known", "source", VB_BACKTEST_SOURCES),
        Index("ix_vb_backtest_run_latest", "user_id", "source", "finished_at"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_fk()
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    #: Written on the way **in**, so a run that never finished still says what it was asked.
    params: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Set on success *and* on failure.
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stats: Mapped[JsonObject | None] = mapped_column(JSONB, nullable=True)
    #: The comparison against STRATEGY §4, and whether it is more than a CAGR point out (VB9).
    drift: Mapped[JsonObject | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[CreatedAt]
