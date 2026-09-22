"""Each sleeve's scan state and candidates at one minute — what the web tab shows (``04`` §10).

The worker (``baskfy.options.scan``, OP4) calls :func:`scan_all` once a minute with NIFTY 50's bars,
its daily closes, the master, the chain snapshots it asked for and each sleeve's book and session
context from the database, and writes one ``op_scan`` row per sleeve. Nothing here reads a clock
or a table (law 1): ``now`` and every row are arguments, so a replay of any minute gives the row
the live scan wrote (the task is idempotent per minute, and Tier 1 reuses the same functions).

**A candidate is the plan builder's computation.** Every candidate here is built by the sleeve
module's own ``build`` — ``condor.build``, ``directional.build``, ``expiry_setups.build`` — over a
:class:`~baskfy_core.options.structures.ChainView` of the minute's snapshot, which is exactly what
OP6-OP8's plan builders will call (``04`` §10; tested).

**Which snapshot prices a candidate** (OP4.4). Before a sleeve decides, the latest snapshot — a
live preview. Once it has decided, the snapshot of its **decision minute**: O1's ``plan_time``,
O3-B's ``o3b_plan_time``, and for O2 and O3-A the minute the trigger bar closed. So a decided
verdict does not flip as premiums move after it, and a re-run of any minute is identical. The
worker learns which minutes to load from :func:`wanted_minutes`, which runs the same scan with no
snapshots and records what it asked for.

**States are one-way within a day** (``04`` §10) because every input they read is a stored fact
and every window waits until it is settled (``bars.window_settled``, OP4.3). ``PAUSED`` overrides
(§9.4): the numbers are still computed and shown. O3-A and O3-B are separate rows (their codes
are separate in ``op_sleeve``); O3 uses O2's ``DAY_SKIPPED`` for a setup whose conditions failed
(OP4.9). ``stale`` is ``04`` §2.5's flag: the snapshot is more than ``stale_scan_seconds`` from the
minute it should describe, or missing.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from baskfy_core.options import condor, directional, expiry_setups
from baskfy_core.options.bars import IST, Bar, closed, ist, minute_after
from baskfy_core.options.calendar import (
    Contract,
    expiry_for_o1_o3,
    expiry_for_o2,
    lot_size_for,
    next_session,
    role,
    tick_size_for,
)
from baskfy_core.options.chain import OptionQuote
from baskfy_core.options.config import (
    CondorConfig,
    OptionsCeilings,
    OptionsConfig,
    Sleeve,
)
from baskfy_core.options.session import SessionState
from baskfy_core.options.structures import (
    Candidate,
    ChainView,
    Direction,
    Rejection,
    SleeveBook,
    chain_view,
    to_json,
)


class ScanState(StrEnum):
    """``04`` §10's vocabulary across the three sleeves (``op_scan.state``)."""

    NOT_TODAY = "NOT_TODAY"
    OBSERVING = "OBSERVING"
    WOULD_TRADE = "WOULD_TRADE"
    WOULD_SKIP = "WOULD_SKIP"
    SLOT_TAKEN = "SLOT_TAKEN"
    PLANNED = "PLANNED"
    DONE = "DONE"
    BUILDING_RANGE = "BUILDING_RANGE"
    DAY_SKIPPED = "DAY_SKIPPED"
    ARMED = "ARMED"
    TRIGGERED = "TRIGGERED"
    WINDOW_CLOSED = "WINDOW_CLOSED"
    PAUSED = "PAUSED"


#: The order ``scan_all`` evaluates: O3-B before O3-A, because O3-B holds the day if both fire.
SCAN_ORDER: tuple[Sleeve, ...] = (Sleeve.O1M, Sleeve.O1W, Sleeve.O2, Sleeve.O3B, Sleeve.O3A)
#: Session states that mean a plan is live or was taken today; and those that mean it is over.
_LIVE_SESSION = frozenset({SessionState.PLANNED, SessionState.CONFIRMED, SessionState.OPEN})
_OVER_SESSION = frozenset({SessionState.CLOSED, SessionState.LAPSED, SessionState.SKIPPED})
NO_PREV_CLOSE = "NO_PREV_CLOSE"
O3B_HOLDS = "O3B_HOLDS"
SETUP_DISABLED = "SETUP_DISABLED"


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One minute of ``op_chain_snapshot`` for NIFTY: the minute, the spot quoted with it, and
    every contract's quote."""

    ts: dt.datetime
    spot: Decimal | None
    quotes: tuple[OptionQuote, ...]


@dataclass(frozen=True, slots=True)
class MarketDay:
    """The market facts of ``trade_date``, shared by every user (``03`` §4-§5)."""

    trade_date: dt.date
    trading_day: bool
    contracts: tuple[Contract, ...]
    #: NIFTY 50's one-minute bars of ``trade_date`` (``op_index_minute``).
    bars: tuple[Bar, ...]
    #: NIFTY 50's daily closes through the **previous** session, oldest first
    #: (``index_snapshot_daily``) — the trend's EMA and every sleeve's ``prev_close``.
    daily_closes: tuple[Decimal, ...]
    #: India VIX's previous close (``04`` §4.1).
    vix_prev_close: Decimal | None


@dataclass(frozen=True, slots=True)
class SleeveContext:
    """One sleeve's book and today's session, from the user's ``op_`` rows."""

    book: SleeveBook = field(default_factory=SleeveBook)
    paused: bool = False
    session_state: SessionState | None = None


@dataclass(frozen=True, slots=True)
class DayContext:
    """The user's side of the day: event days, the expiry-day slot, each sleeve's context."""

    event_days: frozenset[dt.date] = frozenset()
    slot_holder: Sleeve | None = None
    sleeves: Mapping[Sleeve, SleeveContext] = field(default_factory=dict)

    def of(self, sleeve: Sleeve) -> SleeveContext:
        return self.sleeves.get(sleeve, SleeveContext())


class SnapshotBook:
    """The snapshots the caller loaded, keyed by the minute asked for (``None`` = the latest).

    Records every request, so :func:`wanted_minutes` can run the scan with an empty book and
    learn which minutes to load. It holds data; it reads nothing.
    """

    def __init__(
        self, snapshots: Mapping[dt.datetime | None, Snapshot | None] | None = None
    ) -> None:
        self._snapshots = dict(snapshots or {})
        self.asked: set[dt.datetime | None] = set()

    def get(self, at: dt.datetime | None) -> Snapshot | None:
        self.asked.add(at)
        return self._snapshots.get(at)


@dataclass(frozen=True, slots=True)
class ScanResult:
    """One ``op_scan`` row: the state, every reason, the numbers and the priced candidates."""

    sleeve: Sleeve
    trade_date: dt.date
    ts: dt.datetime
    state: ScanState
    reasons: tuple[str, ...]
    numbers: dict[str, object]
    candidates: tuple[Candidate, ...]
    as_of_minute: dt.datetime | None
    stale: bool

    def candidates_json(self) -> list[dict[str, object]]:
        return [to_json(c) for c in self.candidates]


# --- helpers -------------------------------------------------------------------------------------


def _q(value: Decimal | None, places: str = "0.01") -> str | None:
    return None if value is None else str(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def _t(value: dt.time | None) -> str | None:
    return None if value is None else value.strftime("%H:%M")


def scan_minute(now: dt.datetime) -> dt.datetime:
    """The row's ``ts``: ``now`` floored to the minute, aware IST."""
    local = ist(now).replace(second=0, microsecond=0)
    return local.replace(tzinfo=IST)


def _at(day: dt.date, clock: dt.time) -> dt.datetime:
    return dt.datetime.combine(day, clock, tzinfo=IST)


def _last_close_by(bars: Sequence[Bar], at: dt.datetime) -> Decimal | None:
    seen = closed(bars, at + dt.timedelta(minutes=1))
    return seen[-1].close if seen else None


def priced_view(
    snapshot: Snapshot | None, expiry: dt.date | None, market: MarketDay, options: OptionsConfig
) -> ChainView | None:
    """The snapshot's chain for ``expiry``, priced at the snapshot's minute."""
    if snapshot is None or expiry is None:
        return None
    tick = tick_size_for(market.contracts, expiry, options.calendar.underlying)
    spot = snapshot.spot or _last_close_by(market.bars, snapshot.ts)
    if tick is None or spot is None or spot <= 0:
        return None
    listed = [
        c.strike
        for c in market.contracts
        if c.expiry == expiry and c.underlying == options.calendar.underlying
    ]
    return chain_view(
        snapshot.quotes,
        expiry=expiry,
        spot=spot,
        now=snapshot.ts,
        tick=tick,
        config=options.chain,
        settle=options.calendar.market_close,
        listed_strikes=listed or None,
    )


def _stale(snapshot: Snapshot | None, reference: dt.datetime, options: OptionsConfig) -> bool:
    """``04`` §2.5: missing, or more than ``stale_scan_seconds`` from the minute it describes."""
    if snapshot is None:
        return True
    gap = abs((ist(snapshot.ts) - ist(reference)).total_seconds())
    return gap > options.chain.stale_scan_seconds


def _slot_free(sleeve: Sleeve, context: DayContext) -> bool:
    return context.slot_holder is None or context.slot_holder is sleeve


def _condor_config(sleeve: Sleeve, options: OptionsConfig) -> CondorConfig:
    return options.condor_monthly if sleeve is Sleeve.O1M else options.condor_weekly


@dataclass(slots=True)
class _Row:
    """A result under construction."""

    state: ScanState
    reasons: list[str] = field(default_factory=list)
    numbers: dict[str, object] = field(default_factory=dict)
    candidates: list[Candidate] = field(default_factory=list)
    snapshot: Snapshot | None = None
    reference: dt.datetime | None = None
    priced: bool = False


def _finish(
    sleeve: Sleeve, row: _Row, market: MarketDay, now: dt.datetime, options: OptionsConfig
) -> ScanResult:
    stale = False
    if row.priced:
        stale = _stale(row.snapshot, row.reference or now, options)
    return ScanResult(
        sleeve=sleeve,
        trade_date=market.trade_date,
        ts=scan_minute(now),
        state=row.state,
        reasons=tuple(row.reasons),
        numbers=row.numbers,
        candidates=tuple(row.candidates),
        as_of_minute=row.snapshot.ts if row.snapshot is not None else None,
        stale=stale,
    )


def _not_today(sleeve: Sleeve, market: MarketDay, context: DayContext, reason: str) -> _Row:
    row = _Row(ScanState.NOT_TODAY, reasons=[reason])
    if sleeve is not Sleeve.O2:
        upcoming = next_session(
            market.trade_date, sleeve, rows=market.contracts, event_days=context.event_days
        )
        row.numbers["next_date"] = upcoming.isoformat() if upcoming else None
    return row


def _paused(row: _Row, paused: bool) -> _Row:
    if paused and row.state is not ScanState.NOT_TODAY:
        row.numbers["state_before_pause"] = row.state.value
        row.state = ScanState.PAUSED
        if Rejection.REJECTED_PAUSED.value not in row.reasons:
            row.reasons.append(Rejection.REJECTED_PAUSED.value)
    return row


def _candidate_state(
    candidate: Candidate, ok: ScanState, skip: ScanState
) -> tuple[ScanState, list[str]]:
    if candidate.viable:
        return ok, []
    code = candidate.rejection
    if code is Rejection.REJECTED_SLOT_TAKEN:
        return ScanState.SLOT_TAKEN, [code.value]
    return skip, [code.value if code is not None else "REJECTED"]


# --- O1 ------------------------------------------------------------------------------------------


def _scan_o1(  # noqa: PLR0913, PLR0917 - the sleeve, the market, the user, the clock, the book, the rules
    sleeve: Sleeve,
    market: MarketDay,
    context: DayContext,
    now: dt.datetime,
    snapshots: SnapshotBook,
    options: OptionsConfig,
    ceilings: OptionsCeilings,
) -> _Row:
    cfg = _condor_config(sleeve, options)
    mine = context.of(sleeve)
    verdict_role = role(
        market.trade_date,
        sleeve,
        rows=market.contracts,
        event_days=context.event_days,
        trading_day=market.trading_day,
    )
    if not verdict_role.trades:
        return _not_today(sleeve, market, context, verdict_role.reason.value)
    if not market.daily_closes:
        state = ScanState.OBSERVING if ist(now).time() < cfg.plan_time else ScanState.WOULD_SKIP
        return _paused(_Row(state, reasons=[NO_PREV_CLOSE]), mine.paused)
    verdict = condor.observe(
        market.bars,
        market.daily_closes[-1],
        now=now,
        event_day=market.trade_date in context.event_days,
        config=cfg,
        grace_seconds=options.chain.stale_scan_seconds,
    )
    n = verdict.numbers
    row = _Row(ScanState.OBSERVING)
    row.numbers = {
        "variant": cfg.variant.value,
        "prev_close": _q(n.prev_close),
        "bars": n.bars,
        "open_0915": _q(n.open_0915),
        "gap_pct": _q(n.gap_pct, "0.0001"),
        "gap_max_pct": str(cfg.gap_max_pct),
        "obs_high": _q(n.obs_high),
        "obs_low": _q(n.obs_low),
        "range_pct": _q(n.range_pct, "0.0001"),
        "range_max_pct": str(cfg.range_max_pct),
        "or_high": _q(n.or_high),
        "or_low": _q(n.or_low),
        "last_close": _q(n.last_close),
        "contained": n.contained,
        "er": _q(n.er, "0.0001"),
        "er_max": str(cfg.er_max),
        "min_bars": cfg.min_bars,
        "plan_time": _t(cfg.plan_time),
        "entry_window_end": _t(cfg.entry_window_end),
    }
    row.reasons = [r.value for r in verdict.reasons]
    clock = ist(now).time()
    if not verdict.final or clock < cfg.plan_time:
        return _paused(row, mine.paused)
    if verdict.reasons:
        row.state = ScanState.WOULD_SKIP
    else:
        decision = _at(market.trade_date, cfg.plan_time)
        snapshot = snapshots.get(decision)
        expiry = expiry_for_o1_o3(market.trade_date)
        candidate = condor.build(
            priced_view(snapshot, expiry, market, options),
            or_high=n.or_high or Decimal(0),
            or_low=n.or_low or Decimal(0),
            lot_size=lot_size_for(market.contracts, expiry, options.calendar.underlying),
            book=mine.book,
            config=cfg,
            options=options,
            ceilings=ceilings,
            slot_free=_slot_free(sleeve, context),
            paused=mine.paused,
            expiry=expiry,
        )
        row.candidates = [candidate]
        row.snapshot, row.reference, row.priced = snapshot, decision, True
        row.state, row.reasons = _candidate_state(
            candidate, ScanState.WOULD_TRADE, ScanState.WOULD_SKIP
        )
    row.numbers["verdict"] = row.state.value
    if mine.session_state in _LIVE_SESSION:
        row.state = ScanState.PLANNED
    elif mine.session_state in _OVER_SESSION or clock >= cfg.entry_window_end:
        row.state = ScanState.DONE
    return _paused(row, mine.paused)


# --- O2 ------------------------------------------------------------------------------------------


def _scan_o2(  # noqa: PLR0913, PLR0917 - the market, the user, the clock, the book, the rules
    market: MarketDay,
    context: DayContext,
    now: dt.datetime,
    snapshots: SnapshotBook,
    options: OptionsConfig,
    ceilings: OptionsCeilings,
) -> _Row:
    cfg = options.directional
    mine = context.of(Sleeve.O2)
    verdict_role = role(
        market.trade_date,
        Sleeve.O2,
        rows=market.contracts,
        event_days=context.event_days,
        trading_day=market.trading_day,
    )
    if not verdict_role.trades:
        return _not_today(Sleeve.O2, market, context, verdict_role.reason.value)
    filters = directional.day_filters(
        market.bars,
        market.daily_closes,
        market.vix_prev_close,
        now=now,
        event_day=market.trade_date in context.event_days,
        config=cfg,
        grace_seconds=options.chain.stale_scan_seconds,
    )
    expiry = expiry_for_o2(market.trade_date, market.contracts, options.calendar.underlying)
    row = _Row(ScanState.BUILDING_RANGE)
    row.numbers = {
        "trend": None if filters.trend is None else filters.trend.value,
        "ema": _q(filters.ema),
        "trend_ema_days": cfg.trend_ema_days,
        "index": "NIFTY 50",
        "prev_close": _q(filters.prev_close),
        "open_0915": _q(filters.open_0915),
        "gap_pct": _q(filters.gap_pct, "0.0001"),
        "gap_max_pct": str(cfg.gap_max_pct),
        "or_high": _q(filters.or_high),
        "or_low": _q(filters.or_low),
        "or_pct": _q(filters.or_pct, "0.0001"),
        "or_max_pct": str(cfg.or_max_pct),
        "vix": _q(filters.vix),
        "vix_max": str(cfg.vix_max),
        "bars": filters.bars,
        "expiry": expiry.isoformat() if expiry else None,
    }
    row.reasons = [r.value for r in filters.reasons]
    if not filters.final:
        return _paused(row, mine.paused)
    if filters.reasons:
        row.state = ScanState.DAY_SKIPPED
        return _paused(row, mine.paused)
    direction = filters.trend or Direction.UP
    trig = directional.find_trigger(
        market.bars,
        or_high=filters.or_high or Decimal(0),
        or_low=filters.or_low or Decimal(0),
        direction=direction,
        now=now,
        config=cfg,
        session_open=options.calendar.market_open,
        grace_seconds=options.chain.stale_scan_seconds,
    )
    level = trig.level_up if direction is Direction.UP else trig.level_down
    row.numbers |= {
        "direction": direction.value,
        "trigger_level": _q(level),
        "level_up": _q(trig.level_up),
        "level_down": _q(trig.level_down),
        "last_close": _q(trig.last_close),
        "counter_trend_breaks": [
            {"close_time": _t(b.close_time), "close": _q(b.close), "direction": b.direction.value}
            for b in trig.counter_breaks
        ],
    }
    if trig.trigger is not None:
        decision = _at(market.trade_date, minute_after(trig.trigger.close_time))
        row.state = ScanState.TRIGGERED
        row.numbers |= {
            "trigger_time": _t(trig.trigger.close_time),
            "trigger_close": _q(trig.trigger.close),
        }
        snapshot = snapshots.get(decision)
        row.snapshot, row.reference = snapshot, decision
    elif trig.window_closed:
        row.state = ScanState.WINDOW_CLOSED
        return _paused(row, mine.paused)
    else:
        row.state = ScanState.ARMED
        if trig.last_close is not None:
            distance = (
                level - trig.last_close if direction is Direction.UP else trig.last_close - level
            )
            row.numbers |= {
                "distance_points": _q(distance),
                "distance_pct": _q(distance / trig.last_close * Decimal(100), "0.0001"),
            }
        row.snapshot, row.reference = snapshots.get(None), now
    candidate = directional.build(
        priced_view(row.snapshot, expiry, market, options),
        direction,
        lot_size=lot_size_for(market.contracts, expiry, options.calendar.underlying)
        if expiry
        else None,
        book=mine.book,
        config=cfg,
        options=options,
        ceilings=ceilings,
        expiry=expiry or market.trade_date,
        paused=mine.paused,
    )
    row.candidates, row.priced = [candidate], True
    if not candidate.viable and candidate.rejection is not None:
        row.reasons.append(candidate.rejection.value)
    return _paused(row, mine.paused)


# --- O3 ------------------------------------------------------------------------------------------


def _o3_role(
    sleeve: Sleeve, market: MarketDay, context: DayContext, options: OptionsConfig
) -> _Row | None:
    verdict_role = role(
        market.trade_date,
        sleeve,
        rows=market.contracts,
        event_days=context.event_days,
        trading_day=market.trading_day,
    )
    if not verdict_role.trades:
        return _not_today(sleeve, market, context, verdict_role.reason.value)
    enabled = (
        options.expiry_setups.o3a_enabled
        if sleeve is Sleeve.O3A
        else options.expiry_setups.o3b_enabled
    )
    if not enabled:
        return _Row(ScanState.NOT_TODAY, reasons=[SETUP_DISABLED])
    return None


def _spread(  # noqa: PLR0913, PLR0917 - one candidate's inputs
    snapshot: Snapshot | None,
    direction: Direction,
    market: MarketDay,
    mine: SleeveContext,
    options: OptionsConfig,
    ceilings: OptionsCeilings,
    *,
    slot_free: bool,
) -> Candidate:
    expiry = expiry_for_o1_o3(market.trade_date)
    return expiry_setups.build(
        priced_view(snapshot, expiry, market, options),
        direction,
        lot_size=lot_size_for(market.contracts, expiry, options.calendar.underlying),
        book=mine.book,
        config=options.expiry_setups,
        options=options,
        ceilings=ceilings,
        expiry=expiry,
        slot_free=slot_free,
        paused=mine.paused,
    )


def _scan_o3b(  # noqa: PLR0913, PLR0917 - the market, the user, the clock, the book, the rules
    market: MarketDay,
    context: DayContext,
    now: dt.datetime,
    snapshots: SnapshotBook,
    options: OptionsConfig,
    ceilings: OptionsCeilings,
) -> _Row:
    cfg = options.expiry_setups
    mine = context.of(Sleeve.O3B)
    early = _o3_role(Sleeve.O3B, market, context, options)
    if early is not None:
        return early
    if not market.daily_closes:
        return _paused(_Row(ScanState.BUILDING_RANGE, reasons=[NO_PREV_CLOSE]), mine.paused)
    gh = expiry_setups.gap_hold(
        market.bars,
        market.daily_closes[-1],
        now=now,
        event_day=market.trade_date in context.event_days,
        config=cfg,
        session_open=options.calendar.market_open,
        grace_seconds=options.chain.stale_scan_seconds,
    )
    row = _Row(ScanState.BUILDING_RANGE)
    row.numbers = {
        "prev_close": _q(market.daily_closes[-1]),
        "open_0915": _q(gh.open_0915),
        "gap_points": _q(gh.gap_points),
        "gap_pct": _q(gh.gap_pct, "0.0001"),
        "gap_min_pct": str(cfg.o3b_gap_min_pct),
        "gap_max_pct": str(cfg.o3b_gap_max_pct),
        "half_gap": _q(gh.half_gap),
        "direction": None if gh.direction is None else gh.direction.value,
        "held_so_far": gh.held_so_far,
        "bars": gh.bars,
        "hold_end": _t(cfg.o3b_hold_end),
        "plan_time": _t(cfg.o3b_plan_time),
        "window_end": _t(cfg.o3b_window_end),
    }
    row.reasons = [r.value for r in gh.reasons]
    slot_free = _slot_free(Sleeve.O3B, context)
    if gh.final and gh.reasons:
        row.state = ScanState.DAY_SKIPPED
    elif gh.triggered and gh.direction is not None:
        decision = _at(market.trade_date, cfg.o3b_plan_time)
        snapshot = snapshots.get(decision)
        candidate = _spread(
            snapshot, gh.direction, market, mine, options, ceilings, slot_free=slot_free
        )
        row.candidates = [candidate]
        row.snapshot, row.reference, row.priced = snapshot, decision, True
        row.state, extra = _candidate_state(candidate, ScanState.TRIGGERED, ScanState.TRIGGERED)
        row.reasons += extra
    elif gh.window_closed:
        row.state = ScanState.WINDOW_CLOSED
    elif gh.direction is not None and not gh.reasons:
        snapshot = snapshots.get(None)
        row.candidates = [
            _spread(snapshot, gh.direction, market, mine, options, ceilings, slot_free=slot_free)
        ]
        row.snapshot, row.reference, row.priced = snapshot, now, True
    return _paused(row, mine.paused)


def _o3b_holds(o3b: _Row | None, context: DayContext) -> bool:
    """``04`` §5.5: O3-B fired first with a viable candidate and its session has not let it go."""
    if o3b is None or o3b.state is not ScanState.TRIGGERED:
        return False
    if not any(c.viable for c in o3b.candidates):
        return False
    return context.of(Sleeve.O3B).session_state not in (SessionState.LAPSED, SessionState.SKIPPED)


def _scan_o3a(  # noqa: PLR0913, PLR0917 - the market, the user, the clock, the book, the rules, O3-B
    market: MarketDay,
    context: DayContext,
    now: dt.datetime,
    snapshots: SnapshotBook,
    options: OptionsConfig,
    ceilings: OptionsCeilings,
    o3b: _Row | None,
) -> _Row:
    cfg = options.expiry_setups
    mine = context.of(Sleeve.O3A)
    early = _o3_role(Sleeve.O3A, market, context, options)
    if early is not None:
        return early
    if not market.daily_closes:
        return _paused(_Row(ScanState.BUILDING_RANGE, reasons=[NO_PREV_CLOSE]), mine.paused)
    rb = expiry_setups.range_break(
        market.bars,
        market.daily_closes[-1],
        now=now,
        event_day=market.trade_date in context.event_days,
        config=cfg,
        session_open=options.calendar.market_open,
        grace_seconds=options.chain.stale_scan_seconds,
    )
    row = _Row(ScanState.BUILDING_RANGE)
    row.numbers = {
        "prev_close": _q(market.daily_closes[-1]),
        "range_high": _q(rb.range_high),
        "range_low": _q(rb.range_low),
        "range_pct": _q(rb.range_pct, "0.0001"),
        "range_max_pct": str(cfg.o3a_range_max_pct),
        "bars": rb.bars,
        "level_up": _q(rb.level_up),
        "level_down": _q(rb.level_down),
        "er_min": str(cfg.o3a_er_min),
        "last_close": _q(rb.last_close),
        "window_start": _t(cfg.o3a_window_start),
        "window_end": _t(cfg.o3a_window_end),
        "low_er_breaks": [
            {"close_time": _t(t), "close": _q(c), "er": _q(e, "0.0001")}
            for t, c, e in rb.low_er_breaks
        ],
    }
    row.reasons = [r.value for r in rb.reasons]
    holds = _o3b_holds(o3b, context)
    slot_free = _slot_free(Sleeve.O3A, context) and not holds
    if not rb.final:
        return _paused(row, mine.paused)
    if rb.reasons:
        row.state = ScanState.DAY_SKIPPED
    elif rb.direction is not None and rb.trigger_time is not None:
        decision = _at(market.trade_date, minute_after(rb.trigger_time))
        row.numbers |= {
            "trigger_time": _t(rb.trigger_time),
            "trigger_close": _q(rb.trigger_close),
            "trigger_er": _q(rb.trigger_er, "0.0001"),
            "direction": rb.direction.value,
        }
        snapshot = snapshots.get(decision)
        candidate = _spread(
            snapshot, rb.direction, market, mine, options, ceilings, slot_free=slot_free
        )
        row.candidates = [candidate]
        row.snapshot, row.reference, row.priced = snapshot, decision, True
        row.state, extra = _candidate_state(candidate, ScanState.TRIGGERED, ScanState.TRIGGERED)
        row.reasons += extra
        if holds:
            row.reasons.append(O3B_HOLDS)
    elif rb.window_closed:
        row.state = ScanState.WINDOW_CLOSED
    else:
        row.state = ScanState.SLOT_TAKEN if not slot_free else ScanState.ARMED
        if holds:
            row.reasons.append(O3B_HOLDS)
        snapshot = snapshots.get(None)
        row.candidates = [
            _spread(snapshot, side, market, mine, options, ceilings, slot_free=slot_free)
            for side in (Direction.UP, Direction.DOWN)
        ]
        row.snapshot, row.reference, row.priced = snapshot, now, True
    return _paused(row, mine.paused)


# --- the entry points ----------------------------------------------------------------------------


def scan_all(  # noqa: PLR0913, PLR0917 - the market, the user, the clock, the snapshots, the rules
    market: MarketDay,
    context: DayContext,
    now: dt.datetime,
    snapshots: SnapshotBook,
    options: OptionsConfig,
    ceilings: OptionsCeilings,
) -> tuple[ScanResult, ...]:
    """Every sleeve's row for the minute of ``now``, in :data:`SCAN_ORDER`."""
    rows: dict[Sleeve, _Row] = {}
    for sleeve in SCAN_ORDER:
        if sleeve in (Sleeve.O1M, Sleeve.O1W):
            rows[sleeve] = _scan_o1(sleeve, market, context, now, snapshots, options, ceilings)
        elif sleeve is Sleeve.O2:
            rows[sleeve] = _scan_o2(market, context, now, snapshots, options, ceilings)
        elif sleeve is Sleeve.O3B:
            rows[sleeve] = _scan_o3b(market, context, now, snapshots, options, ceilings)
        else:
            rows[sleeve] = _scan_o3a(
                market, context, now, snapshots, options, ceilings, rows.get(Sleeve.O3B)
            )
    return tuple(_finish(s, rows[s], market, now, options) for s in SCAN_ORDER)


def wanted_minutes(
    market: MarketDay,
    context: DayContext,
    now: dt.datetime,
    options: OptionsConfig,
    ceilings: OptionsCeilings,
) -> frozenset[dt.datetime | None]:
    """The snapshot minutes :func:`scan_all` will ask for at ``now`` (``None`` = the latest)."""
    book = SnapshotBook()
    scan_all(market, context, now, book, options, ceilings)
    return frozenset(book.asked)
