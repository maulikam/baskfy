"""TW6 — the evening and morning plans for the three-weeks-tight sleeve (``docs/twt/04`` §§10, 11).

Every trading session, after the detector has written what it saw and computed tomorrow's
trigger, this job decides what the desk should be asked to confirm and writes it as a ``tw_plan``
a person can act on line by line:

1. **Exits first**, because a morning that runs out of attention should have armed the stops
   (``04`` §10.3, ``05`` §2). An ``OPEN`` position with shares and no resting GTT is an
   ``ARM_GTT``; one whose evening-computed ``next_trigger`` exceeds its resting ``gtt_trigger``
   is a ``RAISE_GTT_STOP`` — **the ratchet**, and the one mechanism this sleeve adds to Baskfy.
2. **Then the entries.** ``build_entries`` over the session's entry events, ranked by the signal
   session's own turnover, against the gate, the sleeve's own money and the book (``04`` §10.1) —
   with every name it passed over recorded as a ``tw_plan_skip``, because a plan is not honest
   without its skips.
3. **Then the session row**, which is what the page's counters read.

**Nothing here places an order.** Every line is ``PROPOSED``; a person confirms it on the desk,
and only then does anything reach ``OrderGateway``. ``02`` Track C §3, and there is no flag that
changes it: non-negotiable 1's named exception belongs to the swing sleeve alone.

THE CLOCK
---------
``04`` §11: the plan is built from the **last completed session**. ``EVENING`` builds it from
that session's close for the next morning; ``MORNING`` rebuilds the *same* plan before the open,
from the same signals and the same levels, **re-sized and not re-detected** (``04`` §11.3) —
because the desk's plans expire in thirty minutes and an evening plan cannot be confirmed at
09:15. The two differ in `tw_plan.source` and in the equity they were sized against, and in
nothing else.

TWO THINGS THIS MODULE DOES THAT VBT-1'S EVENING DOES NOT
---------------------------------------------------------
**It offers ``SCAN_ONLY`` rows to the plan.** VBT-1 drops them before building; ``04`` §10.1
names ``BELOW_LIQUIDITY_FLOOR`` as one of its skips in order, and a skip the plan never sees is
a skip that cannot be recorded. The funnel is the argument for a ₹5 crore floor, and a plan that
silently dropped the names the floor rejected could not show it. DECISIONS-TW **TW6.1**.

**It tells the two causes of a null trigger apart.** A position with no ``RAISE_GTT_STOP`` line
either did not ratchet (the ordinary case, on a book whose stop moves only on a new high) or
ratcheted *down* because a corporate action re-derived a lower level and TW0.7 refused. The
second is a ``TWT_ADJUSTMENT_RESET``: the GTT resting at the exchange is quoting a pre-split
price on a post-split instrument and **a person has to look at it** (``04`` §7.3). They are told
apart by ``ohlcv_daily.adj_factor != tw_position.entry_adj_factor``, which is the same test the
ratchet itself used.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_api.email import Mailer
from baskfy_api.settings import Settings
from baskfy_api.twt_settings import TwtConfigNotSeeded, read_config
from baskfy_api.twt_sleeve import (
    OPEN_STATE,
    book_state,
    config_for,
    load_sleeve,
    slot_multiplier,
)
from baskfy_core.models import (
    Instrument,
    OhlcvDaily,
    TradingDay,
    TwBreadthDaily,
    TwConfig,
    TwPlan,
    TwPlanLine,
    TwPlanSkip,
    TwPosition,
    TwSession,
    TwSignalDaily,
    TwStateDaily,
)
from baskfy_core.models.base import JsonObject
from baskfy_core.models.twt import TW_PLAN_TTL_MINUTES
from baskfy_core.seed_data import NSE_EXCHANGE_ID
from baskfy_core.twt.config import DEFAULT_TWT_CONFIG, Gate, SignalState, TwtConfig
from baskfy_core.twt.plan import (
    Candidate,
    LineKind,
    PlanLine,
    TwtPlan,
    assemble,
    build_entries,
    client_id_for,
    exit_lines,
)
from baskfy_worker.alerts import Alert, AlertName, Severity, dispatch
from baskfy_worker.ops import RUNBOOKS
from baskfy_worker.steps import StepOutcome, StepStatus

log = logging.getLogger(__name__)

#: ``tw_plan.source`` — the two this job writes. ``MANUAL`` exists in the schema for a rebuild a
#: person asks the desk for and is nobody's default.
SOURCE_EVENING: Final = "EVENING"
SOURCE_MORNING: Final = "MORNING"

#: What the ``tw_config_audit`` trail says when this job moves a system-owned field.
CHANGED_BY: Final = "twt-evening"

#: One runbook for all four ``TWT_*`` names (``baskfy_worker.ops.RUNBOOKS``).
RUNBOOK: Final = RUNBOOKS[AlertName.TWT_EVENING]

_ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class EveningReport:
    """What the evening did. The step payload and the ``TWT_EVENING`` alert are both this."""

    session: dt.date
    source: str
    gate: str
    plan_id: str
    equity_inr: Decimal
    entries: int
    arms: int
    ratchets: int
    skips: int
    adjustments: int
    naked: int

    def as_detail(self) -> JsonObject:
        return {
            "session": self.session.isoformat(),
            "source": self.source,
            "gate": self.gate,
            "plan_id": self.plan_id,
            "equity_inr": str(self.equity_inr),
            "entries": self.entries,
            "arms": self.arms,
            "ratchets": self.ratchets,
            "skips": self.skips,
            "adjustments": self.adjustments,
            "naked": self.naked,
        }


async def load_twt_config(session: AsyncSession, user_id: int) -> TwtConfig:
    """The engine's configuration with this user's ``tw_config`` written into it (``03`` §1).

    An unseeded sleeve gets the shipped defaults, which is arithmetically irrelevant: its equity
    is ₹0 and every signal is skipped ``NO_SLEEVE_CAPITAL`` before a threshold is consulted.
    """
    try:
        row = await read_config(session, user_id)
    except TwtConfigNotSeeded:
        return DEFAULT_TWT_CONFIG
    return config_for(row)


async def config_row(session: AsyncSession, user_id: int) -> TwConfig | None:
    return (
        await session.execute(select(TwConfig).where(TwConfig.user_id == user_id))
    ).scalar_one_or_none()


async def gate_for_session(session: AsyncSession, user_id: int, as_of: dt.date) -> Gate:
    """The gate of the **signal's own close**, never a later one (``04`` §11).

    A session with no breadth row is ``SHUT``: the detector has not spoken about it, and the
    honest reading of "we do not know what the tape was" is the one that buys nothing.
    """
    value = (
        await session.execute(
            select(TwBreadthDaily.gate).where(
                TwBreadthDaily.user_id == user_id, TwBreadthDaily.date == as_of
            )
        )
    ).scalar_one_or_none()
    return Gate(value) if value else Gate.SHUT


async def candidates_for(session: AsyncSession, user_id: int, as_of: dt.date) -> list[Candidate]:
    """The session's entry events, as ``04`` §10.1 needs them — **including ``SCAN_ONLY``**.

    A ``SCAN_ONLY`` row is an entry event the liquidity floor rejected. It is offered to
    :func:`baskfy_core.twt.plan.build_entries` rather than filtered out here, so the refusal is
    recorded as ``BELOW_LIQUIDITY_FLOOR`` on the plan instead of vanishing between two modules.
    DECISIONS-TW **TW6.1**.

    Both prices are the signal session's ``entry_reference_close`` — an **exchange** price
    (``03`` §3). The evening plan previews the size and the stop against it; the confirm re-sizes
    (``04`` §10.5).
    """
    rows = (
        await session.execute(
            select(TwSignalDaily, Instrument.symbol)
            .join(Instrument, Instrument.id == TwSignalDaily.instrument_id)
            .where(TwSignalDaily.user_id == user_id, TwSignalDaily.date == as_of)
            .order_by(TwSignalDaily.rank_key.desc(), Instrument.symbol)
        )
    ).all()
    return [
        Candidate(
            instrument_id=int(row.instrument_id),
            symbol=str(symbol),
            signal_date=row.date,
            rank_key=Decimal(str(row.rank_key or 0)),
            turnover_avg_inr=(
                None if row.turnover_avg_20 is None else Decimal(str(row.turnover_avg_20))
            ),
            entry_price=Decimal(str(row.entry_reference_close)),
            stop_reference_price=Decimal(str(row.entry_reference_close)),
            entry_bar=None,
        )
        for row, symbol in rows
    ]


async def adjusted_positions(
    session: AsyncSession, user_id: int, as_of: dt.date
) -> dict[int, tuple[int, str, Decimal, Decimal]]:
    """Open positions whose instrument was re-adjusted under the hold (``04`` §7.3).

    Keyed by ``instrument_id``; the value is ``(position id, symbol, entry factor, as-of
    factor)``. The comparison is the one the ratchet made: the as-of ``ohlcv_daily`` row's
    ``adj_factor`` against the position's ``entry_adj_factor``. A position with no bar on the
    session is not here — nothing changed that anybody can see.
    """
    rows = (
        await session.execute(
            select(
                TwPosition.id,
                TwPosition.instrument_id,
                Instrument.symbol,
                TwPosition.entry_adj_factor,
                OhlcvDaily.adj_factor,
            )
            .join(Instrument, Instrument.id == TwPosition.instrument_id)
            .join(
                OhlcvDaily,
                (OhlcvDaily.instrument_id == TwPosition.instrument_id) & (OhlcvDaily.date == as_of),
            )
            .where(
                TwPosition.user_id == user_id,
                TwPosition.state == OPEN_STATE,
                TwPosition.quantity_open > 0,
            )
        )
    ).all()
    return {
        int(instrument_id): (int(position_id), str(symbol), Decimal(str(entry)), Decimal(str(now)))
        for position_id, instrument_id, symbol, entry, now in rows
        if Decimal(str(entry)) != Decimal(str(now))
    }


def annotate_adjustments(
    lines: list[PlanLine], adjusted: dict[int, tuple[int, str, Decimal, Decimal]]
) -> list[PlanLine]:
    """``04`` §7.3 step 3: the ``RAISE_GTT_STOP`` line **carries the reason**.

    A raise on a name that has just split is not the ordinary ratchet — the level was re-derived
    from a rewritten series — and the person confirming it needs to be told so on the row, not in
    a separate message they may not have read.
    """
    out: list[PlanLine] = []
    for line in lines:
        found = adjusted.get(line.instrument_id)
        if found is None or line.kind is not LineKind.RAISE_GTT_STOP:
            out.append(line)
            continue
        _position_id, _symbol, entry_factor, now_factor = found
        out.append(
            PlanLine(
                kind=line.kind,
                instrument_id=line.instrument_id,
                symbol=line.symbol,
                quantity=line.quantity,
                entry_price=line.entry_price,
                stop_price=line.stop_price,
                previous_stop=line.previous_stop,
                high_since=line.high_since,
                value_inr=line.value_inr,
                cap=line.cap,
                note=(
                    f"a corporate action re-adjusted this name under the hold "
                    f"({entry_factor} -> {now_factor}); the level was re-derived and the "
                    f"resting trigger is a pre-adjustment price — check it at the broker"
                ),
            )
        )
    return out


async def store_plan(  # noqa: PLR0913 - a stored plan is its plan and its context
    session: AsyncSession,
    *,
    user_id: int,
    as_of: dt.date,
    source: str,
    plan: TwtPlan,
    now: dt.datetime,
) -> uuid.UUID:
    """Replace this ``(session, source)``'s plan. A re-run rewrites rather than duplicates.

    The 30-minute expiry is :data:`baskfy_core.models.twt.TW_PLAN_TTL_MINUTES`, read from the
    schema module rather than written here, because the writer and the reader (``/twt/execute``)
    must not each carry their own number (``04`` §10.4).
    """
    existing = list(
        (
            await session.execute(
                select(TwPlan.id).where(
                    TwPlan.user_id == user_id,
                    TwPlan.session_date == as_of,
                    TwPlan.source == source,
                )
            )
        )
        .scalars()
        .all()
    )
    if existing:
        await session.execute(delete(TwPlan).where(TwPlan.id.in_(existing)))
        await session.flush()
    plan_id = uuid.uuid4()
    row = TwPlan(
        plan_id=plan_id,
        user_id=user_id,
        session_date=as_of,
        source=source,
        built_at=now,
        expires_at=now + dt.timedelta(minutes=TW_PLAN_TTL_MINUTES),
        plan_hash=plan.plan_hash(),
        gate=plan.gate.value,
        sleeve_equity_inr=plan.sleeve_equity_inr,
        total_new_exposure_inr=plan.total_new_exposure_inr,
    )
    session.add(row)
    await session.flush()
    for line in plan.lines:
        session.add(
            TwPlanLine(
                plan_id=row.id,
                user_id=user_id,
                instrument_id=line.instrument_id,
                kind=line.kind.value,
                state="PROPOSED",
                quantity=line.quantity,
                stop_price=line.stop_price,
                value_inr=line.value_inr,
                high_since=line.high_since,
                previous_trigger=line.previous_stop,
                note=line.note,
                client_id=client_id_for(str(plan_id), line.symbol, line.kind),
            )
        )
    for skip in plan.skips:
        session.add(
            TwPlanSkip(
                plan_id=row.id,
                user_id=user_id,
                instrument_id=skip.instrument_id,
                reason=skip.reason.value,
                detail=skip.detail,
            )
        )
    await session.flush()
    return plan_id


async def _counts(session: AsyncSession, user_id: int, as_of: dt.date) -> tuple[int, int]:
    """``tw_session.states`` and ``tw_session.signals`` — what the detector saw."""
    states = (
        await session.execute(
            select(func.count())
            .select_from(TwStateDaily)
            .where(TwStateDaily.user_id == user_id, TwStateDaily.date == as_of)
        )
    ).scalar_one()
    signals = (
        await session.execute(
            select(func.count())
            .select_from(TwSignalDaily)
            .where(
                TwSignalDaily.user_id == user_id,
                TwSignalDaily.date == as_of,
                TwSignalDaily.state == SignalState.SIGNAL.value,
            )
        )
    ).scalar_one()
    return int(states), int(signals)


async def settle_session(  # noqa: PLR0913 - the session row is its counters
    session: AsyncSession,
    *,
    user_id: int,
    as_of: dt.date,
    gate: Gate,
    plan_id: uuid.UUID,
    source: str,
    execution_enabled: bool,
) -> TwSession:
    """The ``tw_session`` row for the session the plan reads.

    **It does not move the first-live countdown.** ``04`` §6.4 counts *filled entries*, by the
    session that filled them, and TW5's :func:`baskfy_api.twt_sleeve.count_first_live_entry` is
    the only door. An evening that decremented it would spend the half-size discipline on a plan
    nobody confirmed.

    ``confirms``, ``fills``, ``ratchets``, ``exits`` and ``naked_at_1515`` are the desk's
    counters; this job never resets them, so a morning rebuild after two confirms does not erase
    the two confirms.

    **``dry_run_sessions`` is not moved here either, and that is deliberate rather than
    forgotten.** ``02`` §3 makes it information, not a gate — Maulik's 11 Sep decision removed
    the session count, so ``DRY_RUN_SESSIONS_REQUIRED`` is 0 and nothing reads the column. VBT-1
    increments its equivalent because twenty of them *are* its gate. A counter this job moved
    that nothing consulted would be a number that looks like a condition and is not, which is
    the one thing ``02`` §3 was rewritten to stop.
    """
    row = (
        await session.execute(
            select(TwSession).where(TwSession.user_id == user_id, TwSession.session_date == as_of)
        )
    ).scalar_one_or_none()
    if row is None:
        row = TwSession(user_id=user_id, session_date=as_of)
        session.add(row)
    states, signals = await _counts(session, user_id, as_of)
    row.mode = "LIVE" if execution_enabled else "DRY_RUN"
    row.gate = gate.value
    row.states = states
    row.signals = signals
    row.plan_ids = {**(row.plan_ids or {}), source.lower(): str(plan_id)}
    await session.flush()
    return row


async def _raise_adjustment_alert(
    adjusted: dict[int, tuple[int, str, Decimal, Decimal]],
    *,
    as_of: dt.date,
    settings: Settings | None,
    mailer: Mailer | None,
) -> JsonObject | None:
    """``04`` §7.3 step 3 — and it fires whether or not a raise was emitted.

    The alert is not "the trigger moved"; it is "the resting GTT is quoting a pre-split price on
    a post-split instrument". That is true in both branches, and the branch where **nothing** was
    emitted — the one TW0.7 refuses — is the one where the alert is the only thing that happens.
    """
    if not adjusted:
        return None
    return await dispatch(
        Alert(
            name=AlertName.TWT_ADJUSTMENT_RESET,
            severity=Severity.CRITICAL,
            summary=(
                f"{len(adjusted)} three-weeks-tight position(s) were re-adjusted under the hold "
                f"on {as_of.isoformat()}; a resting GTT may be quoting a pre-adjustment price."
            ),
            labels={"trade_date": as_of.isoformat()},
            detail={
                "positions": [
                    {
                        "id": position_id,
                        "instrument_id": instrument_id,
                        "symbol": symbol,
                        "entry_adj_factor": str(entry),
                        "adj_factor": str(now),
                    }
                    for instrument_id, (position_id, symbol, entry, now) in sorted(adjusted.items())
                ]
            },
            runbook=RUNBOOK,
        ),
        settings,
        mailer=mailer,
    )


async def _raise_evening_alert(
    report: EveningReport,
    plan: TwtPlan,
    *,
    settings: Settings | None,
    mailer: Mailer | None,
) -> JsonObject | None:
    """The ``TWT_EVENING`` digest (``06`` § TW6). Never raises.

    A message that could not be built or delivered must not take down an evening job whose real
    output is four database tables: the plan is on the page whether or not the mail arrived.
    """
    try:
        return await dispatch(
            Alert(
                name=AlertName.TWT_EVENING,
                severity=Severity.WARNING,
                summary=(
                    f"TWT {report.source.lower()} plan for {report.session.isoformat()}: gate "
                    f"{report.gate}, {report.entries} entr{'y' if report.entries == 1 else 'ies'}, "
                    f"{report.arms} arm(s), {report.ratchets} ratchet(s), {report.skips} skip(s)."
                ),
                labels={"trade_date": report.session.isoformat(), "source": report.source},
                detail={
                    **report.as_detail(),
                    "lines": [
                        {
                            "kind": line.kind.value,
                            "symbol": line.symbol,
                            "quantity": line.quantity,
                            "stop_price": None if line.stop_price is None else str(line.stop_price),
                            "value_inr": str(line.value_inr),
                            "note": line.note,
                        }
                        for line in plan.lines
                    ],
                    "skipped": [
                        {"symbol": skip.symbol, "reason": skip.reason.value, "detail": skip.detail}
                        for skip in plan.skips
                    ],
                },
                runbook=RUNBOOK,
            ),
            settings,
            mailer=mailer,
        )
    except Exception as exc:  # the plan is the output; the message is a courtesy
        log.warning("TWT evening alert not sent: %s", exc)
        return None


async def run_twt_evening(  # noqa: PLR0913 - one keyword per input the evening depends on
    session: AsyncSession,
    outcome: StepOutcome,
    trade_date: dt.date,
    *,
    user_id: int,
    execution_enabled: bool = False,
    source: str = SOURCE_EVENING,
    now: dt.datetime | None = None,
    settings: Settings | None = None,
    mailer: Mailer | None = None,
    notify: bool = True,
) -> EveningReport | None:
    """Build the session's plan and settle its row. ``None`` when there is nothing to plan from.

    **Nothing here places an order.** Every line is ``PROPOSED`` until a person confirms it on
    the desk, and there is no argument to this function that changes that: ``execution_enabled``
    decides the *session's mode* and the half-size multiplier, not whether anything is sent.

    ``MORNING`` runs the identical body against the identical session (``04`` §11.3). It re-sizes
    — the sleeve's equity has moved with the marks — and it re-detects nothing, because it reads
    the same ``tw_signal_daily`` rows the evening read.
    """
    stamp = now or dt.datetime.now(tz=dt.UTC)
    gate_row = (
        await session.execute(
            select(TwBreadthDaily.date).where(
                TwBreadthDaily.user_id == user_id, TwBreadthDaily.date == trade_date
            )
        )
    ).scalar_one_or_none()
    if gate_row is None:
        outcome.status = StepStatus.SKIPPED
        outcome.note(
            skipped_reason=(
                f"{trade_date.isoformat()} has no tw_breadth_daily row: the detector has not "
                f"spoken about this session, and a plan built without its gate is a guess"
            )
        )
        return None

    config = await load_twt_config(session, user_id)
    settings_row = await config_row(session, user_id)
    gate = await gate_for_session(session, user_id, trade_date)
    sleeve = await load_sleeve(session, user_id, trade_date)
    equity = sleeve.value.quantize().equity_inr
    book = await book_state(session, user_id=user_id, as_of=trade_date, signal_date=trade_date)
    multiplier = await slot_multiplier(
        session, user_id=user_id, execution_enabled=execution_enabled, config=config
    )
    candidates = await candidates_for(session, user_id, trade_date)
    entries, skips = build_entries(
        candidates,
        gate=gate,
        equity=equity,
        book=book,
        config=config,
        slot_multiplier=multiplier,
        max_open_positions=None if settings_row is None else settings_row.max_open_positions,
    )
    adjusted = await adjusted_positions(session, user_id, trade_date)
    exits = annotate_adjustments(exit_lines(book, trade_date), adjusted)
    plan = assemble(trade_date, gate, equity, exits, entries, skips, source)
    plan_id = await store_plan(
        session, user_id=user_id, as_of=trade_date, source=source, plan=plan, now=stamp
    )
    await settle_session(
        session,
        user_id=user_id,
        as_of=trade_date,
        gate=gate,
        plan_id=plan_id,
        source=source,
        execution_enabled=execution_enabled,
    )
    report = EveningReport(
        session=trade_date,
        source=source,
        gate=gate.value,
        plan_id=str(plan_id),
        equity_inr=equity,
        entries=_count(plan.lines, LineKind.BUY_AT_OPEN),
        arms=_count(plan.lines, LineKind.ARM_GTT),
        ratchets=_count(plan.lines, LineKind.RAISE_GTT_STOP),
        skips=len(plan.skips),
        adjustments=len(adjusted),
        naked=len(book.positions_naked_of_gtt),
    )
    if notify:
        await _raise_adjustment_alert(adjusted, as_of=trade_date, settings=settings, mailer=mailer)
        await _raise_evening_alert(report, plan, settings=settings, mailer=mailer)
    outcome.rows_out = len(plan.lines)
    outcome.note(mark_fallbacks=len(sleeve.stale_marks), **report.as_detail())
    return report


def _count(lines: tuple[PlanLine, ...], kind: LineKind) -> int:
    return sum(1 for line in lines if line.kind is kind)


async def last_detected_session(
    session: AsyncSession, user_id: int, on_or_before: dt.date
) -> dt.date | None:
    """The most recent session the detector wrote a **tradeable** breadth row for.

    What the morning plan is built from. ``04`` §11.1: the plan reads the last *completed*
    session, and before the open that is yesterday — but "yesterday" is a calendar word and the
    honest answer is the last session this sleeve actually saw. A thin session is skipped,
    because the rules did not trade it (``04`` §2.1).
    """
    return (
        await session.execute(
            select(TwBreadthDaily.date)
            .where(
                TwBreadthDaily.user_id == user_id,
                TwBreadthDaily.date <= on_or_before,
                TwBreadthDaily.thin_session.is_(False),
            )
            .order_by(TwBreadthDaily.date.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def next_trading_day(session: AsyncSession, after: dt.date) -> dt.date | None:
    """The session a ``BUY_AT_OPEN`` would fill in, from the exchange's own calendar."""
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
