#!/usr/bin/env python
"""Paper-only runner: intraday defined-risk option selling.

NO CODE PATH IN THIS COMMAND PLACES, MODIFIES OR CANCELS AN ORDER. It reads the live
chain, prices a hypothetical position at executable quotes, and records it. The word
"open" below means "write a row"; nothing reaches the broker.

The overnight arm was dropped on 16 Aug 2026: the system no longer holds an option past
the close (core/guards.py), so that arm could never have been executed. What remains is a
one-armed measurement — does intraday short premium cover its own costs — which is a test
against zero rather than a comparison.

THE PROTOCOL

    # once, before any data is collected
    python -m scripts.options_ab register

    python -m scripts.options_ab open                        # ~09:45
    python -m scripts.options_ab watch                       # any time, records touches
    python -m scripts.options_ab close --reason square_off   # 15:12

    python -m scripts.options_ab report
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

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
# Two, not four: the overnight pair was removed with the arm. They were registered but
# never ran, so they consumed no look at the data and must not inflate N.
PREREGISTERED = [
    X.Variant("wing05", X.INTRADAY, {"short_delta": 0.16, "wing_delta": 0.05},
              note="control: the current specification"),
    X.Variant("wing08", X.INTRADAY, {"short_delta": 0.16, "wing_delta": 0.08},
              note="wing variant, preregistered rather than chosen after the fact"),
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

    expiry, lots, lot_size = plan.expiry, plan.lots, plan.lot_size
    max_loss = plan.max_loss

    # MIS, always: it is the only product this system will put an option under, and the
    # margin has to be estimated under the product the position would actually use.
    #
    # No expiry-day ELM is added by hand. That 2% of contract value per short leg is real
    # and lands on any short option position on expiry day, intraday ones included — but a
    # basket estimate requested ON that day already reflects the day's own requirement.
    # The manual addition existed only because an overnight arm was margined the evening
    # BEFORE the ELM applied, which the endpoint could not know. Worth spot-checking
    # against a live Tuesday basket the first time this runs on an expiry day.
    product = "MIS"
    margin, margin_source = None, ""
    if not args.no_margin:
        try:
            from app.strategies.options_market import basket_margin_for_legs
            est = basket_margin_for_legs(
                kite.kc,
                [{"tradingsymbol": f.label, "transaction_type": f.side,
                  "quantity": f.quantity, "price": f.price} for f in fills],
                product=product)
            margin = float(est.get("total") or est.get("initial") or 0.0) or None
            margin_source = f"kite_basket:{product}"
        except Exception as exc:
            margin_source = f"unavailable: {exc}"

    variant_id = X.Variant(args.name, X.INTRADAY,
                           {"short_delta": cfg.short_delta,
                            "wing_delta": cfg.wing_delta}).variant_id
    now = dt.datetime.now()
    with db.connect() as conn:
        db.migrate(conn)
        arm_id = X.open_arm(
            conn, variant_id=variant_id, strategy="seller", arm=X.INTRADAY,
            expiry=expiry, lots=lots, lot_size=lot_size,
            entry_fills=fills, entry_at=now, entry_spot=live.market.spot,
            dte_at_entry=(expiry - now.date()).days,
            max_loss=max_loss, margin=margin, margin_source=margin_source,
            note=args.note or "")
    print(json.dumps({
        "status": "OPENED", "arm_id": arm_id, "arm": X.INTRADAY, "variant": variant_id,
        "expiry": expiry.isoformat(), "dte": (expiry - now.date()).days,
        "lots": lots, "entry_credit_rs": round(sum(
            f.turnover if f.side == "SELL" else -f.turnover for f in fills), 2),
        "max_loss_rs": max_loss, "margin_rs": margin, "margin_source": margin_source,
        "product": product,
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

    results = []
    with db.connect() as conn:
        db.migrate(conn)
        for a in X.open_arms(conn):
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
                                          exit_reason=args.reason)})
    print(json.dumps({"status": "DONE", "closed": len(results), "results": results,
                      "paper_only": True, "orders_submitted": 0}, indent=2, default=str))
    return 0


def cmd_report(args) -> int:
    with db.connect() as conn:
        db.migrate(conn)
        rep = X.report(conn)
    print(json.dumps(rep, indent=2, default=str))
    n = rep.get("closed_arms", 0)
    if n < 100:
        # stderr, so stdout stays valid JSON and the command can be piped into jq.
        print(f"\n{n} closed arms. A high-win-rate condor needs roughly 550-710 to "
              "separate a real edge from noise at 80% power after a cost drag.",
              file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="paper-only intraday option-selling record")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("register", help="preregister the variant set (do this first)")

    o = sub.add_parser("open", help="record a paper entry at executable quotes")
    o.add_argument("--name", default="wing05")
    o.add_argument("--wing-delta", type=float, default=None)
    o.add_argument("--no-margin", action="store_true")
    o.add_argument("--note", default="")

    sub.add_parser("watch", help="record short-strike breaches without closing")

    c = sub.add_parser("close", help="close open arms at executable quotes")
    c.add_argument("--reason", required=True)

    sub.add_parser("report", help="what the intraday arm has done so far")

    args = p.parse_args()
    return {"register": cmd_register, "open": cmd_open, "watch": cmd_watch,
            "close": cmd_close, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
