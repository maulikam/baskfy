#!/usr/bin/env python
"""V1 session runner — PAPER ONLY.

NO CODE PATH IN THIS COMMAND PLACES, MODIFIES OR CANCELS AN ORDER. Fills are simulated
against live depth by app/strategies/strangle/fills_paper.py, and the package contains no
order call at all. Live execution is V4 and will route through app/core/gateway.py.

    python -m scripts.strangle --check          # market facts, sizing, gates. Trades nothing.
    python -m scripts.strangle --collect        # record today's ATM straddle, then stop
    python -m scripts.strangle                  # run the paper session

WHY --collect EXISTS.
The IV gates need reference_band, and reference_band cannot be reconstructed from history:
Kite drops expired contracts, so the series that governed any past session is unlookupable
(see calibrate.py). Bands therefore have to be collected forward — but the config refuses
to trade without them, on purpose, so that paper and live are gated on the same rule set.
--collect resolves that deadlock: it records one observation per session and trades nothing,
so the bands build up while nothing is at risk. Run it daily until the report says ready.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys

from app.kite_client import Kite
from app.strategies.strangle import book as B
from app.strategies.strangle import calendar_nse as CALN
from app.strategies.strangle import calibrate as CAL
from app.strategies.strangle import clock as C
from app.strategies.strangle import config as SC
from app.strategies.strangle import fills_paper as F
from app.strategies.strangle import journal as JN
from app.strategies.strangle import levels as L
from app.strategies.strangle import live as LIVE
from app.strategies.strangle import market as MK
from app.strategies.strangle import rules as R
from app.strategies.strangle import selection as SEL
from app.strategies.strangle import session as SESS
from app.strategies.strangle import sizing as Z
from app.strategies.strangle import state as ST

FORWARD_PATH = "data/outputs/strangle_straddle_record.jsonl"
SESSION_LOCK = "data/outputs/strangle_session.lock"


def _acquire_session_lock(path: str = SESSION_LOCK) -> bool:
    """One session process at a time, across the whole machine.

    The ops lock lives inside the web process, so it cannot see a session started by
    launchd — and once this is scheduled, the button on /options and the scheduled job can
    collide. Two sessions on one day would both enter, both journal, and produce a paper
    record describing a position nobody held.

    A stale lock from a killed process is reclaimed rather than blocking forever: a crash
    at 09:31 must not cost the whole day.
    """
    import errno
    old = pathlib.Path(path)
    if old.exists():
        try:
            pid = int(old.read_text().split()[0])
            os.kill(pid, 0)
            return False                       # a live process holds it
        except (ValueError, IndexError, ProcessLookupError, PermissionError):
            pass
        except OSError as exc:
            if exc.errno != errno.ESRCH:
                return False
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text(f"{os.getpid()} {dt.datetime.now().isoformat(timespec='seconds')}\n")
    return True


def _release_session_lock(path: str = SESSION_LOCK) -> None:
    try:
        pathlib.Path(path).unlink()
    except OSError:
        pass


def _out(payload: dict) -> int:
    print(json.dumps(payload, indent=2, default=str))
    return 0 if payload.get("status") in ("OK", "COLLECTED", "SKIPPED") else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="NIFTY intraday strangle — paper only")
    ap.add_argument("--config", default=SC.DEFAULT_PATH)
    ap.add_argument("--check", action="store_true", help="report readiness, trade nothing")
    ap.add_argument("--collect", action="store_true",
                    help="record today's ATM straddle for band calibration, then stop")
    ap.add_argument("--forward", default=FORWARD_PATH)
    ap.add_argument("--entry-only", action="store_true",
                    help="enter and stop, without running the management loop (V1 behaviour)")
    ap.add_argument("--max-ticks", type=int, default=0,
                    help="stop the loop after N polls; 0 means run to the force-exit time")
    args = ap.parse_args()

    cfg = SC.load(args.config)
    today = dt.date.today()
    jr = JN.Journal(cfg["operational"]["journal_path"])

    kite = Kite()
    if not kite.is_authed():
        return _out({"status": "AUTH_REQUIRED", "login_url": kite.login_url()})

    ins = cfg["instrument"]
    instruments = kite.kc.instruments(ins["exchange"])
    # Derived, not hardcoded: past holidays come from index history, future ones from
    # expiries that shifted off a Tuesday. See calendar_nse.py for what it cannot see.
    cal = CALN.build_from_kite(kite.kc, name=ins["name"],
                               extra=cfg["session"].get("extra_holidays") or (),
                               today=today)
    try:
        facts = SC.assert_market_facts(cfg, instruments, today, calendar=cal)
    except SC.StartupRefused as exc:
        jr.write("startup_refused", error=str(exc))
        return _out({"status": "STARTUP_REFUSED", "error": str(exc)})

    # --- lockout, before anything else ---------------------------------------------------
    locked = ST.Session.locked_out(today, cfg["operational"]["lockout_path"])
    if locked and not (args.check or args.collect):
        return _out({"status": "LOCKED_OUT", "lockout": locked,
                     "note": "a restart must not resume trading on a day that already "
                             "hit its stop"})

    if not cal.is_trading_day(today):
        return _out({"status": "NOT_A_TRADING_DAY", "session": today.isoformat(),
                     "weekday": today.strftime("%A"),
                     "holiday": today in cal.holidays,
                     "note": "the session parameters would still compute, which is exactly "
                             "why this is checked rather than assumed"})

    session = ST.Session(today, cfg["operational"]["lockout_path"])
    expiry = C.resolve_expiry(today, sorted({i["expiry"] for i in instruments
                                             if i.get("name") == ins["name"]
                                             and i.get("expiry")}))
    try:
        params = C.session_params(today, expiry, cfg, holidays=cal.holidays)
    except C.ExpiryResolutionError as exc:
        return _out({"status": "EXPIRY_ERROR", "error": str(exc)})

    # --- chain --------------------------------------------------------------------------
    snap = MK.snapshot(kite.kc, instruments=instruments, index_key=ins["index_key"],
                       expiry=expiry, name=ins["name"])
    asp, atm_k = MK.atm_straddle(snap, int(ins["strike_step"]))
    base = {"session": today.isoformat(), "expiry": expiry.isoformat(),
            "dte": params.dte, "bucket": params.bucket,
            "target_points": params.target_points, "stop_points": params.stop_points,
            "size_mult": params.size_mult, "spot": snap.spot, "atm_strike": atm_k,
            "asp": None if asp is None else round(asp, 2),
            "chain_rows": len(snap.rows), "stale_seconds": round(snap.stale_seconds, 1),
            "paper_only": True, "orders_submitted": 0}

    # --- collect mode -------------------------------------------------------------------
    if args.collect:
        if asp is None:
            return _out({**base, "status": "NO_ASP",
                         "note": "no two-sided ATM quote — nothing to record"})
        ce = pe = 0.0
        for r in snap.rows:
            if abs(r["strike"] - atm_k) < 1e-6 and r["bid"] > 0 and r["ask"] > 0:
                mid = (r["bid"] + r["ask"]) / 2.0
                ce, pe = (mid, pe) if r["kind"] == "CE" else (ce, mid)
        obs = CAL.Observation(session=today, expiry=expiry, dte=params.dte,
                              bucket=params.bucket, spot=snap.spot, strike=atm_k,
                              call=ce, put=pe)
        CAL.record_forward(args.forward, obs)
        record = CAL.load_forward(args.forward)
        bands = CAL.build_bands(record) if record else {}
        jr.write("collected", **obs.as_dict())
        return _out({**base, "status": "COLLECTED", "recorded": obs.as_dict(),
                     "forward_sessions": len(record),
                     "readiness": CAL.readiness(bands) if bands else
                     {"ready": False, "note": "no observations yet"}})

    # --- gates --------------------------------------------------------------------------
    band = {**(cfg.get("reference_band") or {})}
    forward = CAL.load_forward(args.forward)
    if forward and not band:
        band = CAL.build_bands(forward)
    # There is exactly one authenticated broker session in this system today. Operational
    # rule R1 wants a second, funded to buy back the whole position if the primary dies.
    # Reported as false rather than quietly waived: the gate is what makes the gap visible.
    secondary_ok = False
    rsnap = R.Snapshot(now=dt.datetime.now(), spot=snap.spot, asp=asp, marks={},
                       feed_age_seconds=snap.stale_seconds,
                       secondary_broker_ok=secondary_ok)
    vetoes = R.day_vetoes(rsnap, cfg, reference_band=band, dte_bucket=params.bucket)
    if not params.tradeable:
        vetoes.insert(0, params.reason)

    if args.check:
        return _out({**base, "status": "OK", "market_facts": facts,
                     "vetoes": vetoes, "would_trade": not vetoes,
                     "forward_sessions": len(forward),
                     "band_source": "config" if cfg.get("reference_band") else
                                    ("forward_record" if forward else "none")})

    session.to(ST.State.GATED, "gates evaluated")
    if vetoes:
        session.to(ST.State.SKIPPED, "; ".join(vetoes[:3]))
        jr.write("skipped", vetoes=vetoes, **base)
        return _out({**base, "status": "SKIPPED", "vetoes": vetoes})

    # --- selection ----------------------------------------------------------------------
    session.to(ST.State.WAITING_ENTRY, "gates clear")
    hist = kite.kc.historical_data(
        256265, today - dt.timedelta(days=int(cfg["levels"]["swing_lookback_days"]) * 2),
        today, "60minute")
    bars = [L.Bar(ts=r["date"], open=float(r["open"]), high=float(r["high"]),
                  low=float(r["low"]), close=float(r["close"])) for r in hist]
    lv = L.compute_levels(bars, snap.spot, cfg)
    oi = L.oi_structure(snap.as_rows(), snap.spot)
    pr = L.priced_range(snap.spot, asp, cfg)

    try:
        pair = SEL.select_pair(snap.as_rows(), pr=pr, levels=lv, oi=oi, cfg=cfg,
                               step=int(ins["strike_step"]))
        pair = SEL.attach_wings(pair, snap.as_rows(), cfg)
    except SEL.NoQualifyingPair as exc:
        session.to(ST.State.NO_ENTRY, str(exc))
        jr.write("no_entry", error=str(exc), **base)
        return _out({**base, "status": "NO_ENTRY", "reason": str(exc),
                     "levels": {"resistance": lv.resistance, "support": lv.support},
                     "priced_range": pr.as_dict()})

    # --- entry-cost gate --------------------------------------------------------------
    # Before sizing and before any fill: if the stop cannot survive the friction of getting
    # in and out, the session has no room for the trade to be right.
    probe_legs = [{"symbol": pair.call.symbol, "side": "SELL"},
                  {"symbol": pair.put.symbol, "side": "SELL"}]
    if pair.call_wing and pair.put_wing:
        probe_legs += [{"symbol": pair.call_wing.symbol, "side": "BUY"},
                       {"symbol": pair.put_wing.symbol, "side": "BUY"}]
    probe_units = Z.session_lots(cfg, params.size_mult) * int(ins["lot_size"])
    try:
        entry_cost = B.estimate_entry_cost(probe_legs, snap.as_rows(), cfg,
                                           units=probe_units,
                                           lot_size=int(ins["lot_size"]))
    except F.DepthUnavailable as exc:
        session.to(ST.State.NO_ENTRY, str(exc))
        jr.write("depth_unavailable", error=str(exc), **base)
        return _out({**base, "status": "NO_DEPTH", "reason": str(exc)})

    ok, why = R.entry_cost_gate(params.stop_points, entry_cost["total_points"], cfg)
    if not ok:
        session.to(ST.State.NO_ENTRY, why)
        jr.write("entry_cost_veto", entry_cost=entry_cost, reason=why, **base)
        return _out({**base, "status": "ENTRY_COST_VETO", "reason": why,
                     "entry_cost": entry_cost,
                     "pair": pair.as_dict()})

    # --- sizing, from queried margin --------------------------------------------------
    lots = Z.session_lots(cfg, params.size_mult)
    legs = [(pair.call, "SELL"), (pair.put, "SELL")]
    if pair.call_wing and pair.put_wing:
        legs += [(pair.call_wing, "BUY"), (pair.put_wing, "BUY")]
    qty = lots * int(ins["lot_size"])
    basket = [{"exchange": ins["exchange"], "tradingsymbol": c.symbol,
               "transaction_type": side, "variety": "regular", "product": "MIS",
               "order_type": "MARKET", "quantity": qty} for c, side in legs]
    try:
        margin = Z.query_margin(kite.kc, basket)
        sized = Z.evaluate(cfg, lots=lots, margin_required=margin)
    except Z.SizingRefused as exc:
        session.to(ST.State.NO_ENTRY, str(exc))
        jr.write("sizing_refused", error=str(exc), **base)
        return _out({**base, "status": "SIZING_REFUSED", "reason": str(exc)})

    # --- simulated entry ----------------------------------------------------------------
    by_symbol = {r["symbol"]: r for r in snap.rows}
    try:
        fills = F.simulate_basket(
            [{"symbol": c.symbol, "side": side, "quantity": qty,
              "depth": by_symbol[c.symbol]["depth"]} for c, side in legs], cfg)
    except F.DepthUnavailable as exc:
        session.to(ST.State.NO_ENTRY, str(exc))
        jr.write("depth_unavailable", error=str(exc), **base)
        return _out({**base, "status": "NO_DEPTH", "reason": str(exc)})

    bk = B.Book(units_at_entry=qty, stop_points=params.stop_points,
                target_points=params.target_points)
    credit = 0.0
    for (c, side), f in zip(legs, fills):
        role = ("short_call" if (side == "SELL" and c.kind == "CE") else
                "short_put" if side == "SELL" else
                "wing_call" if c.kind == "CE" else "wing_put")
        bk.add(B.Leg(symbol=c.symbol, kind=c.kind, strike=c.strike, side=side,
                     quantity=qty, entry_price=f.avg_price, role=role,
                     opened_at=dt.datetime.now()))
        credit += f.avg_price * qty * (1 if side == "SELL" else -1)
    bk.entry_credit = credit
    bk.accrued_costs = B.cost_of(
        [{"side": s, "price": f.avg_price, "quantity": qty} for (_c, s), f in zip(legs, fills)],
        int(ins["lot_size"]))

    marks = {c.symbol: (by_symbol[c.symbol]["ask"] if side == "SELL"
                        else by_symbol[c.symbol]["bid"]) for c, side in legs}
    entry_pnl_pts = bk.pnl_points(marks)
    headroom = R.entry_cost_headroom(entry_pnl_pts, params.stop_points)
    headroom["estimated"] = entry_cost

    session.to(ST.State.MANAGING, "entered")
    entered = {"pair": pair.as_dict(), "sizing": sized.as_dict(),
               "entry_credit": round(credit),
               "entry_mark_points": round(entry_pnl_pts, 3),
               "entry_cost_headroom": headroom,
               "levels": {"resistance": lv.resistance, "support": lv.support},
               "priced_range": pr.as_dict()}
    jr.write("entered", fills=[f.as_dict() for f in fills], **entered, **base)

    if args.entry_only:
        return _out({**base, **entered, "status": "OK", "state": session.state.value,
                     "note": "entered and stopped; the management loop was not run"})

    # --- the management loop --------------------------------------------------------
    if not _acquire_session_lock():
        return _out({**base, **entered, "status": "ALREADY_RUNNING",
                     "note": "another strangle session process is live; refusing to run a "
                             "second. The scheduled job and the /options button cannot see "
                             "each other's in-process locks, only this file."})
    # Exits are evaluated before adjustments on every poll. The provider is a generator so
    # the same loop can be driven from a scripted sequence in the tests.
    originals = list(bk.legs)
    force = dt.time(*(int(x) for x in cfg["timing"]["force_exit"].split(":")))
    frames = MK.live_provider(kite.kc, instruments=instruments,
                              index_key=ins["index_key"], expiry=expiry,
                              name=ins["name"],
                              poll_seconds=float(cfg["management"]["poll_seconds"]),
                              until=force)
    if args.max_ticks:
        frames = _capped(frames, args.max_ticks)

    try:
        result = SESS.run_session(book=bk, provider=frames, cfg=cfg, levels=lv, oi=oi,
                                  journal=jr, lot_size=int(ins["lot_size"]),
                                  step=int(ins["strike_step"]), original_legs=originals,
                                  kill_switch=LIVE.KillSwitch.from_config(cfg))
    finally:
        _release_session_lock()

    if result["flat"]:
        session.to(ST.State.EXITING, result.get("exit_reason") or "flat")
        if result.get("ends_day"):
            session.to(ST.State.LOCKED_OUT, result.get("exit_code") or "flat")

    return _out({**base, **entered, "status": result["status"],
                 "state": session.state.value, "session": result})


def _capped(frames, n: int):
    for i, f in enumerate(frames):
        if i >= n:
            return
        yield f


if __name__ == "__main__":
    raise SystemExit(main())
