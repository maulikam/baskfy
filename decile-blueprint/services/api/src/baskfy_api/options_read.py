"""The options tab's reads, and its two money-free writes (OP5, ``docs/options/05`` §2).

Everything here reads what the worker and the desk wrote — ``op_scan``, ``op_expiry``,
``op_event_day``, ``op_chain_snapshot``, ``op_index_minute``, ``op_session``, ``op_position``,
``op_journal``, ``op_backtest_run`` — and **computes no signal of its own**. The scan is the
worker's (OP4) and the plan is the desk's (OP6-OP10); a second computation here would be a second
answer to the same question with none of their tests behind it.

THE TWO CLOCKS (root ``CLAUDE.md``, the clock table's options row)
------------------------------------------------------------------
Scan states, candidates and the chain are stamped with **the latest minute the collector wrote**
(``op_scan.as_of_minute``) while the session is open, and are ``stale`` beyond two minutes;
outside the session they are the last scanned session's final minute. Nothing here reaches Kite.
The header's NIFTY 50 / India VIX levels are the collector's last minute bar, else the daily
snapshot's close; the web page may overlay the shared live-marks read on those two numbers only
(DECISIONS-OP OP5.3).

WHY AN EMPTY TAB SAYS WHY
-------------------------
With ``BASKFY_OPTIONS_COLLECT_ENABLED`` / ``BASKFY_OPTIONS_SCAN_ENABLED`` false (the default, and
the state of the box when OP5 shipped) ``op_scan`` holds nothing. ``empty_reason`` names which of
the absences it is — collector off, scan off, or no scan yet today — because "nothing found" and
"nothing looked" render identically and only one is a statement about the market (the TWT lesson,
DECISIONS-TW TW14.1).

WRITES
------
Exactly two, and neither moves money: add or remove a person's **event day** (``source='USER'``;
a seeded, source-verified day cannot be removed from the web), and the settings patch, which is
``baskfy_api.options_settings`` unchanged. No function here names a broker, a plan or an order.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.market_hours_cb import IST
from baskfy_core.models import (
    IndexDef,
    IndexSnapshotDaily,
    Instrument,
    OpBacktestRun,
    OpBookConfig,
    OpChainSnapshot,
    OpEventDay,
    OpExpiry,
    OpIndexMinute,
    OpJournal,
    OpPosition,
    OpScan,
    OpSession,
    OpSleeveConfig,
)
from baskfy_core.options.calendar import Contract, next_session, role
from baskfy_core.options.config import (
    DEFAULT_OPTIONS_CONFIG,
    OptionsConfig,
    OptionType,
    SizingMode,
    Sleeve,
    SleeveGroup,
)
from baskfy_core.options.journal import JournalRow, PoolKey, Summary, key_of, summarize

UNDERLYING: Final = "NIFTY"
#: ``instrument.symbol`` of the two index rows the collector writes minute bars for (OP3).
NIFTY_50: Final = "NIFTY 50"
INDIA_VIX: Final = "INDIA VIX"
#: ``index_snapshot_daily`` slugs, as OP4's scan reads them.
NIFTY_50_SLUG: Final = "nifty-50"
INDIA_VIX_SLUG: Final = "india-vix"

#: ``05`` §2: the header shows the next six expiries.
HEADER_EXPIRIES: Final = 6
#: ``05`` §2's clock table: amber beyond two minutes.
STALE_AFTER: Final = dt.timedelta(minutes=2)
#: The chain panel's strikes either side of ATM.
CHAIN_STRIKES_EACH_SIDE: Final = 10

#: The order the page lays the sleeves out in: premium selling, directional, expiry-day setups.
DISPLAY_ORDER: Final[tuple[Sleeve, ...]] = (
    Sleeve.O1M,
    Sleeve.O1W,
    Sleeve.O2,
    Sleeve.O3A,
    Sleeve.O3B,
)

#: ``02`` §3.2 — the paper period per sleeve group: sessions, of which carried a position.
PAPER_PERIOD: Final[Mapping[SleeveGroup, tuple[int, int]]] = {
    SleeveGroup.O1M: (6, 3),
    SleeveGroup.O1W: (12, 6),
    SleeveGroup.O2: (60, 25),
    SleeveGroup.O3: (20, 8),
}

#: Session states that end a session — a skipped day counts toward the paper period (``02`` §3.2).
OVER_STATES: Final = ("CLOSED", "LAPSED", "SKIPPED")

#: Why the tab has no scan to show (``empty_reason``), most fundamental first.
EMPTY_COLLECTOR_OFF: Final = "collector_off"
EMPTY_SCAN_OFF: Final = "scan_off"
EMPTY_NO_SCAN_YET: Final = "no_scan_yet_today"
EMPTY_NEVER: Final = "never_scanned"

#: The ``op_event_day`` source a person's day carries; seeded days are ``SEED``.
USER_SOURCE: Final = "USER"


# --- small helpers -------------------------------------------------------------------------------


def _ist(value: dt.datetime) -> dt.datetime:
    return value.astimezone(IST)


def _stub(expiry: dt.date, lot_size: int) -> Contract:
    """``calendar.role`` asks the master's expiries; ``op_expiry`` *is* those expiries (OP2's
    nightly rebuild), so one stub contract per expiry answers the same question without
    loading the ~thousand-row master for a page header (DECISIONS-OP OP5.4)."""
    return Contract(
        instrument_token=0,
        tradingsymbol="",
        underlying=UNDERLYING,
        expiry=expiry,
        strike=Decimal(0),
        option_type=OptionType.CE,
        lot_size=lot_size,
        tick_size=Decimal("0.05"),
    )


def _live_expiry(row: OpExpiry) -> bool:
    """A withdrawn expiry (a holiday shift) is not an expiry any more (OP2.7)."""
    return "withdrawn_on" not in (row.detail or {})


# --- the header ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExpiryView:
    expiry_date: dt.date
    kind: str
    lot_size: int
    event_day: bool
    event_reason: str | None


@dataclass(frozen=True, slots=True)
class RoleView:
    sleeve: str
    today: bool
    reason: str
    next_date: dt.date | None


@dataclass(frozen=True, slots=True)
class IndexLevel:
    symbol: str
    level: Decimal | None
    #: The minute bar's timestamp, or null when the level is a daily close.
    at: dt.datetime | None
    #: The daily close's session when the level is not a minute bar.
    close_of: dt.date | None


@dataclass(frozen=True, slots=True)
class PauseView:
    scope: str
    paused_until: dt.date | None
    paused_reason: str | None


@dataclass(frozen=True, slots=True)
class ScanRowView:
    sleeve: str
    trade_date: dt.date
    ts: dt.datetime
    state: str
    reasons: tuple[str, ...]
    numbers: dict[str, object]
    candidates: list[object]
    as_of_minute: dt.datetime | None
    stale: bool


@dataclass(frozen=True, slots=True)
class PositionView:
    session_id: int
    sleeve: str
    trade_date: dt.date
    lots: int
    entry_points: Decimal
    entry_inr: Decimal
    opened_at: dt.datetime
    hard_exit_at: dt.datetime
    last_mark_points: Decimal | None
    last_mark_at: dt.datetime | None
    minutes_to_hard_exit: int | None
    simulated: bool


@dataclass(frozen=True, slots=True)
class ClosedTradeView:
    session_id: int
    sleeve: str
    trade_date: dt.date
    structure: str
    net_pnl_inr: Decimal
    r_multiple: Decimal
    closed_reason: str
    minutes_held: int
    simulated: bool
    sizing_mode: str


@dataclass(frozen=True, slots=True)
class WeekR:
    sleeve: str
    simulated: bool
    r: Decimal
    trades: int


@dataclass(frozen=True, slots=True)
class TodayView:
    today: dt.date
    session_day: bool
    market_open: bool
    #: The session the scan rows are from — today while it is running, else the last scanned.
    scan_date: dt.date | None
    #: The newest minute the collector wrote among those rows.
    as_of_minute: dt.datetime | None
    live: bool
    stale: bool
    empty_reason: str | None
    collect_enabled: bool
    scan_enabled: bool
    roles: list[RoleView]
    expiries: list[ExpiryView]
    pauses: list[PauseView]
    nifty: IndexLevel
    vix: IndexLevel
    scans: list[ScanRowView]
    positions: list[PositionView]
    closed_today: list[ClosedTradeView]
    week_r: list[WeekR]


async def _event_days(
    session: AsyncSession, user_id: int, *, start: dt.date | None = None
) -> dict[dt.date, OpEventDay]:
    query = select(OpEventDay).where(OpEventDay.user_id == user_id)
    if start is not None:
        query = query.where(OpEventDay.date >= start)
    return {row.date: row for row in (await session.execute(query)).scalars()}


async def _expiry_rows(
    session: AsyncSession, *, start: dt.date, end: dt.date | None = None
) -> list[OpExpiry]:
    query = select(OpExpiry).where(OpExpiry.underlying == UNDERLYING, OpExpiry.expiry_date >= start)
    if end is not None:
        query = query.where(OpExpiry.expiry_date <= end)
    rows = (await session.execute(query.order_by(OpExpiry.expiry_date))).scalars()
    return [row for row in rows if _live_expiry(row)]


def roles_for(
    day: dt.date,
    *,
    session_day: bool,
    expiries: Sequence[OpExpiry],
    event_days: Iterable[dt.date],
) -> list[RoleView]:
    """Today's role per sleeve (``04`` §1.2) and, for the expiry sleeves, the next date."""
    contracts = [_stub(row.expiry_date, row.lot_size) for row in expiries]
    events = set(event_days)
    out: list[RoleView] = []
    for sleeve in DISPLAY_ORDER:
        verdict = role(day, sleeve, rows=contracts, event_days=events, trading_day=session_day)
        upcoming = (
            None
            if sleeve is Sleeve.O2
            else next_session(day, sleeve, rows=contracts, event_days=events)
        )
        out.append(RoleView(sleeve.value, verdict.trades, verdict.reason.value, upcoming))
    return out


async def _minute_level(session: AsyncSession, symbol: str, day: dt.date) -> IndexLevel | None:
    start = dt.datetime.combine(day, dt.time(0, 0), tzinfo=IST)
    row = (
        await session.execute(
            select(OpIndexMinute.close, OpIndexMinute.ts)
            .join(Instrument, Instrument.id == OpIndexMinute.instrument_id)
            .where(Instrument.symbol == symbol, OpIndexMinute.ts >= start)
            .order_by(OpIndexMinute.ts.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return IndexLevel(symbol=symbol, level=Decimal(row[0]), at=row[1], close_of=None)


async def _daily_level(session: AsyncSession, symbol: str, slug: str, day: dt.date) -> IndexLevel:
    row = (
        await session.execute(
            select(IndexSnapshotDaily.level, IndexSnapshotDaily.date)
            .join(IndexDef, IndexDef.id == IndexSnapshotDaily.index_id)
            .where(
                IndexDef.slug == slug,
                IndexSnapshotDaily.date <= day,
                IndexSnapshotDaily.level.is_not(None),
            )
            .order_by(IndexSnapshotDaily.date.desc())
            .limit(1)
        )
    ).first()
    if row is None or row[0] is None:
        return IndexLevel(symbol=symbol, level=None, at=None, close_of=None)
    return IndexLevel(symbol=symbol, level=Decimal(row[0]), at=None, close_of=row[1])


async def index_level(session: AsyncSession, symbol: str, slug: str, day: dt.date) -> IndexLevel:
    """The collector's last minute bar of ``day``, else the last daily close on or before it."""
    minute = await _minute_level(session, symbol, day)
    return minute if minute is not None else await _daily_level(session, symbol, slug, day)


async def latest_scan_date(
    session: AsyncSession, user_id: int, on_or_before: dt.date
) -> dt.date | None:
    value = (
        await session.execute(
            select(func.max(OpScan.trade_date)).where(
                OpScan.user_id == user_id, OpScan.trade_date <= on_or_before
            )
        )
    ).scalar_one_or_none()
    return value


def _scan_view(row: OpScan) -> ScanRowView:
    # The column is typed as a JSON object and holds a JSON array (OP4's note on the same
    # column); widen before narrowing so the check is not "unreachable" to the type checker.
    raw: object = row.candidates
    candidates: list[object] = list(raw) if isinstance(raw, list) else []
    return ScanRowView(
        sleeve=str(row.sleeve),
        trade_date=row.trade_date,
        ts=row.ts,
        state=row.state,
        reasons=tuple(row.reasons or ()),
        numbers=dict(row.numbers or {}),
        candidates=candidates,
        as_of_minute=row.as_of_minute,
        stale=row.stale,
    )


async def latest_scans(session: AsyncSession, user_id: int, day: dt.date) -> list[ScanRowView]:
    """The newest ``op_scan`` row per sleeve on ``day``, in display order."""
    newest = (
        select(OpScan.sleeve, func.max(OpScan.ts).label("ts"))
        .where(OpScan.user_id == user_id, OpScan.trade_date == day)
        .group_by(OpScan.sleeve)
        .subquery()
    )
    rows = (
        await session.execute(
            select(OpScan)
            .join(
                newest,
                and_(OpScan.sleeve == newest.c.sleeve, OpScan.ts == newest.c.ts),
            )
            .where(OpScan.user_id == user_id, OpScan.trade_date == day)
        )
    ).scalars()
    by_sleeve = {str(row.sleeve): _scan_view(row) for row in rows}
    return [by_sleeve[s.value] for s in DISPLAY_ORDER if s.value in by_sleeve]


async def scan_history(
    session: AsyncSession, user_id: int, sleeve: Sleeve, day: dt.date
) -> list[ScanRowView]:
    rows = (
        await session.execute(
            select(OpScan)
            .where(
                OpScan.user_id == user_id,
                OpScan.sleeve == sleeve.value,
                OpScan.trade_date == day,
            )
            .order_by(OpScan.ts)
        )
    ).scalars()
    return [_scan_view(row) for row in rows]


async def pauses(session: AsyncSession, user_id: int) -> list[PauseView]:
    out: list[PauseView] = []
    book = await session.get(OpBookConfig, user_id)
    if book is not None:
        out.append(PauseView("BOOK", book.paused_until, book.paused_reason))
    sleeves = (
        await session.execute(
            select(OpSleeveConfig)
            .where(OpSleeveConfig.user_id == user_id)
            .order_by(OpSleeveConfig.sleeve)
        )
    ).scalars()
    out.extend(PauseView(row.sleeve, row.paused_until, row.paused_reason) for row in sleeves)
    return out


async def open_positions(
    session: AsyncSession, user_id: int, now: dt.datetime
) -> list[PositionView]:
    rows = (
        await session.execute(
            select(OpPosition, OpSession.sleeve, OpSession.trade_date)
            .join(OpSession, OpSession.id == OpPosition.session_id)
            .where(OpPosition.user_id == user_id, OpPosition.closed_at.is_(None))
            .order_by(OpPosition.opened_at)
        )
    ).all()
    out: list[PositionView] = []
    for position, sleeve, trade_date in rows:
        left = int((position.hard_exit_at - now).total_seconds() // 60)
        out.append(
            PositionView(
                session_id=position.session_id,
                sleeve=str(sleeve),
                trade_date=trade_date,
                lots=position.lots,
                entry_points=position.entry_points,
                entry_inr=position.entry_inr,
                opened_at=position.opened_at,
                hard_exit_at=position.hard_exit_at,
                last_mark_points=position.last_mark_points,
                last_mark_at=position.last_mark_at,
                minutes_to_hard_exit=max(left, 0),
                simulated=position.simulated,
            )
        )
    return out


def _closed_view(row: OpJournal) -> ClosedTradeView:
    return ClosedTradeView(
        session_id=row.session_id,
        sleeve=str(row.sleeve),
        trade_date=row.trade_date,
        structure=row.structure,
        net_pnl_inr=row.net_pnl_inr,
        r_multiple=row.r_multiple,
        closed_reason=row.closed_reason,
        minutes_held=row.minutes_held,
        simulated=row.simulated,
        sizing_mode=row.sizing_mode,
    )


async def closed_on(session: AsyncSession, user_id: int, day: dt.date) -> list[ClosedTradeView]:
    rows = (
        await session.execute(
            select(OpJournal)
            .where(OpJournal.user_id == user_id, OpJournal.trade_date == day)
            .order_by(OpJournal.sleeve)
        )
    ).scalars()
    return [_closed_view(row) for row in rows]


async def week_r(session: AsyncSession, user_id: int, day: dt.date) -> list[WeekR]:
    """Each sleeve's running R for ``day``'s ISO week — real and simulated never pooled."""
    monday = day - dt.timedelta(days=day.weekday())
    rows = (
        await session.execute(
            select(
                OpJournal.sleeve,
                OpJournal.simulated,
                func.sum(OpJournal.r_multiple),
                func.count(),
            )
            .where(
                OpJournal.user_id == user_id,
                OpJournal.trade_date >= monday,
                OpJournal.trade_date <= day,
            )
            .group_by(OpJournal.sleeve, OpJournal.simulated)
        )
    ).all()
    return sorted(
        (
            WeekR(str(sleeve), bool(simulated), Decimal(total or 0), int(count))
            for sleeve, simulated, total, count in rows
        ),
        key=lambda w: (w.sleeve, w.simulated),
    )


def empty_reason(  # noqa: PLR0913 - the rows, their date, today, and the three switches
    *,
    scans: Sequence[ScanRowView],
    scan_date: dt.date | None,
    today: dt.date,
    session_day: bool,
    collect_enabled: bool,
    scan_enabled: bool,
) -> str | None:
    """Why there is no scan for today to show — ``None`` when today's rows are on the page.

    The flags come first: with the collector off there is no chain for a scan to read, so "no
    scan yet" would blame the clock for a switch. Then "no scan yet today" on a session day, and
    "never" when the table has nothing at all.
    """
    if scans and scan_date == today:
        return None
    if not collect_enabled:
        return EMPTY_COLLECTOR_OFF
    if not scan_enabled:
        return EMPTY_SCAN_OFF
    if session_day:
        return EMPTY_NO_SCAN_YET
    return None if scans else EMPTY_NEVER


async def today_view(  # noqa: PLR0913 - the session, the tenant, the clock and the two flags
    session: AsyncSession,
    *,
    user_id: int,
    now: dt.datetime,
    session_day: bool,
    market_open: bool,
    collect_enabled: bool,
    scan_enabled: bool,
) -> TodayView:
    today = _ist(now).date()
    scan_date = await latest_scan_date(session, user_id, today)
    scans = await latest_scans(session, user_id, scan_date) if scan_date is not None else []
    minutes = [row.as_of_minute for row in scans if row.as_of_minute is not None]
    as_of = max(minutes) if minutes else (max(row.ts for row in scans) if scans else None)
    live = market_open and scan_date == today and bool(scans)
    stale = live and as_of is not None and now - as_of > STALE_AFTER
    events = await _event_days(session, user_id)
    upcoming = await _expiry_rows(session, start=today)
    return TodayView(
        today=today,
        session_day=session_day,
        market_open=market_open,
        scan_date=scan_date,
        as_of_minute=as_of,
        live=live,
        stale=stale,
        empty_reason=empty_reason(
            scans=scans,
            scan_date=scan_date,
            today=today,
            session_day=session_day,
            collect_enabled=collect_enabled,
            scan_enabled=scan_enabled,
        ),
        collect_enabled=collect_enabled,
        scan_enabled=scan_enabled,
        roles=roles_for(today, session_day=session_day, expiries=upcoming, event_days=events),
        expiries=[
            ExpiryView(
                expiry_date=row.expiry_date,
                kind=row.kind,
                lot_size=row.lot_size,
                event_day=row.expiry_date in events,
                event_reason=events[row.expiry_date].reason if row.expiry_date in events else None,
            )
            for row in upcoming[:HEADER_EXPIRIES]
        ],
        pauses=await pauses(session, user_id),
        nifty=await index_level(session, NIFTY_50, NIFTY_50_SLUG, today),
        vix=await index_level(session, INDIA_VIX, INDIA_VIX_SLUG, today),
        scans=scans,
        positions=await open_positions(session, user_id, now),
        closed_today=await closed_on(session, user_id, scan_date or today),
        week_r=await week_r(session, user_id, today),
    )


# --- the chain -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChainRowView:
    strike: Decimal
    option_type: str
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    oi: int | None
    oi_change: int | None
    iv: Decimal | None
    delta: Decimal | None
    gamma: Decimal | None
    theta: Decimal | None


@dataclass(frozen=True, slots=True)
class ChainExpiryView:
    expiry: dt.date
    ts: dt.datetime
    spot: Decimal | None
    forward: Decimal | None
    atm_strike: Decimal | None
    atm_iv: Decimal | None
    #: Put OI over call OI across the strikes served. A number, no chart (``05`` §2, v1).
    pcr_oi: Decimal | None
    rows: list[ChainRowView] = field(default_factory=list)


def _atm(strikes: Sequence[Decimal], reference: Decimal | None) -> Decimal | None:
    if not strikes or reference is None:
        return None
    return min(strikes, key=lambda k: (abs(k - reference), k))


async def _chain_for(
    session: AsyncSession, expiry: dt.date, *, day_start: dt.datetime
) -> ChainExpiryView | None:
    latest = (
        await session.execute(
            select(func.max(OpChainSnapshot.ts)).where(
                OpChainSnapshot.expiry == expiry, OpChainSnapshot.ts >= day_start
            )
        )
    ).scalar_one_or_none()
    if latest is None:
        latest = (
            await session.execute(
                select(func.max(OpChainSnapshot.ts)).where(OpChainSnapshot.expiry == expiry)
            )
        ).scalar_one_or_none()
    if latest is None:
        return None
    rows = list(
        (
            await session.execute(
                select(OpChainSnapshot).where(
                    OpChainSnapshot.expiry == expiry, OpChainSnapshot.ts == latest
                )
            )
        ).scalars()
    )
    session_start = dt.datetime.combine(_ist(latest).date(), dt.time(0, 0), tzinfo=IST)
    first_ts = (
        await session.execute(
            select(func.min(OpChainSnapshot.ts)).where(
                OpChainSnapshot.expiry == expiry, OpChainSnapshot.ts >= session_start
            )
        )
    ).scalar_one_or_none()
    opening: dict[int, int | None] = {}
    if first_ts is not None and first_ts != latest:
        opening = {
            token: oi
            for token, oi in (
                await session.execute(
                    select(OpChainSnapshot.instrument_token, OpChainSnapshot.oi).where(
                        OpChainSnapshot.expiry == expiry, OpChainSnapshot.ts == first_ts
                    )
                )
            ).all()
        }
    spot = next((r.spot for r in rows if r.spot is not None), None)
    forward = next((r.forward for r in rows if r.forward is not None), None)
    strikes = sorted({r.strike for r in rows})
    atm = _atm(strikes, forward if forward is not None else spot)
    if atm is not None:
        index = strikes.index(atm)
        lo = max(index - CHAIN_STRIKES_EACH_SIDE, 0)
        keep = set(strikes[lo : index + CHAIN_STRIKES_EACH_SIDE + 1])
        rows = [r for r in rows if r.strike in keep]
    rows.sort(key=lambda r: (r.strike, r.option_type))
    atm_ivs = [r.iv for r in rows if r.strike == atm and r.iv is not None]
    put_oi = sum(r.oi or 0 for r in rows if r.option_type == OptionType.PE.value)
    call_oi = sum(r.oi or 0 for r in rows if r.option_type == OptionType.CE.value)
    return ChainExpiryView(
        expiry=expiry,
        ts=latest,
        spot=spot,
        forward=forward,
        atm_strike=atm,
        atm_iv=(sum(atm_ivs, Decimal(0)) / len(atm_ivs)).quantize(Decimal("0.000001"))
        if atm_ivs
        else None,
        pcr_oi=(Decimal(put_oi) / Decimal(call_oi)).quantize(Decimal("0.0001"))
        if call_oi
        else None,
        rows=[
            ChainRowView(
                strike=r.strike,
                option_type=r.option_type,
                bid=r.bid,
                ask=r.ask,
                last=r.last,
                oi=r.oi,
                oi_change=(
                    None
                    if r.oi is None or opening.get(r.instrument_token) is None
                    else r.oi - int(opening[r.instrument_token] or 0)
                ),
                iv=r.iv,
                delta=r.delta,
                gamma=r.gamma,
                theta=r.theta,
            )
            for r in rows
        ],
    )


async def chain(
    session: AsyncSession, *, now: dt.datetime, expiry: dt.date | None
) -> list[ChainExpiryView]:
    """The named expiry, or the nearest two live ones (``05`` §2's chain panel)."""
    today = _ist(now).date()
    day_start = dt.datetime.combine(today, dt.time(0, 0), tzinfo=IST)
    wanted = (
        [expiry]
        if expiry is not None
        else [row.expiry_date for row in (await _expiry_rows(session, start=today))[:2]]
    )
    out: list[ChainExpiryView] = []
    for one in wanted:
        view = await _chain_for(session, one, day_start=day_start)
        if view is not None:
            out.append(view)
    return out


# --- sessions, journal, backtest -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionView:
    id: int
    sleeve: str
    trade_date: dt.date
    expiry_used: dt.date | None
    mode: str
    state: str
    verdict: str | None
    skip_reasons: tuple[str, ...]
    closed_reason: str | None
    pnl_inr: Decimal | None
    pnl_r: Decimal | None
    slot_holder: str | None


async def sessions(
    session: AsyncSession,
    *,
    user_id: int,
    start: dt.date,
    end: dt.date,
    sleeve: Sleeve | None,
) -> list[SessionView]:
    query = select(OpSession).where(
        OpSession.user_id == user_id, OpSession.trade_date >= start, OpSession.trade_date <= end
    )
    if sleeve is not None:
        query = query.where(OpSession.sleeve == sleeve.value)
    rows = (await session.execute(query.order_by(OpSession.trade_date, OpSession.sleeve))).scalars()
    return [
        SessionView(
            id=row.id,
            sleeve=str(row.sleeve),
            trade_date=row.trade_date,
            expiry_used=row.expiry_used,
            mode=row.mode,
            state=row.state,
            verdict=row.verdict,
            skip_reasons=tuple(row.skip_reasons or ()),
            closed_reason=row.closed_reason,
            pnl_inr=row.pnl_inr,
            pnl_r=row.pnl_r,
            slot_holder=row.slot_holder,
        )
        for row in rows
    ]


@dataclass(frozen=True, slots=True)
class PaperProgress:
    group: str
    sessions_needed: int
    traded_needed: int
    sessions_done: int
    traded: int


@dataclass(frozen=True, slots=True)
class JournalView:
    summaries: tuple[Summary, ...]
    skips_by_reason: dict[str, dict[str, int]]
    progress: list[PaperProgress]
    #: ``tier3_min_sessions`` per sleeve: the sample banner shows while a sleeve is below it.
    min_sessions: dict[str, int]
    recent: list[ClosedTradeView]
    #: Each pool's R values in date order — the page's histogram, never merged across pools.
    r_values: dict[PoolKey, tuple[Decimal, ...]] = field(default_factory=dict)


def _journal_row(row: OpJournal) -> JournalRow:
    return JournalRow(
        sleeve=Sleeve(str(row.sleeve)),
        trade_date=row.trade_date,
        simulated=row.simulated,
        sizing_mode=SizingMode(row.sizing_mode),
        traded=True,
        net_pnl_inr=row.net_pnl_inr,
        r_inr=row.risk_budget_inr if row.risk_budget_inr > 0 else Decimal(1),
        closed_reason=row.closed_reason,
        minutes_held=row.minutes_held,
        mae_r=row.mae_r if row.mae_r is not None else Decimal(0),
        mfe_r=row.mfe_r if row.mfe_r is not None else Decimal(0),
    )


def _group(sleeve: str) -> SleeveGroup:
    return SleeveGroup.O3 if sleeve in (Sleeve.O3A.value, Sleeve.O3B.value) else SleeveGroup(sleeve)


async def journal(
    session: AsyncSession,
    *,
    user_id: int,
    sleeve: Sleeve | None,
    config: OptionsConfig = DEFAULT_OPTIONS_CONFIG,
) -> JournalView:
    """``04`` §12's summary per (sleeve, simulated, sizing mode) — never pooled (OP1.6)."""
    query = select(OpJournal).where(OpJournal.user_id == user_id)
    if sleeve is not None:
        query = query.where(OpJournal.sleeve == sleeve.value)
    rows = list(
        (await session.execute(query.order_by(OpJournal.trade_date, OpJournal.sleeve))).scalars()
    )
    session_query = select(OpSession).where(
        OpSession.user_id == user_id, OpSession.state.in_(OVER_STATES)
    )
    if sleeve is not None:
        session_query = session_query.where(OpSession.sleeve == sleeve.value)
    over = list((await session.execute(session_query)).scalars())
    skips: dict[str, dict[str, int]] = defaultdict(dict)
    done: dict[SleeveGroup, int] = defaultdict(int)
    for ended in over:
        group = _group(str(ended.sleeve))
        if ended.mode == "PAPER":
            done[group] += 1
        if ended.state == "SKIPPED":
            reason = (ended.skip_reasons or ["UNSPECIFIED"])[0]
            bucket = skips[str(ended.sleeve)]
            bucket[reason] = bucket.get(reason, 0) + 1
    traded: dict[SleeveGroup, int] = defaultdict(int)
    for row in rows:
        if row.simulated:
            traded[_group(str(row.sleeve))] += 1
    groups = [_group(sleeve.value)] if sleeve is not None else list(PAPER_PERIOD)
    progress = [
        PaperProgress(
            group=group.value,
            sessions_needed=PAPER_PERIOD[group][0],
            traded_needed=PAPER_PERIOD[group][1],
            sessions_done=done[group],
            traded=traded[group],
        )
        for group in groups
    ]
    journal_rows = [_journal_row(row) for row in rows]
    r_values: dict[PoolKey, list[Decimal]] = defaultdict(list)
    for journal_row in journal_rows:
        r_values[key_of(journal_row)].append(journal_row.r)
    return JournalView(
        summaries=summarize(journal_rows),
        r_values={key: tuple(values) for key, values in r_values.items()},
        skips_by_reason={key: dict(value) for key, value in skips.items()},
        progress=progress,
        min_sessions={s.value: config.tier3_min_sessions(s) for s in DISPLAY_ORDER},
        recent=[_closed_view(row) for row in reversed(rows[-20:])],
    )


@dataclass(frozen=True, slots=True)
class BacktestRunView:
    id: int
    sleeve: str
    tier: int
    date_from: dt.date
    date_to: dt.date
    sessions: int
    signals: int
    traded: int
    skipped_by_reason: dict[str, object]
    win_rate: Decimal | None
    expectancy_r: Decimal | None
    net_pnl_inr: Decimal | None
    max_drawdown_r: Decimal | None
    #: Verbatim from the row (``07`` §4) — the page prints this, never its own paraphrase.
    caveats: str
    ran_at: dt.datetime


async def backtests(
    session: AsyncSession, *, user_id: int, sleeve: Sleeve | None
) -> list[BacktestRunView]:
    """The newest run per (sleeve, tier). Tiers are never pooled; an empty list is "not run"."""
    newest = (
        select(
            OpBacktestRun.sleeve,
            OpBacktestRun.tier,
            func.max(OpBacktestRun.id).label("id"),
        )
        .where(OpBacktestRun.user_id == user_id)
        .group_by(OpBacktestRun.sleeve, OpBacktestRun.tier)
        .subquery()
    )
    query = select(OpBacktestRun).join(newest, OpBacktestRun.id == newest.c.id)
    if sleeve is not None:
        query = query.where(OpBacktestRun.sleeve == sleeve.value)
    rows = (
        await session.execute(query.order_by(OpBacktestRun.sleeve, OpBacktestRun.tier))
    ).scalars()
    return [
        BacktestRunView(
            id=row.id,
            sleeve=str(row.sleeve),
            tier=row.tier,
            date_from=row.date_from,
            date_to=row.date_to,
            sessions=row.sessions,
            signals=row.signals,
            traded=row.traded,
            skipped_by_reason=dict(row.skipped_by_reason_json or {}),
            win_rate=row.win_rate,
            expectancy_r=row.expectancy_r,
            net_pnl_inr=row.net_pnl_inr,
            max_drawdown_r=row.max_drawdown_r,
            caveats=row.caveats,
            ran_at=row.ran_at,
        )
        for row in rows
    ]


# --- the calendar and its one write --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EventDayView:
    date: dt.date
    reason: str
    source: str
    source_url: str | None
    note: str | None
    #: Only a person's own day may be removed from the web; a seeded day is a verified fact.
    removable: bool


@dataclass(frozen=True, slots=True)
class CalendarView:
    year: int
    expiries: list[ExpiryView]
    event_days: list[EventDayView]


def _event_view(row: OpEventDay) -> EventDayView:
    return EventDayView(
        date=row.date,
        reason=row.reason,
        source=row.source,
        source_url=row.source_url,
        note=row.note,
        removable=row.source == USER_SOURCE,
    )


async def calendar(session: AsyncSession, *, user_id: int, year: int) -> CalendarView:
    start, end = dt.date(year, 1, 1), dt.date(year, 12, 31)
    events = {
        day: row
        for day, row in (await _event_days(session, user_id, start=start)).items()
        if day <= end
    }
    expiries = await _expiry_rows(session, start=start, end=end)
    return CalendarView(
        year=year,
        expiries=[
            ExpiryView(
                expiry_date=row.expiry_date,
                kind=row.kind,
                lot_size=row.lot_size,
                event_day=row.expiry_date in events,
                event_reason=events[row.expiry_date].reason if row.expiry_date in events else None,
            )
            for row in expiries
        ],
        event_days=[_event_view(events[day]) for day in sorted(events)],
    )


class EventDayConflict(ValueError):
    """The day already exists, or is a seeded day the web may not remove."""


async def add_event_day(
    session: AsyncSession, *, user_id: int, day: dt.date, reason: str, note: str | None
) -> EventDayView:
    existing = await session.get(OpEventDay, (user_id, day))
    if existing is not None:
        raise EventDayConflict(f"{day.isoformat()} is already an event day ({existing.reason})")
    row = OpEventDay(user_id=user_id, date=day, reason=reason, source=USER_SOURCE, note=note)
    session.add(row)
    await session.flush()
    return _event_view(row)


async def remove_event_day(session: AsyncSession, *, user_id: int, day: dt.date) -> bool:
    """``False`` when there is no such day; raises for a seeded one."""
    row = await session.get(OpEventDay, (user_id, day))
    if row is None:
        return False
    if row.source != USER_SOURCE:
        raise EventDayConflict(
            f"{day.isoformat()} is a seeded, source-verified day ({row.reason}); "
            "it is removed by a DECISIONS-OP entry, not from the web"
        )
    await session.delete(row)
    await session.flush()
    return True


# --- the thresholds, read-only -------------------------------------------------------------------

#: Each ``OptionsConfig`` section and the ``04`` anchor that owns it.
SECTION_ANCHORS: Final[Mapping[str, str]] = {
    "calendar": "04 §1",
    "chain": "04 §2",
    "condor_monthly": "04 §3",
    "condor_weekly": "04 §3",
    "directional": "04 §4",
    "expiry_setups": "04 §5",
    "costs": "04 §6",
    "sizing": "04 §7",
    "execution": "04 §8",
    "risk": "04 §9",
}


@dataclass(frozen=True, slots=True)
class ThresholdView:
    section: str
    key: str
    value: str
    anchor: str


def thresholds(config: OptionsConfig = DEFAULT_OPTIONS_CONFIG) -> list[ThresholdView]:
    """Every ``04`` field, as the string it is — read-only; a change is a DECISIONS-OP entry."""
    out: list[ThresholdView] = []
    for section in dataclasses.fields(config):
        value = getattr(config, section.name)
        if not dataclasses.is_dataclass(value):
            continue
        anchor = SECTION_ANCHORS.get(section.name, "04")
        for item in dataclasses.fields(value):
            raw = getattr(value, item.name)
            text = raw.isoformat() if isinstance(raw, dt.time) else str(raw)
            out.append(ThresholdView(section.name, item.name, text, anchor))
    return out
