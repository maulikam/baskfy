"""The three-weeks-tight sleeve's tables — ``docs/twt/03-data-model.md`` (TW3).

The ``tw_`` prefix separates this sleeve from the screener schema (``ohlcv_daily``,
``factor_daily``), the swing book (``sw_``), the volume-breakout sleeve (``vb_``), the curated
baskets (``cb_``) and the desk's journal. Nothing here replaces or alters an existing table.

THIRTEEN TABLES, ONE MIGRATION
------------------------------
``0041_twt`` creates all of them, for the reason ``0028_swing`` and ``0037_vbt`` gave: they are
one *thing*. A signal table with no plan, or a plan with no positions, is not a half-working
sleeve — it is a schema no code can use.

WHERE THE VOCABULARY COMES FROM, AND WHY IT IS SPELLED OUT HERE
---------------------------------------------------------------
The swing and VBT models import every state, kind and reason from their engine package, so the
database and the engine cannot disagree. **This module cannot, yet.** ``baskfy_core.twt`` is
TW1's and did not exist when TW3 was written; importing a module that is being authored in a
parallel session would make this schema unimportable for as long as that session ran, and TW3's
own brief forbids touching that tree. So the vocabulary is transcribed from ``docs/twt/03`` and
``04``, which is the specification both halves are written against, and
``packages/core/tests/test_twt_schema.py`` pins every tuple to the document (house rule 2: the
test asserts the spec, never current behaviour). DECISIONS-TW **TW3.1** records the choice and
its reversal: when ``baskfy_core.twt.config`` exists, replace each tuple with
``tuple(x.value for x in <Enum>)`` and keep the document test beside it.

EVERY TABLE CARRIES ``user_id``
-------------------------------
``docs/twt/02`` Track C §6 (P4.1). That includes the three tables ``03`` sketches with a
market-wide key — ``tw_state_daily`` and ``tw_signal_daily`` on ``(date, instrument_id)`` and
``tw_breadth_daily`` on ``(date)``. All three are **snapshots of what this user's system saw**,
and it is that user's settings that decide what may be acted on.

MONEY AND PRICES ARE ``numeric``
--------------------------------
House rule 9, and ``docs/twt/03``'s own conventions paragraph names the vocabulary it uses:
``PRICE`` (18,2) for prices and levels, ``PRICE_RAW`` (18,4) where an exchange print must
survive adjustment arithmetic, ``INR`` (12,2) for money, ``BREADTH`` (7,4) for the gate's
percentage, ``ADJ_FACTOR`` (18,10), ``BigIntPk``, ``CreatedAt``, JSONB for ``detail``. It does
not name ``MONEY``, so this sleeve's rupees are ``INR`` throughout — one money type, rather than
two that agree until the day one of them is widened. Rounding happens at write time (house rule
8) in the worker, not here.

FOUR CONSTRAINTS THAT ARE RULES RATHER THAN HYGIENE
---------------------------------------------------
``ck_tw_position_stop_never_below_initial`` is the floor a bug cannot get under: the stop only
ever rises (``04`` §7.2), and this is the database saying so a third time — the ratchet takes a
maximum, the desk refuses a raise below the resting trigger, and this constraint catches the
write that got past both. **A stop that quietly fell is a stop nobody notices until it does not
fire.**

``ck_tw_position_next_trigger_has_a_session`` refuses a ``next_trigger`` with no
``next_trigger_for``. ``04`` §11.3: a plan never reads a trigger computed for another session,
and a trigger that cannot say which session it is for cannot be checked against that rule.

``ck_tw_signal_daily_stop_below_entry`` is ``04`` §7.1's ``STOP_NOT_BELOW_ENTRY`` in the
database. It cannot arise at a 20 % stop and the check is here because a ceiling edit could make
it arise.

``uq_tw_order_one_per_signal`` is what makes the evening job idempotent (house rule 7): one entry
order per name per signal per side, so a re-run of the evening cannot double-place.

WHAT IS DELIBERATELY *NOT* CONSTRAINED
--------------------------------------
No threshold from ``docs/twt/04`` is written into a CHECK. ``week_range_pct <= 3.01``,
``month_low_ratio >= 1.3``, ``sessions_out_before >= 5`` and ``expires_at = built_at + 30 min``
are all true of the rows the shipped configuration produces, and all four are
``baskfy_core.twt.config`` fields: pinning one here would make a recalibration a migration, and
would put a number in schema history that ``test_twt_no_literals.py`` exists to keep out of the
source. The constraints below assert the *shapes* those numbers take (a range is non-negative, a
ratio is positive, an expiry is after its build) and leave the values to the config.
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
    ForeignKeyConstraint,
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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from baskfy_core.models.base import (
    ADJ_FACTOR,
    BREADTH,
    INR,
    PRICE,
    PRICE_RAW,
    Base,
    BigIntPk,
    CreatedAt,
    JsonObject,
    UpdatedAt,
)

#: The sleeve's percentage settings — ``max_position_pct``, ``stop_pct`` and ``trail_pct``
#: (``docs/twt/03`` §1 gives all three as ``numeric(5,2)``).
SLEEVE_PCT = Numeric(5, 2)
#: ``tw_state_daily``'s two derived ratios: the weekly range in per cent and the month-3 low
#: multiple. Four decimals, because ``04`` §3.1's band is 3.01 and a two-decimal store would
#: round 3.0149 to 3.01 and make an out-of-band row look in-band.
RATIO_4DP = Numeric(10, 4)
#: Per-position outcome percentages at close — ``return_pct`` and ``r_multiple``.
MEASURE = Numeric(10, 2)

#: ``tw_signal_daily.state`` — ``04`` §3.5 and ``03`` §3. ``SCAN_ONLY`` is an entry event the
#: liquidity floor rejected, and it is **stored**: a system that keeps only what it accepted
#: cannot show a person what it passed over.
TW_SIGNAL_STATES: tuple[str, ...] = ("SIGNAL", "SCAN_ONLY")

#: ``tw_signal_daily.failed_filters`` — the only filter that can reject an entry event is the
#: liquidity floor (``04`` §3.5). One member, and it is a tuple rather than a bare string so the
#: column's contents stay readable the way VBT-1's A-F list is.
TW_FAILED_FILTERS: tuple[str, ...] = ("TURNOVER",)

#: ``04`` §4.3: two values, not three. There is no amber and no exposure ladder — that is the
#: swing book's shape, and this book has ten equal slots.
TW_GATES: tuple[str, ...] = ("OPEN", "SHUT")

TW_POSITION_STATES: tuple[str, ...] = ("OPEN", "CLOSED")

#: ``tw_position.close_reason`` — ``03`` §5, and the list is short on purpose.
#:
#: **There is no ``EMA_EXIT`` and no ``TIME_EXIT``.** TWT-1 has exactly two exits and both are
#: the stop (``docs/twt/01`` §5): the 20 % disaster stop and the 20 % trail that ratchets.
#: ``STOP_DAY0`` is ``04`` §7.4's fill-day rule, ``NO_BAR`` is §7.5's write-off, and ``MANUAL``
#: is the one a person asked for. A reason that exists is a reason somebody writes code for, so
#: the 50-SMA exit — which is a *different strategy* with the same signal — has no name here.
TW_CLOSE_REASONS: tuple[str, ...] = (
    "STOP_HIT",
    "STOP_GAP",
    "STOP_DAY0",
    "NO_BAR",
    "MANUAL",
)

TW_FILL_SIDES: tuple[str, ...] = ("BUY", "SELL")

#: ``tw_order.side``. A ``SELL`` exists only for a ``MANUAL`` exit a person asked for; the
#: strategy's own exits are the resting GTT's and never an order this table holds (``03`` §6).
TW_ORDER_SIDES: tuple[str, ...] = ("BUY", "SELL")

#: ``tw_order.state`` — ``03`` §6. Shorter than VBT-1's by two: TWT-1 buys at the next open at
#: market, so there is no working order, no expiry sweep and therefore no ``EXPIRED``.
TW_ORDER_STATES: tuple[str, ...] = (
    "PROPOSED",
    "CONFIRMED",
    "SENT",
    "FILLED",
    "PARTIAL",
    "CANCELLED",
    "REJECTED",
)

#: ``tw_plan.source`` — the evening's plan from the closed session, the morning's rebuild before
#: the open (same signals, re-sized), or a rebuild a person asked the desk for. There is no
#: live-trigger source: this is an end-of-day strategy and nothing about it fires inside a
#: session (``02`` Track C §3).
TW_PLAN_SOURCES: tuple[str, ...] = ("EVENING", "MORNING", "MANUAL")

#: ``tw_plan_line.kind`` — ``03`` §7.
#:
#: ``SELL_AT_OPEN`` is **present in the schema and produced by nothing in TWT-1.** The strategy
#: has no end-of-day sell rule; the GTT is the exit. The kind exists so a person can be given a
#: line for a ``MANUAL`` exit without a migration, and TW10 asserts that no TWT rule ever emits
#: one — a check the swing and VBT packs did not need and this one does, because the shape it
#: copied has such a rule and copying shapes is how rules get imported by accident.
TW_LINE_KINDS: tuple[str, ...] = (
    "BUY_AT_OPEN",
    "ARM_GTT",
    "RAISE_GTT_STOP",
    "SELL_AT_OPEN",
)

TW_LINE_STATES: tuple[str, ...] = (
    "PROPOSED",
    "CONFIRMED",
    "SENT",
    "FILLED",
    "REJECTED",
    "EXPIRED",
    "SKIPPED",
)

#: ``tw_plan_skip.reason`` — ``04`` §10.1's ``SkipReason`` values, transcribed from ``03`` §7.
#: **A plan is not honest without them.**
TW_SKIP_REASONS: tuple[str, ...] = (
    "GATE_SHUT",
    "ALREADY_HELD",
    "SESSION_CAP",
    "SLOTS_FULL",
    "EXPOSURE_FULL",
    "BELOW_MIN_TRADE_VALUE",
    "TURNOVER_CAP",
    "LOCKED_UPPER_CIRCUIT",
    "NO_SLEEVE_CAPITAL",
    "NO_BAR",
    "BELOW_LIQUIDITY_FLOOR",
    "STOP_NOT_BELOW_ENTRY",
)

#: ``tw_session.mode``. ``DRY_RUN`` is **information, not a gate** (``02`` §3): Maulik decided on
#: 11 Sep 2026 that there is no paper phase, so ``DRY_RUN_SESSIONS_REQUIRED`` is 0 and this
#: counter exists because "this sleeve has rehearsed three sessions" is worth knowing after it
#: stops being a condition.
TW_SESSION_MODES: tuple[str, ...] = ("DRY_RUN", "LIVE")

#: ``tw_backtest_run.source`` — which bars produced the number. Kept apart on the page so a
#: reproduction of the research and a run over the plant can never be read as the same claim
#: (``05`` §4 shows both).
TW_BACKTEST_SOURCES: tuple[str, ...] = ("PLANT", "RESEARCH_EXPORT")

#: ``tw_scan_run.status`` — the swing book's four words, in the swing book's order (TW12).
#: Spelled the same across all three sleeves on purpose: a person reading "Scan now" on the swing
#: page and on this one is reading the same state machine, and a page that has to learn a second
#: vocabulary per sleeve is a page that will eventually render the wrong one.
TW_SCAN_STATUSES: tuple[str, ...] = ("QUEUED", "RUNNING", "DONE", "FAILED")

#: ``tw_scan_run.source`` — who pressed it. ``desk`` is the operator console, ``web`` the ``/twt``
#: hub, ``cli`` a hand-run. ``vb_scan_run`` admits only ``desk`` and ``cli`` because VBT-1 has no
#: web button; this sleeve is getting one, so ``web`` is named here rather than smuggled in as
#: ``desk`` — a row that cannot say where a request came from is a row that cannot be audited.
TW_SCAN_SOURCES: tuple[str, ...] = ("desk", "web", "cli")

#: ``docs/twt/03`` §7 and ``04`` §10.4: the desk's own plan lifetime, restated for this surface.
#: Named here because both the writer (the evening job) and the reader (``/twt/execute``) need
#: it and neither may invent its own number. Prefixed, because the swing book exports its own
#: ``PLAN_TTL_MINUTES`` and VBT-1 its own ``VB_PLAN_TTL_MINUTES``: two sleeves agreeing on a
#: number today is not the same as one number.
TW_PLAN_TTL_MINUTES: int = 30


def _in_check(name: str, column: str, values: tuple[str, ...]) -> CheckConstraint:
    quoted = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({quoted})", name=name)


def _user_fk() -> Mapped[int]:
    """``user_id``, non-null, cascading. Every ``tw_`` table has one (Track C §6)."""
    return mapped_column(BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False)


def _instrument_fk() -> Mapped[int]:
    return mapped_column(
        BigInteger, ForeignKey("instrument.id", ondelete="CASCADE"), nullable=False
    )


def _pipeline_run_fk() -> Mapped[int | None]:
    """Which nightly run produced the row — provenance, as ``screen_run`` has (``03`` §2)."""
    return mapped_column(
        BigInteger, ForeignKey("pipeline_run.id", ondelete="SET NULL"), nullable=True
    )


class TwConfig(Base):
    """One row per user: the sleeve's capital and the five numbers that are settings.

    Everything **not** here is a ``baskfy_core.twt.config`` default, and changing one is a code
    change with a ``DECISIONS-TW.md`` entry (``02`` §4). The reason is behavioural rather than
    technical: a threshold that can be changed in a form gets changed after a bad week, which is
    the failure mode a rule-based sleeve exists to prevent.

    ``sleeve_capital_inr`` defaults to **0**, and that is this table's safety property. ``04``
    §9.3: a sleeve at ₹0 plans nothing — every signal is skipped ``NO_SLEEVE_CAPITAL``. So a
    freshly seeded system detects, ranks, stores and plans **nothing to buy** until Maulik enters
    the capital himself on the first live morning. No agent sets this number; the root
    ``CLAUDE.md`` safety rails and ``02`` §3 both say so by name.

    ``trail_pct`` is the odd one and it is odd on purpose: it is bounded **below**, by
    ``BASKFY_TWT_TRAIL_PCT_MIN`` [18.00], not above. The trail is TWT-1's only exit and the
    measured cliff is in the tightening direction — 20 % → 15 % took the research's CAGR from
    20.9 % to 9.6 % and its drawdown from -24.7 % to -43 %. Widening it is merely unprofitable;
    tightening it is the failure mode. DECISIONS-TW **TW0.5**.

    ``first_live_entries_left`` and ``dry_run_sessions`` are the evening job's, **never a
    form's**. The first counts **entries, not sessions** (``04`` §6.4) and is decremented once
    per *filled* entry by the session that filled it; a person who could set it could delete the
    half-size discipline. The second is information (``02`` §3).
    """

    __tablename__ = "tw_config"
    __table_args__ = (
        CheckConstraint("sleeve_capital_inr >= 0", name="capital_non_negative"),
        CheckConstraint("max_open_positions > 0", name="max_positions_positive"),
        CheckConstraint("max_position_pct > 0", name="position_pct_positive"),
        CheckConstraint("stop_pct > 0", name="stop_pct_positive"),
        CheckConstraint("trail_pct > 0", name="trail_pct_positive"),
        CheckConstraint("first_live_entries_left >= 0", name="first_live_non_negative"),
        CheckConstraint("dry_run_sessions >= 0", name="dry_run_sessions_non_negative"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True
    )
    #: The cash this sleeve may deploy. Its own money, never the whole account (``04`` §9.3).
    #: **Seeded at 0 and never set by anything in this repository.**
    sleeve_capital_inr: Mapped[Decimal] = mapped_column(INR, nullable=False, server_default="0")
    #: ``SizingConfig.max_slots`` [10] is the strategy's own number; the plan takes the smaller
    #: of that and this setting, so a value above ten is a form that lies rather than a wider
    #: book. Ceiling ``BASKFY_TWT_MAX_OPEN_POSITIONS_MAX`` [15].
    max_open_positions: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="10"
    )
    #: ``04`` §6.2's per-position ceiling, above the slot size so it binds only when equity has
    #: drifted. Ceiling ``BASKFY_TWT_MAX_POSITION_PCT_MAX`` [15.00].
    max_position_pct: Mapped[Decimal] = mapped_column(
        SLEEVE_PCT, nullable=False, server_default="12.50"
    )
    #: The disaster stop. A setting because ``01`` §7 measured 15-30 % and found the band flat.
    #: Ceiling ``BASKFY_TWT_STOP_PCT_MAX`` [25.00].
    stop_pct: Mapped[Decimal] = mapped_column(SLEEVE_PCT, nullable=False, server_default="20.00")
    #: The ratchet's width. **Floor** ``BASKFY_TWT_TRAIL_PCT_MIN`` [18.00] — see the class
    #: docstring and DECISIONS-TW TW0.5. A ceiling here would refuse a setting that is merely
    #: conservative; the direction that breaks the strategy is down.
    trail_pct: Mapped[Decimal] = mapped_column(SLEEVE_PCT, nullable=False, server_default="20.00")
    #: ``SizingConfig.first_live_entries`` [10], counting down once per **filled** entry once
    #: execution is enabled (``04`` §6.4).
    first_live_entries_left: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="10"
    )
    #: How many ``DRY_RUN`` sessions have closed with a plan built and at least one line
    #: simulated. Information, not a gate (``02`` §3).
    dry_run_sessions: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    updated_at: Mapped[UpdatedAt]
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class TwConfigAudit(Base):
    """What a setting **was** on the evening a stop was armed.

    ``tw_config.updated_at`` says who touched the row last; it cannot answer "what was
    ``trail_pct`` on the morning that stop was armed", and on a book whose lines are held for
    months that is the question somebody asks. One row per field changed, written in the same
    transaction as the change — the shape ``vb_config_audit``, ``sw_config_audit`` and the desk's
    own ``settings_audit`` already use (``03`` §1b).
    """

    __tablename__ = "tw_config_audit"
    __table_args__ = (Index("ix_tw_config_audit_user_time", "user_id", "changed_at"),)

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_fk()
    #: The column that changed.
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_at: Mapped[CreatedAt]
    #: A user id as text, or a job name for the fields a job owns (``twt-evening``).
    changed_by: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class TwStateDaily(Base):
    """The scan's **state**, per session — about fifty names a day (``03`` §2).

    **Snapshotted, never recomputed for a past date**, the same rule as ``market_health_daily``,
    ``sw_setup_daily`` and ``vb_signal_daily``: a recalibration must not rewrite the record of
    what the system saw.

    This table exists separately from :class:`TwSignalDaily` because TWT-1's scan is a *state*
    and its signal is the **first day** of one. Storing only the signals would make "how long has
    this been tight" and "which names left the state today" unanswerable, and both are on the
    page (``05`` §2).

    ``week_close_0/1/2`` are stored because the one rule anybody will dispute is that one —
    ``04`` §3.2's three weekly closes, of which ``w₀`` is *today's* close read as the current
    week's partial candle. Keeping the three numbers means a dispute is settled by reading a row
    rather than by re-running a detector against a series that has since been re-adjusted.
    """

    __tablename__ = "tw_state_daily"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "date", "instrument_id", name="pk_tw_state_daily"),
        CheckConstraint("close > 0 AND close_raw > 0", name="close_positive"),
        CheckConstraint("week_range_pct >= 0", name="week_range_non_negative"),
        CheckConstraint("month_low_3 > 0 AND month_low_ratio > 0", name="month_low_positive"),
        CheckConstraint("sessions_in_state > 0", name="sessions_in_state_positive"),
        CheckConstraint("bars_in_window IS NULL OR bars_in_window >= 0", name="bars_non_negative"),
        CheckConstraint("volume >= 0 AND vol_sma_50 >= 0", name="volume_non_negative"),
        Index("ix_tw_state_daily_session", "date", "instrument_id"),
        Index("ix_tw_state_daily_instrument", "instrument_id", "date"),
    )

    user_id: Mapped[int] = _user_fk()
    #: A **closed** session (``04`` §11.1). There is no "today" bar until today ends.
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    instrument_id: Mapped[int] = _instrument_fk()

    #: The bar as stored: ``close`` adjusted, ``close_raw`` the exchange print. ``open``,
    #: ``high`` and ``low`` are nullable because ``ohlcv_daily`` admits a bar without them and a
    #: state row must not be refused for a field the state's own predicate never reads.
    open: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    high: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    low: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    close: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    close_raw: Mapped[Decimal] = mapped_column(PRICE_RAW, nullable=False)
    #: The row's factor, so a later split is recognisable and a level can be turned back into an
    #: exchange price (``03`` §10).
    adj_factor: Mapped[Decimal] = mapped_column(ADJ_FACTOR, nullable=False, server_default="1")

    #: ``04`` §3.2's ``W``: today's adjusted close, then the last close of each of the two
    #: preceding weeks the calendar holds. Non-null, because the tight test is evaluated only
    #: when all three are finite and a row is here only if it held.
    week_close_0: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    week_close_1: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    week_close_2: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    #: ``(max(W) / min(W) - 1) x 100`` — the tightness itself. ``≤ tight_band_pct`` [3.01] for a
    #: state row; the band is config and is deliberately not a CHECK (see the module docstring).
    week_range_pct: Mapped[Decimal] = mapped_column(RATIO_4DP, nullable=False)

    #: ``04`` §3.3: the minimum adjusted low over the calendar month three months back, and
    #: ``close / month_low_3``. ``≥ month_low_multiple`` [1.3] for a state row.
    month_low_3: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    month_low_ratio: Mapped[Decimal] = mapped_column(RATIO_4DP, nullable=False)

    #: ``04`` §3.1 line 5's input and the day's own volume.
    vol_sma_50: Mapped[int] = mapped_column(BigInteger, nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: The day's rupee turnover (``close_raw x volume``) and the liquidity floor's 20-session
    #: mean. The average is nullable: ``04`` §2.2's tolerance can leave it unmeasured on a name
    #: that has only just started trading, and a state row is not conditional on it.
    turnover_inr: Mapped[int] = mapped_column(BigInteger, nullable=False)
    turnover_avg_20: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    #: The 200-session mean close, so the breadth funnel is auditable from the same rows.
    #: Nullable: a name can hold the tight state while its 200-day average is still warming up,
    #: and ``04`` §4.2 puts such a name in neither side of the breadth reading.
    sma_dma: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)

    #: How many consecutive sessions including this one the state has held; ``1`` on an entry
    #: day. This is the column that makes "how long has this been tight" a read.
    sessions_in_state: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    #: How many bars the 200-session window actually held, so ``04`` §2.2's 10 % tolerance is
    #: auditable rather than asserted.
    bars_in_window: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    #: ``upper_circuit > 0 and high >= upper_circuit``. Kept and flagged, never dropped — the
    #: plan is what skips it (``04`` §5.2).
    locked_upper_circuit: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    pipeline_run_id: Mapped[int | None] = _pipeline_run_fk()
    created_at: Mapped[CreatedAt]


class TwSignalDaily(Base):
    """The entry **events**, per session — a subset of :class:`TwStateDaily`'s rows (``03`` §3).

    The state is true today and was false on each of the previous ``entry_min_sessions_out`` [5]
    sessions (``04`` §3.4). Written in the same transaction as the state rows.

    A row is written for **every** entry event, including one the liquidity floor rejects
    (``state = 'SCAN_ONLY'``, ``failed_filters = {TURNOVER}``), so the funnel on the page is the
    funnel the strategy applied. VBT-1 keeps its rejects for the same reason: a system that
    stores only what it accepted cannot show a person what it passed over.

    ``entry_reference_close`` is **not an entry level.** TWT-1 buys the next open at market; the
    number is here so the morning's fill can be read against the close that produced the signal.

    ``rank_key`` is the **signal session's own turnover**, ``close_raw x volume`` of that day —
    not the 20-session average the research note's prose names. The code that produced every
    number in ``01`` §6 ranks by the day's own turnover, and the numbers are the fact while the
    prose is the stale half. DECISIONS-TW **TW0.2**; ``turnover_avg_20`` is stored beside it so
    a re-ranking would need no migration and no re-detection.
    """

    __tablename__ = "tw_signal_daily"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "date", "instrument_id", name="pk_tw_signal_daily"),
        _in_check("state_known", "state", TW_SIGNAL_STATES),
        CheckConstraint("entry_reference_close > 0", name="entry_reference_positive"),
        CheckConstraint(
            "stop_preview > 0 AND stop_preview < entry_reference_close",
            name="stop_below_entry",
        ),
        CheckConstraint("sessions_out_before >= 0", name="sessions_out_non_negative"),
        Index("ix_tw_signal_daily_rank", "date", "state", text("rank_key DESC")),
        Index("ix_tw_signal_daily_instrument", "instrument_id", "date"),
    )

    user_id: Mapped[int] = _user_fk()
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    instrument_id: Mapped[int] = _instrument_fk()
    #: ``SIGNAL`` (the event held and the name cleared ``min_turnover_inr``) or ``SCAN_ONLY``
    #: (the event held, the liquidity floor did not).
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    #: ``{TURNOVER}`` for a ``SCAN_ONLY`` row, empty for a ``SIGNAL``. The page reads it verbatim.
    failed_filters: Mapped[list[str]] = mapped_column(
        ARRAY(String(16)), nullable=False, server_default="{}"
    )

    #: The signal session's close, as an exchange price.
    entry_reference_close: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    #: ``entry_reference_close x (1 - stop_pct/100)``, snapped down to the tick — **a preview**.
    #: The real stop is set from the fill (``04`` §7.1).
    stop_preview: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    #: How many sessions the state had been false. ``>= entry_min_sessions_out`` [5] by
    #: construction; a larger number says how cold the name was.
    sessions_out_before: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    #: What a same-session tie is broken by: the signal session's own rupee turnover.
    rank_key: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    #: The liquidity floor's input, repeated here so the skip is readable without a join.
    turnover_avg_20: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pipeline_run_id: Mapped[int | None] = _pipeline_run_fk()
    created_at: Mapped[CreatedAt]


class TwBreadthDaily(Base):
    """The gate's reading, per session (``03`` §4). Same job, same transaction as §2 and §3.

    **Why this table exists when ``vb_breadth_daily`` holds the same measurement.** It is the
    same reading over the same universe at the same threshold, and ``baskfy_core.twt.breadth``
    calls VBT-1's implementation so there is exactly **one** arithmetic (``04`` §4.4). What is
    not shared is the *row*: a sleeve whose gate is stored in another sleeve's table stops having
    a gate the day that sleeve's nightly flag is set false, and the two thresholds are
    independent numbers that happen to agree today. DECISIONS-TW **TW0.4**.

    Not to be confused with ``market_health_daily.pct_above_200dma`` either: that is computed
    over an **index's** point-in-time membership, a different measurement of a different
    population, and reusing it would change the gate that produced every number in ``01`` §6.

    The gate a plan reads is the row of the **signal session**, never a later one (``04`` §11).
    """

    __tablename__ = "tw_breadth_daily"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "date", name="pk_tw_breadth_daily"),
        _in_check("gate_known", "gate", TW_GATES),
        CheckConstraint("above_count <= measured_count", name="above_within_measured"),
        CheckConstraint("measured_count <= universe_count", name="measured_within_universe"),
        # A session dropped from the calendar never opens the gate (``04`` §2.1): the strategy
        # does not trade a muhurat session, and a row that said OPEN would let a later reader
        # believe it might have.
        CheckConstraint("NOT thin_session OR gate = 'SHUT'", name="thin_session_is_shut"),
        # And a session that *was* measured must carry its percentage: ``03`` §4 allows a null
        # only on the thin-session row written for the record.
        CheckConstraint(
            "thin_session OR pct_above_dma IS NOT NULL", name="measured_session_has_a_percentage"
        ),
    )

    user_id: Mapped[int] = _user_fk()
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    #: Names in the scan universe with a bar on the date.
    universe_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Of those, the ones with a valid 200-DMA under ``04`` §2.2's tolerance — the denominator.
    #: A name whose average is still warming up is in neither count: counting it as "not above"
    #: would report a listing wave as a bear market.
    measured_count: Mapped[int] = mapped_column(Integer, nullable=False)
    above_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: ``above_count / measured_count x 100``, four decimals. Null only on a thin session.
    #: ``measured_count = 0`` gives ``0.0000`` and a SHUT gate (``04`` §4.2).
    pct_above_dma: Mapped[Decimal | None] = mapped_column(BREADTH, nullable=True)
    #: ``OPEN`` when ``pct_above_dma > min_pct_above_dma`` [40.0] strictly, else ``SHUT``.
    gate: Mapped[str] = mapped_column(String(8), nullable=False)
    #: 200 — stored so a recalibration of the window is visible in the history rather than
    #: implied.
    dma_bars: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="200")
    #: This date was dropped from the rolling calendar (``04`` §2.1).
    thin_session: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    #: The funnel: universe → with a bar → with a 200-DMA → above it → in state → entries →
    #: signals, and ``dropped_thin_sessions`` in the window.
    detail: Mapped[JsonObject | None] = mapped_column(JSONB, nullable=True)
    pipeline_run_id: Mapped[int | None] = _pipeline_run_fk()
    created_at: Mapped[CreatedAt]


class TwPosition(Base):
    """The book. One row per **entry**, never per symbol (``03`` §5).

    A second entry into a name after an exit is a second row. This table is the sleeve's source
    of truth for what it owns, and Track C §5 turns that into a rule: **the sleeve never sells a
    holding it did not buy.** A name in the broker's account that is not here is the weekly
    book's, the swing book's, VBT-1's or Maulik's, and this sleeve cannot see it.

    **Why ``high_since`` is a column and not a query.** It could be recomputed from
    ``ohlcv_daily`` every evening, and TW4 asserts that it agrees with such a recomputation. It
    is stored because the stop resting at the exchange was derived from a specific number on a
    specific evening, and when a corporate action rewrites the adjusted series underneath a
    600-session hold, "what was this stop derived from" must still have an answer. It is
    ``PRICE_RAW`` for the same reason: the exchange trades in exchange prices.

    ``entry_adj_factor`` is here although ``03`` §5's column table does not list it:
    ``04`` §7.3 and DECISIONS-TW **TW0.7** both read it by name — the evening job detects a
    corporate action by comparing the as-of row's factor with *the position's*, and a rule that
    needs a column the schema does not have is a rule that needs a migration on the morning of a
    split. DECISIONS-TW **TW3.2**.
    """

    __tablename__ = "tw_position"
    __table_args__ = (
        _in_check("state_known", "state", TW_POSITION_STATES),
        _in_check("close_reason_known", "close_reason", TW_CLOSE_REASONS),
        CheckConstraint("quantity_entered > 0", name="quantity_positive"),
        CheckConstraint("quantity_open >= 0", name="open_non_negative"),
        CheckConstraint("quantity_open <= quantity_entered", name="open_within_entered"),
        CheckConstraint("entry_avg > 0 AND initial_stop > 0", name="entry_and_stop_positive"),
        CheckConstraint("initial_stop < entry_avg", name="initial_stop_below_entry"),
        # THE RULE WHOSE VIOLATION IS SILENT. ``04`` §7.2: a stop never falls. The ratchet takes
        # a maximum, the desk refuses a raise at or below the resting trigger, and this is the
        # third place — the one that catches the write which got past the other two.
        CheckConstraint("stop_price >= initial_stop", name="stop_never_below_initial"),
        CheckConstraint("high_since > 0", name="high_since_positive"),
        # ``04`` §11.3: a plan never reads a trigger computed for another session. A trigger
        # that cannot say which session it was computed for cannot be checked against that rule.
        CheckConstraint(
            "next_trigger IS NULL OR next_trigger_for IS NOT NULL",
            name="next_trigger_has_a_session",
        ),
        Index("ix_tw_position_book", "user_id", "state", "instrument_id"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_fk()
    broker_account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    instrument_id: Mapped[int] = _instrument_fk()
    #: The ``tw_order`` that filled it. ``use_alter`` because ``tw_order.position_id`` points
    #: back: the two tables are genuinely mutually referential, and the migration adds this
    #: constraint after both exist.
    order_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("tw_order.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    #: The session whose close produced the signal.
    signal_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    entry_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    entry_avg: Mapped[Decimal] = mapped_column(PRICE_RAW, nullable=False)
    quantity_entered: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The factor the entry bar carried. ``04`` §7.3 compares it with the as-of row's to detect
    #: a split under a hold.
    entry_adj_factor: Mapped[Decimal] = mapped_column(
        ADJ_FACTOR, nullable=False, server_default="1"
    )

    #: 20 % under the fill, what it was at entry — the denominator of R.
    initial_stop: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    #: The stop in force. **Only ever rises** (``04`` §7.2).
    stop_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    #: The highest high since entry, the exchange print. The ratchet's only input besides
    #: ``trail_pct``. Initialised to the fill price and raised after every close.
    high_since: Mapped[Decimal] = mapped_column(PRICE_RAW, nullable=False)
    #: The session that set it, so a page can say "the stop has not moved in 40 sessions".
    high_since_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    gtt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    gtt_trigger: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    gtt_armed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: **Tomorrow's trailing trigger**, computed by the evening job (``04`` §7.2); null when it
    #: does not exceed ``gtt_trigger``. This is what makes the morning's ratchet line arithmetic
    #: that has already been done.
    next_trigger: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    #: The session ``next_trigger`` was computed for.
    next_trigger_for: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    quantity_open: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="OPEN")
    closed_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    exit_avg: Mapped[Decimal | None] = mapped_column(PRICE_RAW, nullable=True)
    #: One of five. **There is no ``EMA_EXIT`` and no ``TIME_EXIT``** — see
    #: :data:`TW_CLOSE_REASONS`.
    close_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)

    pnl_inr: Mapped[Decimal | None] = mapped_column(INR, nullable=True)
    return_pct: Mapped[Decimal | None] = mapped_column(MEASURE, nullable=True)
    r_multiple: Mapped[Decimal | None] = mapped_column(MEASURE, nullable=True)
    hold_sessions: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)

    #: True for every ``DRY_RUN`` / flag-off fill. The pages label them and the backtest card
    #: never mixes them.
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    #: Sized at ``risk_multiplier_first_live`` [0.5] x the slot (``04`` §6.4). Records which
    #: lines the first-live countdown applied to.
    half_size: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class TwOrder(Base):
    """One entry order (``03`` §6).

    Simpler than VBT-1's ``vb_order``: TWT-1 buys **at the next open, at market**, so there is no
    working order, no expiry sweep and no ``sessions_worked``. An order is proposed by an evening
    plan, confirmed by a person the next morning, and either fills or does not.

    ``signal_date`` is a foreign key into :class:`TwSignalDaily`, which is what makes the order
    answerable: every order points at the stored row that produced it. **A consequence worth
    naming for TW4:** the nightly detection must *upsert* its signal rows, never delete and
    re-insert them, or a re-detection would be refused by an order that already references one.
    That is what house rule 7 asks for anyway.

    ``uq_tw_order_one_per_signal`` is what makes the evening idempotent: one entry order per name
    per signal per side, so a re-run cannot double-place. ``client_id = plan_id:symbol`` is the
    gateway's own idempotency key on top of it (non-negotiable 6).
    """

    __tablename__ = "tw_order"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "signal_date", "instrument_id", "side", name="uq_tw_order_one_per_signal"
        ),
        ForeignKeyConstraint(
            ["user_id", "signal_date", "instrument_id"],
            ["tw_signal_daily.user_id", "tw_signal_daily.date", "tw_signal_daily.instrument_id"],
            name="fk_tw_order_signal",
        ),
        _in_check("side_known", "side", TW_ORDER_SIDES),
        _in_check("state_known", "state", TW_ORDER_STATES),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("stop_price > 0", name="stop_positive"),
        CheckConstraint("filled_quantity >= 0", name="filled_non_negative"),
        CheckConstraint("filled_quantity <= quantity", name="filled_within_quantity"),
        Index("ix_tw_order_state", "user_id", "state"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_fk()
    broker_account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    instrument_id: Mapped[int] = _instrument_fk()
    #: The session whose close produced the signal.
    signal_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    #: ``BUY`` or ``SELL``. A ``SELL`` exists only for a ``MANUAL`` exit a person asked for.
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The stop this order's fill will be given (``04`` §7.1).
    stop_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="PROPOSED")
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: ``plan_id:symbol`` — the gateway's idempotency key, so a re-posted plan cannot double-send.
    client_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    filled_quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    avg_fill_price: Mapped[Decimal | None] = mapped_column(PRICE_RAW, nullable=True)
    position_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tw_position.id", ondelete="SET NULL"), nullable=True
    )
    #: True for every ``DRY_RUN`` / flag-off order.
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    #: The skip context, the sizing cap that bound, the gateway's answer.
    detail: Mapped[JsonObject | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class TwFill(Base):
    """One row per fill, so ``entry_avg`` and ``exit_avg`` are derivable and auditable."""

    __tablename__ = "tw_fill"
    __table_args__ = (
        _in_check("side_known", "side", TW_FILL_SIDES),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("price > 0", name="price_positive"),
        Index("ix_tw_fill_position", "position_id", "filled_at"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_fk()
    position_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tw_position.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tw_order.id", ondelete="SET NULL"), nullable=True
    )
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(PRICE_RAW, nullable=False)
    filled_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    journal_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[CreatedAt]


class TwPlan(Base):
    """A session's plan (``03`` §7). The desk's lifecycle, unchanged.

    A ``plan_id`` and a **30-minute expiry** (:data:`TW_PLAN_TTL_MINUTES`): orders fire only from
    ``POST /twt/execute`` with ``confirm=true`` and an unexpired ``plan_id`` issued by the plan
    that showed the line (non-negotiable 1). ``plan_hash`` is the sha256 of the canonical lines,
    so the same plan hashes the same (``04`` §10.3).
    """

    __tablename__ = "tw_plan"
    __table_args__ = (
        _in_check("source_known", "source", TW_PLAN_SOURCES),
        _in_check("gate_known", "gate", TW_GATES),
        # The TTL itself is config (:data:`TW_PLAN_TTL_MINUTES`); what the database refuses is a
        # plan that expired before it was built, which is a clock bug rather than a setting.
        CheckConstraint("expires_at > built_at", name="expiry_after_build"),
        CheckConstraint("sleeve_equity_inr >= 0", name="equity_non_negative"),
        CheckConstraint("total_new_exposure_inr >= 0", name="exposure_non_negative"),
        Index("ix_tw_plan_session", "user_id", "session_date", "built_at"),
    )

    id: Mapped[BigIntPk]
    plan_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True)
    user_id: Mapped[int] = _user_fk()
    #: The **closed** session the plan reads. A plan rebuilt in the morning reads the same one:
    #: it re-sizes against the sleeve's equity, it does not re-detect (``04`` §11.3).
    session_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    built_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    gate: Mapped[str] = mapped_column(String(8), nullable=False)
    sleeve_equity_inr: Mapped[Decimal] = mapped_column(INR, nullable=False)
    total_new_exposure_inr: Mapped[Decimal] = mapped_column(INR, nullable=False, server_default="0")
    created_at: Mapped[CreatedAt]


class TwPlanLine(Base):
    """One confirmable line (``03`` §7). Four kinds, and one of them is produced by nothing.

    ``BUY_AT_OPEN`` carries the size, the value and a preview of the stop the fill will be given.
    ``ARM_GTT`` is non-negotiable 4's same-session stop and the 15:15 sweep's re-arm.
    ``RAISE_GTT_STOP`` is **the ratchet** — ``stop_price`` is the new trigger, and the line
    carries ``high_since`` and ``previous_trigger`` so the page can show the move.

    ``SELL_AT_OPEN`` exists so a person can be given a line for a ``MANUAL`` exit without a
    migration, and **no TWT rule ever emits one** (TW10 asserts it). The kind is a hazard the
    schema accepts deliberately, because the shape this table copied has such a rule and copying
    shapes is how rules get imported by accident.
    """

    __tablename__ = "tw_plan_line"
    __table_args__ = (
        _in_check("kind_known", "kind", TW_LINE_KINDS),
        _in_check("state_known", "state", TW_LINE_STATES),
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        CheckConstraint("value_inr >= 0", name="value_non_negative"),
        UniqueConstraint("plan_id", "instrument_id", "kind", name="uq_tw_plan_line_once"),
        Index("ix_tw_plan_line_plan", "plan_id", "kind"),
    )

    id: Mapped[BigIntPk]
    plan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tw_plan.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = _user_fk()
    instrument_id: Mapped[int] = _instrument_fk()
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="PROPOSED")
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: A ``BUY_AT_OPEN``'s preview of the 20 % stop; a ``RAISE_GTT_STOP``'s **new trigger**.
    stop_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    value_inr: Mapped[Decimal] = mapped_column(INR, nullable=False, server_default="0")
    #: The ratchet's two explanatory numbers (``03`` §7): the high the new trigger was derived
    #: from, and the trigger resting at the exchange before it.
    high_since: Mapped[Decimal | None] = mapped_column(PRICE_RAW, nullable=True)
    previous_trigger: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    #: Names the cap that bound (``04`` §6.2), or the adjustment that produced the raise
    #: (``04`` §7.3). Prose, because what a person needs here is a sentence.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: ``plan_id:symbol:kind`` — the gateway's idempotency key for a GTT leg.
    client_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    journal_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    order_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tw_order.id", ondelete="SET NULL"), nullable=True
    )
    position_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tw_position.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class TwPlanSkip(Base):
    """Every signal the rules passed over, with its reason (``03`` §7).

    **A plan is not honest without them.** ``reason`` is check-constrained to ``04`` §10.1's
    ``SkipReason`` values, so a reason the engine can emit and the database would reject is an
    outage at 21:00 on a Tuesday rather than a surprise in production.
    """

    __tablename__ = "tw_plan_skip"
    __table_args__ = (
        _in_check("reason_known", "reason", TW_SKIP_REASONS),
        Index("ix_tw_plan_skip_plan", "plan_id"),
    )

    id: Mapped[BigIntPk]
    plan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tw_plan.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = _user_fk()
    instrument_id: Mapped[int] = _instrument_fk()
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[CreatedAt]


class TwSession(Base):
    """One row per session the system ran (``03`` §8).

    ``ratchets`` is its own column because it is the number that says whether the sleeve's one
    new mechanism actually ran: a book of ten open lines in a rising market should ratchet most
    nights, and **a week of zeroes with the market up is a bug, not a quiet spell.**

    ``counted_for_dry_run`` is set once, so a re-run of the evening does not inflate the counter
    (house rule 7). ``first_live_entries_counted`` records how many of
    ``tw_config.first_live_entries_left`` this session consumed — the countdown moves once per
    **filled** entry, by the session that filled it, never by a request (``04`` §6.4).
    """

    __tablename__ = "tw_session"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "session_date", name="pk_tw_session"),
        _in_check("mode_known", "mode", TW_SESSION_MODES),
        _in_check("gate_known", "gate", TW_GATES),
        CheckConstraint(
            "states >= 0 AND signals >= 0 AND orders_placed >= 0 AND confirms >= 0 "
            "AND fills >= 0 AND ratchets >= 0 AND exits >= 0 AND naked_at_1515 >= 0 "
            "AND first_live_entries_counted >= 0",
            name="counters_non_negative",
        ),
    )

    user_id: Mapped[int] = _user_fk()
    session_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, server_default="DRY_RUN")
    gate: Mapped[str] = mapped_column(String(8), nullable=False, server_default="SHUT")
    #: How many names held the tight state, and how many of those were entry events.
    states: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    signals: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    orders_placed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    confirms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    fills: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: The one that says the ratchet ran. See the class docstring.
    ratchets: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    exits: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: Open lines with no resting GTT at the 15:15 sweep (``04`` §7.4, TW7). A number that is
    #: not zero is the alert ``TWT_POSITION_NAKED``.
    naked_at_1515: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    first_live_entries_counted: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0"
    )
    counted_for_dry_run: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    plan_ids: Mapped[JsonObject | None] = mapped_column(JSONB, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class TwBacktestRun(Base):
    """One row per run of the backtest (TW9, ``03`` §9). **Append-only.**

    A re-run inserts a new row and nothing edits a stored result, exactly as ``sw_backtest_run``
    and ``vb_backtest_run`` are: the number that was on the page when the flag was considered
    must survive a recalibration that produces a different one.

    The run is sized against ``params.sleeve_inr``, **never** ``tw_config.sleeve_capital_inr``
    (Track C §6): a backtest that read the live sleeve's capital would change its own history
    the day a person funded the book.

    ``ix_tw_backtest_run_latest`` serves the page's one query — this user's latest **finished**
    run per source. The latest *finished*, not the latest started, so a run in flight or a failed
    re-run never displaces the last good number.
    """

    __tablename__ = "tw_backtest_run"
    __table_args__ = (
        _in_check("source_known", "source", TW_BACKTEST_SOURCES),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at", name="finished_after_started"
        ),
        Index("ix_tw_backtest_run_latest", "user_id", "source", "finished_at"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_fk()
    #: ``PLANT`` (the run's own bars) or ``RESEARCH_EXPORT`` (TW2's reproduction against the
    #: research panel) — so the two can never be confused on the page.
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    #: Written on the way **in**, so a run that never finished still says what it was asked.
    params: Mapped[JsonObject] = mapped_column(JSONB, nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Set on success *and* on failure.
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Trades, the yearly and monthly tables, the equity curve, the funnel, the gate-on/gate-off
    #: comparison, and every price a string of its exact decimal. Null until the run finishes,
    #: forever if it failed.
    stats: Mapped[JsonObject | None] = mapped_column(JSONB, nullable=True)
    #: The comparison against ``01`` §6's published numbers, and ``flagged`` when the CAGR is
    #: more than a point out (TW9).
    drift: Mapped[JsonObject | None] = mapped_column(JSONB, nullable=True)
    #: ``"{ExceptionType}: {message}"`` and the traceback; the exception is re-raised after it is
    #: recorded.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[CreatedAt]


class TwScanRun(Base):
    """One press of **Scan now** on the three-weeks-tight page (TW12; ``docs/twt/03`` §13's shape).

    The sleeve had no such table. The swing book has ``sw_scan_run`` (SW15) and VBT-1 has
    ``vb_scan_run`` (VB12); this is the third of the same row, and it is deliberately the
    **narrower** of the two shapes.

    **There is no ``provisional`` column, and that is the design.** ``sw_scan_run`` carries one
    because the swing book's setups can be read off a bar still being formed — a base and a pivot
    are visible at 13:42. This strategy's signal is not: ``04`` §2 measures three *weekly* ranges
    that have closed, against a monthly low, with a sessions-out count over closed sessions. A bar
    built from a live quote would change the answer without making it truer, and the row would
    then have to carry a warning about a number nobody should have computed. So this row asks for
    one thing: **the latest published session, detected again.** (DECISIONS-TW **TW12.2**.)

    **It runs the detector that already exists.** ``baskfy.twt.scan`` claims this row, calls
    ``baskfy_worker.tasks.twt.detect_session`` — the same function ``baskfy.twt.detect`` calls,
    with ``force=True`` because being asked for is the point — and writes ``DONE`` with the
    funnel in ``detail``, or ``FAILED`` with the reason in ``error`` and nothing else written.
    Detection is idempotent per ``(user_id, date)`` (house rule 7), so pressing the button twice
    overwrites the same rows and moves no counter.

    **It cannot place, size or cancel anything.** It writes ``tw_signal_daily``,
    ``tw_state_daily`` and ``tw_breadth_daily`` and nothing else. The plan is still the evening
    job's and the confirm is still a person's — ``02`` Track C §3 is untouched by this table, and
    ``packages/core/tests/test_twt_safety_properties.py`` proves it over the routes that write it.

    The desk inserts the row ``QUEUED`` and the worker's minute sweep publishes it, because the
    desk has no Celery client (`app/twt_desk.py`); the API publishes directly and the sweep is its
    fallback when no broker is configured. Same path, same reason, as both older sleeves.
    """

    __tablename__ = "tw_scan_run"
    __table_args__ = (
        _in_check("status_known", "status", TW_SCAN_STATUSES),
        _in_check("source_known", "source", TW_SCAN_SOURCES),
        CheckConstraint(
            "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at",
            name="finished_after_started",
        ),
        Index("ix_tw_scan_run_user_id_requested_at", "user_id", "requested_at"),
    )

    id: Mapped[BigIntPk]
    user_id: Mapped[int] = _user_fk()
    requested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Which published session was detected again. Null until the worker has decided, because the
    #: caller asks for "the latest" and only the worker knows which that is.
    session_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="QUEUED")
    source: Mapped[str] = mapped_column(String(8), nullable=False, server_default="desk")
    #: ``{"signals": n, "status": "...", ...the funnel}`` — the same shape the nightly step
    #: writes, so the row a person reads after pressing the button reads like the night's own.
    detail: Mapped[JsonObject | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The broker's message id once published. A row without one has not been picked up yet, and
    #: the sweep is what publishes it — which is how the desk's button works with no Celery.
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[CreatedAt]
