"""The FO ledger, pauses and paper checklist — the database half (``docs/fno/06`` FO10).

The arithmetic is :mod:`baskfy_core.fno.ledger` and :mod:`baskfy_core.fno.checklist` (pure);
this module reads ``fo_journal``, ``fo_position``, ``fo_mark``, ``fo_fill``, ``fo_plan``,
``fo_scan`` and ``fo_config_audit`` and hands them over. **Read-only** except
:func:`lift_f1_pause`, which appends one ``fo_config_audit`` row. It is the reader FO5's API
calls (``read_ledger``, ``read_pauses``, ``read_checklist``) and the scan's source for ``04`` §7.

Never pooled: every reader takes or returns figures per ``simulated`` (DECISIONS-FO FO10.1).
The desk writes the journal and the stored pauses (``app/fno_ledger.py``); nothing here does.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Final

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.fno.checklist import (
    Checklist,
    CycleOutcome,
    F1Cycle,
    F2Close,
    LegFill,
    Violation,
    ViolationKind,
    into_expiry,
    journal_gap,
    mark_gaps,
    missed_cycles,
    paper_checklist,
    uncovered_steps,
)
from baskfy_core.fno.config import (
    DEFAULT_FNO_CEILINGS,
    DEFAULT_FNO_CONFIG,
    FnoCeilings,
    FnoConfig,
    FoSleeve,
    PlanKind,
    ScanState,
    Structure,
)
from baskfy_core.fno.journal import CloseKind
from baskfy_core.fno.ledger import (
    ClosedTrade,
    FoPause,
    Ledger,
    OpenMark,
    build_ledger,
    evaluate_pauses,
)
from baskfy_core.models import (
    FoBookConfig,
    FoConfigAudit,
    FoFill,
    FoIngestDay,
    FoJournal,
    FoLeg,
    FoMark,
    FoPlan,
    FoPosition,
    FoScan,
    TradingDay,
)
from baskfy_core.models.base import JsonObject
from baskfy_core.options.config import Side
from baskfy_worker.fno.ingest import STATUS_INGESTED

#: Whether an FO entry today is journalled ``simulated``. Every one is: the FO gateway is
#: paper-pinned and a LIVE sleeve is refused before any order (DECISIONS-FO FO7.1), so the scan
#: evaluates the paper ledger's pauses. The desk, which reads its own switches, evaluates the mode
#: it actually trades when it raises a plan (FO10.1). Flip with the live read-back.
ENTRIES_SIMULATED: Final = True
#: ``fo_config_audit.key`` of Maulik's lift of an F1 underlying's pause (FO10.2).
LIFT_KEY: Final = "lift:{sleeve}"
_F1: Final = (FoSleeve.F1N, FoSleeve.F1B)
_VIOLATION_CODES: Final = {k.value: k for k in ViolationKind}


def _aware(at: dt.datetime) -> dt.datetime:
    return at if at.tzinfo is not None else at.replace(tzinfo=dt.UTC)


async def load_trades(session: AsyncSession, user_id: int) -> list[ClosedTrade]:
    """Every ``fo_journal`` row of the user, both modes, each carrying its ``simulated``."""
    rows = await session.execute(
        select(FoJournal, FoPosition.closed_at)
        .join(FoPosition, FoPosition.id == FoJournal.position_id)
        .where(FoJournal.user_id == user_id)
        .order_by(FoJournal.closed_on, FoJournal.position_id)
    )
    return [
        ClosedTrade(
            position_id=int(j.position_id),
            sleeve=FoSleeve(j.sleeve),
            symbol=j.symbol,
            simulated=bool(j.simulated),
            closed_on=j.closed_on,
            closed_at=None if closed_at is None else _aware(closed_at),
            net_pnl_inr=Decimal(j.net_pnl_inr),
            r_multiple=Decimal(j.r_multiple),
            closed_reason=j.closed_reason,
            rolls=int(j.rolls),
        )
        for j, closed_at in rows
    ]


async def load_lifts(session: AsyncSession, user_id: int) -> dict[FoSleeve, dt.datetime]:
    """The latest lift of each F1 underlying's pause (``fo_config_audit``, FO10.2)."""
    keys = {LIFT_KEY.format(sleeve=s.value): s for s in _F1}
    rows = await session.execute(
        select(FoConfigAudit.key, func.max(FoConfigAudit.changed_at))
        .where(FoConfigAudit.user_id == user_id, FoConfigAudit.key.in_(list(keys)))
        .group_by(FoConfigAudit.key)
    )
    return {keys[key]: _aware(at) for key, at in rows if at is not None}


async def load_open_marks(session: AsyncSession, user_id: int) -> list[OpenMark]:
    """Each open position's latest ``fo_mark`` (a position not yet marked carries ₹0)."""
    latest = (
        select(FoMark.position_id, func.max(FoMark.trade_date).label("day"))
        .where(FoMark.user_id == user_id)
        .group_by(FoMark.position_id)
        .subquery()
    )
    rows = await session.execute(
        select(FoPosition, FoMark.trade_date, FoMark.pnl_inr)
        .outerjoin(latest, latest.c.position_id == FoPosition.id)
        .outerjoin(
            FoMark,
            (FoMark.position_id == FoPosition.id) & (FoMark.trade_date == latest.c.day),
        )
        .where(FoPosition.user_id == user_id, FoPosition.closed_at.is_(None))
        .order_by(FoPosition.id)
    )
    return [
        OpenMark(
            position_id=int(p.id),
            sleeve=FoSleeve(p.sleeve),
            symbol=p.symbol,
            simulated=bool(p.simulated),
            trade_date=day,
            pnl_inr=Decimal(pnl) if pnl is not None else Decimal(0),
            r_inr=None if p.max_loss_inr is None else Decimal(p.max_loss_inr),
        )
        for p, day, pnl in rows
    ]


async def read_ledger(session: AsyncSession, user_id: int, as_of: dt.date) -> Ledger:
    """Per sleeve and underlying and per book, per ``simulated``: FO5's ledger read."""
    return build_ledger(
        await load_trades(session, user_id), await load_open_marks(session, user_id), as_of
    )


async def read_pauses(  # noqa: PLR0913 - the session, the user, the day and the rules
    session: AsyncSession,
    user_id: int,
    as_of: dt.date,
    *,
    simulated: bool,
    config: FnoConfig = DEFAULT_FNO_CONFIG,
    ceilings: FnoCeilings = DEFAULT_FNO_CEILINGS,
) -> tuple[FoPause, ...]:
    """``04`` §7 for one mode on ``as_of`` (the pauses the journal earns; the stored and
    hand-set ones are ``fo_sleeve_config``/``fo_book_config``'s columns)."""
    book = await session.get(FoBookConfig, user_id)
    return evaluate_pauses(
        await load_trades(session, user_id),
        simulated=simulated,
        as_of=as_of,
        monthly_pause_inr=Decimal(0) if book is None else Decimal(book.monthly_pause_inr),
        lifted_after=await load_lifts(session, user_id),
        config=config,
        ceilings=ceilings,
    )


async def lift_f1_pause(
    session: AsyncSession, user_id: int, sleeve: FoSleeve, *, changed_by: str, note: str
) -> None:
    """Maulik's lift of an F1 underlying's pause: one audit row; only closes after it count
    toward the next run (FO10.2). FO5's settings form is its caller."""
    if sleeve not in _F1:
        raise ValueError(f"only an F1 underlying's pause is lifted by hand, not {sleeve.value}")
    await session.execute(
        insert(FoConfigAudit).values(
            user_id=user_id,
            scope="F1",
            key=LIFT_KEY.format(sleeve=sleeve.value),
            old_value="paused",
            new_value="lifted",
            changed_by=changed_by,
            note=note,
        )
    )


# --- the paper checklist -------------------------------------------------------------------------


def _day(value: object) -> dt.date | None:
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and value:
        return dt.date.fromisoformat(value[:10])
    return None


async def _sessions(session: AsyncSession, as_of: dt.date) -> list[dt.date]:
    rows = await session.execute(
        select(TradingDay.date)
        .where(TradingDay.is_trading_day.is_(True), TradingDay.date <= as_of)
        .order_by(TradingDay.date)
    )
    return list(rows.scalars())


async def _landed(session: AsyncSession, as_of: dt.date) -> set[dt.date]:
    rows = await session.execute(
        select(FoIngestDay.trade_date).where(
            FoIngestDay.status == STATUS_INGESTED, FoIngestDay.trade_date <= as_of
        )
    )
    return set(rows.scalars())


def _recorded(plan: FoPlan) -> list[Violation]:
    """``fo_plan.detail.violations`` (the desk's ``LATE_EXIT``, ``NAKED_FUTURE``; FO7.12)."""
    detail: JsonObject = plan.detail or {}
    raw = detail.get("violations")
    found: list[Violation] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        kind = _VIOLATION_CODES.get(str(item.get("code")))
        on = _day(item.get("at")) or plan.trade_date
        if kind is not None:
            found.append(
                Violation(kind, FoSleeve(plan.sleeve), on, plan.plan_id, str(item.get("message")))
            )
    return found


def _leg_expiry(position: FoPosition) -> dt.date | None:
    raw = position.legs.get("legs")
    legs = raw if isinstance(raw, list) else []
    days = [d for d in (_day(x.get("expiry")) for x in legs if isinstance(x, dict)) if d]
    return min(days) if days else None


async def _cycles(
    session: AsyncSession, user_id: int, as_of: dt.date, plans: Mapping[str, FoPlan],
    opened: set[str],
) -> list[F1Cycle]:  # fmt: skip
    rows = await session.execute(
        select(FoScan)
        .where(
            FoScan.user_id == user_id,
            FoScan.sleeve.in_([s.value for s in _F1]),
            FoScan.state != ScanState.NOT_ENTRY_DAY.value,
        )
        .order_by(FoScan.trade_date)
    )
    latest: dict[tuple[FoSleeve, dt.date], FoScan] = {}
    for scan in rows.scalars():
        entry = _day(scan.detail.get("entry_session"))
        if entry is not None and entry <= as_of and scan.trade_date < entry:
            latest[(FoSleeve(scan.sleeve), entry)] = scan
    out: list[F1Cycle] = []
    for (sleeve, entry), scan in sorted(latest.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        plan = next(
            (
                p
                for p in plans.values()
                if p.sleeve == sleeve.value and p.trade_date == entry and p.kind == "ENTRY"
            ),
            None,
        )
        if plan is not None and plan.plan_id in opened:
            out.append(F1Cycle(sleeve, entry, CycleOutcome.OPENED, plan.plan_id))
        elif plan is None and scan.state == ScanState.CANDIDATE.value:
            out.append(
                F1Cycle(
                    sleeve,
                    entry,
                    CycleOutcome.MISSED,
                    f"{sleeve.value} was a candidate for {entry.isoformat()} and no plan was "
                    "raised: the desk was not there (04 §9)",
                )
            )
        else:
            why = plan.status if plan is not None else scan.state
            out.append(F1Cycle(sleeve, entry, CycleOutcome.SKIPPED, why))
    return out


async def read_checklist(session: AsyncSession, user_id: int, as_of: dt.date) -> Checklist:
    """``04`` §9's tally for the paper period at ``as_of`` (paper rows only)."""
    positions = list(
        (
            await session.execute(
                select(FoPosition).where(
                    FoPosition.user_id == user_id, FoPosition.simulated.is_(True)
                )
            )
        ).scalars()
    )
    plans = {
        p.plan_id: p
        for p in (await session.execute(select(FoPlan).where(FoPlan.user_id == user_id))).scalars()
    }
    journalled = set(
        (
            await session.execute(
                select(FoJournal.position_id).where(
                    FoJournal.user_id == user_id, FoJournal.simulated.is_(True)
                )
            )
        ).scalars()
    )
    marks: dict[int, set[dt.date]] = {}
    for pid, day in await session.execute(
        select(FoMark.position_id, FoMark.trade_date).where(FoMark.user_id == user_id)
    ):
        marks.setdefault(int(pid), set()).add(day)
    sessions = await _sessions(session, as_of)
    landed = await _landed(session, as_of)
    opened = {
        p.entry_plan_id for p in positions if p.closed_reason != CloseKind.ABANDONED_PARTIAL.value
    }
    violations: list[Violation] = []
    paper_entries = {p.entry_plan_id for p in positions}
    for plan in plans.values():
        if plan.kind == PlanKind.ENTRY.value and plan.plan_id in paper_entries:
            violations.extend(_recorded(plan))
    for p in positions:
        sleeve = FoSleeve(p.sleeve)
        ref = str(p.id)
        closed_on = None if p.closed_at is None else p.closed_at.date()
        if p.closed_reason == CloseKind.ABANDONED_PARTIAL.value:
            continue
        found = into_expiry(
            sleeve=sleeve, ref=ref, expiry=_leg_expiry(p), closed_on=closed_on, as_of=as_of
        )
        if found is not None:
            violations.append(found)
        if closed_on is not None:
            gap = journal_gap(
                sleeve=sleeve, ref=ref, closed_on=closed_on, journalled=int(p.id) in journalled
            )
            if gap is not None:
                violations.append(gap)
        opened_on = p.opened_at.date()
        until = closed_on or as_of
        held = [d for d in sessions if opened_on <= d < until and d in landed]
        violations.extend(
            mark_gaps(sleeve=sleeve, ref=ref, held_sessions=held, marked=marks.get(int(p.id), ()))
        )
    violations.extend(await _uncovered(session, user_id, positions))
    cycles = await _cycles(session, user_id, as_of, plans, opened)
    violations.extend(missed_cycles(cycles))
    f2_days = [
        p.trade_date
        for p in plans.values()
        if p.sleeve == FoSleeve.F2.value and p.kind == PlanKind.ENTRY.value
    ]
    closes = [
        F2Close(t.closed_on, t.rolls)
        for t in await load_trades(session, user_id)
        if t.simulated
        and t.sleeve is FoSleeve.F2
        and t.closed_reason != CloseKind.ABANDONED_PARTIAL.value
    ]
    return paper_checklist(
        as_of=as_of,
        f1_cycles=cycles,
        f2_start=min(f2_days) if f2_days else None,
        f2_closes=closes,
        sessions=sessions,
        violations=violations,
    )


async def _uncovered(
    session: AsyncSession, user_id: int, positions: Sequence[FoPosition]
) -> list[Violation]:
    condors = {
        int(p.id): FoSleeve(p.sleeve)
        for p in positions
        if p.structure == Structure.IRON_CONDOR.value
    }
    if not condors:
        return []
    rows = await session.execute(
        select(
            FoFill.position_id,
            FoLeg.role,
            FoFill.side,
            FoFill.quantity,
            FoFill.filled_at,
            FoPlan.plan_id,
        )
        .join(FoLeg, FoLeg.id == FoFill.leg_id)
        .join(FoPlan, FoPlan.id == FoLeg.plan_id)
        .where(FoFill.user_id == user_id, FoFill.position_id.in_(list(condors)))
        .order_by(FoFill.position_id, FoFill.filled_at, FoFill.id)
    )
    by_position: dict[int, list[LegFill]] = {}
    for pid, role, side, qty, at, plan_id in rows:
        by_position.setdefault(int(pid), []).append(
            LegFill(str(role), Side(side), int(qty), str(plan_id), at.date())
        )
    found: list[Violation] = []
    for pid, fills in by_position.items():
        found.extend(uncovered_steps(condors[pid], fills))
    return found


__all__ = [
    "ENTRIES_SIMULATED",
    "LIFT_KEY",
    "lift_f1_pause",
    "load_lifts",
    "load_open_marks",
    "load_trades",
    "read_checklist",
    "read_ledger",
    "read_pauses",
]
