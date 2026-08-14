"""Regime planning + execution orchestration (checkpoint 3).

THREE INDEPENDENT GATES, IN THIS ORDER
1. REGIME_ENABLED — the feature switch. Off means the overlay produces nothing at all.
2. REGIME_MODE    — observe (no executable plan) / propose (plan, needs approval) /
                    enforce (plan may be executed).
3. DRY_RUN        — the global no-order guarantee. It applies in EVERY mode and is
                    checked again inside core/gateway.py, so there is no path where an
                    order escapes because one layer was misconfigured.

DRY_RUN permits: downloading and caching market data, computing candidate/policy tiers,
generating proposed sales, displaying tax flags. It forbids: submitting any order,
committing a policy-tier transition, marking a rebalance executed.

ORDER PATH
Execution routes through core/gateway.py — guards -> risk -> rate limits -> journal, in
that order, unmodified. This module never calls kc.place_order.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from .. import config as C
from ..core.gateway import OrderGateway
from ..core.regime import (AllocationError, ExecutionStatus, ForcedAction, NewBuyMode,
                           RegimeMode, RegimeState, RegimeTier)
from ..core.regime_alloc import (Allocation, Candidate, OrderLine, Position, RegimePlan,
                                 build_orders, order_sales, projected_equity_pct,
                                 select_survivors, solve_allocation)
from . import regime_store as RS
from . import tax_lots

log = logging.getLogger("regime_run")

# stable reason codes for the planning layer
PLAN_INFEASIBLE = "PLAN_INFEASIBLE"
PLAN_CAP_UNREACHABLE = "PLAN_CAP_UNREACHABLE"
PLAN_TAX_DEFERRED = "PLAN_TAX_DEFERRED"
PLAN_TAX_SOLD_ANYWAY = "PLAN_TAX_SOLD_ANYWAY"
PLAN_NOT_EXECUTABLE_OBSERVE = "PLAN_NOT_EXECUTABLE_OBSERVE"
PLAN_NEEDS_APPROVAL = "PLAN_NEEDS_APPROVAL"
PLAN_DRY_RUN = "PLAN_DRY_RUN"
PLAN_GUARD_EXITS_PRESERVED = "PLAN_GUARD_EXITS_PRESERVED"


class RegimeDisabledError(RuntimeError):
    pass


class PlanNotExecutableError(RuntimeError):
    pass


# =====================================================================================
# planning
# =====================================================================================
def plan_regime_rebalance(state: RegimeState, candidates: Sequence[Candidate],
                          positions: Sequence[Position], *, capital: float,
                          tax_reviews: Mapping[str, tax_lots.TaxReview] | None = None,
                          mode: RegimeMode = RegimeMode.OBSERVE,
                          dry_run: bool = True,
                          min_sleeve_w: float | None = None,
                          max_sleeve_w: float | None = None,
                          cluster_cap: float | None = None,
                          target_positions: tuple[int, int] | None = None,
                          residual_max_names: int | None = None,
                          residual_ordering: Sequence[str] | None = None,
                          nearby_band: int | None = None,
                          half_factor: float | None = None) -> RegimePlan:
    """Build ONE final target book for the committed policy tier, then net the orders."""
    min_w = C.MIN_POSITION_WEIGHT if min_sleeve_w is None else min_sleeve_w
    max_w = C.MAX_SINGLE_WEIGHT if max_sleeve_w is None else max_sleeve_w
    ccap = C.CLUSTER_CAP if cluster_cap is None else cluster_cap
    lo, hi = target_positions or C.TARGET_POSITIONS
    band = C.REGIME_NEARBY_RANK_BAND if nearby_band is None else nearby_band
    hf = C.REGIME_HALF_SIZE_FACTOR if half_factor is None else half_factor
    reviews = dict(tax_reviews or {})
    codes: list[str] = []

    guard_exits = {p.symbol for p in positions if p.guard_exit}
    if guard_exits:
        codes.append(PLAN_GUARD_EXITS_PRESERVED)

    flagged = set(tax_lots.flagged_symbols(reviews))
    sleeve = state.target_equity_cap_pct

    # --- residual policy for R4 --------------------------------------------------------
    residual = None
    ordering = tuple(residual_ordering or C.REGIME_R4_RESIDUAL_ORDERING)
    if state.policy_tier is RegimeTier.R4:
        residual = (C.REGIME_R4_MAX_RESIDUAL_NAMES if residual_max_names is None
                    else residual_max_names)
        if residual <= 0:
            # No residual policy means full cash — never invent names to retain.
            sleeve = 0.0

    # --- constituent selection ---------------------------------------------------------
    survivors, _dropped = select_survivors(
        candidates, positions, max_positions=hi, new_buys=state.new_buys,
        residual_max_names=residual, residual_ordering=ordering,
        nearby_band=band, tax_flagged=flagged)
    survivors = [c for c in survivors if c.symbol not in guard_exits]

    if sleeve <= 0 or not survivors:
        alloc = Allocation({}, {}, frozenset(), sleeve)
        orders = build_orders(alloc, positions, candidates, capital=capital,
                              tax_flagged=flagged, guard_exits=guard_exits)
        proj = projected_equity_pct(orders, capital)
        return _finalise(state, alloc, orders, proj, mode, dry_run, codes, reviews,
                         infeasible=None)

    # A residual policy deliberately holds very few names, so the normal sleeve floor and
    # cap cannot apply: 2 names must take ~50% of the sleeve each. In NAV terms that is
    # still tiny (50% of a 10% sleeve = 5% of NAV), so the concentration limit is relaxed
    # only for the residual case and only within the shrunken sleeve.
    if residual is not None and survivors:
        share = 100.0 / len(survivors)
        min_w = min(min_w, share)
        max_w = max(max_w, share)

    # --- half-size provisional entries --------------------------------------------------
    held = {p.symbol for p in positions}
    half: set[str] = set()
    if state.new_buys is NewBuyMode.HALF:
        half = {c.symbol for c in survivors if c.symbol not in held}

    # --- allocate on the sleeve ----------------------------------------------------------
    try:
        alloc = solve_allocation(survivors, sleeve_pct=sleeve, min_sleeve_w=min_w,
                                 max_sleeve_w=max_w, cluster_cap=ccap,
                                 half_sized=half, half_factor=hf)
    except AllocationError as exc:
        # Retry once at the low end of the target range before giving up — a solvable
        # book with fewer names is better than a manual-review dead end.
        try:
            trimmed = survivors[:lo]
            alloc = solve_allocation(trimmed, sleeve_pct=sleeve, min_sleeve_w=min_w,
                                     max_sleeve_w=max_w, cluster_cap=ccap,
                                     half_sized=half & {c.symbol for c in trimmed},
                                     half_factor=hf)
            survivors = trimmed
        except AllocationError:
            codes.append(PLAN_INFEASIBLE)
            empty = Allocation({}, {}, frozenset(), sleeve)
            return _finalise(state, empty, [], state.actual_equity_pct, mode, dry_run,
                             codes, reviews, infeasible=str(exc))

    orders = build_orders(alloc, positions, candidates, capital=capital,
                          tax_flagged=flagged, guard_exits=guard_exits)
    proj = projected_equity_pct(orders, capital)
    if flagged and any(o.tax_review for o in orders):
        # Tax is advisory: the sale still happens, but it is surfaced for review rather
        # than leaving the exposure cap silently breached.
        codes.append(PLAN_TAX_SOLD_ANYWAY if proj <= sleeve + 1e-6 else PLAN_TAX_DEFERRED)
    return _finalise(state, alloc, orders, proj, mode, dry_run, codes, reviews,
                     infeasible=None)


def _finalise(state: RegimeState, alloc: Allocation, orders: Sequence[OrderLine],
              projected: float, mode: RegimeMode, dry_run: bool, codes: list[str],
              reviews: Mapping[str, tax_lots.TaxReview],
              infeasible: str | None) -> RegimePlan:
    gap = round(projected - state.target_equity_cap_pct, 6)
    manual = bool(infeasible) or any(r.manual_review for r in reviews.values())
    if gap > 1e-6:
        codes.append(PLAN_CAP_UNREACHABLE)
        manual = True

    executable = mode is RegimeMode.ENFORCE and not dry_run and not infeasible
    if mode is RegimeMode.OBSERVE:
        codes.append(PLAN_NOT_EXECUTABLE_OBSERVE)
    elif mode is RegimeMode.PROPOSE:
        codes.append(PLAN_NEEDS_APPROVAL)
    if dry_run:
        codes.append(PLAN_DRY_RUN)

    return RegimePlan(
        tier=state.policy_tier, sleeve_pct=alloc.sleeve_pct, allocation=alloc,
        orders=tuple(orders), projected_equity_pct=projected,
        exposure_gap_after_plan_pct=gap, manual_action_required=manual,
        executable=executable, reason_codes=tuple(dict.fromkeys(codes)),
        tax_flagged=tuple(sorted(s for s, r in reviews.items() if r.flagged)),
        infeasible_reason=infeasible)


# =====================================================================================
# execution — the ONLY path is core/gateway.py
# =====================================================================================
@dataclass
class ExecutionReport:
    submitted: list[dict]
    skipped: list[dict]
    status: ExecutionStatus
    dry_run: bool
    mode: str

    @property
    def filled_pct(self) -> float:
        if not self.submitted:
            return 0.0
        ok = sum(1 for r in self.submitted if r.get("status") in ("PLACED", "DRY_RUN"))
        return ok / len(self.submitted) * 100.0


async def execute_regime_plan(gateway: OrderGateway, plan: RegimePlan, *,
                              mode: RegimeMode, dry_run: bool,
                              approved: bool = False) -> ExecutionReport:
    """Submit a plan through the existing gateway. Sells first, then buys.

    Refuses unless enforce mode AND an explicit approval AND not DRY_RUN. The gateway
    performs its own DRY_RUN check afterwards, so this is belt and braces, not the only
    protection.
    """
    if not C.REGIME_ENABLED:
        raise RegimeDisabledError("REGIME_ENABLED is false — no regime orders are placed")
    if mode is not RegimeMode.ENFORCE:
        raise PlanNotExecutableError(
            f"mode={mode.value}: only enforce mode may execute a regime plan")
    if not approved:
        raise PlanNotExecutableError("regime plan requires explicit approval")
    if dry_run:
        raise PlanNotExecutableError(
            "DRY_RUN is active — refusing to submit regime orders")
    if plan.infeasible_reason:
        raise PlanNotExecutableError(f"plan is infeasible: {plan.infeasible_reason}")

    submitted, skipped = [], []
    # Sells first (frees cash and reduces exposure), then buys. This mirrors the existing
    # /execute ordering; the gateway's internal layer sequence is untouched.
    ordered = sorted((o for o in plan.orders if o.delta != 0),
                     key=lambda o: (o.delta > 0, -abs(o.delta * o.ref_price)))
    for o in ordered:
        side = "SELL" if o.delta < 0 else "BUY"
        res = await gateway.place(symbol=o.symbol, qty=abs(o.delta), side=side,
                                  product="CNC", order_type="LIMIT", price=o.ref_price,
                                  exchange="NSE")
        res["regime_action"] = o.action
        (submitted if res.get("status") in ("PLACED", "DRY_RUN") else skipped).append(res)

    if not submitted and not skipped:
        status = ExecutionStatus.NOT_PLANNED
    elif skipped and submitted:
        status = ExecutionStatus.PARTIAL
    elif skipped:
        status = ExecutionStatus.FAILED
    else:
        status = ExecutionStatus.COMPLETED
    return ExecutionReport(submitted=submitted, skipped=skipped, status=status,
                           dry_run=dry_run, mode=mode.value)


# =====================================================================================
# reconciliation
# =====================================================================================
def reconcile(conn, state: RegimeState, plan: RegimePlan, *,
              actual_equity_pct: float, capital: float | None = None,
              execution_status: ExecutionStatus = ExecutionStatus.NOT_PLANNED,
              observed_at: str | None = None) -> dict:
    """Record where exposure actually stands. Safe and expected on EVERY invocation.

    Deliberately separate from the weekly decision: a failed or partial plan leaves a gap
    that is re-derived here next time, so it can be retried without minting a new tier
    transition.
    """
    # Pending legs are RUPEE values; they must be divided by rupee capital, not by the
    # exposure PERCENTAGE. Dividing by 85 instead of 12,00,000 reported 4235% pending.
    pending_sell = sum(abs(o.delta) * o.ref_price for o in plan.sells)
    pending_buy = sum(o.delta * o.ref_price for o in plan.buys)
    base = capital if capital and capital > 0 else None
    return RS.record_exposure(
        conn, evaluation_id=state.evaluation_id, actual_equity_pct=actual_equity_pct,
        target_equity_cap_pct=state.target_equity_cap_pct,
        pending_buy_pct=round(pending_buy / base * 100.0, 6) if base else 0.0,
        pending_sell_pct=round(pending_sell / base * 100.0, 6) if base else 0.0,
        execution_status=execution_status, observed_at=observed_at,
        note=f"tier={state.policy_tier.value} projected={plan.projected_equity_pct:.2f}%")


def evaluation_snapshot(plan: RegimePlan | None = None,
                        tax_reviews: Mapping[str, tax_lots.TaxReview] | None = None
                        ) -> dict:
    """Build the input_snapshot payload the /regime page renders.

    Without this the page can show the forced-action ENUM but not the sales it implies,
    and the manual-review counter is permanently zero — labels with no data behind them.
    Pass the result to regime_store.save_preview()/commit_evaluation().
    """
    snap: dict = {}
    if plan is not None:
        snap["plan"] = summarise(plan)
        snap["proposed_orders"] = [
            {"symbol": o.symbol, "action": o.action, "qty_now": o.qty_now,
             "delta": o.delta, "qty_final": o.qty_final, "ref_price": o.ref_price,
             "weight_nav": o.weight_nav, "weight_sleeve": o.weight_sleeve,
             "value": o.value, "rank": o.rank, "half_sized": o.half_sized,
             "tax_review": o.tax_review, "note": o.note}
            for o in plan.orders if o.delta != 0]
    if tax_reviews:
        snap["manual_review_items"] = tax_lots.manual_review_items(tax_reviews)
    return snap


def summarise(plan: RegimePlan) -> dict:
    weights = dict(plan.allocation.weights_sleeve)
    largest = max(weights.items(), key=lambda kv: kv[1], default=(None, 0.0))
    return {
        "tier": plan.tier.value,
        "sleeve_pct": plan.sleeve_pct,
        "positions": plan.allocation.n_positions,
        "half_sized": sorted(plan.allocation.half_sized),
        "cluster_weights": dict(plan.allocation.cluster_weights),
        "largest_position": {"symbol": largest[0], "weight_sleeve": round(largest[1], 2)},
        "allocation_notes": list(plan.allocation.notes),
        "projected_equity_pct": plan.projected_equity_pct,
        "exposure_gap_after_plan_pct": plan.exposure_gap_after_plan_pct,
        "buys": len(plan.buys), "sells": len(plan.sells),
        "round_trips": list(plan.round_trips),
        "executable": plan.executable,
        "manual_action_required": plan.manual_action_required,
        "tax_flagged": list(plan.tax_flagged),
        "reason_codes": list(plan.reason_codes),
        "infeasible_reason": plan.infeasible_reason,
    }
