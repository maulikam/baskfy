#!/usr/bin/env python
"""Read-only live option planner and forward-data recorder.

No code path in this command places, modifies or cancels an order.  It resolves the live
NIFTY contracts, builds a signal snapshot, evaluates one strategy, optionally asks Kite
for a basket margin estimate, and can append the full observation to JSONL.

Examples:
    python -m scripts.options_research seller
    python -m scripts.options_research buyer --record data/outputs/options_forward.jsonl
    python -m scripts.options_research seller --no-margin
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from app import config as C
from app.kite_client import Kite
from app.strategies.options import (
    BreakoutBuyerPlanner,
    IronCondorPlanner,
    OptionPlanRejected,
)
from app.strategies.options_market import (
    LiveSnapshotUnavailable,
    basket_margin_estimate,
    build_live_snapshot,
)


def _append(path: str, record: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str, separators=(",", ":")) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="read-only NIFTY option plan and forward-data capture")
    parser.add_argument("strategy", choices=("seller", "buyer"))
    parser.add_argument("--record", metavar="JSONL",
                        help="append the full chain and evaluation to this file")
    parser.add_argument("--no-margin", action="store_true",
                        help="skip the read-only Kite basket margin estimate")
    args = parser.parse_args()

    try:
        kite = Kite()
    except Exception as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, indent=2))
        return 2
    if not kite.is_authed():
        print(json.dumps({"status": "AUTH_REQUIRED", "login_url": kite.login_url()},
                         indent=2))
        return 2

    if args.strategy == "seller":
        cfg = C.option_selling_config()
        planner = IronCondorPlanner(cfg)
    else:
        cfg = C.option_buying_config()
        planner = BreakoutBuyerPlanner(cfg)

    record: dict = {
        "schema": 1,
        "captured_at": dt.datetime.now().astimezone().isoformat(),
        "strategy": args.strategy,
        "safety": {"read_only": True, "paper_only": True, "orders_submitted": 0},
    }
    try:
        live = build_live_snapshot(kite, cfg)
        record["market"] = live.as_dict(include_chain=True)
    except LiveSnapshotUnavailable as exc:
        record.update(status="DATA_UNAVAILABLE", error=str(exc))
        if args.record:
            _append(args.record, record)
        print(json.dumps(record, indent=2, default=str))
        return 2

    try:
        plan = planner.plan(live.market)
    except OptionPlanRejected as exc:
        record.update(status="NO_TRADE", rejection={"code": exc.code,
                                                     "reason": exc.reason})
    else:
        record.update(status="PLAN", plan=plan.as_dict())
        if not args.no_margin:
            try:
                record["broker_basket_margin"] = basket_margin_estimate(kite.kc, plan)
            except Exception as exc:
                # A margin endpoint failure must not discard an otherwise valuable
                # forward observation.  It does mean the plan is incomplete.
                record["broker_basket_margin_error"] = str(exc)

    if args.record:
        _append(args.record, record)
    print(json.dumps(record, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
