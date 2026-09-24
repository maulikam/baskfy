#!/usr/bin/env python
"""A paper day per FO sleeve through the desk's real path, against Postgres (FO11).

    cd kite-momentum-rebalancer
    BASKFY_TEST_DATABASE_URL=postgresql+asyncpg://baskfy:baskfy@localhost:5433/baskfy_test \\
        .venv/bin/python ../tools/fno/drill.py

`docs/fno/06` FO11 (after the options pack's OP13 drill): each sleeve's life on the FO7 replay
fixtures, and the line `sleeve=F1N confirms=1 fills=8 orders_to_broker=0` (and F1B, F2) with the
`fo_journal` row it closed with.

WHAT A DAY IS. The FO7 fixture month (``kite-momentum-rebalancer/tests/test_fno_monitor.py``: a
November monthly on 24 Nov 2026, the NFO master, Kite books with depth, the F&O bhavcopy prints):

* **F1N / F1B** — the nightly scan's condor (``fo_scan``) is raised by the real
  ``fno_monitor.FnoMonitor`` at 09:20 on its entry session (15 sessions before expiry), re-priced
  on the fixture's live quotes, the margin checked; it is confirmed through the real
  ``POST /fno/execute`` route (``app.fno_desk``) with ``confirm=true``, which runs
  ``fno_execute.execute_entry`` over the desk's FO gateway (paper-pinned, the covered-overnight
  rule wired): longs first, four simulated fills. It is **carried**: the night's bhavcopy lands
  and ``nightly`` writes a ``fo_mark`` at the settle. It **exits** at 15:00 on E-1 (23 Nov), an
  EXIT plan under the entry's confirm, shorts first, four more fills, and ``fo_journal`` gets its
  row.
* **F2** — RELIANCE's breakout is raised at 09:20 the next session, confirmed through the same
  route (the future bought, its GTT stop resting under a paper handle the same session); the
  evening **trail** moves the GTT through the gateway's modify (never lower); at 15:00 on E-1 the
  **roll** sells November and buys December under the original confirm, the stop carried; the
  **exit** is the 40-session time exit at 15:00, and ``fo_journal`` gets its row.

SAFETY. ``DRY_RUN`` is forced true in this process and every FO switch false; the broker is a
recorder that must stay empty (the drill exits non-zero if anything reached it). The database
must be a test database (its name contains ``test``) unless ``--allow-any-db``. Each sleeve runs
under a throwaway ``app_user``, deleted with everything it owns unless ``--keep``.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import sys
import tempfile
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DESK = ROOT / "kite-momentum-rebalancer"
DESK_TESTS = DESK / "tests"
for path in (DESK, DESK_TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app import config as C  # noqa: E402
from app import fno_desk  # noqa: E402
from app import fno_execute as X  # noqa: E402
from app import fno_monitor as M  # noqa: E402
from baskfy_core.fno.config import FoSleeve  # noqa: E402
from test_fno_monitor import (  # noqa: E402 - the FO7 fixture month
    F1_ENTRY,
    F2_ENTRY,
    FUT_DEC,
    FUT_NOV,
    HARD_EXIT,
    LOT,
    NEUTRAL,
    NOV,
    SESSIONS,
    STRIKES,
    TYPES,
    FakeInstruments,
    World,
    at,
    before,
    book,
)

FO_SWITCHES = ("OPTIONS_ENABLED", "FNO_CARRY_ENABLED", "FNO_F1_EXECUTION_ENABLED",
               "FNO_F2_EXECUTION_ENABLED", "INTRADAY_ENABLED")  # fmt: skip
UNDERLYING = {FoSleeve.F1N: "NIFTY", FoSleeve.F1B: "BANKNIFTY"}


class BothIndices(FakeInstruments):
    """The FO7 fixture master with BANKNIFTY listed on NIFTY's strikes, for F1B."""

    def option(self, underlying: str, expiry: dt.date, strike: Decimal,
               option_type: str) -> M.Contract | None:  # fmt: skip
        if underlying not in UNDERLYING.values() or expiry != NOV:
            return None
        role = next((r for r, k in STRIKES.items() if k == strike and TYPES[r] == option_type),
                    None)  # fmt: skip
        if role is None:
            return None
        base = 7_000_000 if underlying == "NIFTY" else 7_500_000
        return M.Contract(f"{underlying}26NOV{STRIKES[role]}{TYPES[role]}", base + int(strike),
                          LOT, NOV)  # fmt: skip


@contextlib.contextmanager
def desk_route(w: World, now: list[dt.datetime]) -> Iterator[Any]:
    """The real `POST /fno/execute`, its seams pointed at the fixture world (restored after)."""
    from app import main  # noqa: PLC0415
    from fastapi.testclient import TestClient  # noqa: PLC0415

    @contextlib.contextmanager
    def store() -> Iterator[X.FoStore]:
        yield w.store

    seams = {"open_store": store, "_now": lambda: now[0], "gateway_for": w.gateway_for,
             "quotes": lambda: w.quotes, "view_sources": lambda: (w.quotes, None, None)}
    saved = {name: getattr(fno_desk, name) for name in seams}
    for name, value in seams.items():
        setattr(fno_desk, name, value)
    try:
        # Same-origin, as a browser posts it (core/websec refuses a POST without an Origin).
        yield TestClient(main.app, headers={"Origin": "http://testserver:8420"})
    finally:
        for name, value in saved.items():
            setattr(fno_desk, name, value)


def _count(w: World, sql: str) -> int:
    return int(w.rows(sql, w.uid)[0]["n"])


def _events(w: World) -> list[str]:
    out: list[str] = []
    for path in sorted(w.tmp.glob("*.jsonl")):
        out += [str(json.loads(x).get("event")) for x in path.read_text().splitlines() if x]
    return out


def _confirm(w: World, plan_id: str, now: dt.datetime) -> dict[str, Any]:
    clock = [now]
    with desk_route(w, clock) as client:
        r = client.post("/fno/execute", data={"plan_id": plan_id, "confirm": "true"})
    body: dict[str, Any] = r.json()
    body["http"] = r.status_code
    return body


def day_f1(w: World, sleeve: FoSleeve) -> dict[str, Any]:
    underlying = UNDERLYING[sleeve]
    legs = [{"entry_seq": i, "role": r, "strike": str(STRIKES[r]), "option_type": TYPES[r],
             "expiry": NOV.isoformat(), "qty_sign": 1 if r.startswith("LONG") else -1}
            for i, r in enumerate(("LONG_PUT", "LONG_CALL", "SHORT_PUT", "SHORT_CALL"), 1)]
    w.scan(sleeve.value, before(F1_ENTRY, 1), underlying, {
        "underlying": underlying, "entry_session": F1_ENTRY.isoformat(),
        "expiry": NOV.isoformat(), "hard_exit_date": HARD_EXIT.isoformat(), "legs": legs,
        "lot_size": LOT,
    })  # fmt: skip
    w.quotes.books.update({underlying + s.removeprefix("NIFTY"): q for s, q in NEUTRAL.items()})
    (plan_id,) = w.tick(at(F1_ENTRY, 9, 20)).raised
    body = _confirm(w, plan_id, at(F1_ENTRY, 9, 25))
    out: dict[str, Any] = {"day": F1_ENTRY.isoformat(), "entry": body.get("outcome")}
    if body.get("outcome") != "OPEN":
        return out
    nxt = SESSIONS[SESSIONS.index(F1_ENTRY) + 1]
    for day, marks in ((F1_ENTRY, ("58", "54", "12", "10")), (nxt, ("50", "50", "10", "9"))):
        w.market.landed_days.add(day)
        w.market.day_prints.setdefault(day, {}).update({
            (underlying, NOV, Decimal(STRIKES[r]), TYPES[r]): M.Print(Decimal(m), Decimal(m))
            for r, m in zip(("SHORT_CALL", "SHORT_PUT", "LONG_CALL", "LONG_PUT"), marks,
                            strict=True)
        })  # fmt: skip
    out["marks"] = w.night(nxt).marked
    actions = w.tick(at(HARD_EXIT, 15, 0)).actions
    pos = w.position(int(body["position_id"]))
    out["exit"] = f"{pos['closed_reason']} at {HARD_EXIT} 15:00 ({','.join(actions)})"
    return out


def day_f2(w: World) -> dict[str, Any]:
    w.scan("F2", before(F2_ENTRY, 1), "RELIANCE", {
        "contract_expiry": NOV.isoformat(), "atr14": "20.00", "lot_size": 500,
        "industry": "Oil", "stop": "1340.00",
    }, rv20="0.250000")  # fmt: skip
    w.quotes.books[FUT_NOV] = book("1399.90", "1400.00", last="1400.00")
    (plan_id,) = w.tick(at(F2_ENTRY, 9, 20)).raised
    body = _confirm(w, plan_id, at(F2_ENTRY, 9, 22))
    out: dict[str, Any] = {"day": F2_ENTRY.isoformat(), "entry": body.get("outcome")}
    if body.get("outcome") != "OPEN":
        return out
    pid = int(body["position_id"])
    out["gtt"] = w.position(pid)["gtt_id"]
    w.market.land_future(F2_ENTRY, NOV, close="1450", settle="1449")
    out["trail"] = ",".join(w.night(F2_ENTRY).trailed)
    w.quotes.books[FUT_NOV] = book("1430.00", "1430.10", last="1430.05")
    w.quotes.books[FUT_DEC] = book("1438.00", "1438.10", last="1438.05")
    out["roll"] = ",".join(w.tick(at(HARD_EXIT, 15, 0)).actions)
    time_exit = SESSIONS[SESSIONS.index(F2_ENTRY) + 39]
    actions = w.tick(at(time_exit, 15, 0)).actions
    out["exit"] = f"{w.position(pid)['closed_reason']} at {time_exit} 15:00 ({','.join(actions)})"
    return out


def run(conn: Any, sleeve: FoSleeve, tmp: Path) -> dict[str, Any]:  # noqa: ANN401
    tag = uuid.uuid4().hex[:8]
    uid = int(conn.execute(
        "INSERT INTO app_user (public_id, email) VALUES (?, ?) RETURNING id",
        (f"fo-drill-{tag}", f"fo-drill-{tag}@drill.invalid"),
    ).fetchone()["id"])  # fmt: skip
    conn.execute(
        "INSERT INTO fo_sleeve_config (user_id, sleeve, capital_inr) VALUES (?, 'F1', 2500000), "
        "(?, 'F2', 0)",
        (uid, uid),
    )
    where = tmp / f"{sleeve.value}-{tag}"
    where.mkdir(parents=True)
    w = World(conn, uid, where)
    w.monitor.instruments = BothIndices()
    out = day_f2(w) if sleeve is FoSleeve.F2 else day_f1(w, sleeve)
    out.update(
        sleeve=sleeve.value, user_id=uid, broker=w.kc.calls, events=_events(w),
        confirms=_count(w, "SELECT count(*) AS n FROM fo_plan WHERE user_id = ? AND "
                           "kind = 'ENTRY' AND confirmed_at IS NOT NULL"),
        fills=_count(w, "SELECT count(*) AS n FROM fo_fill WHERE user_id = ?"),
        simulated=_count(w, "SELECT count(*) AS n FROM fo_fill WHERE user_id = ? AND "
                            "simulated"),
        journal=w.rows(
            "SELECT sleeve, symbol, structure, opened_on, closed_on, entry_inr, exit_inr, "
            "gross_pnl_inr, costs_inr, net_pnl_inr, r_multiple, closed_reason, sessions_held, "
            "rolls, simulated, sizing_mode FROM fo_journal WHERE user_id = ?", uid),
    )  # fmt: skip
    return out


def _pg_url(explicit: str | None) -> str:
    url = explicit or os.environ.get("BASKFY_TEST_DATABASE_URL") or ""
    if not url:
        raise SystemExit("set BASKFY_TEST_DATABASE_URL or pass --database-url")
    return url.replace("postgresql+asyncpg://", "postgresql://")


def isolate(tmp: Path) -> None:
    """The suite's conftest isolation, for a process that is not a test: the desk's own database
    and order journal are never touched. ``app.main`` migrates and reads the desk database it is
    configured with when it is imported, so this runs before the route is first built."""
    from app.analytics import db  # noqa: PLC0415
    from app.core import gateway  # noqa: PLC0415

    db.DB_BACKEND = "sqlite"
    C.DB_PATH = str(tmp / "portfolio.db")
    gateway.JOURNAL = str(tmp / "orders_journal.jsonl")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--database-url")
    parser.add_argument("--allow-any-db", action="store_true")
    parser.add_argument("--keep", action="store_true", help="keep the drill's rows")
    args = parser.parse_args(argv)
    url = _pg_url(args.database_url)
    name = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if "test" not in name and not args.allow_any_db:
        raise SystemExit(f"refusing a database whose name has no 'test' in it: {name}")
    C.DRY_RUN = True
    for switch in FO_SWITCHES:
        setattr(C, switch, False)
    from app.analytics.pg import Connection  # noqa: PLC0415 - psycopg only here

    conn = Connection(url)
    results: list[dict[str, Any]] = []
    failed = False
    try:
        with tempfile.TemporaryDirectory(prefix="fo-drill-") as tmp:
            isolate(Path(tmp))
            for sleeve in (FoSleeve.F1N, FoSleeve.F1B, FoSleeve.F2):
                result = run(conn, sleeve, Path(tmp))
                results.append(result)
                calls = len(result["broker"])
                broker_events = [e for e in result["events"] if e in (
                    "placed", "rejected", "error", "gtt_placed", "gtt_modified", "gtt_deleted")]
                failed |= calls != 0 or bool(broker_events)
                failed |= result["simulated"] != result["fills"] or not result["journal"]
                failed |= result["confirms"] != 1
                extra = " ".join(f"{k}={result[k]}" for k in ("gtt", "trail", "roll", "marks")
                                 if result.get(k) not in (None, ""))  # fmt: skip
                print(f"sleeve={result['sleeve']} confirms={result['confirms']} "
                      f"fills={result['fills']} orders_to_broker={calls} day={result['day']} "
                      f"entry={result['entry']} {extra} exit={result.get('exit', '-')}".replace(
                          "  ", " "))  # fmt: skip
                for row in result["journal"]:
                    print("  journal " + " ".join(f"{k}={v}" for k, v in row.items()))
    finally:
        if not args.keep:
            for result in results:
                conn.execute("DELETE FROM app_user WHERE id = ?", (result["user_id"],))
        conn.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
