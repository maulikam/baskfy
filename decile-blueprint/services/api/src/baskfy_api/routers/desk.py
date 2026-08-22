"""``/desk/*`` — M26. The desk's read-only surfaces, on the merged backend.

    GET /desk/performance   what the portfolio is worth, and how that compares to the benchmark
    GET /desk/holdings      what is held right now, from the latest end-of-day snapshot
    GET /desk/tradebook     every trade the desk has taken, closed and open
    GET /desk/regime        the market stance the strategy is currently operating under
    GET /desk/reconcile     what the last plan intended, against what actually filled

Before this, the desk's own answers were visible only through a Jinja console behind an SSH
tunnel, in pages written for the person who built them. M22 put the *basket* on the web app;
this puts the record beside it, so one product has one face.

READ-ONLY, FOR THE SAME REASON M22 WAS
--------------------------------------
There is no ``POST``, ``PUT``, ``PATCH`` or ``DELETE`` in this file and there will not be.
``services/api/tests/test_desk_readonly.py`` asserts it over the source, the way
``test_baskets_readonly.py`` does for the basket surfaces, so the guarantee survives someone
adding a route in a hurry.

**Two of the desk's console pages are deliberately not ported in full, and the reason is not
tidiness.** The desk's ``/stops`` and ``/reconcile`` read *live broker state* — open orders,
resting triggers, holdings straight from Kite — and ``/stops`` carries an arming action that
creates and deletes triggers at the broker. Bringing either across whole would mean a
user-facing web surface holding a live broker session, which is a different regulated activity
and squarely inside the D3 question CLAUDE.md says nothing may be built against.

So what is served here is the part the *database* knows, which is most of the value and all of
the truth: the plan and its fills for reconciliation, and the position record for protection.
Whether a trigger is resting at the broker this minute is confirmed in the desk console, and
each page says so in as many words rather than implying a completeness it does not have.

WHERE THE DATA COMES FROM
-------------------------
The ``desk`` schema M19 cut over to. Nothing here recomputes a number the desk already
computed; if the desk's answer changes, so does this.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Annotated, Final

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import RowMapping, text

from baskfy_api.db import SessionDep
from baskfy_api.problems import Problem, ProblemType

router = APIRouter(prefix="/desk", tags=["desk"])

#: The schema M19's cutover put the desk's own records in.
DESK_SCHEMA: Final = "desk"

#: The benchmark the desk measures itself against (docs/01 §8).
BENCHMARK: Final = "NIFTY500MOMENTM50"

#: Tradebook page size. The desk has ~8,200 trades and no page needs them all at once.
DEFAULT_LIMIT: Final = 200
MAX_LIMIT: Final = 1000


# --- shapes ------------------------------------------------------------------


class NavPointOut(BaseModel):
    """One end-of-day mark."""

    date: str
    nav: float
    invested: float
    cash: float
    #: The desk's own index, rebased to 100 at inception.
    index_value: float | None = None
    #: The benchmark on the same date, rebased to 100 over the same span. None where the
    #: benchmark has no close for that date.
    benchmark_value: float | None = None


class PerformanceOut(BaseModel):
    as_of: str
    nav: float
    invested: float
    cash: float
    #: Percent, since the first snapshot. None when there is only one mark — a return needs two.
    return_pct: float | None = None
    benchmark_return_pct: float | None = None
    #: `return_pct - benchmark_return_pct`, when both exist.
    excess_pct: float | None = None
    series: list[NavPointOut]


class DeskHoldingOut(BaseModel):
    """Prefixed, because `baskfy_api.schemas` already has a `HoldingOut` and a `TradeOut`.

    Two models with one name do not collide in Python -- they are in different modules -- but the
    OpenAPI document is a flat namespace, so the generator module-qualifies one of them and the
    hand-written client that referenced the plain name stops compiling. That is exactly what
    `PlanOut` did at M19, and it broke on the TypeScript side rather than here.
    """

    symbol: str
    quantity: int
    average_price: float | None = None
    price: float | None = None
    value: float | None = None
    #: Unrealised, in rupees. None when either price is missing.
    unrealised: float | None = None
    unrealised_pct: float | None = None
    #: Shares pledged for collateral. They still belong to the position and still sell directly.
    pledged_qty: int = 0
    #: True for instruments the desk will not trade (SGBs, G-secs). They are shown because they
    #: are owned, and marked because the strategy does not manage them.
    excluded: bool = False


class HoldingsOut(BaseModel):
    as_of: str
    rows: list[DeskHoldingOut]
    total_value: float
    #: The part of `total_value` in instruments the desk will not trade.
    excluded_value: float


class DeskTradeOut(BaseModel):
    symbol: str
    quantity: int
    entry_date: str | None = None
    exit_date: str | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    pnl: float | None = None
    #: Percent on the entry value.
    pnl_pct: float | None = None
    costs: float | None = None
    exit_reason: str | None = None
    open: bool = False


class TradebookOut(BaseModel):
    rows: list[DeskTradeOut]
    total: int
    open_count: int
    closed_count: int
    #: Realised, over closed trades only, net of the costs the desk recorded.
    realised_pnl: float
    winners: int
    losers: int


class RegimeOut(BaseModel):
    evaluated_at: str
    signal_date: str | None = None
    #: The policy tier in force — R1 is fully risk-on, later tiers progressively less.
    tier: str
    previous_tier: str | None = None
    #: "full", "half", "none" — how large a new position may be opened.
    new_buys: str | None = None
    mode: str | None = None
    #: Percent of the universe above its 20-day average.
    breadth_pct: float | None = None
    actual_equity_pct: float | None = None
    target_equity_cap_pct: float | None = None
    #: Plain-English sentences the desk itself wrote when it evaluated.
    reasons: list[str]
    data_stale: bool = False
    manual_action_required: bool = False
    next_evaluation_date: str | None = None


class ReconcileRowOut(BaseModel):
    symbol: str
    side: str
    planned_qty: int
    filled_qty: int
    planned_ref_price: float | None = None
    avg_fill_price: float | None = None
    status: str | None = None
    #: filled - planned. Zero means the order did exactly what the plan said.
    shortfall: int = 0
    #: Rupee difference between what was planned and what was paid, where both are known.
    slippage: float | None = None


class ReconcileOut(BaseModel):
    plan_id: str
    created_at: str
    note: str | None = None
    rows: list[ReconcileRowOut]
    planned_count: int
    complete_count: int
    partial_count: int
    unfilled_count: int
    #: True when every planned order filled exactly. The one-line answer the page leads with.
    settled: bool


# --- routes ------------------------------------------------------------------


@router.get("/performance", response_model=PerformanceOut, summary="Portfolio value over time")
async def performance(session: SessionDep) -> PerformanceOut:
    rows = (
        (
            await session.execute(
                text(
                    f"select date, nav, invested, cash, index_value "
                    f'from "{DESK_SCHEMA}".snapshots order by date'
                )
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        raise Problem(
            ProblemType.NOT_FOUND,
            detail="the desk has recorded no end-of-day snapshots yet.",
        )

    marks = (
        (
            await session.execute(
                text(
                    f'select date, close from "{DESK_SCHEMA}".benchmark '
                    f"where index_name = :name order by date"
                ),
                {"name": BENCHMARK},
            )
        )
        .mappings()
        .all()
    )
    closes = {str(m["date"]): _f(m["close"]) for m in marks}

    # Rebased over the portfolio's own span, so the two lines start together. A benchmark
    # rebased over its whole history would be answering a different question.
    base_close = next(
        (closes[str(r["date"])] for r in rows if closes.get(str(r["date"])) not in (None, 0)),
        None,
    )

    series = [
        NavPointOut(
            date=str(row["date"]),
            nav=_num(row["nav"]),
            invested=_num(row["invested"]),
            cash=_num(row["cash"]),
            index_value=_f(row["index_value"]),
            benchmark_value=_rebase(closes.get(str(row["date"])), base_close),
        )
        for row in rows
    ]

    first, last = series[0], series[-1]
    return_pct = _change_pct(first.index_value, last.index_value)
    benchmark_return_pct = _change_pct(first.benchmark_value, last.benchmark_value)
    return PerformanceOut(
        as_of=last.date,
        nav=last.nav,
        invested=last.invested,
        cash=last.cash,
        return_pct=return_pct,
        benchmark_return_pct=benchmark_return_pct,
        excess_pct=(
            round(return_pct - benchmark_return_pct, 2)
            if return_pct is not None and benchmark_return_pct is not None
            else None
        ),
        series=series,
    )


@router.get("/holdings", response_model=HoldingsOut, summary="What is held right now")
async def holdings(session: SessionDep) -> HoldingsOut:
    row = (
        (
            await session.execute(
                text(
                    f'select date, holdings_json from "{DESK_SCHEMA}".snapshots '
                    f"order by date desc limit 1"
                )
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise Problem(
            ProblemType.NOT_FOUND,
            detail="the desk has recorded no end-of-day snapshots yet.",
        )

    payload = _as_object(row["holdings_json"])
    positions = payload.get("positions")
    rows = (
        [_holding(p) for p in positions if isinstance(p, dict)]
        if isinstance(positions, list)
        else []
    )
    rows.sort(key=lambda h: h.value or 0.0, reverse=True)
    return HoldingsOut(
        as_of=str(payload.get("as_of") or row["date"]),
        rows=rows,
        total_value=round(sum(h.value or 0.0 for h in rows), 2),
        excluded_value=round(sum(h.value or 0.0 for h in rows if h.excluded), 2),
    )


@router.get("/tradebook", response_model=TradebookOut, summary="Every trade taken")
async def tradebook(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> TradebookOut:
    totals = (
        (
            await session.execute(
                text(
                    f"select count(*) as total, "
                    f"count(*) filter (where exit_ts is null) as open_count, "
                    f"coalesce(sum(pnl) filter (where exit_ts is not null), 0) as realised, "
                    f"count(*) filter (where exit_ts is not null and pnl > 0) as winners, "
                    f"count(*) filter (where exit_ts is not null and pnl <= 0) as losers "
                    f'from "{DESK_SCHEMA}".trades'
                )
            )
        )
        .mappings()
        .one()
    )
    rows = (
        (
            await session.execute(
                text(
                    f"select symbol, entry_ts, exit_ts, qty, entry_price, exit_price, "
                    f"pnl, costs, exit_reason "
                    f'from "{DESK_SCHEMA}".trades '
                    f"order by coalesce(exit_ts, entry_ts) desc nulls last limit :limit"
                ),
                {"limit": limit},
            )
        )
        .mappings()
        .all()
    )
    total = int(totals["total"] or 0)
    open_count = int(totals["open_count"] or 0)
    return TradebookOut(
        rows=[_trade(r) for r in rows],
        total=total,
        open_count=open_count,
        closed_count=total - open_count,
        realised_pnl=round(_num(totals["realised"]), 2),
        winners=int(totals["winners"] or 0),
        losers=int(totals["losers"] or 0),
    )


@router.get("/regime", response_model=RegimeOut, summary="The market stance in force")
async def regime(session: SessionDep) -> RegimeOut:
    row = (
        (
            await session.execute(
                text(
                    f"select e.evaluation_id, e.created_at, e.signal_session_date, e.policy_tier, "
                    f"e.previous_policy_tier, e.new_buys, e.mode, e.breadth_pct, e.reasons_json, "
                    f"e.data_stale, e.manual_action_required, e.next_evaluation_date, "
                    f"x.actual_equity_pct, x.target_equity_cap_pct "
                    f'from "{DESK_SCHEMA}".regime_evaluations e '
                    f'left join "{DESK_SCHEMA}".regime_exposure x '
                    f"  on x.evaluation_id = e.evaluation_id "
                    f"order by e.id desc limit 1"
                )
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise Problem(
            ProblemType.NOT_FOUND, detail="the desk has recorded no regime evaluations yet."
        )
    return RegimeOut(
        evaluated_at=str(row["created_at"]),
        signal_date=_str_or_none(row["signal_session_date"]),
        tier=str(row["policy_tier"] or "unknown"),
        previous_tier=_str_or_none(row["previous_policy_tier"]),
        new_buys=_str_or_none(row["new_buys"]),
        mode=_str_or_none(row["mode"]),
        breadth_pct=_f(row["breadth_pct"]),
        actual_equity_pct=_f(row["actual_equity_pct"]),
        target_equity_cap_pct=_f(row["target_equity_cap_pct"]),
        reasons=_as_list(row["reasons_json"]),
        data_stale=bool(row["data_stale"]),
        manual_action_required=bool(row["manual_action_required"]),
        next_evaluation_date=_str_or_none(row["next_evaluation_date"]),
    )


@router.get("/reconcile", response_model=ReconcileOut, summary="Plan against fills")
async def reconcile(session: SessionDep) -> ReconcileOut:
    version = (
        (
            await session.execute(
                text(
                    f"select version_id, created_ts, note "
                    f'from "{DESK_SCHEMA}".rebalance_versions order by created_ts desc limit 1'
                )
            )
        )
        .mappings()
        .first()
    )
    if version is None:
        raise Problem(ProblemType.NOT_FOUND, detail="the desk has recorded no plans yet.")

    orders = (
        (
            await session.execute(
                text(
                    f"select symbol, side, planned_qty, planned_ref_price, filled_qty, "
                    f"avg_fill_price, status "
                    f'from "{DESK_SCHEMA}".rebalance_orders where version_id = :vid '
                    f"order by side desc, symbol"
                ),
                {"vid": version["version_id"]},
            )
        )
        .mappings()
        .all()
    )

    rows = [_reconcile_row(o) for o in orders]
    complete = sum(1 for r in rows if r.filled_qty >= r.planned_qty > 0)
    unfilled = sum(1 for r in rows if r.filled_qty == 0)
    return ReconcileOut(
        plan_id=str(version["version_id"]),
        created_at=dt.datetime.fromtimestamp(float(version["created_ts"]), tz=dt.UTC).isoformat(),
        note=_str_or_none(version["note"]),
        rows=rows,
        planned_count=len(rows),
        complete_count=complete,
        partial_count=len(rows) - complete - unfilled,
        unfilled_count=unfilled,
        settled=bool(rows) and complete == len(rows),
    )


# --- conversions -------------------------------------------------------------


def _holding(payload: Mapping[str, object]) -> DeskHoldingOut:
    quantity = int(_num(payload.get("quantity")))
    average = _f(payload.get("average_price"))
    price = _f(payload.get("price"))
    value = _f(payload.get("value"))
    unrealised = (
        round((price - average) * quantity, 2)
        if price is not None and average is not None
        else None
    )
    return DeskHoldingOut(
        symbol=str(payload.get("symbol") or ""),
        quantity=quantity,
        average_price=average,
        price=price,
        value=value,
        unrealised=unrealised,
        unrealised_pct=_change_pct(average, price),
        pledged_qty=int(_num(payload.get("pledged_qty"))),
        excluded=bool(payload.get("excluded")),
    )


def _trade(data: RowMapping) -> DeskTradeOut:
    quantity = int(_num(data.get("qty")))
    entry = _f(data.get("entry_price"))
    pnl = _f(data.get("pnl"))
    basis = (entry or 0.0) * quantity
    return DeskTradeOut(
        symbol=str(data.get("symbol") or ""),
        quantity=quantity,
        entry_date=_day(data.get("entry_ts")),
        exit_date=_day(data.get("exit_ts")),
        entry_price=entry,
        exit_price=_f(data.get("exit_price")),
        pnl=pnl,
        pnl_pct=round(pnl / basis * 100, 2) if pnl is not None and basis else None,
        costs=_f(data.get("costs")),
        exit_reason=_str_or_none(data.get("exit_reason")),
        open=data.get("exit_ts") is None,
    )


def _reconcile_row(data: RowMapping) -> ReconcileRowOut:
    planned = int(_num(data.get("planned_qty")))
    filled = int(_num(data.get("filled_qty")))
    ref = _f(data.get("planned_ref_price"))
    avg = _f(data.get("avg_fill_price"))
    return ReconcileRowOut(
        symbol=str(data.get("symbol") or ""),
        side=str(data.get("side") or ""),
        planned_qty=planned,
        filled_qty=filled,
        planned_ref_price=ref,
        avg_fill_price=avg,
        status=_str_or_none(data.get("status")),
        shortfall=filled - planned,
        slippage=round((avg - ref) * filled, 2) if ref is not None and avg is not None else None,
    )


def _day(value: object) -> str | None:
    """A desk timestamp — seconds since the epoch — as an ISO date."""
    seconds = _f(value)
    if seconds is None:
        return None
    return dt.datetime.fromtimestamp(seconds, tz=dt.UTC).date().isoformat()


def _rebase(close: float | None, base: float | None) -> float | None:
    if close is None or not base:
        return None
    return round(close / base * 100, 4)


def _change_pct(start: float | None, end: float | None) -> float | None:
    if start in (None, 0) or end is None or start is None:
        return None
    return round((end - start) / start * 100, 2)


def _str_or_none(value: object) -> str | None:
    return None if value in (None, "") else str(value)


def _num(value: object) -> float:
    return _f(value) or 0.0


def _f(value: object) -> float | None:
    """A driver's NUMERIC, or None. `object` rather than `Any`, per house rule 3."""
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal, str)):
        try:
            return float(value)
        except ValueError:
            return None
    raise TypeError(f"expected a number from the database, got {type(value).__name__}")


def _as_list(value: object) -> list[str]:
    if value in (None, ""):
        return []
    parsed = json.loads(value) if isinstance(value, str) else value
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _as_object(value: object) -> dict[str, object]:
    if value in (None, ""):
        return {}
    parsed = json.loads(value) if isinstance(value, str) else value
    return {str(k): v for k, v in parsed.items()} if isinstance(parsed, dict) else {}
