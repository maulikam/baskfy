"""The O3 plan builder on the database: ``op_session`` + ``op_plan`` + ``op_leg`` (``06`` OP8).

``baskfy_core.options.plan_o3`` decides — role, setup, the day's slot, strikes, debit, sizing,
costs, margin, ``plan_id``, expiry, exits. This module reads its inputs (the same loaders the scan
uses, so a plan and the scan's candidate are built from the same rows — ``04`` §10), asks the
broker's margin *calculator* about the hedged two-leg basket (``04`` §7.4 — a calculation, never
an order), writes the result, and returns the ``OPTIONS_PLAN`` alert for the caller to send.

* **Idempotent per date and setup** (house rule 7). A session that already left ``OBSERVING``
  is returned as it stands; the ``plan_id`` is deterministic; the session insert is ``ON CONFLICT
  DO NOTHING`` on ``(user_id, sleeve, trade_date)``.
* **O3-B first, then O3-A, every minute** — so O3-A reads a session O3-B wrote this morning, and
  the one-O3-a-day rule (``04`` §5.5, OP8.2) is decided from the rows, never from memory.
* **Nothing here confirms or sends.** A plan is ``ISSUED`` and lapses at ``expires_at``
  (``plan.lapse_expired``, shared). Confirming is the desk's ``POST /nifty-options/execute`` with
  ``confirm=true`` (OP10); there is no auto-execute for any options sleeve (PACK.3).
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
from baskfy_core.options.plan import MarginQuote, PlanState
from baskfy_core.options.plan_o3 import (
    O3Outcome,
    O3Plan,
    contract_label,
    decide_o3,
    finalize_o3,
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
    MarginReader,
    PlanReport,
    ask_margin,
    costs_json,
    existing_session,
    lapse_expired,
    leg_row,
    margin_in_use,
    margin_pool,
    session_values,
    write_session_row,
)
from baskfy_worker.options.scan import load_context, load_market, load_snapshot

log = logging.getLogger("baskfy_worker.options.plan_o3")

RUNBOOK: Final = RUNBOOKS[AlertName.OPTIONS_PLAN]
#: O3-B decides before O3-A reads its session (``04`` §5.5).
ORDER: Final = (Sleeve.O3B, Sleeve.O3A)
#: How long after O3-A's entry window the builder still runs: long enough for the
#: ``NO_TRIGGER`` verdict to become final (the 13:00 bar plus ``stale_scan_seconds``).
_AFTER_WINDOW_MINUTES: Final = 4


def _modes(sleeve: Sleeve) -> Mode:
    return options_gates(sleeve).mode


def plan_detail(plan: O3Plan) -> JsonObject:
    """``op_plan.detail``: what the columns do not carry — the itemised costs, the exits of
    ``04`` §5.3, the setup's numbers, the margin answer and the decision minute."""
    exits = plan.exits
    return {
        "costs": costs_json(plan.costs),
        "debit_inr": str(plan.debit_inr),
        "direction": plan.direction.value,
        "exits": {
            "target_value": str(exits.target_value),
            "stop_value": str(exits.stop_value),
            "invalidation": exits.invalidation,
            "invalidation_level": str(exits.invalidation_level),
            "hard_exit_at": exits.hard_exit_at.isoformat(),
        },
        "margin": None
        if plan.margin is None
        else {"code": plan.margin.code.value, "message": plan.margin.message},
        "half_size": plan.half_size,
        "warnings": list(plan.warnings),
        "gate": dict(plan.numbers),
        "as_of_minute": plan.as_of_minute.isoformat() if plan.as_of_minute else None,
        "mode": plan.mode.value,
    }


def _legs_json(plan: O3Plan) -> list[JsonObject]:
    return [
        {
            "seq": lg.seq,
            "role": lg.role.value,
            "tradingsymbol": lg.tradingsymbol,
            "side": lg.side.value,
            "quantity": lg.quantity,
            "limit_price": str(lg.limit_price),
            "bid": None if lg.bid is None else str(lg.bid),
            "ask": None if lg.ask is None else str(lg.ask),
        }
        for lg in plan.legs
    ]


def plan_alert(outcome: O3Outcome) -> Alert:
    """``OPTIONS_PLAN`` for an O3 setup: the verdict, or the plan in one paragraph a person can act
    on."""
    decision = outcome.decision
    sleeve = decision.sleeve.value
    day = decision.trade_date.isoformat()
    labels = {"trade_date": day, "sleeve": sleeve, "state": outcome.state.value}
    plan = outcome.plan
    if plan is None:
        reasons = ", ".join(outcome.reasons) or "no reason recorded"
        return Alert(
            name=AlertName.OPTIONS_PLAN,
            severity=Severity.WARNING,
            summary=f"{sleeve} {day}: no trade — {reasons}.",
            labels=labels,
            detail={
                "reasons": list(outcome.reasons),
                "gate": dict(decision.numbers),
                "candidate": to_json(decision.candidate) if decision.candidate else None,
            },
            runbook=RUNBOOK,
        )
    exits = plan.exits
    summary = (
        f"{sleeve} {day} {plan.mode.value} plan {plan.plan_id}: {plan.direction.value} debit "
        f"spread {contract_label(plan)} — BUY {plan.legs[0].tradingsymbol} first, then SELL "
        f"{plan.legs[1].tradingsymbol}; {plan.lots} lot(s) of {plan.lot_size} "
        f"({plan.sizing_mode.value}); debit {plan.debit_points} of a {plan.width_points}-point "
        f"width = max loss ₹{plan.max_loss_inr}; target value {exits.target_value} "
        f"(₹{plan.profit_target_inr}), stop value {exits.stop_value} (₹{plan.stop_inr}), "
        f"invalidation at {exits.invalidation_level}, hard exit "
        f"{exits.hard_exit_at.astimezone(IST):%H:%M}; round-trip costs ₹{plan.expected_cost_inr} "
        f"(share {plan.cost_share}); margin "
        f"{'unknown' if plan.margin_required_inr is None else f'₹{plan.margin_required_inr}'}. "
        f"Expires {plan.expires_at.astimezone(IST):%H:%M}; nothing is sent without a confirm on "
        f"the desk."
    )
    return Alert(
        name=AlertName.OPTIONS_PLAN,
        severity=Severity.WARNING,
        summary=summary,
        labels=labels | {"plan_id": plan.plan_id},
        detail=plan_detail(plan) | {"legs": _legs_json(plan)},
        runbook=RUNBOOK,
    )


# --- writes --------------------------------------------------------------------------------------


async def _write_plan(session: AsyncSession, user_id: int, session_pk: int, plan: O3Plan) -> None:
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
        width_points=plan.width_points,
        lots=plan.lots,
        lot_size=plan.lot_size,
        risk_per_lot_inr=plan.risk_per_lot_inr,
        risk_budget_inr=plan.risk_budget_inr,
        max_loss_inr=plan.max_loss_inr,
        profit_target_inr=plan.profit_target_inr,
        stop_inr=plan.stop_inr,
        expected_cost_inr=plan.expected_cost_inr,
        cost_share=plan.cost_share,
        margin_required_inr=plan.margin_required_inr,
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


# --- one setup, one morning ----------------------------------------------------------------------


@dataclass(slots=True)
class BuildResult:
    report: PlanReport
    alert: Alert | None = None


async def build_o3_plan(  # noqa: PLR0913 - the session, the tenant, the setup, the clock and the rules
    session: AsyncSession,
    user_id: int,
    sleeve: Sleeve,
    now: dt.datetime,
    *,
    trading_day: bool,
    margin: MarginReader | None,
    config: OptionsConfig | None = None,
    ceilings: OptionsCeilings | None = None,
    mode_of: Callable[[Sleeve], Mode] = _modes,
) -> BuildResult:
    """``build_plan(O3A | O3B, date, trigger)``: return today's decided session as it stands, or
    decide now and write it. The alert is returned only when this call wrote the session."""
    cfg = config or OptionsConfig()
    ceil = ceilings or OptionsCeilings()
    day = now.astimezone(IST).date()
    current = await existing_session(session, user_id, sleeve, day)
    if current is not None and current.state != SessionState.OBSERVING.value:
        return BuildResult(_report_of(current))
    market = await load_market(session, day, trading_day, cfg)
    mode = mode_of(sleeve)
    context = await load_context(session, user_id, day, mode_of)
    loaded: dict[dt.datetime | None, Snapshot | None] = {}
    for minute in wanted_minutes(sleeve, market, context, now, cfg, ceil):
        loaded[minute] = await load_snapshot(session, minute, now, day)
    decision = decide_o3(
        sleeve, market, context, SnapshotBook(loaded), now, options=cfg, ceilings=ceil
    )
    report = PlanReport(
        sleeve=sleeve.value,
        trade_date=day.isoformat(),
        state=decision.state.value,
        reasons=list(decision.reasons),
    )
    if not decision.writes_session:
        return BuildResult(report)
    asked = "not asked"
    quote: MarginQuote | None = None
    if decision.state is PlanState.PLANNED:
        quote, asked = ask_margin(margin, decision)
    outcome = finalize_o3(
        decision,
        quote,
        user_id=user_id,
        mode=mode,
        margin_pool_inr=await margin_pool(session, user_id),
        margin_in_use_inr=await margin_in_use(session, user_id, day),
        options=cfg,
    )
    final = outcome.decision
    values = session_values(
        sleeve=sleeve,
        trade_date=day,
        expiry=final.expiry,
        numbers=dict(final.numbers),
        candidate=final.candidate,
        as_of_minute=final.as_of_minute,
        verdict=final.verdict,
        mode=mode,
        plan_id=outcome.plan.plan_id if outcome.plan is not None else None,
        reasons=outcome.reasons,
    )
    session_pk = await write_session_row(session, user_id, sleeve, day, values, current)
    if session_pk is None:  # another builder decided this morning first
        raced = await existing_session(session, user_id, sleeve, day)
        return BuildResult(_report_of(raced) if raced is not None else report)
    if outcome.plan is not None:
        await _write_plan(session, user_id, session_pk, outcome.plan)
    report.state = outcome.state.value
    report.reasons = list(outcome.reasons)
    report.plan_id = outcome.plan.plan_id if outcome.plan is not None else None
    report.created = True
    report.detail = {
        "margin": asked,
        "snapshots": [m.isoformat() for m in loaded if m is not None],
    }
    return BuildResult(report, plan_alert(outcome))


# --- the minute the task runs --------------------------------------------------------------------


def plan_o3_gate_free(
    now: dt.datetime,
    *,
    monitor_enabled: bool,
    collect_enabled: bool,
    user_id: int | None,
    config: OptionsConfig | None = None,
) -> str | None:
    """The refusals that cost nothing, checked before a database session (OP6.6's rule, O3's
    windows: from O3-B's plan time to O3-A's entry window end plus four minutes, so a quiet day is
    still written as ``SKIPPED / NO_TRIGGER``)."""
    if not monitor_enabled:
        return "BASKFY_OPTIONS_MONITOR_ENABLED is false"
    if not collect_enabled:
        return "BASKFY_OPTIONS_COLLECT_ENABLED is false (no chain to plan from)"
    cfg = (config or OptionsConfig()).expiry_setups
    clock = now.astimezone(IST).time()
    first = min(cfg.o3b_plan_time, cfg.o3a_window_start)
    last = (
        dt.datetime.combine(dt.date.min, cfg.o3a_entry_window_end)
        + dt.timedelta(minutes=_AFTER_WINDOW_MINUTES)
    ).time()
    if not first <= clock <= last:
        return f"outside {first:%H:%M}-{last:%H:%M} IST"
    if user_id is None:
        return "no BASKFY_SOLE_USER_ID configured"
    return None


async def plan_o3_minute(  # noqa: PLR0913 - the session, the tenant, the clock and the rules
    session: AsyncSession,
    user_id: int,
    now: dt.datetime,
    *,
    trading_day: bool,
    margin: MarginReader | None,
    config: OptionsConfig | None = None,
    ceilings: OptionsCeilings | None = None,
    mode_of: Callable[[Sleeve], Mode] = _modes,
) -> tuple[JsonObject, list[Alert]]:
    """One minute of the O3 builder: lapse what expired, then decide O3-B, then O3-A."""
    lapsed = await lapse_expired(session, user_id, now)
    reports: list[JsonObject] = []
    alerts: list[Alert] = []
    for sleeve in ORDER:
        result = await build_o3_plan(
            session,
            user_id,
            sleeve,
            now,
            trading_day=trading_day,
            margin=margin,
            config=config,
            ceilings=ceilings,
            mode_of=mode_of,
        )
        reports.append(result.report.as_dict())
        if result.alert is not None:
            alerts.append(result.alert)
    return {"at": now.isoformat(), "lapsed": list(lapsed), "sleeves": reports}, alerts


__all__ = [
    "ORDER",
    "BuildResult",
    "build_o3_plan",
    "plan_alert",
    "plan_detail",
    "plan_o3_gate_free",
    "plan_o3_minute",
]
