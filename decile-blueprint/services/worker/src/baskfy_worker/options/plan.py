"""The O1 plan builder on the database: ``op_session`` + ``op_plan`` + ``op_leg`` (``06`` OP6).

``baskfy_core.options.plan`` decides — role, gate, chain, structure, sizing, costs, margin
ceiling, ``plan_id``, expiry. This module reads its inputs (the same loaders the scan uses, so a
plan and the scan's candidate are built from the same rows — ``04`` §10), asks the broker's margin
**calculator** about the plan's two baskets, writes the result, and returns the
``OPTIONS_PLAN`` alert for the caller to send.

* **Idempotent per date** (house rule 7). A session that already left ``OBSERVING`` is returned as
  it stands — the same ``plan_id`` or the same skip — and nothing is recomputed, asked or sent
  again. The ``plan_id`` itself is deterministic (``plan.plan_id_for``); the session insert is
  ``ON CONFLICT DO NOTHING`` on ``(user_id, sleeve, trade_date)``, so two builders racing for one
  morning leave one row.
* **The margin is a ceiling, never a source** (Track C §10): lots come from the risk budget before
  the calculator is asked. The calculator is Kite's ``/margins/basket`` with
  ``consider_positions=False`` (Track C §8) — a calculation, not an order; this module imports no
  order path (law 2) and a test scans it.
* **Nothing here confirms or sends.** A plan is ``ISSUED`` and lapses at ``expires_at`` —
  :func:`lapse_expired` moves it and its session to ``LAPSED``. Confirming is the desk's
  ``POST /nifty-options/execute`` with ``confirm=true`` (OP10); there is no auto-execute for any
  options sleeve (PACK.3) and none is built here.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from baskfy_core.models import OpBookConfig, OpLeg, OpPlan, OpSession
from baskfy_core.models.base import JsonObject
from baskfy_core.options.config import Mode, OptionsCeilings, OptionsConfig, Sleeve
from baskfy_core.options.costs import ChargeBreakdown
from baskfy_core.options.plan import (
    O1_SLEEVES,
    MarginLeg,
    MarginQuote,
    O1Decision,
    O1Outcome,
    O1Plan,
    PlanLeg,
    PlanState,
    condor_config_for,
    decide_o1,
    finalize_o1,
)
from baskfy_core.options.session import Session as CoreSession
from baskfy_core.options.session import SessionState, transition
from baskfy_core.options.structures import to_json
from baskfy_providers.records import MarginLegRecord
from baskfy_worker.alerts import Alert, AlertName, Severity
from baskfy_worker.ops import RUNBOOKS
from baskfy_worker.options import options_gates
from baskfy_worker.options.index_bars import IST
from baskfy_worker.options.reads import GeneralReader
from baskfy_worker.options.scan import load_context, load_market, load_snapshot

log = logging.getLogger("baskfy_worker.options.plan")

#: The broker's margin calculator for one basket: ``final`` (hedged) total in ₹, or ``None``.
MarginReader = Callable[[Sequence[MarginLeg]], Decimal | None]

RUNBOOK: Final = RUNBOOKS[AlertName.OPTIONS_PLAN]
_MICRO: Final = Decimal("0.000001")
#: Sessions whose plan holds margin in the broker's eyes (``04`` §7.4, "margin in use").
_HOLDING_MARGIN: Final = (SessionState.CONFIRMED.value, SessionState.OPEN.value)


@dataclass(slots=True)
class PlanReport:
    """One build attempt for one sleeve; JSON-able for the task and the CLI."""

    sleeve: str
    trade_date: str
    state: str
    reasons: list[str] = field(default_factory=list)
    plan_id: str | None = None
    created: bool = False
    detail: JsonObject = field(default_factory=dict)

    def as_dict(self) -> JsonObject:
        return {
            "sleeve": self.sleeve,
            "trade_date": self.trade_date,
            "state": self.state,
            "reasons": list(self.reasons),
            "plan_id": self.plan_id,
            "created": self.created,
            "detail": dict(self.detail),
        }


def _modes(sleeve: Sleeve) -> Mode:
    return options_gates(sleeve).mode


def _greek(value: float | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(repr(value)).quantize(_MICRO, rounding=ROUND_HALF_UP)


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def costs_json(costs: ChargeBreakdown) -> JsonObject:
    """``04`` §6.1's components, each to the paisa, and their total."""
    return {
        "orders": costs.orders,
        "brokerage": str(costs.brokerage),
        "stt": str(costs.stt),
        "exchange_txn": str(costs.exchange_txn),
        "sebi": str(costs.sebi),
        "ipft": str(costs.ipft),
        "stamp": str(costs.stamp),
        "gst": str(costs.gst),
        "total": str(costs.total),
    }


def plan_detail(plan: O1Plan) -> JsonObject:
    """``op_plan.detail``: what the columns do not carry — the itemised costs, the margin
    figures and verdict, the gate's numbers, the warnings and the decision minute."""
    return {
        "costs": costs_json(plan.costs),
        "credit_inr": str(plan.credit_inr),
        "margin": {
            "hedged_inr": _money(plan.margin_required_inr),
            "transient_inr": _money(plan.margin_transient_inr),
            "code": plan.margin.code.value if plan.margin is not None else None,
            "message": plan.margin.message if plan.margin is not None else None,
        },
        "half_size": plan.half_size,
        "warnings": list(plan.warnings),
        "gate": dict(plan.numbers),
        "as_of_minute": plan.as_of_minute.isoformat() if plan.as_of_minute else None,
        "mode": plan.mode.value,
    }


def _leg_row(leg: PlanLeg, plan_pk: int, user_id: int, simulated: bool) -> dict[str, object]:
    return {
        "user_id": user_id,
        "plan_id": plan_pk,
        "seq": leg.seq,
        "role": leg.role.value,
        "tradingsymbol": leg.tradingsymbol,
        "instrument_token": leg.instrument_token,
        "strike": leg.strike,
        "option_type": leg.option_type.value,
        "side": leg.side.value,
        "quantity": leg.quantity,
        "limit_price": leg.limit_price,
        "iv_at_plan": _greek(leg.iv),
        "delta_at_plan": _greek(leg.delta),
        "bid": leg.bid,
        "ask": leg.ask,
        "depth_json": {
            "buy": [{"price": str(lv.price), "quantity": lv.quantity} for lv in leg.bids],
            "sell": [{"price": str(lv.price), "quantity": lv.quantity} for lv in leg.asks],
        },
        "status": "PENDING",
        "filled_qty": 0,
        "simulated": simulated,
    }


# --- the alert -----------------------------------------------------------------------------------


def _label(sleeve: Sleeve) -> str:
    return "O1-M" if sleeve is Sleeve.O1M else "O1-W"


def plan_alert(outcome: O1Outcome) -> Alert:
    """``OPTIONS_PLAN``: the verdict, or the plan in one paragraph a person can act on."""
    decision = outcome.decision
    day = decision.trade_date.isoformat()
    labels = {"trade_date": day, "sleeve": decision.sleeve.value, "state": outcome.state.value}
    plan = outcome.plan
    if plan is None:
        reasons = ", ".join(outcome.reasons) or "no reason recorded"
        return Alert(
            name=AlertName.OPTIONS_PLAN,
            severity=Severity.WARNING,
            summary=f"{_label(decision.sleeve)} {day}: no trade — {reasons}.",
            labels=labels,
            detail={
                "reasons": list(outcome.reasons),
                "gate": dict(decision.numbers),
                "candidate": to_json(decision.candidate) if decision.candidate else None,
            },
            runbook=RUNBOOK,
        )
    legs = "; ".join(
        f"{lg.seq}. {lg.side.value} {lg.quantity} {lg.tradingsymbol} @ {lg.limit_price}"
        for lg in plan.legs
    )
    margin = (
        f"margin ₹{plan.margin_required_inr}"
        if plan.margin_required_inr is not None
        else "margin not known"
    )
    warnings = f" Warnings: {', '.join(plan.warnings)}." if plan.warnings else ""
    summary = (
        f"{_label(plan.sleeve)} {day} {plan.mode.value} plan {plan.plan_id}: iron condor on the "
        f"{plan.expiry.isoformat()} expiry, credit {plan.credit_points} pts (₹{plan.credit_inr}) "
        f"for {plan.lots} lot(s) of {plan.lot_size} ({plan.sizing_mode.value}); max loss "
        f"₹{plan.max_loss_inr}, round-trip costs ₹{plan.expected_cost_inr} "
        f"(share {plan.cost_share}), {margin}. Legs in send order: {legs}. Expires "
        f"{plan.expires_at.astimezone(IST):%H:%M}; nothing is sent without a confirm on the "
        f"desk.{warnings}"
    )
    return Alert(
        name=AlertName.OPTIONS_PLAN,
        severity=Severity.WARNING,
        summary=summary,
        labels=labels | {"plan_id": plan.plan_id},
        detail=plan_detail(plan) | {"legs": [_leg_json(lg) for lg in plan.legs]},
        runbook=RUNBOOK,
    )


def _leg_json(leg: PlanLeg) -> JsonObject:
    return {
        "seq": leg.seq,
        "role": leg.role.value,
        "tradingsymbol": leg.tradingsymbol,
        "side": leg.side.value,
        "quantity": leg.quantity,
        "limit_price": str(leg.limit_price),
        "bid": _money(leg.bid),
        "ask": _money(leg.ask),
    }


# --- reads ---------------------------------------------------------------------------------------


async def existing_session(
    session: AsyncSession, user_id: int, sleeve: Sleeve, day: dt.date
) -> OpSession | None:
    return (
        await session.execute(
            select(OpSession).where(
                OpSession.user_id == user_id,
                OpSession.sleeve == sleeve.value,
                OpSession.trade_date == day,
            )
        )
    ).scalar_one_or_none()


async def margin_in_use(session: AsyncSession, user_id: int, day: dt.date) -> Decimal:
    """``04`` §7.4's "margin in use by open op_ positions": the hedged figure of every plan
    today whose session is confirmed or open."""
    total = (
        await session.execute(
            select(func.coalesce(func.sum(OpPlan.margin_required_inr), 0))
            .join(OpSession, OpSession.id == OpPlan.session_id)
            .where(
                OpPlan.user_id == user_id,
                OpSession.trade_date == day,
                OpSession.state.in_(_HOLDING_MARGIN),
                OpPlan.kind == "ENTRY",
            )
        )
    ).scalar_one()
    return Decimal(total) if total is not None else Decimal(0)


async def margin_pool(session: AsyncSession, user_id: int) -> Decimal:
    pool = (
        await session.execute(
            select(OpBookConfig.margin_pool_inr).where(OpBookConfig.user_id == user_id)
        )
    ).scalar_one_or_none()
    return Decimal(pool) if pool is not None else Decimal(0)


def ask_margin(reader: MarginReader | None, decision: O1Decision) -> tuple[MarginQuote | None, str]:
    """The calculator's two answers, or ``None`` with the reason it did not answer (recorded on
    the plan — never swallowed)."""
    if reader is None:
        return None, "no Kite session: the margin calculator was not asked"
    hedged_basket, transient_basket = decision.margin_baskets()
    try:
        hedged = reader(hedged_basket)
        transient = reader(transient_basket) if transient_basket else None
    except Exception as exc:  # a broker error is a missing figure, never a crash of the plan
        log.warning("options margin calculator failed", extra={"error": str(exc)})
        return None, f"margin calculator failed: {type(exc).__name__}: {exc}"
    if hedged is None:
        return None, "the margin calculator returned no total"
    return MarginQuote(hedged_inr=hedged, transient_inr=transient), "asked"


# --- writes --------------------------------------------------------------------------------------


def _session_values(outcome: O1Outcome, mode: Mode) -> dict[str, object]:
    decision = outcome.decision
    plan = outcome.plan
    numbers: JsonObject = dict(decision.numbers)
    if decision.candidate is not None:
        numbers["candidate"] = to_json(decision.candidate)
    if decision.as_of_minute is not None:
        numbers["as_of_minute"] = decision.as_of_minute.isoformat()
    state = SessionState.PLANNED if plan is not None else SessionState.SKIPPED
    # The core machine says whether OBSERVING may go there; it raises otherwise.
    transition(
        CoreSession(decision.sleeve, decision.trade_date),
        state,
        None if plan is not None else ",".join(outcome.reasons),
    )
    return {
        "expiry_used": decision.expiry,
        "mode": mode.value,
        "state": state.value,
        "numbers": numbers,
        "verdict": decision.verdict.value if decision.verdict is not None else None,
        "skip_reasons": [] if plan is not None else list(outcome.reasons),
        "plan_id": plan.plan_id if plan is not None else None,
    }


async def _write_session(
    session: AsyncSession, user_id: int, outcome: O1Outcome, mode: Mode, current: OpSession | None
) -> int | None:
    """Insert today's session (or move the ``OBSERVING`` row on); ``None`` if another builder
    wrote it first."""
    values = _session_values(outcome, mode)
    decision = outcome.decision
    if current is not None:
        await session.execute(update(OpSession).where(OpSession.id == current.id).values(**values))
        return int(current.id)
    stmt = (
        insert(OpSession)
        .values(
            user_id=user_id,
            sleeve=decision.sleeve.value,
            trade_date=decision.trade_date,
            **values,
        )
        .on_conflict_do_nothing(constraint="uq_op_session_user_sleeve_date")
        .returning(OpSession.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _write_plan(session: AsyncSession, user_id: int, session_pk: int, plan: O1Plan) -> None:
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
        credit_points=plan.credit_points,
        debit_points=None,
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
        insert(OpLeg), [_leg_row(lg, int(row.id), user_id, simulated) for lg in plan.legs]
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


# --- one sleeve, one morning ---------------------------------------------------------------------


@dataclass(slots=True)
class BuildResult:
    report: PlanReport
    alert: Alert | None = None


async def build_o1_plan(  # noqa: PLR0913 - the session, the tenant, the sleeve, the clock and the rules
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
    """``build_plan(O1M | O1W, date)``: return today's decided session as it stands, or decide
    now and write it. The alert is returned only when this call wrote the session."""
    if sleeve not in O1_SLEEVES:
        raise ValueError(f"{sleeve.value} is not an O1 sleeve")
    cfg = config or OptionsConfig()
    ceil = ceilings or OptionsCeilings()
    day = now.astimezone(IST).date()
    current = await existing_session(session, user_id, sleeve, day)
    if current is not None and current.state != SessionState.OBSERVING.value:
        return BuildResult(_report_of(current))
    market = await load_market(session, day, trading_day, cfg)
    mode = mode_of(sleeve)
    context = await load_context(session, user_id, day, mode_of)
    decision_minute = dt.datetime.combine(day, condor_config_for(sleeve, cfg).plan_time, tzinfo=IST)
    snapshot = await load_snapshot(session, decision_minute, now, day)
    decision = decide_o1(sleeve, market, context, snapshot, now, options=cfg, ceilings=ceil)
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
    outcome = finalize_o1(
        decision,
        quote,
        user_id=user_id,
        mode=mode,
        margin_pool_inr=await margin_pool(session, user_id),
        margin_in_use_inr=await margin_in_use(session, user_id, day),
        options=cfg,
    )
    session_pk = await _write_session(session, user_id, outcome, mode, current)
    if session_pk is None:  # another builder decided this morning first
        raced = await existing_session(session, user_id, sleeve, day)
        return BuildResult(_report_of(raced) if raced is not None else report)
    if outcome.plan is not None:
        await _write_plan(session, user_id, session_pk, outcome.plan)
    report.state = outcome.state.value
    report.reasons = list(outcome.reasons)
    report.plan_id = outcome.plan.plan_id if outcome.plan is not None else None
    report.created = True
    report.detail = {"margin": asked}
    return BuildResult(report, plan_alert(outcome))


async def lapse_expired(session: AsyncSession, user_id: int, now: dt.datetime) -> list[str]:
    """``PLANNED → LAPSED`` for every issued entry plan whose ``expires_at`` has passed
    (``04`` §11): the plan's status and its session's state, together. Returns the plan ids."""
    rows = (
        await session.execute(
            select(OpPlan.id, OpPlan.plan_id, OpPlan.session_id).where(
                OpPlan.user_id == user_id,
                OpPlan.status == "ISSUED",
                OpPlan.kind == "ENTRY",
                OpPlan.expires_at <= now,
            )
        )
    ).all()
    lapsed: list[str] = []
    for pk, plan_id, session_pk in rows:
        await session.execute(update(OpPlan).where(OpPlan.id == pk).values(status="LAPSED"))
        await session.execute(
            update(OpSession)
            .where(OpSession.id == session_pk, OpSession.state == SessionState.PLANNED.value)
            .values(state=SessionState.LAPSED.value)
        )
        lapsed.append(str(plan_id))
    return lapsed


# --- the minute the task runs --------------------------------------------------------------------


def plan_gate_free(
    now: dt.datetime,
    *,
    monitor_enabled: bool,
    collect_enabled: bool,
    user_id: int | None,
    config: OptionsConfig | None = None,
) -> str | None:
    """The refusals that cost nothing, checked before a database session (OP6.6).

    Raising plans is the options monitor's job (``02``: ``BASKFY_OPTIONS_MONITOR_ENABLED`` —
    "raises plans"; operational, moves no money), so the builder is dark until that flag is on;
    with no collector there is no chain to price a plan from.
    """
    if not monitor_enabled:
        return "BASKFY_OPTIONS_MONITOR_ENABLED is false"
    if not collect_enabled:
        return "BASKFY_OPTIONS_COLLECT_ENABLED is false (no chain to plan from)"
    cfg = config or OptionsConfig()
    clock = now.astimezone(IST).time()
    first = min(cfg.condor_monthly.plan_time, cfg.condor_weekly.plan_time)
    last = max(cfg.condor_monthly.entry_window_end, cfg.condor_weekly.entry_window_end)
    if (
        not first
        <= clock
        <= (dt.datetime.combine(dt.date.min, last) + dt.timedelta(minutes=1)).time()
    ):
        return f"outside {first:%H:%M}-{last:%H:%M} IST (+1 minute to lapse)"
    if user_id is None:
        return "no BASKFY_SOLE_USER_ID configured"
    return None


def kite_margin_reader(general: GeneralReader) -> MarginReader:
    """The broker's ``/margins/basket`` as a :data:`MarginReader` — a calculation, asked with
    ``consider_positions=False`` so the answer is this basket's alone (Track C §8)."""

    def read(legs: Sequence[MarginLeg]) -> Decimal | None:
        record = general.basket_order_margins(
            [
                MarginLegRecord(
                    exchange="NFO",
                    tradingsymbol=lg.tradingsymbol,
                    transaction_type="BUY" if lg.side.value == "BUY" else "SELL",
                    quantity=lg.quantity,
                )
                for lg in legs
            ],
            consider_positions=False,
        )
        return record.final_total

    return read


async def plan_o1_minute(  # noqa: PLR0913 - the session, the tenant, the clock and the rules
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
    """One minute of the O1 builder: lapse what expired, then decide each O1 sleeve."""
    lapsed = await lapse_expired(session, user_id, now)
    reports: list[JsonObject] = []
    alerts: list[Alert] = []
    for sleeve in (Sleeve.O1M, Sleeve.O1W):
        result = await build_o1_plan(
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
