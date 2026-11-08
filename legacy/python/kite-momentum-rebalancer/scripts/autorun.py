#!/usr/bin/env python
"""Run whatever today still needs collected. Triggered by logging in.

Both daily jobs are blocked on a Kite token that expires overnight with no refresh, so
logging in is the moment the blocker clears — and on 17-18 Aug 2026 the scheduled runs
exited 2 for want of it, losing two sessions of straddle observations and an EOD snapshot
that cannot be backfilled.

NOTHING HERE PLACES AN ORDER. It runs the same collection steps the scheduled jobs run,
which are reads and database writes.

    python -m scripts.autorun --check     # say what is outstanding, do nothing
    python -m scripts.autorun             # do it
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys

from app.analytics import autorun as AR, db
from app.kite_client import Kite
from app.strategies.strangle import calendar_nse as CALN
from app.strategies.strangle import config as SC
from app.strategies.strangle import instruments as INS

ARGV = {
    "daily": ["-m", "scripts.daily", "--quiet", "--source", "page"],
    "strangle_collect": ["-m", "scripts.strangle", "--collect"],
    "strangle_session": ["-m", "scripts.strangle"],
}


def _argv(item: dict) -> list[str]:
    """The command for one outstanding item, with its instrument if it has one.

    Still an allowlist: the op name selects a hardcoded template and the only thing
    interpolated is a slug the registry already validated.
    """
    from app.strategies.strangle import instruments as INS
    argv = [sys.executable, *ARGV[item["op"]]]
    slug = (item.get("args") or {}).get("instrument")
    if slug:
        argv += ["--instrument", INS.get(slug).slug]
    return argv


def main() -> int:
    ap = argparse.ArgumentParser(description="run today's outstanding collection")
    ap.add_argument("--check", action="store_true", help="report only")
    ap.add_argument("--timeout", type=int, default=900)
    a = ap.parse_args()

    now = dt.datetime.now()
    kite = Kite()
    if not kite.is_authed():
        print(json.dumps({"status": "AUTH_REQUIRED", "login_url": kite.login_url()},
                         indent=2))
        return 2

    # The calendar is derived, so a holiday needs no list of its own. Only the question
    # "is today a trading day" is asked here, and NSE and BSE keep the same holidays, so
    # the default underlying answers it for all three — but it is named explicitly rather
    # than left to a default, because a default that means NIFTY is how the other callers
    # ended up silently deriving the wrong calendar.
    try:
        _u = INS.get(INS.DEFAULT)
        cal = CALN.build_from_kite(
            kite.kc, name=_u.label, index_token=_u.index_token, exchange=_u.exchange,
            extra=(SC.load(_u.config)["session"].get("extra_holidays") or ()),
            today=now.date())
        trading = cal.is_trading_day(now.date())
    except Exception as exc:                                       # noqa: BLE001
        print(json.dumps({"status": "CALENDAR_UNAVAILABLE", "error": str(exc)[:200]},
                         indent=2))
        return 1

    with db.connect() as conn:
        db.migrate(conn)
        items = AR.needed(conn, now=now, is_trading_day=trading)

    head = {"session": now.date().isoformat(), "trading_day": trading,
            "outstanding": [i["op"] for i in items], "summary": AR.summary(items),
            "detail": items}
    if a.check or not items:
        print(json.dumps({**head, "status": "CHECKED" if items else "NOTHING_DUE"},
                         indent=2, default=str))
        return 0

    ran = []
    for item in items:
        argv = _argv(item)

        # A session runs until 15:10. Waiting for it would hold the operations lock for
        # five hours and block the equity daily job behind it, so it is DETACHED and left
        # to its own PID lock. Everything else is short and is waited for.
        if item.get("detached"):
            try:
                proc = subprocess.Popen(argv, start_new_session=True,
                                        stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
                ran.append({"op": item["op"], "label": item["label"],
                            "instrument": item.get("instrument"), "pid": proc.pid,
                            "ok": True, "detached": True,
                            "tail": f"started as pid {proc.pid}; it manages itself and "
                                    "logs to the strangle journal"})
            except Exception as exc:                               # noqa: BLE001
                ran.append({"op": item["op"], "label": item["label"], "ok": False,
                            "detached": True, "tail": str(exc)[:200]})
            continue

        try:
            p = subprocess.run(argv, capture_output=True, text=True, shell=False,
                               timeout=a.timeout)
            ran.append({"op": item["op"], "label": item["label"],
                        "instrument": item.get("instrument"), "code": p.returncode,
                        "ok": p.returncode == 0,
                        "tail": (p.stdout or p.stderr or "").strip()[-400:]})
        except subprocess.TimeoutExpired:
            ran.append({"op": item["op"], "label": item["label"], "code": None,
                        "ok": False, "tail": f"exceeded {a.timeout}s"})
        # One failure must not abandon the rest: partial collection beats none, and the
        # two jobs are independent.

    ok = all(r["ok"] for r in ran)
    print(json.dumps({**head, "status": "OK" if ok else "PARTIAL", "ran": ran},
                     indent=2, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
