"""The volume-breakout sleeve's read surfaces (`docs/vbt/05` §2), and nothing else.

    today(...)      the session's candidates, its rejects, the gate and the funnel
    breadth(...)    the `vb_breadth_daily` series behind the gauge
    book(...)       working orders, open and closed positions, and the fill rate
    backtests(...)  the latest finished run per source, with its drift

**No function here writes.** `02` Track C §4 — "`apps/web` gets no route under `/vbt` that can
reach the gateway" — is enforced at the router by `test_vbt_readonly.py`, and this module is the
reason that assertion is cheap to keep: there is no write to accidentally expose.

Every view is assembled from `vb_` rows and the instrument table. The sleeve is one person's
book (`03` §1), so every query is keyed by `user_id` and none of them has a tenant-free variant.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import Decimal
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import (
    Instrument,
    OhlcvDaily,
    VbBacktestRun,
    VbBreadthDaily,
    VbOrder,
    VbPosition,
    VbSignalDaily,
)
from baskfy_core.models.base import JsonObject
from baskfy_core.vbt.config import DEFAULT_VBT_CONFIG, Gate, SignalState
from baskfy_core.vbt.orders import LIVE_STATES
from baskfy_core.vbt.published import PUBLISHED

#: How many closes the row's mini chart draws (`05` §2). The 200-day average needs more history
#: than that to *exist*, and it is computed by the detector, not by the chart.
CHART_BARS: Final = 130

#: How long the breadth gauge looks back by default: `05` §2's "last year".
BREADTH_DEFAULT_DAYS: Final = 365

#: `05` §2's "sessions the gate has been shut in the last 60".
SHUT_WINDOW_SESSIONS: Final = 60


def _threshold_pct() -> Decimal:
    """`04` §4.2's gate, as the exact decimal the page prints beside the reading.

    The config keeps it as a float because that is what the breadth expression compares against;
    a page renders "the gate opens above 40%" and must not render "40.00000000000001".
    """
    return Decimal(str(DEFAULT_VBT_CONFIG.breadth.min_pct_above_dma))


__all__ = [
    "BREADTH_DEFAULT_DAYS",
    "CHART_BARS",
    "SHUT_WINDOW_SESSIONS",
    "BacktestRunView",
    "BarPoint",
    "BookView",
    "BreadthPoint",
    "BreadthView",
    "CandidateRow",
    "ClosedRow",
    "FillRate",
    "PositionRow",
    "TodayView",
    "WorkingRow",
    "backtests",
    "bars_for",
    "book",
    "breadth",
    "today",
]


@dataclasses.dataclass(frozen=True, slots=True)
class CandidateRow:
    """One `vb_signal_daily` row as the page reads it.

    Prices are **exchange** prices: `04` §7.1 divides by `adj_factor` before it snaps the limit
    to the tick, because the number on this page is typed into a broker. `pct_above_dma` is
    derived here rather than stored — it is the one number on the row that is a *relation*
    between two stored ones, and storing it would be a third place for them to disagree.
    """

    instrument_id: int
    symbol: str
    name: str
    state: str
    failed_filters: tuple[str, ...]
    close: Decimal
    limit_price: Decimal
    stop_price: Decimal
    change_pct: Decimal | None
    rvol: Decimal | None
    close_position: Decimal | None
    ret_20_pct: Decimal | None
    turnover_avg_20: int | None
    sma_200: Decimal | None
    ema_21: Decimal | None
    high_20_prior: Decimal | None
    pct_above_dma: Decimal | None
    locked_upper_circuit: bool
    rank_key: int


@dataclasses.dataclass(frozen=True, slots=True)
class TodayView:
    """`05` §2's Today tab, in one read.

    `as_of` is null on a database the detector has never run against, and that is a **different**
    state from a session with no candidates — the page says so, and the funnel is what lets it.
    """

    as_of: dt.date | None
    gate: str | None
    pct_above_dma: Decimal | None
    above_count: int | None
    measured_count: int | None
    gate_threshold_pct: Decimal
    thin_session: bool
    funnel: JsonObject | None
    shut_sessions_recent: int
    shut_window: int
    candidates: tuple[CandidateRow, ...]
    rejects: tuple[CandidateRow, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class BreadthPoint:
    date: dt.date
    pct_above_dma: Decimal
    above_count: int
    measured_count: int
    gate: str
    thin_session: bool


@dataclasses.dataclass(frozen=True, slots=True)
class BreadthView:
    threshold_pct: Decimal
    data: tuple[BreadthPoint, ...]


@dataclasses.dataclass(frozen=True, slots=True)
class BarPoint:
    date: dt.date
    close: Decimal


@dataclasses.dataclass(frozen=True, slots=True)
class WorkingRow:
    """A limit the desk has put to work, with the number that matters: sessions of three.

    `expires_tonight` is derived from the calendar the evening wrote, not from a count of dates:
    a holiday consumes no session (`04` §7.2), so a row placed on Thursday before a long weekend
    is still in its first session on Tuesday.
    """

    id: int
    instrument_id: int
    symbol: str
    name: str
    limit_price: Decimal
    stop_price: Decimal
    quantity: int
    value_inr: Decimal
    state: str
    signal_date: dt.date
    working_from: dt.date | None
    expires_after_session: dt.date | None
    sessions_worked: int
    sessions_allowed: int
    expires_tonight: bool
    broker_order_id: str | None
    filled_quantity: int
    simulated: bool


@dataclasses.dataclass(frozen=True, slots=True)
class PositionRow:
    id: int
    instrument_id: int
    symbol: str
    name: str
    entry_date: dt.date
    entry_avg: Decimal
    quantity_open: int
    initial_stop: Decimal
    stop_price: Decimal
    gtt_id: str | None
    naked: bool
    last_close: Decimal | None
    ema_21: Decimal | None
    distance_to_ema_pct: Decimal | None
    return_pct: Decimal | None
    r_multiple: Decimal | None
    sessions_held: int | None
    exit_queued_for: dt.date | None
    exit_reason_queued: str | None
    simulated: bool


@dataclasses.dataclass(frozen=True, slots=True)
class ClosedRow:
    id: int
    instrument_id: int
    symbol: str
    name: str
    entry_date: dt.date
    closed_on: dt.date | None
    entry_avg: Decimal
    exit_avg: Decimal | None
    quantity_entered: int
    close_reason: str | None
    return_pct: Decimal | None
    r_multiple: Decimal | None
    simulated: bool


@dataclasses.dataclass(frozen=True, slots=True)
class FillRate:
    """`04` §7.3's honest early warning, and it is on the page from the first fill.

    `filled` over `resolved` — an order still working is neither, because counting it as a miss
    would make every fresh limit look like a failure and counting it as a fill would be a lie.
    `modelled_pct` is the study's 91%: the number this one is drifting away from, if it is.
    """

    filled: int
    resolved: int
    rate_pct: Decimal | None
    modelled_pct: Decimal


@dataclasses.dataclass(frozen=True, slots=True)
class BookView:
    working: tuple[WorkingRow, ...]
    open_positions: tuple[PositionRow, ...]
    closed_positions: tuple[ClosedRow, ...]
    fill_rate: FillRate


@dataclasses.dataclass(frozen=True, slots=True)
class BacktestRunView:
    id: int
    source: str
    started_at: dt.datetime
    finished_at: dt.datetime | None
    params: JsonObject
    stats: JsonObject | None
    drift: JsonObject | None
    error: str | None


# --- today -------------------------------------------------------------------


async def _latest_breadth(session: AsyncSession, user_id: int) -> VbBreadthDaily | None:
    return (
        await session.execute(
            select(VbBreadthDaily)
            .where(VbBreadthDaily.user_id == user_id)
            .order_by(VbBreadthDaily.date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _pct_above_dma(close: Decimal, sma: Decimal | None) -> Decimal | None:
    if sma is None or sma == 0:
        return None
    return ((close / sma - 1) * 100).quantize(Decimal("0.01"))


def _candidate(row: VbSignalDaily, symbol: str, name: str) -> CandidateRow:
    return CandidateRow(
        instrument_id=row.instrument_id,
        symbol=symbol,
        name=name,
        state=row.state,
        failed_filters=tuple(row.failed_filters),
        close=row.close_raw,
        limit_price=row.limit_price,
        stop_price=row.stop_price,
        change_pct=row.change_pct,
        rvol=row.rvol,
        close_position=row.close_position,
        ret_20_pct=row.ret_20_pct,
        turnover_avg_20=row.turnover_avg_20,
        sma_200=row.sma_200,
        ema_21=row.ema_21,
        high_20_prior=row.high_20_prior,
        pct_above_dma=_pct_above_dma(row.close, row.sma_200),
        locked_upper_circuit=row.locked_upper_circuit,
        rank_key=row.rank_key,
    )


async def _rows_for(
    session: AsyncSession, user_id: int, day: dt.date, state: str
) -> tuple[CandidateRow, ...]:
    result = await session.execute(
        select(VbSignalDaily, Instrument.symbol, Instrument.name)
        .join(Instrument, Instrument.id == VbSignalDaily.instrument_id)
        .where(
            VbSignalDaily.user_id == user_id,
            VbSignalDaily.date == day,
            VbSignalDaily.state == state,
        )
        .order_by(VbSignalDaily.rank_key.desc())
    )
    return tuple(_candidate(row, symbol, name) for row, symbol, name in result.all())


async def _shut_recently(session: AsyncSession, user_id: int, day: dt.date) -> int:
    """How many of the last sixty sessions the gate was shut on — sessions, not days."""
    recent = (
        select(VbBreadthDaily.gate)
        .where(VbBreadthDaily.user_id == user_id, VbBreadthDaily.date <= day)
        .order_by(VbBreadthDaily.date.desc())
        .limit(SHUT_WINDOW_SESSIONS)
        .subquery()
    )
    return int(
        (
            await session.execute(
                select(func.count()).select_from(recent).where(recent.c.gate == Gate.SHUT.value)
            )
        ).scalar_one()
    )


async def today(session: AsyncSession, *, user_id: int, day: dt.date | None = None) -> TodayView:
    """The session's candidates, its rejects, the gate that decided and the funnel behind it.

    `day` defaults to the latest session the detector wrote a breadth row for — **not** to
    today's date. `04` §10: a daily bar is a closed day, and the sleeve's clock is the published
    session's, so asking for "today" during a session would answer with an empty page rather than
    with the last real one.
    """
    reading = (
        (
            await session.execute(
                select(VbBreadthDaily).where(
                    VbBreadthDaily.user_id == user_id, VbBreadthDaily.date == day
                )
            )
        ).scalar_one_or_none()
        if day is not None
        else await _latest_breadth(session, user_id)
    )
    threshold = _threshold_pct()
    if reading is None:
        return TodayView(
            as_of=day,
            gate=None,
            pct_above_dma=None,
            above_count=None,
            measured_count=None,
            gate_threshold_pct=threshold,
            thin_session=False,
            funnel=None,
            shut_sessions_recent=0,
            shut_window=SHUT_WINDOW_SESSIONS,
            candidates=(),
            rejects=(),
        )
    return TodayView(
        as_of=reading.date,
        gate=reading.gate,
        pct_above_dma=reading.pct_above_dma,
        above_count=reading.above_count,
        measured_count=reading.measured_count,
        gate_threshold_pct=threshold,
        thin_session=reading.thin_session,
        funnel=reading.detail,
        shut_sessions_recent=await _shut_recently(session, user_id, reading.date),
        shut_window=SHUT_WINDOW_SESSIONS,
        candidates=await _rows_for(session, user_id, reading.date, SignalState.SIGNAL.value),
        rejects=await _rows_for(session, user_id, reading.date, SignalState.SCAN_ONLY.value),
    )


async def bars_for(
    session: AsyncSession, *, instrument_id: int, as_of: dt.date, limit: int = CHART_BARS
) -> tuple[BarPoint, ...]:
    """The row's mini chart: adjusted closes, newest last.

    Adjusted, because the chart shows a **shape** and a raw series with a split in it shows a
    cliff that never happened. The levels drawn beside it — the limit, the stop, the prior high —
    are exchange prices, and `05` §2 says which is which.
    """
    rows = (
        await session.execute(
            select(OhlcvDaily.date, OhlcvDaily.close)
            .where(OhlcvDaily.instrument_id == instrument_id, OhlcvDaily.date <= as_of)
            .order_by(OhlcvDaily.date.desc())
            .limit(limit)
        )
    ).all()
    return tuple(BarPoint(date=date, close=close) for date, close in reversed(rows))


# --- breadth -----------------------------------------------------------------


async def breadth(
    session: AsyncSession,
    *,
    user_id: int,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> BreadthView:
    statement = select(VbBreadthDaily).where(VbBreadthDaily.user_id == user_id)
    if start is not None:
        statement = statement.where(VbBreadthDaily.date >= start)
    if end is not None:
        statement = statement.where(VbBreadthDaily.date <= end)
    rows = (await session.execute(statement.order_by(VbBreadthDaily.date))).scalars()
    return BreadthView(
        threshold_pct=_threshold_pct(),
        data=tuple(
            BreadthPoint(
                date=row.date,
                pct_above_dma=row.pct_above_dma,
                above_count=row.above_count,
                measured_count=row.measured_count,
                gate=row.gate,
                thin_session=row.thin_session,
            )
            for row in rows
        ),
    )


# --- the book ----------------------------------------------------------------


async def _names(session: AsyncSession, instrument_ids: set[int]) -> dict[int, tuple[str, str]]:
    if not instrument_ids:
        return {}
    rows = (
        await session.execute(
            select(Instrument.id, Instrument.symbol, Instrument.name).where(
                Instrument.id.in_(instrument_ids)
            )
        )
    ).all()
    return {int(row[0]): (row[1], row[2]) for row in rows}


async def _last_closes(
    session: AsyncSession, instrument_ids: set[int], as_of: dt.date
) -> dict[int, Decimal]:
    if not instrument_ids:
        return {}
    newest = (
        select(
            OhlcvDaily.instrument_id.label("instrument_id"),
            func.max(OhlcvDaily.date).label("date"),
        )
        .where(OhlcvDaily.instrument_id.in_(instrument_ids), OhlcvDaily.date <= as_of)
        .group_by(OhlcvDaily.instrument_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(OhlcvDaily.instrument_id, OhlcvDaily.close_raw).join(
                newest,
                (OhlcvDaily.instrument_id == newest.c.instrument_id)
                & (OhlcvDaily.date == newest.c.date),
            )
        )
    ).all()
    return {int(instrument_id): close for instrument_id, close in rows}


async def _sessions_held(
    session: AsyncSession, user_id: int, entry: dt.date, as_of: dt.date
) -> int:
    """Published sessions, counted the way the sleeve counts everything else (`04` §10)."""
    return int(
        (
            await session.execute(
                select(func.count()).select_from(
                    select(VbBreadthDaily.date)
                    .where(
                        VbBreadthDaily.user_id == user_id,
                        VbBreadthDaily.date > entry,
                        VbBreadthDaily.date <= as_of,
                        VbBreadthDaily.thin_session.is_(False),
                    )
                    .subquery()
                )
            )
        ).scalar_one()
    )


def _return_pct(entry: Decimal, mark: Decimal | None) -> Decimal | None:
    if mark is None or entry == 0:
        return None
    return ((mark / entry - 1) * 100).quantize(Decimal("0.01"))


def _r_multiple(entry: Decimal, stop: Decimal, mark: Decimal | None) -> Decimal | None:
    risk = entry - stop
    if mark is None or risk <= 0:
        return None
    return ((mark - entry) / risk).quantize(Decimal("0.01"))


async def _fill_rate(session: AsyncSession, user_id: int) -> FillRate:
    counts: dict[str, int] = {
        str(state): int(count)
        for state, count in (
            await session.execute(
                select(VbOrder.state, func.count())
                .where(VbOrder.user_id == user_id)
                .group_by(VbOrder.state)
            )
        ).all()
    }
    live = {state.value for state in LIVE_STATES}
    filled = counts.get("FILLED", 0)
    resolved = sum((count for state, count in counts.items() if state not in live), 0)
    rate = (
        (Decimal(filled) / Decimal(resolved) * 100).quantize(Decimal("0.1")) if resolved else None
    )
    return FillRate(
        filled=filled,
        resolved=resolved,
        rate_pct=rate,
        modelled_pct=Decimal(str(PUBLISHED.modelled_fill_rate_pct)),
    )


async def book(session: AsyncSession, *, user_id: int, as_of: dt.date | None = None) -> BookView:
    """Working orders, the open book, what is closed, and the fill rate under all of it."""
    day = as_of or dt.date.today()
    orders = list(
        (
            await session.execute(
                select(VbOrder)
                .where(
                    VbOrder.user_id == user_id,
                    VbOrder.state.in_(sorted(s.value for s in LIVE_STATES)),
                )
                .order_by(VbOrder.signal_date.desc(), VbOrder.id)
            )
        ).scalars()
    )
    positions = list(
        (
            await session.execute(
                select(VbPosition)
                .where(VbPosition.user_id == user_id)
                .order_by(VbPosition.entry_date.desc(), VbPosition.id)
            )
        ).scalars()
    )
    ids = {order.instrument_id for order in orders} | {row.instrument_id for row in positions}
    names = await _names(session, ids)
    closes = await _last_closes(session, ids, day)
    allowed = DEFAULT_VBT_CONFIG.entry.valid_sessions

    working = tuple(
        WorkingRow(
            id=order.id,
            instrument_id=order.instrument_id,
            symbol=names.get(order.instrument_id, ("?", "?"))[0],
            name=names.get(order.instrument_id, ("?", "?"))[1],
            limit_price=order.limit_price,
            stop_price=order.stop_price,
            quantity=order.quantity,
            value_inr=(order.limit_price * order.quantity).quantize(Decimal("0.01")),
            state=order.state,
            signal_date=order.signal_date,
            working_from=order.working_from,
            expires_after_session=order.expires_after_session,
            sessions_worked=order.sessions_worked,
            sessions_allowed=allowed,
            expires_tonight=order.expires_after_session is not None
            and order.expires_after_session <= day,
            broker_order_id=order.broker_order_id,
            filled_quantity=order.filled_quantity,
            simulated=order.simulated,
        )
        for order in orders
    )

    open_rows: list[PositionRow] = []
    closed_rows: list[ClosedRow] = []
    for row in positions:
        symbol, name = names.get(row.instrument_id, ("?", "?"))
        if row.state == "OPEN":
            mark = closes.get(row.instrument_id)
            open_rows.append(
                PositionRow(
                    id=row.id,
                    instrument_id=row.instrument_id,
                    symbol=symbol,
                    name=name,
                    entry_date=row.entry_date,
                    entry_avg=row.entry_avg,
                    quantity_open=row.quantity_open,
                    initial_stop=row.initial_stop,
                    stop_price=row.stop_price,
                    gtt_id=row.gtt_id,
                    naked=row.gtt_id is None and row.quantity_open > 0,
                    last_close=mark,
                    ema_21=None,
                    distance_to_ema_pct=None,
                    return_pct=_return_pct(row.entry_avg, mark),
                    r_multiple=_r_multiple(row.entry_avg, row.initial_stop, mark),
                    sessions_held=await _sessions_held(session, user_id, row.entry_date, day),
                    exit_queued_for=row.exit_queued_for,
                    exit_reason_queued=row.exit_reason_queued,
                    simulated=row.simulated,
                )
            )
        else:
            closed_rows.append(
                ClosedRow(
                    id=row.id,
                    instrument_id=row.instrument_id,
                    symbol=symbol,
                    name=name,
                    entry_date=row.entry_date,
                    closed_on=row.closed_on,
                    entry_avg=row.entry_avg,
                    exit_avg=row.exit_avg,
                    quantity_entered=row.quantity_entered,
                    close_reason=row.close_reason,
                    return_pct=_return_pct(row.entry_avg, row.exit_avg),
                    r_multiple=_r_multiple(row.entry_avg, row.initial_stop, row.exit_avg),
                    simulated=row.simulated,
                )
            )

    return BookView(
        working=working,
        open_positions=tuple(open_rows),
        closed_positions=tuple(closed_rows),
        fill_rate=await _fill_rate(session, user_id),
    )


# --- the backtest ------------------------------------------------------------


async def backtests(session: AsyncSession, *, user_id: int) -> tuple[BacktestRunView, ...]:
    """The latest **finished** run per source (`05` §2).

    A run still going, or one that failed with an error and no stats, is not what the page
    compares against the published numbers — `vb_backtest_run` is append-only (`03` §8) and the
    page's job is to show the newest one that **finished with a result**, not the newest one that
    started and not the newest one that stopped.
    """
    newest = (
        select(VbBacktestRun.source, func.max(VbBacktestRun.finished_at).label("finished_at"))
        .where(
            VbBacktestRun.user_id == user_id,
            VbBacktestRun.finished_at.is_not(None),
            # **And it must have produced something.** A failed run sets `finished_at` too
            # (`03` §8), so without this a re-run that raised would displace the last good number
            # with a card full of blanks — the opposite of what an append-only table is for.
            VbBacktestRun.stats.is_not(None),
        )
        .group_by(VbBacktestRun.source)
        .subquery()
    )
    rows = (
        await session.execute(
            select(VbBacktestRun)
            .join(
                newest,
                (VbBacktestRun.source == newest.c.source)
                & (VbBacktestRun.finished_at == newest.c.finished_at),
            )
            .where(VbBacktestRun.user_id == user_id)
            .order_by(VbBacktestRun.source)
        )
    ).scalars()
    return tuple(
        BacktestRunView(
            id=row.id,
            source=row.source,
            started_at=row.started_at,
            finished_at=row.finished_at,
            params=row.params,
            stats=row.stats,
            drift=row.drift,
            error=row.error,
        )
        for row in rows
    )
