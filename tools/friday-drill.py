#!/usr/bin/env python
"""The DRY_RUN Friday drill: a whole rebalance session driven through Baskfy's own gateway.

WHY THIS EXISTS (leaf 1.2.3 of the desk-retirement tree)
--------------------------------------------------------
Retiring `kite-momentum-rebalancer` is only safe if Baskfy can do, on its own, the thing the desk
does every Friday: turn a scored scan and a book into a plan, put that plan through
`packages/execution`, and arm a GTT stop behind every buy. Until 1.2.1 landed
`OrderGateway.place_gtt_stop` / `delete_gtt` there was no way to do the GTT half at all, so this
drill could not have been written. Now it can, and what it asserts is the merge's actual claim:

    plan -> gateway -> journal, with zero orders reaching a broker and one stop per filled buy.

WHAT IT IS NOT
--------------
It is not a backtest and it is not a market simulation. The scan and the book below are synthetic
and deterministic on purpose: the subject under test is the *order path*, and a drill whose inputs
move cannot tell you whether the path changed. Every symbol is a NATO alphabet word so that no
output of this script can ever be mistaken for real trading activity.

THE SAFETY STORY, STATED ONCE
-----------------------------
Three independent things have to fail at the same time before this script can touch an account:

1. It refuses to start if `DRY_RUN` is set to anything false-ish, and it never reads that variable
   to *enable* anything -- the gates callable it hands the gateway returns `dry_run=True`
   unconditionally, so a mis-set environment cannot arm it.
2. The only broker it is given is `SpyBroker`, which has no network of any kind. Every verb that
   could reach Zerodha records the attempt and then raises.
3. `--mutate` exists so the assertions can be shown to bite, and every mutation it offers is inert
   with respect to the account: the worst one calls the *spy*.

Run it:

    cd decile-blueprint && DRY_RUN=true uv run python ../tools/friday-drill.py

or, with the environment set for you, `tools/friday-drill.sh`.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import contextlib
import json
import math
import os
import re
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

import pandas as pd
from baskfy_execution.gateway import OrderGateway, ProductGates
from baskfy_execution.gtt import (
    DEFAULT_STOP_BAND,
    DRY_RUN_GTT,
    DRY_RUN_GTT_DELETE,
    band_finding,
)
from baskfy_execution.guards import UntouchableInstrumentError, assert_tradeable
from baskfy_execution.risk import RiskManager
from baskfy_execution.tenancy import TenantIds

from baskfy_core.basket import build_plan

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = REPO_ROOT / "decile-blueprint"
DESK_ROOT = REPO_ROOT / "kite-momentum-rebalancer"

#: One account, one book. The drill is single-tenant; P4.3's cross-tenant refusal has its own
#: tests in `packages/execution/tests/test_tenant_isolation.py` and is not re-litigated here.
TENANT = TenantIds(user_id=1, broker_account_id=1)

#: Statuses that mean "this never went anywhere near a broker". Anything else in a drill result is
#: a failure of the drill's central claim, not a variation on it.
SIMULATED_ONLY: frozenset[str] = frozenset(
    {"DRY_RUN", "DUPLICATE", "BLOCKED", "RISK_BLOCKED", DRY_RUN_GTT, DRY_RUN_GTT_DELETE}
)

#: Journal events that can only be written after the broker answered. Seeing one of these in the
#: drill's journal means an order left the building even if the spy somehow missed it -- two
#: independent witnesses to the same fact, which is the point.
REACHED_BROKER_EVENTS: frozenset[str] = frozenset(
    {
        "placed",
        "rejected",
        "error",
        "gtt_placed",
        "gtt_error",
        "gtt_deleted",
        "gtt_delete_error",
    }
)

#: The authoritative stop formula, transcribed from the `momentum-rebalance` project skill rather
#: than imported: `clamp(ann_vol / sqrt(52) * 2.2, 8%, 12%)`. Importing `stop_from_vol` and then
#: comparing it against itself would assert nothing, so the drill keeps its own longhand copy and
#: checks the plan against it. If the two ever disagree, one of them has drifted.
STOP_VOL_MULT = 2.2
STOP_MIN = 0.08
STOP_MAX = 0.12
WEEKS_PER_YEAR = 52

#: `basket.build_plan` mints a 12-hex-character plan id, and the plan must propose at least
#: `TARGET_POSITIONS[0]` legs or the drill is exercising an empty book.
PLAN_ID_LEN = 12
MIN_PLAN_LEGS = 12
#: Prices are rounded to one decimal at write time (house rule 8), so anything below this is
#: float noise rather than a disagreement about where a stop belongs.
PRICE_EPSILON = 1e-9
#: One name in five sits below its 20-DMA, so breadth lands inside the bullish cash band
#: without being the degenerate 100%.
BELOW_MA_EVERY = 5
BELOW_MA_OFFSET = 4


def _f(value: object) -> float:
    """A number out of an untyped plan row. `float(object)` is not a type mypy will take."""
    return float(str(value))


def _i(value: object) -> int:
    """The same, for quantities. Quantities are whole shares; a fractional one is a bug."""
    return int(float(str(value)))


def independent_stop(price: float, ann_vol: float) -> float:
    """The stop the skill says a name at this price and this volatility should carry."""
    pct = min(max(ann_vol / math.sqrt(WEEKS_PER_YEAR) * STOP_VOL_MULT, STOP_MIN), STOP_MAX)
    return round(price * (1 - pct), 1)


# =====================================================================================
# The broker that is not a broker
# =====================================================================================
class SpyBroker:
    """Every way an order could reach Zerodha, wired to a counter and a refusal.

    `packages/execution/tests` has an `ExplodingKC` that raises; this one *records first*. The
    difference matters here because the drill drives dozens of calls through code it does not
    own, and an exception can be caught and turned into a status. A counter cannot be caught.

    The four class attributes are the broker constants `gtt.gtt_params` reads off the client
    (`GttConstants`); their values are irrelevant in DRY_RUN and are named so that a live path
    reaching them would produce obvious nonsense rather than a plausible wire payload.
    """

    GTT_TYPE_SINGLE = "DRILL-SINGLE"
    TRANSACTION_TYPE_SELL = "DRILL-SELL"
    ORDER_TYPE_LIMIT = "DRILL-LIMIT"
    PRODUCT_CNC = "DRILL-CNC"

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def _refuse(self, verb: str) -> NoReturn:
        self.attempts.append(verb)
        raise AssertionError(
            f"friday-drill: {verb}() reached the broker. DRY_RUN did not hold and this run is void."
        )

    def place_order(self, **_kwargs: object) -> str:
        self._refuse("place_order")

    def modify_order(self, **_kwargs: object) -> str:
        self._refuse("modify_order")

    def cancel_order(self, **_kwargs: object) -> str:
        self._refuse("cancel_order")

    def place_gtt(self, **_kwargs: object) -> dict[str, int]:
        self._refuse("place_gtt")

    def modify_gtt(self, **_kwargs: object) -> dict[str, int]:
        self._refuse("modify_gtt")

    def delete_gtt(self, **_kwargs: object) -> dict[str, int]:
        self._refuse("delete_gtt")

    def instruments(self, exchange: str) -> list[dict[str, object]]:
        # Reached only by the live tick-snap path. A hit here means DRY_RUN was bypassed *and*
        # a network call was attempted before the stop was placed.
        self._refuse(f"instruments({exchange})")

    def quote(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        self._refuse("quote")

    def ltp(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        self._refuse("ltp")

    def holdings(self) -> list[dict[str, object]]:
        self._refuse("holdings")


# =====================================================================================
# The strategy constants, as data
# =====================================================================================
# NOT frozen, and that is a typing fact rather than a preference: `BasketConfig` declares plain
# mutable attributes, and a frozen dataclass's read-only attributes do not satisfy them. Nothing
# writes to an instance of this.
@dataclass(slots=True)
class DrillConfig:
    """`baskfy_core.basket.BasketConfig`, filled with the desk's live numbers.

    Every value is `kite-momentum-rebalancer/app/config.py` verbatim (line numbers in the
    comments). They are copied rather than imported because importing the desk is the one thing
    this drill must not do -- the whole question it answers is whether Baskfy can rebalance after
    the desk is gone.
    """

    CLUSTER_CAP: float = 25.0  # config.py:56
    FULLY_INVESTED: bool = False  # config.py:98
    HALF_SIZE_WEIGHT: float = 3.0  # config.py:79
    MAX_POS_VS_DAY_VALUE: float = 0.01  # config.py:78
    MAX_SINGLE_WEIGHT: float = 15.0  # config.py:54
    MAX_TRADE_COST_PCT: float = 0.20  # config.py:77
    MIN_POSITION_WEIGHT: float = 6.0  # config.py:55
    MIN_TRADE_PCT: float = 2.0  # config.py:70
    MIN_TRADE_VALUE: float = 10_000.0  # config.py:69
    PARABOLIC_RSI: float = 82.0  # config.py:173
    REPLACEMENT_EDGE: float = 8.0  # config.py:178
    RETENTION_BUFFER: int = 5  # config.py:177
    RUNNER_MAX_VALUE: float = 300_000.0  # config.py:172
    TARGET_POSITIONS: tuple[int, int] = (12, 15)  # config.py:57
    TRIM_TO_WEIGHT: float = 2.0  # config.py:174
    STOP_VOL_MULT: float = STOP_VOL_MULT  # config.py:179
    STOP_MIN: float = STOP_MIN  # config.py:179
    STOP_MAX: float = STOP_MAX  # config.py:179
    CASH_BANDS: list[tuple[float, float]] = field(  # config.py:93-97
        default_factory=lambda: [(65.0, 5.0), (45.0, 15.0), (0.0, 35.0)]
    )
    EXCLUDED_SYMBOLS: object = field(default_factory=lambda: {"SGBDE31III"})


# =====================================================================================
# The synthetic Friday
# =====================================================================================
#: Eligible candidates. NATO words so nothing here can be mistaken for a tradeable NSE symbol.
CANDIDATES: tuple[str, ...] = (
    "ALPHA",
    "BRAVO",
    "CHARLIE",
    "DELTA",
    "ECHO",
    "FOXTROT",
    "GOLF",
    "HOTEL",
    "INDIA",
    "JULIET",
    "KILO",
    "LIMA",
    "MIKE",
    "NOVEMBER",
    "OSCAR",
    "PAPA",
    "QUEBEC",
    "ROMEO",
)
#: Filter-rejected names. SIERRA is held, so it becomes a capped runner (non-negotiable 7: a
#: filter-rejected stock is never *bought*, but one already held is not dumped either).
REJECTED: tuple[str, ...] = ("SIERRA", "TANGO", "UNIFORM")

#: Held but absent from the scan universe -> a full EXIT, and it carries a stale GTT that has to
#: be cancelled through the gateway once the position is gone.
ORPHAN = "ZULU"
#: Held, and untouchable. The plan must not propose it and the gateway must refuse it.
UNTOUCHABLE = "SGBAUG30"
#: Held with an RSI above PARABOLIC_RSI -> trimmed to a runner rather than kept at target weight.
PARABOLIC = "CHARLIE"

#: The buy that fills only partially. On 18 Aug 2026 the desk armed stops sized to *planned*
#: quantities before fills were known and ended up with 10,383 shares of GTT against 9,478 held.
#: The drill reproduces the condition and asserts the fix: the stop is sized from the fill.
PARTIAL_FILL = "GOLF"
PARTIAL_FILL_FRACTION = 0.6


#: One position, in the shape `build_plan` documents: symbol, quantity (total, including pledged
#: and T1 -- non-negotiable 2), pledged_qty, last_price, average_price. A plain mapping rather
#: than a TypedDict because the planner's own signature is `list[dict]`.
Holding = dict[str, object]


def scan_frame() -> pd.DataFrame:
    """A scored scan in the shape `build_plan` reads.

    Volatilities are spread from 0.18 to 0.55 deliberately: `clamp(vol/sqrt(52)*2.2, 8%, 12%)`
    saturates below 0.262 and above 0.393, so the run exercises both clamps and the interior.
    """
    rows: list[dict[str, object]] = []
    for i, symbol in enumerate(CANDIDATES):
        rows.append(
            {
                "symbol": symbol,
                "rank": i + 1,
                "SCORE": 100.0 - 2.5 * i,
                "reject": "",
                "close": 240.0 + 55.0 * i,
                # Four names below their 20-DMA: breadth lands in the bullish band without
                # being the degenerate 100%.
                "ma_20": (240.0 + 55.0 * i)
                * (1.04 if i % BELOW_MA_EVERY == BELOW_MA_OFFSET else 0.94),
                "rsi_one_month": 88.0 if symbol == PARABOLIC else 52.0 + (i % 17),
                "volatility_one_year": 0.18 + 0.022 * i,
                "median_volume_one_year": 5.0e8,
            }
        )
    for j, symbol in enumerate(REJECTED):
        rows.append(
            {
                "symbol": symbol,
                "rank": len(CANDIDATES) + j + 1,
                "SCORE": 20.0 - j,
                "reject": "below the 200-DMA",
                "close": 300.0 + 40.0 * j,
                "ma_20": (300.0 + 40.0 * j) * 1.08,
                "rsi_one_month": 34.0,
                "volatility_one_year": 0.45,
                "median_volume_one_year": 5.0e8,
            }
        )
    return pd.DataFrame(rows)


def opening_book(scan: pd.DataFrame) -> list[Holding]:
    """What the account holds before the rebalance."""
    price = dict(zip(scan["symbol"], scan["close"], strict=True))
    return [
        _hold("ALPHA", 900, 0, float(price["ALPHA"])),
        # Pledged shares sell directly on Zerodha (non-negotiable 3); the plan flags them as
        # information only, and this holding is what makes that note appear.
        _hold("BRAVO", 700, 250, float(price["BRAVO"])),
        _hold(PARABOLIC, 500, 0, float(price[PARABOLIC])),
        _hold("SIERRA", 400, 0, float(price["SIERRA"])),
        # Pledged AND on its way out: the exit is a pledged sell, which on Zerodha goes straight
        # through (instant sale) and is flagged as information only -- non-negotiable 3. Without
        # a pledged position that actually sells, `pledged_sells_margin_note` is empty and the
        # assertion about it asserts nothing.
        _hold(ORPHAN, 300, 150, 415.0),
        _hold(UNTOUCHABLE, 120, 0, 6_100.0),
    ]


def _hold(symbol: str, qty: int, pledged: int, price: float) -> Holding:
    return {
        "symbol": symbol,
        "quantity": qty,
        "pledged_qty": pledged,
        "last_price": price,
        "average_price": round(price * 0.86, 2),
    }


def live_prices(scan: pd.DataFrame) -> dict[str, float]:
    """Every eligible name carries an LTP. `build_plan` drops anything that does not."""
    return {str(r.symbol): float(r.close) for _, r in scan.iterrows()}


def clusters() -> dict[str, str]:
    """Sector map, round-robin over four clusters so the cap is approached but not obviously hit."""
    names = ("financials", "materials", "consumer", "technology")
    return {sym: names[i % len(names)] for i, sym in enumerate((*CANDIDATES, *REJECTED))}


def tradeable(symbol: str) -> bool:
    """The gateway's own guard, as the predicate the planner requires.

    Injected rather than reimplemented so a plan cannot propose something the order path would
    refuse -- which is the failure that produced `EXIT -392` on a Rs 60 lakh SGB position.
    """
    try:
        assert_tradeable(symbol)
    except UntouchableInstrumentError:
        return False
    return True


# =====================================================================================
# The report
# =====================================================================================
@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    detail: str


class Report:
    """Every assertion the drill makes, and whether it held.

    Two channels, and keeping them apart is the point. A **check** is something Baskfy claims to
    do and must keep doing: it fails the run. A **gap** is enforcement that provably does not
    exist yet -- leaf 1.2.2 found that non-negotiables 1, 4 and 6 are enforced by desk files that
    retirement deletes -- and folding those into the same tally would make a real regression
    indistinguishable from a known hole. A gap is measured every run, reported loudly, and does
    not fail the drill; when one closes, the ledger says so and it should be promoted to a check.
    """

    def __init__(self) -> None:
        self.checks: list[Check] = []
        self.gaps: list[Check] = []
        self.notes: list[str] = []

    def check(self, name: str, ok: bool, detail: str) -> bool:
        self.checks.append(Check(name, bool(ok), detail))
        return bool(ok)

    def gap(self, name: str, closed: bool, detail: str) -> bool:
        self.gaps.append(Check(name, bool(closed), detail))
        return bool(closed)

    def note(self, text: str) -> None:
        self.notes.append(text)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def render(self) -> str:
        lines = ["", "== FRIDAY DRILL LEDGER =="]
        lines.extend(f"[{'PASS' if c.ok else 'FAIL'}] {c.name}: {c.detail}" for c in self.checks)
        if self.gaps:
            lines.append("")
            lines.append("-- gaps: enforcement that does not exist in packages/ or services/ --")
            lines.extend(
                f"[{'CLOSED' if g.ok else 'OPEN'}] {g.name}: {g.detail}" for g in self.gaps
            )
        if self.notes:
            lines.append("")
            lines.append("-- findings --")
            lines.extend(f"  * {n}" for n in self.notes)
        return "\n".join(lines)


# =====================================================================================
# Journal reading
# =====================================================================================
def read_journal(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    out: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def event_counts(records: Sequence[Mapping[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rec in records:
        key = str(rec.get("event") or "?")
        counts[key] = counts.get(key, 0) + 1
    return counts


# =====================================================================================
# The session
# =====================================================================================
@dataclass(slots=True)
class Session:
    """One run of the drill, with everything the assertions need to read afterwards."""

    plan: dict[str, object]
    spy: SpyBroker
    journal_path: Path
    order_results: list[dict[str, object]] = field(default_factory=list)
    replay_results: list[dict[str, object]] = field(default_factory=list)
    gtt_results: list[dict[str, object]] = field(default_factory=list)
    cancel_results: list[dict[str, object]] = field(default_factory=list)
    fills: dict[str, int] = field(default_factory=dict)
    journal_after_orders: int = 0


def gateway_for(spy: SpyBroker, journal_path: Path) -> OrderGateway:
    """A gateway whose gates can only ever say DRY_RUN.

    A *callable* is what the gateway wants, and this one closes over nothing and reads nothing:
    there is no environment variable, no config module and no argument that can make it return
    `dry_run=False`. The two product switches are left at their fail-closed defaults, so a stray
    MIS or NFO leg is BLOCKED rather than simulated.
    """
    return OrderGateway(
        spy,
        RiskManager(),
        gates=lambda: ProductGates(dry_run=True, intraday_enabled=False, options_enabled=False),
        journal_path=str(journal_path),
    )


def tradeable_legs(plan: Mapping[str, object]) -> list[dict[str, object]]:
    """The legs that would actually be sent, in the desk's own order: sells first, then buys.

    Sells first frees the cash the buys need, and within each side the largest order goes first
    -- `main.py:568`. Preserved because a drill that sends them in a different order is not
    drilling the sequence the account has been rebalanced on.
    """
    orders = plan["orders"]
    if not isinstance(orders, list):
        raise TypeError("plan['orders'] is not a list")
    legs = [dict(o) for o in orders if int(o["delta"]) != 0]
    legs.sort(key=lambda o: (int(o["delta"]) > 0, -abs(int(o["delta"]) * float(o["ref_price"]))))
    return legs


async def send_orders(
    gw: OrderGateway,
    plan: Mapping[str, object],
    legs: Sequence[Mapping[str, object]],
    *,
    client_id: Callable[[str], str],
) -> list[dict[str, object]]:
    """Every leg through `OrderGateway.place`, one at a time, exactly as `/execute` does."""
    gross = _f(plan.get("book_value") or 0.0)
    results: list[dict[str, object]] = []
    for leg in legs:
        symbol = str(leg["symbol"])
        delta = _i(leg["delta"])
        try:
            res = await gw.place(
                symbol=symbol,
                qty=abs(delta),
                side="SELL" if delta < 0 else "BUY",
                product="CNC",
                order_type="LIMIT",
                price=_f(leg["ref_price"]),
                exchange="NSE",
                client_id=client_id(symbol),
                gross_exposure=gross,
                tenant=TENANT,
                plan_tenant=TENANT,
            )
        except UntouchableInstrumentError as exc:
            # Per leg, never per batch: one protected instrument must not abandon a book that
            # has already been half-rebalanced (`main.py:585`).
            res = {"symbol": symbol, "status": "BLOCKED", "error": str(exc)}
        res["action"] = leg["action"]
        results.append(res)
    return results


def simulate_fills(
    legs: Sequence[Mapping[str, object]],
    results: Sequence[Mapping[str, object]],
    *,
    unfilled: str = "",
) -> dict[str, int]:
    """What the exchange did with the buys.

    These are LIMIT orders and some of them rest, which is the entire reason stops are armed
    from the fills rather than from the plan. One name fills partially and (in the probe) one
    fills not at all.

    A leg that the gateway refused fills nothing, and that is not a detail: it is the only
    reason a BLOCKED buy cannot end up wearing a stop for shares that were never bought.
    """
    simulated = {str(r["symbol"]) for r in results if str(r.get("status")) == "DRY_RUN"}
    fills: dict[str, int] = {}
    for leg in legs:
        delta = _i(leg["delta"])
        if delta <= 0:
            continue
        symbol = str(leg["symbol"])
        if symbol == unfilled or symbol not in simulated:
            fills[symbol] = 0
        elif symbol == PARTIAL_FILL:
            fills[symbol] = int(delta * PARTIAL_FILL_FRACTION)
        else:
            fills[symbol] = delta
    return fills


async def arm_stops(
    gw: OrderGateway,
    legs: Sequence[Mapping[str, object]],
    fills: Mapping[str, int],
    *,
    client_id: Callable[[str], str],
    skip: str = "",
) -> list[dict[str, object]]:
    """Non-negotiable 4, after the fills: one vol-scaled GTT behind every share that arrived.

    The quantity is the FILL, never the plan's `qty_final`. The trigger is the plan's `stop` --
    computed by `stop_from_vol` at plan time -- and the gateway is not asked to recompute it,
    because a second implementation of the stop level is the one thing that could make two parts
    of the system disagree about where a stop belongs.

    A buy that filled nothing gets nothing. A 438-share trigger once sat on PARAS before a single
    share of it had filled, and an over-covered stop sells what is not held when it fires.
    """
    results: list[dict[str, object]] = []
    for leg in legs:
        symbol = str(leg["symbol"])
        if _i(leg["delta"]) <= 0 or symbol == skip:
            continue
        qty = int(fills.get(symbol, 0))
        if qty <= 0:
            continue
        res = await gw.place_gtt_stop(
            symbol=symbol,
            qty=qty,
            trigger=_f(leg["stop"]),
            last_price=_f(leg["ref_price"]),
            exchange="NSE",
            client_id=client_id(symbol),
            tenant=TENANT,
            plan_tenant=TENANT,
        )
        results.append(res)
    return results


async def cancel_orphan_stop(gw: OrderGateway, gtt_id: int) -> dict[str, object]:
    """The stale trigger on the exited name, removed through the gateway.

    Cancelling is the half of the GTT surface the desk barely guarded: `kite_client.delete_gtt`
    ran no instrument check at all when the caller omitted the symbol. Driving it here proves
    the guarded path works end to end and not merely that it compiles.
    """
    return await gw.delete_gtt(
        gtt_id=gtt_id, symbol=ORPHAN, exchange="NSE", tenant=TENANT, plan_tenant=TENANT
    )


async def run_session(journal_path: Path, mutate: str) -> Session:
    """Plan, execute, fill, arm, cancel, replay -- one Friday, start to finish."""
    scan = scan_frame()
    plan = build_plan(
        scan,
        list(opening_book(scan)),
        cash=2_500_000.0,
        cfg=DrillConfig(),
        tradeable=tradeable,
        clusters=clusters(),
        live_prices=live_prices(scan),
    )
    plan_id = str(plan["plan_id"])

    def client_id(symbol: str) -> str:
        """`client_id = plan_id:symbol` -- non-negotiable 6, constructed at `main.py:581`.

        Deterministic per plan and symbol, so a re-posted plan cannot double-send. The GTT half
        reuses the identical string on purpose: it is the only way to prove that 1.2.1's separate
        GTT idempotency map really is separate. Were the two maps one, every stop would come back
        DUPLICATE of the buy that created it and the whole book would be armed with nothing.
        """
        if mutate == "wrong-client-id":
            # Still deterministic per plan and symbol -- so idempotency survives -- but no
            # longer `plan_id:symbol`, which is the shape A12 reads out of the order id.
            return f"{plan_id}-{symbol}"
        return f"{plan_id}:{symbol}"

    spy = SpyBroker()
    gw = gateway_for(spy, journal_path)
    session = Session(plan=plan, spy=spy, journal_path=journal_path)

    legs = tradeable_legs(plan)
    session.order_results = await send_orders(gw, plan, legs, client_id=client_id)
    session.journal_after_orders = event_counts(read_journal(journal_path)).get("dry_run", 0)

    session.fills = simulate_fills(legs, session.order_results)
    # Whichever name happens to be the first buy — hard-coding a symbol would let the mutation
    # silently become a no-op the day the plan stops buying it.
    first_buy = next((str(o["symbol"]) for o in legs if _i(o["delta"]) > 0), "")
    session.gtt_results = await arm_stops(
        gw,
        legs,
        session.fills,
        client_id=client_id,
        skip=first_buy if mutate == "skip-a-stop" else "",
    )
    session.cancel_results = [await cancel_orphan_stop(gw, gtt_id=770_000_001)]

    # The re-post. Nothing new may be journalled and nothing new may be simulated.
    session.replay_results = await send_orders(gw, plan, legs, client_id=client_id)

    if mutate == "touch-the-broker":
        # Deliberately inert: this calls the SPY, which has no network. It exists to show that
        # the zero-orders assertion is watching something real rather than counting to zero.
        with_suppressed_refusal(spy)

    return session


def with_suppressed_refusal(spy: SpyBroker) -> None:
    """Provoke the spy exactly once, swallowing its refusal, to prove the counter is the witness.

    The swallow is the point. An assertion that only survives because an exception propagated is
    not an assertion about the system, it is an assertion about the caller's error handling.
    """
    with contextlib.suppress(AssertionError):
        spy.place_order(tradingsymbol="ALPHA", quantity=1)


async def unfilled_buy_probe(journal_path: Path) -> list[dict[str, object]]:
    """A buy that fills nothing must be armed with nothing.

    Kept out of the main session so it cannot be confused with the buys-equal-stops count: this
    is the *opposite* assertion, and folding it into the same tally would let a missing stop and
    a spurious one cancel out.
    """
    scan = scan_frame()
    plan = build_plan(
        scan,
        list(opening_book(scan)),
        cash=2_500_000.0,
        cfg=DrillConfig(),
        tradeable=tradeable,
        clusters=clusters(),
        live_prices=live_prices(scan),
    )
    legs = tradeable_legs(plan)
    buys = [leg for leg in legs if _i(leg["delta"]) > 0]
    if not buys:
        return []
    dead = str(buys[0]["symbol"])
    gw = gateway_for(SpyBroker(), journal_path)

    def client_id(symbol: str) -> str:
        return f"{plan['plan_id']}:{symbol}"

    results = await send_orders(gw, plan, legs, client_id=client_id)
    fills = simulate_fills(legs, results, unfilled=dead)
    armed = await arm_stops(gw, legs, fills, client_id=client_id)
    return [{"unfilled_symbol": dead, "armed_symbols": [str(a["symbol"]) for a in armed]}]


# =====================================================================================
# Guard probes -- refusals, on their own gateway so they cannot pollute the counts
# =====================================================================================
async def guard_probes(journal_path: Path) -> dict[str, dict[str, object]]:
    """Four things the gateway must refuse, driven for real rather than asserted in prose."""
    spy = SpyBroker()
    gw = gateway_for(spy, journal_path)
    out: dict[str, dict[str, object]] = {}

    try:
        await gw.place_gtt_stop(
            symbol=UNTOUCHABLE,
            qty=10,
            trigger=5_500.0,
            last_price=6_100.0,
            tenant=TENANT,
            plan_tenant=TENANT,
        )
        out["untouchable_gtt"] = {"refused": False}
    except UntouchableInstrumentError as exc:
        out["untouchable_gtt"] = {"refused": True, "error": str(exc)}

    # A "stop" at or above the last price is a market exit wearing protection's clothes.
    out["inverted_stop"] = await gw.place_gtt_stop(
        symbol="ALPHA",
        qty=10,
        trigger=250.0,
        last_price=240.0,
        tenant=TENANT,
        plan_tenant=TENANT,
    )
    # An option under CNC would be an overnight option position by construction.
    out["overnight_option"] = await gw.place_gtt_stop(
        symbol="NIFTY26SEP25000CE",
        qty=50,
        trigger=90.0,
        last_price=120.0,
        exchange="NFO",
        tenant=TENANT,
        plan_tenant=TENANT,
    )
    # MIS with INTRADAY_ENABLED off, which is the fail-closed default.
    out["intraday_order"] = await gw.place(
        symbol="ALPHA",
        qty=1,
        side="BUY",
        product="MIS",
        order_type="LIMIT",
        price=240.0,
        exchange="NSE",
        tenant=TENANT,
        plan_tenant=TENANT,
    )
    out["spy"] = {"attempts": list(spy.attempts)}
    return out


# =====================================================================================
# The 30-minute expiry probe (gate G5)
# =====================================================================================
SRC_ROOTS: tuple[str, ...] = (
    "packages/core/src",
    "packages/execution/src",
    "packages/providers/src",
    "services/api/src",
    "services/worker/src",
)
#: The two names Baskfy uses for plan expiry. `curated_plans` defines the TTL and stamps the
#: hint; if either is ever *compared* against a clock, something refuses a stale plan.
_EXPIRY_TOKENS: tuple[str, ...] = ("expires_at_hint", "PLAN_TTL")


def expiry_enforcement_sites() -> dict[str, list[str]]:
    """Where Baskfy's own tree mentions plan expiry, and whether any of it is a refusal.

    `baskfy_core.curated_plans` defines `PLAN_TTL = 30 minutes` and stamps `expires_at_hint` on
    every plan it builds. Stamping is not enforcing. This walks the source that survives desk
    retirement and separates the two.

    A COMPARISON IS FOUND BY PARSING, NOT BY GREP. The first version of this looked for `<` or
    `>` on the line and duly reported `def _expires_at_hint(now: dt.datetime) -> dt.datetime:` as
    enforcement, because a return arrow contains a greater-than sign. A probe that reports a
    gate where none exists is worse than no probe: it would have closed this leaf's G5 on a
    function signature.
    """
    mentions: list[str] = []
    comparisons: list[str] = []
    for root in SRC_ROOTS:
        base = WORKSPACE / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if not any(tok in text for tok in _EXPIRY_TOKENS):
                continue
            where = path.relative_to(WORKSPACE)
            mentions.extend(
                f"{where}:{n}"
                for n, line in enumerate(text.splitlines(), 1)
                if any(tok in line for tok in _EXPIRY_TOKENS)
            )
            for node in ast.walk(ast.parse(text, filename=str(path))):
                if not isinstance(node, ast.Compare):
                    continue
                segment = ast.get_source_segment(text, node) or ""
                if any(tok in segment for tok in _EXPIRY_TOKENS):
                    comparisons.append(f"{where}:{node.lineno}  {segment.strip()}")
    return {"mentions": mentions, "comparisons": comparisons}


# =====================================================================================
# Assertions
# =====================================================================================
def assert_plan(report: Report, session: Session) -> None:
    plan = session.plan
    plan_id = str(plan["plan_id"])
    orders = plan["orders"]
    legs = orders if isinstance(orders, list) else []
    symbols = {str(o["symbol"]) for o in legs}
    excluded = plan["excluded"]
    excluded_list = excluded if isinstance(excluded, list) else []

    report.check(
        "A1 plan built by baskfy_core",
        len(plan_id) == PLAN_ID_LEN and len(legs) >= MIN_PLAN_LEGS,
        f"plan_id={plan_id} legs={len(legs)} capital={plan['capital']} "
        f"breadth={plan['breadth_above_20dma']}% cash_target={plan['cash_target_pct']}%",
    )
    report.check(
        "A2 the untouchable is excluded from the plan",
        UNTOUCHABLE not in symbols and UNTOUCHABLE in excluded_list,
        f"{UNTOUCHABLE} excluded={excluded_list}, absent from every leg",
    )
    pledged = plan["pledged_sells_margin_note"]
    pledged_list = pledged if isinstance(pledged, list) else []
    report.check(
        "A3 a pledged sell is flagged, not gated",
        ORPHAN in pledged_list,
        f"pledged_sells_margin_note={pledged_list}; {ORPHAN} holds 150 pledged of 300 and is a "
        f"full EXIT, so it sells directly with no unpledge step (non-negotiable 3)",
    )


def assert_stop_formula(report: Report, session: Session) -> None:
    """Every stop the plan carries matches the skill's formula, longhand."""
    scan = scan_frame()
    vol = {str(r.symbol): float(r.volatility_one_year) for _, r in scan.iterrows()}
    orders = session.plan["orders"]
    legs = orders if isinstance(orders, list) else []
    wrong: list[str] = []
    checked = 0
    for leg in legs:
        symbol, stop = str(leg["symbol"]), leg["stop"]
        if stop is None or symbol not in vol:
            continue
        checked += 1
        expected = independent_stop(_f(leg["ref_price"]), vol[symbol])
        if abs(float(stop) - expected) > PRICE_EPSILON:
            wrong.append(f"{symbol} plan={stop} skill={expected}")
    report.check(
        "A4 every stop matches clamp(vol/sqrt(52)*2.2, 8%, 12%)",
        checked > 0 and not wrong,
        f"{checked} stops recomputed longhand from the momentum-rebalance skill; "
        f"mismatches={wrong or 'none'}",
    )


def assert_zero_broker_contact(
    report: Report, session: Session, journal: Sequence[dict[str, object]]
) -> None:
    """Gate G2, from two independent witnesses."""
    reached = sorted({str(r.get("event")) for r in journal} & REACHED_BROKER_EVENTS)
    statuses = {
        str(r.get("status"))
        for r in (
            *session.order_results,
            *session.replay_results,
            *session.gtt_results,
            *session.cancel_results,
        )
    }
    stray = sorted(statuses - SIMULATED_ONLY)
    report.check(
        "A5 the broker was never called",
        not session.spy.attempts,
        f"SpyBroker recorded {len(session.spy.attempts)} attempts: "
        f"{session.spy.attempts or 'none'} (every order/GTT verb counts before it raises)",
    )
    report.check(
        "A6 the journal holds no post-broker event",
        not reached,
        f"{len(journal)} journal lines, events={event_counts(journal)}; "
        f"post-broker events present={reached or 'none'}",
    )
    report.check(
        "A7 every result status is a simulation",
        not stray,
        f"statuses={sorted(statuses)}; outside the simulated set={stray or 'none'}",
    )


def assert_buys_equal_stops(report: Report, session: Session) -> None:
    """Gate G3 -- non-negotiable 4, made countable."""
    buys = [
        r
        for r in session.order_results
        if str(r.get("action")) in ("BUY", "ADD") and str(r.get("status")) == "DRY_RUN"
    ]
    filled = [r for r in buys if session.fills.get(str(r["symbol"]), 0) > 0]
    armed = [r for r in session.gtt_results if str(r.get("status")) == DRY_RUN_GTT]
    buy_symbols = {str(r["symbol"]) for r in filled}
    armed_symbols = {str(r["symbol"]) for r in armed}
    zero_fill = [s for s, q in session.fills.items() if q == 0]

    difference = buy_symbols ^ armed_symbols
    sets = "match" if not difference else f"DIFFER by {sorted(difference)}"
    report.check(
        "A8 buys simulated == stops simulated",
        len(filled) == len(armed) and len(armed) > 0 and not difference,
        f"buy legs sent={len(buys)}, of which filled>0={len(filled)}, "
        f"GTT stops simulated={len(armed)}; zero-fill legs={zero_fill or 'none'}; "
        f"symbol sets {sets}",
    )
    every_gtt_ok = all(str(r.get("status")) == DRY_RUN_GTT for r in session.gtt_results)
    report.check(
        "A9 no stop was refused or errored",
        every_gtt_ok,
        f"{len(session.gtt_results)} arming attempts, all "
        f"{sorted({str(r.get('status')) for r in session.gtt_results}) or 'n/a'}",
    )


def assert_stop_sizing(report: Report, session: Session) -> None:
    """The 18 Aug 2026 finding: a trigger sized to the plan rather than the fill."""
    planned = {
        str(leg["symbol"]): _i(leg["delta"])
        for leg in tradeable_legs(session.plan)
        if _i(leg["delta"]) > 0
    }
    mis_sized = [
        f"{r['symbol']} gtt={r.get('qty')} filled={session.fills.get(str(r['symbol']))}"
        for r in session.gtt_results
        if _i(r.get("qty") or 0) != int(session.fills.get(str(r["symbol"]), -1))
    ]
    partial_ok = PARTIAL_FILL in planned and session.fills.get(PARTIAL_FILL, 0) not in (
        0,
        planned.get(PARTIAL_FILL),
    )
    partial_stop = next(
        (r.get("qty") for r in session.gtt_results if str(r["symbol"]) == PARTIAL_FILL), None
    )
    report.check(
        "A10 every stop is sized to the fill, not the plan",
        not mis_sized and partial_ok,
        f"{PARTIAL_FILL} planned={planned.get(PARTIAL_FILL)} "
        f"filled={session.fills.get(PARTIAL_FILL)} stop_qty={partial_stop}; "
        f"mis-sized={mis_sized or 'none'}",
    )


def assert_stop_band(report: Report, session: Session) -> None:
    """Every simulated trigger rests 8-12% below the price it was measured against."""
    by_symbol = {str(leg["symbol"]): leg for leg in tradeable_legs(session.plan) if leg.get("stop")}
    findings: list[str] = []
    for res in session.gtt_results:
        symbol = str(res["symbol"])
        leg = by_symbol.get(symbol)
        if leg is None:
            continue
        finding = band_finding(
            trigger=_f(res["trigger"]),
            last_price=_f(leg["ref_price"]),
            band=DEFAULT_STOP_BAND,
        )
        if finding:
            findings.append(f"{symbol}:{finding}")
        if abs(_f(res["trigger"]) - _f(leg["stop"])) > PRICE_EPSILON:
            findings.append(f"{symbol}: gateway changed the trigger")
    report.check(
        "A11 every trigger is inside the 8-12% band and unmodified",
        not findings,
        f"{len(session.gtt_results)} triggers checked against "
        f"[{DEFAULT_STOP_BAND.min_pct}, {DEFAULT_STOP_BAND.max_pct}]; "
        f"findings={findings or 'none'}",
    )


def assert_idempotency(
    report: Report, session: Session, journal: Sequence[dict[str, object]]
) -> None:
    """Non-negotiable 6: a re-posted plan cannot double-send."""
    plan_id = str(session.plan["plan_id"])
    ids_ok = [
        str(r["order_id"]) == f"DRY-{plan_id}:{r['symbol']}"
        for r in session.order_results
        if r.get("order_id")
    ]
    replay_statuses = {str(r.get("status")) for r in session.replay_results}
    # Cancels and stops were journalled between the two order passes, so the count is taken from
    # the order events alone.
    counts = event_counts(journal)
    report.check(
        "A12 every simulated order carries client_id = plan_id:symbol",
        bool(ids_ok) and all(ids_ok),
        f"{sum(ids_ok)}/{len(ids_ok)} order ids are DRY-{plan_id}:<symbol>",
    )
    report.check(
        "A13 the re-posted plan double-sends nothing",
        replay_statuses == {"DUPLICATE"}
        and counts.get("dry_run", 0) == session.journal_after_orders,
        f"replay of {len(session.replay_results)} legs returned {sorted(replay_statuses)}; "
        f"dry_run journal lines {session.journal_after_orders} before the replay, "
        f"{counts.get('dry_run', 0)} after",
    )
    report.check(
        "A14 the GTT idempotency map is separate from the order map",
        all(str(r.get("status")) == DRY_RUN_GTT for r in session.gtt_results),
        f"{len(session.gtt_results)} stops reused the identical client_id plan_id:symbol and "
        f"none came back DUPLICATE of the buy that created it",
    )


def assert_journal_completeness(
    report: Report, session: Session, journal: Sequence[dict[str, object]]
) -> None:
    """Gate G4's first half: one durable line per simulated action."""
    counts = event_counts(journal)
    expected_orders = sum(1 for r in session.order_results if str(r.get("status")) == "DRY_RUN")
    expected_gtts = len([r for r in session.gtt_results if str(r.get("status")) == DRY_RUN_GTT])
    expected_cancels = len(
        [r for r in session.cancel_results if str(r.get("status")) == DRY_RUN_GTT_DELETE]
    )
    ok = (
        counts.get("dry_run", 0) == expected_orders
        and counts.get("gtt_dry_run", 0) == expected_gtts
        and counts.get("gtt_dry_run_delete", 0) == expected_cancels
    )
    report.check(
        "A15 one journal line per simulated action",
        ok,
        f"orders {counts.get('dry_run', 0)}/{expected_orders}, "
        f"stops {counts.get('gtt_dry_run', 0)}/{expected_gtts}, "
        f"cancels {counts.get('gtt_dry_run_delete', 0)}/{expected_cancels}",
    )
    with_cid = [r for r in journal if "client_id" in r]
    report.gap(
        "G4 the journal line names the client_id it acted on",
        bool(with_cid),
        f"{len(with_cid)} of {len(journal)} journal lines carry a client_id field. The gateway "
        f"writes symbol/side/qty/price and never the id it deduplicated on, in DRY_RUN and in "
        f"live alike, so a journal cannot be reconciled to a plan",
    )


def assert_fail_closed(report: Report) -> None:
    """The two switches that decide whether this could ever have been real."""
    default = ProductGates()
    report.check(
        "A22 ProductGates defaults refuse",
        default.dry_run and not default.intraday_enabled and not default.options_enabled,
        f"ProductGates() = dry_run={default.dry_run}, "
        f"intraday_enabled={default.intraday_enabled}, "
        f"options_enabled={default.options_enabled} - a caller who forgets gets the safest "
        f"configuration, not the most permissive",
    )
    refused = {raw: bool(live_refusal(raw)) for raw in (*LIVE_SPELLINGS, "False", "OFF")}
    allowed = {raw: bool(live_refusal(raw)) for raw in ("true", "1", "", "yes")}
    report.check(
        "A23 the drill refuses to start outside DRY_RUN",
        all(refused.values()) and not any(allowed.values()),
        f"refused={sorted(refused)}, permitted={sorted(allowed)} - checked as a pure function "
        f"of the string, so no shell in this leaf ever set DRY_RUN=false",
    )


#: A shell assignment of DRY_RUN, wherever it sits on the line: bare, exported, or as a one-shot
#: prefix on a command.
_SHELL_DRY_RUN = re.compile(r"(?:^|[\s;&|(])(?:export\s+)?DRY_RUN=([^\s;&|)]*)")


def live_settings(label: str, text: str, *, python: bool) -> list[str]:
    """Anywhere this source could put the system into a live state, found by parsing.

    PARSED, NOT GREPPED, and that is the whole reason this function exists rather than a `grep`
    in the gate. The grep it replaces matched the sentence in `gateway_for`'s docstring that
    explains why nothing can make the gates return a live value -- a check that fails on its own
    explanation trains you to delete the explanation, which is the opposite of what it is for.

    Python: any `dry_run=` keyword or assignment whose value is not the literal `True`. Shell:
    any `DRY_RUN=` assignment, outside a comment, whose value is one of `LIVE_SPELLINGS`.

    It takes TEXT rather than a path so that the mutation which proves it can fail never has to
    write `DRY_RUN=false` into a file that something could later run.
    """
    out: list[str] = []
    if python:
        for node in ast.walk(ast.parse(text, filename=label)):
            if isinstance(node, ast.keyword) and node.arg == "dry_run":
                value = node.value
                if not (isinstance(value, ast.Constant) and value.value is True):
                    out.append(f"{label}:{value.lineno} dry_run= is not the literal True")
            if isinstance(node, ast.Assign) and not (
                isinstance(node.value, ast.Constant) and node.value.value is True
            ):
                out.extend(
                    f"{label}:{node.lineno} {target.id} assigned a non-True value"
                    for target in node.targets
                    if isinstance(target, ast.Name) and target.id in ("dry_run", "DRY_RUN")
                )
        return out
    for lineno, line in enumerate(text.splitlines(), 1):
        code = line.split("#", 1)[0]
        out.extend(
            f"{label}:{lineno} {match.group(0).strip()}"
            for match in _SHELL_DRY_RUN.finditer(code)
            if match.group(1).strip().strip("\"'").lower() in LIVE_SPELLINGS
        )
    return out


def live_settings_in(path: Path) -> list[str]:
    """`live_settings` over one of this leaf's own files."""
    return live_settings(path.name, path.read_text(encoding="utf-8"), python=path.suffix == ".py")


#: A runner that would arm the drill, held as a STRING and never written to disk. The mutation
#: below feeds it to the same parser the real files go through.
_MUTANT_RUNNER = "#!/usr/bin/env bash\nexport DRY_RUN=false\nexec ./friday-drill.py\n"


def assert_no_live_setting(report: Report, mutate: str = "") -> None:
    """Gate G6, audited out of the leaf's own two files rather than asserted in prose."""
    files = [Path(__file__).resolve(), Path(__file__).resolve().with_suffix(".sh")]
    audited = [path.name for path in files if path.exists()]
    offenders = [entry for path in files if path.exists() for entry in live_settings_in(path)]
    if mutate == "live-setting":
        audited.append("mutant-runner.sh (in memory)")
        offenders.extend(live_settings("mutant-runner.sh", _MUTANT_RUNNER, python=False))
    report.check(
        "A24 no file this leaf owns can set a live DRY_RUN",
        not offenders,
        f"parsed {', '.join(audited)}; live dry_run settings={offenders or 'none'}",
    )


def assert_no_desk_import(report: Report) -> None:
    """The drill must not have reached into `kite-momentum-rebalancer` to do any of this."""
    loaded = sorted(
        name
        for name, module in list(sys.modules.items())
        if getattr(module, "__file__", None) and str(DESK_ROOT) in str(module.__file__)
    )
    report.check(
        "A25 the desk was never imported",
        not loaded,
        f"modules loaded from {DESK_ROOT.name}: {loaded or 'none'}",
    )


def assert_guards(report: Report, probes: Mapping[str, Mapping[str, object]]) -> None:
    untouchable = probes["untouchable_gtt"]
    inverted = probes["inverted_stop"]
    overnight = probes["overnight_option"]
    intraday = probes["intraday_order"]
    spy_attempts = probes["spy"]["attempts"]
    report.check(
        "A16 an SGB cannot be given a stop",
        bool(untouchable.get("refused")),
        f"{UNTOUCHABLE}: {untouchable.get('error', 'NOT REFUSED')}",
    )
    report.check(
        "A17 a trigger at or above the last price is refused",
        str(inverted.get("status")) == "BLOCKED",
        f"trigger 250.0 vs last 240.0 -> {inverted.get('status')}: {inverted.get('error')}",
    )
    report.check(
        "A18 an option stop under CNC is refused",
        str(overnight.get("status")) == "BLOCKED",
        f"NIFTY26SEP25000CE -> {overnight.get('status')}: {overnight.get('error')}",
    )
    report.check(
        "A19 MIS is refused while INTRADAY_ENABLED is off",
        str(intraday.get("status")) == "BLOCKED",
        f"MIS order -> {intraday.get('status')}: {intraday.get('error')}",
    )
    report.check(
        "A20 no refusal spent a broker call",
        not spy_attempts,
        f"SpyBroker attempts during the guard probes: {spy_attempts or 'none'}",
    )


def assert_unfilled_probe(report: Report, probe: Sequence[Mapping[str, object]]) -> None:
    if not probe:
        report.check("A21 an unfilled buy gets no stop", False, "the probe produced no buys")
        return
    row = probe[0]
    dead = str(row["unfilled_symbol"])
    armed = row["armed_symbols"]
    armed_list = armed if isinstance(armed, list) else []
    outcome = "NOT AT ALL (correct)" if dead not in armed_list else "ANYWAY (wrong)"
    report.check(
        "A21 an unfilled buy gets no stop",
        dead not in armed_list,
        f"{dead} filled 0 shares and was armed {outcome}; {len(armed_list)} other stops simulated",
    )


def assert_expiry(report: Report, sites: Mapping[str, Sequence[str]]) -> None:
    """Gate G5. Measured, not asserted: the enforcement is absent and that is the finding."""
    report.gap(
        "G5 a plan older than 30 minutes is refused",
        bool(sites["comparisons"]),
        f"PLAN_TTL/expires_at_hint appears at {len(sites['mentions'])} sites in packages/ and "
        f"services/, of which {len(sites['comparisons'])} compare it against a clock: "
        f"{list(sites['comparisons']) or 'NONE - the expiry is stamped, never enforced'}",
    )


# =====================================================================================
# Entry point
# =====================================================================================
#: Every spelling of "not a dry run" this drill refuses to start under.
LIVE_SPELLINGS: tuple[str, ...] = ("false", "0", "no", "off")


def live_refusal(raw: str) -> str:
    """Why this environment must not run the drill, or "" when it is safe.

    A pure function of the string rather than a read of `os.environ`, so the refusal can be
    exercised in the ledger below WITHOUT ever setting `DRY_RUN=false` in a shell. That
    distinction is gate G6's, and it is not pedantry: the only way to test an env-var guard by
    setting the env var is to create, however briefly, the state the guard exists to prevent.
    """
    if raw.strip().lower() in LIVE_SPELLINGS:
        return (
            f"friday-drill: DRY_RUN is set to {raw.strip()!r}. This drill only ever simulates; "
            f"it will not run in an environment that claims to be live."
        )
    return ""


def refuse_unless_dry_run() -> None:
    """Belt and braces over the gates callable, and the thing gate G6 is checkable against."""
    refusal = live_refusal(os.environ.get("DRY_RUN", "true"))
    if refusal:
        raise SystemExit(refusal)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="friday-drill",
        description="Drive a whole rebalance session through Baskfy's gateway, DRY_RUN only.",
    )
    parser.add_argument("--drill", action="store_true", help="run the drill (the default)")
    parser.add_argument("--journal", default="", help="where to write the order journal")
    parser.add_argument(
        "--mutate",
        default="",
        choices=("", "skip-a-stop", "wrong-client-id", "touch-the-broker", "live-setting"),
        help="inject one defect, to prove the matching assertion can fail. All are inert with "
        "respect to a real account: the worst of them calls the spy.",
    )
    return parser.parse_args(list(argv))


async def drill(journal_path: Path, mutate: str) -> Report:
    report = Report()
    session = await run_session(journal_path, mutate)
    journal = read_journal(journal_path)

    assert_plan(report, session)
    assert_stop_formula(report, session)
    assert_zero_broker_contact(report, session, journal)
    assert_buys_equal_stops(report, session)
    assert_stop_sizing(report, session)
    assert_stop_band(report, session)
    assert_idempotency(report, session, journal)
    assert_journal_completeness(report, session, journal)

    probes = await guard_probes(journal_path.with_name("guard-probes.jsonl"))
    assert_guards(report, probes)
    assert_unfilled_probe(
        report, await unfilled_buy_probe(journal_path.with_name("unfilled-probe.jsonl"))
    )
    assert_expiry(report, expiry_enforcement_sites())
    assert_fail_closed(report)
    assert_no_live_setting(report, mutate)
    assert_no_desk_import(report)

    report.note(
        "G4: the gateway journals every simulated action but writes no client_id field on the "
        "line (gateway.py dry_run / gtt_dry_run / placed / gtt_placed branches). The id is "
        "honoured by the idempotency maps and surfaces in an order's DRY-<client_id>, but a "
        "journal cannot be reconciled to a plan by it. Owner: packages/execution (leaf 1.2.1)."
    )
    report.note(
        "G5: no code under packages/ or services/ refuses a stale plan. curated_plans.PLAN_TTL "
        "stamps expires_at_hint and nothing ever reads it back. The confirm/plan_id/expiry gate "
        "lives only at kite-momentum-rebalancer/app/main.py:519-524 and dies with the desk."
    )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    refuse_unless_dry_run()
    args = parse_args(sys.argv[1:] if argv is None else argv)
    tmp = Path(tempfile.mkdtemp(prefix="baskfy-friday-drill-"))
    journal_path = Path(args.journal) if args.journal else tmp / "orders_journal.jsonl"
    journal_path.parent.mkdir(parents=True, exist_ok=True)

    print("== BASKFY FRIDAY DRILL ==")
    print(
        f"DRY_RUN={os.environ.get('DRY_RUN', '(unset -> treated as true)')}  "
        f"gates=ProductGates(dry_run=True, intraday_enabled=False, options_enabled=False)"
    )
    print(f"journal: {journal_path}")
    if args.mutate:
        print(f"MUTATION: {args.mutate} — this run is expected to fail an assertion")

    report = asyncio.run(drill(journal_path, args.mutate))
    print(report.render())

    failed = report.failed
    total = len(report.checks)
    open_gaps = [g for g in report.gaps if not g.ok]
    print("")
    for check in failed:
        print(f"drill: FAILED {check.name} - {check.detail}")
    verdict = "PASS" if not failed else "FAIL"
    print(
        f"drill: gaps open (enforcement absent, gate abandoned): "
        f"{', '.join(g.name.split()[0] for g in open_gaps) or 'none'}"
    )
    print(f"drill: {verdict} - {total - len(failed)} of {total} checks passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
