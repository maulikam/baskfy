"""``/options/*`` — the NIFTY options run's read surface, and its two money-free writes (OP5).

    GET    /options/today                  roles, expiries, pauses, index levels, the five scans,
                                           open positions, today's closed trades, the week's R
    GET    /options/scan/{sleeve}          one sleeve's scan rows through a session
    GET    /options/chain?expiry           the nearest two expiries' strikes around ATM
    GET    /options/positions              open positions and today's closed trades
    GET    /options/sessions?from&to&sleeve
    GET    /options/journal?sleeve         ``04`` §12's summary, never pooled; paper progress
    GET    /options/backtest?sleeve        the newest run per sleeve per tier, caveat on the row
    GET    /options/calendar?year          the year's expiries and event days
    POST   /options/event-day              add a person's event day      (mutation 1 of 2)
    DELETE /options/event-day?date         remove a person's event day   (mutation 1 of 2)
    GET    /options/config                 settings, ceilings, every ``04`` threshold read-only
    PATCH  /options/config                 the settings                  (mutation 2 of 2)

``docs/options/05`` §2 names exactly these, "no other verb on any path", and
``test_options_readonly.py`` asserts it from both sides of the wire.

READ-ONLY WHERE MONEY IS CONCERNED
----------------------------------
There is no execute, confirm, plan or close route here and there is not going to be one: the desk
console's ``/nifty-options`` is the only surface with a Confirm (``05`` §3, ``02`` Track C §4). This
module does not import the execution package, names no broker, and reads the execution flags only
to *report* them (the settings page's "Execution: disabled on this server"). The event day and the
settings are the two writes ``05`` allows; neither can place, size or confirm anything — the
settings patch is ``baskfy_api.options_settings`` with its ceilings, unchanged from OP2.

THE CLOCK
---------
``live`` is true only while the NSE session is open **and** the rows are today's; the page labels
them ``Live · HH:MM`` (amber ``stale`` beyond two minutes) or ``As of close``. Nothing here reaches
Kite: the collector is the one reader of the chain (``05`` §2).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from baskfy_api import options_read as reads
from baskfy_api.auth import AuthenticatedDep, settings_for
from baskfy_api.curated_tenant import scoped_sole_user_id
from baskfy_api.db import SessionDep
from baskfy_api.options_settings import (
    OptionsBookPatch,
    OptionsConfigNotSeeded,
    OptionsSleevePatch,
    apply_book_patch,
    apply_sleeve_patch,
    audit_trail,
    ceilings_from_settings,
    check_book,
    check_sleeve,
    read_book,
    read_sleeve,
)
from baskfy_api.problems import bad_request, not_found
from baskfy_api.settings import Settings
from baskfy_api.swing_health import is_session_day
from baskfy_core.market_hours_cb import IST, is_nse_session_open
from baskfy_core.models.options import OP_EVENT_REASONS
from baskfy_core.options.config import Sleeve, SleeveGroup

router = APIRouter(prefix="/options", tags=["options"])

JSON_MEDIA_TYPE: Final = "application/json"
_SATURDAY: Final = 5
#: The longest window ``/options/sessions`` serves in one request.
MAX_SESSION_WINDOW_DAYS: Final = 400


def _settings(request: Request) -> Settings:
    return settings_for(request)


SettingsDep = Annotated[Settings, Depends(_settings)]


def _now() -> dt.datetime:
    """Seam for tests — production uses wall-clock IST."""
    return dt.datetime.now(tz=IST)


def _json(model: BaseModel, *, status_code: int = 200) -> Response:
    """Pydantic's own JSON, in which a ``Decimal`` is a quoted string — the web app does its
    arithmetic on decimal strings and ``JSON.parse`` of a bare token is a double (the reason
    ``routers/twt._json_decimals_as_strings`` exists; house rule 9)."""
    return Response(
        content=model.model_dump_json(), media_type=JSON_MEDIA_TYPE, status_code=status_code
    )


# --- response models -----------------------------------------------------------------------------


class OptionsExpiryOut(BaseModel):
    expiry_date: dt.date
    kind: str
    lot_size: int
    event_day: bool
    event_reason: str | None


class OptionsRoleOut(BaseModel):
    sleeve: str
    today: bool
    #: ``04`` §1.2's reason: TRADES, NOT_EXPIRY, NOT_MONTHLY, MONTHLY_EXPIRY, EVENT_DAY, …
    reason: str
    next_date: dt.date | None


class OptionsIndexOut(BaseModel):
    symbol: str
    level: Decimal | None
    #: The collector's minute, or null when ``level`` is a daily close.
    at: dt.datetime | None
    close_of: dt.date | None


class OptionsPauseOut(BaseModel):
    scope: str
    paused_until: dt.date | None
    paused_reason: str | None


class OptionsScanOut(BaseModel):
    """One ``op_scan`` row. ``numbers`` and ``candidates`` are the JSONB the worker wrote, passed
    through: every money figure in them is already a decimal string (OP4), and re-rendering them
    here would round a second time."""

    sleeve: str
    trade_date: dt.date
    ts: dt.datetime
    state: str
    reasons: list[str]
    numbers: dict[str, object]
    candidates: list[object]
    as_of_minute: dt.datetime | None
    stale: bool


class OptionsPositionOut(BaseModel):
    session_id: int
    sleeve: str
    trade_date: dt.date
    lots: int
    entry_points: Decimal
    entry_inr: Decimal
    opened_at: dt.datetime
    hard_exit_at: dt.datetime
    #: The desk's persisted mark (every 30 s) — labelled ``Marked HH:MM:SS`` on the page.
    last_mark_points: Decimal | None
    last_mark_at: dt.datetime | None
    minutes_to_hard_exit: int | None
    simulated: bool


class OptionsClosedOut(BaseModel):
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


class OptionsWeekROut(BaseModel):
    sleeve: str
    simulated: bool
    r: Decimal
    trades: int


class OptionsGateOut(BaseModel):
    """Per sleeve group: ``PAPER`` while its execution flag is off on this server. When it is on,
    the desk's own switches decide, and the API cannot see them — ``DESK_DECIDES``, never a guess
    of ``LIVE`` (DECISIONS-OP OP5.5)."""

    group: str
    execution_enabled: bool
    mode: str


class OptionsTodayOut(BaseModel):
    today: dt.date
    session_day: bool
    market_open: bool
    scan_date: dt.date | None
    as_of_minute: dt.datetime | None
    live: bool
    stale: bool
    #: ``collector_off`` | ``scan_off`` | ``no_scan_yet_today`` | ``never_scanned`` | null.
    empty_reason: str | None
    collect_enabled: bool
    scan_enabled: bool
    gates: list[OptionsGateOut]
    roles: list[OptionsRoleOut]
    expiries: list[OptionsExpiryOut]
    pauses: list[OptionsPauseOut]
    nifty: OptionsIndexOut
    vix: OptionsIndexOut
    scans: list[OptionsScanOut]
    positions: list[OptionsPositionOut]
    closed_today: list[OptionsClosedOut]
    week_r: list[OptionsWeekROut]


class OptionsScanHistoryOut(BaseModel):
    sleeve: str
    trade_date: dt.date | None
    rows: list[OptionsScanOut]


class OptionsChainRowOut(BaseModel):
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


class OptionsChainExpiryOut(BaseModel):
    expiry: dt.date
    ts: dt.datetime
    spot: Decimal | None
    forward: Decimal | None
    atm_strike: Decimal | None
    atm_iv: Decimal | None
    pcr_oi: Decimal | None
    rows: list[OptionsChainRowOut]


class OptionsChainOut(BaseModel):
    expiries: list[OptionsChainExpiryOut]


class OptionsPositionsOut(BaseModel):
    positions: list[OptionsPositionOut]
    closed_today: list[OptionsClosedOut]


class OptionsSessionOut(BaseModel):
    id: int
    sleeve: str
    trade_date: dt.date
    expiry_used: dt.date | None
    mode: str
    state: str
    verdict: str | None
    skip_reasons: list[str]
    closed_reason: str | None
    pnl_inr: Decimal | None
    pnl_r: Decimal | None
    slot_holder: str | None


class OptionsSessionsOut(BaseModel):
    sessions: list[OptionsSessionOut]


class OptionsBucketOut(BaseModel):
    label: str
    count: int
    mean_r: Decimal


class OptionsSummaryOut(BaseModel):
    """One pool of ``04`` §12: a (sleeve, simulated, sizing mode) key, never merged with another."""

    sleeve: str
    simulated: bool
    sizing_mode: str
    count: int
    traded: int
    win_rate: Decimal | None
    mean_r: Decimal | None
    expectancy_inr: Decimal | None
    expectancy_r: Decimal | None
    worst_r: Decimal | None
    max_drawdown_r: Decimal
    max_drawdown_inr: Decimal
    r_values: list[Decimal]
    by_closed_reason: list[OptionsBucketOut]


class OptionsProgressOut(BaseModel):
    group: str
    sessions_needed: int
    traded_needed: int
    sessions_done: int
    traded: int


class OptionsJournalOut(BaseModel):
    summaries: list[OptionsSummaryOut]
    skips_by_reason: dict[str, dict[str, int]]
    progress: list[OptionsProgressOut]
    min_sessions: dict[str, int]
    recent: list[OptionsClosedOut]


class OptionsBacktestRunOut(BaseModel):
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
    caveats: str
    ran_at: dt.datetime


class OptionsBacktestOut(BaseModel):
    runs: list[OptionsBacktestRunOut]
    #: Null when ``runs`` is non-empty; otherwise why there is no number.
    reason: str | None


class OptionsEventDayOut(BaseModel):
    date: dt.date
    reason: str
    source: str
    source_url: str | None
    note: str | None
    removable: bool


class OptionsCalendarOut(BaseModel):
    year: int
    expiries: list[OptionsExpiryOut]
    event_days: list[OptionsEventDayOut]


class OptionsEventDayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: dt.date
    reason: str = "MANUAL"
    note: str | None = Field(default=None, max_length=280)


class OptionsBookOut(BaseModel):
    underlying: str
    account_inr: Decimal
    margin_pool_inr: Decimal
    daily_loss_limit_inr: Decimal
    monthly_pause_inr: Decimal
    paused_until: dt.date | None
    paused_reason: str | None


class OptionsSleeveConfigOut(BaseModel):
    sleeve: str
    sleeve_capital_inr: Decimal
    risk_per_trade_pct: Decimal
    max_lots: int
    paper_enabled: bool
    hard_exit_time: dt.time
    paused_until: dt.date | None
    paused_reason: str | None


class OptionsCeilingsOut(BaseModel):
    risk_per_trade_inr_max: Decimal
    risk_pct_max: Decimal
    max_lots_max: int
    book_daily_loss_inr_max: Decimal
    book_monthly_loss_inr_max: Decimal
    hard_exit_latest: dt.time


class OptionsThresholdOut(BaseModel):
    section: str
    key: str
    value: str
    anchor: str


class OptionsAuditOut(BaseModel):
    scope: str
    key: str
    old_value: str | None
    new_value: str | None
    changed_at: dt.datetime
    changed_by: str


class OptionsConfigOut(BaseModel):
    #: False until ``python -m baskfy_worker.options_cli seed`` has run for this tenant.
    seeded: bool
    book: OptionsBookOut | None
    sleeves: list[OptionsSleeveConfigOut]
    ceilings: OptionsCeilingsOut
    gates: list[OptionsGateOut]
    thresholds: list[OptionsThresholdOut]
    audit: list[OptionsAuditOut]


class OptionsConfigPatch(BaseModel):
    """One request, atomic: every ceiling is checked before anything is written."""

    model_config = ConfigDict(extra="forbid")

    book: OptionsBookPatch | None = None
    sleeves: dict[SleeveGroup, OptionsSleevePatch] = Field(default_factory=dict)


# --- helpers -------------------------------------------------------------------------------------


def _gates(settings: Settings) -> list[OptionsGateOut]:
    flags = {
        SleeveGroup.O1M: settings.options_o1m_execution_enabled,
        SleeveGroup.O1W: settings.options_o1w_execution_enabled,
        SleeveGroup.O2: settings.options_o2_execution_enabled,
        SleeveGroup.O3: settings.options_o3_execution_enabled,
    }
    return [
        OptionsGateOut(
            group=group.value,
            execution_enabled=enabled,
            mode="DESK_DECIDES" if enabled else "PAPER",
        )
        for group, enabled in flags.items()
    ]


def _scan_out(row: reads.ScanRowView) -> OptionsScanOut:
    return OptionsScanOut(
        sleeve=row.sleeve,
        trade_date=row.trade_date,
        ts=row.ts,
        state=row.state,
        reasons=list(row.reasons),
        numbers=row.numbers,
        candidates=row.candidates,
        as_of_minute=row.as_of_minute,
        stale=row.stale,
    )


def _position_out(row: reads.PositionView) -> OptionsPositionOut:
    return OptionsPositionOut(
        session_id=row.session_id,
        sleeve=row.sleeve,
        trade_date=row.trade_date,
        lots=row.lots,
        entry_points=row.entry_points,
        entry_inr=row.entry_inr,
        opened_at=row.opened_at,
        hard_exit_at=row.hard_exit_at,
        last_mark_points=row.last_mark_points,
        last_mark_at=row.last_mark_at,
        minutes_to_hard_exit=row.minutes_to_hard_exit,
        simulated=row.simulated,
    )


def _closed_out(row: reads.ClosedTradeView) -> OptionsClosedOut:
    return OptionsClosedOut(
        session_id=row.session_id,
        sleeve=row.sleeve,
        trade_date=row.trade_date,
        structure=row.structure,
        net_pnl_inr=row.net_pnl_inr,
        r_multiple=row.r_multiple,
        closed_reason=row.closed_reason,
        minutes_held=row.minutes_held,
        simulated=row.simulated,
        sizing_mode=row.sizing_mode,
    )


def _expiry_out(row: reads.ExpiryView) -> OptionsExpiryOut:
    return OptionsExpiryOut(
        expiry_date=row.expiry_date,
        kind=row.kind,
        lot_size=row.lot_size,
        event_day=row.event_day,
        event_reason=row.event_reason,
    )


def _index_out(row: reads.IndexLevel) -> OptionsIndexOut:
    return OptionsIndexOut(symbol=row.symbol, level=row.level, at=row.at, close_of=row.close_of)


def _sleeve(value: str) -> Sleeve:
    try:
        return Sleeve(value.upper())
    except ValueError:
        raise not_found("sleeve", value) from None


def _optional_sleeve(value: str | None) -> Sleeve | None:
    return None if value is None else _sleeve(value)


async def _market_state(session: SessionDep) -> tuple[dt.datetime, bool, bool]:
    now = _now()
    today = now.astimezone(IST).date()
    session_day = today.weekday() < _SATURDAY and await is_session_day(session, today)
    return now, session_day, is_nse_session_open(now, {today} if session_day else set())


# --- routes --------------------------------------------------------------------------------------


@router.get("/today", response_model=OptionsTodayOut, summary="The tab: roles, scans, book")
async def get_options_today(
    session: SessionDep, principal: AuthenticatedDep, settings: SettingsDep
) -> Response:
    """``05`` §2's header strip, the four panels and the clock, in one call."""
    user_id = await scoped_sole_user_id(session, principal.user_id)
    now, session_day, market_open = await _market_state(session)
    view = await reads.today_view(
        session,
        user_id=user_id,
        now=now,
        session_day=session_day,
        market_open=market_open,
        collect_enabled=settings.options_collect_enabled,
        scan_enabled=settings.options_scan_enabled,
    )
    return _json(
        OptionsTodayOut(
            today=view.today,
            session_day=view.session_day,
            market_open=view.market_open,
            scan_date=view.scan_date,
            as_of_minute=view.as_of_minute,
            live=view.live,
            stale=view.stale,
            empty_reason=view.empty_reason,
            collect_enabled=view.collect_enabled,
            scan_enabled=view.scan_enabled,
            gates=_gates(settings),
            roles=[
                OptionsRoleOut(
                    sleeve=r.sleeve, today=r.today, reason=r.reason, next_date=r.next_date
                )
                for r in view.roles
            ],
            expiries=[_expiry_out(e) for e in view.expiries],
            pauses=[
                OptionsPauseOut(
                    scope=p.scope, paused_until=p.paused_until, paused_reason=p.paused_reason
                )
                for p in view.pauses
            ],
            nifty=_index_out(view.nifty),
            vix=_index_out(view.vix),
            scans=[_scan_out(s) for s in view.scans],
            positions=[_position_out(p) for p in view.positions],
            closed_today=[_closed_out(c) for c in view.closed_today],
            week_r=[
                OptionsWeekROut(sleeve=w.sleeve, simulated=w.simulated, r=w.r, trades=w.trades)
                for w in view.week_r
            ],
        )
    )


@router.get(
    "/scan/{sleeve}", response_model=OptionsScanHistoryOut, summary="One sleeve's scan rows"
)
async def get_options_scan(
    session: SessionDep,
    principal: AuthenticatedDep,
    sleeve: str,
    date: Annotated[dt.date | None, Query(description="a scanned session; default the latest")] = (
        None
    ),
) -> Response:
    user_id = await scoped_sole_user_id(session, principal.user_id)
    code = _sleeve(sleeve)
    day = date or await reads.latest_scan_date(session, user_id, _now().date())
    rows = [] if day is None else await reads.scan_history(session, user_id, code, day)
    return _json(
        OptionsScanHistoryOut(sleeve=code.value, trade_date=day, rows=[_scan_out(r) for r in rows])
    )


@router.get("/chain", response_model=OptionsChainOut, summary="The chain around ATM")
async def get_options_chain(
    session: SessionDep,
    principal: AuthenticatedDep,
    expiry: Annotated[dt.date | None, Query(description="default: the nearest two")] = None,
) -> Response:
    await scoped_sole_user_id(session, principal.user_id)
    views = await reads.chain(session, now=_now(), expiry=expiry)
    return _json(
        OptionsChainOut(
            expiries=[
                OptionsChainExpiryOut(
                    expiry=v.expiry,
                    ts=v.ts,
                    spot=v.spot,
                    forward=v.forward,
                    atm_strike=v.atm_strike,
                    atm_iv=v.atm_iv,
                    pcr_oi=v.pcr_oi,
                    rows=[
                        OptionsChainRowOut(
                            strike=r.strike,
                            option_type=r.option_type,
                            bid=r.bid,
                            ask=r.ask,
                            last=r.last,
                            oi=r.oi,
                            oi_change=r.oi_change,
                            iv=r.iv,
                            delta=r.delta,
                            gamma=r.gamma,
                            theta=r.theta,
                        )
                        for r in v.rows
                    ],
                )
                for v in views
            ]
        )
    )


@router.get("/positions", response_model=OptionsPositionsOut, summary="Open and closed today")
async def get_options_positions(session: SessionDep, principal: AuthenticatedDep) -> Response:
    user_id = await scoped_sole_user_id(session, principal.user_id)
    now = _now()
    return _json(
        OptionsPositionsOut(
            positions=[_position_out(p) for p in await reads.open_positions(session, user_id, now)],
            closed_today=[
                _closed_out(c) for c in await reads.closed_on(session, user_id, now.date())
            ],
        )
    )


@router.get("/sessions", response_model=OptionsSessionsOut, summary="Sessions in a window")
async def get_options_sessions(
    session: SessionDep,
    principal: AuthenticatedDep,
    date_from: Annotated[dt.date | None, Query(alias="from")] = None,
    date_to: Annotated[dt.date | None, Query(alias="to")] = None,
    sleeve: str | None = None,
) -> Response:
    user_id = await scoped_sole_user_id(session, principal.user_id)
    end = date_to or _now().date()
    start = date_from or end - dt.timedelta(days=30)
    if start > end or (end - start).days > MAX_SESSION_WINDOW_DAYS:
        raise bad_request(
            f"from must be on or before to, and the window at most {MAX_SESSION_WINDOW_DAYS} days"
        )
    rows = await reads.sessions(
        session, user_id=user_id, start=start, end=end, sleeve=_optional_sleeve(sleeve)
    )
    return _json(
        OptionsSessionsOut(
            sessions=[
                OptionsSessionOut(
                    id=r.id,
                    sleeve=r.sleeve,
                    trade_date=r.trade_date,
                    expiry_used=r.expiry_used,
                    mode=r.mode,
                    state=r.state,
                    verdict=r.verdict,
                    skip_reasons=list(r.skip_reasons),
                    closed_reason=r.closed_reason,
                    pnl_inr=r.pnl_inr,
                    pnl_r=r.pnl_r,
                    slot_holder=r.slot_holder,
                )
                for r in rows
            ]
        )
    )


@router.get("/journal", response_model=OptionsJournalOut, summary="The journal, never pooled")
async def get_options_journal(
    session: SessionDep, principal: AuthenticatedDep, sleeve: str | None = None
) -> Response:
    user_id = await scoped_sole_user_id(session, principal.user_id)
    view = await reads.journal(session, user_id=user_id, sleeve=_optional_sleeve(sleeve))
    return _json(
        OptionsJournalOut(
            summaries=[
                OptionsSummaryOut(
                    sleeve=s.key.sleeve.value,
                    simulated=s.key.simulated,
                    sizing_mode=s.key.sizing_mode.value,
                    count=s.count,
                    traded=s.traded,
                    win_rate=s.win_rate,
                    mean_r=s.mean_r,
                    expectancy_inr=s.expectancy_inr,
                    expectancy_r=s.expectancy_r,
                    worst_r=s.worst_r,
                    max_drawdown_r=s.max_drawdown_r,
                    max_drawdown_inr=s.max_drawdown_inr,
                    r_values=list(view.r_values.get(s.key, ())),
                    by_closed_reason=[
                        OptionsBucketOut(label=label, count=b.count, mean_r=b.mean_r)
                        for label, b in s.by_closed_reason
                    ],
                )
                for s in view.summaries
            ],
            skips_by_reason=view.skips_by_reason,
            progress=[
                OptionsProgressOut(
                    group=p.group,
                    sessions_needed=p.sessions_needed,
                    traded_needed=p.traded_needed,
                    sessions_done=p.sessions_done,
                    traded=p.traded,
                )
                for p in view.progress
            ],
            min_sessions=view.min_sessions,
            recent=[_closed_out(c) for c in view.recent],
        )
    )


@router.get("/backtest", response_model=OptionsBacktestOut, summary="Backtests per tier")
async def get_options_backtest(
    session: SessionDep, principal: AuthenticatedDep, sleeve: str | None = None
) -> Response:
    user_id = await scoped_sole_user_id(session, principal.user_id)
    runs = await reads.backtests(session, user_id=user_id, sleeve=_optional_sleeve(sleeve))
    return _json(
        OptionsBacktestOut(
            runs=[
                OptionsBacktestRunOut(
                    id=r.id,
                    sleeve=r.sleeve,
                    tier=r.tier,
                    date_from=r.date_from,
                    date_to=r.date_to,
                    sessions=r.sessions,
                    signals=r.signals,
                    traded=r.traded,
                    skipped_by_reason=r.skipped_by_reason,
                    win_rate=r.win_rate,
                    expectancy_r=r.expectancy_r,
                    net_pnl_inr=r.net_pnl_inr,
                    max_drawdown_r=r.max_drawdown_r,
                    caveats=r.caveats,
                    ran_at=r.ran_at,
                )
                for r in runs
            ],
            reason=None
            if runs
            else "No options backtest has been run yet (OP12 builds them; Tier 3 waits on data).",
        )
    )


@router.get("/calendar", response_model=OptionsCalendarOut, summary="Expiries and event days")
async def get_options_calendar(
    session: SessionDep,
    principal: AuthenticatedDep,
    year: Annotated[int | None, Query(ge=2020, le=2100)] = None,
) -> Response:
    user_id = await scoped_sole_user_id(session, principal.user_id)
    view = await reads.calendar(session, user_id=user_id, year=year or _now().year)
    return _json(_calendar_out(view))


def _calendar_out(view: reads.CalendarView) -> OptionsCalendarOut:
    return OptionsCalendarOut(
        year=view.year,
        expiries=[_expiry_out(e) for e in view.expiries],
        event_days=[_event_out(e) for e in view.event_days],
    )


def _event_out(row: reads.EventDayView) -> OptionsEventDayOut:
    return OptionsEventDayOut(
        date=row.date,
        reason=row.reason,
        source=row.source,
        source_url=row.source_url,
        note=row.note,
        removable=row.removable,
    )


@router.post(
    "/event-day",
    response_model=OptionsEventDayOut,
    status_code=201,
    summary="Add an event day (no sleeve trades it)",
)
async def post_options_event_day(
    session: SessionDep, principal: AuthenticatedDep, body: OptionsEventDayIn
) -> Response:
    """Mutation 1 of 2 (``05`` §2). A day no sleeve trades — it can only make the book *do less*."""
    user_id = await scoped_sole_user_id(session, principal.user_id)
    if body.reason not in OP_EVENT_REASONS:
        raise bad_request(f"reason must be one of {', '.join(OP_EVENT_REASONS)}")
    try:
        row = await reads.add_event_day(
            session, user_id=user_id, day=body.date, reason=body.reason, note=body.note
        )
    except reads.EventDayConflict as exc:
        raise bad_request(str(exc)) from exc
    return _json(_event_out(row), status_code=201)


@router.delete("/event-day", status_code=204, summary="Remove a person's event day")
async def delete_options_event_day(
    session: SessionDep,
    principal: AuthenticatedDep,
    date: Annotated[dt.date, Query(description="the day to remove")],
) -> Response:
    """Mutation 1 of 2, its other half. A seeded, source-verified day is refused (400)."""
    user_id = await scoped_sole_user_id(session, principal.user_id)
    try:
        removed = await reads.remove_event_day(session, user_id=user_id, day=date)
    except reads.EventDayConflict as exc:
        raise bad_request(str(exc)) from exc
    if not removed:
        raise not_found("event day", date.isoformat())
    return Response(status_code=204)


async def _config_out(session: SessionDep, user_id: int, settings: Settings) -> OptionsConfigOut:
    ceilings = ceilings_from_settings(settings)
    book: OptionsBookOut | None = None
    sleeves: list[OptionsSleeveConfigOut] = []
    try:
        row = await read_book(session, user_id)
        book = OptionsBookOut(
            underlying=row.underlying,
            account_inr=row.account_inr,
            margin_pool_inr=row.margin_pool_inr,
            daily_loss_limit_inr=row.daily_loss_limit_inr,
            monthly_pause_inr=row.monthly_pause_inr,
            paused_until=row.paused_until,
            paused_reason=row.paused_reason,
        )
        for group in SleeveGroup:
            sleeve = await read_sleeve(session, user_id, group)
            sleeves.append(
                OptionsSleeveConfigOut(
                    sleeve=sleeve.sleeve,
                    sleeve_capital_inr=sleeve.sleeve_capital_inr,
                    risk_per_trade_pct=sleeve.risk_per_trade_pct,
                    max_lots=sleeve.max_lots,
                    paper_enabled=sleeve.paper_enabled,
                    hard_exit_time=sleeve.hard_exit_time,
                    paused_until=sleeve.paused_until,
                    paused_reason=sleeve.paused_reason,
                )
            )
    except OptionsConfigNotSeeded:
        book, sleeves = None, []
    audit = await audit_trail(session, user_id=user_id)
    return OptionsConfigOut(
        seeded=book is not None,
        book=book,
        sleeves=sleeves,
        ceilings=OptionsCeilingsOut(
            risk_per_trade_inr_max=ceilings.risk_per_trade_inr_max,
            risk_pct_max=ceilings.risk_pct_max,
            max_lots_max=ceilings.max_lots_max,
            book_daily_loss_inr_max=ceilings.book_daily_loss_inr_max,
            book_monthly_loss_inr_max=ceilings.book_monthly_loss_inr_max,
            hard_exit_latest=ceilings.hard_exit_latest,
        ),
        gates=_gates(settings),
        thresholds=[
            OptionsThresholdOut(section=t.section, key=t.key, value=t.value, anchor=t.anchor)
            for t in reads.thresholds()
        ],
        audit=[
            OptionsAuditOut(
                scope=a.scope,
                key=a.key,
                old_value=a.old_value,
                new_value=a.new_value,
                changed_at=a.changed_at,
                changed_by=a.changed_by,
            )
            for a in audit
        ],
    )


@router.get("/config", response_model=OptionsConfigOut, summary="Settings, ceilings, thresholds")
async def get_options_config(
    session: SessionDep, principal: AuthenticatedDep, settings: SettingsDep
) -> Response:
    user_id = await scoped_sole_user_id(session, principal.user_id)
    return _json(await _config_out(session, user_id, settings))


@router.patch("/config", response_model=OptionsConfigOut, summary="Change the options settings")
async def patch_options_config(
    session: SessionDep,
    principal: AuthenticatedDep,
    settings: SettingsDep,
    patch: OptionsConfigPatch,
) -> Response:
    """Mutation 2 of 2. The OP2 boundary unchanged: a value above its ceiling is a 422 naming the
    ceiling and its env var; ``paused_*`` and the execution flags are not fields at all.

    **Atomic across the request**: every part's bounds are checked before any part is written,
    so a two-part patch that crosses a ceiling on its second part changes neither.
    """
    user_id = await scoped_sole_user_id(session, principal.user_id)
    ceilings = ceilings_from_settings(settings)
    now = dt.datetime.now(tz=dt.UTC)
    changed_by = f"user:{user_id}"
    try:
        # Every bound first, over every part, before any row changes — so the refusal is atomic
        # by construction and not only because the transaction is rolled back afterwards.
        if patch.book is not None:
            await read_book(session, user_id)
            check_book(patch.book.changes(), ceilings)
        for group, sleeve_patch in patch.sleeves.items():
            current = await read_sleeve(session, user_id, group)
            check_sleeve(sleeve_patch.changes(), ceilings, current=current)
        if patch.book is not None:
            await apply_book_patch(
                session,
                user_id=user_id,
                patch=patch.book,
                ceilings=ceilings,
                changed_by=changed_by,
                now=now,
            )
        for group, sleeve_patch in patch.sleeves.items():
            await apply_sleeve_patch(
                session,
                user_id=user_id,
                sleeve=group,
                patch=sleeve_patch,
                ceilings=ceilings,
                changed_by=changed_by,
                now=now,
            )
    except OptionsConfigNotSeeded as exc:
        raise not_found("options config", str(user_id)) from exc
    return _json(await _config_out(session, user_id, settings))
