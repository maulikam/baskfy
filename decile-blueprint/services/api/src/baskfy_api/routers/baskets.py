"""``/baskets`` and ``/baskets/plan`` — M22, the merged face.

    GET /baskets                 the basket the strategy wants today
    GET /baskets/plan            the desk's most recent rebalance plan, in full

The screener's web app has never shown a basket; the desk's Jinja pages have never been seen
outside an SSH tunnel. These two endpoints are the seam between them.

READ-ONLY, AND NOT AS A MATTER OF CONVENTION
----------------------------------------------
There is **no** ``POST``, ``PUT``, ``PATCH`` or ``DELETE`` in this file, and there will not be.
Execution stays in the desk console. That is not tidiness — it is the SEBI gate: the desk places
orders for one account, its owner's, and a web surface that could place an order for a logged-in
user is a different regulated activity entirely.

It is also the desk's non-negotiable #1: ``packages/execution`` is the *only* path to an order.
This module does not import it, cannot reach it, and
``services/api/tests/test_baskets_readonly.py`` asserts both — structurally, over the source, so
the guarantee survives someone adding a route in a hurry.

WHERE THE DATA COMES FROM
--------------------------
The basket is computed live: bars → ``compute_factors`` → ``baskfy_core.score`` → the basket
engine, exactly the chain the desk runs. The plan is read from the ``desk`` schema M19 cut over
to. Nothing is duplicated or re-implemented; if the desk's answer changes, so does this.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from decimal import Decimal
from typing import Annotated, Final

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import text

from baskfy_api.db import SessionDep
from baskfy_api.problems import Problem, ProblemType

router = APIRouter(tags=["baskets"])

#: The schema M19's cutover put the desk's own records in.
DESK_SCHEMA: Final = "desk"


class BasketRowOut(BaseModel):
    """One name in the basket, with the score components that put it there."""

    rank: int
    symbol: str
    score: float
    weight: float
    ref_price: float
    stop: float
    value: int
    a_trend: float | None = None
    b_momentum: float | None = None
    c_sharpe: float | None = None
    d_consistency: float | None = None
    e_liquidity: float | None = None
    f_penalty: float | None = None


class BasketOut(BaseModel):
    as_of: str
    screen_run_id: str
    data_version: int
    capital: int
    cash_target_pct: float
    breadth_above_20dma: float
    #: Symbols whose bar history carries an unadjusted corporate action. Surfaced rather than
    #: hidden: their returns and highs are wrong, and a basket page that showed a clean list
    #: would be asserting a cleanliness nobody has established. NEEDS-MAULIK item 4.
    suspect_symbols: list[str]
    rows: list[BasketRowOut]


class RebalanceOrderOut(BaseModel):
    """Every column the desk's Jinja plan table shows, including the pledged flag."""

    symbol: str
    side: str
    planned_qty: int
    planned_ref_price: float | None
    filled_qty: int | None
    avg_fill_price: float | None
    status: str | None
    order_id: str | None
    reconciled_at: str | None


# Named `Rebalance...` rather than `Plan...` deliberately: `PlanOut` already exists in
# `baskfy_api.schemas` and means a *billing* plan. Two models sharing one name made the generated
# TypeScript rename the older one, which broke `packages/api-client/src/client.ts` — a compile
# error in the web app caused by a name chosen carelessly in a Python file. Kept as a comment
# rather than a docstring because a model docstring becomes the public OpenAPI description, and an
# internal naming decision is not something an API consumer needs to read.
class RebalancePlanOut(BaseModel):
    """One rebalance plan: what the desk decided to trade, and what happened to each order."""

    plan_id: str
    created_at: str
    note: str | None
    evaluation_id: str | None
    constituents: list[str]
    weights: dict[str, float]
    orders: list[RebalanceOrderOut]


@router.get("/baskets", response_model=BasketOut)
async def current_basket(
    session: SessionDep,
    date: Annotated[dt.date | None, Query(description="as-of; defaults to the latest bars")] = None,
) -> BasketOut:
    """The basket the strategy wants today.

    Built against an all-cash book on purpose. A basket page answers *"what does the strategy
    want?"*, which is a property of the market; a plan answers *"what would we have to trade?"*,
    which is a property of the book. Those are different questions and `/baskets/plan` is the
    other one.

    **Served from the nightly snapshot when there is one** (M30). Building it live means loading
    every bar the scanned symbols have ever had into Polars, which took about a second at two
    years of history and **67 seconds** at nine. The nightly chain computes it once, after
    publish, into `basket_snapshot`.

    The live path is kept as the fallback, and that is not belt-and-braces: on a fresh database,
    before the first nightly run, or on any night the step was skipped for want of an uploaded
    scan, there is no snapshot. A page that 404s because a cache is cold would be worse than a
    slow page.
    """
    from baskfy_api.baskets import build_current_basket  # noqa: PLC0415
    from baskfy_worker.tasks.basket import latest_snapshot  # noqa: PLC0415

    stored = await latest_snapshot(session, date)
    if stored is not None:
        return BasketOut.model_validate(stored)

    # Live build can take tens of seconds on deep history (M30: ~67s at nine years). Cap it so
    # the web RSC navigation never hangs; callers should rely on the nightly snapshot.
    try:
        resolved = await asyncio.wait_for(build_current_basket(session, date), timeout=3.0)
    except TimeoutError as exc:
        raise Problem(
            ProblemType.NOT_FOUND,
            detail=(
                "no basket snapshot is ready yet, and the live rebuild exceeded the 3s page "
                "budget. Wait for the nightly basket_snapshot job, or retry later."
            ),
        ) from exc
    if resolved is None:
        raise Problem(
            ProblemType.NOT_FOUND,
            detail="no bars are available to build a basket from.",
        )
    return resolved


@router.get("/baskets/plan", response_model=RebalancePlanOut)
async def latest_plan(session: SessionDep) -> RebalancePlanOut:
    """The desk's most recent rebalance plan, read from the `desk` schema."""
    version = (
        (
            await session.execute(
                text(
                    f"select version_id, created_ts, note, evaluation_id, "
                    f"constituents_json, weights_json "
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
                    f"avg_fill_price, status, order_id, reconciled_at "
                    f'from "{DESK_SCHEMA}".rebalance_orders where version_id = :vid '
                    f"order by side desc, symbol"
                ),
                {"vid": version["version_id"]},
            )
        )
        .mappings()
        .all()
    )

    return RebalancePlanOut(
        plan_id=str(version["version_id"]),
        created_at=dt.datetime.fromtimestamp(float(version["created_ts"]), tz=dt.UTC).isoformat(),
        note=version["note"],
        evaluation_id=version["evaluation_id"],
        constituents=_as_list(version["constituents_json"]),
        weights=_as_weights(version["weights_json"]),
        orders=[
            RebalanceOrderOut(
                symbol=o["symbol"],
                side=o["side"],
                planned_qty=int(o["planned_qty"] or 0),
                planned_ref_price=_f(o["planned_ref_price"]),
                filled_qty=int(o["filled_qty"]) if o["filled_qty"] is not None else None,
                avg_fill_price=_f(o["avg_fill_price"]),
                status=o["status"],
                order_id=str(o["order_id"]) if o["order_id"] is not None else None,
                reconciled_at=str(o["reconciled_at"]) if o["reconciled_at"] else None,
            )
            for o in orders
        ],
    )


def _f(value: object) -> float | None:
    """A driver's NUMERIC, or None. Typed as `object` rather than `Any` — the house rule in
    PROMPTS.md §"House rules" forbids the latter, and `object` is the honest description of a
    value whose type is decided by a database driver."""
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal, str)):
        return float(value)
    raise TypeError(f"expected a number from the database, got {type(value).__name__}")


def _as_list(value: object) -> list[str]:
    """`constituents_json` — a JSON array, however the driver hands it over."""
    if value in (None, ""):
        return []
    parsed = json.loads(value) if isinstance(value, str) else value
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _as_weights(value: object) -> dict[str, float]:
    """`weights_json` — a JSON object of symbol to percentage."""
    if value in (None, ""):
        return {}
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict):
        return {}
    return {str(k): float(v) for k, v in parsed.items() if _f(v) is not None}
