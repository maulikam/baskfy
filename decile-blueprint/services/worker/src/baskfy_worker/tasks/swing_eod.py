"""SW5 — the evening job: manage the book, plan tomorrow, say so in an email.

`docs/swing/01` §8's routine, in code: *"End of day: 15-30 minutes on positions and scans."* This
is that half-hour. It runs after the detectors have written the day's candidates, and it does five
things in an order that matters:

1. **Auto-watch and expire.** The day's qualifying flags and every EP join the watchlist; rows
   that have run out of sessions leave it. `baskfy_api.swing_watch` owns the rules.
2. **Manage what is open.** `stops.manage` over every open `sw_position`, with today's bar and
   its two moving averages. The actions become tomorrow's exit lines: a `SELL_AT_OPEN` for a
   partial or a full exit, a `RAISE_GTT_STOP` for a stop that has earned its move.
3. **Settle the ladder** (SW8, and the drawdown since SW9.5). `exposure_tier` over the rung in
   force, the last **real** closed trades (STANDING-ANSWERS A10, SW10.5: real closes from day
   one — there is no paper book for the ladder, PACK.6's paper clause is void, the rung starts
   at 0), the day's gate and the sleeve's drawdown from its peak EOD NAV
   (`04` §8.5; `sleeve_drawdown` in `tasks/swing.py` says how the NAV is computed, SW9.5.1); the
   rung it answers is written back to `sw_config.exposure_level` — audited,
   `changed_by="swing-eod"` — with the peak, the drawdown and the lock-out beside it, and into
   the day's `sw_market_daily`, so the plan, the settings page, the morning rebuild and every hub
   tab read one number. `rung_in_force` says why the rung it starts from is read from the
   previous settlement rather than from `sw_config`.
4. **Count the session, and the first-live countdown** (A9, SW10.5). One `sw_session` row per
   session the system ran; when the session was LIVE and `sw_config.first_live_sessions_left`
   is above zero, it comes down by one — once, `sw_session.first_live_counted` says so — so
   tomorrow's plan is sized with the count the session left behind: the fifth live session
   decrements to 0 and the sixth plans at full risk. Never by a request; a desk restart
   changes nothing.
5. **Plan tomorrow.** `build_entries` over the watchlist with the settled tier and the risk
   multiplier the countdown implies (0.5 while it runs and execution is enabled; a paper plan is
   full size), into an `sw_plan(source=EOD_PREVIEW)` with its lines **and its skips**. `04` §9's
   own words: "A watchlist of twelve names and a plan of two lines is only useful if the other
   ten explain themselves."
6. **Send the email.**

**Exits are computed before entries, and the plan puts them first.** `04` §9.3: "the money they
free is the money the entries spend". A plan that sized tomorrow's buys against today's cash
would be planning to spend rupees that are still in a position it is about to sell.

**This job places nothing.** It writes a plan with a 30-minute expiry that the desk console can
confirm, line by line, on a click (`02` Track C §3). Nothing here imports the execution package.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.email import Mailer, build_transport
from baskfy_api.email.templates import SwingCandidate, SwingDigest, swing_eod
from baskfy_api.settings import Settings, get_settings
from baskfy_api.swing_journal import PAPER_SESSIONS_REQUIRED
from baskfy_api.swing_settings import (
    SYSTEM_OWNED_FIELDS,
    SwingConfigNotSeeded,
    record_system_change,
)
from baskfy_api.swing_watch import auto_watch, list_watch
from baskfy_core.models import (
    AppUser,
    Instrument,
    OhlcvDaily,
    SwConfig,
    SwMarketDaily,
    SwPlan,
    SwPlanLine,
    SwPlanSkip,
    SwPosition,
    SwSession,
    SwSetupDaily,
)
from baskfy_core.models.swing import PLAN_TTL_MINUTES
from baskfy_core.swing.config import DEFAULT_SWING_CONFIG, Setup, SwingConfig
from baskfy_core.swing.market import ExposureTier, MarketGate, exposure_tier
from baskfy_core.swing.plan import (
    LineKind,
    PlanLine,
    Skipped,
    SwingAccount,
    SwingPlan,
    WatchItem,
    assemble,
    build_entries,
    exit_lines,
    first_live_multiplier,
)
from baskfy_core.swing.stops import (
    ActionKind,
    DailyBar,
    OpenPosition,
    TrailMa,
    manage,
)
from baskfy_worker.steps import StepOutcome, StepStatus
from baskfy_worker.tasks.swing import (
    SleeveDrawdown,
    load_swing_config,
    lookback_start,
    sleeve_drawdown,
)
from baskfy_worker.telemetry import swing_span, swing_timed

log = logging.getLogger(__name__)

#: How many bars behind today the moving averages need. `manage` reads MA10 and MA20, so 20 plus
#: room for a name that missed a session.
MA_LOOKBACK_SESSIONS: Final = 40


@dataclass(slots=True)
class EodReport:
    """What the evening did. The email and the step payload are both written from this."""

    trade_date: dt.date
    gate: str
    #: The rung the plan was built with — after the ladder settled, and equal to what
    #: `sw_config.exposure_level` says once the evening has run.
    exposure_level: int
    #: The rung in force coming into the evening, and the closes the ladder read to move it.
    rung_before: int = 0
    closed_r: list[str] = field(default_factory=list)
    #: `04` §8.5: tonight's NAV against the sleeve's peak, and whether the lock-out is in force.
    drawdown_pct: str = "0.00"
    drawdown_locked: bool = False
    watch_added: int = 0
    watch_expired: int = 0
    watching: int = 0
    positions_managed: int = 0
    exit_lines: int = 0
    entry_lines: int = 0
    #: A7: live-gap lines on the plan with no stop yet (only the morning plan carries them).
    pending_lines: int = 0
    skips: int = 0
    naked_positions: list[str] = field(default_factory=list)
    plan_id: str | None = None
    sessions_logged: int = 0
    #: A9: the countdown after tonight, and the multiplier tomorrow's plan was sized with.
    first_live_sessions_left: int = 0
    risk_multiplier: str = "1"
    risk_pct_in_force: str = "0.500"

    def as_detail(self) -> dict[str, object]:
        return {
            "trade_date": self.trade_date.isoformat(),
            "gate": self.gate,
            "exposure_level": self.exposure_level,
            "ladder": {
                "from": self.rung_before,
                "to": self.exposure_level,
                "closed_r_multiples": list(self.closed_r),
                "drawdown_pct": self.drawdown_pct,
                "drawdown_locked": self.drawdown_locked,
            },
            "watch": {
                "added": self.watch_added,
                "expired": self.watch_expired,
                "watching": self.watching,
            },
            "plan": {
                "plan_id": self.plan_id,
                "exits": self.exit_lines,
                "entries": self.entry_lines,
                "pending": self.pending_lines,
                "skips": self.skips,
                "risk_multiplier": self.risk_multiplier,
                "risk_pct_in_force": self.risk_pct_in_force,
            },
            "first_live": {
                "sessions_left": self.first_live_sessions_left,
                "header": self.first_live_header(),
            },
            "positions_managed": self.positions_managed,
            "naked_positions": list(self.naked_positions),
            "sessions_logged": self.sessions_logged,
        }

    def first_live_header(self) -> str:
        """The plan header's line (A9): "first live sessions: N left · risk 0.25%"."""
        return first_live_header(self.first_live_sessions_left, self.risk_pct_in_force)


def first_live_header(sessions_left: int, risk_pct_in_force: str) -> str:
    """The sentence the plan is headed with while the countdown runs; empty once it is done."""
    if sessions_left <= 0:
        return ""
    return f"first live sessions: {sessions_left} left · risk {risk_pct_in_force}%"


def risk_pct_in_force(config: SwingConfig, multiplier: Decimal) -> str:
    """``risk_per_trade_pct x multiplier`` at the setting's three places — what the header says."""
    return str(
        (Decimal(str(config.sizing.risk_per_trade_pct)) * multiplier).quantize(Decimal("0.001"))
    )


async def _bars_for(
    session: AsyncSession, instrument_ids: list[int], start: dt.date, end: dt.date
) -> dict[int, list[tuple[dt.date, Decimal, Decimal, Decimal, Decimal]]]:
    """`(date, open, high, low, close)` per instrument, oldest first."""
    if not instrument_ids:
        return {}
    rows = await session.execute(
        select(
            OhlcvDaily.instrument_id,
            OhlcvDaily.date,
            OhlcvDaily.open,
            OhlcvDaily.high,
            OhlcvDaily.low,
            OhlcvDaily.close,
        )
        .where(
            OhlcvDaily.instrument_id.in_(instrument_ids),
            OhlcvDaily.date >= start,
            OhlcvDaily.date <= end,
        )
        .order_by(OhlcvDaily.instrument_id, OhlcvDaily.date)
    )
    out: dict[int, list[tuple[dt.date, Decimal, Decimal, Decimal, Decimal]]] = {}
    for instrument_id, on, open_, high, low, close in rows:
        out.setdefault(int(instrument_id), []).append((on, open_, high, low, close))
    return out


def _mean(values: list[Decimal], window: int) -> Decimal | None:
    """The trailing mean of the last ``window`` closes, or ``None`` before the window is full.

    `None` rather than a partial average, and that matters here more than on a chart: `manage`
    sells a position on a close below its trail, and a "20-day average" computed from six days is
    a different number that would sell a position that is doing nothing wrong.
    """
    if len(values) < window:
        return None
    return sum(values[-window:], Decimal(0)) / window


async def _sessions_between(session: AsyncSession, start: dt.date, end: dt.date) -> int:
    """Trading days strictly after ``start`` and up to ``end`` — `bars_since_entry`.

    The entry day is bar 0 (`04` §6.4), so a position entered today has zero sessions behind it
    and the partial window (day 3-5) has not opened.
    """
    from baskfy_core.models import TradingDay  # noqa: PLC0415 - one query needs it
    from baskfy_core.seed_data import NSE_EXCHANGE_ID  # noqa: PLC0415

    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(TradingDay)
                .where(
                    TradingDay.exchange_id == NSE_EXCHANGE_ID,
                    TradingDay.is_trading_day.is_(True),
                    TradingDay.date > start,
                    TradingDay.date <= end,
                )
            )
        ).scalar_one()
    )


async def manage_open_positions(
    session: AsyncSession,
    *,
    user_id: int,
    on: dt.date,
    config: SwingConfig = DEFAULT_SWING_CONFIG,
) -> tuple[list[PlanLine], list[str], int]:
    """`stops.manage` over the book. Returns ``(exit lines, naked symbols, positions managed)``.

    A position with no bar today is **skipped, not managed**: the rules read a close, and a name
    that did not trade (a suspension, a series move) has not given them one. Managing it against
    a stale bar would sell it on yesterday's information.
    """
    positions = (
        await session.execute(
            select(SwPosition, Instrument.symbol)
            .join(Instrument, Instrument.id == SwPosition.instrument_id)
            .where(
                SwPosition.user_id == user_id,
                SwPosition.state.in_(("OPEN", "PARTIAL")),
                SwPosition.quantity_open > 0,
            )
            .order_by(Instrument.symbol)
        )
    ).all()
    if not positions:
        return [], [], 0

    instrument_ids = [row[0].instrument_id for row in positions]
    start = await lookback_start(session, on, MA_LOOKBACK_SESSIONS)
    bars = await _bars_for(session, instrument_ids, start, on)

    lines: list[PlanLine] = []
    naked: list[str] = []
    managed = 0
    for position, symbol in positions:
        if position.gtt_id is None:
            # `03` §7: a resting GTT is the one thing the method insists on. A position without
            # one is reported every evening until it has one, whatever else the rules say.
            naked.append(symbol)
        series = bars.get(position.instrument_id, [])
        if not series or series[-1][0] != on:
            continue
        closes = [row[4] for row in series]
        today = series[-1]
        bar = DailyBar(
            date=on,
            open=today[1],
            high=today[2],
            low=today[3],
            close=today[4],
            ma10=_mean(closes, config.ma_fast),
            ma20=_mean(closes, config.ma_slow),
            bars_since_entry=await _sessions_between(session, position.entry_date, on),
        )
        actions = manage(_as_open_position(position, symbol), bar, config.stops)
        managed += 1
        if all(action.kind is ActionKind.HOLD for action in actions):
            continue
        lines.extend(exit_lines(symbol, actions))
    return lines, naked, managed


def _as_open_position(row: SwPosition, symbol: str) -> OpenPosition:
    return OpenPosition(
        symbol=symbol,
        entry_date=row.entry_date,
        entry=row.entry_avg,
        initial_stop=row.initial_stop,
        stop=row.stop,
        quantity=row.quantity_open,
        partial_done=row.partial_done,
        trail=TrailMa(row.trail),
        is_ep_gap_day=row.setup == Setup.EP.value,
    )


async def sleeve_account(
    session: AsyncSession, *, user_id: int, config_row: SwConfig | None
) -> SwingAccount:
    """The sleeve's own money — never the whole account (`02` Track C §5).

    `equity` is the configured capital plus what the open positions are carrying at cost, so a
    book that is fully invested still sizes against the sleeve it was given rather than against
    the cash left in it. `cash_available` is what is actually free.
    """
    capital = config_row.sleeve_capital_inr if config_row is not None else Decimal(0)
    rows = (
        await session.execute(
            select(SwPosition, Instrument.symbol)
            .join(Instrument, Instrument.id == SwPosition.instrument_id)
            .where(
                SwPosition.user_id == user_id,
                SwPosition.state.in_(("OPEN", "PARTIAL")),
                SwPosition.quantity_open > 0,
            )
        )
    ).all()
    exposure = sum((row.entry_avg * row.quantity_open for row, _ in rows), Decimal(0))
    return SwingAccount(
        equity=capital,
        cash_available=max(capital - exposure, Decimal(0)),
        open_symbols=frozenset(symbol for _, symbol in rows),
        open_exposure_inr=exposure,
    )


async def watch_items(
    session: AsyncSession, *, user_id: int, on: dt.date
) -> tuple[list[WatchItem], dict[str, int]]:
    """The watchlist as the plan builder wants it, plus symbol → instrument id for the writer.

    A row with no trigger is dropped: `build_entries` sizes from the trigger and the stop, and a
    `MANUAL` row someone added without levels is a name to look at rather than a plan line. A
    row with a trigger and **no stop** — a live gap the 09:09 scan wrote (SW6.2) — is kept
    since SW10.5 (A7): `build_entries` shows it as a `PENDING_RANGE` line that reserves a slot.

    The ADR, the turnover, the score and the band come from the name's **latest** detection row
    on or before ``on`` — today's when the detectors saw it today, else the last time they did —
    the monitor's own rule (`SignalContext.detected`). Since SW9.5 the ADR decides the widest
    stop (`04` §6), so a watched flag whose base is still forming keeps its measured ADR across
    the sessions it is watched; a name the detectors have never seen (a `MANUAL` row with no
    detection behind it) has no ADR, and a stop nobody can measure against the range is refused
    `STOP_TOO_WIDE` rather than waved through (SW9.5.2) — unless the watch row itself carries
    the ADR (`sw_watch.adr_pct`, SW10.5: the gap scan measures it from the bars) and the score
    it was watched at (`sw_watch.score`), which are read when no detection row exists.
    """
    rows = await list_watch(session, user_id=user_id)
    latest: dict[int, SwSetupDaily] = {}
    for detected_row in (
        await session.execute(
            select(SwSetupDaily)
            .where(SwSetupDaily.user_id == user_id, SwSetupDaily.date <= on)
            .order_by(
                SwSetupDaily.instrument_id, SwSetupDaily.date.desc(), SwSetupDaily.score.desc()
            )
        )
    ).scalars():
        latest.setdefault(detected_row.instrument_id, detected_row)
    items: list[WatchItem] = []
    ids: dict[str, int] = {}
    for row in rows:
        if row.trigger is None:
            continue
        detected = latest.get(row.instrument_id)
        adr = detected.adr_pct if detected and detected.adr_pct else row.adr_pct
        score = detected.score if detected else row.score
        items.append(
            WatchItem(
                symbol=row.symbol,
                setup=Setup(row.setup),
                trigger=row.trigger,
                stop_ref=row.stop_ref,
                adr_pct=Decimal(adr) if adr else Decimal(0),
                avg_turnover_inr=(
                    Decimal(detected.turnover_avg) if detected and detected.turnover_avg else None
                ),
                score=Decimal(score) if score is not None else Decimal(0),
                locked_upper_circuit=bool(detected.locked_upper_circuit) if detected else False,
            )
        )
        ids[row.symbol] = row.instrument_id
    return items, ids


async def store_plan(  # noqa: PLR0913 - one keyword per part of the plan being written
    session: AsyncSession,
    plan: SwingPlan,
    *,
    user_id: int,
    source: str,
    instrument_ids: dict[str, int],
    now: dt.datetime,
) -> str:
    """Write the plan, its lines and its skips. Returns the ``plan_id``.

    The expiry is stored rather than derived, so a later change to :data:`PLAN_TTL_MINUTES`
    cannot retroactively extend a plan that has already been issued.
    """
    plan_id = uuid.uuid4()
    row = SwPlan(
        plan_id=plan_id,
        user_id=user_id,
        as_of=plan.as_of,
        source=source,
        built_at=now,
        expires_at=now + dt.timedelta(minutes=PLAN_TTL_MINUTES),
        plan_hash=plan.plan_hash(),
        gate=plan.gate.value,
        exposure_level=plan.tier.level,
        total_risk_inr=plan.total_risk_inr,
        total_new_exposure_inr=plan.total_new_exposure_inr,
    )
    session.add(row)
    await session.flush()

    for line in plan.lines:
        instrument_id = instrument_ids.get(line.symbol)
        if instrument_id is None:
            log.warning("plan line for %s has no instrument id; skipped", line.symbol)
            continue
        session.add(
            SwPlanLine(
                plan_id=row.id,
                user_id=user_id,
                kind=line.kind.value,
                instrument_id=instrument_id,
                setup=line.setup.value if line.setup else None,
                quantity=line.quantity,
                trigger=line.trigger,
                stop=line.stop,
                risk_inr=line.risk_inr,
                position_value=line.position_value,
                trail=line.trail.value if line.trail else None,
                note=line.note,
                state="PROPOSED",
                # `04` §9.4: the gateway's idempotency key. The kind is in it because a plan can
                # touch one symbol twice — sell part of it and raise its stop — and
                # `plan_id:symbol` alone would report the second as a DUPLICATE and skip it.
                client_id=f"{plan_id}:{line.symbol}:{line.kind.value}",
            )
        )
    for skip in plan.skipped:
        session.add(
            SwPlanSkip(
                plan_id=row.id,
                user_id=user_id,
                instrument_id=instrument_ids.get(skip.symbol),
                symbol=skip.symbol,
                reason=skip.reason.value,
                detail=skip.detail or None,
            )
        )
    await session.flush()
    return str(plan_id)


async def _record_session(
    session: AsyncSession, *, user_id: int, on: dt.date, plan_id: str | None, execution: bool
) -> int:
    """One `sw_session` row per session the system ran. Returns how many exist in total.

    This is the count `docs/swing/02` §3.2 gates the real-money flag on — twenty DRY_RUN sessions
    — so it is written whether or not anything was confirmed. A gate that counted only the
    eventful days would be satisfied by twenty interesting mornings rather than by twenty
    ordinary ones.
    """
    existing = (
        await session.execute(
            select(SwSession).where(SwSession.user_id == user_id, SwSession.session_date == on)
        )
    ).scalar_one_or_none()
    plans = [plan_id] if plan_id else []
    if existing is None:
        session.add(
            SwSession(
                user_id=user_id,
                session_date=on,
                mode="LIVE" if execution else "DRY_RUN",
                monitor_ran=False,
                plan_ids={"plans": plans},
                notes="swing-eod",
            )
        )
    else:
        known = existing.plan_ids if isinstance(existing.plan_ids, dict) else {}
        recorded = known.get("plans")
        seen: list[str] = [str(entry) for entry in recorded] if isinstance(recorded, list) else []
        existing.plan_ids = {"plans": [*seen, *(p for p in plans if p not in seen)]}
    await session.flush()
    return await sessions_logged(session, user_id=user_id)


async def sessions_logged(session: AsyncSession, *, user_id: int) -> int:
    return int(
        (
            await session.execute(
                select(func.count()).select_from(SwSession).where(SwSession.user_id == user_id)
            )
        ).scalar_one()
    )


#: Who the audit row says moved the rung. Read off the settings module's own table so the name
#: the settings page shows and the name this job writes cannot drift apart.
LADDER_CHANGED_BY: Final = SYSTEM_OWNED_FIELDS["exposure_level"]
#: Who counts the first live sessions down (A9): the same evening, never a request.
FIRST_LIVE_CHANGED_BY: Final = SYSTEM_OWNED_FIELDS["first_live_sessions_left"]


async def count_first_live_session(
    session: AsyncSession, *, user_id: int, on: dt.date, execution_enabled: bool, now: dt.datetime
) -> int:
    """`02` §3.5 / STANDING-ANSWERS A9: the countdown, moved once per LIVE session that closes.

    Returns ``sw_config.first_live_sessions_left`` after tonight. A DRY_RUN session counts for
    nothing — the paper sessions are not the first live ones. A LIVE session brings the count
    down by one, and the session row records that it did (``first_live_counted``), so the
    evening re-run for the same date reads the mark and leaves the count alone; a desk restart
    mid-countdown changes nothing because nothing in the desk writes it. The change is audited
    like the rung (`03` §1b), ``changed_by = "swing-eod"``, with the session date in the note.
    """
    row = (
        await session.execute(select(SwConfig).where(SwConfig.user_id == user_id))
    ).scalar_one_or_none()
    if row is None:
        return 0
    left = int(row.first_live_sessions_left)
    if not execution_enabled or left <= 0:
        return left
    day = (
        await session.execute(
            select(SwSession).where(SwSession.user_id == user_id, SwSession.session_date == on)
        )
    ).scalar_one_or_none()
    if day is None or day.first_live_counted:
        return left
    await record_system_change(
        session,
        user_id=user_id,
        field="first_live_sessions_left",
        value=left - 1,
        changed_by=FIRST_LIVE_CHANGED_BY,
        now=now,
        note=f"{on.isoformat()} closed LIVE: first live sessions {left} -> {left - 1}",
    )
    day.first_live_counted = True
    await session.flush()
    return left - 1


@dataclass(frozen=True, slots=True)
class LadderSettlement:
    """What the ladder did tonight, and what it read to do it."""

    rung_before: int
    tier: ExposureTier
    #: The R-multiples the ladder read, oldest first — `sw_market_daily.detail` keeps them so the
    #: rung is explainable (`03` §3), and the journal page shows them (`06` SW8's AC).
    closed_r: tuple[Decimal, ...]
    #: Which book they came from: always ``"real"`` since SW10.5 (A10; PACK.6's paper clause is
    #: void). Kept on the record so an old row that says ``"simulated"`` reads as what it was.
    reads: str
    #: `04` §8.5: the sleeve's NAV against its peak tonight, and the lock-out it carried in.
    drawdown: SleeveDrawdown


async def closed_r_multiples(
    session: AsyncSession,
    *,
    user_id: int,
    on: dt.date,
    count: int,
    simulated: bool = False,
) -> tuple[Decimal, ...]:
    """The last ``count`` closed trades' R, oldest first, closed **on or before** ``on``.

    Bounded by date, unlike the detection job's reader, because the evening can be re-run for a
    past session (a weekend re-detect, a `make swing DATE=` repair) and a ladder that read closes
    from after that date would be sizing yesterday with tomorrow's results — house rule 5.
    The ladder reads the real book (``simulated=False``, A10 — SW10.5); a `CLOSED` row without
    an `r_multiple` is a close-out that never finished writing and is not a trade.
    """
    rows = await session.execute(
        select(SwPosition.r_multiple)
        .where(
            SwPosition.user_id == user_id,
            SwPosition.state == "CLOSED",
            SwPosition.simulated.is_(simulated),
            SwPosition.r_multiple.is_not(None),
            SwPosition.closed_on.is_not(None),
            SwPosition.closed_on <= on,
        )
        .order_by(SwPosition.closed_on.desc(), SwPosition.id.desc())
        .limit(count)
    )
    values = [Decimal(row[0]) for row in rows if row[0] is not None]
    return tuple(reversed(values))


def _settled_rung(row: SwMarketDaily, key: str) -> int | None:
    """The ``from`` or ``to`` rung `settle_ladder` recorded on a row, if it has been settled."""
    detail = row.detail if isinstance(row.detail, dict) else {}
    settled = detail.get("ladder")
    if isinstance(settled, dict):
        value = settled.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


async def rung_in_force(session: AsyncSession, *, user_id: int, market: SwMarketDaily) -> int:
    """The rung the book carried into the session ``market`` describes.

    Three records can say it, read in this order, and each is one this job wrote itself:

    1. **Today's own settlement**, if there is one. `settle_ladder` records the rung it started
       from in ``detail.ladder.from``; a re-run reads that back rather than working it out again
       from state the first run changed.
    2. **The previous session's settlement** — ``detail.ladder.to`` on the newest earlier row.
       That rung was "the tier for the next session" (`03` §3), so it *is* today's rung.
    3. **`sw_config.exposure_level`** — the last rung any evening wrote, which is exact for the
       evening being run tonight and the best available answer for a repair of an older one.

    The previous row's ``exposure_level`` *column* is deliberately not on the list. The detection
    job writes that column too, from `sw_config` and whatever closes exist when it runs; a
    re-detect that runs *after* an evening has settled — the Saturday five-session re-scan does —
    recomputes it from the rung the evening just wrote and lands one rung higher. The settlement
    record is the fact; the column is the detection job's preview of it.

    `sw_config` is last, not first, and the reason is idempotency (house rule 7). This job
    *writes* `sw_config.exposure_level`; a job that also read its starting rung from there would
    climb the ladder twice when the same evening ran twice — five good closes in a GREEN tape
    would be rung 1 after the first run and rung 2 after a re-run that changed nothing else.
    None of the three records moves when tonight's job re-runs, so tonight's answer is the same
    however many times it is asked.
    """
    own = _settled_rung(market, "from")
    if own is not None:
        return own
    previous = (
        await session.execute(
            select(SwMarketDaily)
            .where(SwMarketDaily.user_id == user_id, SwMarketDaily.date < market.date)
            .order_by(SwMarketDaily.date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if previous is not None:
        settled = _settled_rung(previous, "to")
        if settled is not None:
            return settled
    configured = (
        await session.execute(select(SwConfig.exposure_level).where(SwConfig.user_id == user_id))
    ).scalar_one_or_none()
    return int(configured or 0)


async def settle_ladder(  # noqa: PLR0913 - one keyword per input the rung depends on
    session: AsyncSession,
    *,
    user_id: int,
    market: SwMarketDaily,
    config: SwingConfig,
    execution_enabled: bool,
    now: dt.datetime,
) -> LadderSettlement:
    """`04` §8.4 for tonight, written to both places that carry the rung.

    The gate is the day's, as the detectors measured it — this job never invents one. The rung
    it starts from is :func:`rung_in_force`, and is recorded on the row (``detail.ladder.from``)
    so a re-run starts from the same place; the closes are the last `lookback_trades` **real**
    closes (A10, SW10.5), on or before today. ``execution_enabled`` no longer picks a book; it
    is kept on the signature for the callers and the record. The answer goes:

    * to `sw_config.exposure_level` through `record_system_change`, so an `sw_config_audit` row
      says `swing-eod` moved it and from what — the same audit a person's settings change
      leaves, because "why was the book allowed four positions on the 14th" has to have an
      answer. A deployment with no `sw_config` row (nothing seeded) gets the market row only,
      and a warning: a sleeve that has not been set up has no rung to keep;
    * to the day's `sw_market_daily`, replacing the tier the detection job wrote at 21:00 from
      the same inputs — identical on an ordinary evening, and the authoritative one on a re-run.
      The morning rebuild (`swing-premarket`) and every hub tab read the row, so the row must
      say what the plan was built with.

    `record_system_change` writes nothing when the rung has not moved, so an unchanged evening
    leaves no audit row: the audit is a history of changes, not a log of runs.

    **The drawdown** (`04` §8.5, SW9.5) is settled in the same call, because it is an input to
    the same rung. `sleeve_drawdown` reads tonight's NAV of the real book against
    the peak `sw_config.sleeve_peak_inr` carries (null on the first evening: the first session
    is never locked) and the lock-out state the sleeve came in with; `exposure_tier` applies
    the hysteresis. The peak, the drawdown and the lock-out go back to `sw_config` — the two
    measurements unaudited (they move most evenings; the market row is their history), the
    lock-out audited, because "why did the book stop trading on the 14th" has to have an
    answer too — and onto the day's row beside the rung. Re-running the evening changes none
    of it: the peak is a maximum, and the lock-out the re-run reads back is the one it wrote.
    """
    gate = MarketGate(market.gate)
    rung_in = await rung_in_force(session, user_id=user_id, market=market)
    closes = await closed_r_multiples(
        session, user_id=user_id, on=market.date, count=config.market.lookback_trades
    )
    config_row = (
        await session.execute(select(SwConfig).where(SwConfig.user_id == user_id))
    ).scalar_one_or_none()
    reads = "real"
    drawdown = await sleeve_drawdown(
        session, user_id=user_id, on=market.date, config_row=config_row
    )
    tier = exposure_tier(
        current_level=rung_in,
        closed_r_multiples=closes,
        gate=gate,
        config=config.market,
        drawdown_pct=float(drawdown.pct),
        was_drawdown_locked=drawdown.was_locked,
    )
    try:
        await record_system_change(
            session,
            user_id=user_id,
            field="exposure_level",
            value=tier.level,
            changed_by=LADDER_CHANGED_BY,
            now=now,
            note=(
                f"{market.date.isoformat()} {gate.value}: rung {rung_in} -> {tier.level} on "
                f"{len(closes)} real closes "
                f"[{', '.join(str(r) for r in closes)}]"
                + (
                    f"; sleeve {drawdown.pct}% below its peak, locked out"
                    if tier.drawdown_locked
                    else ""
                )
            ),
        )
        await record_system_change(
            session,
            user_id=user_id,
            field="sleeve_peak_inr",
            value=drawdown.peak,
            changed_by=LADDER_CHANGED_BY,
            now=now,
            audited=False,
        )
        await record_system_change(
            session,
            user_id=user_id,
            field="drawdown_pct",
            value=drawdown.pct,
            changed_by=LADDER_CHANGED_BY,
            now=now,
            audited=False,
        )
        await record_system_change(
            session,
            user_id=user_id,
            field="drawdown_locked",
            value=tier.drawdown_locked,
            changed_by=LADDER_CHANGED_BY,
            now=now,
            note=(
                f"{market.date.isoformat()}: NAV {drawdown.nav} against peak {drawdown.peak}, "
                f"{drawdown.pct}% below; lock-out at {config.market.max_drawdown_pct}%, "
                f"release inside {config.market.resume_drawdown_pct}%"
            ),
        )
    except SwingConfigNotSeeded:
        log.warning(
            "no sw_config row for user %s; the rung is kept on the market row only", user_id
        )

    market.exposure_level = tier.level
    market.max_open_positions = tier.max_open_positions
    market.max_exposure_pct = Decimal(str(tier.max_exposure_pct)).quantize(Decimal("0.01"))
    market.new_entries_allowed = tier.new_entries_allowed
    market.drawdown_pct = drawdown.pct
    market.drawdown_locked = tier.drawdown_locked
    detail = dict(market.detail) if isinstance(market.detail, dict) else {}
    detail.update(
        {
            "closed_r_multiples": [str(r) for r in closes],
            "closed_trades_read": reads,
            "ladder": {"from": rung_in, "to": tier.level, "settled_by": LADDER_CHANGED_BY},
            "drawdown": {
                "nav": str(drawdown.nav),
                "peak": str(drawdown.peak),
                "pct": str(drawdown.pct),
                "was_locked": drawdown.was_locked,
                "locked": tier.drawdown_locked,
            },
        }
    )
    market.detail = detail
    await session.flush()
    return LadderSettlement(
        rung_before=rung_in, tier=tier, closed_r=closes, reads=reads, drawdown=drawdown
    )


async def run_swing_eod(  # noqa: PLR0913 - one keyword per input the evening depends on
    session: AsyncSession,
    outcome: StepOutcome,
    trade_date: dt.date,
    *,
    user_id: int,
    execution_enabled: bool = False,
    now: dt.datetime | None = None,
    mailer: Mailer | None = None,
) -> EodReport:
    """Manage the book, plan tomorrow, and record the session. Returns the report.

    Returns rather than raises when there is no market row for the date: the detectors have not
    run, and an evening job that invented a gate would be planning against a tape nobody measured.
    """
    with swing_span("swing.eod", date=trade_date.isoformat()), swing_timed("eod"):
        return await _swing_eod(
            session,
            outcome,
            trade_date,
            user_id=user_id,
            execution_enabled=execution_enabled,
            now=now,
            mailer=mailer,
        )


async def _swing_eod(  # noqa: PLR0913 - one keyword per input the evening depends on
    session: AsyncSession,
    outcome: StepOutcome,
    trade_date: dt.date,
    *,
    user_id: int,
    execution_enabled: bool,
    now: dt.datetime | None,
    mailer: Mailer | None,
) -> EodReport:
    stamp = now or dt.datetime.now(tz=dt.UTC)
    config_row = (
        await session.execute(select(SwConfig).where(SwConfig.user_id == user_id))
    ).scalar_one_or_none()
    config = await load_swing_config(session, user_id)

    market = (
        await session.execute(
            select(SwMarketDaily).where(
                SwMarketDaily.user_id == user_id, SwMarketDaily.date == trade_date
            )
        )
    ).scalar_one_or_none()
    if market is None:
        outcome.status = StepStatus.SKIPPED
        outcome.note(
            trade_date=trade_date.isoformat(),
            skipped_reason="no sw_market_daily row; the detectors have not run for this date",
        )
        return EodReport(trade_date=trade_date, gate="UNKNOWN", exposure_level=0)

    gate = MarketGate(market.gate)
    report = EodReport(trade_date=trade_date, gate=gate.value, exposure_level=market.exposure_level)

    watched = await auto_watch(session, user_id=user_id, on=trade_date, config=config)
    report.watch_added = watched.added
    report.watch_expired = watched.expired

    exits, naked, managed = await manage_open_positions(
        session, user_id=user_id, on=trade_date, config=config
    )
    report.positions_managed = managed
    report.naked_positions = naked
    report.exit_lines = len(exits)

    # After the book is managed and before tomorrow is planned: the plan must be built with the
    # rung the evening leaves behind, or `sw_config` and the plan would say different numbers.
    settled = await settle_ladder(
        session,
        user_id=user_id,
        market=market,
        config=config,
        execution_enabled=execution_enabled,
        now=stamp,
    )
    tier = settled.tier
    report.rung_before = settled.rung_before
    report.exposure_level = tier.level
    report.closed_r = [str(r) for r in settled.closed_r]
    report.drawdown_pct = str(settled.drawdown.pct)
    report.drawdown_locked = tier.drawdown_locked

    # A9: the session is counted — and, if it was LIVE, the first-live countdown moved — BEFORE
    # tomorrow is planned, so the plan is sized with the count the session leaves behind.
    await _record_session(
        session, user_id=user_id, on=trade_date, plan_id=None, execution=execution_enabled
    )
    left = await count_first_live_session(
        session, user_id=user_id, on=trade_date, execution_enabled=execution_enabled, now=stamp
    )
    multiplier = first_live_multiplier(
        sessions_left=left, execution_enabled=execution_enabled, config=config.sizing
    )
    report.first_live_sessions_left = left
    report.risk_multiplier = str(multiplier)
    report.risk_pct_in_force = risk_pct_in_force(config, multiplier)

    items, instrument_ids = await watch_items(session, user_id=user_id, on=trade_date)
    report.watching = len(items)
    account = await sleeve_account(session, user_id=user_id, config_row=config_row)
    entries, skipped = build_entries(
        as_of=trade_date,
        watch=items,
        account=account,
        gate=gate,
        tier=tier,
        config=config,
        risk_multiplier=multiplier,
    )
    report.entry_lines = sum(1 for line in entries if line.kind is LineKind.BUY_ON_TRIGGER)
    report.pending_lines = sum(1 for line in entries if line.kind is LineKind.PENDING_RANGE)
    report.skips = len(skipped)

    plan = assemble(
        as_of=trade_date,
        gate=gate,
        tier=tier,
        entries=entries,
        exits=exits,
        skipped=skipped,
    )
    # The exit lines name symbols that are held rather than watched, so their instrument ids come
    # from the book rather than from the watchlist.
    instrument_ids.update(await held_instrument_ids(session, user_id=user_id))
    report.plan_id = await store_plan(
        session,
        plan,
        user_id=user_id,
        source="EOD_PREVIEW",
        instrument_ids=instrument_ids,
        now=stamp,
    )
    report.sessions_logged = await _record_session(
        session,
        user_id=user_id,
        on=trade_date,
        plan_id=report.plan_id,
        execution=execution_enabled,
    )

    sent = await send_eod_email(
        session,
        user_id=user_id,
        report=report,
        plan_lines=plan.lines,
        skipped=plan.skipped,
        mailer=mailer,
    )

    outcome.rows_out = len(plan.lines)
    outcome.note(**report.as_detail(), email_sent_to=sent)
    return report


def _candidate(line: PlanLine) -> SwingCandidate:
    return SwingCandidate(
        symbol=line.symbol,
        setup=line.setup.value if line.setup else line.kind.value,
        trigger="—" if line.trigger is None else f"{line.trigger:,.2f}",
        stop="—" if line.stop is None else f"{line.stop:,.2f}",
        note=line.note,
    )


async def send_eod_email(  # noqa: PLR0913 - one keyword per part of the message
    session: AsyncSession,
    *,
    user_id: int,
    report: EodReport,
    plan_lines: tuple[PlanLine, ...],
    skipped: tuple[Skipped, ...],
    mailer: Mailer | None = None,
    settings: Settings | None = None,
) -> str | None:
    """`05` §4's evening email. Returns the address it went to, or ``None`` if it did not send.

    Never raises. An email that could not be built or delivered must not take down an evening
    job whose real output is two database tables — the plan is on the page whether or not the
    message arrived, and a job that failed here would leave the book unmanaged tomorrow.
    """
    resolved = settings or get_settings()
    address = (
        await session.execute(select(AppUser.email).where(AppUser.id == user_id))
    ).scalar_one_or_none()
    if address is None:
        return None

    flags = await _watchlist_candidates(session, user_id=user_id, setup=Setup.FLAG)
    eps = await _watchlist_candidates(session, user_id=user_id, setup=Setup.EP)
    digest = SwingDigest(
        as_of=report.trade_date,
        gate=report.gate,
        exposure_level=report.exposure_level,
        max_open_positions=0,
        naked=tuple(report.naked_positions),
        exits=tuple(
            _candidate(line)
            for line in plan_lines
            if line.kind in (LineKind.SELL_AT_OPEN, LineKind.RAISE_GTT_STOP)
        ),
        # A PENDING_RANGE line (A7) is an entry the range has not priced yet; it is listed
        # with the entries, its note saying so, never with the exits.
        entries=tuple(
            _candidate(line)
            for line in plan_lines
            if line.kind in (LineKind.BUY_ON_TRIGGER, LineKind.PENDING_RANGE)
        ),
        skips=tuple(
            (skip.symbol, f"{skip.reason.value} {skip.detail}".strip()) for skip in skipped
        ),
        flags=flags,
        eps=eps,
        sessions_logged=report.sessions_logged,
        sessions_required=PAPER_SESSIONS_REQUIRED,
    )
    message = swing_eod(str(address), digest, swing_url=f"{resolved.public_api_base_url}/swing")
    try:
        await (mailer or Mailer(build_transport(resolved))).deliver(message)
    except Exception as exc:
        log.warning("swing EOD email not sent: %s", exc)
        return None
    return str(address)


#: `05` §4: "up to 10 flags and all EPs with levels". An email is a summary; the page is the list.
MAX_FLAGS_IN_EMAIL: Final = 10


async def _watchlist_candidates(
    session: AsyncSession, *, user_id: int, setup: Setup
) -> tuple[SwingCandidate, ...]:
    rows = [row for row in await list_watch(session, user_id=user_id) if row.setup == setup.value]
    if setup is Setup.FLAG:
        rows = rows[:MAX_FLAGS_IN_EMAIL]
    return tuple(
        SwingCandidate(
            symbol=row.symbol,
            setup=row.setup,
            trigger="—" if row.trigger is None else f"{row.trigger:,.2f}",
            stop="—" if row.stop_ref is None else f"{row.stop_ref:,.2f}",
            note=row.catalyst or "",
        )
        for row in rows
    )


async def held_instrument_ids(session: AsyncSession, *, user_id: int) -> dict[str, int]:
    rows = await session.execute(
        select(Instrument.symbol, SwPosition.instrument_id)
        .join(Instrument, Instrument.id == SwPosition.instrument_id)
        .where(SwPosition.user_id == user_id, SwPosition.quantity_open > 0)
    )
    return {str(symbol): int(instrument_id) for symbol, instrument_id in rows}


__all__ = [
    "FIRST_LIVE_CHANGED_BY",
    "LADDER_CHANGED_BY",
    "MA_LOOKBACK_SESSIONS",
    "PAPER_SESSIONS_REQUIRED",
    "EodReport",
    "LadderSettlement",
    "LineKind",
    "Skipped",
    "closed_r_multiples",
    "count_first_live_session",
    "first_live_header",
    "held_instrument_ids",
    "manage_open_positions",
    "risk_pct_in_force",
    "run_swing_eod",
    "rung_in_force",
    "sessions_logged",
    "settle_ladder",
    "sleeve_account",
    "store_plan",
    "watch_items",
]
