"""The O2 plan builder on the database: ``op_session`` + ``op_plan`` + ``op_leg`` (``06`` OP7).

``baskfy_core.options.plan_o2`` decides — day filters, trigger, expiry, strike, delta, sizing,
costs, ``plan_id``, expiry, exits. This module reads its inputs (the same loaders the scan uses,
so a plan and the scan's candidate are built from the same rows — ``04`` §10), writes the result,
and returns the ``OPTIONS_PLAN`` alert for the caller to send.

* **No broker call at all.** O2's ceiling is the premium cap of ``04`` §4.6, computed in the pure
  core; ``04`` §7.4 asks the margin calculator for O1 and O3 only (OP7.3). This builder is a
  database read and a database write — it imports no provider and no order path (law 2), and a
  test scans the module for both.
* **Idempotent per date** (house rule 7). A session that already left ``OBSERVING`` is returned
  as it stands — the same ``plan_id`` or the same skip — and nothing is recomputed or sent again.
  The ``plan_id`` is deterministic (``plan.plan_id_for``); the session insert is ``ON CONFLICT DO
  NOTHING`` on ``(user_id, sleeve, trade_date)``.
* **One trigger a day, one plan a day.** The trigger minute's snapshot prices the plan
  (``plan_o2.wanted_minutes`` names the minute to load), and once the session is decided the day
  is over for this sleeve whatever the tape then does (``04`` §4.2).
* **Nothing here confirms or sends.** A plan is ``ISSUED`` and lapses at ``expires_at`` —
  ``plan.lapse_expired`` (shared with O1) moves it and its session to ``LAPSED``. Confirming is
  the desk's ``POST /nifty-options/execute`` with ``confirm=true`` (OP10); there is no
  auto-execute for any options sleeve (PACK.3) and none is built here.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import OpLeg, OpPlan, OpSession
from baskfy_core.models.base import JsonObject
from baskfy_core.options.config import Mode, OptionsCeilings, OptionsConfig, Sleeve
from baskfy_core.options.plan_o2 import (
    O2Outcome,
    O2Plan,
    decide_o2,
    finalize_o2,
    wanted_minutes,
)
from baskfy_core.options.scan import Snapshot, SnapshotBook
from baskfy_core.options.session import SessionState
from baskfy_core.options.structures import to_json
from baskfy_worker.alerts import Alert, AlertName, Severity
from baskfy_worker.ops import RUNBOOKS
from baskfy_worker.options import options_gates
from baskfy_worker.options.index_bars import IST
from baskfy_worker.options.plan import (
    PlanReport,
    costs_json,
    existing_session,
    lapse_expired,
    leg_row,
    session_values,
    write_session_row,
)
from baskfy_worker.options.scan import load_context, load_market, load_snapshot

log = logging.getLogger("baskfy_worker.options.plan_o2")

RUNBOOK: Final = RUNBOOKS[AlertName.OPTIONS_PLAN]
#: How long after the entry window the builder still runs: long enough for
#: ``find_trigger``'s "the window closed with no trigger" to become final (OP7.5).
_AFTER_WINDOW_MINUTES: Final = 4


def _modes(sleeve: Sleeve) -> Mode:
    return options_gates(sleeve).mode


def _money(value: object) -> str:
    return str(value)


def plan_detail(plan: O2Plan) -> JsonObject:
    """``op_plan.detail``: what the columns do not carry — the itemised costs, the exits of
    ``04`` §4.5, the worst case of §4.6, the day's numbers and the decision minute."""
    exits = plan.exits
    return {
        "costs": costs_json(plan.costs),
        "premium_inr": _money(plan.premium_inr),
        "gap_through_inr": _money(plan.gap_through_inr),
        "direction": plan.direction.value,
        "exits": {
            "stop_price": _money(exits.stop_price),
            "target_price": _money(exits.target_price),
            "invalidation_level": _money(exits.invalidation_level),
            "time_stop_minutes": exits.time_stop_minutes,
            "time_stop_min_price": _money(exits.time_stop_min_price),
            "hard_exit_at": exits.hard_exit_at.isoformat(),
        },
        "half_size": plan.half_size,
        "warnings": list(plan.warnings),
        "gate": dict(plan.numbers),
        "as_of_minute": plan.as_of_minute.isoformat() if plan.as_of_minute else None,
        "mode": plan.mode.value,
    }


# --- the alert -----------------------------------------------------------------------------------


def _leg_json(plan: O2Plan) -> JsonObject:
    (leg,) = plan.legs
    return {
        "seq": leg.seq,
        "role": leg.role.value,
        "tradingsymbol": leg.tradingsymbol,
        "side": leg.side.value,
        "quantity": leg.quantity,
        "limit_price": str(leg.limit_price),
        "bid": None if leg.bid is None else str(leg.bid),
        "ask": None if leg.ask is None else str(leg.ask),
        "delta": leg.delta,
    }


def plan_alert(outcome: O2Outcome) -> Alert:
    """``OPTIONS_PLAN`` for O2: the verdict, or the plan in one paragraph a person can act on."""
    decision = outcome.decision
    day = decision.trade_date.isoformat()
    labels = {"trade_date": day, "sleeve": Sleeve.O2.value, "state": outcome.state.value}
    plan = outcome.plan
    if plan is None:
        reasons = ", ".join(outcome.reasons) or "no reason recorded"
        return Alert(
            name=AlertName.OPTIONS_PLAN,
            severity=Severity.WARNING,
            summary=f"O2 {day}: no trade — {reasons}.",
            labels=labels,
            detail={
                "reasons": list(outcome.reasons),
                "gate": dict(decision.numbers),
                "candidate": to_json(decision.candidate) if decision.candidate else None,
            },
            runbook=RUNBOOK,
        )
    (leg,) = plan.legs
    exits = plan.exits
    summary = (
        f"O2 {day} {plan.mode.value} plan {plan.plan_id}: BUY {leg.quantity} {leg.tradingsymbol} "
        f"@ {leg.limit_price} ({plan.lots} lot(s) of {plan.lot_size}, {plan.sizing_mode.value}) on "
        f"the {plan.direction.value} break of {plan.numbers.get('trigger_level')} at "
        f"{plan.numbers.get('trigger_time')}; premium ₹{plan.premium_inr}, stop {exits.stop_price} "
        f"(risk ₹{plan.max_loss_inr}), target {exits.target_price} (₹{plan.profit_target_inr}), "
        f"time stop {exits.time_stop_minutes} min, hard exit "
        f"{exits.hard_exit_at.astimezone(IST):%H:%M}; gap-through worst case "
        f"₹{plan.gap_through_inr}; round-trip costs ₹{plan.expected_cost_inr} "
        f"(share {plan.cost_share}). Expires {plan.expires_at.astimezone(IST):%H:%M}; nothing is "
        f"sent without a confirm on the desk."
    )
    return Alert(
        name=AlertName.OPTIONS_PLAN,
        severity=Severity.WARNING,
        summary=summary,
        labels=labels | {"plan_id": plan.plan_id},
        detail=plan_detail(plan) | {"legs": [_leg_json(plan)]},
        runbook=RUNBOOK,
    )


# --- writes --------------------------------------------------------------------------------------


async def _write_plan(session: AsyncSession, user_id: int, session_pk: int, plan: O2Plan) -> None:
    row = OpPlan(
        user_id=user_id,
        plan_id=plan.plan_id,
        session_id=session_pk,
        sleeve=plan.sleeve.value,
        structure=plan.structure.value,
        kind="ENTRY",
        sizing_mode=plan.sizing_mode.value,
        issued_at=plan.issued_at,
        entry_window_end=plan.entry_window_end,
        expires_at=plan.expires_at,
        credit_points=None,
        debit_points=plan.debit_points,
        width_points=None,
        lots=plan.lots,
        lot_size=plan.lot_size,
        risk_per_lot_inr=plan.risk_per_lot_inr,
        risk_budget_inr=plan.risk_budget_inr,
        max_loss_inr=plan.max_loss_inr,
        profit_target_inr=plan.profit_target_inr,
        stop_inr=plan.stop_inr,
        expected_cost_inr=plan.expected_cost_inr,
        cost_share=plan.cost_share,
        margin_required_inr=None,  # a long option costs its premium; 04 §7.4 is O1/O3 (OP7.3)
        status="ISSUED",
        detail=plan_detail(plan),
    )
    session.add(row)
    await session.flush()
    simulated = plan.mode is Mode.PAPER
    await session.execute(
        insert(OpLeg), [leg_row(lg, int(row.id), user_id, simulated) for lg in plan.legs]
    )


def _report_of(row: OpSession) -> PlanReport:
    return PlanReport(
        sleeve=row.sleeve,
        trade_date=row.trade_date.isoformat(),
        state=row.state,
        reasons=list(row.skip_reasons),
        plan_id=row.plan_id,
        created=False,
    )


# --- one morning ---------------------------------------------------------------------------------


@dataclass(slots=True)
class BuildResult:
    report: PlanReport
    alert: Alert | None = None


async def build_o2_plan(  # noqa: PLR0913 - the session, the tenant, the clock and the rules
    session: AsyncSession,
    user_id: int,
    now: dt.datetime,
    *,
    trading_day: bool,
    config: OptionsConfig | None = None,
    ceilings: OptionsCeilings | None = None,
    mode_of: Callable[[Sleeve], Mode] = _modes,
) -> BuildResult:
    """``build_plan(O2, date, trigger)``: return today's decided session as it stands, or decide
    now and write it. The alert is returned only when this call wrote the session."""
    cfg = config or OptionsConfig()
    ceil = ceilings or OptionsCeilings()
    day = now.astimezone(IST).date()
    current = await existing_session(session, user_id, Sleeve.O2, day)
    if current is not None and current.state != SessionState.OBSERVING.value:
        return BuildResult(_report_of(current))
    market = await load_market(session, day, trading_day, cfg)
    mode = mode_of(Sleeve.O2)
    context = await load_context(session, user_id, day, mode_of)
    loaded: dict[dt.datetime | None, Snapshot | None] = {}
    for minute in wanted_minutes(market, context, now, cfg, ceil):
        loaded[minute] = await load_snapshot(session, minute, now, day)
    decision = decide_o2(market, context, SnapshotBook(loaded), now, options=cfg, ceilings=ceil)
    report = PlanReport(
        sleeve=Sleeve.O2.value,
        trade_date=day.isoformat(),
        state=decision.state.value,
        reasons=list(decision.reasons),
    )
    if not decision.writes_session:
        return BuildResult(report)
    outcome = finalize_o2(decision, user_id=user_id, mode=mode, options=cfg)
    values = session_values(
        sleeve=Sleeve.O2,
        trade_date=day,
        expiry=decision.expiry,
        numbers=dict(decision.numbers),
        candidate=decision.candidate,
        as_of_minute=decision.as_of_minute,
        verdict=decision.verdict,
        mode=mode,
        plan_id=outcome.plan.plan_id if outcome.plan is not None else None,
        reasons=outcome.reasons,
    )
    session_pk = await write_session_row(session, user_id, Sleeve.O2, day, values, current)
    if session_pk is None:  # another builder decided this morning first
        raced = await existing_session(session, user_id, Sleeve.O2, day)
        return BuildResult(_report_of(raced) if raced is not None else report)
    if outcome.plan is not None:
        await _write_plan(session, user_id, session_pk, outcome.plan)
    report.state = outcome.state.value
    report.reasons = list(outcome.reasons)
    report.plan_id = outcome.plan.plan_id if outcome.plan is not None else None
    report.created = True
    report.detail = {"snapshots": [m.isoformat() for m in loaded if m is not None]}
    return BuildResult(report, plan_alert(outcome))


# --- the minute the task runs --------------------------------------------------------------------


def plan_o2_gate_free(
    now: dt.datetime,
    *,
    monitor_enabled: bool,
    collect_enabled: bool,
    user_id: int | None,
    config: OptionsConfig | None = None,
) -> str | None:
    """The refusals that cost nothing, checked before a database session (OP6.6's rule, O2's
    window).

    Raising plans is the options monitor's job (``02``: ``BASKFY_OPTIONS_MONITOR_ENABLED`` —
    "raises plans"; operational, moves no money), so the builder is dark until that flag is on;
    with no collector there is no chain to price a plan from. The window is O2's entry window plus
    four minutes, so a day that never triggered is still written as ``SKIPPED / NO_TRIGGER``.
    """
    if not monitor_enabled:
        return "BASKFY_OPTIONS_MONITOR_ENABLED is false"
    if not collect_enabled:
        return "BASKFY_OPTIONS_COLLECT_ENABLED is false (no chain to plan from)"
    cfg = config or OptionsConfig()
    clock = now.astimezone(IST).time()
    first = cfg.directional.entry_window_start
    last = (
        dt.datetime.combine(dt.date.min, cfg.directional.entry_window_end)
        + dt.timedelta(minutes=_AFTER_WINDOW_MINUTES)
    ).time()
    if not first <= clock <= last:
        return f"outside {first:%H:%M}-{last:%H:%M} IST"
    if user_id is None:
        return "no BASKFY_SOLE_USER_ID configured"
    return None


async def plan_o2_minute(  # noqa: PLR0913 - the session, the tenant, the clock and the rules
    session: AsyncSession,
    user_id: int,
    now: dt.datetime,
    *,
    trading_day: bool,
    config: OptionsConfig | None = None,
    ceilings: OptionsCeilings | None = None,
    mode_of: Callable[[Sleeve], Mode] = _modes,
) -> tuple[JsonObject, list[Alert]]:
    """One minute of the O2 builder: lapse what expired, then decide today's O2 session."""
    lapsed = await lapse_expired(session, user_id, now)
    result = await build_o2_plan(
        session,
        user_id,
        now,
        trading_day=trading_day,
        config=config,
        ceilings=ceilings,
        mode_of=mode_of,
    )
    alerts = [result.alert] if result.alert is not None else []
    return {
        "at": now.isoformat(),
        "lapsed": list(lapsed),
        "sleeves": [result.report.as_dict()],
    }, alerts


__all__ = [
    "BuildResult",
    "build_o2_plan",
    "plan_alert",
    "plan_detail",
    "plan_o2_gate_free",
    "plan_o2_minute",
]
