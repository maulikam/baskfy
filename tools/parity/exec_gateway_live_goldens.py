"""Goldens for the gateway's LIVE transport (lane exec_gateway, wave D2).

``desk_diff.py`` runs both desks with ``DRY_RUN=true`` (a safety rail), so the journal records
only the live path writes — ``placed``, ``gtt_placed``, ``gtt_deleted``, ``gtt_modified``,
``order_cancelled``, the broker-error records — are never graded there. This script runs the
PYTHON gateway (``baskfy_execution.OrderGateway``) with ``dry_run=False`` against the harness's
replay Kite (``desk_fake_kite.ReplayKiteConnect``: kiteconnect 5.2.1 shaping every request, the
request answered from a fixture and recorded, **never sent**), scenario by scenario, and writes
what it saw:

* every operation's result dict (or the exception it raised),
* every journal line (``ts`` dropped: it is ``time.time()``),
* the replay Kite's call log, line for line (the format ``desk_diff.py`` compares).

``rust/crates/baskfy-desk/tests/exec_gateway_live.rs`` replays the same operations through the
Rust gateway over ``fake_kite.rs`` and requires all three to be equal. No network: the replay
Kite never opens a socket, and no credential exists here.

    PYTHONPATH=<worktree>/decile-blueprint/packages/execution/src:<worktree>/decile-blueprint/packages/core/src:<worktree>/tools/parity \\
      /Volumes/T7-Work/Dev/projects/baskfy/decile-blueprint/.venv/bin/python tools/parity/exec_gateway_live_goldens.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import desk_fake_kite
from baskfy_core.fno.covered import OptionPosition, uncovered
from baskfy_core.options.config import OptionType
from baskfy_execution import OrderGateway, ProductGates, RiskManager, TenantIds
from baskfy_execution.gtt import StopBand
from baskfy_execution.guards import FoPlanRef

HERE = Path(__file__).resolve().parent
OUT = HERE.parents[1] / "rust" / "crates" / "baskfy-desk" / "fixtures" / "exec_gateway" / "live_goldens.json"

HEADER = (
    "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,"
    "lot_size,instrument_type,segment,exchange\n"
)
NSE_CSV = HEADER + (
    "738561,2885,RELIANCE,RELIANCE,0,,0,0.05,1,EQ,NSE,NSE\n"
    "2748929,10738,OFSS,OFSS,0,,0,1.0,1,EQ,NSE,NSE\n"
    "1,1,SONACOMS,SONACOMS,0,,0,0.1,1,EQ,NSE,NSE\n"
    "2,2,NOTICK,NOTICK,0,,0,0,1,EQ,NSE,NSE\n"
)
NFO_CSV = HEADER + "11,11,RELIANCE26NOVFUT,RELIANCE,0,2026-11-24,0,0.1,250,FUT,NFO-FUT,NFO\n"

T1 = [1, 1]
T2 = [2, 1]


def err(kind: str, route: str) -> dict[str, Any]:
    return {"error": {"type": kind, "message": f"broker refused {route}", "code": 400}}


def leg(kind: str, strike: int, qty: int) -> dict[str, Any]:
    return {"underlying": "NIFTY", "expiry": "2026-11-24", "option_type": kind,
            "strike": str(strike), "quantity": qty}


def buy(symbol: str = "RELIANCE", **kw: Any) -> dict[str, Any]:
    args = {"symbol": symbol, "qty": 10, "side": "BUY", "product": "CNC", "order_type": "LIMIT",
            "price": 100.0, "exchange": "NSE", "client_id": f"PLAN1:{symbol}",
            "gross_exposure": 1000.0, "tenant": T1, "plan_tenant": T1}
    args.update(kw)
    return {"op": "place", "args": args}


def arm(symbol: str = "RELIANCE", **kw: Any) -> dict[str, Any]:
    args = {"symbol": symbol, "qty": 10, "trigger": 89.0, "last_price": 100.0,
            "tenant": T1, "plan_tenant": T1}
    args.update(kw)
    return {"op": "gtt", "args": args}


def delete(gtt_id: Any = 4242, **kw: Any) -> dict[str, Any]:
    args = {"gtt_id": gtt_id, "symbol": "RELIANCE", "tenant": T1, "plan_tenant": T1}
    args.update(kw)
    return {"op": "delete", "args": args}


def modify(**kw: Any) -> dict[str, Any]:
    # A modify with no client_id mints `uuid4().hex[:10]`; the goldens name one so the two
    # gateways' journals can be compared line for line.
    args = {"gtt_id": 4242, "symbol": "RELIANCE", "qty": 7, "trigger": 89.0, "last_price": 100.0,
            "client_id": "RESIZE:RELIANCE", "tenant": T1, "plan_tenant": T1}
    args.update(kw)
    return {"op": "modify", "args": args}


def cancel(order_id: str = "REPLAY000001", **kw: Any) -> dict[str, Any]:
    args = {"order_id": order_id, "symbol": "RELIANCE", "tenant": T1, "plan_tenant": T1}
    args.update(kw)
    return {"op": "cancel", "args": args}


LIVE = {"dry_run": False, "intraday_enabled": False, "options_enabled": False,
        "fno_carry_enabled": False}
FO = {**LIVE, "intraday_enabled": True, "options_enabled": True, "fno_carry_enabled": True}

SCENARIOS: list[dict[str, Any]] = [
    {
        "name": "the weekly book's live day: buy, replay, sell, arm, re-size, cancel, remove",
        "gates": LIVE, "fail": {},
        "ops": [
            buy(),
            buy(),  # DUPLICATE: the idempotency map, not a second order
            buy("OFSS", side="SELL", price=2749.95, client_id="PLAN1:OFSS", qty=3,
                gross_exposure=8249.85),
            buy("SONACOMS", price=612.37, client_id="PLAN1:SONACOMS", tick_size=0.1),
            arm(client_id="PLAN1:RELIANCE"),  # its own namespace: not a DUPLICATE of the buy
            arm(client_id="PLAN1:RELIANCE"),  # this one is
            arm("OFSS", qty=3, trigger=2515.1, last_price=2750.0, client_id="PLAN1:OFSS",
                limit_fraction=0.97),
            arm("NOTICK", trigger=89.03, client_id="PLAN1:NOTICK"),
            modify(qty=7),
            modify(qty=7),  # a re-size is not idempotent by design
            cancel("REPLAY000001"),
            delete(900001, client_id="PLAN1:RELIANCE"),
        ],
    },
    {
        "name": "the band, the snap and a stop that is not a stop",
        "gates": LIVE, "fail": {},
        "ops": [
            arm(trigger=99.5, client_id="close"),
            arm(trigger=80.0, client_id="far"),
            arm(trigger=92.0, client_id="min"),
            arm(trigger=88.0, client_id="max"),
            arm(trigger=101.0, client_id="above"),
            arm("OFSS", qty=1, trigger=2749.6, last_price=2750.0, client_id="snaps-up"),
            arm(qty=0, client_id="zero"),
            modify(qty=7, trigger=100.5),
        ],
    },
    {
        "name": "a swing book's band, injected",
        "gates": LIVE, "fail": {}, "stop_band": [0.005, 0.10],
        "ops": [arm(trigger=98.0, client_id="tight"), arm(trigger=85.0, client_id="wide")],
    },
    {
        "name": "the broker refuses every write, definitively",
        "gates": LIVE,
        "fail": {"order.place": err("InputException", "order.place"),
                 "gtt.place": err("InputException", "gtt.place"),
                 "gtt.modify": err("InputException", "gtt.modify"),
                 "gtt.delete": err("InputException", "gtt.delete"),
                 "order.cancel": err("OrderException", "order.cancel")},
        "ops": [buy(), buy(), arm(client_id="A"), arm(client_id="A"), modify(), delete(),
                cancel()],
    },
    {
        "name": "the broker fails in a way that says nothing about the exchange",
        "gates": LIVE,
        "fail": {"order.place": err("NetworkException", "order.place"),
                 "market.instruments": err("DataException", "market.instruments")},
        "ops": [buy(), buy(), arm(client_id="B")],
    },
    {
        "name": "risk: a market order valued by its reference, an unpriced one, a big one, the kill switch",
        "gates": LIVE, "fail": {},
        "ops": [
            buy(order_type="MARKET", price=None, reference_price=100.0, market_protection=2.0,
                client_id="M:RELIANCE"),
            buy(order_type="MARKET", price=None, client_id="M2:RELIANCE"),
            buy(qty=100000, price=5000.0, gross_exposure=5.0e8, client_id="BIG:RELIANCE"),
            {"op": "kill"},
            buy(client_id="K:RELIANCE"),
            arm(client_id="K"),
            modify(),
            delete(),
            cancel(),
        ],
    },
    {
        "name": "untouchables, tenancy and the product gates, with the broker watching",
        "gates": LIVE, "fail": {},
        "ops": [
            buy("SGBJUN29", client_id="P:SGBJUN29"),
            buy("SOMEBOND", series="GB", client_id="P:SOMEBOND"),
            buy(plan_tenant=T2, client_id="X:RELIANCE"),
            buy(product="MIS", client_id="MIS:RELIANCE"),
            buy("NIFTY25SEPFUT", exchange="NFO", client_id="F:NIFTY25SEPFUT"),
            buy("NIFTY2692223700CE", exchange="NFO", product="NRML", client_id="O:CE"),
            arm("SGBDE31III-GB", client_id="S"),
            arm(series="GS", client_id="GS"),
            arm(plan_tenant=T2, client_id="T"),
            arm("NIFTY25SEPFUT", exchange="NFO", client_id="NF"),
            arm("NIFTY25SEP24000CE", exchange="NFO", client_id="NO"),
            delete(symbol=""),
            delete(symbol="SGBJUN29"),
            delete(gtt_id=0),
            delete(plan_tenant=T2),
            modify(symbol="SGBJUN29"),
            cancel(symbol="SGBJUN29"),
            cancel(plan_tenant=T2),
        ],
    },
    {
        "name": "F&O with every switch on: the F2 future, its trail, a covered condor and a naked short",
        "gates": FO, "fail": {}, "coverage": True,
        "ops": [
            buy("RELIANCE26NOVFUT", qty=250, price=1100.0, exchange="NFO", product="NRML",
                gross_exposure=0.0, client_id="F2-1:RELIANCE26NOVFUT",
                fo_plan={"plan_id": "F2-1", "sleeve": "F2"}),
            arm("RELIANCE26NOVFUT", qty=250, trigger=1000.0, last_price=1100.0, exchange="NFO",
                client_id="F2-1:RELIANCE26NOVFUT", fo_plan={"plan_id": "F2-1", "sleeve": "F2"}),
            modify(symbol="RELIANCE26NOVFUT", qty=250, trigger=1040.0, last_price=1100.0,
                   exchange="NFO", gtt_id=900001, floor_trigger=1000.0,
                   fo_plan={"plan_id": "F2-1", "sleeve": "F2"}),
            modify(symbol="RELIANCE26NOVFUT", qty=250, trigger=990.0, last_price=1100.0,
                   exchange="NFO", gtt_id=900001, floor_trigger=1040.0,
                   fo_plan={"plan_id": "F2-1", "sleeve": "F2"}),
            buy("NIFTY26NOV23500PE", qty=75, price=10.0, exchange="NFO", product="NRML",
                gross_exposure=0.0, client_id="FOPLAN-1:NIFTY26NOV23500PE",
                fo_plan={"plan_id": "FOPLAN-1", "sleeve": "F1N", "step": leg("PE", 23500, 75)}),
            buy("NIFTY26NOV24000PE", qty=75, side="SELL", price=10.0, exchange="NFO",
                product="NRML", gross_exposure=0.0, client_id="FOPLAN-1:NIFTY26NOV24000PE",
                fo_plan={"plan_id": "FOPLAN-1", "sleeve": "F1N", "step": leg("PE", 24000, -75),
                         "plan_filled": [leg("PE", 23500, 75)]}),
            buy("NIFTY26NOV26000CE", qty=75, side="SELL", price=10.0, exchange="NFO",
                product="NRML", gross_exposure=0.0, client_id="FOPLAN-1:NIFTY26NOV26000CE",
                fo_plan={"plan_id": "FOPLAN-1", "sleeve": "F1N", "step": leg("CE", 26000, -75)}),
            buy("NIFTY26NOV25000CE", qty=150, side="SELL", price=10.0, exchange="NFO",
                product="NRML", gross_exposure=0.0, client_id="FOPLAN-2:NIFTY26NOV25000CE",
                fo_plan={"plan_id": "FOPLAN-2", "sleeve": "F1N", "step": leg("CE", 25000, -75),
                         "broker_positions": [leg("CE", 25500, 75)]}),
        ],
    },
    {
        "name": "a caller bug raises instead of answering",
        "gates": LIVE, "fail": {},
        "ops": [arm(limit_fraction=1.03, client_id="bad"), modify(limit_fraction=0.0)],
    },
]


def tenant(pair: list[int] | None) -> TenantIds | None:
    return None if pair is None else TenantIds(user_id=pair[0], broker_account_id=pair[1])


def option_leg(d: dict[str, Any]) -> OptionPosition:
    return OptionPosition(d["underlying"], dt.date.fromisoformat(d["expiry"]),
                          OptionType(d["option_type"]), Decimal(d["strike"]), int(d["quantity"]))


def fo_plan(d: dict[str, Any] | None) -> FoPlanRef | None:
    if d is None:
        return None
    return FoPlanRef(
        plan_id=d["plan_id"], sleeve=d["sleeve"],
        step=option_leg(d["step"]) if d.get("step") else None,
        broker_positions=tuple(option_leg(x) for x in d.get("broker_positions", [])),
        plan_filled=tuple(option_leg(x) for x in d.get("plan_filled", [])),
    )


def kwargs_of(args: dict[str, Any]) -> dict[str, Any]:
    out = dict(args)
    for key in ("tenant", "plan_tenant"):
        if key in out:
            out[key] = tenant(out[key])
    if "fo_plan" in out:
        out["fo_plan"] = fo_plan(out["fo_plan"])
    return out


def run(scenario: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "instruments").mkdir()
        (root / "instruments" / "NSE.csv").write_text(NSE_CSV)
        (root / "instruments" / "NFO.csv").write_text(NFO_CSV)
        (root / "kite.json").write_text(json.dumps(
            {"session": {"authed": True}, "routes": scenario["fail"], "quotes": {},
             "historical": {}}))
        log = root / "calls.jsonl"
        desk_fake_kite.install(str(root), str(log))
        kc = desk_fake_kite.ReplayKiteConnect(api_key="test-api-key")
        risk = RiskManager()
        gates = ProductGates(**scenario["gates"])
        band = scenario.get("stop_band")
        gw = OrderGateway(
            kc, risk, gates=lambda: gates, journal_path=str(root / "journal.jsonl"),
            stop_band=StopBand(*band) if band else None,
            coverage=uncovered if scenario.get("coverage") else None,
        )
        results: list[Any] = []
        for op in scenario["ops"]:
            name = op["op"]
            if name == "kill":
                risk.on_pnl(-500_000.0)
                results.append({"killed": risk.state.killed})
                continue
            method = {"place": gw.place, "gtt": gw.place_gtt_stop, "delete": gw.delete_gtt,
                      "modify": gw.modify_gtt_quantity, "cancel": gw.cancel_order}[name]
            try:
                out = asyncio.run(method(**kwargs_of(op["args"])))
                results.append(json.loads(json.dumps(out, default=str)))
            except Exception as exc:  # noqa: BLE001 - a raise is an answer, recorded as one
                results.append({"raised": [type(exc).__name__, str(exc)]})
        journal = []
        path = root / "journal.jsonl"
        if path.exists():
            for line in path.read_text().splitlines():
                rec = json.loads(line)
                rec.pop("ts", None)
                journal.append(rec)
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return {**scenario, "results": results, "journal": journal, "calls": calls}


def main() -> int:
    out = {
        "note": "Written by tools/parity/exec_gateway_live_goldens.py: the Python gateway, "
                "dry_run=False, over the replay Kite. Do not edit by hand.",
        "nse_csv": NSE_CSV,
        "nfo_csv": NFO_CSV,
        "scenarios": [run(s) for s in SCENARIOS],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(f"{OUT}: {len(SCENARIOS)} scenarios, "
          f"{sum(len(s['ops']) for s in SCENARIOS)} operations", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
