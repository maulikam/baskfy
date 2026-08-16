#!/usr/bin/env python
"""Paper-only A/B runner: intraday versus overnight defined-risk option selling.

NO CODE PATH IN THIS COMMAND PLACES, MODIFIES OR CANCELS AN ORDER. It reads the live
chain, prices a hypothetical position at executable quotes, and records it. The word
"open" below means "write a row"; nothing reaches the broker.

THE PROTOCOL

    # once, before any data is collected
    python -m scripts.options_ab register

    # intraday arm
    python -m scripts.options_ab open  --arm intraday   # ~09:45
    python -m scripts.options_ab watch                  # any time, records touches
    python -m scripts.options_ab close --reason square_off   # 15:12

    # overnight arm
    python -m scripts.options_ab open  --arm overnight  # ~15:15
    python -m scripts.options_ab close --reason scheduled    # 09:20 next session

    python -m scripts.options_ab report

Both arms take the SAME strikes on the same day where the chain allows it, so the only
difference between them is the clock. That is the entire point: any other difference
would confound the one comparison this experiment exists to make.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json

from app import config as C
from app.analytics import db
from app.kite_client import Kite
from app.strategies import options_experiment as X
from app.strategies.options import IronCondorPlanner, OptionPlanRejected
from app.strategies.options_market import LiveSnapshotUnavailable, build_live_snapshot

# The preregistered set. Adding to this list AFTER collection begins invalidates the
# deflated Sharpe for everything already collected — with N trials on a worthless
# strategy the expected best Sharpe is ~1.19 at N=5 and ~1.58 at N=10, so the count is
# part of the result. Four is a deliberate ceiling.
PREREGISTERED = [
    X.Variant("wing05", X.INTRADAY, {"short_delta": 0.16, "wing_delta": 0.05},
              note="control: the current specification"),
    X.Variant("wing05", X.OVERNIGHT, {"short_delta": 0.16, "wing_delta": 0.05},
              note="same structure, held across the close"),
    X.Variant("wing08", X.INTRADAY, {"short_delta": 0.16, "wing_delta": 0.08},
              note="wing variant, preregistered rather than chosen after the fact"),
    X.Variant("wing08", X.OVERNIGHT, {"short_delta": 0.16, "wing_delta": 0.08}),
]


def _kite_or_exit() -> Kite:
    k = Kite()
    if not k.is_authed():
        print(json.dumps({"status": "AUTH_REQUIRED", "login_url": k.login_url()}, indent=2))
        raise SystemExit(2)
    return k


def _leg_quotes(chain, symbols):
    """bid/ask per symbol, from the live chain. Missing symbols are simply absent, and
    the caller refuses rather than guessing."""
    out = {}
    for q in chain:
        if q.instrument.symbol in symbols and q.bid > 0 and q.ask > 0:
            out[q.instrument.symbol] = (q.bid, q.ask)
    return out


def cmd_register(args) -> int:
    with db.connect() as conn:
        db.migrate(conn)
        for v in PREREGISTERED:
            X.register(conn, v)
        rows = X.variants(conn)
    print(json.dumps({"registered": len(rows), "trials": len(rows),
                      "variants": [{"id": r["variant_id"], "arm": r["arm"],
                                    "spec": json.loads(r["spec_json"]),
                                    "at": r["registered_at"]} for r in rows]}, indent=2))
    print("\nExpected best Sharpe from N trials on a WORTHLESS strategy: "
          "N=5 -> 1.19, N=10 -> 1.58 (Bailey/Borwein/Lopez de Prado/Zhu). "
          f"You have registered {len(rows)}.")
    return 0


def cmd_open(args) -> int:
    kite = _kite_or_exit()
    cfg = C.option_selling_config()
    if args.wing_delta is not None:
        from dataclasses import replace
        cfg = replace(cfg, wing_delta=args.wing_delta)
        cfg.validate()

    try:
        live = build_live_snapshot(kite, cfg)
    except LiveSnapshotUnavailable as exc:
        print(json.dumps({"status": "DATA_UNAVAILABLE", "error": str(exc)}, indent=2))
        return 2
    try:
        plan = IronCondorPlanner(cfg).plan(live.market)
    except OptionPlanRejected as exc:
        print(json.dumps({"status": "NO_TRADE", "code": exc.code,
                          "reason": exc.reason}, indent=2))
        return 1

    symbols = {leg.instrument.symbol for leg in plan.entry_legs}
    quotes = _leg_quotes(live.market.quotes, symbols)
    missing = sorted(symbols - set(quotes))
    if missing:
        print(json.dumps({"status": "NO_QUOTE", "legs": missing,
                          "note": "a leg without a two-sided quote is not priceable"},
                         indent=2))
        return 2

    fills = []
    for leg in plan.entry_legs:
        bid, ask = quotes[leg.instrument.symbol]
        fills.append(X.executable_fill(leg.side, bid, ask, leg.quantity,
                                       label=leg.instrument.symbol))

    margin, source = None, ""
    if not args.no_margin:
        try:
            from app.strategies.options_market import basket_margin_estimate
            est = basket_margin_estimate(kite.kc, plan)
            margin = float(est.get("total") or est.get("initial") or 0.0) or None
            source = "kite_basket"
        except Exception as exc:
            source = f"unavailable: {exc}"

    variant_id = X.Variant(args.name, args.arm,
                           {"short_delta": cfg.short_delta,
                            "wing_delta": cfg.wing_delta}).variant_id
    now = dt.datetime.now()
    with db.connect() as conn:
        db.migrate(conn)
        arm_id = X.open_arm(
            conn, variant_id=variant_id, strategy="seller", arm=args.arm,
            expiry=plan.expiry, lots=plan.lots, lot_size=plan.lot_size,
            entry_fills=fills, entry_at=now, entry_spot=live.market.spot,
            dte_at_entry=(plan.expiry - now.date()).days,
            max_loss=plan.max_loss, margin=margin, margin_source=source,
            note=args.note or "")
    print(json.dumps({
        "status": "OPENED", "arm_id": arm_id, "arm": args.arm, "variant": variant_id,
        "expiry": plan.expiry.isoformat(), "dte": (plan.expiry - now.date()).days,
        "lots": plan.lots, "entry_credit_rs": round(sum(
            f.turnover if f.side == "SELL" else -f.turnover for f in fills), 2),
        "max_loss_rs": plan.max_loss, "margin_rs": margin, "margin_source": source,
        "legs": [{"symbol": f.label, "side": f.side, "fill": f.price,
                  "bid": f.bid, "ask": f.ask} for f in fills],
        "paper_only": True, "orders_submitted": 0}, indent=2, default=str))
    return 0


def cmd_watch(args) -> int:
    """Record whether an exit CONDITION has been met, without closing anything."""
    kite = _kite_or_exit()
    cfg = C.option_selling_config()
    try:
        live = build_live_snapshot(kite, cfg)
    except LiveSnapshotUnavailable as exc:
        print(json.dumps({"status": "DATA_UNAVAILABLE", "error": str(exc)}, indent=2))
        return 2

    spot = live.market.spot
    out = []
    with db.connect() as conn:
        db.migrate(conn)
        for a in X.open_arms(conn):
            legs = json.loads(a["entry_fills_json"])
            strikes = [int(x["label"][-7:-2]) for x in legs
                       if x["side"] == "SELL" and x["label"][-7:-2].isdigit()]
            breach = bool(strikes) and (spot >= max(strikes) or spot <= min(strikes))
            if breach:
                X.observe(conn, a["arm_id"], breach=True)
            out.append({"arm_id": a["arm_id"], "arm": a["arm"], "spot": spot,
                        "short_strikes": sorted(strikes), "breach": breach})
    print(json.dumps({"status": "WATCHED", "spot": spot, "arms": out}, indent=2))
    return 0


def cmd_close(args) -> int:
    kite = _kite_or_exit()
    cfg = C.option_selling_config()
    try:
        live = build_live_snapshot(kite, cfg)
    except LiveSnapshotUnavailable as exc:
        print(json.dumps({"status": "DATA_UNAVAILABLE", "error": str(exc)}, indent=2))
        return 2

    holidays = [dt.date.fromisoformat(d) for d in (args.holiday or [])]
    results = []
    with db.connect() as conn:
        db.migrate(conn)
        arms = X.open_arms(conn)
        if args.arm:
            arms = [a for a in arms if a["arm"] == args.arm]
        for a in arms:
            entry = [X._fill_from_json(x) for x in json.loads(a["entry_fills_json"])]
            quotes = _leg_quotes(live.market.quotes, {f.label for f in entry})
            try:
                exits = X.reverse(entry, quotes)
            except X.ExperimentError as exc:
                results.append({"arm_id": a["arm_id"], "status": "NO_QUOTE",
                                "error": str(exc)})
                continue
            results.append({"arm_id": a["arm_id"], "status": "CLOSED",
                            **X.close_arm(conn, a["arm_id"], exit_fills=exits,
                                          exit_at=dt.datetime.now(),
                                          exit_spot=live.market.spot,
                                          exit_reason=args.reason,
                                          event_night=args.event,
                                          holidays=holidays)})
    print(json.dumps({"status": "DONE", "closed": len(results), "results": results,
                      "paper_only": True, "orders_submitted": 0}, indent=2, default=str))
    return 0


def cmd_report(args) -> int:
    with db.connect() as conn:
        db.migrate(conn)
        rep = X.compare(conn)
    print(json.dumps(rep, indent=2, default=str))
    n = rep.get("closed_arms", 0)
    if n < 100:
        print(f"\n{n} closed arms. A high-win-rate condor needs roughly 550-710 to "
              "separate a real edge from noise at 80% power after a cost drag; the "
              "difference between arms needs its own sample on top of that.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="paper-only intraday vs overnight A/B")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("register", help="preregister the variant set (do this first)")

    o = sub.add_parser("open", help="record a paper entry at executable quotes")
    o.add_argument("--arm", choices=X.ARMS, required=True)
    o.add_argument("--name", default="wing05")
    o.add_argument("--wing-delta", type=float, default=None)
    o.add_argument("--no-margin", action="store_true")
    o.add_argument("--note", default="")

    sub.add_parser("watch", help="record short-strike breaches without closing")

    c = sub.add_parser("close", help="close open arms at executable quotes")
    c.add_argument("--reason", required=True)
    c.add_argument("--arm", choices=X.ARMS, default=None)
    c.add_argument("--event", action="store_true", help="an event fell in this window")
    c.add_argument("--holiday", action="append", metavar="YYYY-MM-DD")

    sub.add_parser("report", help="compare the arms")

    args = p.parse_args()
    return {"register": cmd_register, "open": cmd_open, "watch": cmd_watch,
            "close": cmd_close, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
