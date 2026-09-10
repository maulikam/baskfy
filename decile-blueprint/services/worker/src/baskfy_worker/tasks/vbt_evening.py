"""VB6 / VB7 — the evening and morning plans for the volume-breakout sleeve.

Every trading session, after the detector has written what it saw, this job decides what the desk
should be asked to confirm, and writes it as a ``vb_plan`` a person can act on line by line:

1. **Cancels first.** ``expire_orders`` over the working limits: one that has finished its third
   session becomes a ``CANCEL_LIMIT`` line (``04`` §7.2, VB7). Sessions are counted on the run's
   own calendar, so a holiday consumes none.
2. **Then exits.** ``manage`` over each open position with the session's bar and its 21-day EMA:
   a close below the average queues a ``SELL_AT_OPEN`` for the next session (``04`` §6.2); a
   position whose stop was reached is recorded as stopped out; a name that has stopped printing
   is written off.
3. **Then the naked stops.** A filled position with no resting GTT is an ``ARM_GTT`` line, because
   a position without a stop is the one state the method forbids (non-negotiable 4).
4. **Then the entries.** ``build_entries`` over the session's ``SIGNAL`` rows, ranked by
   signal-day turnover, against the gate, the sleeve's own money and the book (``04`` §9.1) —
   with every name it passed over recorded as a ``vb_plan_skip``, because a plan is not honest
   without its skips.
5. **Finally the session row**, which is what ``02`` §3.1's twenty-session gate counts.

**Nothing here places an order.** Every line is `PROPOSED`; a person confirms it on the desk, and
only then does anything reach `OrderGateway`. `02` Track C §3, and there is no flag that changes
it — DECISIONS-VB PACK.2.

IDEMPOTENT BY CONSTRUCTION, NOT BY BOOKKEEPING
---------------------------------------------
``vb_order.sessions_worked`` is **recomputed** from the calendar (``sessions_between(signal_date,
today)``) rather than incremented. An incrementing counter has to remember whether tonight already
ran; a derived one cannot be wrong however many times the job runs, and that is the difference
between a rule and a bookkeeping convention. The plan itself is replaced for its `(session,
source)`, so a re-run rewrites rather than duplicates.

THE CLOCK
---------
``04`` §10: the plan is built from the **last completed session**. `EVENING` builds it from that
session's close for orders to be sent the next morning; `MORNING` rebuilds the same plan before
the open, from the same signals and the same levels, re-sized against the sleeve as it stands —
because the desk's plans expire in thirty minutes and an evening plan cannot be confirmed at 09:20.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

import polars as pl
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.vbt_settings import record_system_change
from baskfy_api.vbt_sleeve import load_sleeve
from baskfy_core.models import (
    Instrument,
    OhlcvDaily,
    TradingDay,
    VbBreadthDaily,
    VbConfig,
    VbOrder,
    VbPlan,
    VbPlanLine,
    VbPlanSkip,
    VbPosition,
    VbSession,
    VbSignalDaily,
)
from baskfy_core.models.vbt import VB_PLAN_TTL_MINUTES
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.vbt.calendar import SessionCalendar, drop_thin_sessions
from baskfy_core.vbt.config import Gate, SignalState, VbtConfig
from baskfy_core.vbt.exits import (
    Action,
    Bar,
    ExitReason,
    ManageAction,
    OpenPosition,
    manage,
)
from baskfy_core.vbt.indicators import with_vbt_indicators
from baskfy_core.vbt.orders import (
    LIVE_STATES,
    CancelReason,
    OrderState,
    WorkingOrder,
    expire_orders,
    expires_after,
    sessions_since,
)
from baskfy_core.vbt.plan import (
    BookState,
    Candidate,
    LineKind,
    PlanLine,
    VbtPlan,
    assemble,
    build_entries,
    exit_lines,
)
from baskfy_core.vbt.sizing import first_live_multiplier
from baskfy_worker.steps import StepOutcome, StepStatus
from baskfy_worker.tasks.vbt import (
    LOOKBACK_SESSIONS,
    load_vbt_bars,
    load_vbt_config,
    lookback_start,
)

log = logging.getLogger(__name__)

#: Plan sources (``03`` §6). There is no live-trigger source: this is an end-of-day strategy and
#: nothing about it fires inside a session.
SOURCE_EVENING: Final = "EVENING"
SOURCE_MORNING: Final = "MORNING"

#: How far back the calendar is rebuilt when the sleeve has no breadth history yet.
CALENDAR_SESSIONS: Final = 60


@dataclass(frozen=True, slots=True)
class EveningReport:
    """What the evening did, for the step's detail and the CLI's output."""

    session: dt.date
    source: str
    gate: str
    plan_id: str
    equity_inr: Decimal
    entries: int
    sells: int
    cancels: int
    arms: int
    skips: int
    counted_dry_run: bool
    counted_first_live: bool

    def as_detail(self) -> dict[str, object]:
        return {
            "session": self.session.isoformat(),
            "source": self.source,
            "gate": self.gate,
            "plan_id": self.plan_id,
            "equity_inr": str(self.equity_inr),
            "entries": self.entries,
            "sells": self.sells,
            "cancels": self.cancels,
            "arms": self.arms,
            "skips": self.skips,
            "counted_dry_run": self.counted_dry_run,
            "counted_first_live": self.counted_first_live,
        }


async def session_calendar(session: AsyncSession, user_id: int, as_of: dt.date) -> SessionCalendar:
    """The run's **own** calendar, read back from what the detector recorded.

    ``vb_breadth_daily`` has one row per session the detector saw, with ``thin_session`` marking
    the ones ``04`` §2.1 removed. Reading it back rather than recomputing from bars is what makes
    the order window and the detection window the same calendar by construction — "three
    sessions" then means the same thing in the book as it does in the backtest, which is the
    whole point of counting sessions rather than days.
    """
    rows = (
        await session.execute(
            select(VbBreadthDaily.date, VbBreadthDaily.thin_session)
            .where(VbBreadthDaily.user_id == user_id, VbBreadthDaily.date <= as_of)
            .order_by(VbBreadthDaily.date.desc())
            .limit(CALENDAR_SESSIONS)
        )
    ).all()
    kept = sorted(date for date, thin in rows if not thin)
    dropped = tuple(sorted(date for date, thin in rows if thin))
    if as_of not in kept:
        # The evening can run before the detector has written the session (a re-run, a manual
        # invocation). The session itself is a session; it is only *thin* if the detector said so.
        kept = sorted({*kept, as_of}) if as_of not in dropped else kept
    return SessionCalendar(sessions=tuple(kept), dropped=dropped, counts={})


async def live_orders(session: AsyncSession, user_id: int) -> list[VbOrder]:
    """Orders still capable of filling, and therefore still holding a slot (``04`` §9.1)."""
    live = tuple(sorted(state.value for state in LIVE_STATES))
    rows = await session.execute(
        select(VbOrder).where(VbOrder.user_id == user_id, VbOrder.state.in_(live))
    )
    return list(rows.scalars())


async def open_positions(session: AsyncSession, user_id: int) -> list[VbPosition]:
    rows = await session.execute(
        select(VbPosition).where(
            VbPosition.user_id == user_id,
            VbPosition.state == "OPEN",
            VbPosition.quantity_open > 0,
        )
    )
    return list(rows.scalars())


async def symbols_for(session: AsyncSession, instrument_ids: list[int]) -> dict[int, str]:
    if not instrument_ids:
        return {}
    rows = await session.execute(
        select(Instrument.id, Instrument.symbol).where(Instrument.id.in_(instrument_ids))
    )
    return {int(identifier): str(symbol) for identifier, symbol in rows}


async def bars_for_book(
    session: AsyncSession,
    instrument_ids: list[int],
    as_of: dt.date,
    config: VbtConfig,
) -> dict[int, Bar]:
    """The session's bar and 21-day EMA for each held name.

    Recomputed for the held names only — ten instruments over 260 sessions, not the register —
    because the EMA is the exit and nothing stores it for a name that was not a signal today.

    **Its calendar is built from the bars, not from the sleeve's own history**, and the difference
    matters on a young sleeve: `session_calendar` reads back what the *detector* recorded, which
    on a book two weeks old is two weeks of sessions — and a 21-day EMA over fourteen sessions is
    null, which `manage` reads as "hold" and which would silently disable the exit. The
    thin-session rule is the same rule in both (`04` §2.1); only the span differs, and here the
    span has to be the indicator's.
    """
    if not instrument_ids:
        return {}
    start = await lookback_start(session, as_of, LOOKBACK_SESSIONS)
    frame = await load_vbt_bars(session, start, as_of, set(instrument_ids))
    if frame.is_empty():
        return {}
    kept, bar_calendar = drop_thin_sessions(frame, config)
    indicated = with_vbt_indicators(kept, bar_calendar, config)
    today = indicated.filter(pl.col("date") == as_of)
    out: dict[int, Bar] = {}
    for row in today.iter_rows(named=True):
        out[int(row["instrument_id"])] = Bar(
            session=as_of,
            open=_price(row["open"]),
            high=_price(row["high"]),
            low=_price(row["low"]),
            close=_price(row["close"]),
            ema_exit=_price(row["ema_exit"]),
        )
    return out


def _price(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


async def candidates_for(session: AsyncSession, user_id: int, as_of: dt.date) -> list[Candidate]:
    """The session's full signals, as the plan needs them. ``SCAN_ONLY`` rows are not candidates."""
    rows = (
        await session.execute(
            select(VbSignalDaily, Instrument.symbol)
            .join(Instrument, Instrument.id == VbSignalDaily.instrument_id)
            .where(
                VbSignalDaily.user_id == user_id,
                VbSignalDaily.date == as_of,
                VbSignalDaily.state == SignalState.SIGNAL.value,
            )
            .order_by(VbSignalDaily.rank_key.desc())
        )
    ).all()
    return [
        Candidate(
            instrument_id=row.instrument_id,
            symbol=symbol,
            signal_date=row.date,
            close_raw=Decimal(str(row.limit_price)),
            turnover_avg_inr=(
                None if row.turnover_avg_20 is None else Decimal(str(row.turnover_avg_20))
            ),
            rank_key=int(row.rank_key or 0),
            locked_upper_circuit=bool(row.locked_upper_circuit),
        )
        for row, symbol in rows
    ]


async def gate_for_session(session: AsyncSession, user_id: int, as_of: dt.date) -> Gate:
    """The gate of the **signal's own close**, never a later one.

    ``04`` §11 reads it at the session before the fill, which is the session that produced the
    signal — so a plan built tonight is governed by tonight's breadth, not tomorrow's.
    """
    value = (
        await session.execute(
            select(VbBreadthDaily.gate).where(
                VbBreadthDaily.user_id == user_id, VbBreadthDaily.date == as_of
            )
        )
    ).scalar_one_or_none()
    return Gate(value) if value else Gate.SHUT


def to_working(order: VbOrder, calendar: SessionCalendar, as_of: dt.date) -> WorkingOrder:
    """A database row as the pure engine sees it, with its session count **derived**."""
    return WorkingOrder(
        instrument_id=order.instrument_id,
        signal_date=order.signal_date,
        limit_price=Decimal(str(order.limit_price)),
        stop_price=Decimal(str(order.stop_price)),
        quantity=order.quantity,
        state=OrderState(order.state),
        sessions_worked=sessions_since(order.signal_date, as_of, calendar),
        filled_quantity=order.filled_quantity,
    )


async def sweep_expired_orders(  # noqa: PLR0913, PLR0917 - the sweep is its inputs
    session: AsyncSession,
    user_id: int,
    as_of: dt.date,
    calendar: SessionCalendar,
    config: VbtConfig,
    symbols: dict[int, str],
) -> tuple[list[tuple[WorkingOrder, str]], int]:
    """VB7: the working orders that have finished their window, and the session counts refreshed.

    The rows are **not** cancelled here. A `SENT` order is live at a broker, and cancelling it is
    an order-shaped action that goes through the gateway on a confirm — so this produces the
    ``CANCEL_LIMIT`` lines and leaves the state alone. An order that never reached a broker
    (`PROPOSED`, `CONFIRMED`) has nothing to cancel and is marked `EXPIRED` here.
    """
    rows = await live_orders(session, user_id)
    working = [to_working(row, calendar, as_of) for row in rows]
    _, done = expire_orders(working, as_of, calendar, config.entry, CancelReason.EXPIRY_SWEEP)
    expired_ids = {order.instrument_id for order in done}
    refreshed = 0
    for row in rows:
        worked = sessions_since(row.signal_date, as_of, calendar)
        expires = expires_after(row.signal_date, calendar, config.entry)
        if row.sessions_worked != worked or row.expires_after_session != expires:
            row.sessions_worked = worked
            if expires is not None:
                row.expires_after_session = expires
            refreshed += 1
        if row.instrument_id in expired_ids and row.state in (
            OrderState.PROPOSED.value,
            OrderState.CONFIRMED.value,
        ):
            row.state = OrderState.EXPIRED.value
            row.cancel_reason = CancelReason.EXPIRY_SWEEP.value
            row.cancelled_on = as_of
    await session.flush()
    return [
        (order, symbols.get(order.instrument_id, str(order.instrument_id)))
        for order in done
        if order.state is OrderState.CANCELLED
    ], refreshed


def managed_actions(
    positions: list[VbPosition],
    bars: dict[int, Bar],
    symbols: dict[int, str],
    config: VbtConfig,
    blanks: dict[int, int],
) -> list[tuple[int, str, int, ManageAction]]:
    """``manage`` over the book — the pure rules, one position at a time (``04`` §6)."""
    out: list[tuple[int, str, int, ManageAction]] = []
    for row in positions:
        bar = bars.get(
            row.instrument_id,
            Bar(session=row.entry_date, open=None, high=None, low=None, close=None, ema_exit=None),
        )
        action = manage(
            OpenPosition(
                instrument_id=row.instrument_id,
                entry_date=row.entry_date,
                entry_price=Decimal(str(row.entry_avg)),
                quantity=row.quantity_open,
                stop_price=Decimal(str(row.stop_price)),
                initial_stop=Decimal(str(row.initial_stop)),
                exit_queued_for=row.exit_queued_for,
            ),
            bar,
            blank_sessions=blanks.get(row.instrument_id, 0),
            config=config.exits,
        )
        out.append(
            (
                row.instrument_id,
                symbols.get(row.instrument_id, str(row.instrument_id)),
                row.quantity_open,
                action,
            )
        )
    return out


def naked_positions(
    positions: list[VbPosition], symbols: dict[int, str]
) -> tuple[tuple[int, str, int, Decimal], ...]:
    """Filled quantity with no resting GTT — the one state the method forbids."""
    return tuple(
        (
            row.instrument_id,
            symbols.get(row.instrument_id, str(row.instrument_id)),
            row.quantity_open,
            Decimal(str(row.stop_price)),
        )
        for row in positions
        if row.gtt_id is None and row.quantity_open > 0
    )


async def blank_session_counts(
    session: AsyncSession,
    positions: list[VbPosition],
    as_of: dt.date,
    calendar: SessionCalendar,
) -> dict[int, int]:
    """How many sessions each held name has gone without printing (``04`` §6.5).

    Counted on the calendar, not in days, and capped at the position's own age: a name bought on
    Friday that has not printed on Monday is one session silent, not a weekend's worth.
    """
    if not positions:
        return {}
    last_bars = await _last_bar_dates(session, [row.instrument_id for row in positions], as_of)
    out: dict[int, int] = {}
    for row in positions:
        last = last_bars.get(row.instrument_id)
        if last is None:
            out[row.instrument_id] = sessions_since(row.entry_date, as_of, calendar)
        else:
            out[row.instrument_id] = sessions_since(last, as_of, calendar)
    return out


async def _last_bar_dates(
    session: AsyncSession, instrument_ids: list[int], as_of: dt.date
) -> dict[int, dt.date]:
    rows = await session.execute(
        select(OhlcvDaily.instrument_id, func.max(OhlcvDaily.date))
        .where(OhlcvDaily.instrument_id.in_(instrument_ids), OhlcvDaily.date <= as_of)
        .group_by(OhlcvDaily.instrument_id)
    )
    return {int(instrument_id): last for instrument_id, last in rows}


async def entries_confirmed_today(session: AsyncSession, user_id: int, as_of: dt.date) -> int:
    """`04` §5.3: the session cap counts what the session has already committed to.

    Orders whose signal is today's session and that a person has confirmed or the desk has sent —
    whatever plan they came from. Without this, a second plan built after two confirms would
    offer three more.
    """
    rows = await session.execute(
        select(VbOrder).where(
            VbOrder.user_id == user_id,
            VbOrder.signal_date == as_of,
            VbOrder.state.in_(
                (OrderState.CONFIRMED.value, OrderState.SENT.value, OrderState.FILLED.value)
            ),
        )
    )
    return len(list(rows.scalars()))


async def store_plan(  # noqa: PLR0913 - a stored plan is its plan and its context
    session: AsyncSession,
    *,
    user_id: int,
    as_of: dt.date,
    source: str,
    plan: VbtPlan,
    now: dt.datetime,
) -> uuid.UUID:
    """Replace this ``(session, source)``'s plan. A re-run rewrites rather than duplicates."""
    existing = (
        (
            await session.execute(
                select(VbPlan.id).where(
                    VbPlan.user_id == user_id,
                    VbPlan.session_date == as_of,
                    VbPlan.source == source,
                )
            )
        )
        .scalars()
        .all()
    )
    if existing:
        await session.execute(delete(VbPlan).where(VbPlan.id.in_(list(existing))))
        await session.flush()
    plan_id = uuid.uuid4()
    row = VbPlan(
        plan_id=plan_id,
        user_id=user_id,
        session_date=as_of,
        source=source,
        built_at=now,
        expires_at=now + dt.timedelta(minutes=VB_PLAN_TTL_MINUTES),
        plan_hash=plan.plan_hash(),
        gate=plan.gate.value,
        sleeve_equity_inr=plan.sleeve_equity_inr,
        total_new_exposure_inr=plan.total_new_exposure_inr,
    )
    session.add(row)
    await session.flush()
    for line in plan.lines:
        session.add(
            VbPlanLine(
                plan_id=row.id,
                user_id=user_id,
                instrument_id=line.instrument_id,
                kind=line.kind.value,
                state="PROPOSED",
                quantity=line.quantity,
                limit_price=line.limit_price,
                stop_price=line.stop_price,
                value_inr=line.value_inr,
                size_cap=None if line.cap is None else line.cap.value,
                reason=None if line.reason is None else line.reason.value,
                note=line.note,
                client_id=f"{plan_id}:{line.symbol}:{line.kind.value}",
            )
        )
    for skip in plan.skips:
        session.add(
            VbPlanSkip(
                plan_id=row.id,
                user_id=user_id,
                instrument_id=skip.instrument_id,
                reason=skip.reason.value,
                detail=skip.detail,
            )
        )
    await session.flush()
    return plan_id


async def queue_exits(
    session: AsyncSession,
    positions: list[VbPosition],
    actions: list[tuple[int, str, int, ManageAction]],
    next_session: dt.date | None,
) -> int:
    """Write ``exit_queued_for`` on the positions ``manage`` decided to sell (``04`` §6.2).

    The *decision* is tonight's and the *fill* is tomorrow's open, so the position carries the
    session it is to be sold on. A position already queued for an earlier session keeps its date:
    a sell that has not been confirmed does not get postponed by another evening running.
    """
    by_id = {row.instrument_id: row for row in positions}
    queued = 0
    for instrument_id, _symbol, _quantity, action in actions:
        row = by_id.get(instrument_id)
        if row is None:
            continue
        if action.action is Action.QUEUE_SELL_AT_OPEN and row.exit_queued_for is None:
            row.exit_queued_for = next_session
            row.exit_reason_queued = ExitReason.EMA_EXIT.value
            queued += 1
    await session.flush()
    return queued


async def settle_session(  # noqa: PLR0913 - the session row is its counters
    session: AsyncSession,
    *,
    user_id: int,
    as_of: dt.date,
    gate: Gate,
    signals: int,
    plan_id: uuid.UUID,
    executable_lines: int,
    execution_enabled: bool,
    now: dt.datetime,
) -> tuple[bool, bool]:
    """The ``vb_session`` row, and the two counters it moves. Returns ``(dry_run, first_live)``.

    **`02` §3.1's twenty is a gate, so what counts as a session matters.** A session counts when
    it closed in `DRY_RUN` mode **and** either a line was confirmed on it or the plan had no
    executable line at all. The second clause is not a loophole: a shut gate with an empty book
    produces nothing to confirm, and a gate that could only be satisfied on days the market
    cooperated would never be satisfied. What it refuses is the case the gate is actually about —
    a session with lines on the page that nobody rehearsed. DECISIONS-VB **VB6.2**.
    """
    row = (
        await session.execute(
            select(VbSession).where(VbSession.user_id == user_id, VbSession.session_date == as_of)
        )
    ).scalar_one_or_none()
    if row is None:
        row = VbSession(user_id=user_id, session_date=as_of)
        session.add(row)
    row.mode = "LIVE" if execution_enabled else "DRY_RUN"
    row.gate = gate.value
    row.signals = signals
    row.plan_ids = {**(row.plan_ids or {}), "evening": str(plan_id)}
    row.notes = None
    await session.flush()

    counted_dry_run = False
    counted_first_live = False
    config_row = (
        await session.execute(select(VbConfig).where(VbConfig.user_id == user_id))
    ).scalar_one_or_none()
    if config_row is None:
        return counted_dry_run, counted_first_live

    if row.mode == "DRY_RUN" and not row.counted_for_dry_run_gate:
        rehearsed = row.confirms > 0 or executable_lines == 0
        if rehearsed:
            row.counted_for_dry_run_gate = True
            await record_system_change(
                session,
                user_id=user_id,
                field="dry_run_sessions",
                value=config_row.dry_run_sessions + 1,
                changed_by="vbt-evening",
                now=now,
                note=f"{as_of.isoformat()} closed in DRY_RUN",
            )
            counted_dry_run = True
    if (
        row.mode == "LIVE"
        and not row.first_live_counted
        and config_row.first_live_sessions_left > 0
    ):
        row.first_live_counted = True
        await record_system_change(
            session,
            user_id=user_id,
            field="first_live_sessions_left",
            value=config_row.first_live_sessions_left - 1,
            changed_by="vbt-evening",
            now=now,
            note=f"{as_of.isoformat()} closed LIVE",
        )
        counted_first_live = True
    await session.flush()
    return counted_dry_run, counted_first_live


async def run_vbt_evening(  # noqa: PLR0913 - one keyword per input the evening depends on
    session: AsyncSession,
    outcome: StepOutcome,
    trade_date: dt.date,
    *,
    user_id: int,
    execution_enabled: bool = False,
    source: str = SOURCE_EVENING,
    now: dt.datetime | None = None,
) -> EveningReport | None:
    """Build the session's plan and settle its row. Returns ``None`` when there is nothing to do.

    Nothing here places an order. Every line is `PROPOSED` until a person confirms it on the desk.
    """
    stamp = now or dt.datetime.now(tz=dt.UTC)
    config = await load_vbt_config(session, user_id)
    calendar = await session_calendar(session, user_id, trade_date)
    if trade_date not in calendar.sessions:
        outcome.status = StepStatus.SKIPPED
        outcome.note(skipped_reason=f"{trade_date} is not a session this sleeve traded")
        return None

    positions = await open_positions(session, user_id)
    orders = await live_orders(session, user_id)
    symbols = await symbols_for(
        session,
        sorted({row.instrument_id for row in positions} | {row.instrument_id for row in orders}),
    )
    cancels, refreshed = await sweep_expired_orders(
        session, user_id, trade_date, calendar, config, symbols
    )
    bars = await bars_for_book(
        session, [row.instrument_id for row in positions], trade_date, config
    )
    blanks = await blank_session_counts(session, positions, trade_date, calendar)
    actions = managed_actions(positions, bars, symbols, config, blanks)

    sleeve = (await load_sleeve(session, user_id, trade_date)).quantize()
    gate = await gate_for_session(session, user_id, trade_date)
    candidates = await candidates_for(session, user_id, trade_date)
    config_row = (
        await session.execute(select(VbConfig).where(VbConfig.user_id == user_id))
    ).scalar_one_or_none()
    multiplier = first_live_multiplier(
        sessions_left=0 if config_row is None else config_row.first_live_sessions_left,
        execution_enabled=execution_enabled,
        config=config.sizing,
    )
    book = BookState(
        open_instrument_ids=frozenset(row.instrument_id for row in positions),
        working_instrument_ids=frozenset(
            row.instrument_id for row in orders if row.state in {s.value for s in LIVE_STATES}
        ),
        open_exposure_inr=sleeve.open_exposure_inr,
        cash_available_inr=sleeve.cash_available_inr,
        entries_already_this_session=await entries_confirmed_today(session, user_id, trade_date),
        positions_naked_of_gtt=naked_positions(positions, symbols),
    )
    entries, skips = build_entries(
        candidates,
        gate=gate,
        equity=sleeve.equity_inr,
        book=book,
        config=config,
        slot_multiplier=multiplier,
        max_open_positions=None if config_row is None else config_row.max_open_positions,
    )
    exits = exit_lines(actions, cancels, book)
    plan = assemble(trade_date, gate, sleeve.equity_inr, exits, entries, skips, source)
    plan_id = await store_plan(
        session, user_id=user_id, as_of=trade_date, source=source, plan=plan, now=stamp
    )
    next_session = calendar.advance(trade_date, 1) or await next_trading_day(session, trade_date)
    await queue_exits(session, positions, actions, next_session)

    executable = sum(1 for line in plan.lines if line.kind is not LineKind.CANCEL_LIMIT)
    counted_dry, counted_live = await settle_session(
        session,
        user_id=user_id,
        as_of=trade_date,
        gate=gate,
        signals=len(candidates),
        plan_id=plan_id,
        executable_lines=executable,
        execution_enabled=execution_enabled,
        now=stamp,
    )
    report = EveningReport(
        session=trade_date,
        source=source,
        gate=gate.value,
        plan_id=str(plan_id),
        equity_inr=sleeve.equity_inr,
        entries=_count(plan.lines, LineKind.PLACE_LIMIT),
        sells=_count(plan.lines, LineKind.SELL_AT_OPEN),
        cancels=_count(plan.lines, LineKind.CANCEL_LIMIT),
        arms=_count(plan.lines, LineKind.ARM_GTT),
        skips=len(plan.skips),
        counted_dry_run=counted_dry,
        counted_first_live=counted_live,
    )
    outcome.rows_out = len(plan.lines)
    outcome.note(orders_refreshed=refreshed, **report.as_detail())
    return report


def _count(lines: tuple[PlanLine, ...], kind: LineKind) -> int:
    return sum(1 for line in lines if line.kind is kind)


async def next_trading_day(session: AsyncSession, after: dt.date) -> dt.date | None:
    """The session a queued exit will fill on, from ``trading_day``.

    **Not** from the sleeve's own calendar, and this is the one place the two must differ: the
    sleeve's calendar is a record of sessions that have *happened*, so tonight is always its last
    entry and "the next one" is not in it. The exchange's calendar knows tomorrow.

    A muhurat session could in principle be next, and the thin-session rule cannot know that in
    advance — no calendar can, since thinness is measured from the bars. The morning plan rebuilds
    from the last session the sleeve actually saw, which is where that corrects itself.
    """
    return (
        await session.execute(
            select(TradingDay.date)
            .where(
                TradingDay.exchange_id == NSE_EXCHANGE_ID,
                TradingDay.is_trading_day.is_(True),
                TradingDay.date > after,
            )
            .order_by(TradingDay.date)
            .limit(1)
        )
    ).scalar_one_or_none()


async def last_detected_session(
    session: AsyncSession, user_id: int, on_or_before: dt.date
) -> dt.date | None:
    """The most recent session the detector wrote a **tradeable** breadth row for.

    What the morning plan is built from. ``04`` §10: a plan is built from the last **completed**
    session, and before the open that is yesterday — but "yesterday" is a calendar word, and the
    honest answer is the last session this sleeve actually saw. A thin session is skipped, because
    the rules did not trade it.
    """
    return (
        await session.execute(
            select(VbBreadthDaily.date)
            .where(
                VbBreadthDaily.user_id == user_id,
                VbBreadthDaily.date <= on_or_before,
                VbBreadthDaily.thin_session.is_(False),
            )
            .order_by(VbBreadthDaily.date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
